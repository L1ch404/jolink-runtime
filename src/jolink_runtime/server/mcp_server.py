"""Low-level MCP server boundary for the standalone Runtime dispatcher."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Protocol

import anyio
import mcp.types as types
from anyio.lowlevel import checkpoint_if_cancelled
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from mcp.server.lowlevel import Server

from .. import __version__
from ..core.dispatcher import Dispatcher
from ..core.wait_state import WaitControl
from .http_trigger import (
    HTTPTriggerControl,
    HTTPTriggerValidationError,
    parse_http_trigger,
)
from .tool_schema import (
    JAVA_PROCESSES_INPUT_SCHEMA,
    JAVA_RUNTIME_INPUT_SCHEMA,
    get_mcp_tools,
)


logger = logging.getLogger(__name__)

SERVER_NAME = "jolink-runtime"
_NO_ACTIVE_SUSPENSION_NEXT_STEP = (
    "List the current breakpoint and exception definitions, and configure "
    "the required one only if it is missing. Then call wait_event with "
    "wait_mode='arm'. After status='armed', trigger the target scenario and "
    "call wait_event with wait_mode='await' using the returned wait_handle."
)
SERVER_INSTRUCTIONS = (
    "Use java_runtime to run, observe, verify, and debug a local Java application. "
    "Use java_processes only to discover an existing JVM for attach. "
    "Treat runtime outputs as bounded observations, not as self-explanatory "
    "causal conclusions. "
    "Clearly separate directly observed facts, inferences, and what remains "
    "unverified. "
    "Do not declare a root cause until evidence shows that the suspected "
    "mechanism actually occurred in the current scenario. "
    "When multiple explanations remain consistent with the evidence, seek the "
    "next piece of evidence that best distinguishes them. "
    "After inspecting a suspension, always call resume or cleanup_debug_state."
)

_TOOL_INPUT_SCHEMAS = {
    "java_runtime": JAVA_RUNTIME_INPUT_SCHEMA,
    "java_processes": JAVA_PROCESSES_INPUT_SCHEMA,
}
_TOOL_VALIDATORS = {
    name: Draft202012Validator(schema)
    for name, schema in _TOOL_INPUT_SCHEMAS.items()
}


class DispatchesRuntimeTools(Protocol):
    """Structural type used by the MCP boundary and its tests."""

    def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        session_key: str = "default",
        wait_control: WaitControl | None = None,
    ) -> dict[str, Any]:
        ...

    def settle_cancelled_wait(
        self,
        wait_control: WaitControl,
        *,
        session_key: str = "default",
    ) -> bool:
        ...

    def interrupt_wait(self, session_key: str = "default") -> bool:
        ...

    def close_session(self, session_key: str = "default") -> bool:
        ...

    def force_close_session(self, session_key: str = "default") -> bool:
        ...

    def wait_for_close_session(
        self,
        session_key: str = "default",
        timeout: float | None = None,
    ) -> bool:
        ...


def _validation_argument(
    error: ValidationError,
    arguments: dict[str, Any],
) -> str | None:
    path = [str(part) for part in error.absolute_path]
    if error.validator == "required":
        container: Any = arguments
        for part in path:
            if not isinstance(container, dict):
                break
            container = container.get(part)
        missing = [
            name
            for name in error.validator_value
            if not isinstance(container, dict) or name not in container
        ]
        if missing:
            path.append(str(missing[0]))
    if not path:
        return None
    # Header names are user-controlled.  The field location is enough to
    # correct a type error without copying a potentially sensitive name.
    if len(path) > 2 and path[:2] == ["http_trigger", "headers"]:
        path = ["http_trigger", "headers", "<header>"]
    return ".".join(path)


def _invalid_argument_payload(
    error: ValidationError,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    argument = _validation_argument(error, arguments)
    payload: dict[str, Any] = {
        "ok": False,
        "error": (
            f"Invalid value for {argument}."
            if argument is not None
            else "Invalid tool arguments."
        ),
        "error_code": "INVALID_ARGUMENT",
        "retryable": True,
        "suggested_next_step": "Correct the indicated argument and retry.",
    }
    if argument is not None:
        payload["argument"] = argument
    payload["validation_rule"] = str(error.validator)
    if error.validator in {
        "type",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
    }:
        payload["expected"] = error.validator_value
    return payload


def _execution_error_payload(error: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "error": f"{type(error).__name__}: {error}",
        "error_code": "MCP_DISPATCH_FAILED",
        "retryable": False,
        "suggested_next_step": (
            "Check the server's stderr logs and java_runtime status before retrying."
        ),
    }


def _argument_parsing_error_payload(_error: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "Invalid tool argument types.",
        "error_code": "INVALID_ARGUMENT",
        "retryable": True,
        "suggested_next_step": "Correct the argument types and retry.",
    }


def _call_tool_result(payload: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        ],
        structuredContent=payload,
        isError=payload.get("ok") is False,
    )


def _closing_payload() -> dict[str, Any]:
    return {
        "ok": False,
        "error": "The Runtime MCP server is shutting down.",
        "error_code": "SERVER_SHUTTING_DOWN",
        "retryable": True,
        "suggested_next_step": "Reconnect to the MCP server and retry.",
    }


def _iso_timestamp(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(
        epoch_seconds,
        tz=timezone.utc,
    ).isoformat()


def _active_waiter_payload(control: WaitControl) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": "A two-phase wait_event observation is still active.",
        "error_code": "ACTIVE_WAITER_EXISTS",
        "status": "active_waiter_exists",
        "retryable": True,
        "wait_handle": control.wait_handle,
        "wait_phase": control.phase,
        "suggested_next_step": (
            "Call wait_event with wait_mode='await' and this wait_handle, "
            "or call cleanup_debug_state to cancel it safely."
        ),
    }
    if control.armed_at is not None:
        payload["armed_at"] = _iso_timestamp(control.armed_at)
    if control.trigger_control is not None:
        payload["http_trigger"] = control.trigger_control.snapshot()
    return payload


def _wait_argument_error(
    argument: str,
    message: str,
    suggested_next_step: str,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": message,
        "error_code": "INVALID_WAIT_ARGUMENTS",
        "argument": argument,
        "retryable": True,
        "suggested_next_step": suggested_next_step,
    }


def _http_trigger_error_payload(
    error: HTTPTriggerValidationError,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": str(error),
        "error_code": error.code,
        "argument": "http_trigger",
        "retryable": True,
        "suggested_next_step": (
            "Correct the local HTTP trigger and retry wait_event with "
            "wait_mode='arm'."
        ),
    }


def _trigger_failed_payload(
    control: WaitControl,
) -> dict[str, Any]:
    trigger = control.trigger_control
    snapshot = trigger.snapshot() if trigger is not None else None
    connection_failed = (
        isinstance(snapshot, dict)
        and snapshot.get("error_code") == "HTTP_TRIGGER_CONNECTION_FAILED"
    )
    return {
        "ok": False,
        "error": (
            "The local HTTP trigger could not be started or connected to "
            "the target."
        ),
        "error_code": "HTTP_TRIGGER_FAILED",
        "status": "http_trigger_failed",
        "retryable": True,
        "wait_handle": control.wait_handle,
        "http_trigger": snapshot,
        "suggested_next_step": (
            (
                "The request was not sent. The application may still be starting; "
                "keep the JVM running and call status to check startup_state. "
                "If readiness is unverified, verify the service port and "
                "configure ready_port on a future run/restart. "
                "After startup_state is ready, start a new wait_event with "
                "wait_mode='arm'."
            )
            if connection_failed
            else (
                "Verify the local application port and URL, then start a new "
                "wait_event with wait_mode='arm'."
            )
        ),
    }


def _application_not_ready_payload(
    observation: dict[str, Any],
) -> dict[str, Any]:
    state = str(observation.get("startup_state", "starting"))
    payload: dict[str, Any] = {
        "ok": False,
        "error": (
            "The configured local application readiness check has not "
            "reached ready state."
        ),
        "error_code": "APPLICATION_NOT_READY",
        "status": "application_not_ready",
        "retryable": True,
        "process_state": observation.get("process_state", "running"),
        "startup_state": state,
        "http_trigger_sent": False,
        "next_action": "status",
        "suggested_next_step": (
            "Keep the application running and call status until startup_state "
            "is ready, then arm the HTTP trigger again."
        ),
    }
    for key in (
        "pid",
        "startup_elapsed_ms",
        "readiness",
        "ready_observed_at",
        "failure_type",
    ):
        if key in observation:
            payload[key] = observation[key]
    if state == "failed":
        payload["next_action"] = "logs"
        payload["suggested_next_step"] = (
            "Call status to confirm the exited process, then inspect logs "
            "before running the application again."
        )
    return payload


def _with_http_trigger(
    payload: dict[str, Any],
    control: WaitControl,
) -> dict[str, Any]:
    trigger = control.trigger_control
    if trigger is None:
        return payload
    combined = dict(payload)
    combined["http_trigger"] = trigger.snapshot()
    return combined


def _variables_observation_state(payload: dict[str, Any]) -> str:
    """Summarize variable evidence without changing Runtime lineage payloads."""
    variables = payload.get("variables")
    if not isinstance(variables, list):
        return "partial"
    if not variables:
        if payload.get("complete") is True and not payload.get("partial"):
            return "complete"
        if payload.get("getvalues_error"):
            return "unavailable"
        return "partial"

    states = [
        variable.get("value_state")
        for variable in variables
        if isinstance(variable, dict)
    ]
    if len(states) != len(variables):
        return "partial"
    if all(state == "observed" for state in states):
        return "complete"
    if all(state == "unavailable" for state in states):
        return "unavailable"
    return "partial"


def _normalize_mcp_payload(
    name: str,
    arguments: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    if (
        name == "java_runtime"
        and payload.get("ok") is False
        and str(payload.get("error", "")).startswith(
            "No active debug suspension."
        )
    ):
        payload = dict(payload)
        payload["error"] = "No active debug suspension."
        payload["error_code"] = "NO_ACTIVE_SUSPENSION"
        payload["retryable"] = True
        payload["suggested_next_step"] = _NO_ACTIVE_SUSPENSION_NEXT_STEP
    if (
        name == "java_runtime"
        and arguments.get("action") == "variables"
        and payload.get("ok") is True
        and "observation_state" not in payload
    ):
        payload = dict(payload)
        payload["observation_state"] = _variables_observation_state(payload)
    return payload


class RuntimeMCPBoundary:
    """Serialize MCP calls and adapt Dispatcher dictionaries to MCP results."""

    def __init__(
        self,
        dispatcher: DispatchesRuntimeTools | None = None,
        *,
        session_key: str = "default",
        wait_poll_interval: float = 0.2,
        cancellation_grace_seconds: float = 3.0,
        unclaimed_suspension_grace_seconds: float = 45.0,
        completed_wait_limit: int = 32,
    ) -> None:
        self.dispatcher = dispatcher if dispatcher is not None else Dispatcher()
        self.session_key = session_key
        self._call_lock = anyio.Lock()
        self._shutdown_lock = anyio.Lock()
        self._state_lock = threading.Lock()
        self._closing = False
        self._closed = False
        self._poisoned_reason = ""
        self._wait_generation = 0
        self._current_waiter: WaitControl | None = None
        self._completed_waits: dict[str, WaitControl] = {}
        self._http_triggers: dict[str, HTTPTriggerControl] = {}
        self._wait_poll_interval = wait_poll_interval
        self._cancellation_grace_seconds = cancellation_grace_seconds
        self._unclaimed_suspension_grace_seconds = (
            unclaimed_suspension_grace_seconds
        )
        self._completed_wait_limit = max(completed_wait_limit, 1)

    def _register_http_trigger(
        self,
        wait_handle: str,
        trigger: HTTPTriggerControl,
    ) -> None:
        with self._state_lock:
            self._http_triggers[wait_handle] = trigger

    def _http_trigger_finished(
        self,
        wait_handle: str,
        trigger: HTTPTriggerControl,
    ) -> None:
        with self._state_lock:
            if self._http_triggers.get(wait_handle) is trigger:
                self._http_triggers.pop(wait_handle, None)

    def _cancel_http_trigger(
        self,
        control: WaitControl,
        *,
        reason: str,
    ) -> None:
        trigger = control.trigger_control
        if trigger is None:
            return
        trigger.cancel_client_wait(reason)

    def _cancel_all_http_triggers(self, *, reason: str) -> int:
        with self._state_lock:
            triggers = list(self._http_triggers.values())
        for trigger in triggers:
            trigger.cancel_client_wait(reason)
        return len(triggers)

    async def list_tools(self) -> list[types.Tool]:
        return get_mcp_tools()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        request_id: str | None = None,
    ) -> types.CallToolResult:
        with self._state_lock:
            if self._closing:
                return _call_tool_result(_closing_payload())

        validator = _TOOL_VALIDATORS.get(name)
        if validator is None:
            raise ValueError(f"Unknown tool: {name}")

        args = arguments or {}
        validation_errors = sorted(
            validator.iter_errors(args),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )
        if validation_errors:
            return _call_tool_result(
                _invalid_argument_payload(validation_errors[0], args)
            )

        if (
            name == "java_runtime"
            and args.get("http_trigger") is not None
            and args.get("action") != "wait_event"
        ):
            return _call_tool_result(_wait_argument_error(
                "http_trigger",
                "http_trigger is only valid for wait_event with wait_mode='arm'.",
                "Remove http_trigger or use action='wait_event' and wait_mode='arm'.",
            ))

        if name == "java_runtime" and args.get("action") == "wait_event":
            return await self._route_wait_event(
                name,
                args,
                request_id=request_id,
            )

        try:
            async with self._call_lock:
                with self._state_lock:
                    if self._closing or self._closed:
                        return _call_tool_result(_closing_payload())

                if name == "java_runtime":
                    active = self._active_background_waiter()
                    action = str(args.get("action", ""))
                    if active is not None:
                        if action in {
                            "cleanup_debug_state",
                            "stop",
                            "restart",
                            "detach",
                        }:
                            cancelled = await self._cancel_background_wait(
                                active,
                                reason=f"superseded_by_{action}",
                            )
                            if not cancelled:
                                return _call_tool_result({
                                    "ok": False,
                                    "error": (
                                        "The active wait_event could not be "
                                        "cancelled safely."
                                    ),
                                    "error_code": "WAIT_CANCEL_FAILED",
                                    "retryable": False,
                                    "wait_handle": active.wait_handle,
                                    "suggested_next_step": (
                                        "Restart the MCP server before issuing "
                                        "another Runtime action."
                                    ),
                                })
                        else:
                            return _call_tool_result(
                                _active_waiter_payload(active)
                            )
                    if action in {
                        "cleanup_debug_state",
                        "stop",
                        "restart",
                        "detach",
                    }:
                        self._cancel_all_http_triggers(
                            reason=f"superseded_by_{action}",
                        )
                payload = await anyio.to_thread.run_sync(
                    lambda: self.dispatcher.dispatch(
                        name,
                        args,
                        session_key=self.session_key,
                    ),
                    abandon_on_cancel=False,
                )
                if (
                    name == "java_runtime"
                    and action in {
                        "cleanup_debug_state",
                        "stop",
                        "restart",
                        "detach",
                    }
                    and payload.get("ok") is True
                ):
                    self._invalidate_completed_waits(
                        reason=f"superseded_by_{action}",
                    )
                    if action == "cleanup_debug_state":
                        payload = self._augment_cleanup_verification(payload)
        except (TypeError, ValueError) as error:
            payload = _argument_parsing_error_payload(error)
        except Exception as error:
            logger.exception("mcp.tool.dispatch_failed tool=%s", name)
            payload = _execution_error_payload(error)

        payload = _normalize_mcp_payload(name, args, payload)
        return _call_tool_result(payload)

    def _start_waiter(
        self,
        request_id: str | None,
        *,
        wait_handle: str = "",
        background: bool = False,
        event_timeout: float = 30.0,
    ) -> WaitControl:
        with self._state_lock:
            if self._closing:
                raise RuntimeError("SERVER_SHUTTING_DOWN")
            if self._current_waiter is not None:
                raise RuntimeError("A wait_event worker is already active")
            self._wait_generation += 1
            control = WaitControl(
                waiter_id=request_id or f"local_{uuid.uuid4().hex}",
                wait_generation=self._wait_generation,
                poll_interval=self._wait_poll_interval,
                wait_handle=wait_handle,
                background=background,
                event_timeout=event_timeout,
            )
            self._current_waiter = control
            return control

    def _active_background_waiter(self) -> WaitControl | None:
        with self._state_lock:
            control = self._current_waiter
            if (
                control is None
                or not control.background
                or control.worker_done
            ):
                return None
            return control

    def _remember_completed_wait(self, control: WaitControl) -> None:
        if not control.wait_handle:
            return
        with self._state_lock:
            self._completed_waits[control.wait_handle] = control
            while len(self._completed_waits) > self._completed_wait_limit:
                oldest = next(iter(self._completed_waits))
                self._completed_waits.pop(oldest, None)

    def _background_wait_finished(self, control: WaitControl) -> None:
        with self._state_lock:
            if self._current_waiter is control:
                self._current_waiter = None
        self._remember_completed_wait(control)

    def _find_wait(self, wait_handle: str) -> WaitControl | None:
        with self._state_lock:
            if (
                self._current_waiter is not None
                and self._current_waiter.wait_handle == wait_handle
            ):
                return self._current_waiter
            return self._completed_waits.get(wait_handle)

    def _forget_wait(self, control: WaitControl) -> None:
        with self._state_lock:
            if self._current_waiter is control and control.worker_done:
                self._current_waiter = None
            if control.wait_handle:
                self._completed_waits.pop(control.wait_handle, None)

    def _invalidate_completed_waits(self, *, reason: str) -> None:
        """Prevent lifecycle cleanup from leaving stale suspension results."""
        with self._state_lock:
            controls = list(self._completed_waits.values())
            self._completed_waits.clear()
        for control in controls:
            control.request_cancel(reason)

    def _augment_cleanup_verification(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Add MCP wait/trigger state to Runtime cleanup verification."""
        with self._state_lock:
            current_waiter = self._current_waiter
            triggers = list(self._http_triggers.values())

        # A trigger completion callback takes the trigger lock before this
        # boundary lock, so snapshots must be read after releasing it.
        trigger_snapshots = [trigger.snapshot() for trigger in triggers]
        active_wait = bool(
            current_waiter is not None and not current_waiter.worker_done
        )
        pending_http_clients = sum(
            snapshot.get("status")
            in {"running", "client_wait_cancel_requested"}
            for snapshot in trigger_snapshots
        )
        cancel_requested = sum(
            snapshot.get("client_wait_cancel_requested") is True
            for snapshot in trigger_snapshots
        )
        server_execution_unknown = sum(
            snapshot.get("server_execution_state") == "unknown"
            for snapshot in trigger_snapshots
        )

        combined = dict(payload)
        verification = dict(combined.get("verification") or {})
        verification.update({
            "active_wait": active_wait,
            "http_trigger_client_wait_count": pending_http_clients,
            "http_trigger_cancel_requested_count": cancel_requested,
            "http_trigger_server_execution_unknown_count": (
                server_execution_unknown
            ),
        })
        combined["verification"] = verification

        warnings = list(combined.get("warnings") or [])
        if active_wait:
            combined["verification_state"] = "partial"
            warning = (
                "A Runtime wait worker remained active after debug cleanup."
            )
            if warning not in warnings:
                warnings.append(warning)
        if pending_http_clients:
            combined["http_trigger_cleanup_state"] = "settling"
            warning = (
                "A local HTTP client cancellation is settling in the "
                "background; server-side request execution may already have "
                "started and cannot be inferred from client cancellation."
            )
            if warning not in warnings:
                warnings.append(warning)
        else:
            combined["http_trigger_cleanup_state"] = "complete"
        combined["warnings"] = warnings

        if combined.get("verification_state") == "complete":
            combined["suggested_next_step"] = (
                "Debug state is clean. Configure another observation when "
                "needed."
                if not pending_http_clients
                else
                "Debug state is clean. The local HTTP client cancellation is "
                "settling in the background; do not assume that server-side "
                "business execution was undone."
            )
        return combined

    def _clear_waiter(self, control: WaitControl) -> None:
        with self._state_lock:
            if self._current_waiter is control:
                if control.worker_done:
                    self._current_waiter = None
                else:
                    self._closing = True
                    self._poisoned_reason = "wait_event_worker_stuck"

    def _poison(self, reason: str, control: WaitControl | None = None) -> None:
        if control is not None:
            control.mark_dirty()
        with self._state_lock:
            self._closing = True
            if not self._poisoned_reason:
                self._poisoned_reason = reason

    def _run_wait_worker(
        self,
        name: str,
        arguments: dict[str, Any],
        control: WaitControl,
    ) -> dict[str, Any]:
        try:
            return self.dispatcher.dispatch(
                name,
                arguments,
                session_key=self.session_key,
                wait_control=control,
            )
        finally:
            control.mark_worker_done()

    def _run_background_wait_worker(
        self,
        name: str,
        arguments: dict[str, Any],
        control: WaitControl,
    ) -> None:
        try:
            payload = self.dispatcher.dispatch(
                name,
                arguments,
                session_key=self.session_key,
                wait_control=control,
            )
            suspension_id = payload.get("suspension_id")
            has_suspension = bool(
                payload.get("ok") is True
                and isinstance(suspension_id, str)
                and suspension_id
            )
            if not has_suspension:
                self._cancel_http_trigger(
                    control,
                    reason="runtime_wait_finished_without_suspension",
                )
            control.publish_result(payload)

            if not has_suspension:
                return

            deadline = (
                time.monotonic()
                + self._unclaimed_suspension_grace_seconds
            )
            while True:
                if control.result_claimed or control.cancelled:
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                control.wait_until_result_claimed(min(remaining, 0.05))

            if not control.expire_unclaimed_result():
                # An await caller won ownership at the lease boundary.
                return
            control.request_cancel("unclaimed_wait_result_expired")
            resumed = False
            try:
                resume_payload = self.dispatcher.dispatch(
                    "java_runtime",
                    {
                        "action": "resume",
                        "suspension_id": suspension_id,
                    },
                    session_key=self.session_key,
                )
                resumed = resume_payload.get("ok") is True
            except Exception:
                logger.exception(
                    "mcp.wait_event.unclaimed_resume_failed wait_handle=%s",
                    control.wait_handle,
                )
            if not resumed:
                # The wait worker has finished reading the event, but this
                # background wrapper has not yet published worker_done.  Do
                # not call settle_cancelled_wait(), whose contract requires
                # worker_done.  Closing JDWP is the safe final fallback and
                # resumes debugger-owned suspensions without killing the JVM.
                try:
                    resumed = self.dispatcher.interrupt_wait(self.session_key)
                except Exception:
                    logger.exception(
                        "mcp.wait_event.unclaimed_disconnect_failed "
                        "wait_handle=%s",
                        control.wait_handle,
                    )
                if resumed:
                    control.mark_dirty()
            if not resumed:
                self._poison("unclaimed_suspension_resume_failed", control)

            self._cancel_http_trigger(
                control,
                reason="unclaimed_wait_result_expired",
            )

            control.replace_result({
                "ok": False,
                "error": (
                    "The wait_event result was not collected before its "
                    "delivery safety deadline."
                ),
                "error_code": "WAIT_RESULT_EXPIRED",
                "status": "wait_result_expired",
                "wait_handle": control.wait_handle,
                "invalidated_suspension_id": suspension_id,
                "retryable": bool(resumed),
                "suggested_next_step": (
                    "Start a new wait_event with wait_mode='arm', trigger the "
                    "scenario after the armed response, then await it promptly."
                ),
            })
        except Exception as error:
            logger.exception(
                "mcp.wait_event.background_failed wait_handle=%s",
                control.wait_handle,
            )
            self._cancel_http_trigger(
                control,
                reason="runtime_wait_worker_failed",
            )
            control.publish_result(_execution_error_payload(error))
        finally:
            control.mark_worker_done()
            self._background_wait_finished(control)

    async def _wait_for_worker_exit(
        self,
        control: WaitControl,
    ) -> bool:
        done = await anyio.to_thread.run_sync(
            control.wait_until_worker_done,
            self._cancellation_grace_seconds,
            abandon_on_cancel=False,
        )
        if done:
            return True

        logger.warning(
            "mcp.wait_event.cancel.force_disconnect waiter_id=%s generation=%s",
            control.waiter_id,
            control.wait_generation,
        )
        try:
            interrupted = await anyio.to_thread.run_sync(
                self.dispatcher.interrupt_wait,
                self.session_key,
                abandon_on_cancel=False,
            )
            control.mark_dirty()
            if not interrupted:
                logger.error(
                    "mcp.wait_event.cancel.force_disconnect_failed "
                    "waiter_id=%s reason=runtime_rejected",
                    control.waiter_id,
                )
        except Exception:
            control.mark_dirty()
            logger.exception(
                "mcp.wait_event.cancel.force_disconnect_failed waiter_id=%s",
                control.waiter_id,
            )

        done = await anyio.to_thread.run_sync(
            control.wait_until_worker_done,
            self._cancellation_grace_seconds,
            abandon_on_cancel=False,
        )
        if not done:
            self._poison("wait_event_worker_stuck", control)
            logger.critical(
                "mcp.wait_event.cancel.worker_stuck waiter_id=%s generation=%s",
                control.waiter_id,
                control.wait_generation,
            )
        return done

    async def _settle_cancelled_wait(self, control: WaitControl) -> bool:
        if not await self._wait_for_worker_exit(control):
            return False
        try:
            settled = await anyio.to_thread.run_sync(
                lambda: self.dispatcher.settle_cancelled_wait(
                    control,
                    session_key=self.session_key,
                ),
                abandon_on_cancel=False,
            )
            if not settled:
                self._poison("cancel_settle_failed", control)
                logger.error(
                    "mcp.wait_event.cancel.settle_failed waiter_id=%s "
                    "generation=%s reason=runtime_rejected",
                    control.waiter_id,
                    control.wait_generation,
                )
                return False
            if not control.dirty:
                control.mark_phase("cancelled_clean")
            return True
        except Exception:
            self._poison("cancel_settle_crashed", control)
            logger.exception(
                "mcp.wait_event.cancel.settle_failed waiter_id=%s generation=%s",
                control.waiter_id,
                control.wait_generation,
            )
            return False

    async def _cancel_background_wait(
        self,
        control: WaitControl,
        *,
        reason: str,
    ) -> bool:
        trigger = control.trigger_control
        if trigger is not None:
            trigger.cancel_client_wait(reason)
        control.request_cancel(reason)
        with anyio.CancelScope(shield=True):
            settled = await self._settle_cancelled_wait(control)
        self._forget_wait(control)
        return settled

    async def _route_wait_event(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        request_id: str | None,
    ) -> types.CallToolResult:
        wait_mode = str(arguments.get("wait_mode", "blocking"))
        wait_handle = str(arguments.get("wait_handle", "") or "")
        has_http_trigger = arguments.get("http_trigger") is not None
        if wait_mode == "blocking":
            if has_http_trigger:
                return _call_tool_result(_wait_argument_error(
                    "http_trigger",
                    "http_trigger is only valid with wait_mode='arm'.",
                    "Use wait_mode='arm' or remove http_trigger.",
                ))
            if wait_handle:
                return _call_tool_result(_wait_argument_error(
                    "wait_handle",
                    "wait_handle is only valid with wait_mode='await'.",
                    "Remove wait_handle or set wait_mode to 'await'.",
                ))
            return await self._call_wait_event_blocking(
                name,
                arguments,
                request_id=request_id,
            )
        if wait_mode == "arm":
            if wait_handle:
                return _call_tool_result(_wait_argument_error(
                    "wait_handle",
                    "wait_mode='arm' creates a new wait_handle.",
                    "Remove wait_handle and retry the arm call.",
                ))
            return await self._call_wait_event_arm(
                name,
                arguments,
                request_id=request_id,
            )
        if has_http_trigger:
            return _call_tool_result(_wait_argument_error(
                "http_trigger",
                "http_trigger is only valid with wait_mode='arm'.",
                "Use the trigger attached during arm; remove it from await.",
            ))
        if not wait_handle:
            return _call_tool_result(_wait_argument_error(
                "wait_handle",
                "wait_mode='await' requires the wait_handle returned by arm.",
                "Call wait_event with wait_mode='arm' first, then await its handle.",
            ))
        return await self._call_wait_event_await(
            wait_handle,
            timeout=float(arguments.get("timeout", 30.0)),
            request_id=request_id,
        )

    async def _call_wait_event_blocking(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        request_id: str | None,
    ) -> types.CallToolResult:
        async with self._call_lock:
            try:
                control = self._start_waiter(request_id)
            except RuntimeError as error:
                if str(error) == "SERVER_SHUTTING_DOWN":
                    return _call_tool_result(_closing_payload())
                return _call_tool_result({
                    "ok": False,
                    "error": str(error),
                    "error_code": "ACTIVE_WAITER_EXISTS",
                    "retryable": True,
                    "suggested_next_step": (
                        "Cancel or wait for the current wait_event call to finish."
                    ),
                })

            try:
                payload = await anyio.to_thread.run_sync(
                    self._run_wait_worker,
                    name,
                    arguments,
                    control,
                    abandon_on_cancel=True,
                )
                # Linearize result publication against an MCP cancellation.
                # If cancellation won while the worker result was crossing the
                # thread boundary, enter the existing settlement path before a
                # suspension can be returned to a caller that will not see it.
                await checkpoint_if_cancelled()
                if control.cancelled:
                    await self._settle_cancelled_wait(control)
                    return _call_tool_result(_closing_payload())
                return _call_tool_result(payload)
            except anyio.get_cancelled_exc_class():
                control.request_cancel("mcp_request_cancelled")
                with anyio.CancelScope(shield=True):
                    await self._settle_cancelled_wait(control)
                raise
            except (TypeError, ValueError) as error:
                return _call_tool_result(_argument_parsing_error_payload(error))
            except Exception as error:
                logger.exception("mcp.wait_event.dispatch_failed")
                return _call_tool_result(_execution_error_payload(error))
            finally:
                self._clear_waiter(control)

    async def _call_wait_event_arm(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        request_id: str | None,
    ) -> types.CallToolResult:
        trigger_spec = None
        readiness_warning = ""
        raw_trigger = arguments.get("http_trigger")
        if raw_trigger is not None:
            try:
                trigger_spec = parse_http_trigger(raw_trigger)
            except HTTPTriggerValidationError as error:
                return _call_tool_result(_http_trigger_error_payload(error))

        async with self._call_lock:
            if trigger_spec is not None:
                startup_observer = getattr(
                    self.dispatcher,
                    "startup_observation",
                    None,
                )
                if callable(startup_observer):
                    observation = await anyio.to_thread.run_sync(
                        startup_observer,
                        self.session_key,
                        abandon_on_cancel=False,
                    )
                    startup_state = str(
                        observation.get("startup_state", "unverified")
                    )
                    process_state = str(
                        observation.get("process_state", "")
                    )
                    if (
                        startup_state in {"starting", "failed"}
                        or process_state == "exited"
                    ):
                        return _call_tool_result(
                            _application_not_ready_payload(observation)
                        )
                    if (
                        startup_state == "unverified"
                        and process_state == "running"
                    ):
                        readiness_warning = (
                            "Application readiness is unverified. The HTTP "
                            "trigger was allowed for compatibility; a failed "
                            "request must not be treated as runtime-path evidence."
                        )

            wait_handle = f"wait_{uuid.uuid4().hex[:12]}"
            event_timeout = float(arguments.get("timeout", 30.0))
            try:
                control = self._start_waiter(
                    request_id,
                    wait_handle=wait_handle,
                    background=True,
                    event_timeout=event_timeout,
                )
            except RuntimeError as error:
                if str(error) == "SERVER_SHUTTING_DOWN":
                    return _call_tool_result(_closing_payload())
                active = self._active_background_waiter()
                if active is not None:
                    return _call_tool_result(_active_waiter_payload(active))
                return _call_tool_result({
                    "ok": False,
                    "error": str(error),
                    "error_code": "ACTIVE_WAITER_EXISTS",
                    "retryable": True,
                    "suggested_next_step": (
                        "Cancel or wait for the current wait_event call to finish."
                    ),
                })

            if trigger_spec is not None:
                trigger = HTTPTriggerControl(
                    trigger_spec,
                    on_done=lambda item: self._http_trigger_finished(
                        wait_handle,
                        item,
                    ),
                )
                control.attach_trigger(trigger)
                self._register_http_trigger(wait_handle, trigger)

            runtime_arguments = {
                key: value
                for key, value in arguments.items()
                if key not in {"wait_mode", "wait_handle", "http_trigger"}
            }
            worker = threading.Thread(
                target=self._run_background_wait_worker,
                args=(name, runtime_arguments, control),
                name=f"jolink-wait-{wait_handle}",
                daemon=True,
            )
            worker.start()

            try:
                ready = await anyio.to_thread.run_sync(
                    control.wait_until_ready,
                    max(min(event_timeout, 30.0), 5.0),
                    abandon_on_cancel=False,
                )
                await checkpoint_if_cancelled()
                if not ready:
                    await self._cancel_background_wait(
                        control,
                        reason="wait_arm_timeout",
                    )
                    return _call_tool_result({
                        "ok": False,
                        "error": "wait_event did not finish arming before the setup deadline.",
                        "error_code": "WAIT_ARM_TIMEOUT",
                        "retryable": True,
                        "wait_handle": wait_handle,
                        "suggested_next_step": (
                            "Check status and stderr logs, then retry wait_mode='arm'."
                        ),
                    })

                result = control.result_copy()
                if control.armed_at is None:
                    self._cancel_http_trigger(
                        control,
                        reason="wait_ended_before_arm",
                    )
                    if result is None:
                        result = _execution_error_payload(RuntimeError(
                            "wait_event ended before arming without a result"
                        ))
                    control.claim_result()
                    await anyio.to_thread.run_sync(
                        control.wait_until_worker_done,
                        self._cancellation_grace_seconds,
                        abandon_on_cancel=False,
                    )
                    self._forget_wait(control)
                    return _call_tool_result(
                        _with_http_trigger(result, control)
                    )

                trigger = control.trigger_control
                if trigger is not None:
                    if control.claim_trigger_start():
                        trigger.start()
                    else:
                        trigger.skip_event_already_ready()

                expires_at = control.armed_at + event_timeout
                result_ready = control.result_copy() is not None
                if trigger is not None:
                    suggested_next_step = (
                        "The local HTTP trigger is managed by joLink. Call "
                        "wait_event with wait_mode='await' and this wait_handle "
                        "now; do not send the HTTP request again."
                    )
                else:
                    suggested_next_step = (
                        "The wait result is already ready. Call wait_event with "
                        "wait_mode='await' and this wait_handle now; do not "
                        "trigger the scenario again."
                        if result_ready
                        else
                        "Start the target scenario without waiting for its "
                        "response, then call wait_event with wait_mode='await' "
                        "and this wait_handle immediately."
                    )
                payload = {
                    "ok": True,
                    "status": "armed",
                    "wait_handle": wait_handle,
                    "armed_at": _iso_timestamp(control.armed_at),
                    "expires_at": _iso_timestamp(expires_at),
                    "timeout_seconds": event_timeout,
                    "armed_breakpoint_ids": list(
                        control.armed_breakpoint_ids
                    ),
                    "armed_exception_ids": list(
                        control.armed_exception_ids
                    ),
                    "result_ready": result_ready,
                    "suggested_next_step": suggested_next_step,
                }
                if trigger is not None:
                    payload["required_next_action"] = {
                        "action": "wait_event",
                        "wait_mode": "await",
                        "wait_handle": wait_handle,
                    }
                if readiness_warning:
                    payload.setdefault("warnings", []).append(
                        readiness_warning
                    )
                return _call_tool_result(
                    _with_http_trigger(payload, control)
                )
            except anyio.get_cancelled_exc_class():
                with anyio.CancelScope(shield=True):
                    await self._cancel_background_wait(
                        control,
                        reason="mcp_arm_request_cancelled",
                    )
                raise

    async def _call_wait_event_await(
        self,
        wait_handle: str,
        *,
        timeout: float,
        request_id: str | None,
    ) -> types.CallToolResult:
        control = self._find_wait(wait_handle)
        if control is None:
            return _call_tool_result({
                "ok": False,
                "error": f"Unknown or expired wait_handle: {wait_handle}",
                "error_code": "WAIT_HANDLE_NOT_FOUND",
                "retryable": True,
                "wait_handle": wait_handle,
                "suggested_next_step": (
                    "Call wait_event with wait_mode='arm' to start a new observation."
                ),
            })

        if not control.background:
            return _call_tool_result(_wait_argument_error(
                "wait_handle",
                "The supplied wait_handle does not identify a two-phase wait.",
                "Use the handle returned by wait_mode='arm'.",
            ))

        await_owner = request_id or f"local_await_{uuid.uuid4().hex}"
        if not control.claim_await(await_owner):
            return _call_tool_result({
                "ok": False,
                "error": "Another await call already owns this wait_handle.",
                "error_code": "WAIT_HANDLE_IN_USE",
                "retryable": True,
                "wait_handle": wait_handle,
                "suggested_next_step": (
                    "Wait for the current await call to finish, then retry with "
                    "the same wait_handle if the observation is still active."
                ),
            })

        deadline = anyio.current_time() + max(timeout, 0.1)
        try:
            while control.result_copy() is None:
                trigger = control.trigger_control
                if trigger is not None and trigger.definite_failure:
                    async with self._call_lock:
                        # A Runtime event always wins over a simultaneous HTTP
                        # client failure.  The WaitControl state lock provides
                        # the exact publication/cancellation linearization.
                        if not control.cancel_if_result_pending(
                            "http_trigger_failed_before_send"
                        ):
                            break
                        await self._cancel_background_wait(
                            control,
                            reason="http_trigger_failed_before_send",
                        )
                        return _call_tool_result(
                            _trigger_failed_payload(control)
                        )

                remaining = deadline - anyio.current_time()
                if remaining <= 0:
                    payload = {
                        "ok": True,
                        "status": "waiting",
                        "wait_handle": wait_handle,
                        "wait_phase": control.phase,
                        "suggested_next_step": (
                            "Call wait_event with wait_mode='await' and the same "
                            "wait_handle again, or call cleanup_debug_state to cancel it."
                        ),
                    }
                    return _call_tool_result(
                        _with_http_trigger(payload, control)
                    )

                await anyio.to_thread.run_sync(
                    control.wait_until_result,
                    min(remaining, max(self._wait_poll_interval, 0.01)),
                    abandon_on_cancel=False,
                )
                await checkpoint_if_cancelled()

            async with self._call_lock:
                if self._find_wait(wait_handle) is not control:
                    if control.cancelled:
                        return _call_tool_result({
                            "ok": False,
                            "error": "The wait_event observation was cancelled.",
                            "error_code": "WAIT_CANCELLED",
                            "retryable": True,
                            "wait_handle": wait_handle,
                            "suggested_next_step": (
                                "Start a new observation with wait_mode='arm' if needed."
                            ),
                        })
                    return _call_tool_result({
                        "ok": False,
                        "error": f"Unknown or expired wait_handle: {wait_handle}",
                        "error_code": "WAIT_HANDLE_NOT_FOUND",
                        "retryable": True,
                        "wait_handle": wait_handle,
                        "suggested_next_step": (
                            "Call wait_event with wait_mode='arm' to start a new observation."
                        ),
                    })

                payload = control.result_copy()
                if payload is None:
                    return _call_tool_result(_with_http_trigger({
                        "ok": True,
                        "status": "waiting",
                        "wait_handle": wait_handle,
                        "wait_phase": control.phase,
                        "suggested_next_step": (
                            "Call wait_event with wait_mode='await' and the same "
                            "wait_handle again."
                        ),
                    }, control))

                claimed = control.claim_result()
                await anyio.to_thread.run_sync(
                    control.wait_until_worker_done,
                    self._cancellation_grace_seconds,
                    abandon_on_cancel=False,
                )
                if not claimed:
                    # Safety expiration won at the same instant as await.
                    # Re-read only after auto-resume has replaced the stale
                    # hit with WAIT_RESULT_EXPIRED.
                    payload = control.result_copy() or _execution_error_payload(
                        RuntimeError("wait_event result ownership was lost")
                    )
                self._forget_wait(control)
                payload = dict(payload)
                payload.setdefault("wait_handle", wait_handle)
                payload = _with_http_trigger(payload, control)
                await checkpoint_if_cancelled()
                return _call_tool_result(payload)
        except anyio.get_cancelled_exc_class():
            with anyio.CancelScope(shield=True):
                async with self._call_lock:
                    if self._find_wait(wait_handle) is control:
                        await self._cancel_background_wait(
                            control,
                            reason="mcp_await_request_cancelled",
                        )
            raise
        finally:
            control.release_await(await_owner)

    async def shutdown(self) -> None:
        """Cancel active waits, then close the existing Runtime session once."""
        async with self._shutdown_lock:
            with self._state_lock:
                if self._closed:
                    return
                self._closing = True
                control = self._current_waiter
                if control is not None:
                    control.request_cancel("server_shutdown")

            self._cancel_all_http_triggers(reason="server_shutdown")

            worker_done = True
            if control is not None and not control.worker_done:
                with anyio.CancelScope(shield=True):
                    worker_done = await self._wait_for_worker_exit(control)

            normal_close_completed = False
            lock_acquired = False
            if worker_done:
                with anyio.move_on_after(
                    self._cancellation_grace_seconds
                ):
                    await self._call_lock.acquire()
                    lock_acquired = True
                if lock_acquired:
                    close_timed_out = False
                    try:
                        with anyio.move_on_after(
                            self._cancellation_grace_seconds
                        ) as close_scope:
                            close_result = await anyio.to_thread.run_sync(
                                self.dispatcher.close_session,
                                self.session_key,
                                abandon_on_cancel=True,
                            )
                        normal_close_completed = (
                            not close_scope.cancel_called
                            and bool(close_result)
                        )
                        close_timed_out = close_scope.cancel_called
                    except Exception:
                        logger.exception(
                            "mcp.runtime.shutdown_failed context=%s",
                            self.session_key,
                        )
                    finally:
                        self._call_lock.release()

                    if close_timed_out:
                        try:
                            await anyio.to_thread.run_sync(
                                self.dispatcher.interrupt_wait,
                                self.session_key,
                                abandon_on_cancel=False,
                            )
                            normal_close_completed = (
                                await anyio.to_thread.run_sync(
                                    self.dispatcher.wait_for_close_session,
                                    self.session_key,
                                    self._cancellation_grace_seconds,
                                    abandon_on_cancel=False,
                                )
                            )
                        except Exception:
                            logger.exception(
                                "mcp.runtime.shutdown.close_settle_failed "
                                "context=%s",
                                self.session_key,
                            )

            if not normal_close_completed:
                self._poison("shutdown_forced", control)
                logger.warning(
                    "mcp.runtime.shutdown.force context=%s "
                    "worker_done=%s lock_acquired=%s",
                    self.session_key,
                    worker_done,
                    lock_acquired,
                )
                try:
                    with anyio.move_on_after(
                        self._cancellation_grace_seconds
                    ) as force_scope:
                        await anyio.to_thread.run_sync(
                            self.dispatcher.force_close_session,
                            self.session_key,
                            abandon_on_cancel=True,
                        )
                    if force_scope.cancel_called:
                        logger.critical(
                            "mcp.runtime.shutdown.force_timeout context=%s",
                            self.session_key,
                        )
                except Exception:
                    logger.exception(
                        "mcp.runtime.shutdown.force_failed context=%s",
                        self.session_key,
                    )

            with self._state_lock:
                self._closed = True
                if control is None or control.worker_done:
                    self._current_waiter = None
                self._completed_waits.clear()
                self._http_triggers.clear()


def create_mcp_server(
    dispatcher: DispatchesRuntimeTools | None = None,
) -> Server:
    """Create the official low-level MCP Server with Runtime handlers."""
    boundary = RuntimeMCPBoundary(dispatcher)

    @asynccontextmanager
    async def lifespan(_server: Server):
        try:
            yield boundary
        finally:
            with anyio.CancelScope(shield=True):
                await boundary.shutdown()

    server = Server(
        SERVER_NAME,
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
        lifespan=lifespan,
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return await boundary.list_tools()

    @server.call_tool(validate_input=False)
    async def call_tool(
        name: str,
        arguments: dict[str, Any],
    ) -> types.CallToolResult:
        try:
            request_id = str(server.request_context.request_id)
        except LookupError:
            request_id = None
        return await boundary.call_tool(
            name,
            arguments,
            request_id=request_id,
        )

    return server


__all__ = [
    "SERVER_INSTRUCTIONS",
    "SERVER_NAME",
    "RuntimeMCPBoundary",
    "create_mcp_server",
]
