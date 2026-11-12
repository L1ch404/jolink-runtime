"""Materialize a resolved project launch without shell command strings."""

from __future__ import annotations

import os
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import JvmLaunchPlan
from .jpms_runtime import ModuleRuntime, module_vm_arguments


_WINDOWS_COMMAND_LIMIT = 30_000
_MANIFEST_LINE_LIMIT = 72


def argument_file_command(command, path):
    """Materialize Java9+ arguments without shell quoting or a pathing manifest."""
    def quoted(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'
    path.write_text("\n".join(quoted(value) for value in command[1:]) + "\n", encoding="utf-8")
    return (command[0], "@" + str(path))


@dataclass(frozen=True)
class MaterializedJavaCommand:
    """An argv vector plus any file that must live as long as the JVM."""

    argv: tuple[str, ...] = field(repr=False)
    materialization: str
    retained_files: tuple[Path, ...] = ()

    def diagnostic_summary(self) -> dict[str, object]:
        return {
            "executable": Path(self.argv[0]).name if self.argv else "",
            "argument_count": max(0, len(self.argv) - 1),
            "materialization": self.materialization,
            "retained_file_count": len(self.retained_files),
        }


class JavaCommandMaterializer:
    """Use direct ``-cp`` normally and a JDK-8 pathing JAR when required."""

    def materialize(
        self,
        plan: JvmLaunchPlan,
        *,
        jdwp_port: int,
        attempt_directory: Path,
        force_pathing_jar: bool = False,
        windows: bool | None = None,
        windows_command_limit: int = _WINDOWS_COMMAND_LIMIT,
    ) -> MaterializedJavaCommand:
        is_windows = os.name == "nt" if windows is None else bool(windows)
        debug_argument = (
            "-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,"
            f"address=127.0.0.1:{jdwp_port}"
        )
        classpath = os.pathsep.join(str(path) for path in plan.classpath)
        jvm_args, module_path = plan.jvm_args, plan.module_path
        if plan.module_name:
            jvm_args, module_path = module_vm_arguments(plan.jvm_args,
                ModuleRuntime(plan.module_name, plan.classpath, plan.module_path, plan.module_arguments),
                None, directory=plan.working_directory)
        direct = (
            str(plan.java_executable),
            debug_argument,
            *jvm_args,
            *(("--module-path", os.pathsep.join(str(p) for p in module_path)) if module_path else ()),
            "-cp", classpath,
            *(("--module", plan.module_name + "/" + plan.main_class) if plan.module_name else (plan.main_class,)),
            *plan.program_args,
        )
        needs_pathing_jar = force_pathing_jar or (
            is_windows
            and len(subprocess.list2cmdline(list(direct)))
            > int(windows_command_limit)
        )
        if not needs_pathing_jar:
            return MaterializedJavaCommand(
                argv=direct,
                materialization="direct_module" if plan.module_name else "direct_classpath",
            )

        if plan.module_name:
            # Module launches require Java9+, whose argument files can retain a
            # long module path; a manifest Class-Path jar cannot represent it.
            attempt_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            arguments = attempt_directory / "runtime-arguments.txt"
            return MaterializedJavaCommand(argument_file_command(direct, arguments), "module_argfile", (arguments,))

        attempt_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        pathing_jar = attempt_directory / "runtime-classpath.jar"
        self._write_pathing_jar(pathing_jar, plan.classpath)
        argv = (
            str(plan.java_executable),
            debug_argument,
            *plan.jvm_args,
            "-cp",
            str(pathing_jar),
            plan.main_class,
            *plan.program_args,
        )
        return MaterializedJavaCommand(
            argv=argv,
            materialization="pathing_jar",
            retained_files=(pathing_jar,),
        )

    @classmethod
    def _write_pathing_jar(
        cls,
        destination: Path,
        classpath: tuple[Path, ...],
    ) -> None:
        uris = [cls._classpath_uri(path) for path in classpath]
        manifest = (
            b"Manifest-Version: 1.0\r\n"
            + cls._fold_manifest_header(
                "Class-Path",
                " ".join(uris),
            )
            + b"\r\n"
        )
        temporary = destination.with_suffix(".tmp")
        try:
            with zipfile.ZipFile(
                temporary,
                "w",
                compression=zipfile.ZIP_STORED,
            ) as archive:
                archive.writestr("META-INF/MANIFEST.MF", manifest)
            temporary.replace(destination)
            try:
                destination.chmod(0o600)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _classpath_uri(path: Path) -> str:
        resolved = path.expanduser().resolve(strict=False)
        uri = resolved.as_uri()
        if resolved.is_dir() and not uri.endswith("/"):
            uri += "/"
        return uri

    @staticmethod
    def _fold_manifest_header(name: str, value: str) -> bytes:
        raw = f"{name}: {value}".encode("utf-8")
        lines: list[bytes] = []
        first = True
        while raw:
            prefix = b"" if first else b" "
            capacity = _MANIFEST_LINE_LIMIT - len(prefix)
            lines.append(prefix + raw[:capacity])
            raw = raw[capacity:]
            first = False
        if not lines:
            lines.append(f"{name}: ".encode("utf-8"))
        return b"\r\n".join(lines) + b"\r\n"


__all__ = [
    "JavaCommandMaterializer",
    "MaterializedJavaCommand",
]
