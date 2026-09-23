"""Status is an overview; result reads the retained attempt, never reruns it."""

import json
import threading
from copy import deepcopy
from types import SimpleNamespace

import anyio
import pytest

from jolink_runtime.core.dispatcher import Dispatcher
from jolink_runtime.launch.application_wait import application_waiter
from jolink_runtime.launch.fast_test_manager import TestAttempt as Attempt
from jolink_runtime.launch.process_supervisor import AttemptToken
from jolink_runtime.server.mcp_server import RuntimeMCPBoundary


def attempt(tmp_path, run_id="test_result", **kwargs):
    return Attempt(
        test_run_id=run_id, generation=1, owner=AttemptToken(run_id, 1),
        project_path=tmp_path, source_files=(), tests=("example.Test",),
        timeout_seconds=300, **kwargs,
    )


@pytest.fixture
def product():
    dispatcher = Dispatcher()
    manager = dispatcher.sessions.get_runtime()._fast_tests
    yield RuntimeMCPBoundary(dispatcher), manager
    dispatcher.close_session()


def test_status_stays_small_while_compile_failure_run_and_result_include_diagnostics(tmp_path, product):
    boundary, manager = product
    errors = [{"resource": f"example/Case{i}.java", "line": 1,
               "message": "cannot resolve value", "severity_name": "ERROR"} for i in range(26)]
    stored = {
        "ok": False, "passed": False, "error_code": "JDT_TEST_COMPILE_FAILED",
        "error_count": 26, "diagnostics": errors, "diagnostics_truncated": False,
        "bootstrap_log_tail": ["private build detail"],
        "error": "large error " * 1000,
    }
    finished = attempt(tmp_path, state="compile_failed", compiled_source_count=281,
                       compiled_source_units=tuple(f"test-src/Case{i}.java" for i in range(281)),
                       result=deepcopy(stored))
    finished.done.set()
    manager._last = finished

    async def scenario():
        for _ in range(2):
            response = await boundary.call_tool("java_status", {"action": "status"})
            assert not response.isError
            summary = response.structuredContent["fast_test"]
            assert summary["error_count"] == 26
            assert summary["compiled_source_count"] == 281
            assert summary["status"] == "compile_failed"
            assert not {"diagnostics", "compiled_source_units", "bootstrap_log_tail", "error"} & summary.keys()
            assert len(response.content[0].text) < 2000
            hint = summary["next_action"]
            detail = await boundary.call_tool(hint["tool"], hint["arguments"])
            assert detail.isError
            assert detail.structuredContent["diagnostics"] == errors
            assert detail.structuredContent["bootstrap_log_tail"] == stored["bootstrap_log_tail"]
            assert "compiled_source_units" not in detail.structuredContent
            assert json.loads(detail.content[0].text) == detail.structuredContent
        assert finished.result == stored  # Neither read consumes or edits the result.
        assert manager._active is None

    anyio.run(scenario)
    runtime = SimpleNamespace(_fast_tests=manager)
    waiter = application_waiter(runtime, "test", {"ok": True, "test_run_id": finished.test_run_id})
    assert not waiter.pending()
    assert waiter.result() == finished.snapshot()
    assert waiter.result()["diagnostics"] == errors
    assert "compiled_source_units" not in waiter.result()
    assert "next_action" not in waiter.result()


@pytest.mark.parametrize("state,code", [
    ("compile_failed", "JDT_TEST_COMPILE_FAILED"),
    ("failed", "JDT_TEST_FULL_COMPILE_FAILED"),
    ("compile_failed", "FAST_TEST_COMPILE_STATE_INVALID"),
])
def test_already_failed_compilation_is_returned_directly_by_start(tmp_path, product, monkeypatch, state, code):
    _, manager = product
    diagnostics = [{"resource": "src/Example.java", "line": 3, "message": "cannot resolve value"}]

    def fail(current):
        current.state = state
        current.result = {"ok": False, "passed": False, "error_code": code,
                          "error_count": 200, "diagnostics": diagnostics, "diagnostics_truncated": True}
        current.done.set()

    monkeypatch.setattr(manager, "_run_attempt", fail)
    response = manager.start(project_path=tmp_path, source_files=(), tests=("example.Test",), short_wait_seconds=2)
    assert response["ok"] is False
    assert response["diagnostics"] == diagnostics
    assert response["error_count"] == 200 and response["diagnostics_truncated"] is True
    assert "diagnostics" not in manager.status()
    assert manager.result(response["test_run_id"])["diagnostics"] == diagnostics


@pytest.mark.parametrize("state,result,expected_error", [
    ("completed", {"ok": True, "passed": True, "tests": 3, "passed_count": 3}, False),
    ("completed", {"ok": True, "passed": False, "failed_count": 1,
                   "failed_tests": [{"message": "assertion", "stack": "stack trace"}]}, False),
    ("failed", {"ok": False, "error_code": "FAST_TEST_FAILED", "error": "setup failed",
                "unsupported_configuration_names": ["jdkToolchain", "useSystemClassLoader"]}, True),
    ("cancelled", {"ok": False, "error_code": "TEST_CANCELLED", "error": "cancelled"}, True),
    ("compiling", None, False),
])
def test_result_keeps_outcome_semantics_without_starting_work(
    tmp_path, product, monkeypatch, state, result, expected_error
):
    boundary, manager = product
    current = attempt(tmp_path, state=state, result=result)
    manager._last = current
    current.done.set()

    def unexpected(*args, **kwargs):
        raise AssertionError("A result read must not start, cancel, or prepare a test")

    for name in ("start", "cancel", "_ensure_project"):
        monkeypatch.setattr(manager, name, unexpected)
    monkeypatch.setattr(manager._runner, "run", unexpected)

    async def scenario():
        response = await boundary.call_tool("java_fast_test", {"action": "result", "test_run_id": current.test_run_id})
        assert response.isError is expected_error
        assert response.structuredContent["status"] == state
        for key, value in (result or {}).items():
            assert response.structuredContent[key] == value
        summary = manager.status()
        assert current.run_response() == current.summary()
        assert "failed_tests" not in summary
        assert ("next_action" in summary) == (result is not None and result.get("passed") is not True)

    anyio.run(scenario)


def test_active_and_last_results_are_addressed_by_id_not_silently_replaced(tmp_path, product):
    boundary, manager = product
    previous = attempt(tmp_path, "test_previous", state="completed", result={"ok": True, "passed": True})
    active = attempt(tmp_path, "test_active", state="running")
    manager._last, manager._active = previous, active
    try:
        async def scenario():
            for expected in (previous, active):
                response = await boundary.call_tool("java_fast_test", {"action": "result", "test_run_id": expected.test_run_id})
                assert response.structuredContent["test_run_id"] == expected.test_run_id
                assert response.structuredContent["status"] == expected.state
            manager._last = active
            manager._active = None
            missing = await boundary.call_tool("java_fast_test", {"action": "result", "test_run_id": previous.test_run_id})
            assert missing.isError
            assert missing.structuredContent["error_code"] == "TEST_RUN_NOT_FOUND"
        anyio.run(scenario)
    finally:
        manager._active = None


def test_result_with_no_retained_attempt_reports_not_found(product):
    boundary, _ = product
    async def scenario():
        response = await boundary.call_tool("java_fast_test", {"action": "result", "test_run_id": "missing"})
        assert response.isError
        assert response.structuredContent["error_code"] == "TEST_RUN_NOT_FOUND"
    anyio.run(scenario)


def test_result_preserves_diagnostic_truncation_and_total_error_count(tmp_path, product):
    boundary, manager = product
    errors = [{"message": "compile error"} for _ in range(128)]
    manager._last = attempt(tmp_path, state="compile_failed", result={
        "ok": False, "passed": False, "error_count": 300,
        "diagnostics": errors, "diagnostics_truncated": True,
    })
    async def scenario():
        summary = manager.status()
        assert summary["error_count"] == 300 and "diagnostics" not in summary
        detail = await boundary.call_tool("java_fast_test", {
            "action": "result", "test_run_id": summary["test_run_id"],
        })
        assert detail.structuredContent["error_count"] == 300
        assert len(detail.structuredContent["diagnostics"]) == 128
        assert detail.structuredContent["diagnostics_truncated"] is True
    anyio.run(scenario)


@pytest.mark.parametrize("compile_failed", [False, True])
def test_synchronous_wait_returns_original_result_even_after_a_new_run(tmp_path, compile_failed):
    first = attempt(tmp_path, "first", state="running")
    manager = SimpleNamespace(_lock=threading.Lock(), _active=first, _last=None)
    waiter = application_waiter(SimpleNamespace(_fast_tests=manager), "test", first.summary())
    assert waiter.pending()
    first.state = "completed"
    first.result = {"ok": True, "passed": False, "failed_count": 1, "failed_tests": [{"message": "failure"}]}
    if compile_failed:
        first.state = "compile_failed"
        first.result = {"ok": False, "diagnostics": [{"resource": "First.java", "line": 1, "message": "compile error"}]}
    first.done.set()
    manager._active = attempt(tmp_path, "replacement")
    assert not waiter.pending()
    assert waiter.result()["test_run_id"] == "first"
    assert "failed_tests" not in waiter.result()
    if compile_failed:
        assert waiter.result()["diagnostics"] == first.result["diagnostics"]


def test_background_compilation_failure_remains_available_via_result(tmp_path, product, monkeypatch):
    boundary, manager = product
    current = attempt(tmp_path, state="compiling")

    def start(**kwargs):
        manager._active = current
        return current.run_response()

    monkeypatch.setattr(manager, "start", start)

    async def scenario():
        pending = await boundary.call_tool("java_fast_test", {
            "project_path": str(tmp_path), "tests": ["example.Test"], "timeout": 0,
        })
        assert pending.structuredContent["status"] == "compiling"
        assert "diagnostics" not in pending.structuredContent
        current.state = "compile_failed"
        current.result = {"ok": False, "diagnostics": [{"resource": "Example.java", "line": 2, "message": "compile error"}]}
        current.done.set()
        assert "diagnostics" not in manager.status()
        result = await boundary.call_tool("java_fast_test", {
            "action": "result", "test_run_id": current.test_run_id,
        })
        assert result.isError
        assert result.structuredContent["diagnostics"] == current.result["diagnostics"]
        assert pending.structuredContent["status"] == "compiling"

    anyio.run(scenario)
