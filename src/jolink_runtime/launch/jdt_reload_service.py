"""Acceptance and background dispatch for persistent JDT reloads."""

from __future__ import annotations

import logging
from typing import Any

from ..core.models import RuntimeAction, RuntimeResult
from .jdt_compile_session import JdtCompileError, PersistentJdtCompileSession
from .project_session import JavaProjectSession, ProjectSessionError
from .reload_coordinator import BackgroundReloadCoordinator


class JdtReloadService:
    def __init__(self, logger: logging.Logger) -> None:
        self._coordinator = BackgroundReloadCoordinator(logger)

    def start(
        self,
        runtime: Any,
        action: RuntimeAction,
        *,
        attempt_id: str,
        generation: int,
        prepared: Any,
        project_session: JavaProjectSession,
    ) -> RuntimeResult:
        plan = prepared.jdt_build_world_plan
        compiler = project_session.compile_session
        if plan is None or not isinstance(compiler, PersistentJdtCompileSession):
            return RuntimeResult(
                ok=False,
                error="Persistent JDT reload is unavailable.",
                data={
                    "error_code": "JDT_SESSION_NOT_READY",
                    "applied": False,
                    "retryable": True,
                    "suggested_next_step": "Call status until compile_ready is true.",
                },
            )
        if not getattr(plan, "is_fresh", lambda: True)():
            return RuntimeResult(
                ok=False,
                error="The JDT BuildWorld changed before reload.",
                data={
                    "error_code": "JDT_BUILD_WORLD_STALE",
                    "applied": False,
                    "retryable": True,
                    "suggested_next_step": "Launch the project again.",
                },
            )
        if runtime._active_suspension is not None:
            return RuntimeResult(
                ok=False,
                error="A debug suspension is active.",
                data={
                    "error_code": "ACTIVE_SUSPENSION_EXISTS",
                    "applied": False,
                    "retryable": True,
                    "suggested_next_step": "Resume before calling reload.",
                },
            )
        if runtime._armed_breakpoint_requests or runtime._armed_exception_requests:
            return RuntimeResult(
                ok=False,
                error="A debug-event wait is still armed.",
                data={
                    "error_code": "ACTIVE_DEBUG_REQUESTS_REMAIN",
                    "applied": False,
                    "retryable": True,
                    "suggested_next_step": (
                        "Await or clean up wait_event before reload."
                    ),
                },
            )
        try:
            source_files = runtime._resolve_jdt_reload_sources(
                plan, getattr(action, "source_files", None)
            )
            source_fingerprint = runtime._source_selection_fingerprint(
                source_files
            )
            reload_attempt = project_session.begin_reload(
                source_fingerprint,
                source_files=source_files,
            )
        except (JdtCompileError, OSError, ProjectSessionError) as error:
            return RuntimeResult(
                ok=False,
                error=str(error),
                data={
                    "error_code": getattr(
                        error, "error_code", "JDT_RELOAD_FAILED"
                    ),
                    "applied": False,
                    "retryable": True,
                    "suggested_next_step": "Correct the reload input and retry.",
                },
            )

        self._coordinator.start(
            project_session=project_session,
            reload_attempt=reload_attempt,
            source_fingerprint=source_fingerprint,
            update_lock=runtime._update_lock,
            operation=lambda: runtime._update_with_jdt(
                action,
                attempt_id=attempt_id,
                generation=generation,
                prepared=prepared,
                project_session=project_session,
                reload_attempt=reload_attempt,
                source_files=source_files,
                source_fingerprint=source_fingerprint,
            ),
        )
        return RuntimeResult(
            ok=True,
            data={
                "status": "reload_started",
                "reload_id": reload_attempt.attempt_id,
                "applied": None,
                "suggested_next_step": (
                    "Call status to observe active_operation and last_reload."
                ),
            },
        )


__all__ = ["JdtReloadService"]
