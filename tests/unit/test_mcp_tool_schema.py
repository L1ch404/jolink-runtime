from __future__ import annotations

from jolink_runtime.adapters.java.tool_schema import JAVA_RUNTIME_SCHEMA
from jolink_runtime.server.tool_schema import (
    JAVA_APPLICATION_DESCRIPTION,
    PUBLIC_APPLICATION_ACTIONS,
    PUBLIC_DEBUGGER_ACTIONS,
    PUBLIC_STATUS_ACTIONS,
    get_mcp_tools,
)


def test_mcp_product_surface_exposes_three_focused_tools() -> None:
    tools = get_mcp_tools()

    assert [tool.name for tool in tools] == [
        "java_application",
        "java_status",
        "java_debugger",
    ]
    assert all(tool.outputSchema is None for tool in tools)


def test_focused_schemas_expose_only_their_public_actions() -> None:
    application, status, debugger = get_mcp_tools()
    actions = application.inputSchema["properties"]["action"]["enum"]
    wait_mode = debugger.inputSchema["properties"]["wait_mode"]
    http_trigger = debugger.inputSchema["properties"]["http_trigger"]

    assert application.inputSchema["additionalProperties"] is False
    assert actions == list(PUBLIC_APPLICATION_ACTIONS)
    assert status.inputSchema["properties"]["action"]["enum"] == list(
        PUBLIC_STATUS_ACTIONS
    )
    assert debugger.inputSchema["properties"]["action"]["enum"] == list(
        PUBLIC_DEBUGGER_ACTIONS
    )
    assert "wait_breakpoint" not in PUBLIC_DEBUGGER_ACTIONS
    assert wait_mode["enum"] == ["blocking", "arm", "await"]
    assert wait_mode["default"] == "blocking"
    assert "wait_handle" in debugger.inputSchema["properties"]
    assert application.inputSchema["properties"]["ready_port"]["maximum"] == 65535
    assert (
        application.inputSchema["properties"][
            "startup_wait_timeout_seconds"
        ]["default"]
        == 30
    )
    assert (
        application.inputSchema["properties"][
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
    source_files = application.inputSchema["properties"]["source_files"]
    assert source_files["minItems"] == 1
    assert source_files["maxItems"] == 16
    assert source_files["uniqueItems"] is True


def test_product_schemas_reject_unknown_fields_and_remote_hosts() -> None:
    application, status, debugger = get_mcp_tools()
    host = application.inputSchema["properties"]["host"]

    assert all(
        tool.inputSchema["additionalProperties"] is False
        for tool in (application, status, debugger)
    )
    assert host["enum"] == ["127.0.0.1", "localhost"]


def test_java_application_description_contains_lifecycle_and_reload_signals() -> None:
    runtime_description = get_mcp_tools()[0].description or ""
    assert runtime_description == JAVA_APPLICATION_DESCRIPTION
    normalized = runtime_description.lower()
    for signal in (
        "launch",
        "attach",
        "reload",
        "restart",
        "source_files",
        "hotswap",
        "fresh project launch",
    ):
        assert signal in normalized


def test_wait_mode_description_contains_two_phase_and_safety_signals() -> None:
    runtime = get_mcp_tools()[2]
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
    properties = get_mcp_tools()[2].inputSchema["properties"]
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
        get_mcp_tools()[2].inputSchema["properties"]["action"]["enum"]
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
