"""Small status projections and on-demand logs; no new observation state."""

from __future__ import annotations

import locale
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..adapters.java.log_manager import read_log_tail_snapshot
from ..core.models import RuntimeAction, RuntimeResult


def _select(value: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: value[key] for key in fields if key in value}


def _details_action() -> dict[str, Any]:
    return {"tool": "java_status", "arguments": {"action": "status", "details": True}}


def status_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the facts needed to wait, resume, or decide the next action."""
    result = _select(payload, (
        "ok", "status", "error", "error_code", "message", "retryable",
        "process_state", "running", "pid", "exit_code", "launch_mode",
        "main_class", "jar_path", "ownership", "debug_state", "suspension_id",
        "breakpoint_count", "exception_count", "debug_requests_invalidated",
        "orphan_events_resumed", "startup_state", "readiness_configured",
        "startup_elapsed_ms", "ready_observed_at", "startup_failed_at",
        "failure_type", "startup_wait_timed_out",
        "attempt_id", "launch_phase", "cancel_requested", "compile_ready",
        "jdt_bootstrap_state", "active_operation", "fast_test", "code_revision",
        "verification_state", "restart_required", "warnings", "next_action",
        "suggested_next_step",
    ))
    if isinstance(payload.get("readiness"), dict):
        result["readiness"] = _select(payload["readiness"], (
            "type", "host", "port", "verified", "last_result",
        ))
    if isinstance(payload.get("build"), dict):
        result["build"] = _select(payload["build"], (
            "running", "running_operation_count", "cancel_requested",
        ))
    if isinstance(payload.get("fast_update"), dict):
        result["fast_update"] = _select(payload["fast_update"], ("available", "status", "reason"))
    if isinstance(payload.get("launch_error"), dict):
        result["launch_error"] = _select(payload["launch_error"], ("error_code", "message", "retryable"))
        result["launch_error"]["next_action"] = _details_action()
        result["suggested_next_step"] = (
            "Read java_status(action='status', details=true) for the launch error, or "
            "java_status(action='logs', source='build') for the build log."
        )
    last = payload.get("last_reload")
    if isinstance(last, dict):
        result["last_reload"] = _select(last, (
            "ok", "status", "reload_id", "stage", "applied", "apply_method",
            "error_code", "restart_reason", "compile_ms", "startup_ms", "total_ms",
            "compiled_source_count", "source_changes_pending", "post_apply_state",
        ))
        result["last_reload"]["next_action"] = _details_action()
    elif "last_reload" in payload:
        result["last_reload"] = None
    return result


def read_build_log(path: Path, lines: int, redact: Callable[[str], str]) -> dict[str, Any]:
    """Share the existing bounded build-log reader and redaction policy."""
    get_encoding = getattr(locale, "getencoding", None)
    encoding = str(get_encoding()) if callable(get_encoding) else locale.getpreferredencoding(False)
    tail = read_log_tail_snapshot(
        str(path), lines, encoding=encoding,
        max_scan_bytes=512 * 1024, max_return_bytes=32 * 1024,
    )
    tail["lines"] = [redact(line) for line in tail["lines"]]
    tail["returned_bytes"] = sum(len(line.encode("utf-8")) for line in tail["lines"])
    return tail


def runtime_logs(runtime: Any, action: RuntimeAction) -> RuntimeResult:
    """Read application logs by default, or the current launch's build log."""
    source = getattr(action, "log_source", "application")
    if source == "build":
        current = runtime._launch_controller.snapshot()
        with runtime._project_state_lock:
            directory = runtime._project_attempt_directories.get(current.get("attempt_id"))
        try:
            if directory is None:
                raise FileNotFoundError("No project build log is available")
            data = read_build_log(directory / "build.log", action.tail, runtime._redact_build_log_line)
        except OSError as error:
            return RuntimeResult(ok=False, error=str(error), data={"error_code": "BUILD_LOG_UNAVAILABLE"})
        data["source"] = "build"
    else:
        data = runtime._log.tail(action.tail)
    if "error" in data:
        return RuntimeResult(ok=False, error=data["error"])
    return RuntimeResult(ok=True, data=data)
