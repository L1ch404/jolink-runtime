"""JDT-first startup with persisted inputs and small build-configuration snapshots."""

from __future__ import annotations

import logging
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from ..core.models import RuntimeResult
from .configuration_inputs import build_configuration_stamps
from .controller import LaunchCancelled, LaunchPipelineFailure
from .jdt_compile_session import JdtCompileError
from .jdt_compilation import open_compile_session, initialize_compile_session, accept_compile_session
from .project_session import JavaProjectSession
from .runtime_preparation import prepared_runtime
from .idea_environment import IdeaEnvironmentImporter, build_preferences_key
from .build_environment import apply_build_preferences


logger = logging.getLogger(__name__)


class JdtLaunchService:
    @staticmethod
    def configuration_rejection(prepared):
        plan = prepared.jdt_build_world_plan
        if plan is None:
            return None  # Direct/non-JDT runtimes do not own a Probe model.
        try:
            request = getattr(prepared, "request", None)
            preferences = apply_build_preferences(
                IdeaEnvironmentImporter().import_preferences(plan.project_root),
                build_java_home=getattr(request, "build_java_home", None),
                maven_home=getattr(request, "maven_home", None),
                maven_settings=getattr(request, "maven_settings", None),
            )
            saved_preferences = getattr(prepared, "build_preferences_identity", None)
            preferences_match = saved_preferences is None or build_preferences_key(
                saved_preferences, prepared.build_system
            ) == build_preferences_key(
                preferences.cache_values(),
                prepared.build_system,
            )
            current = build_configuration_stamps(
                plan.project_root, prepared.build_system, plan.configuration_inputs
            )
            if preferences_match and plan.configuration_stamps is not None and current == plan.configuration_stamps:
                return None
        except OSError:
            pass
        return RuntimeResult(
            ok=False,
            error="Build configuration changed or its saved baseline is unavailable.",
            data={
                "error_code": "BUILD_CONFIGURATION_CHANGED",
                "build_world_changes_pending": True,
                "applied": False,
                "suggested_next_step": (
                    "Use stop then launch to refresh the build configuration. "
                    "The current JVM has not been stopped; reload/restart cannot apply this change."
                ),
            },
        )

    def prepare(self, runtime, context, request, prepared):
        plan = prepared.jdt_build_world_plan
        if plan is None:
            raise LaunchPipelineFailure(
                "JDT_BUILD_WORLD_UNAVAILABLE", "No JDT Build World is available.",
                retryable=True, suggested_next_step="Inspect the Probe result.",
            )
        session = JavaProjectSession(
            root=Path(tempfile.mkdtemp(prefix=f"jolink-project-{context.attempt_id}-")),
            build_world_fingerprint=plan.fingerprint,
        )
        session.retain_directory(prepared.attempt_directory)
        with runtime._project_state_lock:
            runtime._project_sessions[context.attempt_id] = session
        started = time.monotonic()
        try:
            context.check_cancelled()
            session.begin_jdt_bootstrap()
            candidate, selected = prepared_runtime(
                (prepared.build_jdk.home,), check_request=context.check_cancelled
            )
            plan = replace(plan, worker_java_home=selected.home, worker_java_major=selected.major)
            prepared = replace(prepared, jdt_build_world_plan=plan)
            session.record_jdt_worker_runtime(
                java_major=plan.worker_java_major, data_model=64
            )
            workspace, compiler = open_compile_session(
                plan.compilation_configuration, store=runtime._jdt_workspaces,
                project_root=plan.project_root, module_root=plan.module_root,
                candidate=candidate, worker_java=selected, resource_modules=plan.modules,
            )
            session.attach_jdt_workspace_lease(workspace)
            session.attach_compile_session(compiler)
            result = initialize_compile_session(compiler, reuse_workspace=workspace.reusable)
            context.check_cancelled()
            if not result.compile_ok:
                raise JdtCompileError(
                    "JDT_STARTUP_COMPILE_FAILED", "JDT compilation failed.",
                    context={"error_count": result.error_count,
                             "diagnostics": list(result.diagnostics)},
                )
            accept_compile_session(compiler, workspace)
            session.refresh_compile_ready()
            session.complete_jdt_bootstrap(
                (time.monotonic() - started) * 1000,
                reused=workspace.reusable, build_kind=result.actual_build_kind,
            )
            # Persist the current launch JDK/settings even when compilation was reused.
            try:
                runtime._project_pipeline.save_cache(prepared, request)
            except OSError:
                logger.warning("Build World cache write failed; launch continues.")

            generation = session.generations.prepare_startup(compiler.output_directory)
            old_roots = set(prepared.generation_input_roots)
            classpath = []
            inserted = False
            for entry in prepared.jvm_plan.classpath:
                if entry in old_roots:
                    if not inserted:
                        classpath.extend((generation.output_directory,
                                          *prepared.resource_source_roots))
                        inserted = True
                else:
                    classpath.append(entry)
            if plan.modules:
                classpath = list(compiler.runtime_classpath(classpath))
            plan_for_jvm = replace(prepared.jvm_plan, classpath=tuple(classpath))
            plan_for_jvm, command = runtime._project_pipeline.materialize_command(
                plan_for_jvm, jdwp_port=request.jdwp_port,
                attempt_directory=prepared.attempt_directory,
            )
            context.set_jvm_launch_plan(plan_for_jvm)
            session.record_generation_preparation(
                generation_seal_ms=0,
                source_snapshot_ms=0,
            )
            return replace(prepared, jvm_plan=plan_for_jvm, command=command), session, classpath.index(generation.output_directory)
        except Exception as error:
            with runtime._project_state_lock:
                runtime._project_sessions.pop(context.attempt_id, None)
            session.close(cleanup_retained=False)
            if isinstance(error, (LaunchCancelled, LaunchPipelineFailure)):
                raise
            raise LaunchPipelineFailure(
                getattr(error, "error_code", "JDT_STARTUP_FAILED"), str(error),
                retryable=True, suggested_next_step="Inspect the JDT startup error.",
                context=dict(getattr(error, "context", {}) or {}),
            ) from error
