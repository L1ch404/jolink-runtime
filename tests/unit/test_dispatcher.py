from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from jolink_runtime.adapters.java.jdwp_adapter import JavaRuntime
from jolink_runtime.adapters.java.tool_schema import JAVA_RUNTIME_SCHEMA
from jolink_runtime.core import dispatcher as dispatcher_module
from jolink_runtime.core.dispatcher import Dispatcher, parse_runtime_action
from jolink_runtime.core.models import RuntimeResult
from jolink_runtime.core.session_manager import SessionManager


def test_standalone_preserves_runtime_schema_and_observation_guide() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    observation_path = repository_root / "docs" / "java-runtime-observation.md"

    assert JAVA_RUNTIME_SCHEMA["name"] == "java_runtime"
    assert observation_path.is_file()
    observation = observation_path.read_text(encoding="utf-8")
    assert "Distinguish facts, inferences, and unknowns" in observation
    assert "`request_id` for exception events" in observation


def test_runtime_schema_documents_runtime_evidence_boundaries() -> None:
    description = JAVA_RUNTIME_SCHEMA["description"]
    properties = JAVA_RUNTIME_SCHEMA["parameters"]["properties"]

    assert "value_state='observed'" in description
    assert "value_state='partial'" in description
    assert "value_state='unavailable'" in description

    action_description = properties["action"]["description"]
    logs_description = properties["tail"]["description"]
    assert "externally attached JVM" in action_description
    assert "must not be used as evidence" in action_description
    assert "attached JVM's stdout/stderr is not captured" in logs_description

    thread_description = properties["thread_name"]["description"]
    assert "currently suspended thread" in thread_description
    assert "suspended=true" in thread_description

    host_description = properties["host"]["description"]
    assert "localhost" in host_description
    assert "127.0.0.1" in host_description
    assert "::1" not in host_description

    request_id_description = properties["request_id"]["description"]
    assert "breakpoint-hit" in request_id_description
    assert "top level" in request_id_description
    assert "Prefer breakpoint_id" in request_id_description


def test_runtime_is_isolated_by_explicit_session_key() -> None:
    sessions = SessionManager(JavaRuntime)
    dispatcher = Dispatcher(sessions)

    first = dispatcher.dispatch("java_runtime", {"action": "status"}, session_key="a")
    second = dispatcher.dispatch("java_runtime", {"action": "status"}, session_key="b")

    assert first["process_state"] == "absent"
    assert second["process_state"] == "absent"
    assert set(sessions.session_keys) == {"a", "b"}
    assert sessions.get_runtime("a") is not sessions.get_runtime("b")


def test_product_tool_names_map_to_existing_runtime_actions(monkeypatch) -> None:
    dispatcher = Dispatcher()
    runtime = dispatcher.sessions.get_runtime("product-tools")
    observed: list[str] = []

    def record(action):
        observed.append(action.action)
        return RuntimeResult(ok=True, data={"status": action.action})

    monkeypatch.setattr(runtime, "run", record)
    monkeypatch.setattr(runtime, "update", record)
    monkeypatch.setattr(runtime, "status", record)
    monkeypatch.setattr(runtime, "threads", record)

    assert dispatcher.dispatch(
        "java_application",
        {"action": "launch"},
        session_key="product-tools",
    )["status"] == "run"
    assert dispatcher.dispatch(
        "java_application",
        {"action": "reload", "source_files": ["App.java"]},
        session_key="product-tools",
    )["status"] == "update"
    status = dispatcher.dispatch(
        "java_status",
        {"action": "status"},
        session_key="product-tools",
    )
    assert status["status"] == "status"
    assert status["server_diagnostics"]["status"] in {
        "active",
        "stderr_only",
    }
    assert dispatcher.dispatch(
        "java_debugger",
        {"action": "threads"},
        session_key="product-tools",
    )["status"] == "threads"
    assert observed == ["run", "update", "status", "threads"]


def test_dispatcher_logs_lifecycle_without_argument_values(caplog) -> None:
    dispatcher = Dispatcher()
    caplog.set_level(logging.INFO)

    result = dispatcher.dispatch(
        "java_runtime",
        {
            "action": "status",
            "app_args": ["do-not-log-this-value"],
            "vm_args": ["-Dpassword=do-not-log-this-value"],
        },
        session_key="logging-session",
    )

    assert result["process_state"] == "absent"
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "java_runtime.session.created context=logging-session" in messages
    assert "java_runtime.action.start action=status context=logging-session" in messages
    assert "java_runtime.action.finish action=status context=logging-session" in messages
    assert "do-not-log-this-value" not in messages


class _RoutingRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.responses: dict[str, RuntimeResult | Exception] = {}

    def _call(self, method: str, action: Any) -> RuntimeResult:
        self.calls.append((method, action))
        response = self.responses.get(method)
        if isinstance(response, Exception):
            raise response
        if response is not None:
            return response
        return RuntimeResult(data={"status": method})

    def run(self, action: Any) -> RuntimeResult:
        return self._call("run", action)

    def run_project(self, action: Any, request: Any) -> RuntimeResult:
        self.calls.append(("run_project", (action, request)))
        return RuntimeResult(data={"status": "run_project"})

    def stop(self, action: Any) -> RuntimeResult:
        return self._call("stop", action)

    def restart(self, action: Any) -> RuntimeResult:
        return self._call("restart", action)

    def restart_project(self, action: Any, request: Any) -> RuntimeResult:
        self.calls.append(("restart_project", (action, request)))
        return RuntimeResult(data={"status": "restart_project"})

    def attach(self, action: Any) -> RuntimeResult:
        return self._call("attach", action)

    def detach(self, action: Any) -> RuntimeResult:
        return self._call("detach", action)

    def status(self, action: Any) -> RuntimeResult:
        return self._call("status", action)

    def logs(self, action: Any) -> RuntimeResult:
        return self._call("logs", action)

    def breakpoint(self, action: Any) -> RuntimeResult:
        return self._call("breakpoint", action)

    def exception(self, action: Any) -> RuntimeResult:
        return self._call("exception", action)

    def wait_event(self, action: Any) -> RuntimeResult:
        return self._call("wait_event", action)

    def wait_breakpoint(self, action: Any) -> RuntimeResult:
        return self._call("wait_breakpoint", action)

    def threads(self, action: Any) -> RuntimeResult:
        return self._call("threads", action)

    def stack(self, action: Any) -> RuntimeResult:
        return self._call("stack", action)

    def variables(self, action: Any) -> RuntimeResult:
        return self._call("variables", action)

    def resume(self, action: Any) -> RuntimeResult:
        return self._call("resume", action)

    def cleanup_debug_state(self, action: Any) -> RuntimeResult:
        return self._call("cleanup_debug_state", action)

    def update(self, action: Any) -> RuntimeResult:
        return self._call("update", action)


def test_runtime_action_defaults_and_boolean_coercions_are_preserved() -> None:
    defaults = parse_runtime_action({})

    assert defaults.action == "status"
    assert defaults.classpath == "."
    assert defaults.jdwp_port == 5005
    assert defaults.ready_port == 0
    assert defaults.startup_wait_timeout_seconds == 30.0
    assert defaults.startup_wait_timeout_provided is False
    assert defaults.caught is True
    assert defaults.uncaught is True
    assert defaults.semantic_collections is True
    assert defaults.include_proxy is False
    assert defaults.include_generated is False
    assert defaults.include_this is False
    assert defaults.max_value_depth == 1
    assert defaults.item_limit == 16
    assert defaults.map_entry_limit == 16
    assert defaults.timeout == 30.0

    coerced = parse_runtime_action(
        {
            "include_proxy": "yes",
            "include_generated": "unknown",
            "caught": "false",
            "uncaught": "1",
            "allow_broad_caught": "on",
            "include_this": "y",
            "semantic_collections": "0",
            "ready_port": 8080,
            "startup_wait_timeout_seconds": 12,
        }
    )
    assert coerced.include_proxy is True
    assert coerced.include_generated is False
    assert coerced.caught is False
    assert coerced.uncaught is True
    assert coerced.allow_broad_caught is True
    assert coerced.include_this is True
    assert coerced.semantic_collections is False
    assert coerced.ready_port == 8080
    assert coerced.startup_wait_timeout_seconds == 12.0
    assert coerced.startup_wait_timeout_provided is True


def test_all_migrated_runtime_actions_route_to_matching_method() -> None:
    actions = [
        "run",
        "stop",
        "restart",
        "attach",
        "detach",
        "status",
        "logs",
        "breakpoint",
        "exception",
        "wait_event",
        "wait_breakpoint",
        "threads",
        "stack",
        "variables",
        "resume",
        "cleanup_debug_state",
        "update",
    ]
    runtime = _RoutingRuntime()
    dispatcher = Dispatcher(SessionManager(lambda: runtime))

    for action in actions:
        result = dispatcher.dispatch("java_runtime", {"action": action})
        method, parsed_action = runtime.calls[-1]
        assert result == {"ok": True, "status": action}
        assert method == action
        assert parsed_action.action == action


def test_dispatcher_preserves_runtime_error_crash_and_unknown_payloads() -> None:
    runtime = _RoutingRuntime()
    runtime.responses["logs"] = RuntimeResult(
        ok=False,
        data={"error_code": "LOG_READ_FAILED"},
        error="cannot read log",
    )
    runtime.responses["status"] = ValueError("broken status")
    dispatcher = Dispatcher(SessionManager(lambda: runtime))

    assert dispatcher.dispatch("java_runtime", {"action": "logs"}) == {
        "ok": False,
        "error": "cannot read log",
        "error_code": "LOG_READ_FAILED",
    }
    assert dispatcher.dispatch("java_runtime", {"action": "status"}) == {
        "ok": False,
        "error": "ValueError: broken status",
    }
    assert dispatcher.dispatch("java_runtime", {"action": "unknown"}) == {
        "ok": False,
        "error": "Unknown action: unknown",
    }
    assert dispatcher.dispatch("missing_tool", {}) == {
        "ok": False,
        "error": "Unknown tool: missing_tool",
    }


def test_numeric_conversion_failure_stays_before_runtime_dispatch() -> None:
    sessions = SessionManager(lambda: _RoutingRuntime())
    dispatcher = Dispatcher(sessions)

    with pytest.raises(ValueError):
        dispatcher.dispatch(
            "java_runtime",
            {"action": "status", "max_value_depth": "not-an-integer"},
        )

    assert sessions.session_keys == ()


def test_project_launch_routes_without_changing_runtime_action(
    tmp_path: Path,
) -> None:
    runtime = _RoutingRuntime()
    dispatcher = Dispatcher(SessionManager(lambda: runtime))

    result = dispatcher.dispatch(
        "java_runtime",
        {
            "action": "run",
            "project_path": str(tmp_path),
            "launch_name": " Application ",
            "jdwp_port": 6006,
            "ready_port": 8080,
            "startup_wait_timeout_seconds": 12,
        },
    )

    assert result == {"ok": True, "status": "run_project"}
    method, values = runtime.calls[-1]
    action, request = values
    assert method == "run_project"
    assert action.action == "run"
    assert not hasattr(action, "project_path")
    assert request.project_path == tmp_path
    assert request.launch_name == " Application "
    assert request.jdwp_port == 6006
    assert request.ready_port == 8080
    assert request.startup_wait_timeout_seconds == 12


@pytest.mark.parametrize(
    ("arguments", "argument"),
    [
        (
            {"action": "run", "launch_name": "Application"},
            "launch_name",
        ),
        (
            {"action": "status", "project_path": "/tmp/project"},
            "project_path",
        ),
        (
            {"action": "restart", "project_path": "/tmp/project"},
            "project_path",
        ),
        (
            {
                "action": "run",
                "project_path": "/tmp/project",
                "main_class": "Example",
            },
            "main_class",
        ),
    ],
)
def test_invalid_project_arguments_fail_before_runtime_allocation(
    arguments: dict[str, Any],
    argument: str,
) -> None:
    sessions = SessionManager(lambda: _RoutingRuntime())
    result = Dispatcher(sessions).dispatch("java_runtime", arguments)

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_ARGUMENT"
    assert result["argument"] == argument
    assert sessions.session_keys == ()


def test_product_restart_rejects_project_path_before_runtime_allocation() -> None:
    sessions = SessionManager(lambda: _RoutingRuntime())

    result = Dispatcher(sessions).dispatch(
        "java_application",
        {"action": "restart", "project_path": "/tmp/project"},
    )

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_ARGUMENT"
    assert result["argument"] == "project_path"
    assert "sealed Generation" in result["error"]
    assert sessions.session_keys == ()


def test_java_processes_preserves_full_bool_coercion(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_discovery(*, filter_text: str | None, full: bool) -> dict[str, Any]:
        captured.update(filter_text=filter_text, full=full)
        return {"message": "none", "count": 0, "processes": []}

    monkeypatch.setattr(dispatcher_module, "discover_java_processes", fake_discovery)

    result = Dispatcher().dispatch(
        "java_processes",
        {"filter": "demo", "full": "false"},
    )

    assert result == {"message": "none", "count": 0, "processes": []}
    assert captured == {"filter_text": "demo", "full": True}
