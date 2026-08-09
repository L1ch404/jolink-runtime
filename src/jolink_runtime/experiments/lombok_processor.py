"""Private Lombok compiler-model experiment.

The production ``update`` action intentionally remains fail-closed for every
annotation processor.  This module is an isolated evidence collector used to
answer a narrower question: can direct ``javac`` faithfully replay the
Lombok-only compiler model of a fresh Maven build?

Nothing in this module is registered as an MCP action.  It only writes to a
private experiment directory and never publishes classes to Maven output or a
running JVM.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Iterable, Mapping, Sequence

from jolink_runtime.launch.contracts import BuildOperationSpec
from jolink_runtime.launch.maven import (
    MavenBuildSystemAdapter,
    MavenExecutionPlan,
    MavenResolutionError,
)
from jolink_runtime.launch.process_supervisor import (
    AttemptToken,
    ProcessSupervisor,
)


_PROCESSOR_SERVICE = "META-INF/services/javax.annotation.processing.Processor"
_LOMBOK_PROCESSORS = frozenset(
    {
        "lombok.launch.AnnotationProcessorHider$AnnotationProcessor",
        "lombok.launch.AnnotationProcessorHider$ClaimingProcessor",
    }
)
_LOMBOK_MAIN_PROCESSOR = (
    "lombok.launch.AnnotationProcessorHider$AnnotationProcessor"
)
_LOMBOK_PROCESSOR_CLASS_ENTRIES = {
    provider: f"{provider.replace('.', '/')}.class"
    for provider in _LOMBOK_PROCESSORS
}
_JAVAC_ARGFILE_ENCODING = "utf-8"
_JAVAC_ARGFILE_JVM_ARGUMENT = "-J-Dfile.encoding=UTF-8"
_MAX_PROCESSOR_SERVICE_BYTES = 64 * 1024
_MAX_LOMBOK_CONFIG_BYTES = 256 * 1024
_MAX_LOMBOK_CONFIG_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_LOMBOK_CONFIG_FILES = 256
_MAX_LOMBOK_CONFIG_DEPTH = 32
_MAX_SOURCE_FILES = 25_000
_MAX_TOTAL_SOURCE_BYTES = 512 * 1024 * 1024
_MAX_CLASS_FILES = 100_000
_MAX_CLASS_BYTES = 2 * 1024 * 1024 * 1024
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_STOP_BUBBLING = re.compile(
    r"^\s*config\.stopBubbling\s*=\s*(true|false)\s*$",
    re.IGNORECASE,
)
_CLEAR_STOP_BUBBLING = re.compile(
    r"^\s*clear\s+config\.stopBubbling\s*$",
    re.IGNORECASE,
)
_COMPILER_ENVIRONMENT_INPUTS = (
    "JDK_JAVAC_OPTIONS",
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
    "MAVEN_ARGS",
    "MAVEN_OPTS",
)


class _StopBubblingState(Enum):
    """One ordered operation affecting Lombok's bubbling decision."""

    ABSENT = "absent"
    CLEAR = "clear"
    FALSE = "false"
    TRUE = "true"


class LombokExperimentError(RuntimeError):
    """Structured experiment rejection that leaves project state unchanged."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        suggested_next_step: str,
        retryable: bool = False,
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.suggested_next_step = suggested_next_step
        self.context = dict(context or {})

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": False,
            "error_code": self.error_code,
            "error": str(self),
            "retryable": self.retryable,
            "suggested_next_step": self.suggested_next_step,
            **self.context,
        }


@dataclass(frozen=True)
class ProcessorPathEntry:
    """One private Processor search-path artifact.

    Paths and option values are deliberately excluded from repr/results.
    """

    path: Path = field(repr=False)
    sha256: str = field(repr=False)
    providers: tuple[str, ...] = ()
    provider_implementations: tuple[str, ...] = ()
    contains_lombok_classes: bool = False
    lombok_version: str | None = None
    coordinate: str | None = None

    def redacted_summary(self) -> dict[str, object]:
        return {
            "coordinate": self.coordinate,
            "providers": list(self.providers),
            "provider_implementation_count": len(
                self.provider_implementations
            ),
            "lombok_version": self.lombok_version,
            "artifact_fingerprint": self.sha256,
        }


@dataclass(frozen=True)
class AnnotationProcessorModel:
    mode: str
    proc: str
    processor_path: tuple[Path, ...] = field(repr=False)
    path_entries: tuple[ProcessorPathEntry, ...] = field(repr=False)
    explicit_processor_names: tuple[str, ...] = ()
    processor_options: tuple[tuple[str, str | None], ...] = field(
        default_factory=tuple,
        repr=False,
    )

    @property
    def processor_option_names(self) -> tuple[str, ...]:
        return tuple(name for name, _value in self.processor_options)

    @property
    def lombok_versions(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                entry.lombok_version
                for entry in self.path_entries
                if entry.lombok_version
            )
        )

    @property
    def discovered_processors(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                provider
                for entry in self.path_entries
                for provider in entry.providers
            )
        )

    @property
    def activation_args(self) -> tuple[str, ...]:
        if self.proc == "full":
            return ("-proc:full",)
        return ()

    def redacted_summary(self) -> dict[str, object]:
        processor_artifacts = [
            entry.redacted_summary()
            for entry in self.path_entries
            if entry.providers
        ]
        return {
            "mode": self.mode,
            "proc": self.proc,
            "processor_path_entry_count": len(self.processor_path),
            "processor_artifacts": processor_artifacts,
            "explicit_processor_names": list(
                self.explicit_processor_names
            ),
            "processor_option_names": list(self.processor_option_names),
            "unknown_processors": [],
            "supported": True,
        }


@dataclass(frozen=True)
class ExperimentCompilerModel:
    """Compiler arguments faithful to Maven, not Fast Update hardening."""

    build_jdk_major: int
    source_level: int
    target_level: int
    release_level: int | None
    platform_args: tuple[str, ...] = field(repr=False)
    debug_args: tuple[str, ...] = field(repr=False)
    encoding: str
    parameters_enabled: bool
    compiler_plugin_version: str | None
    argfile_encoding: str = _JAVAC_ARGFILE_ENCODING
    javac_jvm_args: tuple[str, ...] = field(
        default=(_JAVAC_ARGFILE_JVM_ARGUMENT,),
        repr=False,
    )

    def redacted_summary(self) -> dict[str, object]:
        return {
            "build_jdk_major": self.build_jdk_major,
            "source_level": self.source_level,
            "target_level": self.target_level,
            "release_level": self.release_level,
            "platform_mode": (
                "release"
                if self.platform_args[:1] == ("--release",)
                else "source_target"
            ),
            "debug": list(self.debug_args),
            "encoding": self.encoding,
            "parameters_enabled": self.parameters_enabled,
            "compiler_plugin_version": self.compiler_plugin_version,
            "argfile_encoding": self.argfile_encoding,
        }


@dataclass(frozen=True)
class LombokConfigInput:
    path: Path = field(repr=False)
    relative_path: str
    sha256: str
    imported: bool = False

    def redacted_summary(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "imported": self.imported,
        }


@dataclass(frozen=True)
class LombokConfigSnapshot:
    project_root: Path = field(repr=False)
    inputs: tuple[LombokConfigInput, ...]
    absent_candidates: tuple[str, ...] = field(repr=False)
    fingerprint: str

    def redacted_summary(self) -> dict[str, object]:
        return {
            "files": [item.redacted_summary() for item in self.inputs],
            "absent_candidate_count": len(self.absent_candidates),
            "external_config_detected": False,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class LombokExperimentPlan:
    project_root: Path = field(repr=False)
    module_root: Path = field(repr=False)
    source_root: Path = field(repr=False)
    formal_output_root: Path = field(repr=False)
    javac_executable: Path = field(repr=False)
    dependency_classpath: tuple[Path, ...] = field(repr=False)
    dependency_input_paths: tuple[Path, ...] = field(repr=False)
    dependency_artifact_fingerprints: tuple[str, ...] = field(repr=False)
    compiler_model: ExperimentCompilerModel
    processor_model: AnnotationProcessorModel
    processor_input_paths: tuple[Path, ...] = field(repr=False)
    processor_artifact_fingerprints: tuple[str, ...] = field(repr=False)
    config_snapshot: LombokConfigSnapshot
    source_files: tuple[Path, ...] = field(repr=False)
    source_hashes: Mapping[Path, str] = field(repr=False)
    configuration_inputs: tuple[Path, ...] = field(repr=False)
    configuration_input_fingerprints: tuple[str, ...] = field(repr=False)
    environment_fingerprint: str = field(repr=False)
    module: str
    plan_fingerprint: str = field(repr=False)

    def redacted_summary(self) -> dict[str, object]:
        return {
            "strategy": "module_full_javac",
            "module": self.module,
            "source_count": len(self.source_files),
            "compile_classpath_entry_count": len(
                self.dependency_classpath
            ),
            "self_output_on_compile_classpath": False,
            "compiler": self.compiler_model.redacted_summary(),
            "annotation_processing": (
                self.processor_model.redacted_summary()
            ),
            "lombok_config": self.config_snapshot.redacted_summary(),
            "plan_fingerprint": self.plan_fingerprint,
        }


@dataclass(frozen=True)
class LombokCompileAttempt:
    attempt_id: str
    attempt_directory: Path
    classes_directory: Path
    generated_sources_directory: Path
    log_file: Path
    elapsed_seconds: float
    class_hashes: Mapping[str, str] = field(repr=False)

    def redacted_summary(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "status": "success",
            "generated_class_count": len(self.class_hashes),
            "javac_duration_ms": round(self.elapsed_seconds * 1000, 3),
        }


class LombokExperimentPlanner:
    """Resolve a Lombok-only direct-javac plan from effective Maven data."""

    def __init__(
        self,
        maven: MavenBuildSystemAdapter | None = None,
    ) -> None:
        self._maven = maven or MavenBuildSystemAdapter()

    def explicit_processor_coordinate(
        self,
        execution: MavenExecutionPlan,
    ) -> tuple[str, str, str] | None:
        project = self._effective_project(execution)
        configurations = self._compiler_configurations(project)
        declarations = _processor_path_declarations(configurations)
        if not declarations:
            return None
        if len(declarations) != 1:
            raise LombokExperimentError(
                "PROCESSOR_PATH_UNRESOLVED",
                "The Lombok P0 experiment supports one Processor artifact.",
                suggested_next_step=(
                    "Use Maven until a general transitive Processor resolver "
                    "is implemented."
                ),
            )
        group, artifact, _version = declarations[0]
        if group != "org.projectlombok" or artifact != "lombok":
            raise LombokExperimentError(
                "MULTIPLE_ANNOTATION_PROCESSORS_UNVERIFIED",
                "annotationProcessorPaths is not Lombok-only.",
                suggested_next_step=(
                    "Keep this P0 experiment Lombok-only or use Maven."
                ),
            )
        return declarations[0]

    def create_processor_copy_operation(
        self,
        execution: MavenExecutionPlan,
        *,
        coordinate: tuple[str, str, str],
        destination: Path,
        timeout_seconds: float,
    ) -> BuildOperationSpec:
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
        _chmod_private(destination)
        group, artifact, version = coordinate
        arguments = self._maven._base_maven_arguments(  # noqa: SLF001
            execution
        )
        if execution.module.relative_path != ".":
            arguments.extend(["-pl", execution.module.relative_path])
        arguments.extend(
            [
                "org.apache.maven.plugins:"
                "maven-dependency-plugin:3.6.1:copy",
                f"-Dartifact={group}:{artifact}:{version}:jar",
                f"-DoutputDirectory={destination}",
                "-DstripVersion=false",
                "-DoverWriteReleases=true",
                "-DoverWriteSnapshots=true",
                "-DoverWriteIfNewer=true",
            ]
        )
        return BuildOperationSpec(
            argv=tuple(arguments),
            cwd=execution.workspace.build_root,
            environment=dict(
                self._maven.create_compile_classpath_operation(
                    execution
                ).environment
            ),
            timeout_seconds=timeout_seconds,
            output_capture=execution.build_log,
            operation_name="resolve_lombok_processor_path",
        )

    def create_plan(
        self,
        execution: MavenExecutionPlan,
        *,
        explicit_processor_directory: Path | None = None,
    ) -> LombokExperimentPlan:
        source_root = execution.module.directory / "src" / "main" / "java"
        if not source_root.is_dir():
            raise LombokExperimentError(
                "COMPILE_EXPERIMENT_UNSUPPORTED",
                "The selected module has no standard src/main/java root.",
                suggested_next_step=(
                    "Use a standard Maven Java module for this experiment."
                ),
            )
        if execution.module.packaging != "jar":
            raise LombokExperimentError(
                "COMPILE_EXPERIMENT_UNSUPPORTED",
                "The selected Maven module is not jar packaging.",
                suggested_next_step=(
                    "Select a standard Java jar module for this experiment."
                ),
            )
        if (source_root / "module-info.java").is_file():
            raise LombokExperimentError(
                "COMPILE_EXPERIMENT_UNSUPPORTED",
                "The Lombok P0 experiment does not model the module path.",
                suggested_next_step="Use the formal Maven build.",
            )
        for required in (
            execution.compile_classpath_file,
            execution.effective_pom_file,
        ):
            if not required.is_file():
                raise LombokExperimentError(
                    "COMPILE_MODEL_UNAVAILABLE",
                    "Maven compiler metadata is unavailable.",
                    suggested_next_step=(
                        "Run the private Maven metadata step again."
                    ),
                    retryable=True,
                )

        effective_root = self._read_effective_root(execution)
        project = self._maven._select_effective_project(  # noqa: SLF001
            effective_root,
            execution.module,
        )
        self._ensure_effective_standard_layout(project, execution)
        dependency_classpath = self._resolve_dependency_classpath(
            execution,
            effective_root,
        )
        maven_project_inputs = tuple(
            execution.workspace.build_root / ".mvn" / name
            for name in ("maven.config", "jvm.config", "extensions.xml")
        )
        try:
            self._maven._ensure_no_unverified_maven_extensions(  # noqa: SLF001
                project,
                maven_project_inputs=maven_project_inputs,
            )
            self._maven._ensure_no_unverified_build_transforms(  # noqa: SLF001
                project,
                dependency_classpath,
                allow_lombok_processor=True,
            )
        except MavenResolutionError as error:
            raise _from_maven_error(error) from error

        compiler_model = self._compiler_model(project, execution)
        processor_model = self._processor_model(
            project,
            dependency_classpath,
            build_jdk_major=compiler_model.build_jdk_major,
            explicit_processor_directory=explicit_processor_directory,
        )
        source_files, source_hashes = _snapshot_source_inputs(source_root)
        config_snapshot = discover_lombok_configuration(
            execution.workspace.project_root,
            source_files,
        )
        # Track authoritative configuration, not generated metadata files.
        # Maven Help embeds a generation timestamp in effective-pom output;
        # the parsed compiler/dependency/Processor models below are the stable
        # semantic representation of those generated files.
        configuration_inputs = tuple(
            [
                *(module.pom_file for module in execution.workspace.modules),
                *maven_project_inputs,
            ]
            + (
                [execution.preferences.user_settings_file]
                if execution.preferences.user_settings_file is not None
                else []
            )
        )
        environment_fingerprint = _environment_fingerprint()
        plan = LombokExperimentPlan(
            project_root=execution.workspace.project_root.resolve(strict=True),
            module_root=execution.module.directory.resolve(strict=True),
            source_root=source_root.resolve(strict=True),
            formal_output_root=(
                execution.module.output_directory.resolve(strict=False)
            ),
            javac_executable=execution.build_jdk.javac_executable,
            dependency_classpath=dependency_classpath,
            dependency_input_paths=dependency_classpath,
            dependency_artifact_fingerprints=tuple(
                _path_fingerprint(path) for path in dependency_classpath
            ),
            compiler_model=compiler_model,
            processor_model=processor_model,
            processor_input_paths=processor_model.processor_path,
            processor_artifact_fingerprints=tuple(
                _path_fingerprint(path)
                for path in processor_model.processor_path
            ),
            config_snapshot=config_snapshot,
            source_files=source_files,
            source_hashes=source_hashes,
            configuration_inputs=configuration_inputs,
            configuration_input_fingerprints=tuple(
                _path_fingerprint(path) for path in configuration_inputs
            ),
            environment_fingerprint=environment_fingerprint,
            module=execution.module.relative_path,
            plan_fingerprint="",
        )
        completed = replace(
            plan,
            plan_fingerprint=_current_plan_fingerprint(plan),
        )
        _assert_artifact_snapshots(completed)
        return completed

    def validate_before_baseline(
        self,
        execution: MavenExecutionPlan,
        *,
        explicit_processor_directory: Path | None = None,
        validate_processor: bool = True,
    ) -> None:
        """Fail closed before the private Maven ``compile`` lifecycle runs.

        Metadata goals have already produced the effective POM and compile
        classpath, but no compile-phase project plugin or Processor has run.
        This pass deliberately uses repository artifacts instead of Reactor
        output; the P0 command accepts only a single-module workspace.
        """

        effective_root = self._read_effective_root(execution)
        project = self._maven._select_effective_project(  # noqa: SLF001
            effective_root,
            execution.module,
        )
        self._ensure_effective_standard_layout(project, execution)
        raw_classpath = self._raw_dependency_classpath(execution)
        maven_project_inputs = tuple(
            execution.workspace.build_root / ".mvn" / name
            for name in ("maven.config", "jvm.config", "extensions.xml")
        )
        try:
            self._maven._ensure_no_unverified_maven_extensions(  # noqa: SLF001
                project,
                maven_project_inputs=maven_project_inputs,
            )
            self._maven._ensure_no_unverified_build_transforms(  # noqa: SLF001
                project,
                raw_classpath,
                allow_lombok_processor=True,
            )
        except MavenResolutionError as error:
            raise _from_maven_error(error) from error
        compiler_model = self._compiler_model(project, execution)
        if validate_processor:
            self._processor_model(
                project,
                raw_classpath,
                build_jdk_major=compiler_model.build_jdk_major,
                explicit_processor_directory=explicit_processor_directory,
            )
        source_root = execution.module.directory / "src" / "main" / "java"
        source_files, _source_hashes = _snapshot_source_inputs(source_root)
        discover_lombok_configuration(
            execution.workspace.project_root,
            source_files,
        )

    def _raw_dependency_classpath(
        self,
        execution: MavenExecutionPlan,
    ) -> tuple[Path, ...]:
        text = self._maven._read_classpath_file(  # noqa: SLF001
            execution.compile_classpath_file
        )
        formal_output = execution.module.output_directory.resolve(
            strict=False
        )
        result: list[Path] = []
        seen: set[str] = set()
        for value in text.split(os.pathsep):
            if not value.strip():
                continue
            raw = Path(value)
            if not raw.is_absolute():
                raw = execution.module.directory / raw
            candidate = raw.resolve(strict=False)
            if candidate == formal_output:
                continue
            key = os.path.normcase(str(candidate))
            if key in seen:
                continue
            _require_regular_compiler_input(candidate)
            seen.add(key)
            result.append(candidate)
        return tuple(result)

    def _ensure_effective_standard_layout(
        self,
        project: ET.Element,
        execution: MavenExecutionPlan,
    ) -> None:
        module = execution.module.directory.resolve(strict=True)
        expected = {
            "directory": (module / "target").resolve(strict=False),
            "outputDirectory": (
                module / "target" / "classes"
            ).resolve(strict=False),
            "sourceDirectory": (
                module / "src" / "main" / "java"
            ).resolve(strict=False),
        }
        build = project.find("./{*}build")
        for name, expected_path in expected.items():
            element = (
                build.find(f"./{{*}}{name}")
                if build is not None
                else None
            )
            raw = (element.text or "").strip() if element is not None else ""
            if not raw:
                actual = expected_path
            else:
                replacements = {
                    "${project.basedir}": str(module),
                    "${basedir}": str(module),
                    "${project.build.directory}": str(module / "target"),
                }
                for macro, value in replacements.items():
                    raw = raw.replace(macro, value)
                if "${" in raw:
                    raise LombokExperimentError(
                        "COMPILE_EXPERIMENT_UNSUPPORTED",
                        "The effective Maven build layout is dynamic.",
                        suggested_next_step="Use the formal Maven build.",
                    )
                path = Path(raw)
                if not path.is_absolute():
                    path = module / path
                actual = path.resolve(strict=False)
            if actual != expected_path:
                raise LombokExperimentError(
                    "COMPILE_EXPERIMENT_UNSUPPORTED",
                    "The experiment requires conventional Maven build paths.",
                    suggested_next_step=(
                        "Use Maven until custom source/build/output paths are "
                        "modeled as a complete private generation."
                    ),
                    context={"nonstandard_build_path": name},
                )

    def _read_effective_root(
        self,
        execution: MavenExecutionPlan,
    ) -> ET.Element:
        try:
            return self._maven._read_effective_pom(  # noqa: SLF001
                execution.effective_pom_file
            )
        except MavenResolutionError as error:
            raise _from_maven_error(error) from error

    def _effective_project(
        self,
        execution: MavenExecutionPlan,
    ) -> ET.Element:
        root = self._read_effective_root(execution)
        try:
            return self._maven._select_effective_project(  # noqa: SLF001
                root,
                execution.module,
            )
        except MavenResolutionError as error:
            raise _from_maven_error(error) from error

    def _compiler_configurations(
        self,
        project: ET.Element,
    ) -> tuple[ET.Element, ...]:
        compiler = self._maven._find_build_plugin(  # noqa: SLF001
            project,
            "maven-compiler-plugin",
        )
        return self._maven._compiler_configurations(compiler)  # noqa: SLF001

    def _resolve_dependency_classpath(
        self,
        execution: MavenExecutionPlan,
        effective_root: ET.Element,
    ) -> tuple[Path, ...]:
        reactor_artifacts = self._maven._reactor_artifacts(  # noqa: SLF001
            effective_root,
            execution.workspace,
        )
        text = self._maven._read_classpath_file(  # noqa: SLF001
            execution.compile_classpath_file
        )
        formal_output = execution.module.output_directory.resolve(
            strict=False
        )
        normalized: list[Path] = []
        seen: set[str] = set()
        for value in text.split(os.pathsep):
            if not value.strip():
                continue
            raw = Path(value)
            if not raw.is_absolute():
                raw = execution.module.directory / raw
            resolved = raw.resolve(strict=False)
            replacement = self._maven._reactor_output_for_path(  # noqa: SLF001
                resolved,
                reactor_artifacts,
            )
            looks_like_workspace = (
                replacement is None
                and self._maven._looks_like_workspace_artifact(  # noqa: SLF001
                    resolved,
                    execution.workspace,
                )
            )
            if looks_like_workspace:
                raise LombokExperimentError(
                    "COMPILE_MODEL_UNAVAILABLE",
                    "A Reactor dependency has no workspace output mapping.",
                    suggested_next_step=(
                        "Run the private Reactor Maven baseline and retry."
                    ),
                )
            selected = replacement or raw
            candidate = selected.resolve(strict=False)
            if candidate == formal_output:
                continue
            key = os.path.normcase(str(candidate))
            if key in seen:
                continue
            _require_regular_compiler_input(candidate)
            _reject_dependency_manifest_class_path(candidate)
            seen.add(key)
            normalized.append(candidate)
        return tuple(normalized)

    def _compiler_model(
        self,
        project: ET.Element,
        execution: MavenExecutionPlan,
    ) -> ExperimentCompilerModel:
        compiler = self._maven._find_build_plugin(  # noqa: SLF001
            project,
            "maven-compiler-plugin",
        )
        configurations = self._maven._compiler_configurations(  # noqa: SLF001
            compiler
        )
        release_text = self._maven._compiler_value(  # noqa: SLF001
            project,
            configurations,
            config_name="release",
            property_name="maven.compiler.release",
        )
        source_text = self._maven._compiler_value(  # noqa: SLF001
            project,
            configurations,
            config_name="source",
            property_name="maven.compiler.source",
        )
        target_text = self._maven._compiler_value(  # noqa: SLF001
            project,
            configurations,
            config_name="target",
            property_name="maven.compiler.target",
        )
        plugin_version = (
            self._maven._child_text(compiler, "version")  # noqa: SLF001
            if compiler is not None
            else None
        ) or None
        build_major = execution.build_jdk.compiler_major_version
        if build_major is None:
            raise LombokExperimentError(
                "COMPILE_JDK_INCOMPATIBLE",
                "The Build JDK compiler version is unavailable.",
                suggested_next_step=(
                    "Use the same supported Build JDK as the formal Maven "
                    "compile."
                ),
            )
        try:
            if release_text:
                release = self._maven._java_level(release_text)  # noqa: SLF001
                source = target = release
                if build_major >= 9:
                    platform_args = ("--release", str(release))
                elif build_major == 8 and _maven_version_at_least(
                    plugin_version,
                    (3, 13, 0),
                ):
                    # Maven Compiler Plugin 3.13+ translates <release> to
                    # source/target when running on JDK 8, whose javac has no
                    # --release option.
                    platform_args = (
                        "-source",
                        str(release),
                        "-target",
                        str(release),
                    )
                else:
                    raise LombokExperimentError(
                        "COMPILE_MODEL_UNAVAILABLE",
                        "The Maven release policy cannot be reproduced on JDK 8.",
                        suggested_next_step=(
                            "Use Maven or Maven Compiler Plugin 3.13+ for "
                            "JDK 8 release compatibility."
                        ),
                    )
            else:
                release = None
                source = self._maven._java_level(source_text)  # noqa: SLF001
                target = self._maven._java_level(target_text)  # noqa: SLF001
                platform_args = (
                    "-source",
                    str(source),
                    "-target",
                    str(target),
                )
        except MavenResolutionError as error:
            raise _from_maven_error(error) from error
        if target > build_major:
            raise LombokExperimentError(
                "COMPILE_JDK_INCOMPATIBLE",
                "The Maven target is incompatible with the Build JDK.",
                suggested_next_step=(
                    "Use the same supported Build JDK as the formal Maven "
                    "compile."
                ),
            )
        encoding = self._source_encoding(project)
        parameters = _boolean_compiler_value(
            self._maven,
            project,
            configurations,
            config_name="parameters",
            property_name="maven.compiler.parameters",
            default=False,
        )
        debug = _boolean_compiler_value(
            self._maven,
            project,
            configurations,
            config_name="debug",
            property_name="maven.compiler.debug",
            default=True,
        )
        debug_level = self._maven._compiler_value(  # noqa: SLF001
            project,
            configurations,
            config_name="debuglevel",
            property_name="maven.compiler.debuglevel",
        )
        if debug:
            debug_args = (
                (f"-g:{debug_level}",) if debug_level else ("-g",)
            )
        else:
            debug_args = ("-g:none",)
        return ExperimentCompilerModel(
            build_jdk_major=build_major,
            source_level=source,
            target_level=target,
            release_level=release,
            platform_args=platform_args,
            debug_args=debug_args,
            encoding=encoding,
            parameters_enabled=parameters,
            compiler_plugin_version=plugin_version,
        )

    def _source_encoding(self, project: ET.Element) -> str:
        try:
            return self._maven._source_encoding(project)  # noqa: SLF001
        except MavenResolutionError as error:
            raise _from_maven_error(error) from error

    def _processor_model(
        self,
        project: ET.Element,
        dependency_classpath: tuple[Path, ...],
        *,
        build_jdk_major: int,
        explicit_processor_directory: Path | None,
    ) -> AnnotationProcessorModel:
        configurations = self._compiler_configurations(project)
        proc = self._maven._compiler_value(  # noqa: SLF001
            project,
            configurations,
            config_name="proc",
            property_name="maven.compiler.proc",
        ).strip().casefold() or "default"
        if proc == "none":
            raise LombokExperimentError(
                "ANNOTATION_PROCESSING_DISABLED",
                "The effective Maven compiler disables annotation processing.",
                suggested_next_step=(
                    "Do not enable Lombok behind an explicit proc=none policy."
                ),
            )
        if proc == "only":
            raise LombokExperimentError(
                "ANNOTATION_PROCESSING_UNVERIFIED",
                "proc=only does not produce the complete module class output.",
                suggested_next_step="Use the formal Maven build.",
            )
        if proc not in {"default", "full"}:
            raise LombokExperimentError(
                "ANNOTATION_PROCESSING_UNVERIFIED",
                "The Maven annotation-processing mode is unsupported.",
                suggested_next_step="Use the formal Maven build.",
            )

        path_declarations = _processor_path_declarations(configurations)
        explicit_names = _explicit_processor_names(configurations)
        processor_options = _processor_options(configurations)
        if path_declarations:
            if explicit_processor_directory is None:
                raise LombokExperimentError(
                    "PROCESSOR_PATH_RESOLUTION_REQUIRED",
                    "The explicit Lombok Processor artifact is unresolved.",
                    suggested_next_step=(
                        "Run the private supervised Maven artifact copy step."
                    ),
                    retryable=True,
                )
            jars = tuple(sorted(explicit_processor_directory.glob("*.jar")))
            if len(jars) != 1:
                raise LombokExperimentError(
                    "PROCESSOR_PATH_UNRESOLVED",
                    "Maven did not resolve exactly one Lombok Processor JAR.",
                    suggested_next_step=(
                        "Inspect the private Maven log and keep using Maven."
                    ),
                    context={"resolved_jar_count": len(jars)},
                )
            group, artifact, version = path_declarations[0]
            processor_path = jars
            path_entries = tuple(
                [_inspect_processor_path_entry(jars[0], required=True)]
            )
            entry = path_entries[0]
            if entry.lombok_version != version:
                raise LombokExperimentError(
                    "PROCESSOR_PATH_UNRESOLVED",
                    "The resolved Lombok artifact version is not the declared version.",
                    suggested_next_step=(
                        "Refresh Maven dependency resolution and retry."
                    ),
                )
            path_entries = (
                ProcessorPathEntry(
                    path=entry.path,
                    sha256=entry.sha256,
                    providers=entry.providers,
                    provider_implementations=entry.provider_implementations,
                    contains_lombok_classes=entry.contains_lombok_classes,
                    lombok_version=entry.lombok_version,
                    coordinate=f"{group}:{artifact}:{version}",
                ),
            )
            mode = "explicit_processor_path"
        else:
            if (
                build_jdk_major >= 23
                and not explicit_names
                and proc != "full"
            ):
                raise LombokExperimentError(
                    "ANNOTATION_PROCESSING_UNVERIFIED",
                    "JDK 23+ disables unrequested classpath discovery.",
                    suggested_next_step=(
                        "Declare annotationProcessorPaths, annotationProcessors, "
                        "or proc=full, then retry."
                    ),
                )
            processor_path = dependency_classpath
            path_entries = tuple(
                _inspect_processor_path_entry(path, required=False)
                for path in processor_path
            )
            mode = "compile_classpath_discovery"

        providers = tuple(
            dict.fromkeys(
                provider
                for entry in path_entries
                for provider in entry.providers
            )
        )
        unknown = sorted(set(providers) - _LOMBOK_PROCESSORS)
        if unknown:
            raise LombokExperimentError(
                "MULTIPLE_ANNOTATION_PROCESSORS_UNVERIFIED",
                "The effective Processor path contains non-Lombok processors.",
                suggested_next_step=(
                    "Keep this P0 experiment Lombok-only or use Maven."
                ),
                context={"unknown_processor_count": len(unknown)},
            )
        main_entries = [
            entry
            for entry in path_entries
            if _LOMBOK_MAIN_PROCESSOR in entry.providers
        ]
        if len(main_entries) != 1:
            raise LombokExperimentError(
                "LOMBOK_PROCESSOR_NOT_FOUND"
                if not main_entries
                else "PROCESSOR_PATH_UNRESOLVED",
                "A unique Lombok transformation Processor is unavailable.",
                suggested_next_step=(
                    "Use one complete Lombok artifact or the formal Maven build."
                ),
                context={"matching_lombok_artifact_count": len(main_entries)},
            )
        distribution = main_entries[0]
        shadowing_entries = [
            entry
            for entry in path_entries
            if entry is not distribution and entry.contains_lombok_classes
        ]
        if shadowing_entries:
            raise LombokExperimentError(
                "PROCESSOR_PATH_UNRESOLVED",
                "Another Processor-path entry can shadow Lombok classes.",
                suggested_next_step=(
                    "Remove duplicate/shaded Lombok classes or use Maven."
                ),
                context={"shadowing_entry_count": len(shadowing_entries)},
            )
        missing_implementations = sorted(
            provider
            for provider in providers
            if provider in _LOMBOK_PROCESSORS
            and provider not in distribution.provider_implementations
        )
        if missing_implementations:
            raise LombokExperimentError(
                "PROCESSOR_PATH_UNRESOLVED",
                "The Lombok service and implementation classes differ.",
                suggested_next_step="Use one complete Lombok distribution.",
                context={
                    "missing_processor_implementation_count": len(
                        missing_implementations
                    )
                },
            )
        if not distribution.lombok_version:
            raise LombokExperimentError(
                "PROCESSOR_PATH_UNRESOLVED",
                "The Lombok Processor artifact has no verifiable version.",
                suggested_next_step="Use an official Lombok distribution.",
            )
        if explicit_names:
            unknown_names = sorted(set(explicit_names) - _LOMBOK_PROCESSORS)
            if unknown_names or _LOMBOK_MAIN_PROCESSOR not in explicit_names:
                raise LombokExperimentError(
                    "ANNOTATION_PROCESSING_UNVERIFIED",
                    "The explicitly selected Processor set is not Lombok-only.",
                    suggested_next_step="Use the formal Maven build.",
                )
        return AnnotationProcessorModel(
            mode=mode,
            proc=proc,
            processor_path=processor_path,
            path_entries=path_entries,
            explicit_processor_names=explicit_names,
            processor_options=processor_options,
        )


class LombokExperimentRunner:
    """Compile the frozen module into an isolated private generation."""

    def __init__(
        self,
        supervisor: ProcessSupervisor | None = None,
    ) -> None:
        self._supervisor = supervisor or ProcessSupervisor()

    def compile(
        self,
        plan: LombokExperimentPlan,
        *,
        root_directory: Path,
        timeout_seconds: float,
    ) -> LombokCompileAttempt:
        _assert_plan_fresh(plan)
        attempt_id = f"lombok_{uuid.uuid4().hex[:12]}"
        attempt = root_directory / attempt_id
        mirror_project = attempt / "workspace"
        mirror_module = mirror_project / plan.module_root.relative_to(
            plan.project_root
        )
        classes = attempt / "classes"
        generated = attempt / "generated-sources"
        headers = attempt / "native-headers"
        empty_sourcepath = attempt / "empty-sourcepath"
        log_file = attempt / "javac.log"
        arg_file = attempt / "javac.args"
        try:
            attempt.mkdir(parents=True, exist_ok=False, mode=0o700)
            _chmod_private(attempt)
            for directory in (
                mirror_project,
                classes,
                generated,
                headers,
                empty_sourcepath,
            ):
                directory.mkdir(parents=True, exist_ok=False)
            (attempt / "lombok.config").write_text(
                "config.stopBubbling = true\n",
                encoding="utf-8",
            )
            mirrored_sources = _materialize_snapshot(
                plan,
                mirror_project=mirror_project,
            )
            compile_classpath = (classes, *plan.dependency_classpath)
            arguments = [
                "-encoding",
                plan.compiler_model.encoding,
                *plan.compiler_model.debug_args,
                "-implicit:none",
                "-sourcepath",
                str(empty_sourcepath),
                "-classpath",
                os.pathsep.join(str(path) for path in compile_classpath),
                "-processorpath",
                os.pathsep.join(
                    str(path) for path in plan.processor_model.processor_path
                ),
                "-d",
                str(classes),
                "-s",
                str(generated),
                "-h",
                str(headers),
                *plan.compiler_model.platform_args,
                *plan.processor_model.activation_args,
            ]
            if plan.compiler_model.parameters_enabled:
                arguments.append("-parameters")
            if plan.processor_model.explicit_processor_names:
                arguments.extend(
                    (
                        "-processor",
                        ",".join(
                            plan.processor_model.explicit_processor_names
                        ),
                    )
                )
            arguments.extend(
                f"-A{name}={value}" if value is not None else f"-A{name}"
                for name, value in plan.processor_model.processor_options
            )
            arguments.extend(str(source) for source in mirrored_sources)
            _write_javac_argfile(
                arg_file,
                arguments,
                encoding=plan.compiler_model.argfile_encoding,
            )
        except Exception:
            shutil.rmtree(attempt, ignore_errors=True)
            raise

        token = AttemptToken(attempt_id=attempt_id, generation=1)
        result = None
        try:
            result = self._supervisor.run(
                BuildOperationSpec(
                    argv=(
                        str(plan.javac_executable),
                        *plan.compiler_model.javac_jvm_args,
                        f"@{arg_file}",
                    ),
                    cwd=mirror_module,
                    timeout_seconds=timeout_seconds,
                    output_capture=log_file,
                    operation_name="lombok_module_full_javac",
                ),
                owner=token,
            )
        finally:
            owner_settled = self._supervisor.release_owner(token)
        if not owner_settled:
            raise LombokExperimentError(
                "PROCESS_CLEANUP_UNSETTLED",
                "The supervised javac process tree did not settle.",
                suggested_next_step=(
                    "Discard this attempt and stop remaining compiler processes."
                ),
                retryable=True,
            )
        assert result is not None
        if result.cancelled:
            raise LombokExperimentError(
                "JAVAC_CANCELLED",
                "The Lombok compile experiment was cancelled.",
                suggested_next_step="Retry after other build activity settles.",
                retryable=True,
            )
        if result.timed_out:
            raise LombokExperimentError(
                "JAVAC_TIMEOUT",
                "The Lombok compile experiment exceeded its timeout.",
                suggested_next_step=(
                    "Increase the experiment timeout or keep using Maven."
                ),
                retryable=True,
                context={"private_compile_log_available": True},
            )
        if not result.succeeded:
            raise LombokExperimentError(
                "JAVAC_EXECUTION_FAILED",
                "Direct javac could not reproduce the Lombok module build.",
                suggested_next_step=(
                    "Inspect the retained private javac log and keep Maven as "
                    "the source of truth."
                ),
                context={
                    "return_code": result.return_code,
                    "private_compile_log_available": True,
                },
            )
        _assert_plan_fresh(plan)
        class_hashes = scan_class_hashes(classes)
        return LombokCompileAttempt(
            attempt_id=attempt_id,
            attempt_directory=attempt,
            classes_directory=classes,
            generated_sources_directory=generated,
            log_file=log_file,
            elapsed_seconds=result.finished_at - result.started_at,
            class_hashes=class_hashes,
        )


def discover_lombok_configuration(
    project_root: Path,
    source_files: Sequence[Path],
) -> LombokConfigSnapshot:
    project = project_root.resolve(strict=True)
    found: dict[Path, LombokConfigInput] = {}
    absent: set[str] = set()
    imported: set[Path] = set()
    budget = _ConfigBudget()
    source_directories = sorted(
        {source.parent.resolve(strict=True) for source in source_files},
        key=lambda item: os.path.normcase(str(item)),
    )
    for source_directory in source_directories:
        current = source_directory
        stop_bubbling_visited: set[Path] = set()
        while True:
            config = current / "lombok.config"
            if config.exists() or _path_is_link_or_reparse(config):
                resolved = _safe_workspace_config(config, project)
                _collect_config_imports(
                    resolved,
                    project=project,
                    found=found,
                    imported=imported,
                    active=[],
                    budget=budget,
                )
                if _config_stops_bubbling(
                    resolved,
                    project=project,
                    active=[],
                    depth=0,
                    visited=stop_bubbling_visited,
                ):
                    break
            else:
                try:
                    relative = config.relative_to(project).as_posix()
                except ValueError:
                    relative = "<outside-workspace>"
                absent.add(relative)
            if current == project:
                _reject_external_lombok_config(project)
                break
            try:
                current.relative_to(project)
            except ValueError as error:
                raise LombokExperimentError(
                    "LOMBOK_CONFIG_OUTSIDE_WORKSPACE_UNVERIFIED",
                    "Lombok configuration search leaves the workspace.",
                    suggested_next_step=(
                        "Move effective configuration into the workspace or "
                        "keep using Maven."
                    ),
                ) from error
            current = current.parent

    ordered = tuple(
        sorted(found.values(), key=lambda item: item.relative_path)
    )
    absent_ordered = tuple(sorted(absent))
    digest = hashlib.sha256()
    for item in ordered:
        digest.update(b"present\0")
        digest.update(item.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.sha256.encode("ascii"))
        digest.update(b"\0imported\0" if item.imported else b"\0direct\0")
    for relative in absent_ordered:
        digest.update(b"absent\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
    return LombokConfigSnapshot(
        project_root=project,
        inputs=ordered,
        absent_candidates=absent_ordered,
        fingerprint=digest.hexdigest(),
    )


@dataclass
class _ConfigBudget:
    paths: set[Path] = field(default_factory=set)
    total_bytes: int = 0

    def observe(self, path: Path, size: int, depth: int) -> None:
        if depth > _MAX_LOMBOK_CONFIG_DEPTH:
            raise _config_limit_error()
        if path not in self.paths:
            self.paths.add(path)
            self.total_bytes += size
        if (
            len(self.paths) > _MAX_LOMBOK_CONFIG_FILES
            or self.total_bytes > _MAX_LOMBOK_CONFIG_TOTAL_BYTES
        ):
            raise _config_limit_error()


def compare_class_outputs(
    expected: Mapping[str, str],
    actual: Mapping[str, str],
    *,
    mismatch_limit: int = 32,
) -> dict[str, object]:
    expected_names = set(expected)
    actual_names = set(actual)
    missing = sorted(expected_names - actual_names)
    unexpected = sorted(actual_names - expected_names)
    changed = sorted(
        name
        for name in expected_names & actual_names
        if expected[name] != actual[name]
    )
    exact = not missing and not unexpected and not changed
    return {
        "class_set_match": not missing and not unexpected,
        "exact_match": exact,
        "expected_class_count": len(expected),
        "actual_class_count": len(actual),
        "exact_class_count": sum(
            1
            for name in expected_names & actual_names
            if expected[name] == actual[name]
        ),
        "missing_class_count": len(missing),
        "unexpected_class_count": len(unexpected),
        "changed_class_count": len(changed),
        "missing_classes": missing[:mismatch_limit],
        "unexpected_classes": unexpected[:mismatch_limit],
        "changed_classes": changed[:mismatch_limit],
        "verification_state": "verified_exact" if exact else "requires_review",
    }


def freeze_plan_artifacts(
    plan: LombokExperimentPlan,
    *,
    destination: Path,
) -> LombokExperimentPlan:
    """Copy dependency and Processor artifacts into one private generation."""

    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    _chmod_private(destination)
    ordered = tuple(
        dict.fromkeys(
            path.resolve(strict=True)
            for path in (
                *plan.dependency_classpath,
                *plan.processor_model.processor_path,
            )
        )
    )
    copied: dict[Path, Path] = {}
    for dependency in plan.dependency_classpath:
        _reject_dependency_manifest_class_path(dependency)
    for index, source in enumerate(ordered):
        _require_regular_compiler_input(source)
        before = _sha256_file(source)
        suffix = source.suffix if source.suffix else ".artifact"
        target = destination / f"{index:04d}-{before[:20]}{suffix}"
        shutil.copyfile(source, target)
        try:
            target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        after = _sha256_file(source)
        copied_hash = _sha256_file(target)
        if before != after or copied_hash != before:
            raise LombokExperimentError(
                "COMPILE_MODEL_STALE",
                "A dependency changed while private inputs were frozen.",
                suggested_next_step=(
                    "Wait for dependency resolution to settle and retry."
                ),
                retryable=True,
            )
        copied[source] = target.resolve(strict=True)

    processor = replace(
        plan.processor_model,
        processor_path=tuple(
            copied[path.resolve(strict=True)]
            for path in plan.processor_model.processor_path
        ),
        path_entries=tuple(
            replace(entry, path=copied[entry.path.resolve(strict=True)])
            for entry in plan.processor_model.path_entries
        ),
    )
    frozen = replace(
        plan,
        dependency_classpath=tuple(
            copied[path.resolve(strict=True)] for path in plan.dependency_classpath
        ),
        processor_model=processor,
        plan_fingerprint="",
    )
    return replace(
        frozen,
        plan_fingerprint=_current_plan_fingerprint(frozen),
    )


def scan_class_hashes(root: Path) -> dict[str, str]:
    base = root.resolve(strict=True)
    files = sorted(base.rglob("*.class"))
    if len(files) > _MAX_CLASS_FILES:
        raise LombokExperimentError(
            "COMPILE_EXPERIMENT_LIMIT_EXCEEDED",
            "The generated class set exceeds the experiment limit.",
            suggested_next_step="Use the formal Maven build.",
        )
    hashes: dict[str, str] = {}
    total = 0
    for path in files:
        if _path_is_link_or_reparse(path) or not path.is_file():
            raise LombokExperimentError(
                "CLASS_OUTPUT_UNVERIFIED",
                "A generated class output is not a regular file.",
                suggested_next_step="Discard this experiment attempt.",
            )
        resolved = path.resolve(strict=True)
        try:
            relative = resolved.relative_to(base).as_posix()
        except ValueError as error:
            raise LombokExperimentError(
                "CLASS_OUTPUT_UNVERIFIED",
                "A generated class output escapes private staging.",
                suggested_next_step="Discard this experiment attempt.",
            ) from error
        size = resolved.stat().st_size
        total += size
        if total > _MAX_CLASS_BYTES:
            raise LombokExperimentError(
                "COMPILE_EXPERIMENT_LIMIT_EXCEEDED",
                "The generated class bytes exceed the experiment limit.",
                suggested_next_step="Use the formal Maven build.",
            )
        hashes[relative] = _sha256_file(resolved)
    return hashes


def _snapshot_source_inputs(
    source_root: Path,
) -> tuple[tuple[Path, ...], dict[Path, str]]:
    root = source_root.resolve(strict=True)
    sources: list[Path] = []
    hashes: dict[Path, str] = {}
    total_bytes = 0
    for candidate in sorted(root.rglob("*.java")):
        _reject_symlink_components(candidate, boundary=root)
        if _path_is_link_or_reparse(candidate) or not candidate.is_file():
            raise LombokExperimentError(
                "SOURCE_LAYOUT_UNVERIFIED",
                "A Java source is not a regular workspace file.",
                suggested_next_step="Use the formal Maven build.",
            )
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise LombokExperimentError(
                "SOURCE_OUTSIDE_MODULE",
                "A Java source escapes the selected source root.",
                suggested_next_step="Use the formal Maven build.",
            ) from error
        size = resolved.stat().st_size
        total_bytes += size
        if (
            len(sources) >= _MAX_SOURCE_FILES
            or total_bytes > _MAX_TOTAL_SOURCE_BYTES
        ):
            raise LombokExperimentError(
                "COMPILE_EXPERIMENT_LIMIT_EXCEEDED",
                "The module source set exceeds the experiment limit.",
                suggested_next_step=(
                    "Raise the internal benchmark limit only after review."
                ),
            )
        sources.append(resolved)
        hashes[resolved] = _sha256_file(resolved)
    if not sources:
        raise LombokExperimentError(
            "SOURCE_FILE_REQUIRED",
            "The selected module has no Java sources.",
            suggested_next_step="Select a module containing main sources.",
        )
    return tuple(sources), hashes


def _materialize_snapshot(
    plan: LombokExperimentPlan,
    *,
    mirror_project: Path,
) -> tuple[Path, ...]:
    mirrored_sources: list[Path] = []
    for source in plan.source_files:
        relative = source.relative_to(plan.project_root)
        destination = mirror_project / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != plan.source_hashes[source]:
            raise LombokExperimentError(
                "SOURCE_CHANGED_DURING_COMPILE",
                "A Java source changed after the plan was frozen.",
                suggested_next_step="Wait for edits to settle and retry.",
                retryable=True,
            )
        destination.write_bytes(data)
        if _sha256_file(destination) != plan.source_hashes[source]:
            raise LombokExperimentError(
                "SOURCE_CHANGED_DURING_COMPILE",
                "A private Java source copy could not be frozen.",
                suggested_next_step="Discard this attempt and retry.",
                retryable=True,
            )
        mirrored_sources.append(destination)
    for item in plan.config_snapshot.inputs:
        source = plan.project_root / item.relative_path
        destination = mirror_project / item.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != item.sha256:
            raise LombokExperimentError(
                "COMPILE_MODEL_STALE",
                "A Lombok configuration changed before compilation.",
                suggested_next_step="Recreate the experiment plan and retry.",
                retryable=True,
            )
        destination.write_bytes(data)
        if _sha256_file(destination) != item.sha256:
            raise LombokExperimentError(
                "COMPILE_MODEL_STALE",
                "A private Lombok configuration copy could not be frozen.",
                suggested_next_step="Discard this attempt and retry.",
                retryable=True,
            )
    return tuple(mirrored_sources)


def _current_plan_fingerprint(plan: LombokExperimentPlan) -> str:
    digest = hashlib.sha256()
    current_sources, source_hashes = _snapshot_source_inputs(plan.source_root)
    current_config = discover_lombok_configuration(
        plan.project_root,
        current_sources,
    )
    for path in plan.configuration_inputs:
        digest.update(b"config\0")
        digest.update(_normalized_path_bytes(path))
        if (
            path.exists()
            and path.is_file()
            and not _path_is_link_or_reparse(path)
        ):
            digest.update(_sha256_file(path).encode("ascii"))
        else:
            digest.update(b"<missing-or-unsafe>")
    digest.update(b"environment\0")
    digest.update(_environment_fingerprint().encode("ascii"))
    digest.update(b"javac\0")
    digest.update(_normalized_path_bytes(plan.javac_executable))
    try:
        javac_stat = plan.javac_executable.stat()
        digest.update(
            f"{javac_stat.st_size}:{javac_stat.st_mtime_ns}".encode("ascii")
        )
    except OSError:
        digest.update(b"<missing>")
    for path in plan.dependency_input_paths:
        digest.update(b"classpath-origin\0")
        digest.update(_normalized_path_bytes(path))
        digest.update(_path_fingerprint(path).encode("ascii"))
    for path in plan.processor_input_paths:
        digest.update(b"processor-path-origin\0")
        digest.update(_normalized_path_bytes(path))
        digest.update(_path_fingerprint(path).encode("ascii"))
    for path in plan.dependency_classpath:
        digest.update(b"classpath\0")
        digest.update(_normalized_path_bytes(path))
        digest.update(_path_fingerprint(path).encode("ascii"))
    for path in plan.processor_model.processor_path:
        digest.update(b"processor-path\0")
        digest.update(_normalized_path_bytes(path))
        digest.update(_path_fingerprint(path).encode("ascii"))
    digest.update(repr(plan.compiler_model).encode("utf-8"))
    digest.update(plan.processor_model.mode.encode("utf-8"))
    digest.update(plan.processor_model.proc.encode("utf-8"))
    for name in plan.processor_model.explicit_processor_names:
        digest.update(name.encode("utf-8"))
    for name, value in plan.processor_model.processor_options:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        if value is not None:
            digest.update(value.encode("utf-8"))
    digest.update(current_config.fingerprint.encode("ascii"))
    for path in current_sources:
        digest.update(path.relative_to(plan.project_root).as_posix().encode("utf-8"))
        digest.update(source_hashes[path].encode("ascii"))
    return digest.hexdigest()


def _assert_plan_fresh(plan: LombokExperimentPlan) -> None:
    if plan.environment_fingerprint != _environment_fingerprint():
        raise LombokExperimentError(
            "COMPILE_MODEL_STALE",
            "The compiler environment changed after planning.",
            suggested_next_step="Recreate the experiment plan and retry.",
            retryable=True,
        )
    _assert_artifact_snapshots(plan)
    if plan.plan_fingerprint != _current_plan_fingerprint(plan):
        raise LombokExperimentError(
            "COMPILE_MODEL_STALE",
            "Compiler inputs changed after the experiment plan was frozen.",
            suggested_next_step="Recreate the experiment plan and retry.",
            retryable=True,
        )


def _assert_artifact_snapshots(plan: LombokExperimentPlan) -> None:
    current = (
        tuple(_path_fingerprint(path) for path in plan.dependency_classpath),
        tuple(
            _path_fingerprint(path)
            for path in plan.processor_model.processor_path
        ),
        tuple(_path_fingerprint(path) for path in plan.configuration_inputs),
    )
    expected = (
        plan.dependency_artifact_fingerprints,
        plan.processor_artifact_fingerprints,
        plan.configuration_input_fingerprints,
    )
    if current != expected:
        raise LombokExperimentError(
            "COMPILE_MODEL_STALE",
            "A compiler input changed while its generation was captured.",
            suggested_next_step=(
                "Wait for dependency and Maven metadata writes to settle, "
                "then retry."
            ),
            retryable=True,
        )


def _processor_path_declarations(
    configurations: Sequence[ET.Element],
) -> tuple[tuple[str, str, str], ...]:
    declarations: list[tuple[tuple[str, str, str], ...]] = []
    for configuration in configurations:
        container = configuration.find("./{*}annotationProcessorPaths")
        if container is None:
            continue
        values: list[tuple[str, str, str]] = []
        for path in container.findall("./{*}path"):
            group = _child_text(path, "groupId")
            artifact = _child_text(path, "artifactId")
            version = _child_text(path, "version")
            if (
                not group
                or not artifact
                or not version
                or any("${" in value for value in (group, artifact, version))
                or any(_child_text(path, name) for name in ("classifier", "type"))
                or path.find("./{*}exclusions") is not None
            ):
                raise LombokExperimentError(
                    "PROCESSOR_PATH_UNRESOLVED",
                    "An annotationProcessorPaths entry is not reproducible.",
                    suggested_next_step="Use the formal Maven build.",
                )
            values.append((group, artifact, version))
        declarations.append(tuple(values))
    if not declarations:
        return ()
    first = declarations[0]
    if not first or any(value != first for value in declarations[1:]):
        raise LombokExperimentError(
            "PROCESSOR_PATH_UNRESOLVED",
            "Multiple effective Processor path declarations disagree.",
            suggested_next_step="Use the formal Maven build.",
        )
    return first


def _explicit_processor_names(
    configurations: Sequence[ET.Element],
) -> tuple[str, ...]:
    declarations: list[tuple[str, ...]] = []
    for configuration in configurations:
        container = configuration.find("./{*}annotationProcessors")
        if container is None:
            continue
        declarations.append(
            tuple(
                (item.text or "").strip()
                for item in container.findall("./{*}annotationProcessor")
                if (item.text or "").strip()
            )
        )
    if not declarations:
        return ()
    first = declarations[0]
    if not first or any(value != first for value in declarations[1:]):
        raise LombokExperimentError(
            "ANNOTATION_PROCESSING_UNVERIFIED",
            "Multiple explicit Processor declarations disagree.",
            suggested_next_step="Use the formal Maven build.",
        )
    return first


def _processor_options(
    configurations: Sequence[ET.Element],
) -> tuple[tuple[str, str | None], ...]:
    declarations: list[tuple[tuple[str, str | None], ...]] = []
    for configuration in configurations:
        container = configuration.find("./{*}compilerArgs")
        if container is None:
            continue
        values: list[tuple[str, str | None]] = []
        for element in container.findall("./{*}arg"):
            raw = (element.text or "").strip()
            if not raw.startswith("-A") or len(raw) <= 2:
                raise LombokExperimentError(
                    "COMPILE_MODEL_UNAVAILABLE",
                    "A compiler argument is outside the Processor option model.",
                    suggested_next_step="Use the formal Maven build.",
                )
            option = raw[2:]
            name, separator, value = option.partition("=")
            if not name or any(character.isspace() for character in name):
                raise LombokExperimentError(
                    "COMPILE_MODEL_UNAVAILABLE",
                    "An Annotation Processor option name is invalid.",
                    suggested_next_step="Use the formal Maven build.",
                )
            values.append((name, value if separator else None))
        declarations.append(tuple(values))
    if not declarations:
        return ()
    first = declarations[0]
    if any(value != first for value in declarations[1:]):
        raise LombokExperimentError(
            "COMPILE_MODEL_UNAVAILABLE",
            "Multiple Processor option declarations disagree.",
            suggested_next_step="Use the formal Maven build.",
        )
    return first


def _inspect_processor_path_entry(
    path: Path,
    *,
    required: bool,
) -> ProcessorPathEntry:
    if _path_is_link_or_reparse(path):
        raise LombokExperimentError(
            "PROCESSOR_PATH_UNRESOLVED",
            "A Processor search-path entry is a link or reparse point.",
            suggested_next_step="Use a regular Processor artifact.",
        )
    if not path.exists():
        raise LombokExperimentError(
            "PROCESSOR_PATH_UNRESOLVED",
            "A Processor search-path entry is missing.",
            suggested_next_step="Refresh the private Maven model.",
        )
    resolved = path.resolve(strict=True)
    providers: tuple[str, ...] = ()
    implementations: tuple[str, ...] = ()
    contains_lombok_classes = False
    version: str | None = None
    if resolved.is_dir():
        service_path = resolved / _PROCESSOR_SERVICE
        if service_path.exists() or _path_is_link_or_reparse(service_path):
            _reject_symlink_components(service_path, boundary=resolved)
            if _path_is_link_or_reparse(service_path) or not service_path.is_file():
                raise LombokExperimentError(
                    "ANNOTATION_PROCESSING_UNVERIFIED",
                    "A Processor service declaration is unsafe.",
                    suggested_next_step="Use the formal Maven build.",
                )
            providers = _parse_processor_service(service_path.read_bytes())
        manifest_path = resolved / "META-INF" / "MANIFEST.MF"
        if manifest_path.is_file():
            _reject_manifest_class_path(manifest_path.read_bytes())
        implementations = tuple(
            provider
            for provider, relative in _LOMBOK_PROCESSOR_CLASS_ENTRIES.items()
            if (resolved / relative).is_file()
        )
        lombok_root = resolved / "lombok"
        if lombok_root.exists() or _path_is_link_or_reparse(lombok_root):
            _reject_symlink_components(lombok_root, boundary=resolved)
            if _path_is_link_or_reparse(lombok_root) or not lombok_root.is_dir():
                raise LombokExperimentError(
                    "PROCESSOR_PATH_UNRESOLVED",
                    "A Lombok package entry is unsafe.",
                    suggested_next_step="Use regular dependency artifacts.",
                )
            contains_lombok_classes = any(
                candidate.is_file()
                for candidate in lombok_root.rglob("*.class")
            )
    elif resolved.is_file() and zipfile.is_zipfile(resolved):
        with zipfile.ZipFile(resolved) as archive:
            infos = archive.infolist()
            service_infos = [
                info for info in infos if info.filename == _PROCESSOR_SERVICE
            ]
            if len(service_infos) > 1:
                raise LombokExperimentError(
                    "ANNOTATION_PROCESSING_UNVERIFIED",
                    "A Processor JAR contains duplicate service declarations.",
                    suggested_next_step="Use a canonical dependency artifact.",
                )
            info = service_infos[0] if service_infos else None
            if info is not None:
                if info.file_size > _MAX_PROCESSOR_SERVICE_BYTES:
                    raise _processor_service_limit_error()
                providers = _parse_processor_service(archive.read(info))
            try:
                manifest = archive.read("META-INF/MANIFEST.MF")
            except KeyError:
                manifest = b""
            _reject_manifest_class_path(manifest)
            match = re.search(
                rb"(?im)^Lombok-Version:\s*([^\r\n]+)",
                manifest,
            )
            if match is not None:
                version = match.group(1).decode("ascii", errors="strict").strip()
            names = {info.filename for info in infos}
            implementations = tuple(
                provider
                for provider, relative in _LOMBOK_PROCESSOR_CLASS_ENTRIES.items()
                if relative in names
            )
            contains_lombok_classes = any(
                name.startswith("lombok/") and name.endswith(".class")
                for name in names
            )
    elif required:
        raise LombokExperimentError(
            "PROCESSOR_PATH_UNRESOLVED",
            "The explicit Lombok Processor artifact is not a JAR.",
            suggested_next_step="Use the formal Maven build.",
        )
    return ProcessorPathEntry(
        path=resolved,
        sha256=_path_fingerprint(resolved),
        providers=providers,
        provider_implementations=implementations,
        contains_lombok_classes=contains_lombok_classes,
        lombok_version=version,
    )


def _reject_manifest_class_path(data: bytes) -> None:
    if _manifest_declares_class_path(data):
        raise LombokExperimentError(
            "PROCESSOR_PATH_UNRESOLVED",
            "A Processor-path manifest expands the effective class path.",
            suggested_next_step=(
                "Remove the manifest Class-Path or use the formal Maven build."
            ),
        )


def _reject_dependency_manifest_class_path(path: Path) -> None:
    """Reject JAR-relative classpath expansion before private relocation.

    The experiment copies and content-addresses dependencies into a private
    directory.  A manifest ``Class-Path`` is resolved relative to the original
    JAR location, so preserving only the declaring JAR would change javac's
    effective classpath.
    """

    if not path.is_file() or not zipfile.is_zipfile(path):
        return
    try:
        with zipfile.ZipFile(path) as archive:
            manifests = [
                info
                for info in archive.infolist()
                if info.filename.casefold() == "meta-inf/manifest.mf"
            ]
            if len(manifests) > 1:
                raise LombokExperimentError(
                    "COMPILE_MODEL_UNAVAILABLE",
                    "A compile dependency has duplicate manifests.",
                    suggested_next_step="Use the formal Maven build.",
                )
            data = archive.read(manifests[0]) if manifests else b""
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise LombokExperimentError(
            "COMPILE_MODEL_UNAVAILABLE",
            "A compile dependency manifest could not be inspected safely.",
            suggested_next_step="Use the formal Maven build.",
        ) from error
    if _manifest_declares_class_path(data):
        raise LombokExperimentError(
            "COMPILE_MODEL_UNAVAILABLE",
            "A compile dependency manifest expands the effective class path.",
            suggested_next_step=(
                "Use Maven until manifest Class-Path dependencies are modeled."
            ),
        )


def _manifest_declares_class_path(data: bytes) -> bool:
    if not data:
        return False
    text = data.decode("iso-8859-1", errors="strict")
    unfolded = re.sub(r"\r?\n[ \t]+", "", text)
    return bool(re.search(r"(?im)^Class-Path\s*:", unfolded))


def _parse_processor_service(data: bytes) -> tuple[str, ...]:
    if len(data) > _MAX_PROCESSOR_SERVICE_BYTES:
        raise _processor_service_limit_error()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LombokExperimentError(
            "ANNOTATION_PROCESSING_UNVERIFIED",
            "A Processor service declaration is not UTF-8.",
            suggested_next_step="Use the formal Maven build.",
        ) from error
    return tuple(
        dict.fromkeys(
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    )


def _collect_config_imports(
    path: Path,
    *,
    project: Path,
    found: dict[Path, LombokConfigInput],
    imported: set[Path],
    active: list[Path],
    budget: _ConfigBudget,
) -> None:
    if path in active:
        raise LombokExperimentError(
            "LOMBOK_CONFIG_IMPORT_UNVERIFIED",
            "A Lombok configuration import cycle is unsupported.",
            suggested_next_step="Remove the cycle or use Maven.",
        )
    existing = found.get(path)
    if existing is not None:
        if path in imported and not existing.imported:
            found[path] = replace(existing, imported=True)
        return
    data = path.read_bytes()
    budget.observe(path, len(data), len(active))
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise LombokExperimentError(
            "LOMBOK_CONFIG_UNVERIFIED",
            "A Lombok configuration input is not UTF-8.",
            suggested_next_step="Use the formal Maven build.",
        ) from error
    relative = path.relative_to(project).as_posix()
    found[path] = LombokConfigInput(
        path=path,
        relative_path=relative,
        sha256=hashlib.sha256(data).hexdigest(),
        imported=path in imported,
    )
    active.append(path)
    try:
        for raw in _config_imports(text):
            imported_path = _resolve_config_import(
                raw,
                importing=path,
                project=project,
            )
            imported.add(imported_path)
            _collect_config_imports(
                imported_path,
                project=project,
                found=found,
                imported=imported,
                active=active,
                budget=budget,
            )
    finally:
        active.pop()


def _config_stops_bubbling(
    path: Path,
    *,
    project: Path,
    active: list[Path],
    depth: int,
    visited: set[Path] | None = None,
) -> bool:
    resolution_visited = visited if visited is not None else set()
    return _config_graph_stops_bubbling(
        path,
        project=project,
        active=active,
        depth=depth,
        visited=resolution_visited,
    )


def _config_graph_stops_bubbling(
    path: Path,
    *,
    project: Path,
    active: list[Path],
    depth: int,
    visited: set[Path],
) -> bool:
    """Match Lombok's per-resolution visited and stop-bubbling OR rules."""
    if depth > _MAX_LOMBOK_CONFIG_DEPTH or path in active:
        raise LombokExperimentError(
            "LOMBOK_CONFIG_IMPORT_UNVERIFIED",
            "The Lombok import graph is cyclic or too deep.",
            suggested_next_step="Use the formal Maven build.",
        )
    if path in visited:
        return False
    visited.add(path)
    text = path.read_text(encoding="utf-8-sig")
    stops_bubbling = (
        _local_config_stop_bubbling_state(text)
        is _StopBubblingState.TRUE
    )
    active.append(path)
    try:
        for raw in _config_imports(text):
            imported = _resolve_config_import(
                raw,
                importing=path,
                project=project,
            )
            imported_stops = _config_graph_stops_bubbling(
                imported,
                project=project,
                active=active,
                depth=depth + 1,
                visited=visited,
            )
            if imported_stops:
                stops_bubbling = True
    finally:
        active.pop()
    return stops_bubbling


def _local_config_stop_bubbling_state(text: str) -> _StopBubblingState:
    """Resolve last-wins operations declared by one config file only."""
    state = _StopBubblingState.ABSENT
    for line in text.splitlines():
        stripped = line.strip()
        match = _STOP_BUBBLING.match(stripped)
        if match is not None:
            state = (
                _StopBubblingState.TRUE
                if match.group(1).casefold() == "true"
                else _StopBubblingState.FALSE
            )
        elif _CLEAR_STOP_BUBBLING.match(stripped) is not None:
            state = _StopBubblingState.CLEAR
    return state


def _config_imports(text: str) -> tuple[str, ...]:
    imports: list[str] = []
    saw_setting = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("import ") and not saw_setting:
            imports.append(stripped[len("import ") :].strip())
        else:
            saw_setting = True
    return tuple(imports)


def _resolve_config_import(
    raw: str,
    *,
    importing: Path,
    project: Path,
) -> Path:
    if (
        not raw
        or raw.startswith(("~", "<"))
        or "!" in raw
        or Path(raw).is_absolute()
        or PureWindowsPath(raw).is_absolute()
        or _WINDOWS_ABSOLUTE.match(raw)
    ):
        raise LombokExperimentError(
            "LOMBOK_CONFIG_IMPORT_UNVERIFIED",
            "A Lombok configuration import uses an unsupported location.",
            suggested_next_step=(
                "Use a relative workspace file import or the formal Maven build."
            ),
        )
    lexical = Path(os.path.abspath(importing.parent / raw))
    try:
        lexical.relative_to(project)
    except ValueError as error:
        raise LombokExperimentError(
            "LOMBOK_CONFIG_IMPORT_UNVERIFIED",
            "A Lombok configuration import leaves the workspace.",
            suggested_next_step="Use workspace-relative imports or Maven.",
        ) from error
    _reject_symlink_components(lexical, boundary=project)
    return _safe_workspace_config(lexical, project)


def _safe_workspace_config(path: Path, project: Path) -> Path:
    _reject_symlink_components(path, boundary=project)
    if _path_is_link_or_reparse(path) or not path.is_file():
        raise LombokExperimentError(
            "LOMBOK_CONFIG_UNVERIFIED",
            "A Lombok configuration input is not a regular file.",
            suggested_next_step="Use regular workspace configuration files.",
        )
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(project)
    except ValueError as error:
        raise LombokExperimentError(
            "LOMBOK_CONFIG_OUTSIDE_WORKSPACE_UNVERIFIED",
            "Lombok reads configuration outside the workspace.",
            suggested_next_step=(
                "Move effective configuration into the workspace or use Maven."
            ),
        ) from error
    if resolved.stat().st_size > _MAX_LOMBOK_CONFIG_BYTES:
        raise _config_limit_error()
    return resolved


def _reject_external_lombok_config(project: Path) -> None:
    current = project.parent
    while True:
        candidate = current / "lombok.config"
        if candidate.exists() or _path_is_link_or_reparse(candidate):
            raise LombokExperimentError(
                "LOMBOK_CONFIG_OUTSIDE_WORKSPACE_UNVERIFIED",
                "Lombok reads configuration outside the workspace.",
                suggested_next_step=(
                    "Move effective configuration into the workspace or use Maven."
                ),
            )
        if current.parent == current:
            break
        current = current.parent


def _reject_symlink_components(path: Path, *, boundary: Path) -> None:
    boundary = boundary.resolve(strict=True)
    lexical = Path(os.path.abspath(path))
    try:
        relative = lexical.relative_to(boundary)
    except ValueError as error:
        raise LombokExperimentError(
            "PATH_OUTSIDE_WORKSPACE",
            "A compiler input leaves its allowed workspace boundary.",
            suggested_next_step="Use regular files inside the workspace.",
        ) from error
    current = boundary
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError:
            continue
        if _metadata_is_link_or_reparse(metadata):
            raise LombokExperimentError(
                "SYMLINK_INPUT_UNVERIFIED",
                "A compiler input traverses a link or reparse point.",
                suggested_next_step="Use regular workspace files.",
            )


def _metadata_is_link_or_reparse(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(
        stat.S_ISLNK(metadata.st_mode)
        or (reparse_flag and attributes & reparse_flag)
    )


def _path_is_link_or_reparse(path: Path) -> bool:
    try:
        return _metadata_is_link_or_reparse(path.lstat())
    except OSError:
        return False


def _require_regular_compiler_input(path: Path) -> None:
    if _path_is_link_or_reparse(path) or not path.is_file():
        raise LombokExperimentError(
            "COMPILE_MODEL_UNAVAILABLE",
            "A compile-classpath entry is not one regular artifact file.",
            suggested_next_step=(
                "Use regular dependency artifacts or the formal Maven build."
            ),
        )


def validate_compiler_environment() -> None:
    configured = [name for name in _COMPILER_ENVIRONMENT_INPUTS if os.environ.get(name)]
    if configured:
        raise LombokExperimentError(
            "COMPILER_ENVIRONMENT_UNVERIFIED",
            "External JVM/Maven/compiler options can change experiment semantics.",
            suggested_next_step=(
                "Unset JDK_JAVAC_OPTIONS, JAVA_TOOL_OPTIONS, and _JAVA_OPTIONS "
                "as well as MAVEN_ARGS/MAVEN_OPTS for this experiment."
            ),
            context={"configured_environment_name_count": len(configured)},
        )


def _environment_fingerprint() -> str:
    validate_compiler_environment()
    digest = hashlib.sha256()
    for name in _COMPILER_ENVIRONMENT_INPUTS:
        digest.update(name.encode("ascii"))
        digest.update(b"<unset>")
    return digest.hexdigest()


def _boolean_compiler_value(
    maven: MavenBuildSystemAdapter,
    project: ET.Element,
    configurations: tuple[ET.Element, ...],
    *,
    config_name: str,
    property_name: str,
    default: bool,
) -> bool:
    value = maven._compiler_value(  # noqa: SLF001
        project,
        configurations,
        config_name=config_name,
        property_name=property_name,
    ).strip()
    if not value:
        return default
    if value.casefold() not in {"true", "false"}:
        raise LombokExperimentError(
            "COMPILE_MODEL_UNAVAILABLE",
            f"The Maven {config_name} policy cannot be reproduced.",
            suggested_next_step="Use the formal Maven build.",
        )
    return value.casefold() == "true"


def _maven_version_at_least(
    value: str | None,
    required: tuple[int, int, int],
) -> bool:
    if not value:
        return False
    match = re.fullmatch(
        r"\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-.][0-9A-Za-z_.-]+)?\s*",
        value,
    )
    if match is None:
        return False
    parsed = tuple(int(item or "0") for item in match.groups())
    return parsed >= required


def _child_text(element: ET.Element, name: str) -> str:
    child = element.find(f"./{{*}}{name}")
    return (child.text or "").strip() if child is not None else ""


def _path_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    if _path_is_link_or_reparse(path):
        raise LombokExperimentError(
            "COMPILER_INPUT_UNVERIFIED",
            "A compiler path is a link or reparse point.",
            suggested_next_step="Use the formal Maven build.",
        )
    if path.is_file():
        return _sha256_file(path)
    if not path.is_dir():
        return hashlib.sha256(b"<missing>").hexdigest()
    for child in sorted(path.rglob("*")):
        if _path_is_link_or_reparse(child):
            raise LombokExperimentError(
                "COMPILER_INPUT_UNVERIFIED",
                "A compiler path contains a symbolic link.",
                suggested_next_step="Use the formal Maven build.",
            )
        if child.is_file():
            digest.update(child.relative_to(path).as_posix().encode("utf-8"))
            digest.update(b"\0")
            with child.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalized_path_bytes(path: Path) -> bytes:
    return os.path.normcase(str(path.resolve(strict=False))).encode(
        "utf-8",
        errors="surrogateescape",
    )


def _write_javac_argfile(
    path: Path,
    arguments: Iterable[str],
    *,
    encoding: str = _JAVAC_ARGFILE_ENCODING,
) -> None:
    values = [str(argument) for argument in arguments]
    if any(
        any(character in value for character in ("\r", "\n", "\x00"))
        for value in values
    ):
        raise LombokExperimentError(
            "INVALID_COMPILER_ARGUMENT",
            "A javac argument contains a forbidden control character.",
            suggested_next_step=(
                "Use regular project paths and compiler options."
            ),
        )
    lines = [_quote_javac_argument(value) for value in values]
    path.write_text("\n".join(lines) + "\n", encoding=encoding)


def _quote_javac_argument(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _from_maven_error(error: MavenResolutionError) -> LombokExperimentError:
    return LombokExperimentError(
        str(error.error_code),
        str(error),
        suggested_next_step=error.suggested_next_step,
        retryable=error.retryable,
        context=error.context,
    )


def _config_limit_error() -> LombokExperimentError:
    return LombokExperimentError(
        "LOMBOK_CONFIG_UNVERIFIED",
        "The Lombok configuration graph exceeds experiment limits.",
        suggested_next_step="Use the formal Maven build.",
    )


def _processor_service_limit_error() -> LombokExperimentError:
    return LombokExperimentError(
        "ANNOTATION_PROCESSING_UNVERIFIED",
        "A Processor service declaration exceeds the safety limit.",
        suggested_next_step="Use the formal Maven build.",
    )


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(0o700)
    except OSError:
        pass


def monotonic_ms(started_at: float) -> float:
    return round((time.monotonic() - started_at) * 1000, 3)


__all__ = [
    "AnnotationProcessorModel",
    "ExperimentCompilerModel",
    "LombokCompileAttempt",
    "LombokExperimentError",
    "LombokExperimentPlan",
    "LombokExperimentPlanner",
    "LombokExperimentRunner",
    "compare_class_outputs",
    "discover_lombok_configuration",
    "monotonic_ms",
    "scan_class_hashes",
    "validate_compiler_environment",
]
