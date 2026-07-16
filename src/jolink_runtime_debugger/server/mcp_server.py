"""Low-level MCP server boundary for the standalone Runtime dispatcher."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import anyio
import mcp.types as types
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from mcp.server.lowlevel import Server

from .. import __version__
from ..core.dispatcher import Dispatcher
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
    ) -> dict[str, Any]:
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


class RuntimeMCPBoundary:
    """Serialize MCP calls and adapt Dispatcher dictionaries to MCP results."""

    def __init__(
        self,
        dispatcher: DispatchesRuntimeTools | None = None,
        *,
        session_key: str = "default",
    ) -> None:
        self.dispatcher = dispatcher if dispatcher is not None else Dispatcher()
        self.session_key = session_key
        self._call_lock = anyio.Lock()

    async def list_tools(self) -> list[types.Tool]:
        return get_mcp_tools()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
    ) -> types.CallToolResult:
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

        try:
            async with self._call_lock:
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

        return _call_tool_result(payload)


def create_mcp_server(
    dispatcher: DispatchesRuntimeTools | None = None,
) -> Server:
    """Create the official low-level MCP Server with Runtime handlers."""
    boundary = RuntimeMCPBoundary(dispatcher)
    server = Server(
        SERVER_NAME,
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return await boundary.list_tools()

    @server.call_tool(validate_input=False)
    async def call_tool(
        name: str,
        arguments: dict[str, Any],
    ) -> types.CallToolResult:
        return await boundary.call_tool(name, arguments)

    return server


__all__ = [
    "SERVER_INSTRUCTIONS",
    "SERVER_NAME",
    "RuntimeMCPBoundary",
    "create_mcp_server",
]
