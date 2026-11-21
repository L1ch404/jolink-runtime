"""Protocol snapshots a slow CI machine can expose between a request and its result."""
from types import SimpleNamespace

import anyio
import pytest

from java_support import call_workflow


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def call_tool(self, tool, arguments):
        self.calls.append((tool, arguments))
        return SimpleNamespace(structuredContent=next(self.responses))


@pytest.mark.parametrize("phase", ["runtime_active", "failed", "cancelled", "stopped"])
def test_wait_for_original_launch_when_status_snapshot_only_has_phase(phase):
    terminal = {"ok": True, "attempt_id": "L", "launch_phase": phase}
    session = Session([
        {"ok": True, "status": "project_launch_started", "attempt_id": "L"},
        {"ok": True, "attempt_id": "old", "launch_phase": "runtime_active"},
        {"ok": True, "attempt_id": "L", "launch_phase": "resolving_runtime",
         "last_reload": {"reload_id": "old", "stage": "succeeded"}},
        {"ok": True, "attempt_id": "L", "launch_phase": "starting_jvm"},
        terminal,
    ])
    observed = anyio.run(call_workflow, session, "java_application", {"action": "launch"})
    assert observed == {**terminal, "ok": phase == "runtime_active"}
    assert [t for t, _ in session.calls] == ["java_application"] + ["java_status"] * 4


@pytest.mark.parametrize("stage", ["succeeded", "failed"])
def test_restart_does_not_consume_old_reload_or_current_app_readiness(stage):
    terminal = {"reload_id": "R", "stage": stage}
    session = Session([
        {"ok": True, "status": "restart_started", "reload_id": "R"},
        {"ok": True, "launch_phase": "runtime_active", "last_reload": {"reload_id": "old", "stage": "succeeded"}},
        {"ok": True, "launch_phase": "runtime_active", "active_operation": {"reload_id": "R"}},
        {"ok": True, "last_reload": terminal},
    ])
    assert anyio.run(call_workflow, session, "java_application", {"action": "restart"}) == terminal


def test_fast_test_polling_keeps_its_id_and_reports_compile_failure():
    terminal = {"ok": False, "status": "compile_failed", "test_run_id": "T"}
    session = Session([{"status": "starting", "test_run_id": "T"}, {"status": "compiling", "test_run_id": "T"}, terminal])
    assert anyio.run(call_workflow, session, "java_fast_test", {"tests": ["Foo"]}) == terminal
    assert session.calls[1:] == [("java_fast_test", {"action": "result", "test_run_id": "T"})] * 2
