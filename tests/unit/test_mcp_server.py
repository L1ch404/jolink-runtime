from __future__ import annotations

import json
from typing import Any

import anyio
import mcp.types as types

from jolink_runtime.server.mcp_server import RuntimeMCPBoundary


class _FakeDispatcher:
    def __init__(
        self,
        response: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response if response is not None else {"ok": True}
        self.error = error
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        session_key: str = "default",
    ) -> dict[str, Any]:
        args = arguments or {}
        self.calls.append((tool_name, args, session_key))
        if self.error is not None:
            raise self.error
        return self.response


def _text_payload(result: types.CallToolResult) -> dict[str, Any]:
    assert len(result.content) == 1
    content = result.content[0]
    assert isinstance(content, types.TextContent)
    return json.loads(content.text)


def test_success_returns_json_text_and_structured_content() -> None:
    payload = {"ok": True, "value": "男", "observation_state": "complete"}
    dispatcher = _FakeDispatcher(payload)
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario() -> types.CallToolResult:
        return await boundary.call_tool("java_runtime", {"action": "status"})

    result = anyio.run(scenario)

    assert result.isError is False
    assert result.structuredContent == payload
    assert _text_payload(result) == payload
    assert "男" in result.content[0].text
    assert dispatcher.calls == [
        ("java_runtime", {"action": "status"}, "default")
    ]


def test_runtime_ok_false_maps_to_mcp_is_error_true() -> None:
    payload = {
        "ok": False,
        "error": "No active suspension",
        "error_code": "NO_ACTIVE_SUSPENSION",
    }
    boundary = RuntimeMCPBoundary(_FakeDispatcher(payload))

    async def scenario() -> types.CallToolResult:
        return await boundary.call_tool(
            "java_runtime",
            {"action": "resume", "suspension_id": "susp_missing"},
        )

    result = anyio.run(scenario)

    assert result.isError is True
    assert result.structuredContent == payload
    assert _text_payload(result) == payload


def test_partial_or_unavailable_observation_is_not_a_tool_error() -> None:
    for state in ("partial", "unavailable"):
        payload = {
            "ok": True,
            "observation_state": state,
            "warnings": ["Local variable metadata is unavailable."],
        }
        boundary = RuntimeMCPBoundary(_FakeDispatcher(payload))

        async def scenario() -> types.CallToolResult:
            return await boundary.call_tool(
                "java_runtime",
                {"action": "variables"},
            )

        result = anyio.run(scenario)
        assert result.isError is False
        assert result.structuredContent == payload


def test_variables_observation_state_is_derived_from_value_states() -> None:
    cases = [
        (
            {
                "ok": True,
                "complete": True,
                "partial": False,
                "variables": [
                    {"name": "a", "value_state": "observed", "value": 1},
                    {"name": "b", "value_state": "observed", "value": None},
                ],
            },
            "complete",
        ),
        (
            {
                "ok": True,
                "complete": False,
                "partial": True,
                "variables": [
                    {"name": "a", "value_state": "unavailable"},
                    {"name": "b", "value_state": "unavailable"},
                ],
            },
            "unavailable",
        ),
        (
            {
                "ok": True,
                "complete": True,
                "partial": False,
                "variables": [
                    {"name": "a", "value_state": "observed", "value": 1},
                    {"name": "items", "value_state": "partial", "value": {}},
                ],
            },
            "partial",
        ),
    ]

    async def scenario(
        payload: dict[str, Any],
    ) -> types.CallToolResult:
        return await RuntimeMCPBoundary(
            _FakeDispatcher(payload)
        ).call_tool("java_runtime", {"action": "variables"})

    for payload, expected in cases:
        result = anyio.run(scenario, payload)
        assert result.isError is False
        assert result.structuredContent["observation_state"] == expected


def test_empty_variables_are_complete_only_when_runtime_read_succeeded() -> None:
    complete = {
        "ok": True,
        "complete": True,
        "partial": False,
        "variables": [],
        "getvalues_error": None,
    }
    incomplete = {
        "ok": True,
        "complete": False,
        "partial": True,
        "variables": [],
        "getvalues_error": "StackFrame/GetValues failed",
    }

    async def scenario(
        payload: dict[str, Any],
    ) -> types.CallToolResult:
        return await RuntimeMCPBoundary(
            _FakeDispatcher(payload)
        ).call_tool("java_runtime", {"action": "variables"})

    complete_result = anyio.run(scenario, complete)
    unavailable_result = anyio.run(scenario, incomplete)

    assert complete_result.structuredContent["observation_state"] == "complete"
    assert (
        unavailable_result.structuredContent["observation_state"]
        == "unavailable"
    )


def test_java_processes_payload_without_ok_remains_successful() -> None:
    payload = {"message": "Found 0 Java process(es)", "count": 0, "processes": []}
    boundary = RuntimeMCPBoundary(_FakeDispatcher(payload))

    async def scenario() -> types.CallToolResult:
        return await boundary.call_tool("java_processes", {})

    result = anyio.run(scenario)

    assert result.isError is False
    assert result.structuredContent == payload


def test_schema_validation_returns_structured_invalid_argument_error() -> None:
    dispatcher = _FakeDispatcher()
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario() -> tuple[types.CallToolResult, types.CallToolResult]:
        missing = await boundary.call_tool("java_runtime", {})
        wrong_type = await boundary.call_tool(
            "java_runtime",
            {"action": "status", "max_value_depth": "deep"},
        )
        return missing, wrong_type

    missing, wrong_type = anyio.run(scenario)

    assert missing.isError is True
    assert missing.structuredContent["error_code"] == "INVALID_ARGUMENT"
    assert missing.structuredContent["argument"] == "action"
    assert wrong_type.isError is True
    assert wrong_type.structuredContent["error_code"] == "INVALID_ARGUMENT"
    assert wrong_type.structuredContent["argument"] == "max_value_depth"
    assert dispatcher.calls == []


def test_dispatcher_argument_parse_errors_are_structured() -> None:
    boundary = RuntimeMCPBoundary(
        _FakeDispatcher(error=ValueError("invalid integer"))
    )

    async def scenario() -> types.CallToolResult:
        return await boundary.call_tool("java_runtime", {"action": "status"})

    result = anyio.run(scenario)

    assert result.isError is True
    assert result.structuredContent["error_code"] == "INVALID_ARGUMENT"
    assert result.structuredContent["retryable"] is True


def test_unexpected_dispatcher_errors_are_structured() -> None:
    boundary = RuntimeMCPBoundary(
        _FakeDispatcher(error=RuntimeError("unexpected"))
    )

    async def scenario() -> types.CallToolResult:
        return await boundary.call_tool("java_runtime", {"action": "status"})

    result = anyio.run(scenario)

    assert result.isError is True
    assert result.structuredContent["error_code"] == "MCP_DISPATCH_FAILED"
    assert result.structuredContent["retryable"] is False
