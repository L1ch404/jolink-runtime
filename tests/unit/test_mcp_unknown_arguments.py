"""MCP argument tolerance is per tool, shallow, and local to one response."""

import json
from copy import deepcopy

import anyio
import pytest

from jolink_runtime.server.mcp_server import RuntimeMCPBoundary, _call_tool_result


class Dispatcher:
    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else {"ok": True, "status": "done"}

    def dispatch(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
        return self.result


def call(boundary, name, arguments):
    async def invoke():
        result = await boundary.call_tool(name, arguments, request_id="request-42")
        assert json.loads(result.content[0].text) == result.structuredContent
        return result

    return anyio.run(invoke)


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("java_application", {"action": "stop"}),
        ("java_fast_test", {"action": "result", "test_run_id": "test-id"}),
        ("java_status", {"action": "status"}),
        ("java_debugger", {"action": "threads"}),
        ("java_runtime", {"action": "status"}),
        ("java_processes", {}),
    ],
)
def test_unknown_fields_are_removed_for_each_tool_without_mutating_input(
    tool, arguments
):
    dispatcher = Dispatcher()
    boundary = RuntimeMCPBoundary(dispatcher)
    request = {
        **arguments,
        "extra": {"private": ["not-for-response"]},
        "_product_status": False,
        "_mcp_background_launch": True,
    }
    before = deepcopy(request)
    response = call(boundary, tool, request)
    assert not response.isError
    assert response.structuredContent["ignored_arguments"] == [
        "_mcp_background_launch",
        "_product_status",
        "extra",
    ]
    assert dispatcher.calls[0][1] == arguments
    assert request == before
    assert "not-for-response" not in response.content[0].text


def test_allowlist_is_specific_to_the_tool():
    boundary = RuntimeMCPBoundary(Dispatcher())
    ignored = call(
        boundary,
        "java_fast_test",
        {"action": "result", "test_run_id": "id", "tail": 50},
    )
    assert ignored.structuredContent["ignored_arguments"] == ["tail"]
    kept = call(boundary, "java_status", {"action": "logs", "tail": 50})
    assert "ignored_arguments" not in kept.structuredContent


@pytest.mark.parametrize(
    "arguments,argument,rule",
    [
        (
            {"project_paht": "/project", "tests": ["ExampleTest"]},
            "project_path",
            "required",
        ),
        (
            {"action": "unknown", "project_path": "/project", "tests": ["ExampleTest"]},
            "action",
            "enum",
        ),
        ({"project_path": "/project", "tests": "ExampleTest"}, "tests", "type"),
        ({"project_path": "/project", "tests": []}, "tests", "minItems"),
        (
            {"project_path": "/project", "tests": ["ExampleTest"], "timeout": -1},
            "timeout",
            "minimum",
        ),
    ],
)
def test_known_argument_errors_still_fail_before_execution(arguments, argument, rule):
    dispatcher = Dispatcher()
    response = call(
        RuntimeMCPBoundary(dispatcher), "java_fast_test", {**arguments, "extra": 1}
    )
    payload = response.structuredContent
    assert response.isError and payload["error_code"] == "INVALID_ARGUMENT"
    assert payload["argument"] == argument and payload["validation_rule"] == rule
    assert "extra" in payload["ignored_arguments"]
    assert dispatcher.calls == []


def test_failure_and_shared_result_are_not_rewritten_or_contaminated():
    original = {
        "ok": False,
        "error_code": "TEST_RUN_NOT_FOUND",
        "error": "not retained",
    }
    dispatcher = Dispatcher(original)
    boundary = RuntimeMCPBoundary(dispatcher)
    arguments = {"action": "result", "test_run_id": "id"}
    annotated = call(boundary, "java_fast_test", {**arguments, "extra": 1})
    assert annotated.isError
    assert annotated.structuredContent == {**original, "ignored_arguments": ["extra"]}
    clean = call(boundary, "java_fast_test", arguments)
    assert clean.isError and clean.structuredContent == original
    assert "ignored_arguments" not in dispatcher.result


def test_nested_http_payload_is_preserved_and_request_id_is_forwarded(monkeypatch):
    boundary = RuntimeMCPBoundary(Dispatcher())
    captured = []

    async def route(name, arguments, *, request_id):
        captured.append((name, arguments, request_id))
        return _call_tool_result({"ok": True, "status": "armed"})

    monkeypatch.setattr(boundary, "_route_wait_event", route)
    arguments = {
        "action": "wait_event",
        "wait_mode": "arm",
        "extra": 1,
        "http_trigger": {
            "method": "POST",
            "url": "http://127.0.0.1:8080/",
            "headers": {"X-Custom": "kept"},
            "json_body": {"extra": "business field", "nested": {"tail": 10}},
        },
    }
    before = deepcopy(arguments)
    response = call(boundary, "java_debugger", arguments)
    assert response.structuredContent == {
        "ok": True,
        "status": "armed",
        "ignored_arguments": ["extra"],
    }
    assert captured[0][1]["http_trigger"] == before["http_trigger"]
    assert captured[0][2] == "request-42"
    assert arguments == before


def test_unknown_nested_configuration_is_not_silently_removed():
    dispatcher = Dispatcher()
    result = call(
        RuntimeMCPBoundary(dispatcher),
        "java_debugger",
        {
            "action": "wait_event",
            "extra": 1,
            "http_trigger": {
                "method": "GET",
                "url": "http://127.0.0.1:8080/",
                "unknown": 1,
            },
        },
    )
    assert result.isError and result.structuredContent["argument"] == "http_trigger"
    assert result.structuredContent["ignored_arguments"] == ["extra"]
    assert dispatcher.calls == []


@pytest.mark.parametrize("closing", [False, True])
def test_early_returns_include_ignored_fields(closing):
    dispatcher = Dispatcher()
    boundary = RuntimeMCPBoundary(dispatcher)
    boundary._closing = closing
    result = call(
        boundary,
        "java_debugger",
        {
            "action": "threads",
            "extra": 1,
            "http_trigger": {"method": "GET", "url": "http://127.0.0.1:8080/"},
        },
    )
    assert result.isError
    assert result.structuredContent["error_code"] == (
        "SERVER_SHUTTING_DOWN" if closing else "INVALID_WAIT_ARGUMENTS"
    )
    assert result.structuredContent["ignored_arguments"] == ["extra"]
    assert not dispatcher.calls


def test_unknown_tool_name_is_not_guessed():
    with pytest.raises(ValueError, match="Unknown tool"):
        call(RuntimeMCPBoundary(Dispatcher()), "java_typo", {"action": "status"})
