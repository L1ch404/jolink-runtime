"""The public Test tool must reach the existing manager, not a new backend."""

import anyio
import pytest

from jolink_runtime.core.dispatcher import Dispatcher
from jolink_runtime.server.mcp_server import RuntimeMCPBoundary
from jolink_runtime.server.tool_schema import get_mcp_tools


def test_test_tool_is_discoverable_and_its_schema_is_focused():
    tools = {tool.name: tool for tool in get_mcp_tools()}
    testing = tools["java_fast_test"]
    assert "Run selected Java tests" in testing.description
    for signal in (
        "Maven",
        "Gradle",
        "JUnit",
        "TestNG",
        "No application",
        "first",
        "minutes",
        "java_status",
    ):
        assert signal.lower() in testing.description.lower()
    properties = testing.inputSchema["properties"]
    assert set(properties) == {
        "action",
        "project_path",
        "tests",
        "source_files",
        "build_system",
        "java_home",
        "maven_home",
        "maven_settings",
        "timeout",
        "test_run_id",
    }
    assert properties["action"]["default"] == "run"
    assert "tail" not in properties
    assert "log_files" in testing.description
    assert "tail" in tools["java_status"].inputSchema["properties"]
    for name in ("java_application", "java_debugger"):
        assert "tests" not in tools[name].inputSchema["properties"]
        assert "test_run_id" not in tools[name].inputSchema["properties"]


@pytest.mark.parametrize("explicit_action", [False, True])
def test_default_and_explicit_run_and_cancel_use_same_manager(
    tmp_path, monkeypatch, explicit_action
):
    dispatcher = Dispatcher()
    runtime = dispatcher.sessions.get_runtime("default")
    manager = runtime._fast_tests
    calls = []

    def start(**kwargs):
        calls.append(("run", kwargs))
        return {
            "ok": True,
            "status": "completed",
            "test_run_id": "test_same",
            "passed": True,
        }

    def cancel(run_id):
        calls.append(("cancel", run_id))
        return {"ok": True, "status": "cancel_requested", "test_run_id": run_id}

    monkeypatch.setattr(manager, "start", start)
    monkeypatch.setattr(manager, "cancel", cancel)
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario():
        arguments = {
            "project_path": str(tmp_path),
            "tests": ["example.Test#works"],
            "timeout": 90,
        }
        if explicit_action:
            arguments["action"] = "run"
        result = await boundary.call_tool("java_fast_test", arguments)
        assert result.structuredContent["test_run_id"] == "test_same"
        assert result.structuredContent["passed"] is True
        cancelled = await boundary.call_tool(
            "java_fast_test", {"action": "cancel", "test_run_id": "test_same"}
        )
        assert cancelled.structuredContent["status"] == "cancel_requested"
        assert dispatcher.sessions.get_runtime("default")._fast_tests is manager
        assert (
            "action" in arguments
        ) is explicit_action  # no mutation of caller inputs

    try:
        anyio.run(scenario)
        assert calls[0][1]["project_path"] == tmp_path
        assert calls[0][1]["tests"] == ["example.Test#works"]
        assert (
            "timeout_seconds" not in calls[0][1]
        )  # Reply budget does not change Runner limit.
        assert calls[1] == ("cancel", "test_same")
    finally:
        dispatcher.close_session()


@pytest.mark.parametrize(
    "tool,args",
    [
        ("java_fast_test", {}),
        ("java_fast_test", {"project_path": "/fixture"}),
        ("java_fast_test", {"tests": ["example.Test"]}),
        ("java_fast_test", {"action": "cancel"}),
        ("java_fast_test", {"action": "result"}),
        (
            "java_fast_test",
            {"action": "test", "project_path": "/fixture", "tests": ["example.Test"]},
        ),
        (
            "java_application",
            {"action": "test", "project_path": "/fixture", "tests": ["example.Test"]},
        ),
        ("java_application", {"action": "cancel_test", "test_run_id": "test_same"}),
    ],
)
def test_invalid_or_old_public_calls_do_not_allocate_runtime(tool, args):
    dispatcher = Dispatcher()
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario():
        result = await boundary.call_tool(tool, args)
        assert result.isError
        assert result.structuredContent["error_code"] == "INVALID_ARGUMENT"
        assert dispatcher.sessions.session_keys == ()

    anyio.run(scenario)


def test_unrelated_launch_parameter_does_not_block_fast_test(tmp_path, monkeypatch):
    dispatcher = Dispatcher()
    manager = dispatcher.sessions.get_runtime()._fast_tests
    calls = []

    def start(**arguments):
        calls.append(arguments)
        return {"ok": True, "status": "completed", "passed": True}

    monkeypatch.setattr(manager, "start", start)
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario():
        result = await boundary.call_tool("java_fast_test", {
            "project_path": str(tmp_path), "tests": ["example.Test"], "ready_port": 8080,
        })
        assert not result.isError and result.structuredContent["passed"]
        assert result.structuredContent["ignored_arguments"] == ["ready_port"]
        assert len(calls) == 1 and calls[0]["project_path"] == tmp_path
        assert "ready_port" not in calls[0]

    try:
        anyio.run(scenario)
    finally:
        dispatcher.close_all_sessions()


def test_unrelated_test_selector_preserves_debugger_business_error():
    dispatcher = Dispatcher()
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario():
        result = await boundary.call_tool("java_debugger", {"action": "threads", "tests": ["example.Test"]})
        assert result.isError
        assert result.structuredContent["error_code"] == "NO_ACTIVE_SUSPENSION"
        assert result.structuredContent["ignored_arguments"] == ["tests"]
        plain = await boundary.call_tool("java_debugger", {"action": "threads"})
        assert plain.structuredContent == {key: value for key, value in result.structuredContent.items()
                                           if key != "ignored_arguments"}

    try:
        anyio.run(scenario)
    finally:
        dispatcher.close_all_sessions()
