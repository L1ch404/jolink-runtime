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
    LaunchIntent,
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
from .gradle_probe import (
    GradleProbeError,
    ProductGradleProbe,
    gradle_configuration_environment_names,
    gradle_configuration_inputs,
    wrapper_version,
    gradle_build_root,
)
from .gradle_runtime_build_world import (
    GradleRuntimeBuildWorldError,
    create_gradle_runtime_build_world,
)
from .gradle_module_world import GradleModuleError
from .jdt_compile_session import JdtBuildWorldPlan
from .jdt_modules import compilation_modules
from .maven_probe import ProductMavenProbe
from .maven_module_world import load_module_worlds
from .project_launch_cache import (
    ProjectLaunchCache,
    stabilize_jdt_plan,
)
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
    build_system: str = ""


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
    jdt_build_world_plan: JdtBuildWorldPlan | None = None
    jdt_unavailable_reason: str | None = None
    source_manifest_fingerprint: str | None = None
    source_manifest_before_ms: float | None = None
    source_manifest_after_ms: float | None = None
    probe_cache_reused: bool = False
    launch_intent: LaunchIntent | None = None
    build_preferences_identity: dict[str, Any] | None = None


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
        launch_cache: ProjectLaunchCache | None = None,
    ) -> None:
        self._idea_launches = idea_launches or IdeaLaunchImporter()
        self._idea_environment = (
            idea_environment or IdeaEnvironmentImporter()
        )
        self._maven = maven_adapter or MavenBuildSystemAdapter()
        self._java = java_toolchains or JavaToolchainResolver()
        self._maven_tools = maven_tools or MavenToolResolver()
        self._commands = command_materializer or JavaCommandMaterializer()
        self._launch_cache = launch_cache or ProjectLaunchCache()

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

        preferences_identity = None
        has_maven = (request.project_path / "pom.xml").is_file()
        has_gradle = gradle_build_root(request.project_path) is not None
        if has_maven and has_gradle and not request.build_system:
            raise LaunchPipelineFailure(
                "BUILD_SYSTEM_AMBIGUOUS",
                "The project contains both supported Maven and Gradle builds.",
                retryable=False,
                suggested_next_step=(
                    "Specify build_system='maven' or 'gradle'."
                ),
            )
        build_system = request.build_system or ("gradle" if has_gradle else "maven")
        available = has_gradle if build_system == "gradle" else has_maven
        if request.build_system and not available:
            raise LaunchPipelineFailure(
                "BUILD_SYSTEM_NOT_FOUND", "The selected build system is not present.",
                retryable=True, suggested_next_step="Check project_path and build_system.",
            )
        cached = self._launch_cache.load(
            project_root=request.project_path,
            intent=imported.intent,
            build_system=build_system,
            ready_port=request.ready_port,
            startup_wait_timeout_seconds=(
                request.startup_wait_timeout_seconds
            ),
            build_preferences=preferences_identity,
        )
        if cached is not None:
            context.set_build_plan(
                BuildPlan(
                    build_system=cached.build_system,
                    build_root=cached.jdt_plan.project_root,
                    target_module=str(cached.jdt_plan.module_root),
                    build_java_executable=cached.build_jdk.java_executable,
                    compile_required=False,
                    provider_options={"probe_cache_reused": True},
                )
            )
            context.transition(LaunchPhase.RESOLVING_RUNTIME)
            plan, command = self.materialize_command(
                cached.jvm_plan,
                jdwp_port=request.jdwp_port,
                attempt_directory=attempt_directory,
            )
            context.set_jvm_launch_plan(plan)
            return PreparedProjectLaunch(
                execution=None,
                build_system=cached.build_system,
                build_offline=cached.build_offline,
                build_jdk=cached.build_jdk,
                module_output=cached.module_output,
                generation_input_roots=cached.generation_input_roots,
                generation_input_manifest={},
                resource_source_roots=cached.resource_source_roots,
                resource_input_manifest={},
                build_world_inputs=cached.jdt_plan.configuration_inputs,
                runtime_jdk=cached.runtime_jdk,
                jvm_plan=plan,
                command=command,
                warnings=(
                    "Reused the persisted Probe Build World; the build tool "
                    "was not invoked.",
                ),
                attempt_directory=attempt_directory,
                jdt_build_world_plan=cached.jdt_plan,
                source_manifest_fingerprint=None,
                probe_cache_reused=True,
                launch_intent=imported.intent,
                build_preferences_identity=preferences_identity,
            )
        preferences = self._idea_environment.import_preferences(
            request.project_path
        )
        preferences_identity = preferences.redacted_summary()
        preferences_identity.pop("warnings", None)
        preferences_identity["jdk_homes_by_name"] = {
            name: [str(path) for path in paths]
            for name, paths in preferences.jdk_homes_by_name.items()
        }
        if build_system == "gradle":
            try:
                prepared = self._prepare_gradle(
                    context,
                    request,
                    imported=imported,
                    preferences=preferences,
                    attempt_directory=attempt_directory,
                )
                return self._stabilize(replace(
                    prepared,
                    build_preferences_identity=preferences_identity,
                ))
            except GradleProbeError as error:
                raise LaunchPipelineFailure(
                    error.error_code,
                    str(error),
                    retryable=False,
                    suggested_next_step=(
                        "Correct the Gradle Wrapper/Probe input and retry launch."
                    ),
                ) from error
        standalone_module_warning: tuple[str, ...] = ()
        try:
            (
                workspace,
                module,
                standalone_module_warning,
            ) = self._resolve_maven_workspace_and_module(
                request.project_path,
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
        prepared = self._prepare_maven_probe(
            context, request, imported.intent, preferences, workspace, module,
            build_jdk, runtime_jdk, maven, attempt_directory, build_log,
        )
        return self._stabilize(replace(
            prepared,
            warnings=tuple(dict.fromkeys((*imported.warnings, *preferences.warnings,
                                          *standalone_module_warning))),
            build_preferences_identity=preferences_identity,
        ))

    def _prepare_maven_probe(self, context, request, intent, preferences, workspace,
                               module, build_jdk, runtime_jdk, maven, directory, log):
        import json
        probe = ProductMavenProbe.load()
        local_repository = preferences.local_repository or Path.home() / ".m2/repository"
        settings = preferences.user_settings_file
        settings_input = settings if settings is not None else Path.home() / ".m2/settings.xml"
        if settings is None and settings_input.is_file():
            settings = settings_input
        offline = "-o" in shlex.split(os.environ.get("MAVEN_ARGS", "")) or "--offline" in shlex.split(os.environ.get("MAVEN_ARGS", ""))
        prepared = probe.prepare(attempt_directory=directory / "probe",
            source_settings=settings, local_repository=local_repository, offline=offline)
        command = [*maven.argv_prefix, "--batch-mode", "-f", str(workspace.root_pom),
            "-s", str(prepared.settings_file), f"-Dmaven.repo.local={local_repository}",
            prepared.goal.replace("export-build-world", "export-reactor-world"),
            f"-Djolink.probe.targetDirectory={module.directory}",
            f"-Djolink.probe.outputDirectory={prepared.output_directory}", "-Djolink.probe.scope=runtime"]
        if offline: command.append("--offline")
        if preferences.active_profiles: command.extend(["-P", ",".join(preferences.active_profiles)])
        context.set_build_plan(BuildPlan(build_system="maven", build_root=workspace.build_root,
            target_module=module.relative_path, build_java_executable=build_jdk.java_executable,
            compile_required=False))
        context.transition(LaunchPhase.RESOLVING_RUNTIME)
        try:
            result = context.run_operation(BuildOperationSpec(argv=tuple(command),
                cwd=workspace.build_root, environment=self._java.maven_environment(build_jdk),
                timeout_seconds=900, output_capture=log, operation_name="maven_module_probe"))
            if not result.succeeded:
                raise LaunchPipelineFailure("MAVEN_MODULE_PROBE_FAILED", "Maven module export failed.",
                    retryable=True, suggested_next_step="Inspect build.log_tail.")
            modules = load_module_worlds(prepared.output_directory, module.directory, build_jdk, tests=False)
            target = next(m for m in modules if Path(m["module_root"]) == module.directory)
            snapshot = probe.load_snapshot(prepared, module_root=module.directory)
        finally:
            prepared.settings_file.unlink(missing_ok=True)
        roots = tuple(Path(p) for m in modules for p in m["source_roots"])
        resources = tuple(Path(p) for p in target["resource_roots"])
        plan = JdtBuildWorldPlan(project_root=request.project_path, module_root=module.directory,
            source_roots=roots, dependency_entries=tuple(Path(p) for p in target["classpath"]),
            processor_entries=tuple(Path(p) for p in target["processor_entries"]),
            processor_names=tuple(target.get("processor_names", ())),
            processor_options=target.get("processor_options", {}),
            preparation_inputs=tuple(Path(p) for m in modules for p in m.get("preparation_inputs", ())),
            preparation_roots=tuple(Path(p) for m in modules for p in m.get("preparation_roots", ())),
            lombok_entries=tuple(dict.fromkeys(Path(p) for m in modules for p in m["lombok_entries"])),
            target_java_home=Path(target["target_java_home"]), source_encoding=target["source_encoding"],
            source_level=target["source_level"], target_level=target["source_level"],
            fingerprint=hashlib.sha256(json.dumps(compilation_modules(modules), sort_keys=True).encode()).hexdigest(),
            configuration_inputs=tuple(dict.fromkeys((
                *(m.pom_file for m in workspace.modules),
                settings_input,
                *(workspace.build_root / ".mvn" / name for name in
                  ("maven.config", "jvm.config", "extensions.xml")),
            ))),
            configuration_environment_names=(), javac_executable=build_jdk.javac_executable,
            method_parameters=target["method_parameters"],
            worker_min_heap_mb=max(m["worker_min_heap_mb"] for m in modules),
            worker_max_heap_mb=max(m["worker_max_heap_mb"] for m in modules),
            modules=modules if len(modules) > 1 else ())
        jvm = JvmLaunchPlan(java_executable=runtime_jdk.java_executable,
            classpath=tuple(Path(p) for p in snapshot["runtimeClasspathElements"]),
            main_class=intent.main_class, working_directory=intent.working_directory,
            jvm_args=intent.jvm_args, program_args=intent.program_args,
            environment_overrides=dict(intent.environment), ready_port=request.ready_port,
            startup_wait_timeout_seconds=request.startup_wait_timeout_seconds)
        jvm, materialized = self.materialize_command(jvm, jdwp_port=request.jdwp_port, attempt_directory=directory)
        return PreparedProjectLaunch(execution=None, build_system="maven", build_offline=offline,
            build_jdk=build_jdk, runtime_jdk=runtime_jdk, module_output=Path(target["output_directory"]),
            generation_input_roots=(Path(target["output_directory"]),), generation_input_manifest={},
            resource_source_roots=resources, resource_input_manifest={},
            build_world_inputs=plan.configuration_inputs, jvm_plan=jvm, command=materialized,
            warnings=(), attempt_directory=directory, jdt_build_world_plan=plan, launch_intent=intent)

    @staticmethod
    def _stabilize(prepared: PreparedProjectLaunch) -> PreparedProjectLaunch:
        from .configuration_inputs import maven_configuration_files, build_configuration_stamps

        plan = prepared.jdt_build_world_plan
        if plan is None:
            return prepared
        if prepared.build_system == "maven":
            plan = replace(
                plan,
                configuration_inputs=maven_configuration_files(
                    plan.project_root, plan.configuration_inputs
                ),
            )
        stable = stabilize_jdt_plan(
            plan,
            attempt_directory=prepared.attempt_directory,
        )
        stable = replace(stable, configuration_stamps=build_configuration_stamps(
            stable.project_root, prepared.build_system, stable.configuration_inputs
        ))
        return replace(
            prepared,
            jdt_build_world_plan=stable,
            build_world_inputs=stable.configuration_inputs,
        )

    def save_cache(
        self,
        prepared: PreparedProjectLaunch,
        request: ProjectLaunchRequest,
    ) -> None:
        plan = prepared.jdt_build_world_plan
        if plan is None:
            return
        intent = prepared.launch_intent
        if intent is None:
            return
        self._launch_cache.save(
            project_root=request.project_path,
            intent=intent,
            build_system=prepared.build_system,
            build_offline=prepared.build_offline,
            build_jdk=prepared.build_jdk,
            runtime_jdk=prepared.runtime_jdk,
            module_output=prepared.module_output,
            generation_input_roots=prepared.generation_input_roots,
            resource_source_roots=prepared.resource_source_roots,
            jvm_plan=prepared.jvm_plan,
            jdt_plan=plan,
            build_preferences=prepared.build_preferences_identity,
        )

    def _resolve_maven_workspace_and_module(
        self,
        project_root: Path,
        intent: Any,
    ) -> tuple[Any, Any, tuple[str, ...]]:
        workspace = self._maven.resolve_workspace(project_root)
        try:
            return (
                workspace,
                self._maven.select_module(workspace, intent),
                (),
            )
        except MavenResolutionError as error:
            if error.error_code is not LaunchErrorCode.BUILD_MODULE_NOT_FOUND:
                raise

        root = project_root.expanduser().resolve(strict=True)
        candidates: list[Path] = []
        module_name = str(getattr(intent, "ide_module_name", "") or "")
        if module_name and Path(module_name).name == module_name:
            exact = root / module_name
            if (exact / "pom.xml").is_file() and not exact.is_symlink():
                candidates.append(exact)
        source_relative = (
            Path("src/main/java")
            / Path(*str(intent.main_class).split("."))
        ).with_suffix(".java")
        for pom in sorted(root.glob("*/pom.xml")):
            directory = pom.parent
            if (
                directory not in candidates
                and not directory.is_symlink()
                and (directory / source_relative).is_file()
            ):
                candidates.append(directory)
        if len(candidates) != 1:
            raise MavenResolutionError(
                LaunchErrorCode.BUILD_MODULE_NOT_FOUND,
                "No unique standalone Maven module contains the selected main class.",
                retryable=True,
                suggested_next_step=(
                    "Choose an IDEA launch whose module maps to one direct "
                    "child Maven project."
                ),
                context={
                    "ide_module_name": module_name,
                    "main_class": intent.main_class,
                    "candidate_count": len(candidates),
                },
            )
        standalone = self._maven.resolve_workspace(candidates[0])
        module = self._maven.select_module(standalone, intent)
        return (
            standalone,
            module,
            (
                "The IDEA module is outside the parent Maven reactor; joLink "
                "is using the module POM as the standalone build authority.",
            ),
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
        project = gradle_build_root(request.project_path) or request.project_path
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
        context.transition(LaunchPhase.RESOLVING_RUNTIME)
        try:
            operation = context.run_operation(
                BuildOperationSpec(
                    argv=probe.command(
                        wrapper=wrapper,
                        prepared=prepared_probe,
                        offline=offline,
                        main_class=imported.intent.main_class,
                        target_directory=request.project_path,
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
                project_root=request.project_path,
                configuration_inputs=configuration_inputs,
                configuration_environment_names=environment_names,
            )
        except (GradleRuntimeBuildWorldError, GradleModuleError) as error:
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
        return PreparedProjectLaunch(
            execution=None,
            build_system="gradle",
            build_offline=offline,
            build_jdk=build_jdk,
            module_output=world.module_output,
            generation_input_roots=world.generation_input_roots,
            generation_input_manifest={},
            resource_source_roots=world.resource_source_roots,
            resource_input_manifest=resource_after,
            build_world_inputs=world.configuration_inputs,
            runtime_jdk=runtime_jdk,
            jvm_plan=plan,
            command=command,
            warnings=tuple(dict.fromkeys((*imported.warnings, *preferences.warnings))),
            attempt_directory=attempt_directory,
            jdt_build_world_plan=world.jdt_plan,
            source_manifest_fingerprint=source_after,
            source_manifest_before_ms=source_before_ms,
            source_manifest_after_ms=source_after_ms,
            launch_intent=imported.intent,
        )

    @staticmethod
    def _gradle_inputs(project: Path) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        inputs = list(gradle_configuration_inputs(project))
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
