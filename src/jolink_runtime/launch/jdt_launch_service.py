"""JDT-first startup using persisted inputs without repeating validation."""

from __future__ import annotations

import logging
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from .controller import LaunchCancelled, LaunchPipelineFailure
from .jdt_compile_session import (
    JdtCandidate, JdtCompileError, PersistentJdtCompileSession,
    discover_target_system_entries, lombok_worker_jvm_arguments,
    select_target_system_home,
)
from .project_session import JavaProjectSession


logger = logging.getLogger(__name__)


class JdtLaunchService:
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
            candidate = JdtCandidate.load_product()
            persist_model = not prepared.probe_cache_reused or plan.worker_java_home is None
            if plan.worker_java_home is None or not plan.system_entries:
                selected = prepared.build_jdk
                major = selected.major_version or prepared.runtime_jdk.major_version or 8
                target_home = select_target_system_home(
                    (
                        plan.target_java_home,
                        prepared.build_jdk.home,
                        prepared.runtime_jdk.home,
                    ),
                    plan.target_level,
                )
                plan = replace(
                    plan,
                    target_java_home=target_home,
                    worker_java_home=selected.home,
                    worker_java_major=major,
                    system_entries=discover_target_system_entries(
                        target_home, plan.target_level
                    ),
                )
                prepared = replace(prepared, jdt_build_world_plan=plan)
            session.record_jdt_worker_runtime(
                java_major=plan.worker_java_major, data_model=64
            )
            workspace = runtime._jdt_workspaces.claim(
                project_root=plan.project_root, module_root=plan.module_root,
                identity={
                    "worker": candidate.root.name,
                    "build_world": plan.fingerprint,
                    "worker_java_home": str(plan.worker_java_home),
                },
            )
            session.attach_jdt_workspace_lease(workspace)
            compiler = PersistentJdtCompileSession(
                root=workspace.root, candidate=candidate,
                worker_java_home=plan.worker_java_home,
                source_roots=plan.source_roots,
                classpath_entries=(*plan.system_entries, *plan.dependency_entries),
                source_encoding=plan.source_encoding,
                source_level=plan.source_level,
                method_parameters=plan.method_parameters,
                processor_entries=plan.processor_entries,
                java_agents=tuple(f"{path}=ECJ" for path in plan.lombok_entries),
                extra_jvm_arguments=lombok_worker_jvm_arguments(
                    plan.worker_java_major, lombok_enabled=bool(plan.lombok_entries)
                ),
                min_heap_mb=plan.worker_min_heap_mb,
                max_heap_mb=plan.worker_max_heap_mb,
                preserve_root_on_close=True,
            )
            session.attach_compile_session(compiler)
            result = compiler.start(reuse_workspace=workspace.reusable, build_on_reuse=False)
            if workspace.reusable:
                changed = compiler.workspace_source_changes()
                if changed:
                    result = compiler.compile(changed)
            context.check_cancelled()
            if not result.compile_ok:
                raise JdtCompileError(
                    "JDT_STARTUP_COMPILE_FAILED", "JDT compilation failed.",
                    context={"error_count": result.error_count,
                             "diagnostics": list(result.diagnostics)},
                )
            compiler.accept_baseline()
            compiler.save_source_index()
            workspace.mark_initialized()
            session.refresh_compile_ready()
            session.complete_jdt_bootstrap(
                (time.monotonic() - started) * 1000,
                reused=workspace.reusable, build_kind=result.actual_build_kind,
            )
            if persist_model:
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
