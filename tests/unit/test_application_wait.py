import threading
from types import SimpleNamespace

import anyio
import pytest

from jolink_runtime.core.dispatcher import runtime_operation
from jolink_runtime.launch.application_wait import ApplicationWait, application_waiter
from jolink_runtime.launch.contracts import LaunchAttempt, LaunchPhase
from jolink_runtime.server import mcp_server
from jolink_runtime.server.mcp_server import RuntimeMCPBoundary


class Dispatcher:
    def __init__(self, action):
        self.action = action
        self.started = threading.Event()
        self.done = threading.Event()
        self.calls = []
        self.initial = {
            "ok": True,
            "status": "starting",
            "attempt_id" if action in {"launch", "restart"} else "test_run_id": "original",
        }
        self.final = {**self.initial, "status": "completed"}

    def dispatch(self, tool, args, **kwargs):
        self.calls.append((tool, args))
        operation = runtime_operation(tool, args)
        if operation == {"launch": "run", "restart": "restart", "test": "test"}[self.action]:
            self.started.set()
            return dict(self.initial)
        if operation in {"stop", "cancel_test"}:
            self.final.update(ok=False, status="cancelled", error="cancelled")
            self.done.set()
        return {"ok": True}

    def application_waiter(self, *args, **kwargs):
        return ApplicationWait(
            lambda: not self.done.is_set(),
            lambda: dict(self.final if self.done.is_set() else self.initial),
        )


def request(action, **args):
    if action == "test":
        return "java_fast_test", {"project_path": "/fixture", "tests": ["example.Test"], **args}
    if action == "cancel_test":
        return "java_fast_test", {"action": "cancel", "test_run_id": "original", **args}
    return "java_application", {"action": action, **args}


@pytest.mark.parametrize("action", ["launch", "restart", "test"])
def test_wait_returns_completed_result_without_status_polling(action):
    dispatcher = Dispatcher(action)
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario():
        async def finish():
            assert await anyio.to_thread.run_sync(lambda: dispatcher.started.wait(2))
            dispatcher.done.set()

        async with anyio.create_task_group() as group:
            group.start_soon(finish)
            result = await boundary.call_tool(*request(action))
        assert result.structuredContent == dispatcher.final
        assert len(dispatcher.calls) == 1

    anyio.run(scenario)


def test_launch_observer_keeps_original_attempt_after_replacement():
    original = SimpleNamespace(
        attempt=LaunchAttempt("old", 1, phase=LaunchPhase.COMPILING)
    )
    controller = SimpleNamespace(_lock=threading.Lock(), _current=original)
    runtime = SimpleNamespace(_launch_controller=controller)
    wait = application_waiter(runtime, "launch", {"ok": True, "attempt_id": "old"})
    assert wait.pending()
    original.attempt.phase = LaunchPhase.CANCELLED
    controller._current = SimpleNamespace(
        attempt=LaunchAttempt("new", 2, phase=LaunchPhase.RUNTIME_ACTIVE)
    )
    assert not wait.pending()
    # No Runtime status method: querying the replacement here would fail.
    result = wait.result()
    assert result["attempt_id"] == "old"
    assert result["ok"] is False and result["status"] == "cancelled"


@pytest.mark.parametrize("timeout", [0, 0.02])
@pytest.mark.parametrize("action", ["launch", "restart", "test"])
def test_timeout_returns_original_background_task(action, timeout):
    dispatcher = Dispatcher(action)
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario():
        result = await boundary.call_tool(*request(action, timeout=timeout))
        payload = result.structuredContent
        assert payload["status"] == "starting"
        assert payload.get("test_run_id", payload.get("attempt_id")) == "original"
        assert not dispatcher.done.is_set()
        assert "sleep" in payload["suggested_next_step"]
        assert "Start-Sleep" in payload["suggested_next_step"]
        assert "10" not in payload["suggested_next_step"]
        assert len(dispatcher.calls) == 1

    anyio.run(scenario)


@pytest.mark.parametrize("timeout", [30, 60, 10000])
def test_large_timeout_waits_only_thirty_without_rejection(monkeypatch, timeout):
    dispatcher = Dispatcher("test")
    boundary = RuntimeMCPBoundary(dispatcher)
    clock = [0.0]
    monkeypatch.setattr(mcp_server, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(mcp_server.anyio, "sleep", sleep)

    async def scenario():
        result = await boundary.call_tool(*request("test", timeout=timeout))
        assert result.isError is False
        assert clock[0] == pytest.approx(30)
        assert not dispatcher.done.is_set()

    anyio.run(scenario)


@pytest.mark.parametrize("action,cancel", [("launch", "stop"), ("restart", "stop"), ("test", "cancel_test")])
def test_wait_does_not_block_status_or_cancellation(action, cancel):
    dispatcher = Dispatcher(action)
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario():
        async def control():
            assert await anyio.to_thread.run_sync(lambda: dispatcher.started.wait(2))
            with anyio.fail_after(1):
                await boundary.call_tool("java_status", {"action": "status"})
                await boundary.call_tool(*request(cancel))

        async with anyio.create_task_group() as group:
            group.start_soon(control)
            result = await boundary.call_tool(*request(action))
        assert result.structuredContent["status"] == "cancelled"
        assert result.isError is True

    anyio.run(scenario)


def test_removed_startup_parameter_is_not_accepted():
    boundary = RuntimeMCPBoundary(Dispatcher("launch"))

    async def scenario():
        result = await boundary.call_tool(
            "java_application", {"action": "launch", "startup_wait_timeout_seconds": 1}
        )
        assert result.isError is True

    anyio.run(scenario)


@pytest.mark.parametrize("state", ["ready", "unverified", "failed", "starting"])
def test_direct_launch_guidance_matches_final_state(state):
    process = SimpleNamespace(pid=42, is_alive=lambda: state != "failed")
    runtime = SimpleNamespace(
        _proc=SimpleNamespace(
            current=process,
            observe_readiness=lambda _: {"startup_state": state},
        )
    )
    initial = {
        "ok": True,
        "pid": 42,
        "startup_state": "starting",
        "next_action": "status",
        "suggested_next_step": "still starting",
        "startup_wait_timed_out": True,
    }
    waiter = application_waiter(runtime, "launch", initial)
    result = waiter.result()
    assert waiter.pending() is (state == "starting")
    if state == "starting":
        assert result == initial
    else:
        assert "startup_wait_timed_out" not in result
        if state == "failed":
            assert result["ok"] is False and result["next_action"] == "logs"
            assert "starting" not in result["suggested_next_step"]
        else:
            assert result["ok"] is True
            assert "next_action" not in result and "suggested_next_step" not in result
    assert initial["next_action"] == "status"  # Do not mutate the submitted snapshot.
