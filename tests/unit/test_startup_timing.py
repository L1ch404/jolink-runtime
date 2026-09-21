"""Previous startup is an observation, not a prediction or the current duration."""

import threading
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from jolink_runtime.core.dispatcher import Dispatcher
from jolink_runtime.core.models import RuntimeResult
from jolink_runtime.adapters.java.process_manager import ProcessInfo, ProcessManager
from jolink_runtime.launch.application_wait import application_waiter
from jolink_runtime.launch.contracts import LaunchAttempt, LaunchPhase
from jolink_runtime.launch.startup_timing import StartupTimings
from jolink_runtime.server.mcp_server import RuntimeMCPBoundary


@pytest.fixture(autouse=True)
def isolated_timing_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "cache"))


def project_request(root, name="App", ready_port=8080):
    return SimpleNamespace(project_path=root, launch_name=name, build_system="", ready_port=ready_port)


def test_project_timing_survives_stop_but_does_not_cross_projects_or_profiles(tmp_path, monkeypatch):
    dispatcher = Dispatcher()
    runtime = dispatcher.sessions.get_runtime()
    request = project_request(tmp_path)
    project_session = SimpleNamespace(_lock=threading.Lock(), _last_successful_startup_ms=1200.0)
    monkeypatch.setattr(runtime._launch_controller, "snapshot", lambda: {"attempt_id": "active"})

    def launch(action, request):
        runtime._last_project_request = request
        return RuntimeResult(ok=True, data={"status": "project_launch_started"})

    def stop(action):
        runtime._project_sessions.clear()
        return RuntimeResult(ok=True, data={"status": "stopped"})

    monkeypatch.setattr(runtime, "run_project", launch)
    monkeypatch.setattr(runtime, "stop", stop)
    args = {"action": "launch", "project_path": str(tmp_path), "launch_name": "App", "ready_port": 8080}
    try:
        first = dispatcher.dispatch("java_application", args)
        assert first["previous_startup_ms"] is None
        runtime._project_sessions["active"] = project_session
        StartupTimings().save(StartupTimings._project_key(request), 1200.0)
        dispatcher.dispatch("java_application", {"action": "stop"})
        assert dispatcher.dispatch("java_application", args)["previous_startup_ms"] == 1200
        assert dispatcher.dispatch("java_application", {**args, "launch_name": "Other"})["previous_startup_ms"] is None
        other = tmp_path / "other"
        other.mkdir()
        assert dispatcher.dispatch("java_application", {**args, "project_path": str(other)})["previous_startup_ms"] is None
        # A failed/incomplete startup must not erase the previous success.
        runtime._last_project_request = request
        runtime._project_sessions["active"] = SimpleNamespace(_lock=threading.Lock(), _last_successful_startup_ms=None)
        assert dispatcher.dispatch("java_application", args)["previous_startup_ms"] == 1200
        assert dispatcher.dispatch("java_application", {k:v for k,v in args.items() if k != "ready_port"})["previous_startup_ms"] is None
    finally:
        runtime._project_sessions.clear()
        dispatcher.close_session()


def test_restart_captures_previous_value_before_replacing_current_success(tmp_path, monkeypatch):
    dispatcher = Dispatcher()
    runtime = dispatcher.sessions.get_runtime()
    runtime._last_project_request = project_request(tmp_path)
    current = SimpleNamespace(_lock=threading.Lock(), _last_successful_startup_ms=1200.0)
    runtime._project_sessions["active"] = current
    StartupTimings().save(StartupTimings._project_key(runtime._last_project_request), 1200.0)
    monkeypatch.setattr(runtime._launch_controller, "snapshot", lambda: {"attempt_id": "active"})
    def restart(action):
        current._last_successful_startup_ms = 2500.0
        StartupTimings().save(StartupTimings._project_key(runtime._last_project_request), 2500.0)
        return RuntimeResult(ok=True, data={"status": "restarted", "last_successful_startup_ms": 2500.0})
    monkeypatch.setattr(runtime, "restart", restart)
    try:
        result = dispatcher.dispatch("java_application", {"action": "restart"})
        assert result["previous_startup_ms"] == 1200
        assert result["last_successful_startup_ms"] == 2500
        # HotSwap leaves that successful JVM startup duration unchanged.
        monkeypatch.setattr(runtime, "restart", lambda _: RuntimeResult(ok=True, data={"apply_method": "hotswap"}))
        assert dispatcher.dispatch("java_application", {"action": "restart"})["previous_startup_ms"] == 2500
    finally:
        runtime._project_sessions.clear()
        dispatcher.close_session()


@pytest.mark.parametrize("state,ready_port,expected", [("ready", 8080, 2400), ("unverified", 0, 2000), ("starting", 8080, None), ("failed", 8080, None)])
def test_direct_startup_uses_frozen_ready_or_jdwp_timing(state, ready_port, expected, monkeypatch):
    monkeypatch.setattr("jolink_runtime.adapters.java.process_manager.time.monotonic", lambda: 0)
    timings = StartupTimings()
    action = SimpleNamespace(main_class="example.App", jar_path="", classpath="classes", ready_port=ready_port)
    key = timings._direct_key(action)
    process = ProcessInfo(SimpleNamespace(pid=999, poll=lambda: None), 5005, action.main_class,
                          ready_port=ready_port, startup_timing_key=key)
    process._startup_state = state
    if state == "ready":
        process._ready_observed_monotonic = 2.4
    elif state == "failed":
        process.mark_startup_failed("fixture failure")
    if state == "unverified":
        process.record_startup_timing(2000)
    ProcessManager().observe_readiness(process, refresh=False)
    runtime = SimpleNamespace(
        _last_project_request=None, _last_direct_action=action,
    )
    assert timings.previous(runtime, {"action": "restart"}) == expected
    attached = ProcessInfo(None, 5005, action.main_class, pid=999, owned=False, startup_timing_key=key)
    attached.record_startup_timing(99999)
    fresh = StartupTimings()
    assert fresh.previous(runtime, {"action": "restart"}) == expected


def test_an_existing_reader_sees_a_new_mcp_process_measurement(tmp_path):
    first, second = StartupTimings(), StartupTimings()
    key = first._project_key(project_request(tmp_path))
    first.save(key, 1234)
    assert second.load(key) == 1234
    second.save(key, 5678)
    assert first.load(key) == 5678
    assert len(list(first.root.glob("*.json"))) == 1


def test_readiness_saves_once_without_waiting_for_another_tool_call(tmp_path, monkeypatch):
    key = StartupTimings._project_key(project_request(tmp_path))
    process = ProcessInfo(SimpleNamespace(pid=999, poll=lambda: None), 5005, "App",
                          ready_port=8080, startup_timing_key=key)
    manager = ProcessManager()
    monkeypatch.setattr(manager, "_check_tcp_port", lambda *_: True)
    ready = manager.observe_readiness(process)
    assert StartupTimings().load(key) == ready["startup_elapsed_ms"]
    # Another window can publish a newer measurement. Polling the old process
    # must not overwrite it or rewrite this file on every status request.
    StartupTimings().save(key, 99999)
    manager.observe_readiness(process)
    assert StartupTimings().load(key) == 99999


def test_process_exit_before_readiness_does_not_replace_saved_timing(tmp_path, monkeypatch):
    key = StartupTimings._project_key(project_request(tmp_path))
    StartupTimings().save(key, 4321)
    polls = iter((None, 7))
    process = ProcessInfo(SimpleNamespace(pid=999, poll=lambda: next(polls)), 5005, "App",
                          ready_port=8080, startup_timing_key=key)
    manager = ProcessManager()
    monkeypatch.setattr(manager, "_check_tcp_port", lambda *_: True)
    assert manager.observe_readiness(process)["startup_state"] == "failed"
    assert StartupTimings().load(key) == 4321


def test_missing_or_broken_file_is_unknown_and_next_success_replaces_it(tmp_path):
    store = StartupTimings()
    key = store._project_key(project_request(tmp_path))
    assert store.load(key) is None
    store.root.mkdir(parents=True)
    store._file(key).write_text("{unfinished", encoding="utf-8")
    assert store.load(key) is None
    store.save(key, 1234)
    assert StartupTimings().load(key) == 1234


def test_timing_write_failure_keeps_old_file_and_does_not_fail_startup(tmp_path, monkeypatch):
    store = StartupTimings()
    key = store._project_key(project_request(tmp_path))
    store.save(key, 1234)
    def fail(*_):
        raise OSError("cannot replace timing file")
    monkeypatch.setattr(Path, "replace", fail)
    process = ProcessInfo(SimpleNamespace(pid=999, poll=lambda: None), 5005, "App", startup_timing_key=key)
    process.record_startup_timing(5678)
    assert store.load(key) == 1234
    assert not list(store.root.glob("*.tmp"))


def test_relative_direct_targets_are_separate_between_working_directories(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    store = StartupTimings()
    key = store._direct_identity("", "App", ".", 0)
    store.save(key, 1000)
    monkeypatch.chdir(second)
    assert store.load(store._direct_identity("", "App", ".", 0)) is None
    assert store.load(store._direct_identity("", "App", str(first), 0)) == 1000


def test_application_wait_preserves_previous_duration_when_current_launch_finishes():
    attempt = LaunchAttempt("launch", 1, phase=LaunchPhase.COMPILING)
    record = SimpleNamespace(attempt=attempt)
    runtime = SimpleNamespace(
        _launch_controller=SimpleNamespace(_lock=threading.Lock(), _current=record),
        status=lambda _: RuntimeResult(ok=True, data={"last_successful_startup_ms": 2500.0}),
    )
    initial = {"ok": True, "attempt_id": "launch", "previous_startup_ms": 1200.0}
    waiter = application_waiter(runtime, "launch", initial)
    assert waiter.result()["previous_startup_ms"] == 1200
    attempt.phase = LaunchPhase.RUNTIME_ACTIVE
    assert waiter.result()["previous_startup_ms"] == 1200
    assert waiter.result()["last_successful_startup_ms"] == 2500


def test_status_details_is_a_flag_and_old_action_is_not_exposed():
    boundary = RuntimeMCPBoundary()
    async def scenario():
        tools = {t.name: t for t in await boundary.list_tools()}
        schema = tools["java_status"].inputSchema
        assert "details" not in schema["properties"]["action"]["enum"]
        assert schema["properties"]["details"]["default"] is False
        old = await boundary.call_tool("java_status", {"action": "details"})
        assert old.isError and old.structuredContent["error_code"] == "INVALID_ARGUMENT"
        for value in (False, True):
            result = await boundary.call_tool("java_status", {"action": "status", "details": value})
            assert not result.isError
            assert "server_diagnostics" not in result.structuredContent
        await boundary.shutdown()
    anyio.run(scenario)
