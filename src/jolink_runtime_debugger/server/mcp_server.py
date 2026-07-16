"""Low-level MCP server boundary for the standalone Runtime dispatcher."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from contextlib import asynccontextmanager
from typing import Any, Protocol

import anyio
import mcp.types as types
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

SERVER_NAME = "jolink-runtime-debugger"
SERVER_INSTRUCTIONS = (
    "Use java_processes to find a local JVM and java_runtime to observe it. "
    "After inspecting a suspension, resume or clean it up."
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
        self._wait_poll_interval = wait_poll_interval
        self._cancellation_grace_seconds = cancellation_grace_seconds

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
            return await self._call_wait_event(
                name,
                args,
                request_id=request_id,
            )

        try:
            async with self._call_lock:
                with self._state_lock:
                    if self._closing or self._closed:
                        return _call_tool_result(_closing_payload())
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

    def _start_waiter(self, request_id: str | None) -> WaitControl:
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
            )
            self._current_waiter = control
            return control

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

    async def _call_wait_event(
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
