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
    worker_min_heap_mb: int = 64
    worker_max_heap_mb: int = 2048

    def is_fresh(self) -> bool:
        return self.fingerprint == fast_compile_fingerprint(
            configuration_inputs=self.configuration_inputs,
            configuration_environment_names=(
                self.configuration_environment_names
            ),
            javac_executable=self.javac_executable,
            compile_classpath=self.dependency_entries,
        )


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
        self.source_encoding = source_encoding
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
        self._source_map: dict[Path, Path] = {}
        self._client: JdtWorkerClient | None = None
        self._poisoned = False
        self._poison_reason = ""
        self._baseline_ready = False
        self._published_output_manifest: dict[str, str] = {}
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
                client = self._start_worker(
                    classpath_file=classpath_file,
                    processor_file=processor_file,
                )
                with self._state_lock:
                    self._client = client
                result = self._build("FULL", source_changes_pending=False)
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
            try:
                selected = tuple(
                    path.expanduser().resolve(strict=True)
                    for path in source_files
                )
            except OSError as error:
                raise JdtCompileError(
                    "SOURCE_LIFECYCLE_UNSUPPORTED",
                    "Adding or deleting Java sources is not supported yet.",
                ) from error
            if not selected:
                raise JdtCompileError(
                    "INVALID_ARGUMENT",
                    "reload requires at least one Java source file.",
                )
            snapshots: dict[Path, tuple[Path, bytes, bytes]] = {}
            for source in selected:
                private = self._source_map.get(source)
                if private is None:
                    raise JdtCompileError(
                        "SOURCE_OUTSIDE_BUILD_WORLD",
                        "A reload source is outside this CompileSession.",
                    )
                content = source.read_bytes()
                snapshots[source] = (private, content, private.read_bytes())
            replaced: list[tuple[Path, bytes]] = []
            changed_input_count = sum(
                1
                for _source, (_private, content, original) in snapshots.items()
                if content != original
            )
            try:
                for private, content, original in snapshots.values():
                    metadata = private.stat()
                    temporary = private.with_name(
                        f".{private.name}.{time.time_ns()}.tmp"
                    )
                    temporary.write_bytes(content)
                    temporary.replace(private)
                    replaced.append((private, original))
                    if content != original:
                        forced_mtime = max(
                            time.time_ns(),
                            metadata.st_mtime_ns + 2_000_000_000,
                        )
                        os.utime(
                            private,
                            ns=(metadata.st_atime_ns, forced_mtime),
                        )
                        if private.stat().st_mtime_ns <= metadata.st_mtime_ns:
                            raise OSError(
                                "private source timestamp did not advance"
                            )
            except Exception as error:
                rollback_ok = True
                for private, original in reversed(replaced):
                    try:
                        private.write_bytes(original)
                    except OSError:
                        rollback_ok = False
                if not rollback_ok:
                    self._poison("SOURCE_MIRROR_ROLLBACK_FAILED")
                raise JdtCompileError(
                    "SOURCE_MIRROR_UPDATE_FAILED",
                    "The private source mirror could not be updated atomically.",
                ) from error
            result = self._build("INCREMENTAL", source_changes_pending=False)
            if changed_input_count and result.compiled_source_count == 0:
                self._poison("JDT_SOURCE_CHANGE_NOT_OBSERVED")
                raise JdtCompileError(
                    "JDT_SOURCE_CHANGE_NOT_OBSERVED",
                    "The JDT Worker did not compile a changed source file.",
                )
            pending = any(
                not source.is_file()
                or _sha256_file(source)
                != hashlib.sha256(content).hexdigest()
                for source, (_private, content, _original) in snapshots.items()
            )
            return replace(
                result,
                source_changes_pending=pending,
                **self._publication_delta(),
            )

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
        settled = client.force_close() if client is not None else True
        with self._operation_lock:
            shutil.rmtree(self.root, ignore_errors=True)
            self._source_map.clear()
            self._published_output_manifest.clear()
        return settled

    def _materialize_sources(self) -> None:
        self.private_source.mkdir(parents=True, exist_ok=False)
        mapped: dict[str, str] = {}
        for source_root, baseline_root in zip(
            self.source_roots, self.baseline_source_roots
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
                destination = self.private_source / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(baseline_source, destination)
                self._source_map[source.resolve(strict=False)] = destination

    def _start_worker(
        self,
        *,
        classpath_file: Path,
        processor_file: Path | None,
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
            "--instrumentation",
            "enabled",
        ]
        if processor_file is not None:
            command.extend(["--apt-processors-file", str(processor_file)])
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
        changed = frame.get("changed_classes")
        deleted = frame.get("deleted_classes")
        if not all(isinstance(value, list) for value in (compiled, changed, deleted)):
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
        return JdtCompileResult(
            compile_ok=frame.get("compile_ok") is True,
            actual_build_kind=(
                str(frame["actual_build_kind"])
                if frame.get("actual_build_kind") is not None
                else None
            ),
            compiled_source_count=len(compiled),
            changed_classes=tuple(str(value) for value in changed),
            deleted_classes=tuple(str(value) for value in deleted),
            changed_resources=changed_resources,
            deleted_resources=deleted_resources,
            error_count=int(frame.get("error_count", 0)),
            warning_count=int(frame.get("warning_count", 0)),
            diagnostics=diagnostics,
            diagnostics_truncated=frame.get("diagnostics_truncated") is True,
            elapsed_ms=elapsed_ms,
            source_changes_pending=source_changes_pending,
            output_directory=self.output_directory,
        )

    def _resource_manifest(self) -> dict[str, str]:
        if not self.output_directory.is_dir():
            return {}
        return {
            path.relative_to(self.output_directory).as_posix(): _sha256_file(path)
            for path in sorted(self.output_directory.rglob("*"))
            if path.is_file() and path.suffix != ".class"
        }

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
]
