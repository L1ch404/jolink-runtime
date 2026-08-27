"""Persistent headless JDT compiler session used by Java project reload.

Maven/IDE discovery supplies the frozen Build World.  This module owns only
the verified Equinox Worker process, private source mirror, JavaBuilder state,
and compile result.  Mutable Worker output is never a publishable Generation.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


class JdtCompileError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class JdtCandidate:
    candidate_id: str
    root: Path
    launcher: Path
    worker_java_sha256: str
    lock: dict[str, Any]

    @classmethod
    def load(cls, lock_path: Path, cache_root: Path) -> "JdtCandidate":
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            candidate_id = str(lock["candidate_id"])
            artifacts = [*lock["artifacts"], lock["worker_artifact"]]
            equinox = lock["equinox"]
            worker_java_sha256 = str(
                lock["worker_build"]["java_home_identity"][
                    "java_binary_sha256"
                ]
            )
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise JdtCompileError(
                "JDT_CANDIDATE_LOCK_INVALID",
                "The JDT Worker candidate lock is unavailable or invalid.",
            ) from error
        root = cache_root.expanduser().resolve(strict=False) / "candidates" / (
            candidate_id
        )
        for artifact in artifacts:
            path = root / "plugins" / str(artifact["filename"])
            if (
                not path.is_file()
                or _sha256_file(path) != str(artifact["sha256"])
            ):
                raise JdtCompileError(
                    "JDT_CANDIDATE_INTEGRITY_MISMATCH",
                    "A locked JDT Worker artifact is missing or changed.",
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
            )
        launcher = root / "plugins" / str(equinox["launcher_filename"])
        return cls(
            candidate_id=candidate_id,
            root=root,
            launcher=launcher,
            worker_java_sha256=worker_java_sha256,
            lock=lock,
        )

    def verify_worker_java(self, java_home: Path) -> Path:
        executable = java_home.expanduser().resolve(strict=True) / "bin" / (
            "java.exe" if os.name == "nt" else "java"
        )
        if (
            not executable.is_file()
            or _sha256_file(executable) != self.worker_java_sha256
        ):
            raise JdtCompileError(
                "JDT_WORKER_JDK_IDENTITY_MISMATCH",
                "The JDT Worker JDK does not match the locked candidate.",
            )
        return executable


class JdtWorkerClient:
    def __init__(
        self,
        process: subprocess.Popen[str],
        stderr_stream: Any,
        *,
        timeout: float,
    ) -> None:
        self.process = process
        self._stderr_stream = stderr_stream
        self.timeout = timeout
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

    def command(self, command: str) -> dict[str, Any]:
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
        return self.receive()

    def close(self) -> bool:
        try:
            if self.process.poll() is None:
                try:
                    frame = self.command("STOP")
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
            self._stderr_stream.close()


@dataclass(frozen=True)
class JdtCompileResult:
    compile_ok: bool
    actual_build_kind: str | None
    compiled_source_count: int
    changed_classes: tuple[str, ...]
    deleted_classes: tuple[str, ...]
    error_count: int
    warning_count: int
    elapsed_ms: float
    source_changes_pending: bool
    output_directory: Path


class PersistentJdtCompileSession:
    """One long-lived JavaBuilder state for one frozen Build World."""

    def __init__(
        self,
        *,
        root: Path,
        candidate: JdtCandidate,
        worker_java_home: Path,
        source_roots: Sequence[Path],
        classpath_entries: Sequence[Path],
        source_encoding: str,
        processor_entries: Sequence[Path] = (),
        java_agents: Sequence[str] = (),
        extra_jvm_arguments: Sequence[str] = (),
        timeout: float = 600.0,
    ) -> None:
        self.root = root.expanduser().resolve(strict=False)
        self.candidate = candidate
        self.worker_java_home = worker_java_home
        self.source_roots = tuple(
            path.expanduser().resolve(strict=True) for path in source_roots
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
        self.timeout = timeout
        self.private_project = self.root / "workspace/plain-fixture"
        self.private_source = self.private_project / "src"
        self.output_directory = self.private_project / "bin"
        self._source_map: dict[Path, Path] = {}
        self._client: JdtWorkerClient | None = None
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self._client is not None

    def start(self) -> JdtCompileResult:
        with self._lock:
            if self._client is not None:
                raise JdtCompileError(
                    "JDT_SESSION_ALREADY_STARTED",
                    "The JDT CompileSession is already active.",
                )
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
            self._client = self._start_worker(
                classpath_file=classpath_file,
                processor_file=processor_file,
            )
            return self._build("FULL", source_changes_pending=False)

    def compile(self, source_files: Iterable[Path]) -> JdtCompileResult:
        with self._lock:
            if self._client is None:
                raise JdtCompileError(
                    "JDT_SESSION_NOT_READY",
                    "The JDT CompileSession is not active.",
                )
            selected = tuple(
                path.expanduser().resolve(strict=True) for path in source_files
            )
            if not selected:
                raise JdtCompileError(
                    "INVALID_ARGUMENT",
                    "reload requires at least one Java source file.",
                )
            before: dict[Path, str] = {}
            for source in selected:
                private = self._source_map.get(source)
                if private is None:
                    raise JdtCompileError(
                        "SOURCE_OUTSIDE_BUILD_WORLD",
                        "A reload source is outside this CompileSession.",
                    )
                content = source.read_bytes()
                before[source] = hashlib.sha256(content).hexdigest()
                private.parent.mkdir(parents=True, exist_ok=True)
                private.write_bytes(content)
            result = self._build("INCREMENTAL", source_changes_pending=False)
            pending = any(
                not source.is_file()
                or _sha256_file(source) != expected
                for source, expected in before.items()
            )
            return JdtCompileResult(
                **{
                    **result.__dict__,
                    "source_changes_pending": pending,
                }
            )

    def close(self) -> bool:
        with self._lock:
            client = self._client
            self._client = None
        return client.close() if client is not None else True

    def _materialize_sources(self) -> None:
        self.private_source.mkdir(parents=True, exist_ok=False)
        mapped: dict[str, str] = {}
        for source_root in self.source_roots:
            for source in sorted(source_root.rglob("*.java")):
                if source.is_symlink():
                    raise JdtCompileError(
                        "SOURCE_LINK_UNSUPPORTED",
                        "CompileSession source roots may not contain links.",
                    )
                relative = source.relative_to(source_root)
                key = relative.as_posix()
                digest = _sha256_file(source)
                if key in mapped and mapped[key] != digest:
                    raise JdtCompileError(
                        "SOURCE_ROOT_COLLISION",
                        "Two source roots map different Java files to one path.",
                    )
                mapped[key] = digest
                destination = self.private_source / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                self._source_map[source] = destination

    def _start_worker(
        self,
        *,
        classpath_file: Path,
        processor_file: Path | None,
    ) -> JdtWorkerClient:
        java = self.candidate.verify_worker_java(self.worker_java_home)
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
            "-Xms64m",
            "-Xmx512m",
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
            )
        except Exception:
            stderr_stream.close()
            raise
        client = JdtWorkerClient(process, stderr_stream, timeout=self.timeout)
        ready = client.receive()
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
        assert self._client is not None
        started = time.monotonic()
        frame = self._client.command(f"BUILD\t{kind}")
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        if frame.get("operation_ok") is not True:
            raise JdtCompileError(
                "JDT_BUILD_ABORTED",
                "The JDT build operation did not complete.",
            )
        compiled = frame.get("compiled_source_units")
        changed = frame.get("changed_classes")
        deleted = frame.get("deleted_classes")
        if not all(isinstance(value, list) for value in (compiled, changed, deleted)):
            raise JdtCompileError(
                "JDT_WORKER_PROTOCOL_ERROR",
                "The JDT build result omitted output delta fields.",
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
            error_count=int(frame.get("error_count", 0)),
            warning_count=int(frame.get("warning_count", 0)),
            elapsed_ms=elapsed_ms,
            source_changes_pending=source_changes_pending,
            output_directory=self.output_directory,
        )


__all__ = [
    "JdtCandidate",
    "JdtCompileError",
    "JdtCompileResult",
    "JdtWorkerClient",
    "PersistentJdtCompileSession",
]
