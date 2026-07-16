from __future__ import annotations

import json

from jolink_runtime_debugger.adapters.java.tool_schema import JAVA_RUNTIME_SCHEMA
from jolink_runtime_debugger.server.tool_schema import (
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

    assert actions == list(PUBLIC_RUNTIME_ACTIONS)
    assert len(actions) == 15
    assert "wait_event" in actions
    assert "wait_breakpoint" not in actions
    assert "skill_view" not in runtime.inputSchema["properties"]


def test_java_runtime_description_contains_required_selection_and_safety_signals() -> None:
    assert "local JVM" in JAVA_RUNTIME_DESCRIPTION
    assert "source code, logs, or tests" in JAVA_RUNTIME_DESCRIPTION
    assert "suspension_id" in JAVA_RUNTIME_DESCRIPTION
    assert "resume" in JAVA_RUNTIME_DESCRIPTION
    assert "cleanup_debug_state" in JAVA_RUNTIME_DESCRIPTION


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
