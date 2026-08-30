"""IDEA-to-Maven project launch preparation for one supervised attempt."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .controller import (
    LaunchContext,
    LaunchPipelineFailure,
)
from .contracts import (
    BuildOperationSpec,
    BuildPlan,
    JvmLaunchPlan,
    LaunchErrorCode,
    LaunchPhase,
)
from .idea_environment import (
    IdeaBuildPreferences,
    IdeaEnvironmentImporter,
)
from .idea_importer import (
    IdeaLaunchImportError,
    IdeaLaunchImporter,
)
from .fast_compile import FastCompilePlan
from .gradle_probe import (
    GradleProbeError,
    ProductGradleProbe,
    gradle_configuration_environment_names,
    gradle_configuration_inputs,
    wrapper_version,
)
from .gradle_runtime_build_world import (
    GradleRuntimeBuildWorldError,
    create_gradle_runtime_build_world,
)
from .jdt_compile_session import JdtBuildWorldPlan
from .java_command import (
    JavaCommandMaterializer,
    MaterializedJavaCommand,
)
from .maven import (
    MavenBuildSystemAdapter,
    MavenExecutionPlan,
    MavenResolutionError,
)
from .toolchain import (
    JavaToolchainCandidate,
    JavaToolchainResolver,
    MavenToolCandidate,
    MavenToolResolver,
)
from .test_build_world import build_input_manifest


@dataclass(frozen=True)
class ProjectLaunchRequest:
    """MCP-only project startup parameters, separate from RuntimeAction."""

    project_path: Path
    launch_name: str | None
    jdwp_port: int
    ready_port: int
    startup_wait_timeout_seconds: float


@dataclass(frozen=True)
class PreparedProjectLaunch:
    execution: MavenExecutionPlan | None
    build_system: str
    build_offline: bool
    build_jdk: JavaToolchainCandidate
    module_output: Path
    generation_input_roots: tuple[Path, ...]
    generation_input_manifest: dict[str, str]
    resource_source_roots: tuple[Path, ...]
    resource_input_manifest: dict[str, str]
    build_world_inputs: tuple[Path, ...]
    runtime_jdk: JavaToolchainCandidate
    jvm_plan: JvmLaunchPlan
    command: MaterializedJavaCommand
    warnings: tuple[str, ...]
    attempt_directory: Path
    fast_compile_plan: FastCompilePlan | None = None
    fast_compile_unavailable_reason: str | None = None
    jdt_build_world_plan: JdtBuildWorldPlan | None = None
    jdt_unavailable_reason: str | None = None
    jdt_source_snapshot_roots: tuple[Path, ...] = ()
    source_manifest_fingerprint: str | None = None
    source_manifest_before_ms: float | None = None
    source_manifest_after_ms: float | None = None


class ProjectLaunchPipeline:
    """Resolve a launch plan; the Java adapter still owns the long-lived JVM."""

    def __init__(
        self,
        *,
        idea_launches: IdeaLaunchImporter | None = None,
        idea_environment: IdeaEnvironmentImporter | None = None,
        maven_adapter: MavenBuildSystemAdapter | None = None,
        java_toolchains: JavaToolchainResolver | None = None,
        maven_tools: MavenToolResolver | None = None,
        command_materializer: JavaCommandMaterializer | None = None,
    ) -> None:
        self._idea_launches = idea_launches or IdeaLaunchImporter()
        self._idea_environment = (
            idea_environment or IdeaEnvironmentImporter()
        )
        self._maven = maven_adapter or MavenBuildSystemAdapter()
        self._java = java_toolchains or JavaToolchainResolver()
        self._maven_tools = maven_tools or MavenToolResolver()
        self._commands = command_materializer or JavaCommandMaterializer()

    def create_attempt_directory(self, attempt_id: str) -> Path:
        directory = Path(
            tempfile.mkdtemp(prefix=f"jolink-{attempt_id}-")
        )
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        return directory

    def prepare(
        self,
        context: LaunchContext,
        request: ProjectLaunchRequest,
        *,
        attempt_directory: Path,
    ) -> PreparedProjectLaunch:
        context.check_cancelled()
        try:
            imported = self._idea_launches.select(
                request.project_path,
                request.launch_name,
            )
        except IdeaLaunchImportError as error:
            raise self._idea_failure(error) from error
        context.set_intent(imported.intent)
        context.transition(LaunchPhase.RESOLVING_BUILD)

        preferences = self._idea_environment.import_preferences(
            request.project_path
        )
        has_maven = (request.project_path / "pom.xml").is_file()
        has_gradle = (
            (request.project_path / "gradlew").is_file()
            or (request.project_path / "gradlew.bat").is_file()
        ) and any(
            (request.project_path / name).is_file()
            for name in ("build.gradle", "build.gradle.kts")
        )
        if has_maven and has_gradle:
            raise LaunchPipelineFailure(
                "BUILD_SYSTEM_AMBIGUOUS",
                "The project contains both supported Maven and Gradle builds.",
                retryable=False,
                suggested_next_step=(
                    "Use a project_path containing one authoritative build."
                ),
            )
        if has_gradle:
            try:
                return self._prepare_gradle(
                    context,
                    request,
                    imported=imported,
                    preferences=preferences,
                    attempt_directory=attempt_directory,
                )
            except GradleProbeError as error:
                raise LaunchPipelineFailure(
                    error.error_code,
                    str(error),
                    retryable=False,
                    suggested_next_step=(
                        "Correct the Gradle Wrapper/Probe input and retry launch."
                    ),
                ) from error
        try:
            workspace = self._maven.resolve_workspace(
                request.project_path
            )
            module = self._maven.select_module(
                workspace,
                imported.intent,
            )
        except MavenResolutionError as error:
            raise self._maven_failure(error) from error

        build_log = attempt_directory / "build.log"
        build_jdk = self._select_java(
            context,
            preferences=preferences,
            explicit_reference=None,
            for_build=True,
            cwd=workspace.build_root,
            build_log=build_log,
        )
        runtime_jdk = self._select_java(
            context,
            preferences=preferences,
            explicit_reference=imported.intent.runtime_jdk_reference,
            for_build=False,
            cwd=workspace.build_root,
            build_log=build_log,
            already_probed=build_jdk,
        )
        maven = self._select_maven(
            context,
            project_root=workspace.project_root,
            preferences=preferences,
            build_jdk=build_jdk,
            build_log=build_log,
        )
        try:
            execution = self._maven.create_execution_plan(
                workspace=workspace,
                module=module,
                intent=imported.intent,
                maven=maven,
                build_jdk=build_jdk,
                preferences=preferences,
                attempt_directory=attempt_directory,
            )
        except MavenResolutionError as error:
            raise self._maven_failure(error) from error
        context.set_build_plan(execution.build_plan)
        source_roots = (module.directory / "src/main/java",)
        manifest_started = time.monotonic()
        source_manifest_before = self.source_manifest_fingerprint(
            source_roots
        )
        source_manifest_before_ms = (
            time.monotonic() - manifest_started
        ) * 1000

        if imported.intent.build_before_run:
            context.transition(LaunchPhase.COMPILING)
        else:
            context.transition(LaunchPhase.RESOLVING_RUNTIME)
        build_result = context.run_operation(
            self._maven.create_build_operation(execution)
        )
        if build_result.timed_out:
            raise LaunchPipelineFailure(
                LaunchErrorCode.BUILD_TIMEOUT,
                "The supervised Maven build timed out.",
                retryable=True,
                suggested_next_step=(
                    "Inspect build.log_tail, correct the blocked build, and "
                    "retry run."
                ),
            )
        if not build_result.succeeded:
            raise LaunchPipelineFailure(
                LaunchErrorCode.BUILD_FAILED,
                "The supervised Maven build failed.",
                retryable=True,
                suggested_next_step=(
                    "Inspect build.log_tail, correct the Maven failure, and "
                    "retry run."
                ),
                context={"return_code": build_result.return_code},
            )
        manifest_started = time.monotonic()
        source_manifest_after = self.source_manifest_fingerprint(source_roots)
        source_manifest_after_ms = (
            time.monotonic() - manifest_started
        ) * 1000
        if source_manifest_before != source_manifest_after:
            raise LaunchPipelineFailure(
                LaunchErrorCode.SOURCE_CHANGED_DURING_BUILD,
                "Project Java sources changed while Maven was building.",
                retryable=True,
                suggested_next_step=(
                    "Wait for source edits to settle, then call launch again."
                ),
            )
        compile_classpath_result = context.run_operation(
            self._maven.create_compile_classpath_operation(execution)
        )
        if imported.intent.build_before_run:
            context.transition(LaunchPhase.RESOLVING_RUNTIME)
        try:
            plan = self._maven.consume_jvm_launch_plan(
                execution=execution,
                intent=imported.intent,
                runtime_jdk=runtime_jdk,
                ready_port=request.ready_port,
                startup_wait_timeout_seconds=(
                    request.startup_wait_timeout_seconds
                ),
            )
        except MavenResolutionError as error:
            raise self._maven_failure(error) from error
        plan, command = self.materialize_command(
            plan,
            jdwp_port=request.jdwp_port,
            attempt_directory=attempt_directory,
        )
        context.set_jvm_launch_plan(plan)
        fast_compile_plan: FastCompilePlan | None = None
        fast_compile_unavailable_reason: str | None = None
        jdt_build_world_plan: JdtBuildWorldPlan | None = None
        jdt_unavailable_reason: str | None = None
        if (
            compile_classpath_result.succeeded
            and not execution.build_plan.compile_required
        ):
            fast_compile_unavailable_reason = (
                "JDT_RELOAD_REQUIRES_FRESH_MAVEN_BASELINE"
            )
            jdt_unavailable_reason = (
                "JDT_RELOAD_REQUIRES_FRESH_MAVEN_BASELINE"
            )
        elif compile_classpath_result.succeeded:
            try:
                fast_compile_plan = self._maven.consume_fast_compile_plan(
                    execution=execution,
                    runtime_jdk=runtime_jdk,
                )
            except MavenResolutionError as error:
                fast_compile_unavailable_reason = error.error_code.value
            try:
                jdt_build_world_plan = self._maven.consume_jdt_build_world_plan(
                    execution=execution,
                    runtime_jdk=runtime_jdk,
                )
            except MavenResolutionError as error:
                jdt_unavailable_reason = error.error_code.value
        else:
            fast_compile_unavailable_reason = "COMPILE_CLASSPATH_UNAVAILABLE"
            jdt_unavailable_reason = "COMPILE_CLASSPATH_UNAVAILABLE"
        capability_warnings: tuple[str, ...] = ()
        if fast_compile_plan is None and jdt_build_world_plan is None:
            capability_warnings = (
                "Fast runtime-only source update is unavailable for this "
                "launch; formal Maven build and restart remain available.",
            )
        if jdt_build_world_plan is None:
            capability_warnings = (
                *capability_warnings,
                "Persistent JDT reload is unavailable for this launch; "
                "restart remains available.",
            )
        return PreparedProjectLaunch(
            execution=execution,
            build_system="maven",
            build_offline=False,
            build_jdk=build_jdk,
            module_output=module.output_directory,
            generation_input_roots=(module.output_directory,),
            generation_input_manifest={},
            resource_source_roots=(),
            resource_input_manifest={},
            build_world_inputs=(
                execution.effective_pom_file,
                execution.classpath_file,
                execution.compile_classpath_file,
            ),
            runtime_jdk=runtime_jdk,
            jvm_plan=plan,
            command=command,
            warnings=tuple(
                dict.fromkeys(
                    (
                        *imported.warnings,
                        *preferences.warnings,
                        *capability_warnings,
                    )
                )
            ),
            attempt_directory=attempt_directory,
            fast_compile_plan=fast_compile_plan,
            fast_compile_unavailable_reason=(
                fast_compile_unavailable_reason
            ),
            jdt_build_world_plan=jdt_build_world_plan,
            jdt_unavailable_reason=jdt_unavailable_reason,
            source_manifest_fingerprint=source_manifest_after,
            source_manifest_before_ms=source_manifest_before_ms,
            source_manifest_after_ms=source_manifest_after_ms,
        )

    def _prepare_gradle(
        self,
        context: LaunchContext,
        request: ProjectLaunchRequest,
        *,
        imported: Any,
        preferences: IdeaBuildPreferences,
        attempt_directory: Path,
    ) -> PreparedProjectLaunch:
        project = request.project_path.expanduser().resolve(strict=True)
        if not imported.intent.build_before_run:
            raise LaunchPipelineFailure(
                "GRADLE_RUNTIME_REQUIRES_BUILD_BEFORE_RUN",
                "Gradle Runtime launch requires IDEA Make/Build before run.",
                retryable=False,
                suggested_next_step=(
                    "Enable Make/Build in the IDEA launch configuration and retry."
                ),
            )
        if (project / "buildSrc").exists() or (project / "build-logic").exists():
            raise LaunchPipelineFailure(
                "GRADLE_BUILD_LOGIC_UNSUPPORTED",
                "Gradle buildSrc/build-logic is not supported in G4.",
                retryable=False,
                suggested_next_step="Use the formal Gradle launch for this project.",
            )
        build_log = attempt_directory / "build.log"
        build_jdk = self._select_java(
            context,
            preferences=preferences,
            explicit_reference=None,
            for_build=True,
            cwd=project,
            build_log=build_log,
        )
        runtime_jdk = self._select_java(
            context,
            preferences=preferences,
            explicit_reference=imported.intent.runtime_jdk_reference,
            for_build=False,
            cwd=project,
            build_log=build_log,
            already_probed=build_jdk,
        )
        probe = ProductGradleProbe.load()
        version = wrapper_version(project)
        if version not in probe.supported_versions:
            raise LaunchPipelineFailure(
                "GRADLE_VERSION_UNSUPPORTED",
                "This Gradle Wrapper version has no product evidence.",
                retryable=False,
                suggested_next_step="Use Gradle 8.10 or 8.14 for G4.",
            )
        wrapper = project / ("gradlew.bat" if os.name == "nt" else "gradlew")
        if not wrapper.is_file():
            raise LaunchPipelineFailure(
                "GRADLE_WRAPPER_UNAVAILABLE",
                "The Gradle Wrapper executable is unavailable.",
                retryable=True,
                suggested_next_step="Restore the Gradle Wrapper and retry launch.",
            )
        gradle_args = shlex.split(os.environ.get("GRADLE_ARGS", ""))
        if any(value not in {"-o", "--offline"} for value in gradle_args):
            raise LaunchPipelineFailure(
                "GRADLE_ARGUMENTS_UNSUPPORTED",
                "Only Gradle offline mode is supported through GRADLE_ARGS.",
                retryable=False,
                suggested_next_step="Remove unsupported GRADLE_ARGS and retry.",
            )
        offline = any(value in {"-o", "--offline"} for value in gradle_args)
        prepared_probe = probe.prepare(
            attempt_directory / "gradle-probe", scope="runtime"
        )
        environment = JavaToolchainResolver.maven_environment(build_jdk)
        context.set_build_plan(
            BuildPlan(
                build_system="gradle",
                build_root=project,
                target_module=":",
                build_java_executable=build_jdk.java_executable,
                compile_required=True,
                provider_options={
                    "gradle_version": version,
                    "offline": offline,
                },
            )
        )
        source_roots = ((project / "src/main/java"),)
        resource_roots = ((project / "src/main/resources"),)
        manifest_started = time.monotonic()
        source_before = self.source_manifest_fingerprint(source_roots)
        resource_before = build_input_manifest((), resource_roots)
        source_before_ms = (time.monotonic() - manifest_started) * 1000
        context.transition(LaunchPhase.COMPILING)
        try:
            operation = context.run_operation(
                BuildOperationSpec(
                    argv=probe.command(
                        wrapper=wrapper,
                        prepared=prepared_probe,
                        offline=offline,
                    ),
                    cwd=project,
                    environment=environment,
                    timeout_seconds=600.0,
                    output_capture=build_log,
                    max_output_bytes=16 * 1024 * 1024,
                    operation_name="gradle_runtime_bootstrap",
                )
            )
            if operation.timed_out:
                raise LaunchPipelineFailure(
                    LaunchErrorCode.BUILD_TIMEOUT,
                    "The supervised Gradle Runtime Bootstrap timed out.",
                    retryable=True,
                    suggested_next_step="Inspect build.log_tail and retry launch.",
                )
            if not operation.succeeded:
                if prepared_probe.output_file.is_file():
                    probe.load_model(prepared_probe)
                raise LaunchPipelineFailure(
                    LaunchErrorCode.BUILD_FAILED,
                    "The supervised Gradle Runtime Bootstrap failed.",
                    retryable=True,
                    suggested_next_step="Inspect build.log_tail and retry launch.",
                    context={"return_code": operation.return_code},
                )
            model = probe.load_model(prepared_probe)
        finally:
            probe.cleanup(prepared_probe)
        manifest_started = time.monotonic()
        source_after = self.source_manifest_fingerprint(source_roots)
        resource_after = build_input_manifest((), resource_roots)
        source_after_ms = (time.monotonic() - manifest_started) * 1000
        if source_before != source_after:
            raise LaunchPipelineFailure(
                LaunchErrorCode.SOURCE_CHANGED_DURING_BUILD,
                "Project Java sources changed while Gradle was building.",
                retryable=True,
                suggested_next_step="Wait for source edits to settle and retry.",
            )
        if resource_before != resource_after:
            raise LaunchPipelineFailure(
                LaunchErrorCode.SOURCE_CHANGED_DURING_BUILD,
                "Project resources changed while Gradle was building.",
                retryable=True,
                suggested_next_step=(
                    "Wait for resource edits to settle and retry launch."
                ),
            )
        configuration_inputs, environment_names = self._gradle_inputs(project)
        try:
            world = create_gradle_runtime_build_world(
                model=model,
                project_root=project,
                configuration_inputs=configuration_inputs,
                configuration_environment_names=environment_names,
            )
        except GradleRuntimeBuildWorldError as error:
            raise LaunchPipelineFailure(
                error.error_code,
                str(error),
                retryable=False,
                suggested_next_step="Use the formal Gradle launch for this project.",
            ) from error
        runtime_major = runtime_jdk.major_version
        if runtime_major is not None and runtime_major < world.jdt_plan.target_level:
            raise LaunchPipelineFailure(
                LaunchErrorCode.JAVA_TOOLCHAIN_NOT_FOUND,
                "The selected Runtime JDK is older than Gradle compile target.",
                retryable=True,
                suggested_next_step="Select a compatible IDEA Runtime JDK.",
            )
        plan = JvmLaunchPlan(
            java_executable=runtime_jdk.java_executable,
            classpath=world.runtime_classpath,
            main_class=imported.intent.main_class,
            working_directory=imported.intent.working_directory,
            jvm_args=imported.intent.jvm_args,
            program_args=imported.intent.program_args,
            environment_overrides=imported.intent.environment,
            ready_port=request.ready_port,
            startup_wait_timeout_seconds=request.startup_wait_timeout_seconds,
        )
        plan, command = self.materialize_command(
            plan,
            jdwp_port=request.jdwp_port,
            attempt_directory=attempt_directory,
        )
        context.set_jvm_launch_plan(plan)
        context.transition(LaunchPhase.RESOLVING_RUNTIME)
        return PreparedProjectLaunch(
            execution=None,
            build_system="gradle",
            build_offline=offline,
            build_jdk=build_jdk,
            module_output=world.module_output,
            generation_input_roots=world.generation_input_roots,
            generation_input_manifest=world.generation_input_manifest,
            resource_source_roots=world.resource_source_roots,
            resource_input_manifest=resource_after,
            build_world_inputs=world.configuration_inputs,
            runtime_jdk=runtime_jdk,
            jvm_plan=plan,
            command=command,
            warnings=tuple(dict.fromkeys((*imported.warnings, *preferences.warnings))),
            attempt_directory=attempt_directory,
            fast_compile_plan=None,
            fast_compile_unavailable_reason="DIRECT_RELOAD_NOT_USED",
            jdt_build_world_plan=world.jdt_plan,
            source_manifest_fingerprint=source_after,
            source_manifest_before_ms=source_before_ms,
            source_manifest_after_ms=source_after_ms,
        )

    @staticmethod
    def _gradle_inputs(project: Path) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        inputs = list(gradle_configuration_inputs(project))
        resources = project / "src/main/resources"
        if resources.is_dir():
            inputs.extend(path for path in resources.rglob("*") if path.is_file())
        return (
            tuple(dict.fromkeys(path.resolve(strict=False) for path in inputs)),
            gradle_configuration_environment_names(),
        )

    def materialize_command(
        self,
        plan: JvmLaunchPlan,
        *,
        jdwp_port: int,
        attempt_directory: Path,
    ) -> tuple[JvmLaunchPlan, MaterializedJavaCommand]:
        """Materialize a resolved plan after Generation classpath rewriting."""

        command = self._commands.materialize(
            plan,
            jdwp_port=jdwp_port,
            attempt_directory=attempt_directory,
        )
        return (
            replace(
                plan,
                command_materialization=command.materialization,
            ),
            command,
        )

    @staticmethod
    def source_manifest_fingerprint(
        source_roots: tuple[Path, ...],
    ) -> str:
        digest = hashlib.sha256()
        source_count = 0
        for index, source_root in enumerate(source_roots):
            root = source_root.expanduser().resolve(strict=False)
            if not root.is_dir():
                continue
            for source in sorted(root.rglob("*.java")):
                if source.is_symlink():
                    raise LaunchPipelineFailure(
                        "SOURCE_LINK_UNSUPPORTED",
                        "Project Java source roots may not contain links.",
                        retryable=False,
                        suggested_next_step=(
                            "Replace linked Java sources with regular files."
                        ),
                    )
                source_count += 1
                if source_count > 50_000:
                    raise LaunchPipelineFailure(
                        "SOURCE_MANIFEST_LIMIT_EXCEEDED",
                        "Project Java source manifest exceeds the safety limit.",
                        retryable=False,
                        suggested_next_step=(
                            "Launch a smaller Maven module or use the formal "
                            "runtime workflow without persistent reload."
                        ),
                    )
                digest.update(str(index).encode("ascii"))
                digest.update(b"\0")
                digest.update(source.relative_to(root).as_posix().encode("utf-8"))
                digest.update(b"\0")
                try:
                    digest.update(source.read_bytes())
                except OSError as error:
                    raise LaunchPipelineFailure(
                        "SOURCE_MANIFEST_UNAVAILABLE",
                        "A project Java source could not be read consistently.",
                        retryable=True,
                        suggested_next_step=(
                            "Wait for source edits or file operations to finish, "
                            "then call launch again."
                        ),
                    ) from error
                digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def cleanup_attempt_directory(directory: Path | None) -> None:
        if directory is None:
            return
        try:
            shutil.rmtree(directory)
        except OSError:
            pass

    def _select_java(
        self,
        context: LaunchContext,
        *,
        preferences: IdeaBuildPreferences,
        explicit_reference: str | None,
        for_build: bool,
        cwd: Path,
        build_log: Path,
        already_probed: JavaToolchainCandidate | None = None,
    ) -> JavaToolchainCandidate:
        candidates = self._java.candidates(
            preferences=preferences,
            explicit_reference=explicit_reference,
            for_build=for_build,
        )
        if (
            already_probed is not None
            and any(
                candidate.java_executable == already_probed.java_executable
                for candidate in candidates
            )
            and (not for_build or already_probed.has_compiler)
        ):
            return already_probed
        for candidate in candidates:
            if not candidate.has_runtime:
                continue
            if for_build and not candidate.has_compiler:
                continue
            log_offset = self._file_size(build_log)
            result = context.run_operation(
                self._java.probe_spec(
                    candidate,
                    cwd=cwd,
                    output_capture=build_log,
                    operation_name=(
                        "build_java_probe" if for_build else "runtime_java_probe"
                    ),
                )
            )
            if result.succeeded:
                detected_major = (
                    self._read_probed_java_major(
                        build_log,
                        offset=log_offset,
                    )
                    or candidate.major_version
                )
                detected_compiler_major: int | None = None
                if for_build:
                    compiler_log_offset = self._file_size(build_log)
                    compiler_result = context.run_operation(
                        self._java.compiler_probe_spec(
                            candidate,
                            cwd=cwd,
                            output_capture=build_log,
                            operation_name="build_javac_probe",
                        )
                    )
                    if not compiler_result.succeeded:
                        continue
                    detected_compiler_major = (
                        self._read_probed_javac_major(
                            build_log,
                            offset=compiler_log_offset,
                        )
                        or candidate.compiler_major_version
                    )
                return replace(
                    candidate,
                    detected_major_version=detected_major,
                    detected_compiler_major_version=(detected_compiler_major),
                )
        role = "build" if for_build else "runtime"
        raise LaunchPipelineFailure(
            LaunchErrorCode.JAVA_TOOLCHAIN_NOT_FOUND,
            f"No usable {role} Java toolchain matched the IDEA intent.",
            retryable=True,
            suggested_next_step=(
                "Configure the referenced IDEA JDK on this machine, or "
                "correct JAVA_HOME/PATH when the project has no IDEA JDK."
            ),
            context={
                "toolchain_role": role,
                "configured_reference": (
                    preferences.maven_runner_jdk_name
                    if for_build
                    else (explicit_reference or preferences.project_jdk_name)
                ),
            },
        )

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _read_probed_java_major(
        path: Path,
        *,
        offset: int,
    ) -> int | None:
        try:
            with path.open("rb") as stream:
                stream.seek(max(0, offset))
                output = stream.read(64 * 1024).decode(
                    "utf-8",
                    errors="replace",
                )
        except OSError:
            return None
        return JavaToolchainCandidate.parse_major_version_output(output)

    @staticmethod
    def _read_probed_javac_major(
        path: Path,
        *,
        offset: int,
    ) -> int | None:
        try:
            with path.open("rb") as stream:
                stream.seek(max(0, offset))
                output = stream.read(64 * 1024).decode(
                    "utf-8",
                    errors="replace",
                )
        except OSError:
            return None
        return JavaToolchainCandidate.parse_compiler_major_version_output(output)

    def _select_maven(
        self,
        context: LaunchContext,
        *,
        project_root: Path,
        preferences: IdeaBuildPreferences,
        build_jdk: JavaToolchainCandidate,
        build_log: Path,
    ) -> MavenToolCandidate:
        environment = self._java.maven_environment(build_jdk)
        for candidate in self._maven_tools.candidates(
            project_root=project_root,
            preferences=preferences,
        ):
            result = context.run_operation(
                self._maven_tools.probe_spec(
                    candidate,
                    cwd=project_root,
                    environment=environment,
                    output_capture=build_log,
                )
            )
            if result.succeeded:
                return candidate
        raise LaunchPipelineFailure(
            LaunchErrorCode.MAVEN_NOT_FOUND,
            "No usable Maven installation matched the project environment.",
            retryable=True,
            suggested_next_step=(
                "Restore the IDEA Maven installation or Maven wrapper, or "
                "configure Maven on PATH and retry run."
            ),
        )

    @staticmethod
    def _idea_failure(
        error: IdeaLaunchImportError,
    ) -> LaunchPipelineFailure:
        payload = error.to_payload()
        suggested = str(
            payload.pop(
                "suggested_next_step",
                "Correct the IDEA launch configuration and retry run.",
            )
        )
        for key in ("ok", "error", "error_code", "retryable"):
            payload.pop(key, None)
        return LaunchPipelineFailure(
            error.error_code,
            str(error),
            retryable=error.retryable,
            suggested_next_step=suggested,
            context=payload,
        )

    @staticmethod
    def _maven_failure(
        error: MavenResolutionError,
    ) -> LaunchPipelineFailure:
        return LaunchPipelineFailure(
            error.error_code,
            str(error),
            retryable=error.retryable,
            suggested_next_step=error.suggested_next_step,
            context=error.context,
        )


__all__ = [
    "PreparedProjectLaunch",
    "ProjectLaunchPipeline",
    "ProjectLaunchRequest",
]
