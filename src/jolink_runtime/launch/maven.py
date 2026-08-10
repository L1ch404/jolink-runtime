"""Maven P0 workspace model, operation specs, and classpath consumption."""

from __future__ import annotations

import locale
import hashlib
import os
import re
import xml.etree.ElementTree as ET
import zipfile
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
from .fast_compile import FastCompilePlan, fast_compile_fingerprint
from .toolchain import (
    JavaToolchainCandidate,
    JavaToolchainResolver,
    MavenToolCandidate,
)


_MAX_POM_BYTES = 2 * 1024 * 1024
_MAX_EFFECTIVE_POM_BYTES = 32 * 1024 * 1024
_MAX_MAVEN_PROJECT_CONFIG_BYTES = 1024 * 1024
_MAX_MODULES = 128
_SAFE_COORDINATE = re.compile(r"^[A-Za-z0-9_.-]+$")
_DEPENDENCY_PLUGIN_GOAL = (
    "org.apache.maven.plugins:"
    "maven-dependency-plugin:3.6.1:build-classpath"
)
_HELP_PLUGIN_GOAL = (
    "org.apache.maven.plugins:"
    "maven-help-plugin:3.2.0:effective-pom"
)
_PROCESSOR_SERVICE = (
    "META-INF/services/javax.annotation.processing.Processor"
)
_BYTECODE_TRANSFORM_PLUGINS = frozenset(
    {
        "aspectj-maven-plugin",
        "apt-maven-plugin",
        "byte-buddy-maven-plugin",
        "eclipselink-staticweave-maven-plugin",
        "hibernate-enhance-maven-plugin",
        "lombok-maven-plugin",
        "maven-processor-plugin",
        "openjpa-maven-plugin",
    }
)
_BYTECODE_TRANSFORM_GOAL_TOKENS = (
    "aspect",
    "enhance",
    "instrument",
    "redefine",
    "rewrite",
    "transform",
    "weave",
)
_FAST_COMPILE_AFFECTING_PHASES = frozenset(
    {
        "validate",
        "initialize",
        "generate-sources",
        "process-sources",
        "generate-resources",
        "process-resources",
        "compile",
        "process-classes",
    }
)
_SAFE_AFFECTING_PHASE_PLUGIN_GOALS = {
    (
        "org.apache.maven.plugins",
        "maven-compiler-plugin",
        "compile",
    ): frozenset({"compile"}),
    (
        "org.apache.maven.plugins",
        "maven-enforcer-plugin",
        "validate",
    ): frozenset({"enforce"}),
    (
        "org.apache.maven.plugins",
        "maven-resources-plugin",
        "process-resources",
    ): frozenset({"resources"}),
}
_SAFE_IMPLICIT_PHASE_PLUGIN_GOALS = {
    ("org.apache.maven.plugins", "maven-clean-plugin"): frozenset(
        {"clean"}
    ),
    ("org.apache.maven.plugins", "maven-compiler-plugin"): frozenset(
        {"compile", "testcompile"}
    ),
    ("org.apache.maven.plugins", "maven-deploy-plugin"): frozenset(
        {"deploy"}
    ),
    ("org.apache.maven.plugins", "maven-enforcer-plugin"): frozenset(
        {"enforce"}
    ),
    ("org.apache.maven.plugins", "maven-failsafe-plugin"): frozenset(
        {"integration-test", "verify"}
    ),
    ("org.apache.maven.plugins", "maven-install-plugin"): frozenset(
        {"install"}
    ),
    ("org.apache.maven.plugins", "maven-jar-plugin"): frozenset(
        {"jar", "test-jar"}
    ),
    ("org.apache.maven.plugins", "maven-resources-plugin"): frozenset(
        {"copy-resources", "resources", "testresources"}
    ),
    ("org.apache.maven.plugins", "maven-site-plugin"): frozenset(
        {"deploy", "site"}
    ),
    # source:jar-no-fork has declared package as its default phase since it
    # was introduced in maven-source-plugin 2.1.  Unlike source:jar, it does
    # not fork an earlier lifecycle, so an execution without an explicit
    # phase cannot affect the compile/process-classes model validated here.
    ("org.apache.maven.plugins", "maven-source-plugin"): frozenset(
        {"jar-no-fork"}
    ),
    ("org.apache.maven.plugins", "maven-surefire-plugin"): frozenset(
        {"test"}
    ),
    ("org.springframework.boot", "spring-boot-maven-plugin"): frozenset(
        {"build-info", "repackage", "start", "stop"}
    ),
}
_SAFE_COMPILER_CONFIGURATION = frozenset(
    {
        "compilerId",
        "debug",
        "debuglevel",
        "encoding",
        "enablePreview",
        "executable",
        "failOnWarning",
        "forceJavacCompilerUse",
        "forceLegacyJavacApi",
        "fork",
        "parameters",
        "proc",
        "release",
        "showDeprecation",
        "showWarnings",
        "source",
        "staleMillis",
        "target",
        "useIncrementalCompilation",
        "verbose",
    }
)
_MAVEN_ENVIRONMENT_INPUTS = ("MAVEN_ARGS", "MAVEN_OPTS")
_MAVEN_PROJECT_CONFIG_NAMES = (
    "maven.config",
    "jvm.config",
    "extensions.xml",
)
_UNMODELED_MAVEN_ARGUMENT_PATTERN = re.compile(
    r"(?i)(?:maven\.compiler\.|maven\.ext\.class\.path|"
    r"project\.build\.sourceencoding|(?:^|[\s'\"])-dencoding(?:=|\s)|"
    r"-javaagent:|-xbootclasspath(?:/a|/p)?:|--patch-module)"
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
    compile_classpath_file: Path
    effective_pom_file: Path


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
            compile_classpath_file=(
                attempt_directory
                / (
                    f"{module.group_id}-{module.artifact_id}"
                    ".compile-classpath"
                )
            ),
            effective_pom_file=(
                attempt_directory / "effective-reactor-pom.xml"
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

    def create_compile_classpath_operation(
        self,
        execution: MavenExecutionPlan,
    ) -> BuildOperationSpec:
        """Resolve compile scope separately without changing launch behavior."""
        arguments = self._base_maven_arguments(execution)
        if execution.module.relative_path != ".":
            arguments.extend(
                ["-pl", execution.module.relative_path, "-am"]
            )
        arguments.extend(
            [
                _DEPENDENCY_PLUGIN_GOAL,
                "-DincludeScope=compile",
                (
                    "-Dmdep.outputFile="
                    f"{execution.compile_classpath_file.parent}"
                    "/${project.groupId}-${project.artifactId}"
                    ".compile-classpath"
                ),
                f"-Dmdep.pathSeparator={os.pathsep}",
                "-Dmdep.regenerateFile=true",
                "-DoutputEncoding=UTF-8",
                _HELP_PLUGIN_GOAL,
                f"-Doutput={execution.effective_pom_file}",
            ]
        )
        return BuildOperationSpec(
            argv=tuple(arguments),
            cwd=execution.workspace.build_root,
            environment=JavaToolchainResolver.maven_environment(
                execution.build_jdk
            ),
            timeout_seconds=60.0,
            output_capture=execution.build_log,
            operation_name="maven_compile_classpath",
        )

    def consume_fast_compile_plan(
        self,
        *,
        execution: MavenExecutionPlan,
        runtime_jdk: JavaToolchainCandidate,
    ) -> FastCompilePlan:
        """Create the optional, strict source-update plan.

        Failure here must be handled as a capability warning by the project
        launcher; it must never invalidate an otherwise valid JVM launch plan.
        """
        source_root = execution.module.directory / "src" / "main" / "java"
        if not source_root.is_dir():
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "The selected Maven module has no standard src/main/java root.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build for this project layout."
                ),
            )
        if execution.module.packaging != "jar":
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "Fast source update supports standard Maven jar modules only.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build for this packaging type."
                ),
            )
        if (source_root / "module-info.java").is_file():
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "Fast source update does not model the Java module path.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build and restart this JPMS module."
                ),
            )
        if not execution.compile_classpath_file.is_file():
            raise MavenResolutionError(
                LaunchErrorCode.RUNTIME_RESOLUTION_FAILED,
                "Maven did not produce the optional compile classpath.",
                retryable=True,
                suggested_next_step=(
                    "Use the formal Maven build and restart for this change."
                ),
            )
        if not execution.effective_pom_file.is_file():
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "Maven did not produce the effective compiler model.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build and restart the application."
                ),
            )

        effective_root = self._read_effective_pom(
            execution.effective_pom_file
        )
        effective_project = self._select_effective_project(
            effective_root,
            execution.module,
        )
        reactor_artifacts = self._reactor_artifacts(
            effective_root,
            execution.workspace,
        )

        classpath_text = self._read_classpath_file(
            execution.compile_classpath_file
        )
        entries: list[Path] = [execution.module.output_directory]
        for raw_entry in classpath_text.split(os.pathsep):
            if not raw_entry.strip():
                continue
            raw_path = (
                Path(raw_entry)
                if Path(raw_entry).is_absolute()
                else execution.module.directory / raw_entry
            )
            resolved_raw = raw_path.resolve(strict=False)
            replacement = self._reactor_output_for_path(
                resolved_raw,
                reactor_artifacts,
            )
            if replacement is None and self._looks_like_workspace_artifact(
                resolved_raw,
                execution.workspace,
            ):
                raise MavenResolutionError(
                    LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                    "A Reactor classpath artifact has no effective mapping.",
                    retryable=False,
                    suggested_next_step=(
                        "Use the formal Maven build and restart the "
                        "application."
                    ),
                )
            entries.append(replacement or raw_path)
        normalized: list[Path] = []
        seen: set[str] = set()
        missing: list[Path] = []
        for entry in entries:
            resolved = entry.expanduser().resolve(strict=False)
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            if not resolved.exists():
                missing.append(resolved)
                continue
            seen.add(key)
            normalized.append(resolved)
        if missing:
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "The Maven compile classpath contains missing entries.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build and restart after dependency "
                    "resolution is complete."
                ),
                context={"missing_path_count": len(missing)},
            )
        if not normalized:
            raise MavenResolutionError(
                LaunchErrorCode.RUNTIME_RESOLUTION_FAILED,
                "The optional compile classpath is empty.",
                retryable=True,
                suggested_next_step=(
                    "Use the formal Maven build and restart for this change."
                ),
            )

        maven_project_inputs = tuple(
            execution.workspace.build_root / ".mvn" / name
            for name in _MAVEN_PROJECT_CONFIG_NAMES
        )
        self._ensure_no_unverified_maven_extensions(
            effective_project,
            maven_project_inputs=maven_project_inputs,
        )
        self._ensure_no_unverified_build_transforms(
            effective_project,
            tuple(normalized),
        )
        compiler_model = self._compiler_model(
            effective_project,
            build_jdk=execution.build_jdk,
            runtime_jdk=runtime_jdk,
        )

        configuration_inputs: list[Path] = [
            module.pom_file
            for module in execution.workspace.modules
        ]
        configuration_inputs.extend(
            (
                execution.compile_classpath_file,
                execution.effective_pom_file,
                *maven_project_inputs,
            )
        )
        if execution.preferences.user_settings_file is not None:
            configuration_inputs.append(
                execution.preferences.user_settings_file
            )
        encoding = self._source_encoding(effective_project)
        fingerprint = fast_compile_fingerprint(
            configuration_inputs=configuration_inputs,
            configuration_environment_names=_MAVEN_ENVIRONMENT_INPUTS,
            javac_executable=execution.build_jdk.javac_executable,
            compile_classpath=normalized,
        )
        baseline_class_hashes: dict[str, str] = {}
        output_targets: set[int] = set()
        class_files = sorted(
            execution.module.output_directory.rglob("*.class")
        )
        if len(class_files) > 50_000:
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "The module class output exceeds the fast-update file limit.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build and restart for this module."
                ),
            )
        for class_file in class_files:
            try:
                relative = class_file.relative_to(
                    execution.module.output_directory
                ).as_posix()
                class_bytes = class_file.read_bytes()
                baseline_class_hashes[relative] = hashlib.sha256(
                    class_bytes
                ).hexdigest()
                output_targets.add(
                    self._class_file_java_release(class_bytes)
                )
            except OSError as error:
                raise MavenResolutionError(
                    LaunchErrorCode.RUNTIME_RESOLUTION_FAILED,
                    "The compiled class output changed while it was inspected.",
                    retryable=True,
                    suggested_next_step=(
                        "Wait for external builds to finish, then restart the "
                        "project before using update."
                    ),
                ) from error
            except ValueError as error:
                raise MavenResolutionError(
                    LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                    "A formal class output has an invalid bytecode header.",
                    retryable=False,
                    suggested_next_step=(
                        "Use the formal Maven build and restart the application."
                    ),
                ) from error
        if output_targets != {compiler_model["target_level"]}:
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "The effective Maven target does not match formal class output.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build and restart with one verified "
                    "compiler target."
                ),
                context={
                    "effective_target": compiler_model["target_level"],
                    "output_target_count": len(output_targets),
                },
            )
        return FastCompilePlan(
            project_root=execution.workspace.project_root,
            module_root=execution.module.directory,
            source_root=source_root.resolve(strict=True),
            output_root=execution.module.output_directory.resolve(
                strict=False
            ),
            javac_executable=execution.build_jdk.javac_executable,
            compile_classpath=tuple(normalized),
            build_jdk_major=compiler_model["build_jdk_major"],
            runtime_jdk_major=compiler_model["runtime_jdk_major"],
            source_level=compiler_model["source_level"],
            target_level=compiler_model["target_level"],
            release_level=compiler_model["release_level"],
            javac_platform_args=compiler_model["javac_platform_args"],
            encoding=encoding,
            configuration_inputs=tuple(configuration_inputs),
            configuration_environment_names=_MAVEN_ENVIRONMENT_INPUTS,
            configuration_fingerprint=fingerprint,
            baseline_class_hashes=baseline_class_hashes,
            target_module=execution.module.relative_path,
        )

    def _read_effective_pom(self, source: Path) -> ET.Element:
        try:
            metadata = source.stat()
            raw = source.read_bytes()
        except OSError as error:
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "The effective Maven model could not be read.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build and restart the application."
                ),
            ) from error
        if metadata.st_size > _MAX_EFFECTIVE_POM_BYTES or b"\x00" in raw:
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "The effective Maven model exceeds safety limits.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build and restart the application."
                ),
            )
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "The effective Maven model is not UTF-8 XML.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build and restart the application."
                ),
            ) from error
        lowered = text.casefold()
        if "<!doctype" in lowered or "<!entity" in lowered:
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "The effective Maven model contains unsupported declarations.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build and restart the application."
                ),
            )
        try:
            root = ET.fromstring(text)
        except ET.ParseError as error:
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "The effective Maven model is invalid XML.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build and restart the application."
                ),
            ) from error
        node_count = 0
        stack = [root]
        while stack:
            node = stack.pop()
            node_count += 1
            if node_count > 250_000:
                raise MavenResolutionError(
                    LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                    "The effective Maven model exceeds structural limits.",
                    retryable=False,
                    suggested_next_step=(
                        "Use the formal Maven build and restart the application."
                    ),
                )
            stack.extend(node)
        return root

    def _select_effective_project(
        self,
        root: ET.Element,
        module: MavenModule,
    ) -> ET.Element:
        projects = (
            [root]
            if self._local_name(root.tag) == "project"
            else list(root.findall("./{*}project"))
        )
        matches = [
            project
            for project in projects
            if self._child_text(project, "groupId") == module.group_id
            and self._child_text(project, "artifactId") == module.artifact_id
        ]
        if len(matches) != 1:
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "The selected module has no unique effective Maven model.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build and restart the application."
                ),
                context={"matching_model_count": len(matches)},
            )
        return matches[0]

    def _reactor_artifacts(
        self,
        root: ET.Element,
        workspace: MavenWorkspace,
    ) -> tuple[tuple[MavenModule, tuple[str, str, str]], ...]:
        """Map Reactor artifact paths without widening Maven's classpath."""
        projects = (
            [root]
            if self._local_name(root.tag) == "project"
            else list(root.findall("./{*}project"))
        )
        effective_by_ga: dict[
            tuple[str, str],
            list[tuple[str, str, str]],
        ] = {}
        for project in projects:
            coordinate = self._effective_coordinate(project)
            if coordinate is not None:
                effective_by_ga.setdefault(coordinate[:2], []).append(
                    coordinate
                )
        workspace_ga = {
            (module.group_id, module.artifact_id)
            for module in workspace.modules
            if module.packaging != "pom"
        }
        for project in projects:
            for dependency in project.findall(
                "./{*}dependencies/{*}dependency"
            ):
                dependency_ga = (
                    self._child_text(dependency, "groupId"),
                    self._child_text(dependency, "artifactId"),
                )
                dependency_type = (
                    self._child_text(dependency, "type") or "jar"
                )
                classifier = self._child_text(
                    dependency,
                    "classifier",
                )
                if (
                    dependency_ga in workspace_ga
                    and (
                        dependency_type != "jar"
                        or bool(classifier)
                    )
                ):
                    raise MavenResolutionError(
                        LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                        "A Reactor dependency uses an unsupported artifact "
                        "type or classifier.",
                        retryable=False,
                        suggested_next_step=(
                            "Use the formal Maven build and restart the "
                            "application."
                        ),
                    )

        artifacts: list[
            tuple[MavenModule, tuple[str, str, str]]
        ] = []
        for module in workspace.modules:
            if module.packaging == "pom":
                continue
            coordinates = effective_by_ga.get(
                (module.group_id, module.artifact_id),
                [],
            )
            if len(coordinates) > 1:
                raise MavenResolutionError(
                    LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                    "A Reactor module has ambiguous effective coordinates.",
                    retryable=False,
                    suggested_next_step=(
                        "Use the formal Maven build and restart the "
                        "application."
                    ),
                    context={
                        "matching_coordinate_count": len(coordinates),
                    },
                )
            if not coordinates:
                continue
            artifacts.append((module, coordinates[0]))
        return tuple(artifacts)

    @classmethod
    def _effective_coordinate(
        cls,
        element: ET.Element,
    ) -> tuple[str, str, str] | None:
        group_id = cls._child_text(element, "groupId")
        artifact_id = cls._child_text(element, "artifactId")
        version = cls._child_text(element, "version")
        if (
            not group_id
            or not artifact_id
            or not version
            or "${" in group_id
            or "${" in artifact_id
            or "${" in version
        ):
            return None
        return group_id, artifact_id, version

    @staticmethod
    def _reactor_output_for_path(
        path: Path,
        artifacts: tuple[
            tuple[MavenModule, tuple[str, str, str]],
            ...,
        ],
    ) -> Path | None:
        for module, coordinate in artifacts:
            output = module.output_directory.resolve(strict=False)
            if path == output:
                return output
            group_id, artifact_id, version = coordinate
            group_parts = tuple(group_id.split("."))
            expected_tail = (*group_parts, artifact_id, version)
            parent_parts = path.parent.parts
            if (
                len(parent_parts) >= len(expected_tail)
                and tuple(parent_parts[-len(expected_tail) :])
                == expected_tail
            ):
                return output
        return None

    @staticmethod
    def _looks_like_workspace_artifact(
        path: Path,
        workspace: MavenWorkspace,
    ) -> bool:
        parent_parts = path.parent.parts
        for module in workspace.modules:
            if module.packaging == "pom":
                continue
            expected = (*module.group_id.split("."), module.artifact_id)
            # Maven repository layout ends in group/artifact/version/file.
            if (
                len(parent_parts) >= len(expected) + 1
                and tuple(
                    parent_parts[
                        -(len(expected) + 1) : -1
                    ]
                )
                == expected
            ):
                return True
        return False

    def _ensure_no_unverified_maven_extensions(
        self,
        project: ET.Element,
        *,
        maven_project_inputs: tuple[Path, ...],
    ) -> None:
        """Reject Maven extension/compiler inputs outside the frozen model."""

        if project.findall("./{*}build/{*}extensions/{*}extension"):
            self._raise_unverified_compiler_model(
                "Maven build extensions are not modeled by fast update."
            )
        for plugin in project.findall("./{*}build/{*}plugins/{*}plugin"):
            extension_flag = plugin.find("./{*}extensions")
            if extension_flag is not None and (
                (extension_flag.text or "").strip().casefold() != "false"
            ):
                self._raise_unverified_compiler_model(
                    "A Maven plugin enables an unmodeled build extension."
                )

        extension_property = project.find(
            "./{*}properties/{*}maven.ext.class.path"
        )
        if extension_property is not None and (
            extension_property.text or ""
        ).strip():
            self._raise_unverified_compiler_model(
                "The Maven core extension path is not modeled by fast update."
            )

        for path in maven_project_inputs:
            if path.name == "extensions.xml" and path.exists():
                self._raise_unverified_compiler_model(
                    "Project-level Maven core extensions are not supported."
                )
            if not path.exists():
                continue
            if path.is_symlink() or not path.is_file():
                self._raise_unverified_compiler_model(
                    "A Maven project configuration input is not a regular file."
                )
            try:
                metadata = path.stat()
                raw = path.read_bytes()
            except OSError as error:
                raise MavenResolutionError(
                    LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                    "A Maven project configuration input is unreadable.",
                    retryable=False,
                    suggested_next_step=(
                        "Use the formal Maven build and restart the application."
                    ),
                ) from error
            if (
                metadata.st_size > _MAX_MAVEN_PROJECT_CONFIG_BYTES
                or len(raw) > _MAX_MAVEN_PROJECT_CONFIG_BYTES
                or b"\x00" in raw
            ):
                self._raise_unverified_compiler_model(
                    "A Maven project configuration input exceeds safety limits."
                )
            text = raw.decode("utf-8", errors="surrogateescape")
            if _UNMODELED_MAVEN_ARGUMENT_PATTERN.search(text):
                self._raise_unverified_compiler_model(
                    "Maven project arguments override the compiler or extension model."
                )

        for name in _MAVEN_ENVIRONMENT_INPUTS:
            value = os.environ.get(name, "")
            if value and _UNMODELED_MAVEN_ARGUMENT_PATTERN.search(value):
                self._raise_unverified_compiler_model(
                    "Maven environment arguments override the compiler or "
                    "extension model."
                )

    def _ensure_no_unverified_build_transforms(
        self,
        project: ET.Element,
        compile_classpath: tuple[Path, ...],
        *,
        allow_lombok_processor: bool = False,
    ) -> None:
        """Validate build-time transformations for private javac execution.

        Production Fast Update keeps ``allow_lombok_processor`` disabled and
        therefore preserves its existing fail-closed behavior.  The internal
        Lombok experiment enables it only after a separate planner has proved
        that the effective processor model is Lombok-only; this method still
        rejects every unrelated compiler argument, lifecycle transform, or
        bytecode plugin.
        """
        compiler_matches = [
            plugin
            for plugin in project.findall(
                "./{*}build/{*}plugins/{*}plugin"
            )
            if self._child_text(plugin, "artifactId")
            == "maven-compiler-plugin"
        ]
        if len(compiler_matches) > 1:
            self._raise_unverified_transform()
        compiler = compiler_matches[0] if compiler_matches else None
        if compiler is not None:
            compiler_group = (
                self._child_text(compiler, "groupId")
                or "org.apache.maven.plugins"
            )
            compile_execution_count = sum(
                1
                for execution in compiler.findall(
                    "./{*}executions/{*}execution"
                )
                if "compile"
                in {
                    (goal.text or "").strip()
                    for goal in execution.findall("./{*}goals/{*}goal")
                }
            )
            if (
                compiler_group != "org.apache.maven.plugins"
                or compile_execution_count > 1
            ):
                self._raise_unverified_transform()
        configurations = self._compiler_configurations(compiler)
        proc_values = self._compiler_declared_values(
            project,
            configurations,
            config_name="proc",
            property_name="maven.compiler.proc",
        )
        if any(value.casefold() not in {"none", "full"} for value in proc_values):
            self._raise_unverified_transform()
        proc = (
            "none"
            if proc_values and all(
                value.casefold() == "none" for value in proc_values
            )
            else "full"
        )
        fail_on_warning_values = self._compiler_declared_values(
            project,
            configurations,
            config_name="failOnWarning",
            property_name="maven.compiler.failOnWarning",
        )
        if any(
            value.casefold() != "false"
            for value in fail_on_warning_values
        ):
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "The Maven fail-on-warning policy cannot be reproduced "
                "safely.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build and restart the application."
                ),
            )
        self._ensure_reproducible_compiler_identity(
            project,
            configurations,
        )
        for configuration in configurations:
            experiment_only_names = (
                {
                    "annotationProcessorPaths",
                    "annotationProcessors",
                    "compilerArgs",
                }
                if allow_lombok_processor
                else set()
            )
            for child in configuration:
                if (
                    self._local_name(child.tag)
                    not in _SAFE_COMPILER_CONFIGURATION
                    and self._local_name(child.tag)
                    not in experiment_only_names
                ):
                    self._raise_unverified_transform()
            if (
                (
                    not allow_lombok_processor
                    and (
                        configuration.find(
                            "./{*}annotationProcessorPaths"
                        )
                        is not None
                        or configuration.find(
                            "./{*}annotationProcessors"
                        )
                        is not None
                        or configuration.find("./{*}compilerArgs")
                        is not None
                    )
                )
                or configuration.find("./{*}compilerArguments") is not None
                or self._config_text(configuration, "compilerArgument")
                or self._config_text(configuration, "executable")
            ):
                self._raise_unverified_transform()
        for plugin in project.findall("./{*}build/{*}plugins/{*}plugin"):
            group_id = (
                self._child_text(plugin, "groupId")
                or "org.apache.maven.plugins"
            ).casefold()
            artifact_id = self._child_text(plugin, "artifactId").casefold()
            if artifact_id in _BYTECODE_TRANSFORM_PLUGINS:
                self._raise_unverified_transform()
            for execution in plugin.findall(
                "./{*}executions/{*}execution"
            ):
                phase = self._child_text(execution, "phase").casefold()
                goals = [
                    (goal.text or "").strip().casefold()
                    for goal in execution.findall("./{*}goals/{*}goal")
                ]
                if any(
                    token in goal_name
                    for goal_name in goals
                    for token in _BYTECODE_TRANSFORM_GOAL_TOKENS
                ):
                    self._raise_unverified_transform()
                if "${" in phase:
                    self._raise_unverified_transform()
                if goals and phase in _FAST_COMPILE_AFFECTING_PHASES:
                    safe_goals = _SAFE_AFFECTING_PHASE_PLUGIN_GOALS.get(
                        (group_id, artifact_id, phase),
                        frozenset(),
                    )
                    if not set(goals).issubset(safe_goals):
                        self._raise_unverified_transform()
                if (
                    goals
                    and not phase
                    and not set(goals).issubset(
                        _SAFE_IMPLICIT_PHASE_PLUGIN_GOALS.get(
                            (group_id, artifact_id),
                            frozenset(),
                        )
                    )
                ):
                    self._raise_unverified_transform()

        if proc != "none":
            try:
                processor_present = any(
                    self._contains_annotation_processor(path)
                    for path in compile_classpath
                )
            except (OSError, zipfile.BadZipFile) as error:
                raise MavenResolutionError(
                    LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                    "A compile dependency could not be inspected safely.",
                    retryable=False,
                    suggested_next_step=(
                        "Use the formal Maven build and restart the application."
                    ),
                ) from error
            if processor_present and not allow_lombok_processor:
                self._raise_unverified_transform()

    def _compiler_model(
        self,
        project: ET.Element,
        *,
        build_jdk: JavaToolchainCandidate,
        runtime_jdk: JavaToolchainCandidate,
    ) -> dict[str, Any]:
        compiler = self._find_build_plugin(
            project,
            "maven-compiler-plugin",
        )
        configurations = self._compiler_configurations(compiler)
        release_text = self._compiler_value(
            project,
            configurations,
            config_name="release",
            property_name="maven.compiler.release",
        )
        source_text = self._compiler_value(
            project,
            configurations,
            config_name="source",
            property_name="maven.compiler.source",
        )
        target_text = self._compiler_value(
            project,
            configurations,
            config_name="target",
            property_name="maven.compiler.target",
        )
        if release_text:
            release_level = self._java_level(release_text)
            source_level = release_level
            target_level = release_level
        else:
            release_level = None
            source_level = self._java_level(source_text)
            target_level = self._java_level(target_text)
        if source_level > target_level:
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "The effective Maven source level exceeds its target level.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build and restart the application."
                ),
            )

        build_major = build_jdk.compiler_major_version
        runtime_major = runtime_jdk.major_version
        if build_major is None or runtime_major is None:
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_JDK_INCOMPATIBLE,
                "The build and runtime JDK platform versions are unverified.",
                retryable=False,
                suggested_next_step=(
                    "Use JDK installations with readable JAVA_HOME/release "
                    "metadata, or use the formal Maven build and restart."
                ),
            )
        if target_level > build_major or target_level > runtime_major:
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_JDK_INCOMPATIBLE,
                "The compiler target is incompatible with the selected JDKs.",
                retryable=False,
                suggested_next_step=(
                    "Use compatible build/runtime JDKs or the formal Maven "
                    "build and restart."
                ),
            )
        if build_major >= 9:
            if release_level is None and source_level != target_level:
                raise MavenResolutionError(
                    LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                    "Different source and target levels cannot be reproduced "
                    "with a bounded --release compilation.",
                    retryable=False,
                    suggested_next_step=(
                        "Use the formal Maven build and restart the application."
                    ),
                )
            platform_args = ("--release", str(target_level))
        else:
            if not (
                build_major == runtime_major == target_level == 8
                and source_level <= 8
            ):
                raise MavenResolutionError(
                    LaunchErrorCode.FAST_COMPILE_JDK_INCOMPATIBLE,
                    "JDK 8 fast compilation requires build, runtime, and "
                    "target platform 8.",
                    retryable=False,
                    suggested_next_step=(
                        "Use the formal Maven build and restart the application."
                    ),
                )
            platform_args = (
                "-source",
                str(source_level),
                "-target",
                str(target_level),
            )
        return {
            "build_jdk_major": build_major,
            "runtime_jdk_major": runtime_major,
            "source_level": source_level,
            "target_level": target_level,
            "release_level": release_level,
            "javac_platform_args": platform_args,
        }

    @classmethod
    def _compiler_configurations(
        cls,
        compiler: ET.Element | None,
    ) -> tuple[ET.Element, ...]:
        if compiler is None:
            return ()
        configurations: list[ET.Element] = []
        for execution in compiler.findall(
            "./{*}executions/{*}execution"
        ):
            goals = {
                (goal.text or "").strip()
                for goal in execution.findall("./{*}goals/{*}goal")
            }
            if "compile" not in goals:
                continue
            configuration = execution.find("./{*}configuration")
            if configuration is not None:
                configurations.append(configuration)
        configuration = compiler.find("./{*}configuration")
        if configuration is not None:
            configurations.append(configuration)
        return tuple(configurations)

    @classmethod
    def _compiler_value(
        cls,
        project: ET.Element,
        configurations: tuple[ET.Element, ...],
        *,
        config_name: str,
        property_name: str,
    ) -> str:
        for configuration in configurations:
            value = cls._config_text(configuration, config_name)
            if value:
                return value
        property_element = project.find(
            f"./{{*}}properties/{{*}}{property_name}"
        )
        if property_element is not None:
            return (property_element.text or "").strip()
        return ""

    @classmethod
    def _compiler_declared_values(
        cls,
        project: ET.Element,
        configurations: tuple[ET.Element, ...],
        *,
        config_name: str,
        property_name: str,
    ) -> tuple[str, ...]:
        """Return every non-empty declaration, without assuming precedence."""

        values = [
            value
            for configuration in configurations
            if (value := cls._config_text(configuration, config_name))
        ]
        property_element = project.find(
            f"./{{*}}properties/{{*}}{property_name}"
        )
        if property_element is not None:
            property_value = (property_element.text or "").strip()
            if property_value:
                values.append(property_value)
        return tuple(values)

    def _ensure_reproducible_compiler_identity(
        self,
        project: ET.Element,
        configurations: tuple[ET.Element, ...],
    ) -> None:
        policies = (
            ("compilerId", "maven.compiler.compilerId", {"javac"}),
            ("fork", "maven.compiler.fork", {"false"}),
            ("debug", "maven.compiler.debug", {"true"}),
            (
                "enablePreview",
                "maven.compiler.enablePreview",
                {"false"},
            ),
            (
                "forceJavacCompilerUse",
                "maven.compiler.forceJavacCompilerUse",
                {"false"},
            ),
            (
                "forceLegacyJavacApi",
                "maven.compiler.forceLegacyJavacApi",
                {"false"},
            ),
        )
        for config_name, property_name, allowed in policies:
            values = self._compiler_declared_values(
                project,
                configurations,
                config_name=config_name,
                property_name=property_name,
            )
            if any(value.casefold() not in allowed for value in values):
                self._raise_unverified_compiler_model(
                    "The Maven compiler identity or mode cannot be reproduced."
                )

        executable_values = self._compiler_declared_values(
            project,
            configurations,
            config_name="executable",
            property_name="maven.compiler.executable",
        )
        if executable_values:
            self._raise_unverified_compiler_model(
                "A custom Maven compiler executable cannot be reproduced."
            )

        debug_levels = self._compiler_declared_values(
            project,
            configurations,
            config_name="debuglevel",
            property_name="maven.compiler.debuglevel",
        )
        for value in debug_levels:
            parts = tuple(
                item.strip().casefold()
                for item in value.split(",")
                if item.strip()
            )
            if len(parts) != 3 or set(parts) != {
                "lines",
                "source",
                "vars",
            }:
                self._raise_unverified_compiler_model(
                    "The Maven debug metadata policy cannot be reproduced."
                )

    @staticmethod
    def _config_text(configuration: ET.Element, name: str) -> str:
        element = configuration.find(f"./{{*}}{name}")
        return (element.text or "").strip() if element is not None else ""

    @classmethod
    def _find_build_plugin(
        cls,
        project: ET.Element,
        artifact_id: str,
    ) -> ET.Element | None:
        matches = [
            plugin
            for plugin in project.findall(
                "./{*}build/{*}plugins/{*}plugin"
            )
            if cls._child_text(plugin, "artifactId") == artifact_id
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _contains_annotation_processor(path: Path) -> bool:
        if path.is_dir():
            return (path / _PROCESSOR_SERVICE).is_file()
        if not path.is_file() or not zipfile.is_zipfile(path):
            return False
        with zipfile.ZipFile(path) as archive:
            try:
                service = archive.read(_PROCESSOR_SERVICE)
            except KeyError:
                return False
        return bool(
            any(
                line.strip() and not line.lstrip().startswith(b"#")
                for line in service.splitlines()
            )
        )

    @staticmethod
    def _java_level(raw: str) -> int:
        value = raw.strip()
        if not value or "${" in value:
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "The effective Maven Java level is unavailable.",
                retryable=False,
                suggested_next_step=(
                    "Configure a static maven.compiler.release or matching "
                    "maven.compiler.source/target, then restart the project."
                ),
            )
        match = re.fullmatch(r"(?:1\.)?(\d+)", value)
        if match is None:
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "The effective Maven Java level is unsupported.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build and restart the application."
                ),
            )
        level = int(match.group(1))
        if level < 8:
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "Fast source update supports Java 8 or newer targets.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build and restart the application."
                ),
            )
        return level

    @staticmethod
    def _class_file_java_release(class_bytes: bytes) -> int:
        if len(class_bytes) < 8 or class_bytes[:4] != b"\xca\xfe\xba\xbe":
            raise ValueError("invalid class file")
        major = int.from_bytes(class_bytes[6:8], byteorder="big")
        release = major - 44
        if release < 8:
            raise ValueError("unsupported class file target")
        return release

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    @staticmethod
    def _raise_unverified_transform() -> None:
        raise MavenResolutionError(
            LaunchErrorCode.ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED,
            "The Maven build uses unverified annotation processing or "
            "bytecode transformation.",
            retryable=False,
            suggested_next_step=(
                "Use the formal Maven build and restart the application."
            ),
        )

    @staticmethod
    def _raise_unverified_compiler_model(message: str) -> None:
        raise MavenResolutionError(
            LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
            message,
            retryable=False,
            suggested_next_step=(
                "Use the formal Maven build and restart the application."
            ),
        )

    def _base_maven_arguments(
        self,
        execution: MavenExecutionPlan,
    ) -> list[str]:
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
        return arguments

    def _source_encoding(self, project: ET.Element) -> str:
        """Return an explicit effective compiler encoding or fail closed."""
        compiler = self._find_build_plugin(
            project,
            "maven-compiler-plugin",
        )
        configurations = self._compiler_configurations(compiler)
        value = self._compiler_value(
            project,
            configurations,
            config_name="encoding",
            property_name="encoding",
        )
        if not value:
            element = project.find(
                "./{*}properties/{*}project.build.sourceEncoding"
            )
            if element is not None:
                value = (element.text or "").strip()
        if not value or "${" in value:
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "The effective Maven source encoding is not explicit.",
                retryable=False,
                suggested_next_step=(
                    "Configure project.build.sourceEncoding or the compiler "
                    "plugin encoding, then relaunch the project; otherwise "
                    "use the formal Maven build and restart."
                ),
            )
        try:
            "".encode(value)
        except LookupError as error:
            raise MavenResolutionError(
                LaunchErrorCode.UNSUPPORTED_BUILD_MODEL,
                "The Maven source encoding is not supported by this host.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build for this compiler setup."
                ),
            ) from error
        return value

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
