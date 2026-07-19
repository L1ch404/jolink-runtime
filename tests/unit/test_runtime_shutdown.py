from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import anyio

from jolink_runtime_debugger.adapters.java.jdwp_adapter import (
    JavaRuntime,
    SuspensionSnapshot,
)
from jolink_runtime_debugger.adapters.java.jdwp_client import (
    Cmd,
    EventKind,
    IDSizes,
    SuspendPolicy,
)
from jolink_runtime_debugger.core.dispatcher import Dispatcher
from jolink_runtime_debugger.core.models import RuntimeAction, RuntimeResult
from jolink_runtime_debugger.core.session_manager import SessionManager
from jolink_runtime_debugger.core.wait_state import WaitControl
from jolink_runtime_debugger.server.mcp_server import RuntimeMCPBoundary


class _SessionRuntime:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.close_error = close_error
        self.close_count = 0
        self.force_close_count = 0
        self.interrupt_count = 0
        self.settled: list[object] = []
        self.wait_controls: list[object | None] = []

    def __getattr__(self, name: str):
        if name in {
            "run",
            "stop",
            "restart",
            "attach",
            "detach",
            "status",
            "logs",
            "breakpoint",
            "exception",
            "wait_breakpoint",
            "threads",
            "stack",
            "variables",
            "resume",
            "cleanup_debug_state",
        }:
            return lambda action: RuntimeResult(data={"status": name})
        raise AttributeError(name)

    def close(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error

    def interrupt_wait(self) -> None:
        self.interrupt_count += 1

    def settle_cancelled_wait(self, wait_control: object) -> bool:
        self.settled.append(wait_control)
        return True

    def force_close(self) -> None:
        self.force_close_count += 1

    def wait_event(
        self,
        action: Any,
        *,
        wait_control: object | None = None,
    ) -> RuntimeResult:
        self.wait_controls.append(wait_control)
        return RuntimeResult(data={"status": "timeout"})


def test_shutdown_helpers_do_not_allocate_a_missing_session() -> None:
    created = 0

    def factory() -> _SessionRuntime:
        nonlocal created
        created += 1
        return _SessionRuntime()

    dispatcher = Dispatcher(SessionManager(factory))
    control = WaitControl(waiter_id="waiter", wait_generation=1)

    assert dispatcher.interrupt_wait("missing") is False
    assert dispatcher.settle_cancelled_wait(
        control,
        session_key="missing",
    ) is False
    assert dispatcher.close_session("missing") is True
    assert created == 0
    assert dispatcher.sessions.session_keys == ()


def test_boundary_shutdown_with_absent_session_is_clean_and_idempotent() -> None:
    dispatcher = Dispatcher()
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario() -> None:
        await boundary.shutdown()
        await boundary.shutdown()

    anyio.run(scenario)

    assert dispatcher.sessions.session_keys == ()
    assert boundary._closed is True
    assert boundary._poisoned_reason == ""


def test_close_session_removes_and_closes_runtime_once() -> None:
    runtime = _SessionRuntime()
    sessions = SessionManager(lambda: runtime)
    assert sessions.get_runtime("dogfood") is runtime

    assert sessions.close_session("dogfood") is True
    assert sessions.close_session("dogfood") is True
    assert runtime.close_count == 1
    assert sessions.get_existing_runtime("dogfood") is None


def test_close_all_isolates_runtime_close_failures() -> None:
    broken = _SessionRuntime(close_error=RuntimeError("broken close"))
    healthy = _SessionRuntime()
    runtimes = iter((broken, healthy))
    sessions = SessionManager(lambda: next(runtimes))
    sessions.get_runtime("broken")
    sessions.get_runtime("healthy")

    sessions.close_all()

    assert broken.close_count == 1
    assert healthy.close_count == 1
    assert sessions.session_keys == ()


def test_session_helpers_report_runtime_failures() -> None:
    class BrokenRuntime(_SessionRuntime):
        def interrupt_wait(self) -> None:
            raise RuntimeError("interrupt failed")

        def settle_cancelled_wait(self, wait_control: object) -> bool:
            raise RuntimeError("settle failed")

        def force_close(self) -> None:
            raise RuntimeError("force close failed")

    control = WaitControl(waiter_id="waiter", wait_generation=1)

    interrupt_sessions = SessionManager(BrokenRuntime)
    interrupt_sessions.get_runtime()
    assert interrupt_sessions.interrupt_wait() is False

    settle_sessions = SessionManager(BrokenRuntime)
    settle_sessions.get_runtime()
    assert settle_sessions.settle_cancelled_wait(control) is False

    force_sessions = SessionManager(BrokenRuntime)
    force_sessions.get_runtime()
    assert force_sessions.force_close_session() is False

    close_sessions = SessionManager(
        lambda: _SessionRuntime(close_error=RuntimeError("close failed"))
    )
    close_sessions.get_runtime()
    assert close_sessions.close_session() is False
    assert close_sessions.wait_for_close_session(timeout=0) is False


def test_session_manager_waits_for_in_progress_close() -> None:
    class BlockingRuntime(_SessionRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.close_started = threading.Event()
            self.close_release = threading.Event()

        def close(self) -> None:
            self.close_count += 1
            self.close_started.set()
            self.close_release.wait()

    runtime = BlockingRuntime()
    sessions = SessionManager(lambda: runtime)
    sessions.get_runtime()
    closer = threading.Thread(target=sessions.close_session)
    closer.start()
    assert runtime.close_started.wait(timeout=1)

    assert sessions.wait_for_close_session(timeout=0.01) is False
    assert sessions.interrupt_wait() is True

    runtime.close_release.set()
    closer.join(timeout=1)
    assert not closer.is_alive()
    assert sessions.wait_for_close_session(timeout=0.01) is True


def test_clear_preserves_legacy_forget_without_close_semantics() -> None:
    runtime = _SessionRuntime()
    sessions = SessionManager(lambda: runtime)
    sessions.get_runtime()

    sessions.clear()

    assert runtime.close_count == 0
    assert sessions.session_keys == ()


def test_dispatcher_passes_wait_control_only_to_wait_event() -> None:
    runtime = _SessionRuntime()
    dispatcher = Dispatcher(SessionManager(lambda: runtime))
    control = WaitControl(waiter_id="waiter", wait_generation=1)

    result = dispatcher.dispatch(
        "java_runtime",
        {"action": "wait_event", "timeout": 0},
        wait_control=control,
    )

    assert result == {"ok": True, "status": "timeout"}
    assert runtime.wait_controls == [control]


@dataclass
class _ManagedProcess:
    owned: bool
    pid: int = 4321


class _ProcessManager:
    def __init__(self, *, owned: bool) -> None:
        self.current = _ManagedProcess(owned=owned)
        self.is_running = True
        self.stop_count = 0
        self.detach_count = 0

    def stop(self) -> dict[str, Any]:
        self.stop_count += 1
        if not self.current.owned:
            raise AssertionError("attached JVM must never enter stop")
        return {"status": "stopped", "pid": self.current.pid}

    def detach(self) -> dict[str, Any]:
        self.detach_count += 1
        if self.current.owned:
            raise AssertionError("owned JVM must use stop")
        return {"status": "detached", "pid": self.current.pid}


def _snapshot() -> SuspensionSnapshot:
    return SuspensionSnapshot(
        suspension_id="suspension",
        generation=1,
        request_id=17,
        thread_id=10,
        location={},
        observed_at="2026-07-16T00:00:00+00:00",
        suspend_policy=SuspendPolicy.EVENT_THREAD,
    )


def test_owned_runtime_close_still_stops_after_cleanup_failure() -> None:
    runtime = JavaRuntime()
    process = _ProcessManager(owned=True)
    snapshot = _snapshot()
    runtime._proc = process
    runtime._breakpoints = {17: {"line": 42}}
    runtime._exceptions = {18: {"class": "Ljava/lang/Exception;"}}
    runtime._active_suspension = snapshot
    runtime.cleanup_debug_state = lambda action: RuntimeResult(
        ok=False,
        error="cleanup failed",
    )

    runtime.close()
    runtime.close()

    assert process.stop_count == 1
    assert process.detach_count == 0
    assert runtime._breakpoints == {}
    assert runtime._exceptions == {}
    assert runtime._active_suspension is None
    assert snapshot.valid is False


def test_attached_runtime_close_detaches_even_when_cleanup_crashes() -> None:
    runtime = JavaRuntime()
    process = _ProcessManager(owned=False)
    runtime._proc = process

    def fail_cleanup(action: Any) -> RuntimeResult:
        raise RuntimeError("cleanup crashed")

    runtime.cleanup_debug_state = fail_cleanup

    runtime.close()

    assert process.stop_count == 0
    assert process.detach_count == 1


def test_force_close_owned_target_skips_jdwp_commands_and_stops() -> None:
    runtime = JavaRuntime()
    process = _ProcessManager(owned=True)
    client = _DebugClient()
    runtime._proc = process
    runtime._jdwp = client
    runtime._breakpoints = {17: {"line": 42}}
    runtime._active_suspension = _snapshot()

    runtime.force_close()
    runtime.force_close()

    assert client.commands == []
    assert client.close_count == 1
    assert process.stop_count == 1
    assert process.detach_count == 0
    assert runtime._active_suspension is None


def test_force_close_attached_target_never_terminates_it() -> None:
    runtime = JavaRuntime()
    process = _ProcessManager(owned=False)
    runtime._proc = process

    runtime.force_close()

    assert process.stop_count == 0
    assert process.detach_count == 1


def test_run_cleans_target_published_after_force_close_race() -> None:
    runtime = JavaRuntime()

    class Info:
        pid = 501
        jdwp_port = 5005
        launch_mode = "class"
        jar_path = ""
        main_class = "RaceMain"
        owned = True

    class RaceProcess:
        current = None
        stop_count = 0

        def start(self, **_kwargs: Any) -> Info:
            runtime.force_close()
            self.current = Info()
            return self.current

        def stop(self) -> dict[str, Any]:
            self.stop_count += 1
            pid = self.current.pid
            self.current = None
            return {"status": "stopped", "pid": pid}

        def detach(self) -> dict[str, Any]:
            raise AssertionError("owned late publication must be stopped")

    process = RaceProcess()
    runtime._proc = process
    runtime._log.create = lambda _label: "/tmp/race-run.log"

    result = runtime.run(RuntimeAction(
        action="run",
        classpath=".",
        main_class="RaceMain",
    ))

    assert result.ok is False
    assert result.data["error_code"] == "SERVER_SHUTTING_DOWN"
    assert process.stop_count == 1
    assert process.current is None


def test_attach_forgets_target_published_after_force_close_race() -> None:
    runtime = JavaRuntime()

    class Info:
        pid = 502
        jdwp_port = 5005
        launch_mode = "attached"
        main_class = "RaceAttach"
        owned = False

    class RaceProcess:
        current = None
        detach_count = 0

        def attach(self, **_kwargs: Any) -> Info:
            runtime.force_close()
            self.current = Info()
            return self.current

        def detach(self) -> dict[str, Any]:
            self.detach_count += 1
            pid = self.current.pid
            self.current = None
            return {"status": "detached", "pid": pid}

        def stop(self) -> dict[str, Any]:
            raise AssertionError("attached late publication must not be stopped")

    process = RaceProcess()
    runtime._proc = process
    runtime._connect = lambda: (_ for _ in ()).throw(
        AssertionError("closing attach must not connect JDWP")
    )

    result = runtime.attach(RuntimeAction(
        action="attach",
        pid=502,
        jdwp_port=5005,
        main_class="RaceAttach",
    ))

    assert result.ok is False
    assert result.data["error_code"] == "SERVER_SHUTTING_DOWN"
    assert process.detach_count == 1
    assert process.current is None


def test_force_close_releases_each_late_target_by_identity() -> None:
    @dataclass
    class Info:
        pid: int
        generation: int
        owned: bool = True

    class ExactProcessManager:
        def __init__(self, current: Info) -> None:
            self.current = current
            self.released: list[Info] = []

        def stop_target(self, expected: Info) -> dict[str, Any]:
            self.released.append(expected)
            if self.current is expected:
                self.current = None
            return {"status": "stopped", "pid": expected.pid}

        def detach_target(self, expected: Info) -> dict[str, Any]:
            raise AssertionError(f"owned target {expected.pid} must be stopped")

    first = Info(pid=101, generation=1)
    second = Info(pid=202, generation=2)
    process = ExactProcessManager(first)
    runtime = JavaRuntime()
    runtime._proc = process

    runtime.force_close()
    process.current = second
    closing = runtime._release_late_published_target(second, operation="run")

    assert closing is not None
    assert closing.data["error_code"] == "SERVER_SHUTTING_DOWN"
    assert [target.pid for target in process.released] == [101, 202]
    assert process.current is None


def test_force_close_releases_each_late_attached_target_by_identity() -> None:
    @dataclass
    class Info:
        pid: int
        generation: int
        owned: bool = False

    class ExactProcessManager:
        def __init__(self, current: Info) -> None:
            self.current = current
            self.released: list[Info] = []

        def stop_target(self, expected: Info) -> dict[str, Any]:
            raise AssertionError(f"attached target {expected.pid} must not be stopped")

        def detach_target(self, expected: Info) -> dict[str, Any]:
            self.released.append(expected)
            if self.current is expected:
                self.current = None
            return {"status": "detached", "pid": expected.pid}

    first = Info(pid=301, generation=1)
    second = Info(pid=302, generation=2)
    process = ExactProcessManager(first)
    runtime = JavaRuntime()
    runtime._proc = process

    runtime.force_close()
    process.current = second
    closing = runtime._release_late_published_target(second, operation="attach")

    assert closing is not None
    assert closing.data["error_code"] == "SERVER_SHUTTING_DOWN"
    assert [target.pid for target in process.released] == [301, 302]
    assert process.current is None


class _DebugClient:
    ids = IDSizes(8, 8, 8, 8, 8)

    def __init__(self) -> None:
        self.commands: list[tuple[int, int, bytes]] = []
        self.close_count = 0

    def drain_events(self) -> list[dict[str, Any]]:
        return []

    def command(
        self,
        command_set: int,
        command: int,
        data: bytes = b"",
    ) -> tuple[int, bytes]:
        self.commands.append((command_set, command, data))
        return 0, b""

    def close(self) -> None:
        self.close_count += 1


def test_close_resumes_snapshot_cleans_requests_then_detaches() -> None:
    runtime = JavaRuntime()
    process = _ProcessManager(owned=False)
    client = _DebugClient()
    runtime._proc = process
    runtime._jdwp = client
    runtime._connect = lambda: client
    runtime._breakpoints = {17: {"line": 42}}
    runtime._exceptions = {18: {"class": "Ljava/lang/Exception;"}}
    runtime._active_suspension = _snapshot()

    runtime.close()

    commands = [(command_set, command) for command_set, command, _ in client.commands]
    assert commands == [
        (Cmd.THREAD, 3),
        (Cmd.VM, 9),
    ]
    assert client.close_count == 1
    assert process.stop_count == 0
    assert process.detach_count == 1


def test_close_uses_emergency_resume_when_cleanup_returns_error() -> None:
    runtime = JavaRuntime()
    process = _ProcessManager(owned=False)
    client = _DebugClient()
    runtime._proc = process
    runtime._jdwp = client
    runtime._active_suspension = _snapshot()
    runtime.cleanup_debug_state = lambda action: RuntimeResult(
        ok=False,
        error="cleanup failed",
    )

    runtime.close()

    assert [
        (command_set, command)
        for command_set, command, _ in client.commands
    ] == [
        (Cmd.THREAD, 3),
        (Cmd.VM, 9),
    ]
    assert client.close_count == 1
    assert process.stop_count == 0
    assert process.detach_count == 1


def test_interrupt_wait_preserves_definitions_and_invalidates_live_requests() -> None:
    runtime = JavaRuntime()
    process = _ProcessManager(owned=False)
    client = _DebugClient()
    snapshot = _snapshot()
    runtime._proc = process
    runtime._jdwp = client
    runtime._breakpoints = {"bp_001": {"breakpoint_id": "bp_001", "line": 42}}
    runtime._exceptions = {18: {"exception_class": "Ljava/lang/Exception;"}}
    runtime._armed_breakpoint_requests = {17: "bp_001"}
    runtime._armed_exception_requests = {19: 18}
    runtime._active_suspension = snapshot

    runtime.interrupt_wait()
    runtime.interrupt_wait()

    assert client.close_count == 1
    assert runtime._jdwp is None
    assert set(runtime._breakpoints) == {"bp_001"}
    assert set(runtime._exceptions) == {18}
    assert runtime._armed_breakpoint_requests == {}
    assert runtime._armed_exception_requests == {}
    assert runtime._active_suspension is None
    assert snapshot.valid is False
    assert runtime._debug_connection_dirty is True
    assert "definitions were preserved" in runtime._debug_connection_warning
    assert process.stop_count == 0
    assert process.detach_count == 0


def test_status_explains_force_disconnected_debug_requests() -> None:
    class ProcessInfo:
        pid = 4321
        jdwp_port = 5005
        launch_mode = "attached"
        owned = False
        main_class = "Example"
        exit_code = None

        @staticmethod
        def is_alive() -> bool:
            return True

    class Process:
        current = ProcessInfo()

    runtime = JavaRuntime()
    runtime._proc = Process()
    runtime._debug_connection_dirty = True
    runtime._debug_connection_warning = "requests must be set again"
    runtime._connect = lambda: (_ for _ in ()).throw(
        RuntimeError("debugger disconnected")
    )

    status = runtime.status(RuntimeAction(action="status"))

    assert status.ok is True
    assert status.data["debug_requests_invalidated"] is True
    assert status.data["breakpoint_count"] == 0
    assert status.data["exception_count"] == 0
    assert status.data["warnings"] == ["requests must be set again"]
    assert "Retry wait_event" in status.data["suggested_next_step"]


def test_stale_connection_invalidates_requests_before_reconnect() -> None:
    class ProcessInfo:
        @staticmethod
        def is_alive() -> bool:
            return True

        jdwp_port = 5005
        pid = 4321

    class Process:
        current = ProcessInfo()

    class StaleClient:
        def __init__(self) -> None:
            self.close_count = 0

        def command(self, *_args: Any, **_kwargs: Any):
            raise OSError("connection reset")

        def close(self) -> None:
            self.close_count += 1

    runtime = JavaRuntime()
    runtime._proc = Process()
    client = StaleClient()
    runtime._jdwp = client
    runtime._breakpoints = {"bp_001": {"breakpoint_id": "bp_001", "line": 42}}
    runtime._exceptions = {18: {"exception_class": "Ljava/lang/Exception;"}}
    runtime._armed_breakpoint_requests = {17: "bp_001"}
    runtime._armed_exception_requests = {19: 18}

    try:
        runtime._connect()
    except RuntimeError as error:
        assert "JDWP connection changed" in str(error)
    else:  # pragma: no cover - failure path
        raise AssertionError("stale active debug state must require an explicit retry")

    assert client.close_count == 1
    assert runtime._jdwp is None
    assert set(runtime._breakpoints) == {"bp_001"}
    assert set(runtime._exceptions) == {18}
    assert runtime._armed_breakpoint_requests == {}
    assert runtime._armed_exception_requests == {}
    assert runtime._debug_connection_dirty is True
