"""Observe an existing launch/Test attempt without holding the MCP call lock."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..core.models import RuntimeAction

WAIT_NEXT_STEP = (
    "The task is still running in the background. Choose a suitable waiting "
    "interval based on its current stage; you can use sleep or PowerShell "
    "Start-Sleep before calling java_status(action='status'). Avoid rapid "
    "repeated queries and do not submit this task again."
)
_LAUNCH_FINISHED = {"runtime_active", "failed", "cancelled", "stopped"}


@dataclass
class ApplicationWait:
    pending: Callable[[], bool]
    result: Callable[[], dict]


def application_waiter(runtime, action: str, initial: dict) -> ApplicationWait | None:
    """Capture the original attempt so another request cannot replace its result."""
    if initial.get("ok") is False:
        return None
    if action == "test":
        manager = runtime._fast_tests
        with manager._lock:
            attempt = manager._active or manager._last
            if attempt is None or attempt.test_run_id != initial.get("test_run_id"):
                return None
        return ApplicationWait(
            pending=lambda: not attempt.done.is_set(),
            result=attempt.snapshot,
        )
    if action != "launch":
        return None
    if initial.get("attempt_id"):
        controller = runtime._launch_controller
        with controller._lock:
            record = controller._current
            if record is None or record.attempt.attempt_id != initial["attempt_id"]:
                return None

        def pending():
            with controller._lock:
                return record.attempt.phase not in _LAUNCH_FINISHED

        def result():
            # Full status only once when returning, never on every wait tick.
            with controller._lock:
                current = controller._current is record
                snapshot = record.attempt.public_snapshot()
            if current:
                status = runtime.status(RuntimeAction(action="status"))
                snapshot = {**snapshot, **(status.data or {})}
            phase = snapshot.get("launch_phase")
            payload = {
                **initial,
                **snapshot,
                "ok": phase not in {"failed", "cancelled", "stopped"},
            }
            payload.pop("suggested_next_step", None)
            if phase == "runtime_active":
                payload["status"] = "process_started"
            elif phase == "failed":
                failure = snapshot.get("launch_error", {})
                payload.update(
                    status="failed",
                    error=failure.get("message", "Application launch failed."),
                    error_code=failure.get("error_code", "JVM_START_FAILED"),
                    suggested_next_step=failure.get(
                        "suggested_next_step", "Inspect application logs."
                    ),
                )
            elif phase in {"cancelled", "stopped"}:
                payload.update(
                    status=phase,
                    error="Application launch was stopped.",
                    error_code="LAUNCH_CANCELLED",
                )
            return payload

        return ApplicationWait(pending=pending, result=result)

    process = runtime._proc.current
    if process is None or process.pid != initial.get("pid"):
        return None

    def pending():
        return runtime._proc.observe_readiness(process)["startup_state"] == "starting"

    def result():
        observation = runtime._proc.observe_readiness(process)
        payload = {**initial, **observation}
        failed = observation.get("startup_state") == "failed" or not process.is_alive()
        if failed or observation.get("startup_state") in {"ready", "unverified"}:
            for name in ("next_action", "suggested_next_step", "startup_wait_timed_out"):
                payload.pop(name, None)
        if failed:
            payload.update(
                ok=False,
                error="Application exited during startup.",
                error_code="JVM_START_FAILED",
                next_action="logs",
                suggested_next_step="Inspect application logs before launching again.",
            )
        return payload

    return ApplicationWait(pending=pending, result=result)
