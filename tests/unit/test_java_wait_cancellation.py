from __future__ import annotations

from typing import Any

from jolink_runtime.adapters.java.jdwp_adapter import (
    JavaRuntime,
    SuspensionSnapshot,
)
from jolink_runtime.adapters.java.jdwp_client import (
    Cmd,
    EventKind,
    IDSizes,
    SuspendPolicy,
)
from jolink_runtime.core.models import RuntimeAction
from jolink_runtime.core.wait_state import WaitControl


class _RunningProcess:
    is_running = True


class _EventClient:
    ids = IDSizes(8, 8, 8, 8, 8)

    def __init__(self) -> None:
        self.commands: list[tuple[int, int, bytes]] = []
        self.wait_timeouts: list[float] = []
        self.pending: list[dict[str, Any]] = []
        self.on_wait = None

    def drain_events(self) -> list[dict[str, Any]]:
        events = list(self.pending)
        self.pending.clear()
        return events

    def wait_for_event(self, timeout: float) -> dict[str, Any] | None:
        self.wait_timeouts.append(timeout)
        if self.on_wait is None:
            return None
        return self.on_wait()

    def command(
        self,
        command_set: int,
        command: int,
        data: bytes = b"",
    ) -> tuple[int, bytes]:
        self.commands.append((command_set, command, data))
        return 0, b""


class _ResumeFailureClient(_EventClient):
    def __init__(self, *, vm_resume_error: int) -> None:
        super().__init__()
        self.vm_resume_error = vm_resume_error
        self.close_count = 0

    def command(
        self,
        command_set: int,
        command: int,
        data: bytes = b"",
    ) -> tuple[int, bytes]:
        self.commands.append((command_set, command, data))
        if (command_set, command) == (Cmd.THREAD, 3):
            return 13, b""
        if (command_set, command) == (Cmd.VM, 9):
            return self.vm_resume_error, b""
        return 0, b""

    def close(self) -> None:
        self.close_count += 1


def _breakpoint_composite(request_id: int = 42) -> dict[str, Any]:
    return {
        "suspend_policy": SuspendPolicy.EVENT_THREAD,
        "events": [{
            "kind": EventKind.BREAKPOINT,
            "request_id": request_id,
            "thread_id": 10,
            "location": {
                "class_id": 20,
                "method_id": 30,
                "index": 40,
            },
        }],
    }


def _runtime(client: _EventClient) -> JavaRuntime:
    runtime = JavaRuntime()
    runtime._proc = _RunningProcess()
    runtime._breakpoints = {
        "bp_001": {
            "breakpoint_id": "bp_001",
            "class": "LExample;",
            "method": "run()V",
            "line": 123,
        }
    }
    runtime._connect = lambda: client
    runtime._describe_location = lambda _jdwp, _location: {
        "class": "LExample;",
        "method": "run()V",
        "line": 123,
    }
    runtime._thread_name = lambda _jdwp, _thread_id: "main"
    runtime._arm_debug_requests = lambda _jdwp, _kinds, _control: (
        runtime._armed_breakpoint_requests.update({42: "bp_001"}) or None
    )
    return runtime


def _disarm_commands() -> list[tuple[int, int, bytes]]:
    return [
        (Cmd.EVENT, 2, bytes([EventKind.BREAKPOINT]) + (42).to_bytes(4, "big")),
        (Cmd.VM, 1, b""),
    ]


def _control(generation: int = 1) -> WaitControl:
    return WaitControl(
        waiter_id=f"waiter-{generation}",
        wait_generation=generation,
        poll_interval=0.02,
    )


def test_wait_event_checks_cancellation_between_short_poll_slices() -> None:
    client = _EventClient()
    runtime = _runtime(client)
    control = _control()

    def cancel_without_event() -> None:
        control.request_cancel("test")
        return None

    client.on_wait = cancel_without_event
    result = runtime.wait_event(
        RuntimeAction(action="wait_event", timeout=30),
        wait_control=control,
    )

    assert result.data["status"] == "wait_cancelled"
    assert len(client.wait_timeouts) == 1
    assert client.wait_timeouts[0] <= control.poll_interval
    assert runtime._active_suspension is None
    assert set(runtime._breakpoints) == {"bp_001"}
    assert client.commands == _disarm_commands()


def test_event_arriving_after_cancel_is_resumed_without_suspension() -> None:
    client = _EventClient()
    runtime = _runtime(client)
    control = _control()

    def cancel_then_deliver() -> dict[str, Any]:
        control.request_cancel("test_race")
        return _breakpoint_composite()

    client.on_wait = cancel_then_deliver
    result = runtime.wait_event(
        RuntimeAction(action="wait_event", timeout=30),
        wait_control=control,
    )

    assert result.data["status"] == "wait_cancelled"
    assert runtime._active_suspension is None
    assert set(runtime._breakpoints) == {"bp_001"}
    assert client.commands == [
        (Cmd.THREAD, 3, (10).to_bytes(8, "big")),
        *_disarm_commands(),
    ]


def test_cancel_before_capture_falls_back_to_vm_resume() -> None:
    client = _ResumeFailureClient(vm_resume_error=0)
    runtime = _runtime(client)
    control = _control()

    def cancel_then_deliver() -> dict[str, Any]:
        control.request_cancel("before_capture")
        return _breakpoint_composite()

    client.on_wait = cancel_then_deliver
    result = runtime.wait_event(
        RuntimeAction(action="wait_event", timeout=30),
        wait_control=control,
    )

    assert result.data["status"] == "wait_cancelled"
    assert [(item[0], item[1]) for item in client.commands] == [
        (Cmd.THREAD, 3),
        (Cmd.VM, 9),
        (Cmd.EVENT, 2),
        (Cmd.VM, 1),
    ]
    assert client.close_count == 0
    assert set(runtime._breakpoints) == {"bp_001"}
    assert runtime._active_suspension is None


def test_cancel_before_capture_force_disconnects_if_all_resume_fails() -> None:
    client = _ResumeFailureClient(vm_resume_error=14)
    runtime = _runtime(client)
    runtime._jdwp = client
    control = _control()

    def cancel_then_deliver() -> dict[str, Any]:
        control.request_cancel("before_capture")
        return _breakpoint_composite()

    client.on_wait = cancel_then_deliver
    result = runtime.wait_event(
        RuntimeAction(action="wait_event", timeout=30),
        wait_control=control,
    )

    assert result.data["status"] == "wait_cancelled"
    assert [(item[0], item[1]) for item in client.commands] == [
        (Cmd.THREAD, 3),
        (Cmd.VM, 9),
    ]
    assert client.close_count == 1
    assert runtime._jdwp is None
    assert set(runtime._breakpoints) == {"bp_001"}
    assert runtime._debug_connection_dirty is True
    assert runtime._active_suspension is None


def test_cancel_after_suspension_creation_auto_resumes_owned_snapshot() -> None:
    client = _EventClient()
    runtime = _runtime(client)
    control = _control()
    original_capture = runtime._capture_breakpoint_event

    def capture_then_cancel(*args: Any, **kwargs: Any):
        result = original_capture(*args, **kwargs)
        control.request_cancel("after_capture")
        return result

    runtime._capture_breakpoint_event = capture_then_cancel
    client.on_wait = _breakpoint_composite

    result = runtime.wait_event(
        RuntimeAction(action="wait_event", timeout=30),
        wait_control=control,
    )

    assert result.data["status"] == "wait_cancelled"
    assert runtime._active_suspension is None
    assert control.phase == "settling"
    assert client.commands == [
        (Cmd.THREAD, 3, (10).to_bytes(8, "big")),
        *_disarm_commands(),
    ]


def test_settle_cancelled_wait_does_not_resume_another_waiter_snapshot() -> None:
    client = _EventClient()
    runtime = _runtime(client)
    old_control = _control(1)
    old_control.request_cancel("old_cancel")
    newer_control = _control(2)
    runtime._jdwp = client
    runtime._active_suspension = SuspensionSnapshot(
        suspension_id="susp_newer",
        generation=2,
        request_id=42,
        thread_id=10,
        location={},
        observed_at="2026-07-16T00:00:00+00:00",
        waiter_id=newer_control.waiter_id,
        wait_generation=newer_control.wait_generation,
        suspend_policy=SuspendPolicy.EVENT_THREAD,
    )

    runtime.settle_cancelled_wait(old_control)

    assert runtime._active_suspension is not None
    assert runtime._active_suspension.suspension_id == "susp_newer"
    assert client.commands == []


def test_settle_clears_owned_snapshot_after_forced_disconnect() -> None:
    runtime = _runtime(_EventClient())
    control = _control()
    control.request_cancel("forced_disconnect")
    snapshot = SuspensionSnapshot(
        suspension_id="susp_late",
        generation=1,
        request_id=42,
        thread_id=10,
        location={},
        observed_at="2026-07-16T00:00:00+00:00",
        waiter_id=control.waiter_id,
        wait_generation=control.wait_generation,
        suspend_policy=SuspendPolicy.EVENT_THREAD,
    )
    runtime._jdwp = None
    runtime._active_suspension = snapshot

    settled = runtime.settle_cancelled_wait(control)

    assert settled is True
    assert snapshot.resumed is True
    assert snapshot.valid is False
    assert runtime._active_suspension is None


def test_settle_cancelled_wait_drains_and_resumes_pending_event() -> None:
    client = _EventClient()
    runtime = _runtime(client)
    control = _control()
    control.request_cancel("client_cancel")
    runtime._jdwp = client
    client.pending.append(_breakpoint_composite())

    runtime.settle_cancelled_wait(control)

    assert runtime._active_suspension is None
    assert set(runtime._breakpoints) == {"bp_001"}
    assert client.commands == [
        (Cmd.THREAD, 3, (10).to_bytes(8, "big")),
    ]


def test_thread_resume_failure_falls_back_to_vm_resume() -> None:
    client = _ResumeFailureClient(vm_resume_error=0)
    runtime = _runtime(client)
    control = _control()
    control.request_cancel("resume_fallback")
    runtime._jdwp = client
    snapshot = SuspensionSnapshot(
        suspension_id="susp_owned",
        generation=1,
        request_id=42,
        thread_id=10,
        location={},
        observed_at="2026-07-16T00:00:00+00:00",
        waiter_id=control.waiter_id,
        wait_generation=control.wait_generation,
        suspend_policy=SuspendPolicy.EVENT_THREAD,
    )
    runtime._active_suspension = snapshot

    settled = runtime.settle_cancelled_wait(control)

    assert settled is True
    assert control.dirty is True
    assert snapshot.resumed is True
    assert snapshot.valid is False
    assert runtime._active_suspension is None
    assert [(item[0], item[1]) for item in client.commands] == [
        (Cmd.THREAD, 3),
        (Cmd.THREAD, 3),
        (Cmd.VM, 9),
    ]


def test_failed_thread_and_vm_resume_preserves_diagnostic_suspension() -> None:
    client = _ResumeFailureClient(vm_resume_error=14)
    runtime = _runtime(client)
    control = _control()
    control.request_cancel("resume_failed")
    runtime._jdwp = client
    snapshot = SuspensionSnapshot(
        suspension_id="susp_owned",
        generation=1,
        request_id=42,
        thread_id=10,
        location={},
        observed_at="2026-07-16T00:00:00+00:00",
        waiter_id=control.waiter_id,
        wait_generation=control.wait_generation,
        suspend_policy=SuspendPolicy.EVENT_THREAD,
    )
    runtime._active_suspension = snapshot

    settled = runtime.settle_cancelled_wait(control)

    assert settled is False
    assert control.dirty is True
    assert snapshot.resumed is False
    assert snapshot.valid is True
    assert runtime._active_suspension is snapshot


def test_composite_event_thread_resume_covers_every_suspended_thread() -> None:
    client = _EventClient()
    runtime = _runtime(client)
    composite = _breakpoint_composite()
    composite["events"].append({
        "kind": EventKind.BREAKPOINT,
        "request_id": 42,
        "thread_id": 11,
        "location": {
            "class_id": 20,
            "method_id": 30,
            "index": 41,
        },
    })
    runtime._armed_breakpoint_requests = {42: "bp_001"}

    hit = runtime._handle_debug_composite(
        client,
        composite,
        {EventKind.BREAKPOINT},
        "debug_event",
    )
    assert hit is not None
    assert runtime._active_suspension is not None
    assert runtime._active_suspension.suspended_thread_ids == (10, 11)

    resumed = runtime.resume(RuntimeAction(
        action="resume",
        suspension_id=hit.data["suspension_id"],
    ))

    assert resumed.ok is True
    assert client.commands == [
        (Cmd.THREAD, 3, (10).to_bytes(8, "big")),
        (Cmd.THREAD, 3, (11).to_bytes(8, "big")),
    ]


def test_cancel_event_race_is_stable_across_repeated_waiters() -> None:
    for generation in range(1, 51):
        client = _EventClient()
        runtime = _runtime(client)
        control = _control(generation)
        if generation % 2:
            def deliver_after_cancel(
                *,
                current_control: WaitControl = control,
            ) -> dict[str, Any]:
                current_control.request_cancel("stress_before_capture")
                return _breakpoint_composite()

            client.on_wait = deliver_after_cancel
        else:
            original_capture = runtime._capture_breakpoint_event

            def capture_then_cancel(
                *args: Any,
                current_control: WaitControl = control,
                current_capture=original_capture,
                **kwargs: Any,
            ):
                result = current_capture(*args, **kwargs)
                current_control.request_cancel("stress_after_capture")
                return result

            runtime._capture_breakpoint_event = capture_then_cancel
            client.on_wait = _breakpoint_composite

        result = runtime.wait_event(
            RuntimeAction(action="wait_event", timeout=30),
            wait_control=control,
        )

        assert result.data["status"] == "wait_cancelled"
        assert runtime._active_suspension is None
        assert set(runtime._breakpoints) == {"bp_001"}
        assert client.commands == [
            (Cmd.THREAD, 3, (10).to_bytes(8, "big")),
            *_disarm_commands(),
        ]
