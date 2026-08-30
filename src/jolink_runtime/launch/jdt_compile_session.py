"""Persistent headless JDT compiler session used by Java project reload.

Maven/IDE discovery supplies the frozen Build World.  This module owns only
the verified Equinox Worker process, private source mirror, JavaBuilder state,
and compile result.  Mutable Worker output is never a publishable Generation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from .process_tree import ProcessTreeHandle, ProcessTreeTerminator
from .fast_compile import fast_compile_fingerprint


class JdtCompileError(RuntimeError):
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class WorkerJavaRuntime:
    home: Path
    executable: Path
    major: int
    data_model: int


def lombok_worker_jvm_arguments(
    worker_major: int,
    *,
    lombok_enabled: bool,
) -> tuple[str, ...]:
    if not lombok_enabled or worker_major < 9:
        return ()
    return ("--add-opens=java.base/java.lang=ALL-UNNAMED",)


@dataclass(frozen=True)
class JdtCandidate:
    candidate_id: str
    root: Path
    launcher: Path
    worker_java_sha256: str | None
    lock: dict[str, Any]
    worker_java_minimum: int = 17
    worker_class_major: int = 61
    _product_install_lock = threading.Lock()

    @classmethod
    def load_product(cls) -> "JdtCandidate":
        """Load or atomically install the content-addressed product candidate."""

        lock_path = Path(__file__).with_name("jdt-product-candidate.json")
        try:
            lock_raw = lock_path.read_bytes()
            lock = json.loads(lock_raw)
            candidate_id = str(lock["candidate_id"])
            if Path(candidate_id).name != candidate_id:
                raise ValueError("invalid candidate_id")
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise JdtCompileError(
                "JDT_CANDIDATE_LOCK_INVALID",
                "The JDT Worker product lock is unavailable or invalid.",
            ) from error
        identity = hashlib.sha256(lock_raw).hexdigest()
        cache_base = Path.home() / ".cache/jolink-runtime"
        product_root = (
            cache_base
            / "jdt-worker/candidates"
            / candidate_id
            / identity
        )
        try:
            return cls._load_root(lock, product_root)
        except JdtCompileError:
            pass
        with cls._product_install_lock:
            try:
                return cls._load_root(lock, product_root)
            except JdtCompileError:
                cls._install_product_candidate(
                    lock,
                    product_root=product_root,
                    legacy_roots=(
                        cache_base / "jdt-worker/candidates" / candidate_id,
                        cache_base / "jdt-poc/candidates" / candidate_id,
                    ),
                )
                return cls._load_root(lock, product_root)

    @classmethod
    def load(cls, lock_path: Path, cache_root: Path) -> "JdtCandidate":
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            candidate_id = str(lock["candidate_id"])
            if Path(candidate_id).name != candidate_id:
                raise ValueError("invalid candidate_id")
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise JdtCompileError(
                "JDT_CANDIDATE_LOCK_INVALID",
                "The JDT Worker candidate lock is unavailable or invalid.",
            ) from error
        root = cache_root.expanduser().resolve(strict=False) / "candidates" / (
            candidate_id
        )
        return cls._load_root(lock, root)

    @classmethod
    def _load_root(
        cls,
        lock: dict[str, Any],
        root: Path,
    ) -> "JdtCandidate":
        try:
            candidate_id = str(lock["candidate_id"])
            artifacts = [*lock["artifacts"], lock["worker_artifact"]]
            equinox = lock["equinox"]
            worker_java_sha256 = (
                str(
                    lock["worker_build"]["java_home_identity"][
                        "java_binary_sha256"
                    ]
                )
                if int(lock.get("schema_version", 0)) >= 2
                else None
            )
            worker_java_minimum = int(lock.get("worker_java_minimum", 17))
            worker_class_major = int(lock.get("worker_class_major", 61))
        except (KeyError, TypeError, ValueError) as error:
            raise JdtCompileError(
                "JDT_CANDIDATE_LOCK_INVALID",
                "The JDT Worker candidate lock is unavailable or invalid.",
            ) from error
        root = root.expanduser().resolve(strict=False)
        for artifact in artifacts:
            filename = str(artifact["filename"])
            if Path(filename).name != filename:
                raise JdtCompileError(
                    "JDT_CANDIDATE_LOCK_INVALID",
                    "The JDT Candidate lock contains an invalid artifact name.",
                )
            path = root / "plugins" / filename
            if (
                not path.is_file()
                or _sha256_file(path) != str(artifact["sha256"])
            ):
                raise JdtCompileError(
                    "JDT_CANDIDATE_INTEGRITY_MISMATCH",
                    f"Locked JDT artifact is missing or changed: {filename}.",
                    context={"artifact": filename},
                )
        config = root / "configuration/config.ini"
        if (
            not config.is_file()
            or _sha256_file(config)
            != str(equinox["configuration_sha256"])
        ):
            raise JdtCompileError(
                "JDT_CANDIDATE_INTEGRITY_MISMATCH",
                "The locked Equinox configuration is missing or changed.",
                context={"artifact": "configuration/config.ini"},
            )
        launcher = root / "plugins" / str(equinox["launcher_filename"])
        worker_path = root / "plugins" / str(lock["worker_artifact"]["filename"])
        observed_majors: set[int] = set()
        try:
            with zipfile.ZipFile(worker_path) as archive:
                for name in archive.namelist():
                    if not name.endswith(".class"):
                        continue
                    raw = archive.read(name)
                    if len(raw) < 8 or raw[:4] != b"\xca\xfe\xba\xbe":
                        raise ValueError("invalid class")
                    observed_majors.add(int.from_bytes(raw[6:8], "big"))
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            raise JdtCompileError(
                "JDT_CANDIDATE_INTEGRITY_MISMATCH",
                "The locked JDT Worker class version cannot be verified.",
                context={"artifact": worker_path.name},
            ) from error
        if not observed_majors or observed_majors != {worker_class_major}:
            raise JdtCompileError(
                "JDT_CANDIDATE_INTEGRITY_MISMATCH",
                "The locked JDT Worker class version does not match its lock.",
                context={"artifact": worker_path.name},
            )
        return cls(
            candidate_id=candidate_id,
            root=root,
            launcher=launcher,
            worker_java_sha256=worker_java_sha256,
            lock=lock,
            worker_java_minimum=worker_java_minimum,
            worker_class_major=worker_class_major,
        )

    @classmethod
    def _install_product_candidate(
        cls,
        lock: dict[str, Any],
        *,
        product_root: Path,
        legacy_roots: Sequence[Path],
    ) -> None:
        product_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = product_root.parent / (
            f".{product_root.name}.{uuid.uuid4().hex}.tmp"
        )
        plugins = temporary / "plugins"
        configuration = temporary / "configuration"
        plugins.mkdir(parents=True, mode=0o700)
        configuration.mkdir(mode=0o700)
        artifact_name = "candidate"
        try:
            repository = str(lock["repository_url"]).rstrip("/")
            for artifact in lock["artifacts"]:
                artifact_name = str(artifact["filename"])
                expected = str(artifact["sha256"])
                destination = plugins / artifact_name
                source = next(
                    (
                        root / "plugins" / artifact_name
                        for root in legacy_roots
                        if (root / "plugins" / artifact_name).is_file()
                        and _sha256_file(root / "plugins" / artifact_name)
                        == expected
                    ),
                    None,
                )
                if source is not None:
                    shutil.copyfile(source, destination)
                else:
                    cls._download_product_artifact(
                        f"{repository}/plugins/{artifact_name}",
                        destination,
                        artifact=artifact_name,
                    )
                if _sha256_file(destination) != expected:
                    raise JdtCompileError(
                        "JDT_CANDIDATE_INTEGRITY_MISMATCH",
                        f"Downloaded JDT artifact failed verification: {artifact_name}.",
                        context={"artifact": artifact_name},
                    )

            worker = lock["worker_artifact"]
            artifact_name = str(worker["filename"])
            worker_bytes = base64.b64decode(
                "".join(
                    Path(__file__).with_name(
                        "jdt-product-worker.jar.b64"
                    ).read_text(encoding="ascii").split()
                ),
                validate=True,
            )
            worker_path = plugins / artifact_name
            worker_path.write_bytes(worker_bytes)
            if _sha256_file(worker_path) != str(worker["sha256"]):
                raise JdtCompileError(
                    "JDT_CANDIDATE_INTEGRITY_MISMATCH",
                    "The packaged JDT Worker failed verification.",
                    context={"artifact": artifact_name},
                )

            config_path = configuration / "config.ini"
            shutil.copyfile(
                Path(__file__).with_name("jdt-product-config.ini"),
                config_path,
            )
            cls._load_root(lock, temporary)
            if product_root.exists():
                try:
                    cls._load_root(lock, product_root)
                except JdtCompileError:
                    product_root.rename(
                        product_root.with_name(
                            f".{product_root.name}.corrupt-{uuid.uuid4().hex}"
                        )
                    )
                else:
                    return
            try:
                temporary.rename(product_root)
            except OSError as error:
                # Another MCP server may have published the same immutable
                # lock identity after our existence check. Accept only a
                # fully verified winner; otherwise preserve the real failure.
                try:
                    cls._load_root(lock, product_root)
                except JdtCompileError:
                    raise error
        except JdtCompileError:
            raise
        except Exception as error:
            raise JdtCompileError(
                "JDT_CANDIDATE_INSTALL_FAILED",
                "The locked JDT Candidate could not be installed.",
                context={
                    "artifact": artifact_name,
                    "retryable": True,
                },
            ) from error
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def _download_product_artifact(
        url: str,
        destination: Path,
        *,
        artifact: str,
    ) -> None:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "joLink-Runtime/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                with destination.open("wb") as stream:
                    total = 0
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > 64 * 1024 * 1024:
                            raise JdtCompileError(
                                "JDT_CANDIDATE_INSTALL_FAILED",
                                "A JDT artifact exceeded the download limit.",
                                context={"artifact": artifact},
                            )
                        stream.write(chunk)
        except JdtCompileError:
            raise
        except Exception as error:
            raise JdtCompileError(
                "JDT_CANDIDATE_INSTALL_FAILED",
                f"Unable to install locked JDT artifact: {artifact}.",
                context={"artifact": artifact, "retryable": True},
            ) from error

    def verify_worker_java(self, java_home: Path) -> WorkerJavaRuntime:
        home = java_home.expanduser().resolve(strict=True)
        executable = home / "bin" / (
            "java.exe" if os.name == "nt" else "java"
        )
        if not executable.is_file():
            raise JdtCompileError(
                "JDT_WORKER_JDK_IDENTITY_MISMATCH",
                "The JDT Worker Java executable is unavailable.",
            )
        if self.worker_java_sha256 is not None:
            if _sha256_file(executable) != self.worker_java_sha256:
                raise JdtCompileError(
                    "JDT_WORKER_JDK_IDENTITY_MISMATCH",
                    "The JDT Worker JDK does not match the locked candidate.",
                )
        try:
            observed = subprocess.run(
                [str(executable), "-XshowSettings:properties", "-version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise JdtCompileError(
                "JDT_WORKER_JDK_IDENTITY_MISMATCH",
                "The JDT Worker JDK version could not be verified.",
            ) from error
        output = (observed.stderr or observed.stdout)
        match = re.search(
            r'version\s+"(?P<first>\d+)(?:\.(?P<second>\d+))?',
            output,
        )
        if match is None:
            raise JdtCompileError(
                "JDT_WORKER_JDK_IDENTITY_MISMATCH",
                "The JDT Worker JDK version output is unrecognized.",
            )
        major = int(match.group("first"))
        if major == 1 and match.group("second") is not None:
            major = int(match.group("second"))
        if major < self.worker_java_minimum:
            raise JdtCompileError(
                "JDT_WORKER_JDK_IDENTITY_MISMATCH",
                "The JDT Worker JDK is older than the locked minimum.",
            )
        data_model_match = re.search(
            r"(?m)^\s*sun\.arch\.data\.model\s*=\s*(32|64)\s*$",
            output,
        )
        if data_model_match is None:
            raise JdtCompileError(
                "JDT_WORKER_JDK_IDENTITY_MISMATCH",
                "The JDT Worker JDK data model could not be verified.",
            )
        return WorkerJavaRuntime(
            home=home,
            executable=executable,
            major=major,
            data_model=int(data_model_match.group(1)),
        )

    def select_worker_java(
        self,
        preferred: Iterable[Path] = (),
    ) -> WorkerJavaRuntime:
        candidates: list[Path] = [
            path.expanduser().resolve(strict=False) for path in preferred
        ]
        java_on_path = shutil.which("java")
        if java_on_path:
            candidates.append(Path(java_on_path).resolve().parent.parent)
        candidates.extend(Path.home().glob(".jdks/*"))
        candidates.extend(
            Path.home().glob(
                "Library/Java/JavaVirtualMachines/*/Contents/Home"
            )
        )
        candidates.extend(
            Path("/Library/Java/JavaVirtualMachines").glob("*/Contents/Home")
        )
        candidates.extend(Path("/usr/lib/jvm").glob("*"))
        for environment_name in ("JAVA_HOME",):
            value = os.environ.get(environment_name)
            if value:
                candidates.append(Path(value))
        seen: set[str] = set()
        compatible_32_bit = False
        for candidate in candidates:
            key = os.path.normcase(str(candidate))
            if key in seen:
                continue
            seen.add(key)
            try:
                runtime = self.verify_worker_java(candidate)
            except (OSError, JdtCompileError):
                continue
            if runtime.data_model != 64:
                compatible_32_bit = True
                continue
            return runtime
        if compatible_32_bit:
            raise JdtCompileError(
                "JDT_WORKER_64_BIT_JDK_UNAVAILABLE",
                "Only a 32-bit compatible Worker JDK was found; the product "
                "heap policy requires a 64-bit JDK.",
            )
        raise JdtCompileError(
            "JDT_WORKER_JDK_UNAVAILABLE",
            "The JDT Worker JDK used by the locked candidate was not found.",
        )

    def select_worker_java_home(
        self,
        preferred: Iterable[Path] = (),
    ) -> Path:
        """Compatibility wrapper for experiment callers."""

        return self.select_worker_java(preferred).home


class JdtWorkerClient:
    def __init__(
        self,
        process: subprocess.Popen[str],
        stderr_stream: Any,
        *,
        timeout: float,
        process_tree: ProcessTreeHandle | None = None,
    ) -> None:
        self.process = process
        self._stderr_stream = stderr_stream
        self.timeout = timeout
        self._process_tree = process_tree or ProcessTreeHandle.from_process(
            process  # type: ignore[arg-type]
        )
        self._terminator = ProcessTreeTerminator()
        self._close_lock = threading.Lock()
        self._closed = False
        self._frames: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self._frames.put(line.rstrip("\r\n"))
        finally:
            self._frames.put(None)

    def receive(self, timeout: float | None = None) -> dict[str, Any]:
        try:
            line = self._frames.get(
                timeout=self.timeout if timeout is None else timeout
            )
        except queue.Empty as error:
            raise JdtCompileError(
                "JDT_WORKER_TIMEOUT",
                "Timed out waiting for a JDT Worker response.",
            ) from error
        if line is None:
            raise JdtCompileError(
                "JDT_WORKER_EXITED",
                "The JDT Worker exited before returning a response.",
            )
        try:
            frame = json.loads(line)
        except json.JSONDecodeError as error:
            raise JdtCompileError(
                "JDT_WORKER_PROTOCOL_ERROR",
                "The JDT Worker returned a non-JSON protocol frame.",
            ) from error
        if not isinstance(frame, dict):
            raise JdtCompileError(
                "JDT_WORKER_PROTOCOL_ERROR",
                "The JDT Worker response is not an object.",
            )
        return frame

    def command(
        self,
        command: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if self.process.stdin is None:
            raise JdtCompileError(
                "JDT_WORKER_EXITED",
                "The JDT Worker input stream is unavailable.",
            )
        try:
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise JdtCompileError(
                "JDT_WORKER_EXITED",
                "The JDT Worker input stream closed unexpectedly.",
            ) from error
        return self.receive(timeout=timeout)

    def close(self) -> bool:
        with self._close_lock:
            if self._closed:
                return self.process.poll() is not None
            self._closed = True
        try:
            if self.process.poll() is None:
                try:
                    frame = self.command("STOP", timeout=5.0)
                    acknowledged = (
                        frame.get("ok") is True
                        and frame.get("status") == "stopped"
                    )
                except JdtCompileError:
                    acknowledged = False
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
                return acknowledged and self.process.poll() is not None
            return True
        finally:
            if not self._stderr_stream.closed:
                self._stderr_stream.close()

    def force_close(self) -> bool:
        with self._close_lock:
            if self._closed:
                return self.process.poll() is not None
            self._closed = True
        report = self._terminator.terminate(
            self._process_tree,
            deadline=time.monotonic() + 5.0,
            force=True,
        )
        if not self._stderr_stream.closed:
            self._stderr_stream.close()
        return report.terminated


@dataclass(frozen=True)
class JdtCompileResult:
    compile_ok: bool
    actual_build_kind: str | None
    compiled_source_count: int
    compiled_source_units: tuple[str, ...]
    changed_classes: tuple[str, ...]
    deleted_classes: tuple[str, ...]
    changed_resources: tuple[str, ...]
    deleted_resources: tuple[str, ...]
    error_count: int
    warning_count: int
    diagnostics: tuple[dict[str, Any], ...]
    diagnostics_truncated: bool
    elapsed_ms: float
    source_changes_pending: bool
    output_directory: Path
    candidate_changed_classes: tuple[str, ...] = ()
    candidate_deleted_classes: tuple[str, ...] = ()
    candidate_changed_resources: tuple[str, ...] = ()
    candidate_deleted_resources: tuple[str, ...] = ()
    main_compile_ok: bool = True
    test_compile_ok: bool = True
    main_error_count: int = 0
    test_error_count: int = 0
    main_warning_count: int = 0
    test_warning_count: int = 0
    test_output_directory: Path | None = None
    deleted_source_units: tuple[str, ...] = ()


@dataclass(frozen=True)
class JdtBuildWorldPlan:
    project_root: Path
    module_root: Path
    source_roots: tuple[Path, ...]
    dependency_entries: tuple[Path, ...]
    processor_entries: tuple[Path, ...]
    lombok_entries: tuple[Path, ...]
    target_java_home: Path
    source_encoding: str
    source_level: int
    target_level: int
    fingerprint: str
    configuration_inputs: tuple[Path, ...]
    configuration_environment_names: tuple[str, ...]
    javac_executable: Path
    method_parameters: bool = False
    freshness_entries: tuple[Path, ...] = ()
    resource_roots: tuple[Path, ...] = ()
    resource_fingerprint: str = ""
    worker_min_heap_mb: int = 64
    worker_max_heap_mb: int = 2048

    def is_fresh(self) -> bool:
        configuration_fresh = self.fingerprint == fast_compile_fingerprint(
            configuration_inputs=self.configuration_inputs,
            configuration_environment_names=(
                self.configuration_environment_names
            ),
            javac_executable=self.javac_executable,
            compile_classpath=(
                self.freshness_entries or self.dependency_entries
            ),
        )
        if not configuration_fresh:
            return False
        if not self.resource_roots:
            return True
        return self.resource_fingerprint == resource_tree_fingerprint(
            self.resource_roots
        )


def resource_tree_fingerprint(roots: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for root in roots:
        normalized = root.expanduser().resolve(strict=False)
        digest.update(
            str(normalized).encode("utf-8", errors="surrogateescape")
        )
        if not normalized.is_dir():
            digest.update(b"<absent>")
            continue
        for path in sorted(normalized.rglob("*")):
            if not path.is_file():
                continue
            digest.update(
                path.relative_to(normalized).as_posix().encode("utf-8")
            )
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"<unreadable>")
    return digest.hexdigest()


def discover_java8_system_entries(java_home: Path) -> tuple[Path, ...]:
    """Return the Java 8 boot/ext archives needed by the frozen JDT model."""

    home = java_home.expanduser().resolve(strict=True)
    runtime = home / "jre" if (home / "jre").is_dir() else home
    candidates = [
        runtime / "lib" / name
        for name in (
            "resources.jar",
            "rt.jar",
            "jsse.jar",
            "jce.jar",
            "charsets.jar",
            "jfr.jar",
        )
    ]
    for directory in (
        runtime / "lib" / "ext",
        runtime / "lib" / "endorsed",
        home / "lib" / "endorsed",
    ):
        if directory.is_dir():
            candidates.extend(sorted(directory.glob("*.jar")))
    entries: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        key = os.path.normcase(str(resolved))
        if not resolved.is_file() or key in seen:
            continue
        seen.add(key)
        entries.append(resolved)
    if not any(path.name == "rt.jar" for path in entries):
        raise JdtCompileError(
            "JDT_TARGET_PLATFORM_UNAVAILABLE",
            "The frozen product JDT model requires a Java 8 rt.jar platform.",
        )
    return tuple(entries)


def discover_target_system_entries(
    java_home: Path,
    target_level: int,
) -> tuple[Path, ...]:
    if int(target_level) == 8:
        return discover_java8_system_entries(java_home)
    if int(target_level) == 11:
        home = java_home.expanduser().resolve(strict=True)
        jrt = home / "lib/jrt-fs.jar"
        if not jrt.is_file():
            raise JdtCompileError(
                "JDT_TARGET_PLATFORM_UNAVAILABLE",
                "The Java 11 target JDK has no jrt-fs.jar system image.",
            )
        return (jrt,)
    raise JdtCompileError(
        "JDT_TARGET_PLATFORM_UNSUPPORTED",
        "The product JDT model supports Java target 8 or 11.",
    )


class PersistentJdtCompileSession:
    """One long-lived JavaBuilder state for one frozen Build World."""

    def __init__(
        self,
        *,
        root: Path,
        candidate: JdtCandidate,
        worker_java_home: Path,
        source_roots: Sequence[Path],
        baseline_source_roots: Sequence[Path] | None = None,
        classpath_entries: Sequence[Path],
        source_encoding: str,
        source_level: int = 8,
        method_parameters: bool = False,
        test_source_roots: Sequence[Path] = (),
        baseline_test_source_roots: Sequence[Path] | None = None,
        test_classpath_entries: Sequence[Path] = (),
        baseline_main_output: Path | None = None,
        baseline_test_output: Path | None = None,
        processor_entries: Sequence[Path] = (),
        java_agents: Sequence[str] = (),
        extra_jvm_arguments: Sequence[str] = (),
        min_heap_mb: int = 64,
        max_heap_mb: int = 2048,
        timeout: float = 600.0,
    ) -> None:
        self.root = root.expanduser().resolve(strict=False)
        self.candidate = candidate
        self.worker_java_home = worker_java_home
        self.source_roots = tuple(
            path.expanduser().resolve(strict=True) for path in source_roots
        )
        self.baseline_source_roots = tuple(
            path.expanduser().resolve(strict=True)
            for path in (baseline_source_roots or source_roots)
        )
        if len(self.baseline_source_roots) != len(self.source_roots):
            raise JdtCompileError(
                "JDT_SOURCE_SNAPSHOT_INVALID",
                "The frozen source snapshot does not match source roots.",
            )
        self.classpath_entries = tuple(
            path.expanduser().resolve(strict=True) for path in classpath_entries
        )
        self.test_source_roots = tuple(
            path.expanduser().resolve(strict=True) for path in test_source_roots
        )
        self.baseline_test_source_roots = tuple(
            path.expanduser().resolve(strict=True)
            for path in (baseline_test_source_roots or test_source_roots)
        )
        if len(self.baseline_test_source_roots) != len(self.test_source_roots):
            raise JdtCompileError(
                "JDT_TEST_SOURCE_SNAPSHOT_INVALID",
                "The frozen test source snapshot does not match test roots.",
            )
        self.test_classpath_entries = tuple(
            path.expanduser().resolve(strict=True)
            for path in test_classpath_entries
        )
        self.baseline_main_output = (
            baseline_main_output.expanduser().resolve(strict=True)
            if baseline_main_output is not None
            else None
        )
        self.baseline_test_output = (
            baseline_test_output.expanduser().resolve(strict=True)
            if baseline_test_output is not None
            else None
        )
        self.source_encoding = source_encoding
        if int(source_level) not in {8, 11}:
            raise JdtCompileError(
                "JDT_SOURCE_LEVEL_UNSUPPORTED",
                "The product JDT Worker supports source level 8 or 11.",
            )
        self.source_level = int(source_level)
        self.method_parameters = bool(method_parameters)
        self.processor_entries = tuple(
            path.expanduser().resolve(strict=True) for path in processor_entries
        )
        self.java_agents = tuple(java_agents)
        self.extra_jvm_arguments = tuple(extra_jvm_arguments)
        if not 32 <= int(min_heap_mb) <= 8192:
            raise JdtCompileError(
                "JDT_WORKER_HEAP_INVALID",
                "JDT Worker min_heap_mb must be between 32 and 8192.",
            )
        if not 256 <= int(max_heap_mb) <= 8192:
            raise JdtCompileError(
                "JDT_WORKER_HEAP_INVALID",
                "JDT Worker max_heap_mb must be between 256 and 8192.",
            )
        if int(min_heap_mb) > int(max_heap_mb):
            raise JdtCompileError(
                "JDT_WORKER_HEAP_INVALID",
                "JDT Worker min_heap_mb may not exceed max_heap_mb.",
            )
        self.min_heap_mb = int(min_heap_mb)
        self.max_heap_mb = int(max_heap_mb)
        self.timeout = timeout
        self.private_project = self.root / "workspace/plain-fixture"
        self.private_source = self.private_project / "src"
        self.output_directory = self.private_project / "bin"
        self.private_test_source = self.private_project / "test-src"
        self.test_output_directory = self.private_project / "test-bin"
        self._source_map: dict[Path, Path] = {}
        self._client: JdtWorkerClient | None = None
        self._poisoned = False
        self._poison_reason = ""
        self._baseline_ready = False
        self._working_compile_state = "unknown"
        self._last_compile_diagnostics: tuple[dict[str, Any], ...] = ()
        self._last_compile_error_count = 0
        self._published_output_manifest: dict[str, str] = {}
        self._native_full_resource_manifest: dict[str, str] = {}
        self._state_lock = threading.RLock()
        self._operation_lock = threading.Lock()

    @property
    def ready(self) -> bool:
        with self._state_lock:
            return bool(
                self._client is not None
                and self._client.process.poll() is None
                and not self._poisoned
                and self._baseline_ready
            )

    @property
    def working_compile_state(self) -> str:
        with self._state_lock:
            return self._working_compile_state

    @property
    def last_compile_diagnostics(self) -> tuple[dict[str, Any], ...]:
        with self._state_lock:
            return tuple(dict(value) for value in self._last_compile_diagnostics)

    @property
    def last_compile_error_count(self) -> int:
        with self._state_lock:
            return self._last_compile_error_count

    @property
    def native_full_resource_manifest(self) -> dict[str, str]:
        """Resources produced by JDT FULL before formal-resource overlay."""

        with self._state_lock:
            return dict(self._native_full_resource_manifest)

    def start(self) -> JdtCompileResult:
        with self._operation_lock:
            with self._state_lock:
                if self._poisoned:
                    raise self._poisoned_error()
                if self._client is not None:
                    raise JdtCompileError(
                        "JDT_SESSION_ALREADY_STARTED",
                        "The JDT CompileSession is already active.",
                    )
            client: JdtWorkerClient | None = None
            try:
                self.root.mkdir(parents=True, exist_ok=False, mode=0o700)
                self._materialize_sources()
                classpath_file = self.root / "worker-classpath.private.txt"
                classpath_file.write_text(
                    "".join(f"{path}\n" for path in self.classpath_entries),
                    encoding="utf-8",
                )
                processor_file: Path | None = None
                if self.processor_entries:
                    processor_file = self.root / "apt-processors.private.txt"
                    processor_file.write_text(
                        "".join(f"{path}\n" for path in self.processor_entries),
                        encoding="utf-8",
                    )
                test_classpath_file: Path | None = None
                if self.test_source_roots:
                    test_classpath_file = (
                        self.root / "worker-test-classpath.private.txt"
                    )
                    test_classpath_file.write_text(
                        "".join(
                            f"{path}\n" for path in self.test_classpath_entries
                        ),
                        encoding="utf-8",
                    )
                client = self._start_worker(
                    classpath_file=classpath_file,
                    processor_file=processor_file,
                    test_classpath_file=test_classpath_file,
                )
                with self._state_lock:
                    self._client = client
                result = self._build("FULL", source_changes_pending=False)
                with self._state_lock:
                    self._native_full_resource_manifest = (
                        self._native_resource_manifest()
                    )
                if self.baseline_main_output is not None:
                    self._copy_frozen_resources(
                        self.baseline_main_output,
                        self.output_directory,
                    )
                if self.baseline_test_output is not None:
                    self._copy_frozen_resources(
                        self.baseline_test_output,
                        self.test_output_directory,
                    )
                return result
            except Exception:
                with self._state_lock:
                    self._client = None
                if client is not None:
                    client.force_close()
                self._source_map.clear()
                shutil.rmtree(self.root, ignore_errors=True)
                raise

    def compile(self, source_files: Iterable[Path]) -> JdtCompileResult:
        with self._operation_lock:
            with self._state_lock:
                if self._poisoned:
                    raise self._poisoned_error()
                if self._client is None:
                    raise JdtCompileError(
                        "JDT_SESSION_NOT_READY",
                        "The JDT CompileSession is not active.",
                    )
                if not self._baseline_ready:
                    raise JdtCompileError(
                        "JDT_BASELINE_NOT_ACCEPTED",
                        "The initial JDT FULL baseline has not passed validation.",
                    )
            selected = tuple(
                path.expanduser().resolve(strict=False)
                for path in source_files
            )
            if not selected:
                raise JdtCompileError(
                    "INVALID_ARGUMENT",
                    "reload requires at least one Java source file.",
                )
            snapshots: dict[
                Path, tuple[Path, bytes | None, bytes | None, bool]
            ] = {}
            for source in selected:
                private = self._source_map.get(source)
                if private is None:
                    private = self._private_path_for_workspace_source(source)
                if private is None:
                    raise JdtCompileError(
                        "SOURCE_OUTSIDE_BUILD_WORLD",
                        "A reload source is outside this CompileSession.",
                    )
                mapped = source in self._source_map
                content = source.read_bytes() if source.is_file() else None
                original = private.read_bytes() if private.is_file() else None
                if content is None and original is None:
                    # A fresh Bootstrap may already include a requested
                    # deletion. There is no additional lifecycle delta.
                    continue
                snapshots[source] = (private, content, original, mapped)
            if not snapshots:
                return self._build("INCREMENTAL", source_changes_pending=False)
            replaced: list[tuple[Path, bytes | None, Path, bool]] = []
            changed_private_sources = {
                private.relative_to(self.private_project).as_posix()
                for _source, (private, content, original, _mapped)
                in snapshots.items()
                if content is not None and content != original
            }
            try:
                for source, (private, content, original, mapped) in snapshots.items():
                    metadata = private.stat() if private.is_file() else None
                    private.parent.mkdir(parents=True, exist_ok=True)
                    if not mapped:
                        self._source_map[source] = private
                    replaced.append((private, original, source, mapped))
                    if content is None:
                        private.unlink(missing_ok=True)
                        continue
                    temporary = private.with_name(
                        f".{private.name}.{time.time_ns()}.tmp"
                    )
                    temporary.write_bytes(content)
                    temporary.replace(private)
                    if content != original:
                        forced_mtime = max(
                            time.time_ns(),
                            (
                                metadata.st_mtime_ns + 2_000_000_000
                                if metadata is not None
                                else time.time_ns()
                            ),
                        )
                        os.utime(
                            private,
                            ns=(
                                metadata.st_atime_ns
                                if metadata is not None
                                else forced_mtime,
                                forced_mtime,
                            ),
                        )
                        if (
                            metadata is not None
                            and private.stat().st_mtime_ns
                            <= metadata.st_mtime_ns
                        ):
                            raise OSError(
                                "private source timestamp did not advance"
                            )
            except Exception as error:
                rollback_ok = True
                for private, original, source, mapped in reversed(replaced):
                    try:
                        if original is None:
                            private.unlink(missing_ok=True)
                        else:
                            private.parent.mkdir(parents=True, exist_ok=True)
                            private.write_bytes(original)
                        if not mapped:
                            self._source_map.pop(source, None)
                    except OSError:
                        rollback_ok = False
                if not rollback_ok:
                    self._poison("SOURCE_MIRROR_ROLLBACK_FAILED")
                raise JdtCompileError(
                    "SOURCE_MIRROR_UPDATE_FAILED",
                    "The private source mirror could not be updated atomically.",
                ) from error
            result = self._build("INCREMENTAL", source_changes_pending=False)
            requested_deleted_source_units = {
                private.relative_to(self.private_project).as_posix()
                for _source, (private, content, _original, _mapped)
                in snapshots.items()
                if content is None
            }
            missing_deleted_source_units = sorted(
                requested_deleted_source_units
                - set(result.deleted_source_units)
            )
            if missing_deleted_source_units:
                self._poison("JDT_SOURCE_DELETION_NOT_OBSERVED")
                raise JdtCompileError(
                    "JDT_SOURCE_DELETION_NOT_OBSERVED",
                    "The JDT Worker did not observe every deleted source file.",
                    context={
                        "missing_source_count": len(
                            missing_deleted_source_units
                        ),
                        "missing_sources": missing_deleted_source_units,
                        "observed_deleted_sources": list(
                            result.deleted_source_units
                        ),
                    },
                )
            missing_compiled_sources = sorted(
                changed_private_sources - set(result.compiled_source_units)
            )
            if missing_compiled_sources:
                self._poison("JDT_SOURCE_CHANGE_NOT_OBSERVED")
                raise JdtCompileError(
                    "JDT_SOURCE_CHANGE_NOT_OBSERVED",
                    "The JDT Worker did not compile every changed source file.",
                    context={
                        "missing_source_count": len(missing_compiled_sources),
                        "missing_sources": missing_compiled_sources,
                    },
                )
            if result.compile_ok:
                for source, (_private, content, _original, _mapped) in (
                    snapshots.items()
                ):
                    if content is None:
                        self._source_map.pop(source, None)
            pending = any(
                (
                    source.exists()
                    if content is None
                    else not source.is_file()
                    or _sha256_file(source)
                    != hashlib.sha256(content).hexdigest()
                )
                for source, (_private, content, _original, _mapped)
                in snapshots.items()
            )
            return replace(
                result,
                source_changes_pending=pending,
                **self._publication_delta(),
            )

    def _private_path_for_workspace_source(self, source: Path) -> Path | None:
        matches: list[Path] = []
        for roots, destination in (
            (self.source_roots, self.private_source),
            (self.test_source_roots, self.private_test_source),
        ):
            for root in roots:
                if source.is_relative_to(root):
                    matches.append(destination / source.relative_to(root))
        if len(matches) != 1:
            return None
        return matches[0]

    def accept_baseline(self) -> None:
        """Publish initial FULL only after Maven/JDT compatibility succeeds."""

        with self._operation_lock:
            with self._state_lock:
                if self._poisoned or self._client is None:
                    raise JdtCompileError(
                        "JDT_SESSION_NOT_READY",
                        "The JDT baseline cannot be accepted.",
                    )
                self._published_output_manifest = (
                    self._complete_output_manifest()
                )
                self._baseline_ready = True
                self._working_compile_state = "valid"
                self._last_compile_diagnostics = ()
                self._last_compile_error_count = 0

    def workspace_source_changes(self) -> tuple[Path, ...]:
        """Return main/test workspace sources that differ from the private mirror."""

        with self._state_lock:
            mapped = dict(self._source_map)
        observed: set[Path] = set()
        changed: set[Path] = set()
        for root in (*self.source_roots, *self.test_source_roots):
            for source in root.rglob("*.java"):
                resolved = source.resolve(strict=False)
                observed.add(resolved)
                private = mapped.get(resolved)
                if private is None:
                    changed.add(resolved)
                    continue
                try:
                    if source.read_bytes() != private.read_bytes():
                        changed.add(resolved)
                except OSError:
                    changed.add(resolved)
        changed.update(set(mapped) - observed)
        return tuple(sorted(changed, key=str))

    def mark_published(self) -> None:
        """Advance the Runtime publication baseline after a confirmed apply."""

        with self._operation_lock:
            with self._state_lock:
                if self._poisoned or not self._baseline_ready:
                    raise JdtCompileError(
                        "JDT_SESSION_NOT_READY",
                        "The JDT publication baseline cannot be advanced.",
                    )
                self._published_output_manifest = (
                    self._complete_output_manifest()
                )

    def close(self) -> bool:
        with self._state_lock:
            client = self._client
            self._client = None
            self._poisoned = True
            self._poison_reason = "SESSION_CLOSED"
            self._baseline_ready = False
            self._working_compile_state = "unknown"
        settled = client.force_close() if client is not None else True
        with self._operation_lock:
            shutil.rmtree(self.root, ignore_errors=True)
            self._source_map.clear()
            self._published_output_manifest.clear()
        return settled

    def interrupt(self, reason: str = "JDT_SESSION_INTERRUPTED") -> None:
        """Force-close the Worker so a synchronous build unblocks promptly."""

        self._poison(reason)

    def _materialize_sources(self) -> None:
        self._materialize_source_group(
            source_roots=self.source_roots,
            baseline_roots=self.baseline_source_roots,
            destination_root=self.private_source,
        )
        if self.test_source_roots:
            self._materialize_source_group(
                source_roots=self.test_source_roots,
                baseline_roots=self.baseline_test_source_roots,
                destination_root=self.private_test_source,
            )

    def _materialize_source_group(
        self,
        *,
        source_roots: Sequence[Path],
        baseline_roots: Sequence[Path],
        destination_root: Path,
    ) -> None:
        destination_root.mkdir(parents=True, exist_ok=False)
        mapped: dict[str, str] = {}
        for source_root, baseline_root in zip(
            source_roots, baseline_roots
        ):
            for baseline_source in sorted(baseline_root.rglob("*.java")):
                if baseline_source.is_symlink():
                    raise JdtCompileError(
                        "SOURCE_LINK_UNSUPPORTED",
                        "CompileSession source roots may not contain links.",
                    )
                relative = baseline_source.relative_to(baseline_root)
                source = source_root / relative
                key = relative.as_posix()
                digest = _sha256_file(baseline_source)
                if key in mapped and mapped[key] != digest:
                    raise JdtCompileError(
                        "SOURCE_ROOT_COLLISION",
                        "Two source roots map different Java files to one path.",
                    )
                mapped[key] = digest
                destination = destination_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(baseline_source, destination)
                self._source_map[source.resolve(strict=False)] = destination

    @staticmethod
    def _copy_frozen_resources(source_root: Path, output_root: Path) -> None:
        output_root.mkdir(parents=True, exist_ok=True)
        for source in sorted(source_root.rglob("*")):
            if not source.is_file() or source.suffix == ".class":
                continue
            relative = source.relative_to(source_root)
            destination = output_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)

    def _start_worker(
        self,
        *,
        classpath_file: Path,
        processor_file: Path | None,
        test_classpath_file: Path | None,
    ) -> JdtWorkerClient:
        java = self.candidate.verify_worker_java(
            self.worker_java_home
        ).executable
        configuration = self.root / "configuration"
        configuration.mkdir()
        template = (
            self.candidate.root / "configuration/config.ini"
        ).read_text(encoding="utf-8")
        prefix = (self.candidate.root / "plugins").resolve().as_uri() + "/"
        (configuration / "config.ini").write_text(
            template.replace("file:plugins/", prefix),
            encoding="utf-8",
        )
        stderr_path = self.root / "worker.stderr.log"
        stderr_stream = stderr_path.open("w", encoding="utf-8")
        command = [
            str(java),
            f"-Xms{self.min_heap_mb}m",
            f"-Xmx{self.max_heap_mb}m",
            *self.extra_jvm_arguments,
            *(f"-javaagent:{agent}" for agent in self.java_agents),
            "-jar",
            str(self.candidate.launcher),
            "-clean",
            "-nosplash",
            "-install",
            str(self.candidate.root),
            "-configuration",
            str(configuration),
            "-data",
            str(self.root / "workspace"),
            "-application",
            "net.jolink.runtime.jdt.worker",
            "--system-libraries",
            str(classpath_file),
            "--source-encoding",
            self.source_encoding,
            "--source-level",
            str(self.source_level),
            "--parameters",
            "true" if self.method_parameters else "false",
            "--instrumentation",
            "enabled",
        ]
        if processor_file is not None:
            command.extend(["--apt-processors-file", str(processor_file)])
        if test_classpath_file is not None:
            command.extend(
                ["--test-classpath-file", str(test_classpath_file)]
            )
        try:
            process_options: dict[str, Any] = {}
            if os.name == "nt":
                process_options["creationflags"] = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
            else:
                process_options["start_new_session"] = True
            process = subprocess.Popen(
                command,
                cwd=self.candidate.root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_stream,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **process_options,
            )
        except Exception:
            stderr_stream.close()
            raise
        client = JdtWorkerClient(process, stderr_stream, timeout=self.timeout)
        try:
            ready = client.receive()
        except Exception:
            client.force_close()
            raise
        if (
            ready.get("ok") is not True
            or ready.get("status") != "ready"
            or ready.get("java_builder_count") != 1
            or ready.get("source_encoding_verified") is not True
            or ready.get("source_level")
            not in ({"8", "1.8"} if self.source_level == 8 else {"11"})
            or ready.get("method_parameters")
            != ("generate" if self.method_parameters else "do not generate")
            or (
                test_classpath_file is not None
                and ready.get("test_model_configured") is not True
            )
            or (
                processor_file is not None
                and ready.get("apt_factory_path_verified") is not True
            )
        ):
            client.close()
            raise JdtCompileError(
                "JDT_WORKER_NOT_READY",
                "The JDT Worker did not verify the requested Build World.",
            )
        return client

    def _build(
        self,
        kind: str,
        *,
        source_changes_pending: bool,
    ) -> JdtCompileResult:
        with self._state_lock:
            if self._poisoned:
                raise self._poisoned_error()
            client = self._client
        if client is None:
            raise JdtCompileError(
                "JDT_SESSION_NOT_READY",
                "The JDT CompileSession is not active.",
            )
        resources_before = self._resource_manifest()
        started = time.monotonic()
        try:
            frame = client.command(f"BUILD\t{kind}")
        except JdtCompileError as error:
            self._poison(error.error_code)
            raise
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        if frame.get("operation_ok") is not True:
            self._poison("JDT_BUILD_ABORTED")
            raise JdtCompileError(
                "JDT_BUILD_ABORTED",
                "The JDT build operation did not complete.",
            )
        compiled = frame.get("compiled_source_units")
        deleted_sources = frame.get("deleted_source_units", [])
        changed = frame.get("changed_classes")
        deleted = frame.get("deleted_classes")
        if not all(
            isinstance(value, list)
            for value in (compiled, deleted_sources, changed, deleted)
        ):
            self._poison("JDT_WORKER_PROTOCOL_ERROR")
            raise JdtCompileError(
                "JDT_WORKER_PROTOCOL_ERROR",
                "The JDT build result omitted output delta fields.",
            )
        resources_after = self._resource_manifest()
        changed_resources = tuple(
            sorted(
                path
                for path, digest in resources_after.items()
                if resources_before.get(path) != digest
            )
        )
        deleted_resources = tuple(
            sorted(path for path in resources_before if path not in resources_after)
        )
        raw_diagnostics = frame.get("diagnostic_details", [])
        if not isinstance(raw_diagnostics, list) or not all(
            isinstance(value, dict) for value in raw_diagnostics
        ):
            self._poison("JDT_WORKER_PROTOCOL_ERROR")
            raise JdtCompileError(
                "JDT_WORKER_PROTOCOL_ERROR",
                "The JDT build result contains invalid diagnostics.",
            )
        diagnostics = tuple(
            {
                key: value
                for key, value in diagnostic.items()
                if key in {"resource", "line", "severity_name", "message"}
            }
            for diagnostic in raw_diagnostics
        )
        compiled_source_units = tuple(
            sorted({str(value).replace("\\", "/") for value in compiled})
        )
        compile_ok = frame.get("compile_ok") is True
        error_count = int(frame.get("error_count", 0))
        with self._state_lock:
            self._working_compile_state = "valid" if compile_ok else "failed"
            self._last_compile_diagnostics = diagnostics if not compile_ok else ()
            self._last_compile_error_count = error_count if not compile_ok else 0
        return JdtCompileResult(
            compile_ok=compile_ok,
            actual_build_kind=(
                str(frame["actual_build_kind"])
                if frame.get("actual_build_kind") is not None
                else None
            ),
            compiled_source_count=len(compiled_source_units),
            compiled_source_units=compiled_source_units,
            changed_classes=tuple(str(value) for value in changed),
            deleted_classes=tuple(str(value) for value in deleted),
            changed_resources=changed_resources,
            deleted_resources=deleted_resources,
            error_count=error_count,
            warning_count=int(frame.get("warning_count", 0)),
            diagnostics=diagnostics,
            diagnostics_truncated=frame.get("diagnostics_truncated") is True,
            elapsed_ms=elapsed_ms,
            source_changes_pending=source_changes_pending,
            output_directory=self.output_directory,
            main_compile_ok=frame.get("main_compile_ok") is not False,
            test_compile_ok=frame.get("test_compile_ok") is not False,
            main_error_count=int(frame.get("main_error_count", 0)),
            test_error_count=int(frame.get("test_error_count", 0)),
            main_warning_count=int(frame.get("main_warning_count", 0)),
            test_warning_count=int(frame.get("test_warning_count", 0)),
            test_output_directory=(
                self.test_output_directory if self.test_source_roots else None
            ),
            deleted_source_units=tuple(
                sorted(
                    {
                        str(value).replace("\\", "/")
                        for value in deleted_sources
                    }
                )
            ),
        )

    def _resource_manifest(self) -> dict[str, str]:
        if not self.output_directory.is_dir():
            return {}
        return {
            path.relative_to(self.output_directory).as_posix(): _sha256_file(path)
            for path in sorted(self.output_directory.rglob("*"))
            if path.is_file() and path.suffix != ".class"
        }

    def _native_resource_manifest(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for prefix, root in (
            ("main", self.output_directory),
            ("test", self.test_output_directory),
        ):
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix == ".class":
                    continue
                result[f"{prefix}/{path.relative_to(root).as_posix()}"] = (
                    _sha256_file(path)
                )
        return result

    def _complete_output_manifest(self) -> dict[str, str]:
        if not self.output_directory.is_dir():
            return {}
        return {
            path.relative_to(self.output_directory).as_posix(): _sha256_file(path)
            for path in sorted(self.output_directory.rglob("*"))
            if path.is_file()
        }

    def _publication_delta(self) -> dict[str, tuple[str, ...]]:
        current = self._complete_output_manifest()
        with self._state_lock:
            published = dict(self._published_output_manifest)
        changed = sorted(
            path
            for path, digest in current.items()
            if published.get(path) != digest
        )
        deleted = sorted(path for path in published if path not in current)
        return {
            "candidate_changed_classes": tuple(
                path for path in changed if path.endswith(".class")
            ),
            "candidate_deleted_classes": tuple(
                path for path in deleted if path.endswith(".class")
            ),
            "candidate_changed_resources": tuple(
                path for path in changed if not path.endswith(".class")
            ),
            "candidate_deleted_resources": tuple(
                path for path in deleted if not path.endswith(".class")
            ),
        }

    def _poison(self, reason: str) -> None:
        with self._state_lock:
            if self._poisoned:
                return
            self._poisoned = True
            self._poison_reason = reason
            self._baseline_ready = False
            self._working_compile_state = "unknown"
            client = self._client
            self._client = None
        if client is not None:
            client.force_close()

    def _poisoned_error(self) -> JdtCompileError:
        return JdtCompileError(
            "JDT_SESSION_POISONED",
            "The JDT CompileSession cannot be reused after an unconfirmed "
            f"Worker outcome ({self._poison_reason or 'unknown'}).",
        )


__all__ = [
    "JdtCandidate",
    "JdtCompileError",
    "JdtCompileResult",
    "JdtBuildWorldPlan",
    "JdtWorkerClient",
    "WorkerJavaRuntime",
    "lombok_worker_jvm_arguments",
    "PersistentJdtCompileSession",
    "discover_java8_system_entries",
    "discover_target_system_entries",
    "resource_tree_fingerprint",
]
