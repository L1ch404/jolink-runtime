"""Compact status preserves outcomes; only explicit log reads touch build.log."""

import json
from copy import deepcopy
from types import SimpleNamespace

import anyio
import pytest

from jolink_runtime.core.dispatcher import Dispatcher
from jolink_runtime.launch import runtime_observation as observation
from jolink_runtime.server.mcp_server import RuntimeMCPBoundary


@pytest.fixture
def product(tmp_path, monkeypatch):
    dispatcher = Dispatcher()
    runtime = dispatcher.sessions.get_runtime()
    boundary = RuntimeMCPBoundary(dispatcher)
    diagnostics = [{"message": "example error", "resource": "Example.java"}] * 128
    launch = {
        "attempt_id": "launch_example", "launch_phase": "failed", "process_state": "absent",
        "build": {"running": False, "running_operation_count": 0, "in_flight_operation_count": 0, "pids": []},
        "launch_error": {"error_code": "JDT_COMPILE_FAILED", "message": "Compilation failed",
                         "retryable": True, "diagnostics": diagnostics},
    }
    last = {"reload_id": "reload_example", "stage": "failed", "ok": False,
            "applied": False, "error_code": "JDT_COMPILE_FAILED", "diagnostics": diagnostics,
            "redefined_classes": [f"example.Class{i}" for i in range(300)]}
    state = {"compile_ready": True, "last_reload": last, "active_operation": None,
             "jdt_worker": {"java_major": 21}, "product_timing_ms": {"jdt_bootstrap": 1234}}
    monkeypatch.setattr(runtime._launch_controller, "snapshot", lambda: deepcopy(launch))
    runtime._project_attempt_directories["launch_example"] = tmp_path
    runtime._project_sessions["launch_example"] = SimpleNamespace(
        public_status=lambda: deepcopy(state), generations=SimpleNamespace(current=None),
    )
    (tmp_path / "build.log").write_text("first\nAuthorization=Bearer private-token\nlast\n", encoding="utf-8")
    yield boundary, runtime, state, tmp_path
    runtime._project_sessions.clear()
    dispatcher.close_session()


def test_status_and_details_do_not_read_build_log_or_return_mcp_log(product, monkeypatch):
    boundary, _, state, _ = product
    def unexpected(*args, **kwargs):
        raise AssertionError("status/details must not read build.log")
    monkeypatch.setattr(observation, "read_log_tail_snapshot", unexpected)

    async def scenario():
        for _ in range(2):
            reply = await boundary.call_tool("java_status", {"action": "status"})
            summary = reply.structuredContent
            assert "diagnostics" not in summary["launch_error"]
            assert "diagnostics" not in summary["last_reload"]
            assert summary["last_reload"]["error_code"] == "JDT_COMPILE_FAILED"
            assert len(reply.content[0].text) < 2000
            for key in ("server_diagnostics", "build_log_tail", "product_timing_ms", "jdt_worker", "overlay_sources"):
                assert key not in summary
            assert "log_tail" not in summary["build"]
            hint = summary["last_reload"]["next_action"]
            detail = await boundary.call_tool(hint["tool"], hint["arguments"])
            payload = detail.structuredContent
            assert payload["last_reload"] == state["last_reload"]
            assert len(payload["launch_error"]["diagnostics"]) == 128
            assert "log_tail" not in payload["build"]
            assert "server_diagnostics" not in payload
            assert json.loads(detail.content[0].text) == payload
    anyio.run(scenario)


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_build_logs_are_explicit_bounded_and_redacted(product, newline):
    boundary, runtime, _, directory = product
    app_log = directory / "application.log"
    app_line = "application output" + newline
    # Write exact bytes so both newline formats are exercised on every OS.
    app_log.write_bytes(app_line.encode("utf-8"))
    (directory / "build.log").write_bytes(
        newline.join(("first", "Authorization=Bearer private-token", "last", "")).encode("utf-8")
    )
    runtime._log._current_file = str(app_log)
    async def scenario():
        default = await boundary.call_tool("java_status", {"action": "logs", "tail": 1})
        assert default.structuredContent["lines"] == [app_line]
        assert default.structuredContent["returned_bytes"] == len(app_line.encode("utf-8"))
        build = await boundary.call_tool("java_status", {"action": "logs", "source": "build", "tail": 2})
        assert not build.isError
        payload = build.structuredContent
        assert payload["returned_lines"] == 2
        assert payload["lines"][-1] == "last" + newline
        assert payload["source"] == "build"
        assert "private-token" not in build.content[0].text
        assert "<redacted>" in build.content[0].text
        assert payload["max_return_bytes"] == 32 * 1024
        (directory / "build.log").write_text("x" * 100_000, encoding="utf-8")
        large = await boundary.call_tool("java_status", {"action": "logs", "source": "build"})
        assert large.structuredContent["returned_bytes"] <= 32 * 1024
        assert large.structuredContent["truncated"] is True
        (directory / "build.log").unlink()
        missing = await boundary.call_tool("java_status", {"action": "logs", "source": "build"})
        assert missing.isError and missing.structuredContent["error_code"] == "BUILD_LOG_UNAVAILABLE"
    anyio.run(scenario)


@pytest.mark.parametrize("state", ["starting", "ready", "failed", "unverified"])
def test_summary_preserves_readiness_suspension_and_unknown_apply_outcomes(state):
    raw = {
        "ok": True, "process_state": "running", "pid": 123, "startup_state": state,
        "debug_state": "suspended", "suspension_id": "susp_1", "restart_required": True,
        "readiness": {"port": 8080, "verified": state == "ready", "last_checked_at": "timestamp"},
        "active_operation": {"reload_id": "new", "stage": "compiling"},
        "last_reload": {"reload_id": "old", "applied": None, "error_code": "HOT_SWAP_OUTCOME_UNKNOWN",
                        "diagnostics": ["large details"]},
    }
    original = deepcopy(raw)
    result = observation.status_summary(raw)
    assert result["startup_state"] == state and result["pid"] == 123
    assert result["suspension_id"] == "susp_1"
    assert result["last_reload"]["applied"] is None
    assert result["last_reload"]["error_code"] == "HOT_SWAP_OUTCOME_UNKNOWN"
    assert result["active_operation"] == raw["active_operation"]
    assert result["restart_required"] is True
    assert raw == original


def test_build_logs_without_a_launch_do_not_create_work():
    dispatcher = Dispatcher()
    try:
        result = dispatcher.dispatch("java_status", {"action": "logs", "source": "build"})
        assert result["error_code"] == "BUILD_LOG_UNAVAILABLE"
        assert dispatcher.sessions.get_runtime()._launch_controller.snapshot()["launch_phase"] == "idle"
    finally:
        dispatcher.close_session()


def test_build_log_reader_keeps_windows_host_encoding(product, monkeypatch):
    boundary, _, _, directory = product
    monkeypatch.setattr(observation.locale, "getencoding", lambda: "gbk")
    (directory / "build.log").write_bytes("\x1b[31m构建错误\x1b[0m\n".encode("gbk"))
    async def scenario():
        response = await boundary.call_tool("java_status", {"action": "logs", "source": "build"})
        assert not response.isError
        assert response.structuredContent["lines"] == ["构建错误\n"]
        assert response.structuredContent["encoding"] == "gbk"
    anyio.run(scenario)
