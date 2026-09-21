"""Compile-aware project restart, reusing the live JDT session and JVM launcher."""

from __future__ import annotations

import os
import time
from dataclasses import replace
from typing import Any

from ..core.models import RuntimeResult
from ..adapters.java.process_manager import ProcessStartCancelledError, ProcessStartupError
from .contracts import LaunchErrorCode, LaunchPhase, RuntimeProcessState
from .project_launcher import ProjectLaunchRequest
from .controller import LaunchCancelled, LaunchContext, LaunchControlError, LaunchPipelineFailure
from .jdt_compile_session import JdtCompileError, PersistentJdtCompileSession
from .jdt_launch_service import JdtLaunchService
from .project_session import JavaProjectSession, ProjectSessionError
from .startup_timing import StartupTimings


def start_project_restart(runtime, action):
    snapshot = runtime._launch_controller.snapshot()
    attempt_id = snapshot.get("attempt_id")
    with runtime._project_state_lock:
        prepared = runtime._project_update_plans.get(attempt_id)
        session = runtime._project_sessions.get(attempt_id)
    # A stopped/unavailable session or changed build model uses the existing
    # launch path, including its Probe cache refresh and persistent workspace.
    if (prepared is None or session is None or not session.refresh_compile_ready()
            or JdtLaunchService.configuration_rejection(prepared) is not None):
        result = runtime.restart_project(action, runtime._last_project_request)
        result.data["apply_method"] = "restart"
        return result
    return runtime._jdt_reload_service.start(
        runtime, action, attempt_id=attempt_id,
        generation=snapshot["generation"], prepared=prepared,
        project_session=session, restart=True,
    )


def restart_compiled(runtime, action, *, attempt_id, prepared, session):
    """Replace the JVM only; the caller has already compiled its source delta."""
    request = prepared.request
    if action.ready_port > 0:
        request = replace(request, ready_port=action.ready_port,
                          startup_wait_timeout_seconds=action.startup_wait_timeout_seconds)
    if "jdwp_port" in getattr(action, "_launch_overrides", ()):
        request = replace(request, jdwp_port=action.jdwp_port)
    plan = prepared.jvm_plan
    if action.app_args is not None:
        plan = replace(plan, program_args=tuple(action.app_args))
    if action.vm_args is not None:
        plan = replace(plan, jvm_args=tuple(action.vm_args))
    prepared = replace(prepared, jvm_plan=plan, request=request)
    with runtime._project_state_lock:
        runtime._preserve_project_sessions.add(attempt_id)
    try:
        started = runtime._launch_controller.restart(
            lambda context: _launch_compiled(
                runtime, context, request=request, retained=prepared, session=session),
            deadline=time.monotonic() + 5.0,
        )
    except LaunchControlError as error:
        with runtime._project_state_lock:
            runtime._preserve_project_sessions.discard(attempt_id)
        return runtime._launch_control_result(error)
    runtime._last_project_request = request
    return RuntimeResult(ok=True, data={
        **started, "status": "restarting", "applied": None, "apply_method": "restart",
    })


def _launch_compiled(
    runtime,
    context: LaunchContext,
    *,
    request: ProjectLaunchRequest,
    retained: Any,
    session: JavaProjectSession,
) -> None:
    attempt_directory = runtime._project_pipeline.create_attempt_directory(
        context.attempt_id
    )
    process = None
    runtime_active = False
    with runtime._project_state_lock:
        runtime._project_attempt_directories[context.attempt_id] = (
            attempt_directory
        )
        runtime._project_sessions[context.attempt_id] = session
    session.retain_directory(attempt_directory)
    try:
        context.transition(LaunchPhase.RESOLVING_BUILD)
        context.transition(LaunchPhase.RESOLVING_RUNTIME)
        current = session.generations.current
        if current is None:
            raise ProjectSessionError(
                "CURRENT_GENERATION_UNAVAILABLE",
                "The requested generation disappeared before restart.",
            )
        session.generations.verify_generation(current)
        classpath = list(retained.jvm_plan.classpath)
        classpath[retained.generation_classpath_index] = (
            current.output_directory
        )
        plan = replace(retained.jvm_plan, classpath=tuple(classpath))
        plan, command = runtime._project_pipeline.materialize_command(
            plan,
            jdwp_port=request.jdwp_port,
            attempt_directory=attempt_directory,
        )
        context.set_jvm_launch_plan(plan)
        with runtime._project_state_lock:
            runtime._project_update_plans[context.attempt_id] = replace(
                retained,
                attempt_directory=attempt_directory,
                project_session=session,
                jvm_plan=plan,
                request=request,
            )
        context.transition(LaunchPhase.STARTING_JVM)
        context.check_cancelled()
        runtime._reset_debug_state()
        runtime._host = "127.0.0.1"
        log_file = runtime._log.create(context.attempt_id)
        def start_target(
            selected_plan: Any,
            selected_command: Any,
        ) -> tuple[Any, float]:
            started = time.monotonic()
            selected_process = runtime._proc.start(
                classpath=os.pathsep.join(
                    str(path) for path in selected_plan.classpath
                ),
                main_class=selected_plan.main_class,
                app_args=list(selected_plan.program_args),
                jdwp_port=request.jdwp_port,
                vm_args=list(selected_plan.jvm_args),
                log_file=log_file,
                ready_port=request.ready_port,
                startup_wait_timeout_seconds=(
                    request.startup_wait_timeout_seconds
                ),
                readiness_config_source=(
                    "explicit" if request.ready_port else "not_configured"
                ),
                java_executable=str(selected_plan.java_executable),
                working_directory=selected_plan.working_directory,
                environment_overrides=selected_plan.environment_overrides,
                should_stop=lambda: context.cancel_event.is_set(),
                on_published=lambda item: runtime._publish_project_process(
                    context, item
                ),
                command_argv=selected_command.argv,
                retained_files=selected_command.retained_files,
                startup_timing_key=StartupTimings._project_key(request),
            )
            context.check_cancelled()
            readiness = runtime._proc.observe_readiness(selected_process)
            context.set_process_observation(
                process_state=RuntimeProcessState.RUNNING,
                startup_state=str(readiness["startup_state"]),
            )
            if request.ready_port <= 0:
                return selected_process, (time.monotonic() - started) * 1000
            if context.phase is LaunchPhase.STARTING_JVM:
                context.transition(LaunchPhase.WAITING_READINESS)
            deadline = (
                time.monotonic() + request.startup_wait_timeout_seconds
            )
            timeout_marked = False
            while True:
                context.check_cancelled()
                readiness = runtime._proc.observe_readiness(selected_process)
                startup_state = str(readiness["startup_state"])
                process_state = str(
                    readiness.get("process_state", "running")
                )
                context.set_process_observation(
                    process_state=(
                        RuntimeProcessState.RUNNING
                        if process_state == "running"
                        else RuntimeProcessState.EXITED
                    ),
                    startup_state=startup_state,
                )
                if startup_state == "ready":
                    return (
                        selected_process,
                        (time.monotonic() - started) * 1000,
                    )
                if startup_state == "failed" or process_state == "exited":
                    raise LaunchPipelineFailure(
                        LaunchErrorCode.JVM_START_FAILED,
                        "The selected generation failed during restart.",
                        retryable=True,
                        suggested_next_step="Inspect Runtime logs.",
                    )
                if not timeout_marked and time.monotonic() >= deadline:
                    selected_process.mark_startup_wait_timed_out()
                    timeout_marked = True
                context.cancel_event.wait(0.2)

        process, startup_ms = start_target(plan, command)
        context.check_cancelled()
        session.generations.mark_runtime_current()
        compiler = session.compile_session
        if isinstance(compiler, PersistentJdtCompileSession):
            try:
                compiler.reset_publication_baseline(
                    current.output_directory
                )
            except (JdtCompileError, OSError):
                runtime._invalidate_jdt_compile_session(
                    context.attempt_id,
                    retained,
                    session,
                    compiler,
                    reason="JDT_RUNTIME_BASELINE_RESET_FAILED",
                )
        session.record_successful_startup(startup_ms)
        context.transition(LaunchPhase.RUNTIME_ACTIVE)
        runtime_active = True
        return
    except ProcessStartCancelledError as error:
        raise LaunchCancelled(str(error)) from error
    except ProcessStartupError as error:
        raise LaunchPipelineFailure(
            LaunchErrorCode.JVM_START_FAILED,
            str(error),
            retryable=True,
            suggested_next_step="Inspect logs before retrying restart.",
            context={
                "failure_type": error.failure_type,
                "cleanup_settled": error.cleanup_settled,
            },
        ) from error
    finally:
        if process is None:
            with runtime._project_state_lock:
                process = runtime._project_processes.get(context.attempt_id)
        if not runtime_active:
            session.generations.mark_runtime_absent()
            if process is not None:
                stop_result = runtime._proc.stop_target(process)
                if (
                    not process.is_alive()
                    and stop_result.get("status")
                    in {"stopped", "not_running"}
                ):
                    with runtime._project_state_lock:
                        runtime._project_processes.pop(
                            context.attempt_id, None
                        )
