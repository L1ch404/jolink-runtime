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
from .tool_schema import (
    JAVA_PROCESSES_INPUT_SCHEMA,
    JAVA_RUNTIME_INPUT_SCHEMA,
    get_mcp_tools,
)


logger = logging.getLogger(__name__)

SERVER_NAME = "jolink-runtime"
SERVER_INSTRUCTIONS = (
    "Use java_runtime to run, operate, observe, and debug a local Java application. "
    "Use its runtime evidence to verify code changes against actual behavior. "
    "Use java_processes only when discovering an existing JVM for attach. "
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
    if error.absolute_path:
        return str(next(iter(error.absolute_path)))
    if error.validator == "required":
        missing = [
            name
            for name in error.validator_value
            if name not in arguments
        ]
        if missing:
            return str(missing[0])
    return None


def _invalid_argument_payload(
    error: ValidationError,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": f"Invalid tool arguments: {error.message}",
        "error_code": "INVALID_ARGUMENT",
        "retryable": True,
        "suggested_next_step": "Correct the indicated argument and retry.",
    }
    argument = _validation_argument(error, arguments)
    if argument is not None:
        payload["argument"] = argument
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


def _argument_parsing_error_payload(error: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "error": f"Invalid tool arguments: {type(error).__name__}: {error}",
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
        self._wait_poll_interval = wait_poll_interval
        self._cancellation_grace_seconds = cancellation_grace_seconds
        self._unclaimed_suspension_grace_seconds = (
            unclaimed_suspension_grace_seconds
        )
        self._completed_wait_limit = max(completed_wait_limit, 1)

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
                    if active is not None:
                        action = str(args.get("action", ""))
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
                payload = await anyio.to_thread.run_sync(
                    lambda: self.dispatcher.dispatch(
                        name,
                        args,
                        session_key=self.session_key,
                    ),
                    abandon_on_cancel=False,
                )
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
            control.publish_result(payload)

            suspension_id = payload.get("suspension_id")
            if not (
                payload.get("ok") is True
                and isinstance(suspension_id, str)
                and suspension_id
            ):
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
        if wait_mode == "blocking":
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
        if not wait_handle:
            return _call_tool_result(_wait_argument_error(
                "wait_handle",
                "wait_mode='await' requires the wait_handle returned by arm.",
                "Call wait_event with wait_mode='arm' first, then await its handle.",
            ))
        return await self._call_wait_event_await(
            wait_handle,
            timeout=float(arguments.get("timeout", 30.0)),
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
        async with self._call_lock:
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

            runtime_arguments = {
                key: value
                for key, value in arguments.items()
                if key not in {"wait_mode", "wait_handle"}
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
                    return _call_tool_result(result)

                expires_at = control.armed_at + event_timeout
                return _call_tool_result({
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
                    "result_ready": control.result_copy() is not None,
                    "suggested_next_step": (
                        "Trigger the scenario now, then call wait_event with "
                        "wait_mode='await' and this wait_handle."
                    ),
                })
            except anyio.get_cancelled_exc_class():
                control.request_cancel("mcp_arm_request_cancelled")
                with anyio.CancelScope(shield=True):
                    await self._settle_cancelled_wait(control)
                self._forget_wait(control)
                raise

    async def _call_wait_event_await(
        self,
        wait_handle: str,
        *,
        timeout: float,
    ) -> types.CallToolResult:
        async with self._call_lock:
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

            try:
                result_ready = await anyio.to_thread.run_sync(
                    control.wait_until_result,
                    max(timeout, 0.1),
                    abandon_on_cancel=False,
                )
                await checkpoint_if_cancelled()
                payload = control.result_copy()
                if not result_ready or payload is None:
                    return _call_tool_result({
                        "ok": True,
                        "status": "waiting",
                        "wait_handle": wait_handle,
                        "wait_phase": control.phase,
                        "suggested_next_step": (
                            "Call wait_event with wait_mode='await' and the same "
                            "wait_handle again, or call cleanup_debug_state to cancel it."
                        ),
                    })

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
                await checkpoint_if_cancelled()
                return _call_tool_result(payload)
            except anyio.get_cancelled_exc_class():
                control.request_cancel("mcp_await_request_cancelled")
                with anyio.CancelScope(shield=True):
                    await self._settle_cancelled_wait(control)
                self._forget_wait(control)
                raise

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
