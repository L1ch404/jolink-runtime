from __future__ import annotations

from jolink_runtime.adapters.java.tool_schema import JAVA_RUNTIME_SCHEMA
from jolink_runtime.server.tool_schema import (
    JAVA_RUNTIME_DESCRIPTION,
    PUBLIC_RUNTIME_ACTIONS,
    get_mcp_tools,
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
    http_trigger = runtime.inputSchema["properties"]["http_trigger"]

    assert runtime.inputSchema["additionalProperties"] is False
    assert actions == list(PUBLIC_RUNTIME_ACTIONS)
    assert len(actions) == 15
    assert "wait_event" in actions
    assert "wait_breakpoint" not in actions
    assert "skill_view" not in runtime.inputSchema["properties"]
    assert wait_mode["enum"] == ["blocking", "arm", "await"]
    assert wait_mode["default"] == "blocking"
    assert "wait_handle" in runtime.inputSchema["properties"]
    assert runtime.inputSchema["properties"]["ready_port"]["maximum"] == 65535
    assert (
        runtime.inputSchema["properties"][
            "startup_wait_timeout_seconds"
        ]["default"]
        == 30
    )
    assert (
        runtime.inputSchema["properties"][
            "startup_wait_timeout_seconds"
        ]["maximum"]
        == 60
    )
    assert http_trigger["additionalProperties"] is False
    assert http_trigger["required"] == ["method", "url"]
    assert http_trigger["properties"]["method"]["enum"] == [
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    ]
    assert "json_body" in http_trigger["properties"]


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
        "run, observe, and debug",
        "local java application",
        "launch",
        "restart",
        "status and logs",
        "code changes",
        "actual runtime behavior",
        "runtime evidence",
        "breakpoints",
        "exception watches",
        "stack frames",
        "variables",
        "bounded observations",
        "observed facts",
        "interpretations",
        "unverified conclusions",
        "local http request",
        "ready_port",
        "startup_state",
        "tcp readiness",
        "jdwp",
        "resume",
        "cleanup_debug_state",
    ):
        assert signal in normalized


def test_wait_mode_description_contains_two_phase_and_safety_signals() -> None:
    runtime = get_mcp_tools()[0]
    wait_description = (
        runtime.inputSchema["properties"]["wait_mode"]["description"].lower()
    )
    trigger_description = (
        runtime.inputSchema["properties"]["http_trigger"]["description"].lower()
    )

    for signal in (
        "blocking",
        "arm",
        "trigger",
        "http_trigger",
        "await",
        "one call",
        "wait_handle",
        "jdwp",
        "resume",
    ):
        assert signal in wait_description
    for signal in (
        "blocking",
        "one-call",
        "arm",
        "later await",
        "jdwp",
    ):
        assert signal in trigger_description


def test_suspension_selectors_prefer_the_event_thread_without_rediscovery() -> None:
    properties = get_mcp_tools()[0].inputSchema["properties"]
    thread_description = properties["thread_name"]["description"].lower()
    suspension_description = properties["suspension_id"]["description"].lower()

    for signal in (
        "optional",
        "omit",
        "active suspension",
        "event-hit thread",
        "exact",
        "unique prefix or substring must identify",
        "one jvm thread",
        "must be suspended",
    ):
        assert signal in thread_description
    for signal in (
        "stack",
        "variables",
        "resume",
        "stale",
        "event-hit thread",
        "thread_name is omitted",
    ):
        assert signal in suspension_description


def test_legacy_lineage_schema_remains_separate_and_unchanged_in_shape() -> None:
    legacy_actions = (
        JAVA_RUNTIME_SCHEMA["parameters"]["properties"]["action"]["enum"]
    )
    mcp_actions = (
        get_mcp_tools()[0].inputSchema["properties"]["action"]["enum"]
    )

    assert "wait_breakpoint" in legacy_actions
    assert "wait_breakpoint" not in mcp_actions
    assert (
        "project_path"
        not in JAVA_RUNTIME_SCHEMA["parameters"]["properties"]
    )
    assert (
        "launch_name"
        not in JAVA_RUNTIME_SCHEMA["parameters"]["properties"]
    )
    assert JAVA_RUNTIME_SCHEMA["parameters"]["required"] == ["action"]


def test_project_launch_schema_stays_small_and_optional() -> None:
    properties = get_mcp_tools()[0].inputSchema["properties"]

    assert properties["project_path"]["type"] == "string"
    assert properties["launch_name"]["type"] == "string"
    assert "project_path" not in get_mcp_tools()[0].inputSchema["required"]
    assert "default" not in properties["classpath"]
    assert (
        "exact case-sensitive"
        in properties["launch_name"]["description"].lower()
    )
