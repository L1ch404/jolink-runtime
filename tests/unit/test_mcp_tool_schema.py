from __future__ import annotations

import json

from jolink_runtime.adapters.java.tool_schema import JAVA_RUNTIME_SCHEMA
from jolink_runtime.server.tool_schema import (
    JAVA_RUNTIME_DESCRIPTION,
    PUBLIC_RUNTIME_ACTIONS,
    get_mcp_tools,
)


def _serialized_size(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def test_mcp_v01_exposes_only_two_compact_tools() -> None:
    tools = get_mcp_tools()

    assert [tool.name for tool in tools] == [
        "java_runtime",
        "java_processes",
    ]
    assert all(tool.outputSchema is None for tool in tools)


def test_java_runtime_schema_exposes_only_public_v01_actions() -> None:
    runtime = get_mcp_tools()[0]
    actions = runtime.inputSchema["properties"]["action"]["enum"]
    wait_mode = runtime.inputSchema["properties"]["wait_mode"]

    assert runtime.inputSchema["additionalProperties"] is False
    assert actions == list(PUBLIC_RUNTIME_ACTIONS)
    assert len(actions) == 15
    assert "wait_event" in actions
    assert "wait_breakpoint" not in actions
    assert "skill_view" not in runtime.inputSchema["properties"]
    assert wait_mode["enum"] == ["blocking", "arm", "await"]
    assert wait_mode["default"] == "blocking"
    assert "wait_handle" in runtime.inputSchema["properties"]


def test_mcp_v01_schemas_reject_unknown_fields_and_remote_hosts() -> None:
    runtime, processes = get_mcp_tools()
    host = runtime.inputSchema["properties"]["host"]

    assert runtime.inputSchema["additionalProperties"] is False
    assert processes.inputSchema["additionalProperties"] is False
    assert host["enum"] == ["127.0.0.1", "localhost"]


def test_java_runtime_description_contains_required_selection_and_safety_signals() -> None:
    runtime_description = get_mcp_tools()[0].description or ""
    assert runtime_description == JAVA_RUNTIME_DESCRIPTION
    normalized = runtime_description.lower()
    for signal in (
        "run, operate, observe, and debug",
        "local java application",
        "launch",
        "restart",
        "status and logs",
        "verify tests",
        "endpoints",
        "actual behavior",
        "breakpoints",
        "exception watches",
        "stack frames",
        "variables",
        "resume",
        "cleanup_debug_state",
    ):
        assert signal in normalized


def test_wait_mode_description_contains_two_phase_and_safety_signals() -> None:
    runtime = get_mcp_tools()[0]
    wait_description = (
        runtime.inputSchema["properties"]["wait_mode"]["description"].lower()
    )

    for signal in (
        "two-phase waiting",
        "use arm",
        "the scenario",
        "status=armed",
        "use await",
        "returned wait_handle",
        "blocking",
        "returned suspension_id",
        "resume",
        "cleanup_debug_state",
    ):
        assert signal in wait_description


def test_mcp_schema_budget_is_enforced() -> None:
    serialized = [
        tool.model_dump(by_alias=True, exclude_none=True)
        for tool in get_mcp_tools()
    ]
    runtime_size = _serialized_size(serialized[0])
    total_size = _serialized_size(serialized)

    assert runtime_size <= 5_000
    assert total_size <= 6_000


def test_legacy_lineage_schema_remains_separate_and_unchanged_in_shape() -> None:
    legacy_actions = (
        JAVA_RUNTIME_SCHEMA["parameters"]["properties"]["action"]["enum"]
    )
    mcp_actions = (
        get_mcp_tools()[0].inputSchema["properties"]["action"]["enum"]
    )

    assert "wait_breakpoint" in legacy_actions
    assert "wait_breakpoint" not in mcp_actions
    assert JAVA_RUNTIME_SCHEMA["parameters"]["required"] == ["action"]
