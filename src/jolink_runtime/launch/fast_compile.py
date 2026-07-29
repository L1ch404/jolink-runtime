"""Bounded Java source compilation for runtime-only HotSwap updates.

The project-launch pipeline owns discovery of the compile environment.  This
module only freezes that resolved plan and executes ``javac`` into a private
staging directory; it never writes Maven's normal output directory.
"""

from __future__ import annotations

import hashlib
import locale
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from .contracts import BuildOperationSpec
from .process_supervisor import AttemptToken, ProcessSupervisor


_MAX_SOURCE_FILES = 16
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_SOURCE_BYTES = 8 * 1024 * 1024


class FastCompileError(RuntimeError):
    """A structured failure that leaves Maven outputs and the JVM unchanged."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        retryable: bool,
        suggested_next_step: str,
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.suggested_next_step = suggested_next_step
        self.context = context or {}


@dataclass(frozen=True)
class FastCompilePlan:
    """Facts required to compile explicit sources for the active JVM."""

    project_root: Path
    module_root: Path
    source_root: Path
    output_root: Path
    javac_executable: Path
    compile_classpath: tuple[Path, ...] = field(repr=False)
    encoding: str = "UTF-8"
    configuration_inputs: tuple[Path, ...] = field(
        default_factory=tuple,
        repr=False,
    )
    configuration_fingerprint: str = field(default="", repr=False)
    baseline_class_hashes: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
    )
    target_module: str = "."

    def redacted_summary(self) -> dict[str, object]:
        return {
            "available": True,
            "build_system": "maven",
            "target_module": self.target_module,
            "source_root": str(self.source_root),
            "output_root": str(self.output_root),
            "javac": str(self.javac_executable),
            "compile_classpath_entry_count": len(self.compile_classpath),
            "encoding": self.encoding,
        }

    def current_fingerprint(self) -> str:
        return fast_compile_fingerprint(
            configuration_inputs=self.configuration_inputs,
            javac_executable=self.javac_executable,
            compile_classpath=self.compile_classpath,
        )

    def is_fresh(self) -> bool:
        return bool(
            self.configuration_fingerprint
            and self.configuration_fingerprint == self.current_fingerprint()
        )

    def resolve_sources(
        self,
        raw_sources: Iterable[str],
    ) -> tuple[Path, ...]:
        if isinstance(raw_sources, (str, bytes)):
            raise FastCompileError(
                "INVALID_ARGUMENT",
                "source_files must be an array of Java source paths.",
                retryable=True,
                suggested_next_step=(
                    "Provide source_files as an array containing one or more "
                    "paths under the selected module's src/main/java directory."
                ),
                context={"argument": "source_files"},
            )
        try:
            values = list(raw_sources)
        except TypeError as error:
            raise FastCompileError(
                "INVALID_ARGUMENT",
                "source_files must be an array of Java source paths.",
                retryable=True,
                suggested_next_step=(
                    "Provide source_files as an array containing one or more "
                    "paths under the selected module's src/main/java directory."
                ),
                context={"argument": "source_files"},
            ) from error
        if not values:
            raise FastCompileError(
                "INVALID_ARGUMENT",
                "source_files must contain at least one Java source file.",
                retryable=True,
                suggested_next_step=(
                    "Provide one or more existing files under the selected "
                    "module's src/main/java directory."
                ),
                context={"argument": "source_files"},
            )
        if len(values) > _MAX_SOURCE_FILES:
            raise FastCompileError(
                "FAST_COMPILE_LIMIT_EXCEEDED",
                "Too many source files were requested for one fast update.",
                retryable=True,
                suggested_next_step=(
                    "Limit one update to at most 16 explicit Java source files."
                ),
                context={
                    "argument": "source_files",
                    "source_file_count": len(values),
                    "source_file_limit": _MAX_SOURCE_FILES,
                },
            )

        source_root = self.source_root.resolve(strict=True)
        project_root = self.project_root.resolve(strict=True)
        resolved: list[Path] = []
        seen: set[str] = set()
        total_bytes = 0
        for raw in values:
            if not isinstance(raw, str) or not raw.strip():
                raise FastCompileError(
                    "INVALID_ARGUMENT",
                    "Every source_files entry must be a non-empty path.",
                    retryable=True,
                    suggested_next_step=(
                        "Provide Java source paths relative to project_path or "
                        "absolute paths inside the selected source root."
                    ),
                    context={"argument": "source_files"},
                )
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = project_root / candidate
            try:
                candidate = candidate.resolve(strict=True)
                candidate.relative_to(source_root)
            except (OSError, ValueError) as error:
                raise FastCompileError(
                    "SOURCE_OUTSIDE_SELECTED_MODULE",
                    "A source file is outside the selected Maven module.",
                    retryable=True,
                    suggested_next_step=(
                        "Use Java files under the selected module's "
                        "src/main/java directory; cross-module updates are not "
                        "supported in this version."
                    ),
                    context={"argument": "source_files"},
                ) from error
            if candidate.suffix.casefold() != ".java" or not candidate.is_file():
                raise FastCompileError(
                    "INVALID_SOURCE_FILE",
                    "A requested source is not a readable .java file.",
                    retryable=True,
                    suggested_next_step=(
                        "Provide existing .java files under src/main/java."
                    ),
                    context={"argument": "source_files"},
                )
            key = os.path.normcase(str(candidate))
            if key in seen:
                continue
            seen.add(key)
            try:
                size = candidate.stat().st_size
            except OSError as error:
                raise FastCompileError(
                    "INVALID_SOURCE_FILE",
                    "A requested source changed while it was inspected.",
                    retryable=True,
                    suggested_next_step=(
                        "Wait for other editors or build tools to finish, then "
                        "retry update."
                    ),
                    context={"argument": "source_files"},
                ) from error
            if size > _MAX_SOURCE_BYTES:
                raise FastCompileError(
                    "FAST_COMPILE_LIMIT_EXCEEDED",
                    "A requested Java source exceeds the fast-update size limit.",
                    retryable=False,
                    suggested_next_step=(
                        "Use the formal Maven build and restart path for this "
                        "source file."
                    ),
                    context={
                        "source_file_count": len(values),
                        "source_file_size_limit": _MAX_SOURCE_BYTES,
                    },
                )
            total_bytes += size
            resolved.append(candidate)
        if total_bytes > _MAX_TOTAL_SOURCE_BYTES:
            raise FastCompileError(
                "FAST_COMPILE_LIMIT_EXCEEDED",
                "The requested Java sources exceed the total fast-update limit.",
                retryable=True,
                suggested_next_step=(
                    "Split the change into smaller explicit updates or use a "
                    "formal Maven build and restart."
                ),
                context={
                    "source_file_count": len(resolved),
                    "total_source_size_limit": _MAX_TOTAL_SOURCE_BYTES,
                },
            )
        return tuple(resolved)


@dataclass(frozen=True)
class CompileAttemptResult:
    staging_directory: Path
    classes_directory: Path
    log_file: Path
    arg_file: Path
    source_files: tuple[Path, ...]
    source_hashes: Mapping[Path, str] = field(repr=False)
    elapsed_seconds: float

    def sources_unchanged(self) -> bool:
        for source, expected_hash in self.source_hashes.items():
            try:
                current_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            except OSError:
                return False
            if current_hash != expected_hash:
                return False
        return True


class FastCompiler:
    """Run one bounded ``javac`` process tree against a frozen plan."""

    def __init__(
        self,
        supervisor: ProcessSupervisor | None = None,
    ) -> None:
        self._supervisor = supervisor or ProcessSupervisor()

    def close(self, *, deadline: float, force: bool = False) -> bool:
        """Cancel every in-flight compiler process within the caller deadline."""
        report = (
            self._supervisor.force_close(deadline=deadline)
            if force
            else self._supervisor.close(deadline=deadline)
        )
        return report.settled

    def compile(
        self,
        plan: FastCompilePlan,
        source_files: tuple[Path, ...],
        *,
        attempt_directory: Path,
        source_release: int,
        include_parameters: bool,
        timeout_seconds: float = 60.0,
    ) -> CompileAttemptResult:
        try:
            staging = Path(
                tempfile.mkdtemp(
                    prefix="update-",
                    dir=str(attempt_directory),
                )
            )
        except OSError as error:
            raise FastCompileError(
                "FAST_COMPILE_PREPARE_FAILED",
                "Private staging could not be created.",
                retryable=True,
                suggested_next_step=(
                    "Restore access to joLink's temporary launch directory and "
                    "retry update."
                ),
            ) from error
        try:
            staging.chmod(0o700)
        except OSError:
            pass
        classes = staging / "classes"
        empty_sourcepath = staging / "empty-sourcepath"
        source_snapshot_root = staging / "sources"
        log_file = staging / "javac.log"
        arg_file = staging / "javac.args"
        try:
            classes.mkdir()
            empty_sourcepath.mkdir()
            source_snapshot_root.mkdir()
            snapshot_sources: list[Path] = []
            source_hashes: dict[Path, str] = {}
            for source in source_files:
                relative = source.relative_to(plan.source_root)
                snapshot = source_snapshot_root / relative
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                source_bytes = source.read_bytes()
                snapshot.write_bytes(source_bytes)
                snapshot_sources.append(snapshot)
                source_hashes[source] = hashlib.sha256(
                    source_bytes
                ).hexdigest()
            arguments = [
                "-encoding",
                plan.encoding,
                "-g",
                "-proc:none",
                "-implicit:none",
                "-sourcepath",
                str(empty_sourcepath),
                "-classpath",
                os.pathsep.join(str(path) for path in plan.compile_classpath),
                "-d",
                str(classes),
                "-source",
                str(source_release),
                "-target",
                str(source_release),
            ]
            if include_parameters:
                arguments.append("-parameters")
            arguments.extend(str(path) for path in snapshot_sources)
            _write_javac_argfile(arg_file, arguments)
        except (OSError, UnicodeError, ValueError) as error:
            self.discard(staging)
            raise FastCompileError(
                "FAST_COMPILE_PREPARE_FAILED",
                "The bounded javac invocation could not be prepared.",
                retryable=True,
                suggested_next_step=(
                    "Use a project path representable by the build JDK and "
                    "host encoding, or use the formal Maven build and restart."
                ),
            ) from error

        token = AttemptToken(
            attempt_id=f"update_{uuid.uuid4().hex[:12]}",
            generation=1,
        )
        spec = BuildOperationSpec(
            argv=(str(plan.javac_executable), f"@{arg_file}"),
            cwd=plan.module_root,
            timeout_seconds=timeout_seconds,
            output_capture=log_file,
            operation_name="fast_compile",
        )
        try:
            result = self._supervisor.run(spec, owner=token)
        except OSError as error:
            self.discard(staging)
            raise FastCompileError(
                "FAST_COMPILE_START_FAILED",
                "The configured javac process could not be started.",
                retryable=True,
                suggested_next_step=(
                    "Restart the project to refresh its JDK configuration, or "
                    "use the formal Maven build and restart."
                ),
            ) from error
        except Exception:
            self.discard(staging)
            raise
        finally:
            self._supervisor.release_owner(token)
        if result.cancelled:
            self.discard(staging)
            raise FastCompileError(
                "FAST_COMPILE_CANCELLED",
                "The fast Java compilation was cancelled.",
                retryable=True,
                suggested_next_step="Retry update after Runtime activity settles.",
            )
        if result.timed_out:
            tail = _bounded_log_tail(log_file)
            self.discard(staging)
            raise FastCompileError(
                "FAST_COMPILE_TIMEOUT",
                "The fast Java compilation exceeded its bounded timeout.",
                retryable=True,
                suggested_next_step=(
                    "Use the formal Maven build when this source cannot be "
                    "compiled quickly in isolation."
                ),
                context={"compile_log_tail": tail},
            )
        if not result.succeeded:
            tail = _bounded_log_tail(log_file)
            self.discard(staging)
            raise FastCompileError(
                "FAST_COMPILE_FAILED",
                "javac rejected the requested source update.",
                retryable=True,
                suggested_next_step=(
                    "Inspect compile_log_tail. If the source needs annotation "
                    "processing, generated sources, custom compiler plugins, "
                    "or other modules, use the formal Maven build and restart."
                ),
                context={
                    "return_code": result.return_code,
                    "compile_log_tail": tail,
                },
            )
        return CompileAttemptResult(
            staging_directory=staging,
            classes_directory=classes,
            log_file=log_file,
            arg_file=arg_file,
            source_files=source_files,
            source_hashes=source_hashes,
            elapsed_seconds=result.finished_at - result.started_at,
        )

    @staticmethod
    def discard(attempt: CompileAttemptResult | Path | None) -> None:
        if attempt is None:
            return
        directory = (
            attempt.staging_directory
            if isinstance(attempt, CompileAttemptResult)
            else attempt
        )
        shutil.rmtree(directory, ignore_errors=True)


def fast_compile_fingerprint(
    *,
    configuration_inputs: Iterable[Path],
    javac_executable: Path,
    compile_classpath: Iterable[Path],
) -> str:
    """Hash configuration content and dependency identity, never source output."""
    digest = hashlib.sha256()
    for path in sorted(
        (Path(item).resolve(strict=False) for item in configuration_inputs),
        key=lambda item: os.path.normcase(str(item)),
    ):
        digest.update(b"config\0")
        digest.update(str(path).encode("utf-8", errors="surrogateescape"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    javac = javac_executable.resolve(strict=False)
    digest.update(b"javac\0")
    digest.update(str(javac).encode("utf-8", errors="surrogateescape"))
    try:
        stat = javac.stat()
        digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
    except OSError:
        digest.update(b"<missing>")
    for path in compile_classpath:
        normalized = Path(path).resolve(strict=False)
        digest.update(b"classpath\0")
        digest.update(
            str(normalized).encode("utf-8", errors="surrogateescape")
        )
        if normalized.is_file():
            try:
                stat = normalized.stat()
                digest.update(
                    f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii")
                )
            except OSError:
                digest.update(b"<missing>")
        elif normalized.is_dir():
            # Workspace output directories are mutable compile inputs. Hash
            # their class contents so an external Maven/IDE build cannot make
            # javac see a newer dependency generation than the running JVM.
            try:
                class_files = sorted(
                    normalized.rglob("*.class"),
                    key=lambda item: os.path.normcase(
                        item.relative_to(normalized).as_posix()
                    ),
                )
            except OSError:
                digest.update(b"<unreadable-directory>")
                continue
            for class_file in class_files:
                try:
                    relative = class_file.relative_to(normalized).as_posix()
                    digest.update(b"class\0")
                    digest.update(
                        relative.encode("utf-8", errors="surrogateescape")
                    )
                    digest.update(class_file.read_bytes())
                except OSError:
                    digest.update(b"<unreadable-class>")
        else:
            digest.update(b"<missing>")
    return digest.hexdigest()


def _write_javac_argfile(path: Path, arguments: Iterable[str]) -> None:
    # JDK 8 reads @argfiles using the platform encoding.  Newer JDKs also
    # accept it, so using the host encoding keeps Windows paths lossless.
    encoding = locale.getpreferredencoding(False) or "utf-8"
    lines = [_quote_javac_argument(str(argument)) for argument in arguments]
    path.write_text("\n".join(lines) + "\n", encoding=encoding)


def _quote_javac_argument(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _bounded_log_tail(path: Path) -> list[str]:
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    raw = raw[-32 * 1024 :]
    text = raw.decode(
        locale.getpreferredencoding(False) or "utf-8",
        errors="replace",
    )
    return text.splitlines()[-20:]


__all__ = [
    "CompileAttemptResult",
    "FastCompileError",
    "FastCompilePlan",
    "FastCompiler",
    "fast_compile_fingerprint",
]
