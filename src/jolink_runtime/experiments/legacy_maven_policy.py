"""Frozen direct-javac experiment policy; never imported by product launch."""

import os
import re
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET
import zipfile
from jolink_runtime.launch.maven import MavenBuildSystemAdapter, MavenResolutionError
from jolink_runtime.launch.contracts import LaunchErrorCode

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
    ("org.apache.maven.plugins", "maven-clean-plugin"): frozenset({"clean"}),
    ("org.apache.maven.plugins", "maven-compiler-plugin"): frozenset(
        {"compile", "testcompile"}
    ),
    ("org.apache.maven.plugins", "maven-deploy-plugin"): frozenset({"deploy"}),
    ("org.apache.maven.plugins", "maven-enforcer-plugin"): frozenset({"enforce"}),
    ("org.apache.maven.plugins", "maven-failsafe-plugin"): frozenset(
        {"integration-test", "verify"}
    ),
    ("org.apache.maven.plugins", "maven-install-plugin"): frozenset({"install"}),
    ("org.apache.maven.plugins", "maven-jar-plugin"): frozenset({"jar", "test-jar"}),
    ("org.apache.maven.plugins", "maven-resources-plugin"): frozenset(
        {"copy-resources", "resources", "testresources"}
    ),
    ("org.apache.maven.plugins", "maven-site-plugin"): frozenset({"deploy", "site"}),
    # source:jar-no-fork has declared package as its default phase since it
    # was introduced in maven-source-plugin 2.1.  Unlike source:jar, it does
    # not fork an earlier lifecycle, so an execution without an explicit
    # phase cannot affect the compile/process-classes model validated here.
    ("org.apache.maven.plugins", "maven-source-plugin"): frozenset({"jar-no-fork"}),
    ("org.apache.maven.plugins", "maven-surefire-plugin"): frozenset({"test"}),
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
        "optimize",
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

_MAX_MAVEN_PROJECT_CONFIG_BYTES = 1024 * 1024


_MAVEN_ENVIRONMENT_INPUTS = ("MAVEN_ARGS", "MAVEN_OPTS")

_UNMODELED_MAVEN_ARGUMENT_PATTERN = re.compile(
    r"(?i)(?:maven\.compiler\.|maven\.ext\.class\.path|"
    r"project\.build\.sourceencoding|(?:^|[\s'\"])-dencoding(?:=|\s)|"
    r"-javaagent:|-xbootclasspath(?:/a|/p)?:|--patch-module)"
)


class LegacyMavenPolicy(MavenBuildSystemAdapter):
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
            for plugin in project.findall("./{*}build/{*}plugins/{*}plugin")
            if self._child_text(plugin, "artifactId") == "maven-compiler-plugin"
        ]
        if len(compiler_matches) > 1:
            self._raise_unverified_transform()
        compiler = compiler_matches[0] if compiler_matches else None
        if compiler is not None:
            compiler_group = (
                self._child_text(compiler, "groupId") or "org.apache.maven.plugins"
            )
            compile_execution_count = sum(
                1
                for execution in compiler.findall("./{*}executions/{*}execution")
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
            if proc_values and all(value.casefold() == "none" for value in proc_values)
            else "full"
        )
        fail_on_warning_values = self._compiler_declared_values(
            project,
            configurations,
            config_name="failOnWarning",
            property_name="maven.compiler.failOnWarning",
        )
        if any(value.casefold() != "false" for value in fail_on_warning_values):
            raise MavenResolutionError(
                LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED,
                "The Maven fail-on-warning policy cannot be reproduced safely.",
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
                    self._local_name(child.tag) not in _SAFE_COMPILER_CONFIGURATION
                    and self._local_name(child.tag) not in experiment_only_names
                ):
                    self._raise_unverified_transform()
            if (
                (
                    not allow_lombok_processor
                    and (
                        configuration.find("./{*}annotationProcessorPaths") is not None
                        or configuration.find("./{*}annotationProcessors") is not None
                        or configuration.find("./{*}compilerArgs") is not None
                    )
                )
                or configuration.find("./{*}compilerArguments") is not None
                or self._config_text(configuration, "compilerArgument")
                or self._config_text(configuration, "executable")
            ):
                self._raise_unverified_transform()
        for plugin in project.findall("./{*}build/{*}plugins/{*}plugin"):
            group_id = (
                self._child_text(plugin, "groupId") or "org.apache.maven.plugins"
            ).casefold()
            artifact_id = self._child_text(plugin, "artifactId").casefold()
            if artifact_id in _BYTECODE_TRANSFORM_PLUGINS:
                self._raise_unverified_transform()
            for execution in plugin.findall("./{*}executions/{*}execution"):
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
                    self._raise_unverified_transform(
                        context={
                            "plugin": group_id + ":" + artifact_id,
                            "goals": goals,
                            "reason": "implicit_goal_phase_unmodeled",
                        }
                    )

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

        extension_property = project.find("./{*}properties/{*}maven.ext.class.path")
        if extension_property is not None and (extension_property.text or "").strip():
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
                item.strip().casefold() for item in value.split(",") if item.strip()
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
    def _raise_unverified_transform(*, context: dict[str, Any] | None = None) -> None:
        raise MavenResolutionError(
            LaunchErrorCode.ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED,
            "The Maven build uses unverified annotation processing or "
            "bytecode transformation.",
            retryable=False,
            suggested_next_step=(
                "Use the formal Maven build and restart the application."
            ),
            context=context,
        )
