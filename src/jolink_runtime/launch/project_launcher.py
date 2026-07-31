"""IDEA-to-Maven project launch preparation for one supervised attempt."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from .controller import (
    LaunchContext,
    LaunchPipelineFailure,
)
from .contracts import JvmLaunchPlan, LaunchErrorCode, LaunchPhase
from .idea_environment import (
    IdeaBuildPreferences,
    IdeaEnvironmentImporter,
)
from .idea_importer import (
    IdeaLaunchImportError,
    IdeaLaunchImporter,
)
from .fast_compile import FastCompilePlan
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
    execution: MavenExecutionPlan
    runtime_jdk: JavaToolchainCandidate
    jvm_plan: JvmLaunchPlan
    command: MaterializedJavaCommand
    warnings: tuple[str, ...]
    attempt_directory: Path
    fast_compile_plan: FastCompilePlan | None = None
    fast_compile_unavailable_reason: str | None = None


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
        command = self._commands.materialize(
            plan,
            jdwp_port=request.jdwp_port,
            attempt_directory=attempt_directory,
        )
        plan = replace(
            plan,
            command_materialization=command.materialization,
        )
        context.set_jvm_launch_plan(plan)
        fast_compile_plan: FastCompilePlan | None = None
        fast_compile_unavailable_reason: str | None = None
        if compile_classpath_result.succeeded:
            try:
                fast_compile_plan = self._maven.consume_fast_compile_plan(
                    execution=execution,
                    runtime_jdk=runtime_jdk,
                )
            except MavenResolutionError as error:
                fast_compile_unavailable_reason = error.error_code.value
        else:
            fast_compile_unavailable_reason = "COMPILE_CLASSPATH_UNAVAILABLE"
        capability_warnings: tuple[str, ...] = ()
        if fast_compile_plan is None:
            capability_warnings = (
                "Fast runtime-only source update is unavailable for this "
                "launch; formal Maven build and restart remain available.",
            )
        return PreparedProjectLaunch(
            execution=execution,
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
        )

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
                candidate.java_executable
                == already_probed.java_executable
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
                        "build_java_probe"
                        if for_build
                        else "runtime_java_probe"
                    ),
                )
            )
            if result.succeeded:
                detected_major = self._read_probed_java_major(
                    build_log,
                    offset=log_offset,
                ) or candidate.major_version
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
                    detected_compiler_major_version=(
                        detected_compiler_major
                    ),
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
                    else (
                        explicit_reference
                        or preferences.project_jdk_name
                    )
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
        return JavaToolchainCandidate.parse_compiler_major_version_output(
            output
        )

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
