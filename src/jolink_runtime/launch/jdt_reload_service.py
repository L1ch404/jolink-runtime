"""Background Runtime reload: changed sources, JavaBuilder delta, JDWP apply."""

from __future__ import annotations

import logging
import time
from typing import Any

from ..core.models import RuntimeAction, RuntimeResult
from ..adapters.java.jdwp_client import JDWPCommandOutcomeUnknown, JDWPCommandRejected
from .jdt_compile_session import JdtCompileError, PersistentJdtCompileSession
from .project_session import JavaProjectSession, ProjectSessionError, ReloadStage
from .reload_coordinator import BackgroundReloadCoordinator


class JdtReloadService:
    def __init__(self, logger: logging.Logger) -> None:
        self._coordinator = BackgroundReloadCoordinator(logger)

    def start(
        self, runtime: Any, action: RuntimeAction, *, attempt_id: str,
        generation: int, prepared: Any, project_session: JavaProjectSession,
    ) -> RuntimeResult:
        compiler = project_session.compile_session
        if not isinstance(compiler, PersistentJdtCompileSession) or not compiler.ready:
            return RuntimeResult(ok=False, error="JDT is not running.",
                data={"error_code": "JDT_SESSION_NOT_READY", "applied": False})
        if runtime._active_suspension is not None:
            return RuntimeResult(ok=False, error="Resume before reload.",
                data={"error_code": "ACTIVE_SUSPENSION_EXISTS", "applied": False})
        if runtime._armed_breakpoint_requests or runtime._armed_exception_requests:
            return RuntimeResult(ok=False, error="Finish the active debug wait before reload.",
                data={"error_code": "ACTIVE_DEBUG_REQUESTS_REMAIN", "applied": False})
        try:
            sources = runtime._resolve_jdt_reload_sources(
                prepared.jdt_build_world_plan, getattr(action, "source_files", None)
            )
            attempt = project_session.begin_reload(
                "", source_files=sources, background=True
            )
        except (JdtCompileError, OSError, ProjectSessionError) as error:
            return RuntimeResult(ok=False, error=str(error), data={
                "error_code": getattr(error, "error_code", "JDT_RELOAD_FAILED"),
                "applied": False,
            })
        self._coordinator.start(
            project_session=project_session,
            reload_attempt=attempt,
            source_fingerprint="",
            update_lock=runtime._update_lock,
            operation=lambda: self.apply(runtime, compiler, prepared,
                                         project_session, attempt, sources),
        )
        return RuntimeResult(ok=True, data={
            "status": "reload_started", "reload_id": attempt.attempt_id,
            "applied": None,
            "suggested_next_step": "Call status to observe active_operation and last_reload.",
        })

    def apply(self, runtime, compiler, prepared, session, attempt, sources):
        session.transition_reload(ReloadStage.COMPILING)
        started = time.monotonic()
        try:
            result = compiler.compile(sources)
        except JdtCompileError as error:
            if not compiler.ready:
                compiler.close()
                session.clear_compile_session(compiler)
                session.fail_jdt_bootstrap(reason=error.error_code)
            raise
        compile_total_ms = round((time.monotonic() - started) * 1000, 1)
        attempt.compile_ms = result.elapsed_ms
        attempt.compiled_sources = result.compiled_source_count
        timing = {
            "compile_ms": result.elapsed_ms,
            "compile_total_ms": compile_total_ms,
            "jdt_build_ms": result.jdt_build_ms,
            "diagnostics_ms": result.diagnostics_ms,
            "compiled_source_count": result.compiled_source_count,
        }
        if not result.compile_ok:
            return RuntimeResult(ok=False, error="JDT incremental compilation failed.", data={
                **timing, "error_code": "JDT_COMPILE_FAILED", "applied": False,
                "error_count": result.error_count, "diagnostics": list(result.diagnostics),
                "diagnostics_truncated": result.diagnostics_truncated,
            })
        if session.reload_cancel_requested(attempt):
            return RuntimeResult(ok=False, error="Reload cancelled.", data={
                **timing, "error_code": "RELOAD_CANCELLED", "applied": False})
        if (result.runtime_deleted_classes or result.runtime_changed_resources
                or result.runtime_deleted_resources):
            value = runtime._reload_requires_relaunch_result(
                reason_code="NON_HOTSWAPPABLE_OUTPUT_DELTA",
                message="The build changed resources or deleted classes. Launch again to apply them.",
                details=timing,
            )
            return value
        paths = result.runtime_changed_classes
        if not paths:
            attempt.apply_method = "none"
            return RuntimeResult(ok=True, data={
                **timing, "status": "no_changes", "applied": True, "apply_method": "none",
                **runtime._runtime_overlay_snapshot(),
            })

        jdwp = runtime._connect()
        definitions = {}
        signatures = set()
        names = set()
        for relative in paths:
            name = relative.removesuffix(".class").replace("/", ".")
            signature = "L" + name.replace(".", "/") + ";"
            loaded = jdwp.classes_by_signature(signature)
            if len(loaded) != 1:
                return runtime._reload_requires_relaunch_result(
                    reason_code="CLASS_NOT_LOADED" if not loaded else "AMBIGUOUS_CLASS_LOADER",
                    message="The JVM has no unique loaded definition for " + name,
                    details=timing,
                )
            definitions[loaded[0].reference_type_id] = (
                result.output_directory / relative
            ).read_bytes()
            names.add(name)
            signatures.add(signature)
        session.transition_reload(ReloadStage.APPLYING_HOTSWAP)
        started = time.monotonic()
        try:
            jdwp.redefine_classes(definitions)
        except JDWPCommandRejected as error:
            return runtime._reload_requires_relaunch_result(
                reason_code="HOT_SWAP_REJECTED", message=str(error),
                details={**timing, "jdwp_error_code": error.code},
            )
        except JDWPCommandOutcomeUnknown:
            runtime._runtime_overlay_state = "unknown"
            return RuntimeResult(ok=False, error="The JVM did not confirm HotSwap.", data={
                **timing, "error_code": "HOT_SWAP_OUTCOME_UNKNOWN", "applied": None,
                "suggested_next_step": "Restart the application to establish its code state.",
            })
        apply_ms = round((time.monotonic() - started) * 1000, 1)
        compiler.mark_published()
        attempt.apply_method = "hotswap"
        # These are observations of accepted definitions, not a full class digest.
        runtime._runtime_overlay_state = "active"
        runtime._runtime_overlay_sources.update(
            source.relative_to(prepared.jdt_build_world_plan.project_root).as_posix()
            for source in sources
        )
        runtime._code_revision += 1
        breakpoints = runtime._refresh_updated_breakpoints(jdwp, signatures)
        return RuntimeResult(ok=True, data={
            **timing, "apply_ms": apply_ms, "status": "reloaded", "applied": True,
            "apply_method": "hotswap", "persistence": "jdt_workspace",
            "restart_loses_update": False, "redefined_classes": sorted(names),
            "breakpoint_refresh_state": breakpoints["state"],
            "stale_breakpoint_ids": breakpoints["stale"],
            "warnings": breakpoints["warnings"],
            "framework_state_refreshed": False,
            **runtime._runtime_overlay_snapshot(),
            "suggested_next_step": "Trigger a fresh request and verify the changed behavior.",
        })


__all__ = ["JdtReloadService"]
