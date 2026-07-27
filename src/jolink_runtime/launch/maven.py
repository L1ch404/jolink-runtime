"""Maven P0 workspace model, operation specs, and classpath consumption."""

from __future__ import annotations

import locale
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import (
    BuildOperationSpec,
    BuildPlan,
    JvmLaunchPlan,
    LaunchErrorCode,
    LaunchIntent,
)
from .idea_environment import IdeaBuildPreferences
from .toolchain import (
    JavaToolchainCandidate,
    JavaToolchainResolver,
    MavenToolCandidate,
)


_MAX_POM_BYTES = 2 * 1024 * 1024
_MAX_MODULES = 128
_SAFE_COORDINATE = re.compile(r"^[A-Za-z0-9_.-]+$")
_DEPENDENCY_PLUGIN_GOAL = (
    "org.apache.maven.plugins:"
    "maven-dependency-plugin:3.6.1:build-classpath"
)


class MavenResolutionError(RuntimeError):
    """Structured, redacted Maven planning or result failure."""

    def __init__(
        self,
        error_code: LaunchErrorCode,
        message: str,
        *,
        retryable: bool,
        suggested_next_step: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.suggested_next_step = suggested_next_step
        self.context = context or {}


@dataclass(frozen=True)
class MavenModule:
    relative_path: str
    directory: Path
    pom_file: Path
    group_id: str
    artifact_id: str
    name: str | None
    packaging: str
    output_directory: Path

    def redacted_summary(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "group_id": self.group_id,
            "artifact_id": self.artifact_id,
            "name": self.name,
            "packaging": self.packaging,
        }


@dataclass(frozen=True)
class MavenWorkspace:
    project_root: Path
    build_root: Path
    root_pom: Path
    modules: tuple[MavenModule, ...]


@dataclass(frozen=True)
class MavenExecutionPlan:
    build_plan: BuildPlan
    workspace: MavenWorkspace
    module: MavenModule
    maven: MavenToolCandidate
    build_jdk: JavaToolchainCandidate
    preferences: IdeaBuildPreferences
    build_log: Path
    classpath_file: Path


class MavenBuildSystemAdapter:
    """Create inert Maven operations; execution belongs to ProcessSupervisor."""

    kind = "maven"

    def detect(self, project_path: Path) -> bool:
        return (project_path / "pom.xml").is_file()

    def resolve_workspace(self, project_path: Path) -> MavenWorkspace:
        try:
            root = project_path.expanduser().resolve(strict=True)
        except OSError as error:
            raise MavenResolutionError(
                LaunchErrorCode.INVALID_PROJECT_PATH,
                "project_path does not identify a readable directory.",
                retryable=True,
                suggested_next_step=(
                    "Provide the canonical Maven project directory and "
                    "retry run."
                ),
            ) from error
        if not root.is_dir():
            raise MavenResolutionError(
                LaunchErrorCode.INVALID_PROJECT_PATH,
                "project_path does not identify a directory.",
                retryable=True,
                suggested_next_step=(
                    "Provide the Maven project directory and retry run."
                ),
            )
        root_pom = root / "pom.xml"
        if not root_pom.is_file():
            raise MavenResolutionError(
                LaunchErrorCode.BUILD_SYSTEM_NOT_FOUND,
                "No Maven pom.xml was found at project_path.",
                retryable=True,
                suggested_next_step=(
                    "Provide the Maven reactor root as project_path or use "
                    "the existing direct classpath/JAR launch."
                ),
            )

        modules: list[MavenModule] = []
        visited: set[Path] = set()
        self._collect_modules(
            project_root=root,
            pom_file=root_pom,
            modules=modules,
            visited=visited,
        )
        return MavenWorkspace(
            project_root=root,
            build_root=root,
            root_pom=root_pom,
            modules=tuple(modules),
        )

    def select_module(
        self,
        workspace: MavenWorkspace,
        intent: LaunchIntent,
    ) -> MavenModule:
        modules = list(workspace.modules)
        if len(modules) == 1 and modules[0].packaging != "pom":
            return modules[0]

        main_source = Path(
            "src/main/java",
            *intent.main_class.split("."),
        ).with_suffix(".java")
        source_matches = [
            module
            for module in modules
            if module.packaging != "pom"
            and (module.directory / main_source).is_file()
        ]
        if len(source_matches) == 1:
            return source_matches[0]
        if len(source_matches) > 1:
            self._raise_ambiguous(intent, source_matches)

        if intent.ide_module_name:
            module_name = intent.ide_module_name
            name_matches = [
                module
                for module in modules
                if module.packaging != "pom"
                and module_name
                in {
                    module.relative_path,
                    module.directory.name,
                    module.artifact_id,
                    module.name,
                }
            ]
            if len(name_matches) == 1:
                return name_matches[0]
            if len(name_matches) > 1:
                self._raise_ambiguous(intent, name_matches)

        runnable = [
            module
            for module in modules
            if module.packaging != "pom"
        ]
        if len(runnable) == 1:
            return runnable[0]
        if len(runnable) > 1:
            self._raise_ambiguous(intent, runnable)
        raise MavenResolutionError(
            LaunchErrorCode.BUILD_MODULE_NOT_FOUND,
            "No Maven module can contain the selected main class.",
            retryable=True,
            suggested_next_step=(
                "Choose an IDEA configuration whose module maps to one Maven "
                "module, or provide a direct classpath launch."
            ),
            context={
                "ide_module_name": intent.ide_module_name,
                "main_class": intent.main_class,
            },
        )

    def create_execution_plan(
        self,
        *,
        workspace: MavenWorkspace,
        module: MavenModule,
        intent: LaunchIntent,
        maven: MavenToolCandidate,
        build_jdk: JavaToolchainCandidate,
        preferences: IdeaBuildPreferences,
        attempt_directory: Path,
    ) -> MavenExecutionPlan:
        attempt_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            attempt_directory.chmod(0o700)
        except OSError:
            pass
        self._validate_optional_file(
            preferences.user_settings_file,
            name="IDEA Maven settings.xml",
        )
        if (
            preferences.local_repository is not None
            and preferences.local_repository.exists()
            and not preferences.local_repository.is_dir()
        ):
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "The configured Maven local repository is not a directory.",
                retryable=True,
                suggested_next_step=(
                    "Correct the IDEA Maven local repository setting and "
                    "retry run."
                ),
            )

        build_plan = BuildPlan(
            build_system=self.kind,
            build_root=workspace.build_root,
            target_module=module.relative_path,
            build_java_executable=build_jdk.java_executable,
            compile_required=intent.build_before_run,
            provider_options={
                "maven_argv_prefix": maven.argv_prefix,
                "maven_source": maven.source,
                "settings_xml": preferences.user_settings_file,
                "local_repository": preferences.local_repository,
                "profiles": preferences.active_profiles,
            },
        )
        return MavenExecutionPlan(
            build_plan=build_plan,
            workspace=workspace,
            module=module,
            maven=maven,
            build_jdk=build_jdk,
            preferences=preferences,
            build_log=attempt_directory / "build.log",
            classpath_file=(
                attempt_directory
                / f"{module.group_id}-{module.artifact_id}.classpath"
            ),
        )

    def create_build_operation(
        self,
        execution: MavenExecutionPlan,
    ) -> BuildOperationSpec:
        arguments: list[str] = [
            *execution.maven.argv_prefix,
            "--batch-mode",
            "--fail-fast",
            "-T",
            "1",
            "-Dstyle.color=never",
            "-f",
            str(execution.workspace.root_pom),
        ]
        preferences = execution.preferences
        if preferences.user_settings_file is not None:
            arguments.extend(
                ["-s", str(preferences.user_settings_file)]
            )
        if preferences.local_repository is not None:
            arguments.append(
                f"-Dmaven.repo.local={preferences.local_repository}"
            )
        if preferences.active_profiles:
            arguments.extend(
                ["-P", ",".join(preferences.active_profiles)]
            )
        if execution.module.relative_path != ".":
            arguments.extend(
                ["-pl", execution.module.relative_path, "-am"]
            )
        if execution.build_plan.compile_required:
            arguments.append("compile")
        arguments.extend(
            [
                _DEPENDENCY_PLUGIN_GOAL,
                "-DincludeScope=runtime",
                (
                    "-Dmdep.outputFile="
                    f"{execution.classpath_file.parent}"
                    "/${project.groupId}-${project.artifactId}.classpath"
                ),
                f"-Dmdep.pathSeparator={os.pathsep}",
                "-Dmdep.regenerateFile=true",
                "-DoutputEncoding=UTF-8",
            ]
        )
        environment = JavaToolchainResolver.maven_environment(
            execution.build_jdk
        )
        return BuildOperationSpec(
            argv=tuple(arguments),
            cwd=execution.workspace.build_root,
            environment=environment,
            timeout_seconds=None,
            output_capture=execution.build_log,
            operation_name="maven_compile_and_classpath",
        )

    def consume_jvm_launch_plan(
        self,
        *,
        execution: MavenExecutionPlan,
        intent: LaunchIntent,
        runtime_jdk: JavaToolchainCandidate,
        ready_port: int,
        startup_wait_timeout_seconds: float,
    ) -> JvmLaunchPlan:
        if not execution.classpath_file.is_file():
            raise MavenResolutionError(
                LaunchErrorCode.RUNTIME_RESOLUTION_FAILED,
                "Maven succeeded without producing the runtime classpath file.",
                retryable=True,
                suggested_next_step=(
                    "Inspect build.log_tail and verify the Maven dependency "
                    "plugin can run through the configured repository."
                ),
            )
        classpath_text = self._read_classpath_file(
            execution.classpath_file
        )
        entries: list[Path] = [execution.module.output_directory]
        entries.extend(
            Path(raw_entry)
            if Path(raw_entry).is_absolute()
            else execution.module.directory / raw_entry
            for raw_entry in classpath_text.split(os.pathsep)
            if raw_entry.strip()
        )
        normalized: list[Path] = []
        seen: set[str] = set()
        missing: list[str] = []
        for entry in entries:
            resolved = entry.expanduser().resolve(strict=False)
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            seen.add(key)
            if not resolved.exists():
                missing.append(str(resolved))
                continue
            normalized.append(resolved)
        if missing:
            raise MavenResolutionError(
                LaunchErrorCode.RUNTIME_RESOLUTION_FAILED,
                "The resolved Maven runtime classpath contains missing paths.",
                retryable=True,
                suggested_next_step=(
                    "Rebuild the selected module and inspect build.log_tail "
                    "for unresolved reactor or repository dependencies."
                ),
                context={"missing_path_count": len(missing)},
            )

        main_class_file = execution.module.output_directory / Path(
            *intent.main_class.split(".")
        ).with_suffix(".class")
        if not main_class_file.is_file():
            raise MavenResolutionError(
                LaunchErrorCode.RUNTIME_RESOLUTION_FAILED,
                "The selected main class was not compiled in the target module.",
                retryable=True,
                suggested_next_step=(
                    "Enable the IDEA Make task or correct the selected Maven "
                    "module/main class, then retry run."
                ),
                context={
                    "main_class": intent.main_class,
                    "target_module": execution.module.relative_path,
                },
            )
        return JvmLaunchPlan(
            java_executable=runtime_jdk.java_executable,
            classpath=tuple(normalized),
            main_class=intent.main_class,
            working_directory=intent.working_directory,
            jvm_args=intent.jvm_args,
            program_args=intent.program_args,
            environment_overrides=intent.environment,
            ready_port=ready_port,
            startup_wait_timeout_seconds=startup_wait_timeout_seconds,
            command_materialization="direct_classpath",
        )

    def _collect_modules(
        self,
        *,
        project_root: Path,
        pom_file: Path,
        modules: list[MavenModule],
        visited: set[Path],
    ) -> None:
        try:
            resolved_pom = pom_file.resolve(strict=True)
        except OSError as error:
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "A Maven module POM is unreadable.",
                retryable=True,
                suggested_next_step=(
                    "Restore the module POM and retry run."
                ),
            ) from error
        try:
            resolved_pom.relative_to(project_root)
        except ValueError as error:
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "A Maven module points outside project_path.",
                retryable=False,
                suggested_next_step=(
                    "Use the complete Maven reactor root as project_path."
                ),
            ) from error
        if resolved_pom in visited:
            return
        if len(visited) >= _MAX_MODULES:
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "The Maven reactor exceeds the P0 module limit.",
                retryable=False,
                suggested_next_step=(
                    "Use a smaller reactor root or a direct classpath launch."
                ),
            )
        visited.add(resolved_pom)
        xml_root = self._read_pom(resolved_pom)
        directory = resolved_pom.parent
        relative_path = (
            "."
            if directory == project_root
            else directory.relative_to(project_root).as_posix()
        )
        artifact_id = self._child_text(xml_root, "artifactId")
        group_id = (
            self._child_text(xml_root, "groupId")
            or self._nested_text(xml_root, "parent", "groupId")
        )
        if (
            not group_id
            or "${" in group_id
            or not _SAFE_COORDINATE.fullmatch(group_id)
        ):
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "A Maven module has no static safe groupId.",
                retryable=False,
                suggested_next_step="Correct the Maven POM and retry run.",
            )
        if (
            not artifact_id
            or "${" in artifact_id
            or not _SAFE_COORDINATE.fullmatch(artifact_id)
        ):
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "A Maven module is missing artifactId.",
                retryable=False,
                suggested_next_step="Correct the Maven POM and retry run.",
            )
        packaging = self._child_text(xml_root, "packaging") or "jar"
        if "${" in packaging:
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "A Maven module uses a dynamic packaging value.",
                retryable=False,
                suggested_next_step=(
                    "Use a static packaging value or a direct classpath launch."
                ),
            )
        name = self._child_text(xml_root, "name") or None
        output_directory = self._output_directory(xml_root, directory)
        modules.append(
            MavenModule(
                relative_path=relative_path,
                directory=directory,
                pom_file=resolved_pom,
                group_id=group_id,
                artifact_id=artifact_id,
                name=name,
                packaging=packaging,
                output_directory=output_directory,
            )
        )

        dynamic_modules = xml_root.findall(
            "./{*}profiles/{*}profile/{*}modules/{*}module"
        )
        if dynamic_modules:
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "Profile-controlled Maven modules are not supported in P0.",
                retryable=False,
                suggested_next_step=(
                    "Use a static reactor module or a direct classpath launch."
                ),
            )
        for module_element in xml_root.findall(
            "./{*}modules/{*}module"
        ):
            raw_module = (module_element.text or "").strip()
            if not raw_module or "${" in raw_module:
                raise MavenResolutionError(
                    LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                    "Dynamic Maven module paths are not supported in P0.",
                    retryable=False,
                    suggested_next_step=(
                        "Use static module paths or a direct classpath launch."
                    ),
                )
            module_directory = (directory / raw_module).resolve(
                strict=False
            )
            try:
                module_directory.relative_to(project_root)
            except ValueError as error:
                raise MavenResolutionError(
                    LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                    "A Maven module path escapes project_path.",
                    retryable=False,
                    suggested_next_step=(
                        "Use the complete Maven reactor root as project_path."
                    ),
                ) from error
            child_pom = module_directory / "pom.xml"
            if not child_pom.is_file():
                raise MavenResolutionError(
                    LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                    "A declared Maven module has no readable pom.xml.",
                    retryable=True,
                    suggested_next_step=(
                        "Restore the declared module POM or choose another "
                        "project root."
                    ),
                )
            self._collect_modules(
                project_root=project_root,
                pom_file=child_pom,
                modules=modules,
                visited=visited,
            )

    @staticmethod
    def _read_pom(source: Path) -> ET.Element:
        try:
            metadata = source.stat()
            raw = source.read_bytes()
        except OSError as error:
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "A Maven POM could not be read.",
                retryable=True,
                suggested_next_step=(
                    "Restore access to the Maven POM and retry run."
                ),
            ) from error
        if metadata.st_size > _MAX_POM_BYTES:
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "A Maven POM exceeds the P0 size limit.",
                retryable=False,
                suggested_next_step=(
                    "Use a smaller static POM or direct classpath launch."
                ),
            )
        if b"\x00" in raw:
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "Maven POM files must use UTF-8 XML.",
                retryable=False,
                suggested_next_step="Convert the POM to UTF-8 and retry run.",
            )
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "Maven POM files must use UTF-8 XML.",
                retryable=False,
                suggested_next_step="Convert the POM to UTF-8 and retry run.",
            ) from error
        lowered = text.casefold()
        if "<!doctype" in lowered or "<!entity" in lowered:
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "DTD and entity declarations are not accepted in Maven POMs.",
                retryable=False,
                suggested_next_step="Remove the declaration and retry run.",
            )
        try:
            root = ET.fromstring(text)
        except ET.ParseError as error:
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "A Maven POM is not valid XML.",
                retryable=True,
                suggested_next_step="Correct the Maven POM and retry run.",
            ) from error
        node_count = 0
        stack: list[tuple[ET.Element, int]] = [(root, 1)]
        while stack:
            node, depth = stack.pop()
            node_count += 1
            if node_count > 20_000 or depth > 64:
                raise MavenResolutionError(
                    LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                    "A Maven POM exceeds structural safety limits.",
                    retryable=False,
                    suggested_next_step=(
                        "Use a smaller static POM or direct classpath launch."
                    ),
                )
            stack.extend((child, depth + 1) for child in node)
        return root

    @staticmethod
    def _child_text(root: ET.Element, name: str) -> str:
        element = root.find(f"./{{*}}{name}")
        return (element.text or "").strip() if element is not None else ""

    @staticmethod
    def _nested_text(
        root: ET.Element,
        parent: str,
        child: str,
    ) -> str:
        element = root.find(f"./{{*}}{parent}/{{*}}{child}")
        return (element.text or "").strip() if element is not None else ""

    @classmethod
    def _output_directory(
        cls,
        root: ET.Element,
        module_directory: Path,
    ) -> Path:
        element = root.find("./{*}build/{*}outputDirectory")
        if element is None or not (element.text or "").strip():
            return module_directory / "target" / "classes"
        raw = (element.text or "").strip()
        replacements = {
            "${project.basedir}": str(module_directory),
            "${basedir}": str(module_directory),
            "${project.build.directory}": str(
                module_directory / "target"
            ),
        }
        for macro, value in replacements.items():
            raw = raw.replace(macro, value)
        if "${" in raw:
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "The Maven outputDirectory uses an unresolved expression.",
                retryable=False,
                suggested_next_step=(
                    "Use a static outputDirectory or a direct classpath launch."
                ),
            )
        path = Path(raw)
        if not path.is_absolute():
            path = module_directory / path
        return path.resolve(strict=False)

    @staticmethod
    def _read_classpath_file(source: Path) -> str:
        raw = source.read_bytes()
        try:
            return raw.decode("utf-8-sig").strip()
        except UnicodeDecodeError:
            encoding = locale.getpreferredencoding(False) or "utf-8"
            try:
                return raw.decode(encoding).strip()
            except UnicodeDecodeError as error:
                raise MavenResolutionError(
                    LaunchErrorCode.RUNTIME_RESOLUTION_FAILED,
                    "The Maven runtime classpath file is not decodable.",
                    retryable=True,
                    suggested_next_step=(
                        "Use UTF-8 Maven output and retry run."
                    ),
                ) from error

    @staticmethod
    def _validate_optional_file(path: Path | None, *, name: str) -> None:
        if path is not None and not path.is_file():
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                f"The configured {name} does not exist.",
                retryable=True,
                suggested_next_step=(
                    f"Correct the configured {name} and retry run."
                ),
            )

    @staticmethod
    def _raise_ambiguous(
        intent: LaunchIntent,
        modules: list[MavenModule],
    ) -> None:
        raise MavenResolutionError(
            LaunchErrorCode.AMBIGUOUS_BUILD_MODULE,
            "Multiple Maven modules match the IDEA launch intent.",
            retryable=True,
            suggested_next_step=(
                "Choose an IDEA configuration with an exact module/main "
                "class mapping."
            ),
            context={
                "ide_module_name": intent.ide_module_name,
                "main_class": intent.main_class,
                "candidates": [
                    module.redacted_summary() for module in modules
                ],
            },
        )


__all__ = [
    "MavenBuildSystemAdapter",
    "MavenExecutionPlan",
    "MavenModule",
    "MavenResolutionError",
    "MavenWorkspace",
]
