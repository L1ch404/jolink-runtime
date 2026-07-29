"""Transport-independent dispatch for the migrated Java Runtime tools."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ..adapters.java.jdwp_adapter import JavaRuntime
from ..adapters.java.process_discovery import discover_java_processes
from ..launch import ProjectLaunchRequest
from .models import RuntimeAction, RuntimeResult
from .session_manager import SessionManager
from .wait_state import WaitControl


logger = logging.getLogger(__name__)


def _log_error_summary(error: str) -> str:
    """Keep logs diagnostic without copying application output into logs."""
    if not error:
        return "-"
    return error.splitlines()[0][:240]


def _bool_arg(arguments: dict[str, Any], name: str, default: bool = False) -> bool:
    value = arguments.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def parse_runtime_action(arguments: dict[str, Any]) -> RuntimeAction:
    """Parse arguments with the same defaults and coercions as the old handler."""
    action = RuntimeAction(
        action=arguments.get("action", "status"),
        classpath=arguments.get("classpath", "."),
        main_class=arguments.get("main_class", ""),
        jar_path=arguments.get("jar_path", ""),
        app_args=arguments.get("app_args"),
        jdwp_port=arguments.get("jdwp_port", 5005),
        vm_args=arguments.get("vm_args"),
        pid=arguments.get("pid", 0),
        host=arguments.get("host", "127.0.0.1"),
        tail=arguments.get("tail", 50),
        bp_action=arguments.get("bp_action", "set"),
        exception_action=arguments.get("exception_action", "set"),
        breakpoint_id=arguments.get("breakpoint_id", ""),
        request_id=arguments.get("request_id", 0),
        class_pattern=arguments.get("class_pattern", ""),
        include_proxy=_bool_arg(arguments, "include_proxy", False),
        include_generated=_bool_arg(arguments, "include_generated", False),
        exception_class=arguments.get("exception_class", ""),
        caught=_bool_arg(arguments, "caught", True),
        uncaught=_bool_arg(arguments, "uncaught", True),
        allow_broad_caught=_bool_arg(arguments, "allow_broad_caught", False),
        line=arguments.get("line", 0),
        thread_name=arguments.get("thread_name", ""),
        frame_index=arguments.get("frame_index", 0),
        max_frames=arguments.get("max_frames", 20),
        include_this=_bool_arg(arguments, "include_this", False),
        max_value_depth=int(arguments.get("max_value_depth", 1)),
        semantic_collections=_bool_arg(arguments, "semantic_collections", True),
        item_limit=int(arguments.get("item_limit", 16)),
        map_entry_limit=int(arguments.get("map_entry_limit", 16)),
        timeout=float(arguments.get("timeout", 30)),
        suspension_id=arguments.get("suspension_id", ""),
    )
    if "source_files" in arguments:
        # MCP-only extension: keep the frozen Runtime 2.4.0 dataclass shape
        # unchanged for legacy-lineage differential contracts.
        action.source_files = arguments.get("source_files")
    action.configure_startup_readiness(
        ready_port=int(arguments.get("ready_port", 0)),
        wait_timeout_seconds=float(
            arguments.get("startup_wait_timeout_seconds", 30)
        ),
        wait_timeout_provided=(
            "startup_wait_timeout_seconds" in arguments
        ),
    )
    return action


class ProjectLaunchArgumentError(ValueError):
    """A structured MCP-only project-launch argument rejection."""

    def __init__(self, *, argument: str, message: str) -> None:
        super().__init__(message)
        self.payload = {
            "ok": False,
            "error": message,
            "error_code": "INVALID_ARGUMENT",
            "argument": argument,
            "retryable": True,
            "suggested_next_step": (
                "Correct the project launch arguments and retry run/restart."
            ),
        }


def parse_project_launch_request(
    arguments: dict[str, Any],
) -> ProjectLaunchRequest | None:
    """Parse the small MCP-only IDEA/Maven launch surface."""
    has_project_path = "project_path" in arguments
    has_launch_name = "launch_name" in arguments
    if not has_project_path:
        if has_launch_name:
            raise ProjectLaunchArgumentError(
                argument="launch_name",
                message="launch_name requires project_path.",
            )
        return None

    action = str(arguments.get("action", "status"))
    if action not in {"run", "restart"}:
        raise ProjectLaunchArgumentError(
            argument="project_path",
            message="project_path is only valid for run or restart.",
        )
    raw_project_path = arguments.get("project_path")
    if not isinstance(raw_project_path, str) or not raw_project_path.strip():
        raise ProjectLaunchArgumentError(
            argument="project_path",
            message="project_path must be a non-empty local path.",
        )
    raw_launch_name = arguments.get("launch_name")
    if raw_launch_name is not None and (
        not isinstance(raw_launch_name, str)
        or not raw_launch_name.strip()
    ):
        raise ProjectLaunchArgumentError(
            argument="launch_name",
            message="launch_name must be a non-empty string when supplied.",
        )

    direct_fields = tuple(
        field
        for field in (
            "classpath",
            "main_class",
            "jar_path",
            "app_args",
            "vm_args",
        )
        if field in arguments
    )
    if direct_fields:
        raise ProjectLaunchArgumentError(
            argument=direct_fields[0],
            message=(
                "project_path cannot be combined with direct JVM launch "
                f"arguments: {', '.join(direct_fields)}."
            ),
        )

    return ProjectLaunchRequest(
        project_path=Path(raw_project_path).expanduser(),
        launch_name=(
            raw_launch_name
            if isinstance(raw_launch_name, str)
            else None
        ),
        jdwp_port=int(arguments.get("jdwp_port", 5005)),
        ready_port=int(arguments.get("ready_port", 0)),
        startup_wait_timeout_seconds=float(
            arguments.get("startup_wait_timeout_seconds", 30)
        ),
    )


def _runtime_result_payload(result: RuntimeResult) -> dict[str, Any]:
    """Return the exact JSON object represented by ``RuntimeResult.to_json``."""
    return json.loads(result.to_json())


class Dispatcher:
    """Route standalone tool calls without depending on MCP or Hermes."""

    def __init__(self, sessions: SessionManager | None = None) -> None:
        self.sessions = sessions if sessions is not None else SessionManager(JavaRuntime)

    def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        session_key: str = "default",
        wait_control: WaitControl | None = None,
    ) -> dict[str, Any]:
        """Dispatch a migrated tool call and return its parsed JSON object."""
        args = arguments or {}
        if tool_name == "java_runtime":
            return self.dispatch_java_runtime(
                args,
                session_key=session_key,
                wait_control=wait_control,
            )
        if tool_name == "java_processes":
            return discover_java_processes(
                filter_text=args.get("filter"),
                full=bool(args.get("full", False)),
            )
        return {"ok": False, "error": f"Unknown tool: {tool_name}"}

    def dispatch_java_runtime(
        self,
        arguments: dict[str, Any],
        *,
        session_key: str = "default",
        wait_control: WaitControl | None = None,
    ) -> dict[str, Any]:
        """Run one Java Runtime action with the established handler semantics."""
        started_at = time.monotonic()
        action = parse_runtime_action(arguments)
        try:
            project_request = parse_project_launch_request(arguments)
        except ProjectLaunchArgumentError as error:
            return dict(error.payload)
        context_key = str(session_key or "default")
        runtime = self.sessions.get_runtime(context_key)
        logger.info(
            "java_runtime.action.start action=%s context=%s pid=%s main_class=%s "
            "jar_path=%s jdwp=%s:%s breakpoint=%s:%s breakpoint_id=%s "
            "exception=%s request_id=%s suspension=%s",
            action.action,
            context_key,
            action.pid or "-",
            action.main_class or "-",
            action.jar_path or "-",
            action.host,
            action.jdwp_port,
            action.class_pattern or "-",
            action.line or "-",
            action.breakpoint_id or "-",
            action.exception_class or "-",
            action.request_id or "-",
            action.suspension_id or "-",
        )

        handlers = {
            "run": runtime.run,
            "stop": runtime.stop,
            "restart": runtime.restart,
            "attach": runtime.attach,
            "detach": runtime.detach,
            "status": runtime.status,
            "logs": runtime.logs,
            "breakpoint": runtime.breakpoint,
            "exception": runtime.exception,
            "wait_event": runtime.wait_event,
            "wait_breakpoint": runtime.wait_breakpoint,
            "threads": runtime.threads,
            "stack": runtime.stack,
            "variables": runtime.variables,
            "resume": runtime.resume,
            "cleanup_debug_state": runtime.cleanup_debug_state,
            "update": getattr(runtime, "update", None),
        }
        handler = handlers.get(action.action)
        if handler is None:
            error = f"Unknown action: {action.action}"
            logger.warning(
                "java_runtime.action.invalid action=%s context=%s",
                action.action,
                context_key,
            )
            logger.warning(
                "java_runtime.action.finish action=%s context=%s ok=False "
                "duration_ms=%.1f error=%s",
                action.action,
                context_key,
                (time.monotonic() - started_at) * 1000,
                _log_error_summary(error),
            )
            return {"ok": False, "error": error}

        try:
            if project_request is not None:
                project_handler = (
                    runtime.run_project
                    if action.action == "run"
                    else runtime.restart_project
                )
                result = project_handler(action, project_request)
            elif action.action == "wait_event" and wait_control is not None:
                result = runtime.wait_event(action, wait_control=wait_control)
            else:
                result = handler(action)
            data = result.data or {}
            log_finish = logger.warning if result.error else logger.info
            log_finish(
                "java_runtime.action.finish action=%s context=%s ok=%s duration_ms=%.1f "
                "status=%s process=%s startup=%s debug=%s pid=%s suspension=%s "
                "threads=%s frames=%s variables=%s complete=%s error=%s",
                action.action,
                context_key,
                not bool(result.error) and result.ok,
                (time.monotonic() - started_at) * 1000,
                data.get("status", "-"),
                data.get("process_state", "-"),
                data.get("startup_state", "-"),
                data.get("debug_state", "-"),
                data.get("pid", "-"),
                data.get("suspension_id", data.get("invalidated_suspension_id", "-")),
                data.get("thread_count", "-"),
                data.get("frame_count", "-"),
                data.get("variable_count", "-"),
                data.get("complete", "-"),
                _log_error_summary(result.error),
            )
            return _runtime_result_payload(result)
        except Exception as error:
            logger.exception(
                "java_runtime.action.crash action=%s context=%s duration_ms=%.1f",
                action.action,
                context_key,
                (time.monotonic() - started_at) * 1000,
            )
            message = f"{type(error).__name__}: {error}"
            logger.error(
                "java_runtime.action.finish action=%s context=%s ok=False "
                "duration_ms=%.1f error=%s",
                action.action,
                context_key,
                (time.monotonic() - started_at) * 1000,
                _log_error_summary(message),
            )
            return {"ok": False, "error": message}

    def settle_cancelled_wait(
        self,
        wait_control: WaitControl,
        *,
        session_key: str = "default",
    ) -> bool:
        """Settle an existing cancelled wait without allocating a Runtime."""
        return self.sessions.settle_cancelled_wait(wait_control, session_key)

    def interrupt_wait(self, session_key: str = "default") -> bool:
        """Wake an existing Runtime wait without allocating a Runtime."""
        return self.sessions.interrupt_wait(session_key)

    def startup_observation(
        self,
        session_key: str = "default",
    ) -> dict[str, Any]:
        """Return readiness state without allocating a Runtime or touching JDWP."""
        runtime = self.sessions.get_existing_runtime(session_key)
        if runtime is None:
            return {
                "process_state": "absent",
                "readiness_configured": False,
            }
        observer = getattr(runtime, "startup_observation", None)
        if not callable(observer):
            return {
                "startup_state": "unverified",
                "readiness_configured": False,
            }
        return dict(observer())

    def close_session(self, session_key: str = "default") -> bool:
        """Close an existing Runtime session without allocating one."""
        return self.sessions.close_session(session_key)

    def force_close_session(self, session_key: str = "default") -> bool:
        """Force-release an existing Runtime session without JDWP cleanup."""
        return self.sessions.force_close_session(session_key)

    def wait_for_close_session(
        self,
        session_key: str = "default",
        timeout: float | None = None,
    ) -> bool:
        """Wait for an in-progress normal Runtime close."""
        return self.sessions.wait_for_close_session(session_key, timeout)

    def close_all_sessions(self) -> None:
        """Close every existing Runtime session."""
        self.sessions.close_all()


__all__ = [
    "Dispatcher",
    "ProjectLaunchArgumentError",
    "parse_project_launch_request",
    "parse_runtime_action",
]
