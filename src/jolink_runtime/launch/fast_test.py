"""Isolated Java 8 Fast Test Runner and structured local protocol."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .contracts import BuildOperationSpec
from .process_supervisor import AttemptToken, ProcessSupervisor


class FastTestError(RuntimeError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.context = dict(context or {})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class FastTestAssets:
    runner_jar: Path
    runner_main_class: str
    java_minimum: int

    @classmethod
    def load(cls) -> "FastTestAssets":
        package = Path(__file__).parent
        try:
            lock_raw = (package / "fast-test-assets.json").read_bytes()
            lock = json.loads(lock_raw)
            runner = base64.b64decode(
                (package / "fast-test-runner.jar.b64").read_text(
                    encoding="ascii"
                ),
                validate=False,
            )
            expected = str(lock["test_runner"]["sha256"])
            main_class = str(lock["test_runner"]["main_class"])
            java_minimum = int(lock["java_minimum"])
            class_major = int(lock["class_major"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            raise FastTestError(
                "FAST_TEST_ASSETS_UNAVAILABLE",
                "The bundled Fast Test assets are unavailable.",
            ) from error
        if _sha256(runner) != expected:
            raise FastTestError(
                "FAST_TEST_ASSET_INTEGRITY_MISMATCH",
                "The bundled Fast Test Runner does not match its lock.",
            )
        try:
            with zipfile.ZipFile(__import__("io").BytesIO(runner)) as archive:
                majors = {
                    int.from_bytes(archive.read(name)[6:8], "big")
                    for name in archive.namelist()
                    if name.endswith(".class")
                }
        except (ValueError, zipfile.BadZipFile) as error:
            raise FastTestError(
                "FAST_TEST_ASSET_INTEGRITY_MISMATCH",
                "The bundled Fast Test Runner is invalid.",
            ) from error
        if majors != {class_major}:
            raise FastTestError(
                "FAST_TEST_ASSET_INTEGRITY_MISMATCH",
                "The bundled Fast Test Runner uses an unexpected Java level.",
            )
        root = (
            Path.home()
            / ".cache/jolink-runtime/fast-test/runner"
            / expected
        )
        destination = root / "jolink-test-runner.jar"
        if not destination.is_file() or _sha256(destination.read_bytes()) != expected:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = destination.with_name(
                f".{destination.name}.{os.getpid()}.tmp"
            )
            temporary.write_bytes(runner)
            os.replace(temporary, destination)
        return cls(
            runner_jar=destination,
            runner_main_class=main_class,
            java_minimum=java_minimum,
        )


def _contains_class(entry: Path, relative: str) -> bool:
    if entry.is_dir():
        return (entry / relative).is_file()
    if not entry.is_file() or entry.suffix.casefold() != ".jar":
        return False
    try:
        with zipfile.ZipFile(entry) as archive:
            return relative in archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


_JUNIT_PLATFORM_LAUNCHER_CLASSES = (
    "org/junit/platform/launcher/core/LauncherFactory.class",
    "org/junit/platform/launcher/listeners/SummaryGeneratingListener.class",
)


def _numeric_version(value: str) -> tuple[int, ...] | None:
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", value)
    if match is None:
        return None
    return tuple(int(part or 0) for part in match.groups())


def complete_test_runtime_classpath(
    classpath: Sequence[Path],
) -> tuple[Path, ...]:
    """Add the exact JUnit Platform launcher companion when Maven omitted it.

    Maven Surefire commonly supplies ``junit-platform-launcher`` from its
    plugin realm instead of the project's test classpath.  Fast Test does not
    execute Surefire, so it needs that one runner-side companion.  Reuse only
    an already resolved artifact from the same Maven-repository directory and
    exact version as ``junit-platform-engine``; never download or guess a
    different JUnit version.
    """

    normalized = tuple(path.expanduser().resolve(strict=True) for path in classpath)
    if all(
        any(_contains_class(entry, name) for entry in normalized)
        for name in _JUNIT_PLATFORM_LAUNCHER_CLASSES
    ):
        return normalized

    engine_class = "org/junit/platform/engine/TestEngine.class"
    for engine in normalized:
        if not _contains_class(engine, engine_class) or not engine.is_file():
            continue
        version_directory = engine.parent
        artifact_directory = version_directory.parent
        if artifact_directory.name != "junit-platform-engine":
            continue
        launcher_root = artifact_directory.parent / "junit-platform-launcher"
        engine_version = _numeric_version(version_directory.name)
        if engine_version is None or not launcher_root.is_dir():
            continue
        candidates: list[tuple[tuple[int, ...], Path]] = []
        for candidate in sorted(launcher_root.glob("*/*.jar")):
            launcher_version = _numeric_version(candidate.parent.name)
            if (
                launcher_version is None
                or launcher_version[0] != engine_version[0]
                or launcher_version < engine_version
                or not all(
                    _contains_class(candidate, name)
                    for name in _JUNIT_PLATFORM_LAUNCHER_CLASSES
                )
            ):
                continue
            candidates.append((launcher_version, candidate.resolve()))
        if candidates:
            # A newer launcher uses the stable Platform engine SPI and can
            # execute an older engine. Prefer the closest available version.
            return (*normalized, min(candidates, key=lambda item: item[0])[1])
    return normalized


def detect_test_framework(classpath: Sequence[Path]) -> str:
    has_junit5 = all(
        any(_contains_class(entry, name) for entry in classpath)
        for name in (
            _JUNIT_PLATFORM_LAUNCHER_CLASSES[0],
            "org/junit/platform/engine/discovery/DiscoverySelectors.class",
            _JUNIT_PLATFORM_LAUNCHER_CLASSES[1],
        )
    )
    has_junit4 = any(
        _contains_class(entry, "org/junit/runner/JUnitCore.class")
        for entry in classpath
    )
    has_testng = all(
        any(_contains_class(entry, name) for entry in classpath)
        for name in (
            "org/testng/TestNG.class",
            "org/testng/TestListenerAdapter.class",
            "org/testng/xml/XmlSuite.class",
        )
    )
    available = sum((has_junit5, has_junit4, has_testng))
    if available > 1:
        return "auto"
    if has_junit5:
        return "junit5"
    if has_junit4:
        return "junit4"
    if has_testng:
        return "testng"
    raise FastTestError(
        "TEST_FRAMEWORK_UNAVAILABLE",
        "The project test runtime classpath has no supported JUnit/TestNG runner.",
    )


def _manifest_line(name: str, value: str) -> bytes:
    raw = f"{name}: {value}".encode("ascii")
    lines: list[bytes] = []
    first = True
    while raw:
        limit = 70 if first else 69
        chunk, raw = raw[:limit], raw[limit:]
        lines.append((b"" if first else b" ") + chunk)
        first = False
    return b"\r\n".join(lines) + b"\r\n"


def _create_pathing_jar(
    destination: Path,
    *,
    main_class: str,
    classpath: Sequence[Path],
) -> None:
    urls: list[str] = []
    for path in classpath:
        uri = path.as_uri()
        if path.is_dir() and not uri.endswith("/"):
            uri += "/"
        urls.append(uri)
    manifest = (
        _manifest_line("Manifest-Version", "1.0")
        + _manifest_line("Main-Class", main_class)
        + _manifest_line("Class-Path", " ".join(urls))
        + b"\r\n"
    )
    temporary = destination.with_name(f".{destination.name}.tmp")
    with zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        info = zipfile.ZipInfo(
            "META-INF/MANIFEST.MF", (2026, 8, 29, 0, 0, 0)
        )
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, manifest)
    os.replace(temporary, destination)


@dataclass(frozen=True)
class FastTestResult:
    test_run_id: str
    framework: str
    tests: int
    passed_count: int
    failed_count: int
    failed_test_count: int
    failed_container_count: int
    skipped_count: int
    passed: bool
    duration_ms: float
    failures: tuple[dict[str, Any], ...]
    failures_truncated: bool
    log_path: Path


class FastTestRunner:
    def __init__(self, supervisor: ProcessSupervisor | None = None) -> None:
        self._supervisor = supervisor or ProcessSupervisor()

    def run(
        self,
        *,
        java_executable: Path,
        classpath: Sequence[Path],
        selectors: Sequence[str],
        working_directory: Path,
        attempt_directory: Path,
        timeout_seconds: float,
        owner: AttemptToken,
        framework: str | None = None,
        environment: dict[str, str] | None = None,
    ) -> FastTestResult:
        if not selectors or len(selectors) > 64:
            raise FastTestError(
                "INVALID_TEST_SELECTOR",
                "Fast Test requires between 1 and 64 explicit selectors.",
            )
        for selector in selectors:
            class_name, marker, method = selector.partition("#")
            if (
                not class_name
                or not all(
                    part
                    and (part[0].isalpha() or part[0] in "_$")
                    and all(char.isalnum() or char in "_$" for char in part)
                    for part in class_name.split(".")
                )
                or (marker and not method)
            ):
                raise FastTestError(
                    "INVALID_TEST_SELECTOR",
                    "A Fast Test selector must be Class or Class#method.",
                )
        assets = FastTestAssets.load()
        normalized = complete_test_runtime_classpath(classpath)
        detected_framework = detect_test_framework(normalized)
        if framework is None:
            framework = detected_framework
        elif framework not in {"junit4", "junit5", "testng"}:
            raise FastTestError(
                "TEST_FRAMEWORK_UNAVAILABLE",
                "The build authority selected an unsupported test framework.",
            )
        elif detected_framework not in {framework, "auto"}:
            raise FastTestError(
                "TEST_FRAMEWORK_UNAVAILABLE",
                "The selected test framework is unavailable on the runtime classpath.",
            )
        attempt_directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        selectors_file = attempt_directory / "selectors.private.txt"
        selectors_file.write_text(
            "".join(f"{selector}\n" for selector in selectors),
            encoding="utf-8",
        )
        classpath_file = attempt_directory / "classpath.private.txt"
        if any("\n" in str(path) or "\r" in str(path) for path in normalized):
            raise FastTestError(
                "TEST_CLASSPATH_INVALID",
                "A Fast Test classpath entry contains a line break.",
            )
        classpath_file.write_text(
            "".join(f"{path}\n" for path in normalized),
            encoding="utf-8",
        )
        pathing_jar = attempt_directory / "fast-test-pathing.jar"
        _create_pathing_jar(
            pathing_jar,
            main_class=assets.runner_main_class,
            classpath=(assets.runner_jar, *normalized),
        )
        test_run_id = f"test_{uuid.uuid4().hex[:12]}"
        token = secrets.token_hex(32)
        log = attempt_directory / "test.log"
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        server.settimeout(min(max(timeout_seconds, 1.0), 600.0))
        port = int(server.getsockname()[1])
        frames: list[dict[str, Any]] = []
        listener_error: list[BaseException] = []

        def listen() -> None:
            deadline = time.monotonic() + timeout_seconds + 2.0
            try:
                while time.monotonic() < deadline:
                    try:
                        connection, _address = server.accept()
                    except socket.timeout:
                        if frames:
                            return
                        raise
                    with connection, connection.makefile(
                        "r", encoding="utf-8", errors="strict"
                    ) as stream:
                        for line in stream:
                            if len(line) > 256 * 1024:
                                raise FastTestError(
                                    "TEST_RUNNER_PROTOCOL_ERROR",
                                    "A Test Runner protocol frame is too large.",
                                )
                            frame = json.loads(line)
                            if (
                                not isinstance(frame, dict)
                                or frame.get("token") != token
                                or frame.get("test_run_id") != test_run_id
                            ):
                                raise FastTestError(
                                    "TEST_RUNNER_PROTOCOL_ERROR",
                                    "A Test Runner frame has invalid identity.",
                                )
                            frames.append(frame)
                            if frame.get("event") in {
                                "run_finished",
                                "infrastructure_failed",
                            }:
                                return
                    # Infrastructure exceptions reconnect immediately after
                    # closing the first channel. A killed/System.exit Runner
                    # never reconnects, so do not wait for the full test timeout.
                    server.settimeout(0.25)
            except BaseException as error:
                listener_error.append(error)
            finally:
                server.close()

        listener = threading.Thread(
            target=listen,
            name=f"jolink-test-protocol-{test_run_id}",
            daemon=True,
        )
        listener.start()
        command = (
            str(java_executable),
            "-ea",
            "-jar",
            str(pathing_jar),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--token",
            token,
            "--run-id",
            test_run_id,
            "--framework",
            framework,
            "--selectors-file",
            str(selectors_file),
            "--classpath-file",
            str(classpath_file),
        )
        started = time.monotonic()
        operation = self._supervisor.run(
            BuildOperationSpec(
                argv=command,
                cwd=working_directory,
                timeout_seconds=timeout_seconds,
                output_capture=log,
                environment=dict(environment or {}),
                max_output_bytes=4 * 1024 * 1024,
                operation_name="jdt_fast_test",
            ),
            owner=owner,
        )
        listener.join(2.0)
        if listener.is_alive():
            server.close()
            listener.join(1.0)
        if operation.timed_out:
            raise FastTestError(
                "TEST_TIMEOUT",
                "The selected tests exceeded their timeout.",
                context={"test_process_terminated": bool(operation.termination)},
            )
        if operation.output_limit_exceeded:
            raise FastTestError(
                "TEST_OUTPUT_LIMIT_EXCEEDED",
                "The selected tests exceeded the 4 MiB output limit.",
                context={"test_process_terminated": bool(operation.termination)},
            )
        if (
            operation.termination is not None
            and not operation.termination.terminated
        ):
            raise FastTestError(
                "TEST_PROCESS_TREE_NOT_SETTLED",
                "The Test Runner process tree did not settle.",
                context={
                    "remaining_process_count": len(
                        operation.termination.remaining_pids
                    )
                },
            )
        if listener_error:
            error = listener_error[0]
            if isinstance(error, FastTestError):
                raise error
            raise FastTestError(
                "TEST_RUNNER_PROTOCOL_ERROR",
                "The Test Runner protocol failed.",
            ) from error
        terminal = next(
            (
                frame
                for frame in reversed(frames)
                if frame.get("event")
                in {"run_finished", "infrastructure_failed"}
            ),
            None,
        )
        if terminal is not None and terminal.get("event") == "infrastructure_failed":
            raise FastTestError(
                "TEST_RUNNER_FAILED",
                "The project test framework could not execute the selection.",
                context={
                    "error_type": terminal.get("error_type"),
                    "message": terminal.get("message"),
                    "return_code": operation.return_code,
                },
            )
        if terminal is None or operation.return_code not in {0, None}:
            raise FastTestError(
                "TEST_RUNNER_FAILED",
                "The Test Runner exited without a valid terminal result.",
                context={"return_code": operation.return_code},
            )
        failures = terminal.get("failures", [])
        if not isinstance(failures, list):
            raise FastTestError(
                "TEST_RUNNER_PROTOCOL_ERROR",
                "The Test Runner result has invalid failures.",
            )
        test_count = int(terminal.get("tests", 0))
        failed_count = int(terminal.get("failed_count", 0))
        if test_count <= 0 and failed_count <= 0:
            raise FastTestError(
                "TEST_SELECTION_EMPTY",
                "The selected framework discovered no matching tests.",
            )
        visible_failures = tuple(
            dict(value) for value in failures[:8] if isinstance(value, dict)
        )
        return FastTestResult(
            test_run_id=test_run_id,
            framework=str(terminal.get("framework", framework)),
            tests=test_count,
            passed_count=int(terminal.get("passed_count", 0)),
            failed_count=failed_count,
            failed_test_count=int(terminal.get("failed_test_count", failed_count)),
            failed_container_count=int(
                terminal.get("failed_container_count", 0)
            ),
            skipped_count=int(terminal.get("skipped_count", 0)),
            passed=terminal.get("passed") is True,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
            failures=visible_failures,
            failures_truncated=failed_count > len(visible_failures),
            log_path=log,
        )

    def close(self, *, deadline: float | None = None) -> bool:
        effective_deadline = (
            deadline if deadline is not None else time.monotonic() + 5.0
        )
        return self._supervisor.close(
            deadline=effective_deadline
        ).settled


__all__ = [
    "FastTestAssets",
    "FastTestError",
    "FastTestResult",
    "FastTestRunner",
    "detect_test_framework",
]
