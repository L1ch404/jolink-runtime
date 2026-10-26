from __future__ import annotations

from dataclasses import asdict

import anyio
import pytest

from jolink_runtime.core.dispatcher import Dispatcher, parse_runtime_action
from jolink_runtime.core.models import RuntimeAction, RuntimeResult
from jolink_runtime.server.mcp_server import RuntimeMCPBoundary
from jolink_runtime.server.tool_schema import get_mcp_tools


def _boundary_with_evaluator(monkeypatch, response=None):
    dispatcher = Dispatcher()
    runtime = dispatcher.sessions.get_runtime("evaluate-test")
    calls = []

    def evaluate(action):
        calls.append(action)
        return response if response is not None else RuntimeResult(
            data={"value": 42, "value_state": "observed", "suspension_id": "susp_1"}
        )

    monkeypatch.setattr(runtime, "evaluate", evaluate)
    return RuntimeMCPBoundary(dispatcher, session_key="evaluate-test"), calls


def test_evaluate_reuses_debugger_tool_and_value_format_options() -> None:
    tools = {tool.name: tool for tool in get_mcp_tools()}
    debugger = tools["java_debugger"]
    properties = debugger.inputSchema["properties"]

    assert "evaluate" in properties["action"]["enum"]
    assert properties["expression"]["type"] == "string"
    assert {
        "suspension_id", "thread_name", "frame_index", "timeout", "max_value_depth",
        "semantic_collections", "item_limit", "map_entry_limit",
    } <= properties.keys()
    assert "side effects" in properties["expression"]["description"]
    assert "do not automatically retry" in properties["timeout"]["description"]
    for name, tool in tools.items():
        if name != "java_debugger":
            assert "expression" not in tool.inputSchema["properties"]
            assert "evaluate" not in tool.inputSchema["properties"]["action"]["enum"]


def test_expression_extension_preserves_lineage_defaults() -> None:
    default = RuntimeAction(action="variables")
    parsed = parse_runtime_action({"action": "evaluate", "expression": '  "中文" + name  '})

    assert default.expression == ""
    assert parsed.expression == '  "中文" + name  '
    assert "expression" not in asdict(default)
    assert "_expression" not in asdict(parsed)


@pytest.mark.parametrize("tool_name", ["java_debugger", "java_runtime"])
def test_evaluate_dispatches_frame_expression_and_formatting(monkeypatch, tool_name) -> None:
    boundary, calls = _boundary_with_evaluator(monkeypatch)
    arguments = {
        "action": "evaluate",
        "expression": "users.get(0).getName()",
        "suspension_id": "susp_1",
        "thread_name": "http-worker",
        "frame_index": 2,
        "max_value_depth": 3,
        "semantic_collections": False,
        "item_limit": 4,
        "map_entry_limit": 5,
        "timeout": 12,
    }

    async def scenario():
        return await boundary.call_tool(tool_name, arguments)

    result = anyio.run(scenario)
    assert result.isError is False
    assert result.structuredContent["value"] == 42
    assert len(calls) == 1
    for name, value in arguments.items():
        assert getattr(calls[0], name) == value


@pytest.mark.parametrize("tool_name", ["java_debugger", "java_runtime"])
@pytest.mark.parametrize("expression", [None, "", " \n\t ", 42, ["a + b"]])
def test_evaluate_requires_nonblank_string_before_dispatch(monkeypatch, tool_name, expression) -> None:
    boundary, calls = _boundary_with_evaluator(monkeypatch)
    arguments = {"action": "evaluate"}
    if expression is not None:
        arguments["expression"] = expression

    async def scenario():
        return await boundary.call_tool(tool_name, arguments)

    result = anyio.run(scenario)
    assert result.isError is True
    assert result.structuredContent["error_code"] == "INVALID_ARGUMENT"
    assert result.structuredContent["argument"] == "expression"
    assert calls == []


@pytest.mark.parametrize("failed", [False, True])
def test_evaluate_ignores_unknown_fields_without_replaying_or_changing_result(monkeypatch, failed) -> None:
    response = RuntimeResult(
        ok=not failed,
        data={"value": None, "value_state": "unavailable" if failed else "observed"},
        error="Method threw an exception." if failed else "",
    )
    boundary, calls = _boundary_with_evaluator(monkeypatch, response)

    async def scenario():
        return await boundary.call_tool("java_debugger", {
            "action": "evaluate", "expression": "service.run()",
            "invented_evaluation_mode": "private", "maxOutputLength": 8000,
        })

    result = anyio.run(scenario)
    assert result.isError is failed
    assert len(calls) == 1
    assert calls[0].expression == "service.run()"
    assert not hasattr(calls[0], "invented_evaluation_mode")
    assert result.structuredContent["ignored_arguments"] == [
        "invented_evaluation_mode", "maxOutputLength",
    ]
    assert result.structuredContent["value"] is None
    if failed:
        assert result.structuredContent["error"] == "Method threw an exception."


def test_non_evaluation_debug_actions_do_not_require_expression(monkeypatch) -> None:
    boundary, calls = _boundary_with_evaluator(monkeypatch)

    async def scenario():
        return await boundary.call_tool("java_debugger", {"action": "variables"})

    result = anyio.run(scenario)
    assert result.structuredContent["error_code"] == "NO_ACTIVE_SUSPENSION"
    assert calls == []
