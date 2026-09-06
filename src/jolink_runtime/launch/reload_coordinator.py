"""Background execution for accepted Java reload attempts."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from ..core.models import RuntimeResult


class BackgroundReloadCoordinator:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def start(
        self,
        *,
        project_session: Any,
        reload_attempt: Any,
        source_fingerprint: str,
        update_lock: threading.Lock,
        operation: Callable[[], RuntimeResult],
    ) -> threading.Thread:
        def run() -> None:
            result: RuntimeResult | None = None
            try:
                with update_lock:
                    if project_session.reload_cancel_requested(reload_attempt):
                        project_session.record_reload_result(
                            reload_attempt,
                            {
                                "ok": False,
                                "error_code": "RELOAD_CANCELLED",
                                "applied": False,
                            },
                        )
                        project_session.finish_reload(
                            applied=False,
                            source_fingerprint_after=source_fingerprint,
                            error_code="RELOAD_CANCELLED",
                        )
                        return
                    result = operation()
                    project_session.record_reload_result(
                        reload_attempt,
                        {
                            "ok": result.ok,
                            **result.data,
                            **({"error": result.error} if result.error else {}),
                        },
                    )
            except Exception as error:
                self._logger.exception(
                    "java_runtime.reload.background_failed reload_id=%s",
                    reload_attempt.attempt_id,
                )
                applied = reload_attempt.applied is True
                payload = {
                    "ok": False,
                    "error": str(error),
                    "error_code": getattr(
                        error,
                        "error_code",
                        "RELOAD_BACKGROUND_FAILED",
                    ),
                    "applied": False,
                }
                if applied:
                    payload = {
                        "ok": True,
                        "status": "reloaded",
                        "applied": True,
                        "apply_method": reload_attempt.apply_method,
                        "post_apply_state": "incomplete",
                        "warnings": [
                            "The JVM accepted the reload, but post-apply bookkeeping failed. "
                            "Verify current behavior; do not retry on the assumption that code was not applied."
                        ],
                        "post_apply_error_type": type(error).__name__,
                    }
                project_session.record_reload_result(reload_attempt, payload)
                if project_session.active_reload is reload_attempt:
                    project_session.finish_reload(
                        applied=applied,
                        source_fingerprint_after=source_fingerprint,
                        error_code=None
                        if applied
                        else getattr(error, "error_code", "RELOAD_BACKGROUND_FAILED"),
                    )
            finally:
                if (
                    result is not None
                    and project_session.active_reload is reload_attempt
                    and (result.data.get("applied") is not None or not result.ok)
                ):
                    project_session.finish_reload(
                        applied=result.data.get("applied", False),
                        source_fingerprint_after=source_fingerprint,
                        error_code=(
                            result.data.get("error_code") if not result.ok else None
                        ),
                    )

        worker = threading.Thread(
            target=run,
            name=f"jolink-reload-{reload_attempt.attempt_id}",
            daemon=True,
        )
        project_session.attach_reload_worker(reload_attempt, worker)
        worker.start()
        return worker


__all__ = ["BackgroundReloadCoordinator"]
