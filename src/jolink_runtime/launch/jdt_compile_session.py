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
from .product_assets import canonical_lf_bytes
from .toolchain import JavaToolchainCandidate


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
            lock_raw = canonical_lf_bytes(lock_path.read_bytes())
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
            return cls._load_root(lock, product_root, verify=False)
        except JdtCompileError:
            pass
        with cls._product_install_lock:
            try:
                return cls._load_root(lock, product_root, verify=False)
            except JdtCompileError:
                cls._install_product_candidate(
                    lock,
                    product_root=product_root,
                    legacy_roots=(
                        cache_base / "jdt-worker/candidates" / candidate_id,
                        cache_base / "jdt-poc/candidates" / candidate_id,
                    ),
                )
                return cls._load_root(lock, product_root, verify=False)

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
        *,
        verify: bool = True,
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
        launcher = root / "plugins" / str(equinox["launcher_filename"])
        if not verify:
            if not launcher.is_file() or not (root / "plugins" / str(
                lock["worker_artifact"]["filename"]
            )).is_file():
                raise JdtCompileError(
                    "JDT_CANDIDATE_UNAVAILABLE", "The installed Worker is absent."
                )
        else:
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
            config_path.write_bytes(
                canonical_lf_bytes(
                    Path(__file__).with_name(
                        "jdt-product-config.ini"
                    ).read_bytes()
                )
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
    runtime_changed_classes: tuple[str, ...] = ()
    runtime_deleted_classes: tuple[str, ...] = ()
    runtime_changed_resources: tuple[str, ...] = ()
    runtime_deleted_resources: tuple[str, ...] = ()
    main_compile_ok: bool = True
    test_compile_ok: bool = True
    main_error_count: int = 0
    test_error_count: int = 0
    main_warning_count: int = 0
    test_warning_count: int = 0
    test_output_directory: Path | None = None
    deleted_source_units: tuple[str, ...] = ()
    jdt_build_ms: float = 0.0
    diagnostics_ms: float = 0.0


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
    worker_java_home: Path | None = None
    worker_java_major: int | None = None
    system_entries: tuple[Path, ...] = ()

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


def select_target_system_home(
    preferred: Iterable[Path], target_level: int
) -> Path:
    """Find an installed JDK matching the bytecode target, not the Runtime JDK."""

    candidates = [path.expanduser() for path in preferred]
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(Path(java_home))
    candidates.extend(Path.home().glob(".jdks/*"))
    candidates.extend(Path.home().glob(
        "Library/Java/JavaVirtualMachines/*/Contents/Home"
    ))
    candidates.extend(Path("/Library/Java/JavaVirtualMachines").glob(
        "*/Contents/Home"
    ))
    candidates.extend(Path("/usr/lib/jvm").glob("*"))
    for environment in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(environment)
        if base:
            candidates.extend(Path(base).joinpath("Java").glob("*"))
    seen: set[str] = set()
    for candidate in candidates:
        home = candidate.resolve(strict=False)
        key = os.path.normcase(str(home))
        if key in seen:
            continue
        seen.add(key)
        try:
            if int(target_level) == 8:
                discover_java8_system_entries(home)
                return home
            version = JavaToolchainCandidate(
                home=home,
                java_executable=home / "bin" / (
                    "java.exe" if os.name == "nt" else "java"
                ),
                javac_executable=home / "bin" / (
                    "javac.exe" if os.name == "nt" else "javac"
                ),
                source="target_system_discovery",
            ).major_version
            if int(target_level) == 11 and version == 11:
                discover_target_system_entries(home, 11)
                return home
        except (OSError, JdtCompileError):
            continue
    raise JdtCompileError(
        "JDT_TARGET_PLATFORM_UNAVAILABLE",
        f"No installed JDK {int(target_level)} target platform was found.",
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
        preserve_root_on_close: bool = False,
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
        self.classpath_entries = tuple(Path(path) for path in classpath_entries)
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
        self.preserve_root_on_close = bool(preserve_root_on_close)
        self.private_project = self.root / "workspace/plain-fixture"
        self.private_source = self.private_project / "src"
        self.output_directory = self.private_project / "bin"
        self.private_test_source = self.private_project / "test-src"
        self.test_output_directory = self.private_project / "test-bin"
        self._source_map: dict[Path, Path] = {}
        self._source_stamps: dict[Path, tuple[int, int]] = {}
        self._pending_outputs: dict[str, bool] = {}
        self._client: JdtWorkerClient | None = None
        self._poisoned = False
        self._poison_reason = ""
        self._baseline_ready = False
        self._working_compile_state = "unknown"
        self._last_compile_diagnostics: tuple[dict[str, Any], ...] = ()
        self._last_compile_error_count = 0
        self._native_full_resource_manifest: dict[str, str] = {}
        self._worker_ready_frame: dict[str, Any] = {}
        self._last_close_clean = False
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

    @property
    def last_close_clean(self) -> bool:
        with self._state_lock:
            return self._last_close_clean

    @property
    def workspace_reopened(self) -> bool:
        with self._state_lock:
            return self._worker_ready_frame.get(
                "workspace_project_state"
            ) == "reopened"

    def start(
        self,
        *,
        reuse_workspace: bool = False,
        build_on_reuse: bool = True,
    ) -> JdtCompileResult:
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
                if reuse_workspace:
                    if not self.private_project.is_dir():
                        raise JdtCompileError(
                            "JDT_WORKSPACE_RESTORE_UNAVAILABLE",
                            "The saved JDT workspace is unavailable.",
                        )
                    if build_on_reuse:
                        self._synchronize_persisted_sources()
                    else:
                        self._restore_persisted_source_map()
                else:
                    self.root.mkdir(parents=True, exist_ok=False, mode=0o700)
                    self._materialize_sources()
                client = self._start_worker(reuse_workspace=reuse_workspace)
                with self._state_lock:
                    self._client = client
                if reuse_workspace and not self.workspace_reopened:
                    raise JdtCompileError(
                        "JDT_WORKSPACE_NOT_REOPENED",
                        "The Worker did not reopen the saved JDT workspace.",
                    )
                if reuse_workspace and not build_on_reuse:
                    self._baseline_ready = True
                    result = JdtCompileResult(
                        compile_ok=self._working_compile_state == "valid",
                        actual_build_kind=None,
                        compiled_source_count=0,
                        compiled_source_units=(),
                        changed_classes=(),
                        deleted_classes=(),
                        changed_resources=(),
                        deleted_resources=(),
                        error_count=self._last_compile_error_count,
                        warning_count=0,
                        diagnostics=self._last_compile_diagnostics,
                        diagnostics_truncated=False,
                        elapsed_ms=0.0,
                        source_changes_pending=False,
                        output_directory=self.output_directory,
                    )
                else:
                    result = self._build(
                        "INCREMENTAL" if reuse_workspace else "FULL",
                        source_changes_pending=False,
                    )
                if self.test_source_roots or self.baseline_main_output is not None:
                    self._native_full_resource_manifest = (
                        self._native_resource_manifest()
                    )
                self._restore_frozen_resources()
                return result
            except Exception:
                with self._state_lock:
                    self._client = None
                if client is not None:
                    client.force_close()
                self._source_map.clear()
                shutil.rmtree(self.root, ignore_errors=True)
                raise

    def _write_worker_inputs(
        self,
    ) -> tuple[Path, Path | None, Path | None]:
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
        else:
            (self.root / "apt-processors.private.txt").unlink(missing_ok=True)
        test_classpath_file: Path | None = None
        if self.test_source_roots:
            test_classpath_file = self.root / "worker-test-classpath.private.txt"
            test_classpath_file.write_text(
                "".join(f"{path}\n" for path in self.test_classpath_entries),
                encoding="utf-8",
            )
        else:
            (self.root / "worker-test-classpath.private.txt").unlink(
                missing_ok=True
            )
        return classpath_file, processor_file, test_classpath_file

    def compile(self, source_files: Iterable[Path]) -> JdtCompileResult:
        """Copy only requested edits and consume JavaBuilder's output delta."""
        with self._operation_lock:
            if self._poisoned:
                raise self._poisoned_error()
            if not self.ready:
                raise JdtCompileError(
                    "JDT_SESSION_NOT_READY", "The JDT CompileSession is not active."
                )
            touched: list[str] = []
            selected = []
            for raw in source_files:
                source = raw.expanduser().resolve(strict=False)
                private = self._source_map.get(source)
                if private is None:
                    private = self._private_path_for_workspace_source(source)
                if private is None:
                    raise JdtCompileError(
                        "SOURCE_OUTSIDE_BUILD_WORLD",
                        "A reload source is outside this CompileSession.",
                    )
                selected.append((source, private))
            for source, private in selected:
                content = source.read_bytes() if source.is_file() else None
                original = private.read_bytes() if private.is_file() else None
                self._remember_source(source, private)
                if content == original:
                    continue
                private.parent.mkdir(parents=True, exist_ok=True)
                if content is None:
                    private.unlink(missing_ok=True)
                    self._source_map.pop(source, None)
                    self._source_stamps.pop(source, None)
                else:
                    private.write_bytes(content)
                    self._source_map[source] = private
                touched.append(private.relative_to(self.private_project).as_posix())
            if touched:
                result = self._build(
                    "INCREMENTAL",
                    source_changes_pending=False,
                    touched_sources=tuple(touched),
                )
                # Fast Test still overlays its baseline test resources. Runtime
                # keeps resources outside bin, so its reload copies none.
                if self.baseline_main_output is not None or self.baseline_test_output is not None:
                    self._restore_frozen_resources()
            else:
                result = JdtCompileResult(
                    compile_ok=self._working_compile_state == "valid",
                    actual_build_kind=None,
                    compiled_source_count=0,
                    compiled_source_units=(),
                    changed_classes=(),
                    deleted_classes=(),
                    changed_resources=(),
                    deleted_resources=(),
                    error_count=self._last_compile_error_count,
                    warning_count=0,
                    diagnostics=self._last_compile_diagnostics,
                    diagnostics_truncated=False,
                    elapsed_ms=0.0,
                    source_changes_pending=False,
                    output_directory=self.output_directory,
                )
            return replace(result, **self._publication_delta())

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
        with self._state_lock:
            self._pending_outputs.clear()
            self._baseline_ready = True
            self._working_compile_state = "valid"
            self._last_compile_diagnostics = ()
            self._last_compile_error_count = 0

    @staticmethod
    def _source_stamp(source: Path) -> tuple[int, int] | None:
        try:
            metadata = source.stat()
            return metadata.st_mtime_ns, metadata.st_size
        except FileNotFoundError:
            return None

    def _remember_source(self, source: Path, private: Path) -> None:
        stamp = self._source_stamp(source)
        if stamp is not None:
            self._source_map[source] = private
            self._source_stamps[source] = stamp

    def workspace_source_changes(self) -> tuple[Path, ...]:
        """Find edits using saved size/mtime; do not reread unchanged sources."""
        observed: set[Path] = set()
        changed: set[Path] = set()
        for root in (*self.source_roots, *self.test_source_roots):
            for path in root.rglob("*.java"):
                source = path.resolve(strict=False)
                observed.add(source)
                if self._source_stamp(source) != self._source_stamps.get(source):
                    changed.add(source)
        changed.update(set(self._source_map) - observed)
        return tuple(sorted(changed, key=str))

    def mark_published(self) -> None:
        with self._state_lock:
            self._pending_outputs.clear()

    def reset_publication_baseline(self, _output_directory: Path) -> None:
        """Restart now loads the current JDT output, including pending edits."""
        with self._state_lock:
            self._pending_outputs.clear()

    def save_source_index(self) -> None:
        value = {
            str(source): [
                private.relative_to(self.private_project).as_posix(),
                list(self._source_stamps.get(source, ())),
            ]
            for source, private in self._source_map.items()
        }
        path = self.root / "source-index.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "sources": value,
            "compile_state": self._working_compile_state,
            "error_count": self._last_compile_error_count,
            "diagnostics": self._last_compile_diagnostics,
        }), encoding="utf-8")
        temporary.replace(path)

    def save_workspace(self) -> bool:
        with self._operation_lock:
            with self._state_lock:
                client = self._client
                if self._poisoned or client is None:
                    return False
            self.save_source_index()
            frame = client.command("SAVE", timeout=5.0)
            return bool(
                frame.get("ok") is True
                and frame.get("status") == "saved"
            )

    def close(self) -> bool:
        operation_owned = self._operation_lock.acquire(blocking=False)
        if operation_owned and self.root.is_dir():
            try:
                self.save_source_index()
            except OSError:
                pass
        with self._state_lock:
            client = self._client
            self._client = None
            baseline_ready = self._baseline_ready
            previously_poisoned = self._poisoned
            self._poisoned = True
            self._poison_reason = "SESSION_CLOSED"
            self._baseline_ready = False
            self._working_compile_state = "unknown"
        clean = False
        try:
            if client is None:
                settled = True
            elif operation_owned and baseline_ready and not previously_poisoned:
                settled = client.close()
                clean = settled
            else:
                settled = client.force_close()
        finally:
            if operation_owned:
                self._operation_lock.release()
        with self._state_lock:
            self._last_close_clean = clean
        if not self.preserve_root_on_close:
            shutil.rmtree(self.root, ignore_errors=True)
        with self._state_lock:
            self._source_map.clear()
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

    def _synchronize_persisted_sources(self) -> None:
        self._source_map.clear()
        self._synchronize_persisted_source_group(
            source_roots=self.source_roots,
            baseline_roots=self.baseline_source_roots,
            destination_root=self.private_source,
        )
        if self.test_source_roots:
            self._synchronize_persisted_source_group(
                source_roots=self.test_source_roots,
                baseline_roots=self.baseline_test_source_roots,
                destination_root=self.private_test_source,
            )

    def _restore_persisted_source_map(self) -> None:
        """Reconnect original source paths without changing the saved mirror."""

        self._source_map.clear()
        index = self.root / "source-index.json"
        if index.is_file():
            saved = json.loads(index.read_text(encoding="utf-8"))
            self._working_compile_state = saved.get("compile_state", "valid")
            self._last_compile_error_count = saved.get("error_count", 0)
            self._last_compile_diagnostics = tuple(saved.get("diagnostics", ()))
            for original, (relative, stamp) in saved.get("sources", saved).items():
                source = Path(original)
                self._source_map[source] = self.private_project / relative
                if len(stamp) == 2:
                    self._source_stamps[source] = tuple(stamp)
            return
        for source_roots, destination_root in (
            (self.source_roots, self.private_source),
            (self.test_source_roots, self.private_test_source),
        ):
            if not source_roots:
                continue
            if not destination_root.is_dir():
                raise JdtCompileError(
                    "JDT_WORKSPACE_SOURCE_MIRROR_UNAVAILABLE",
                    "The saved JDT source mirror is unavailable.",
                )
            for source_root in source_roots:
                relatives = {
                    path.relative_to(source_root)
                    for path in source_root.rglob("*.java")
                    if path.is_file()
                }
                relatives.update(
                    path.relative_to(destination_root)
                    for path in destination_root.rglob("*.java")
                    if path.is_file()
                )
                for relative in relatives:
                    self._source_map[
                        (source_root / relative).resolve(strict=False)
                    ] = destination_root / relative

    def _synchronize_persisted_source_group(
        self,
        *,
        source_roots: Sequence[Path],
        baseline_roots: Sequence[Path],
        destination_root: Path,
    ) -> None:
        if not destination_root.is_dir():
            raise JdtCompileError(
                "JDT_WORKSPACE_SOURCE_MIRROR_UNAVAILABLE",
                "The saved JDT source mirror is unavailable.",
            )
        desired: dict[str, tuple[str, Path, tuple[Path, ...]]] = {}
        sources_by_key: dict[str, list[Path]] = {}
        for source_root, baseline_root in zip(source_roots, baseline_roots):
            for baseline_source in sorted(baseline_root.rglob("*.java")):
                if baseline_source.is_symlink():
                    raise JdtCompileError(
                        "SOURCE_LINK_UNSUPPORTED",
                        "CompileSession source roots may not contain links.",
                    )
                relative = baseline_source.relative_to(baseline_root)
                key = relative.as_posix()
                digest = _sha256_file(baseline_source)
                source = source_root / relative
                existing = desired.get(key)
                if existing is not None and existing[0] != digest:
                    raise JdtCompileError(
                        "SOURCE_ROOT_COLLISION",
                        "Two source roots map different Java files to one path.",
                    )
                sources_by_key.setdefault(key, []).append(source)
                desired[key] = (
                    digest,
                    baseline_source,
                    tuple(sources_by_key[key]),
                )

        observed = {
            path.relative_to(destination_root).as_posix(): path
            for path in destination_root.rglob("*.java")
            if path.is_file()
        }
        for key, path in observed.items():
            if key not in desired:
                path.unlink(missing_ok=True)
        for key, (digest, baseline_source, workspace_sources) in desired.items():
            destination = destination_root / key
            if not destination.is_file() or _sha256_file(destination) != digest:
                previous_mtime = (
                    destination.stat().st_mtime_ns
                    if destination.is_file()
                    else 0
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(
                    f".{destination.name}.{time.time_ns()}.tmp"
                )
                shutil.copyfile(baseline_source, temporary)
                temporary.replace(destination)
                forced_mtime = max(
                    time.time_ns(),
                    previous_mtime + 2_000_000_000,
                )
                os.utime(destination, ns=(forced_mtime, forced_mtime))
            for source in workspace_sources:
                self._source_map[source.resolve(strict=False)] = destination

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
                self._remember_source(source.resolve(strict=False), destination)

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

    def _restore_frozen_resources(self) -> None:
        if self.baseline_main_output is None and self.baseline_test_output is None:
            return
        state_file = self.root / "frozen-resource-paths.json"
        try:
            previous = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        current: dict[str, list[str]] = {}
        for name, source_root, output_root in (
            ("main", self.baseline_main_output, self.output_directory),
            ("test", self.baseline_test_output, self.test_output_directory),
        ):
            paths = {
                path.relative_to(source_root).as_posix()
                for path in source_root.rglob("*")
                if path.is_file() and path.suffix != ".class"
            } if source_root is not None else set()
            for relative in set(previous.get(name, ())) - paths:
                path = Path(relative)
                if path.is_absolute() or ".." in path.parts:
                    continue
                (output_root / path).unlink(missing_ok=True)
            if source_root is not None:
                self._copy_frozen_resources(source_root, output_root)
            current[name] = sorted(paths)
        temporary = state_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(current, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(state_file)

    def _prepare_worker_command(self) -> list[str]:
        classpath_file, processor_file, test_classpath_file = self._write_worker_inputs()
        java = self.worker_java_home / "bin" / (
            "java.exe" if os.name == "nt" else "java"
        )
        configuration = self.root / "configuration"
        configuration.mkdir(exist_ok=True)
        template = (
            self.candidate.root / "configuration/config.ini"
        ).read_text(encoding="utf-8")
        prefix = (self.candidate.root / "plugins").resolve().as_uri() + "/"
        (configuration / "config.ini").write_text(
            template.replace("file:plugins/", prefix),
            encoding="utf-8",
        )
        command = [
            str(java),
            f"-Xms{self.min_heap_mb}m",
            f"-Xmx{self.max_heap_mb}m",
            *self.extra_jvm_arguments,
            *(f"-javaagent:{agent}" for agent in self.java_agents),
            "-jar",
            str(self.candidate.launcher),
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
        return command

    def _start_worker(self, *, reuse_workspace: bool = False) -> JdtWorkerClient:
        launch_file = self.root / "worker-launch.json"
        reused = reuse_workspace and launch_file.is_file()
        command = (
            json.loads(launch_file.read_text(encoding="utf-8"))
            if reused else self._prepare_worker_command()
        )
        stderr_stream = (self.root / "worker.stderr.log").open("w", encoding="utf-8")
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
        if ready.get("ok") is not True or ready.get("status") != "ready":
            client.close()
            raise JdtCompileError(
                "JDT_WORKER_NOT_READY",
                "The JDT Worker did not start successfully.",
            )
        if not reused:
            launch_file.write_text(json.dumps(
                command + ["--reuse-configuration", "true"]
            ), encoding="utf-8")
        with self._state_lock:
            self._worker_ready_frame = dict(ready)
        return client

    def _build(
        self,
        kind: str,
        *,
        source_changes_pending: bool,
        touched_sources: tuple[str, ...] = (),
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
        started = time.monotonic()
        try:
            command = f"BUILD\t{kind}"
            if touched_sources:
                command += "\t" + "\t".join(
                    base64.b64encode(path.encode("utf-8")).decode("ascii")
                    for path in touched_sources
                )
            frame = client.command(command)
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
        changed_resources = tuple(frame.get("changed_resources", ()))
        deleted_resources = tuple(frame.get("deleted_resources", ()))
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
            self._pending_outputs.update(
                (str(path), False) for path in (*changed, *changed_resources)
            )
            self._pending_outputs.update(
                (str(path), True) for path in (*deleted, *deleted_resources)
            )
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
            jdt_build_ms=float(frame.get("elapsed_ms", 0)),
            diagnostics_ms=float(frame.get("diagnostics_ms", 0)),
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


    def _publication_delta(self) -> dict[str, tuple[str, ...]]:
        with self._state_lock:
            pending = dict(self._pending_outputs)
        return {
            "runtime_changed_classes": tuple(sorted(
                path for path, deleted in pending.items()
                if not deleted and path.endswith(".class")
            )),
            "runtime_deleted_classes": tuple(sorted(
                path for path, deleted in pending.items()
                if deleted and path.endswith(".class")
            )),
            "runtime_changed_resources": tuple(sorted(
                path for path, deleted in pending.items()
                if not deleted and not path.endswith(".class")
            )),
            "runtime_deleted_resources": tuple(sorted(
                path for path, deleted in pending.items()
                if deleted and not path.endswith(".class")
            )),
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
    "select_target_system_home",
    "resource_tree_fingerprint",
]
