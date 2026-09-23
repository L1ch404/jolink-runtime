"""Project restart compiles once, then chooses its application mechanism."""

import time
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jolink_runtime.adapters.java.jdwp_adapter import JavaRuntime, ProjectUpdatePlan
from jolink_runtime.adapters.java.jdwp_client import JDWPCommandOutcomeUnknown, JDWPCommandRejected
from jolink_runtime.core.dispatcher import parse_runtime_action
from jolink_runtime.core.models import RuntimeResult
from jolink_runtime.launch import project_restart
from jolink_runtime.launch.application_wait import application_waiter
from jolink_runtime.launch.configuration_inputs import build_configuration_stamps
from jolink_runtime.launch.jdt_compile_session import PersistentJdtCompileSession
from jolink_runtime.launch.project_session import JavaProjectSession


@pytest.fixture
def running(tmp_path, monkeypatch):
    sources = tuple(tmp_path / f"Changed{i}.java" for i in range(20))
    output = tmp_path / "bin"
    output.mkdir()
    (output / "Changed.class").write_bytes(b"compiled bytecode")
    result = SimpleNamespace(
        compile_ok=True, elapsed_ms=12, jdt_build_ms=10, diagnostics_ms=0,
        compiled_source_count=20, actual_build_kind="INCREMENTAL", error_count=0,
        diagnostics=(), diagnostics_truncated=False, runtime_changed_classes=("Changed.class",),
        runtime_deleted_classes=(), runtime_changed_resources=(), runtime_deleted_resources=(),
    )
    compiler = object.__new__(PersistentJdtCompileSession)
    # Only the local coordination is mocked; the real MCP test exercises JDT/JDWP.
    monkeypatch.setattr(PersistentJdtCompileSession, "ready", property(lambda self: True))
    compiler.workspace_source_changes = Mock(return_value=sources)
    compiler.compile = Mock(return_value=result)
    compiler.class_file = lambda path: (path, output / path)
    compiler.mark_published = Mock()
    session = JavaProjectSession(root=tmp_path / "session", build_world_fingerprint="world")
    session.attach_compile_session(compiler)
    plan = SimpleNamespace(project_root=tmp_path, configuration_inputs=(),
        configuration_stamps=build_configuration_stamps(tmp_path, "maven", ()))
    prepared = ProjectUpdatePlan(attempt_directory=tmp_path, project_session=session,
                                 jdt_build_world_plan=plan)
    runtime = JavaRuntime()
    runtime._proc._process = SimpleNamespace(pid=1, is_alive=lambda: True)
    runtime._last_project_request = object()
    runtime._project_sessions["active"] = session
    runtime._project_update_plans["active"] = prepared
    runtime._launch_controller = SimpleNamespace(snapshot=lambda: {"attempt_id": "active", "generation": 1})
    jdwp = SimpleNamespace(
        classes_by_signature=Mock(return_value=[SimpleNamespace(reference_type_id=10)]),
        redefine_classes=Mock(),
    )
    monkeypatch.setattr(runtime, "_connect", lambda: jdwp)
    monkeypatch.setattr(runtime, "_refresh_updated_breakpoints",
                        lambda *args: {"state": "complete", "stale": [], "warnings": []})
    replace = Mock(return_value=RuntimeResult(ok=True, data={
        "launch_phase": "runtime_active", "status": "process_started", "pid": 42,
    }))
    monkeypatch.setattr(project_restart, "restart_compiled", replace)
    return SimpleNamespace(runtime=runtime, session=session, compiler=compiler,
                           result=result, jdwp=jdwp, replace=replace, sources=sources)


def finish(running, **args):
    initial = running.runtime.restart(parse_runtime_action({"action": "restart", **args}))
    assert initial.ok and initial.data["status"] == "restart_started", initial
    wait = application_waiter(running.runtime, "restart", {"ok": True, **initial.data})
    deadline = time.monotonic() + 3
    while wait.pending():
        assert time.monotonic() < deadline
        time.sleep(.005)
    return wait.result()


def test_restart_defaults_to_hotswap_and_scans_whole_workspace(running):
    result = finish(running)
    assert result["ok"] and result["apply_method"] == "hotswap"
    running.compiler.compile.assert_called_once_with(running.sources)
    running.compiler.mark_published.assert_called_once()
    running.replace.assert_not_called()
    assert result["framework_state_refreshed"] is False


@pytest.mark.parametrize("reason", ["forced", "no_changes", "deleted", "resource", "unloaded", "rejected", "launch_args", "suspended", "process_exited"])
def test_compile_once_before_real_restart(running, reason):
    args = {}
    if reason == "forced": args["hotswap"] = False
    if reason == "no_changes": running.result.runtime_changed_classes = ()
    if reason == "deleted": running.result.runtime_deleted_classes = ("Old.class",)
    if reason == "resource": running.result.runtime_changed_resources = ("generated.txt",)
    if reason == "unloaded": running.jdwp.classes_by_signature.return_value = []
    if reason == "rejected": running.jdwp.redefine_classes.side_effect = JDWPCommandRejected(
        64, command_set=1, command=18, operation="redefine_classes")
    if reason == "launch_args": args["vm_args"] = ["-Xmx512m"]
    if reason == "suspended": running.runtime._active_suspension = object()
    if reason == "process_exited": running.runtime._proc._process = None
    result = finish(running, **args)
    assert result["ok"] and result["applied"] and result["apply_method"] == "restart", result
    running.compiler.compile.assert_called_once_with(running.sources)
    running.replace.assert_called_once()
    if reason != "rejected": running.jdwp.redefine_classes.assert_not_called()


def test_compile_error_keeps_old_process_even_when_forced(running):
    running.result.compile_ok = False
    running.result.error_count = 1
    for args in ({}, {"hotswap": False}):
        result = finish(running, **args)
        assert not result["ok"] and result["error_code"] == "JDT_COMPILE_FAILED"
    running.replace.assert_not_called()
    running.jdwp.redefine_classes.assert_not_called()


def test_unknown_hotswap_is_not_treated_as_rejection(running):
    running.jdwp.redefine_classes.side_effect = JDWPCommandOutcomeUnknown(
        packet_id=1, command_set=1, command=18, operation="redefine_classes", cause=TimeoutError())
    result = finish(running)
    assert not result["ok"] and result["applied"] is None, result
    assert result["error_code"] == "HOT_SWAP_OUTCOME_UNKNOWN", result
    assert "hotswap=false" in result["suggested_next_step"]
    running.replace.assert_not_called()


def test_start_failure_is_reported_not_promoted(running):
    running.replace.return_value = RuntimeResult(ok=False, error="cannot start", data={"error_code": "JVM_START_FAILED"})
    result = finish(running, hotswap=False)
    assert not result["ok"] and not result["applied"], result
    assert result["error_code"] == "JVM_START_FAILED"


def test_cancel_while_compiling_leaves_jvm_unmodified(running):
    entered, release = threading.Event(), threading.Event()
    def compile(sources):
        entered.set()
        assert release.wait(3)
        return running.result
    running.compiler.compile.side_effect = compile
    initial = running.runtime.restart(parse_runtime_action({"action": "restart"}))
    waiter = application_waiter(running.runtime, "restart", {"ok": True, **initial.data})
    try:
        assert entered.wait(2)
        assert waiter.pending()
        running.session.request_reload_cancel("stopped")
    finally:
        release.set()
    deadline = time.monotonic() + 3
    while waiter.pending():
        assert time.monotonic() < deadline
        time.sleep(.005)
    assert waiter.result()["error_code"] == "RELOAD_CANCELLED"
    running.replace.assert_not_called()
    running.jdwp.redefine_classes.assert_not_called()


def test_waiter_keeps_its_result_when_a_later_restart_finishes(running):
    initial = running.runtime.restart(parse_runtime_action({"action": "restart"}))
    waiter = application_waiter(running.runtime, "restart", {"ok": True, **initial.data})
    deadline = time.monotonic() + 3
    while waiter.pending():
        assert time.monotonic() < deadline
        time.sleep(.005)
    first = waiter.result()
    second = finish(running, hotswap=False)
    assert second["reload_id"] != first["reload_id"]
    assert second["apply_method"] == "restart"
    assert waiter.result() == first and first["apply_method"] == "hotswap"
