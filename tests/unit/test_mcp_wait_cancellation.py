from __future__ import annotations

import threading
import time
from typing import Any

import anyio

from jolink_runtime.core.wait_state import WaitControl
from jolink_runtime.server import mcp_server as mcp_server_module
from jolink_runtime.server.mcp_server import RuntimeMCPBoundary


class _CancellableDispatcher:
    def __init__(
        self,
        *,
        hold_after_cancel: bool = False,
        ignore_interrupt: bool = False,
        block_close: bool = False,
    ) -> None:
        self.started = threading.Event()
        self.cancel_seen = threading.Event()
        self.allow_worker_exit = threading.Event()
        if not hold_after_cancel:
            self.allow_worker_exit.set()
        self.ignore_interrupt = ignore_interrupt
        self.close_release = threading.Event()
        if not block_close:
            self.close_release.set()
        self.calls: list[str] = []
        self.settled: list[tuple[str, int]] = []
        self.interrupt_count = 0
        self.close_count = 0
        self.force_close_count = 0

    def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        session_key: str = "default",
        wait_control: WaitControl | None = None,
    ) -> dict[str, Any]:
        action = (arguments or {}).get("action", "")
        self.calls.append(action or tool_name)
        if action != "wait_event":
            return {"ok": True, "status": action}
        assert wait_control is not None
        self.started.set()
        while not wait_control.cancelled:
            time.sleep(0.005)
        self.cancel_seen.set()
        self.allow_worker_exit.wait()
        return {"ok": True, "status": "internally_cancelled"}

    def settle_cancelled_wait(
        self,
        wait_control: WaitControl,
        *,
        session_key: str = "default",
    ) -> bool:
        self.settled.append(
            (wait_control.waiter_id, wait_control.wait_generation)
        )
        return True

    def interrupt_wait(self, session_key: str = "default") -> bool:
        self.interrupt_count += 1
        if not self.ignore_interrupt:
            self.allow_worker_exit.set()
        return True

    def close_session(self, session_key: str = "default") -> bool:
        self.close_count += 1
        self.close_release.wait()
        return True

    def force_close_session(self, session_key: str = "default") -> bool:
        self.force_close_count += 1
        return True

    def wait_for_close_session(
        self,
        session_key: str = "default",
        timeout: float | None = None,
    ) -> bool:
        return self.close_release.wait(timeout)


class _PublishWindowDispatcher(_CancellableDispatcher):
    """Return a suspension immediately so publication can be gated."""

    def __init__(self) -> None:
        super().__init__()
        self.suspension_active = False

    def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        session_key: str = "default",
        wait_control: WaitControl | None = None,
    ) -> dict[str, Any]:
        action = (arguments or {}).get("action", "")
        self.calls.append(action or tool_name)
        assert action == "wait_event"
        assert wait_control is not None
        self.started.set()
        self.suspension_active = True
        return {
            "ok": True,
            "status": "breakpoint_hit",
            "suspension_id": "susp_publish_race",
        }

    def settle_cancelled_wait(
        self,
        wait_control: WaitControl,
        *,
        session_key: str = "default",
    ) -> bool:
        assert wait_control.worker_done is True
        self.suspension_active = False
        return super().settle_cancelled_wait(
            wait_control,
            session_key=session_key,
        )


class _TwoPhaseDispatcher(_CancellableDispatcher):
    """Small deterministic dispatcher for arm -> trigger -> await tests."""

    def __init__(self) -> None:
        super().__init__()
        self.trigger = threading.Event()
        self.suspension_active = False
        self.resume_calls: list[str] = []

    def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        session_key: str = "default",
        wait_control: WaitControl | None = None,
    ) -> dict[str, Any]:
        action = (arguments or {}).get("action", "")
        self.calls.append(action or tool_name)
        if action == "resume":
            suspension_id = str((arguments or {}).get("suspension_id", ""))
            self.resume_calls.append(suspension_id)
            self.suspension_active = False
            return {"ok": True, "status": "resumed"}
        if action != "wait_event":
            return {"ok": True, "status": action}

        assert wait_control is not None
        self.started.set()
        wait_control.mark_armed(
            breakpoint_ids=["bp_001"],
            exception_ids=[7],
        )
        while not self.trigger.wait(0.005):
            if wait_control.cancelled:
                self.cancel_seen.set()
                return {"ok": True, "status": "internally_cancelled"}
        self.suspension_active = True
        return {
            "ok": True,
            "status": "breakpoint_hit",
            "breakpoint_id": "bp_001",
            "suspension_id": "susp_two_phase",
        }

    def settle_cancelled_wait(
        self,
        wait_control: WaitControl,
        *,
        session_key: str = "default",
    ) -> bool:
        assert wait_control.worker_done is True
        self.suspension_active = False
        return super().settle_cancelled_wait(
            wait_control,
            session_key=session_key,
        )

    def interrupt_wait(self, session_key: str = "default") -> bool:
        self.suspension_active = False
        return super().interrupt_wait(session_key)


def test_wait_result_claim_and_expiration_are_mutually_exclusive() -> None:
    control = WaitControl(waiter_id="race", wait_generation=1)
    control.publish_result({"ok": True, "suspension_id": "susp_race"})
    barrier = threading.Barrier(3)
    outcomes: list[tuple[str, bool]] = []

    def claim() -> None:
        barrier.wait()
        outcomes.append(("claimed", control.claim_result()))

    def expire() -> None:
        barrier.wait()
        outcomes.append(("expired", control.expire_unclaimed_result()))

    claim_thread = threading.Thread(target=claim)
    expire_thread = threading.Thread(target=expire)
    claim_thread.start()
    expire_thread.start()
    barrier.wait()
    claim_thread.join(timeout=1)
    expire_thread.join(timeout=1)

    assert sorted(success for _name, success in outcomes) == [False, True]
    assert control.result_disposition in {"claimed", "expired"}


def test_two_phase_wait_arms_before_trigger_then_awaits_hit() -> None:
    dispatcher = _TwoPhaseDispatcher()
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario() -> None:
        armed = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "arm",
                "timeout": 5,
            },
        )
        armed_payload = dict(armed.structuredContent or {})
        assert armed_payload["ok"] is True
        assert armed_payload["status"] == "armed"
        assert armed_payload["armed_breakpoint_ids"] == ["bp_001"]
        assert armed_payload["armed_exception_ids"] == [7]
        assert armed_payload["result_ready"] is False
        suggestion = armed_payload["suggested_next_step"].lower()
        assert (
            "start the target scenario without waiting for its response"
            in suggestion
        )
        assert "wait_mode='await'" in suggestion
        wait_handle = str(armed_payload["wait_handle"])

        rejected = await boundary.call_tool(
            "java_runtime",
            {"action": "status"},
        )
        assert rejected.isError is True
        assert rejected.structuredContent["error_code"] == (
            "ACTIVE_WAITER_EXISTS"
        )
        assert rejected.structuredContent["wait_handle"] == wait_handle

        dispatcher.trigger.set()
        hit = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "await",
                "wait_handle": wait_handle,
                "timeout": 1,
            },
        )
        hit_payload = dict(hit.structuredContent or {})
        assert hit_payload["status"] == "breakpoint_hit"
        assert hit_payload["breakpoint_id"] == "bp_001"
        assert hit_payload["wait_handle"] == wait_handle

        resumed = await boundary.call_tool(
            "java_runtime",
            {
                "action": "resume",
                "suspension_id": hit_payload["suspension_id"],
            },
        )
        assert resumed.structuredContent["status"] == "resumed"
        assert dispatcher.resume_calls == ["susp_two_phase"]

    anyio.run(scenario)


def test_two_phase_arm_does_not_retrigger_when_result_is_already_ready(
    monkeypatch: Any,
) -> None:
    dispatcher = _TwoPhaseDispatcher()
    dispatcher.trigger.set()
    boundary = RuntimeMCPBoundary(dispatcher)
    original_wait_until_ready = WaitControl.wait_until_ready

    def wait_until_result_is_ready(
        control: WaitControl,
        timeout: float | None = None,
    ) -> bool:
        if not original_wait_until_ready(control, timeout):
            return False
        return control.wait_until_result(timeout)

    # Make the event-before-arm-response race deterministic: the boundary's
    # ready wait returns only after the background worker publishes its hit.
    monkeypatch.setattr(
        WaitControl,
        "wait_until_ready",
        wait_until_result_is_ready,
    )

    async def scenario() -> None:
        armed = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "arm",
                "timeout": 5,
            },
        )
        armed_payload = dict(armed.structuredContent or {})
        assert armed_payload["status"] == "armed"
        assert armed_payload["result_ready"] is True
        suggestion = armed_payload["suggested_next_step"].lower()
        assert "wait result is already ready" in suggestion
        assert "wait_mode='await'" in suggestion
        assert "trigger the scenario now" not in suggestion
        assert "do not trigger the scenario again" in suggestion

        hit = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "await",
                "wait_handle": armed_payload["wait_handle"],
                "timeout": 1,
            },
        )
        hit_payload = dict(hit.structuredContent or {})
        assert hit_payload["status"] == "breakpoint_hit"

        resumed = await boundary.call_tool(
            "java_runtime",
            {
                "action": "resume",
                "suspension_id": hit_payload["suspension_id"],
            },
        )
        assert resumed.structuredContent["status"] == "resumed"

    anyio.run(scenario)


def test_two_phase_await_timeout_keeps_observation_active() -> None:
    dispatcher = _TwoPhaseDispatcher()
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario() -> None:
        armed = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "arm",
                "timeout": 5,
            },
        )
        wait_handle = str(armed.structuredContent["wait_handle"])
        waiting = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "await",
                "wait_handle": wait_handle,
                "timeout": 0.1,
            },
        )
        assert waiting.structuredContent["status"] == "waiting"
        assert boundary._active_background_waiter() is not None

        dispatcher.trigger.set()
        hit = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "await",
                "wait_handle": wait_handle,
                "timeout": 1,
            },
        )
        assert hit.structuredContent["status"] == "breakpoint_hit"

    anyio.run(scenario)


def test_cleanup_cancels_two_phase_wait_before_dispatch() -> None:
    dispatcher = _TwoPhaseDispatcher()
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario() -> None:
        armed = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "arm",
                "timeout": 5,
            },
        )
        wait_handle = str(armed.structuredContent["wait_handle"])
        cleaned = await boundary.call_tool(
            "java_runtime",
            {"action": "cleanup_debug_state"},
        )
        assert cleaned.structuredContent["status"] == "cleanup_debug_state"
        assert dispatcher.cancel_seen.is_set()
        assert len(dispatcher.settled) == 1
        assert dispatcher.settled[0][0].startswith("local_")
        assert dispatcher.settled[0][1] == 1
        assert boundary._find_wait(wait_handle) is None
        assert dispatcher.calls == ["wait_event", "cleanup_debug_state"]

    anyio.run(scenario)


def test_unclaimed_two_phase_suspension_is_resumed_and_expired() -> None:
    dispatcher = _TwoPhaseDispatcher()
    boundary = RuntimeMCPBoundary(
        dispatcher,
        unclaimed_suspension_grace_seconds=0.03,
    )

    async def scenario() -> None:
        armed = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "arm",
                "timeout": 5,
            },
        )
        wait_handle = str(armed.structuredContent["wait_handle"])
        dispatcher.trigger.set()
        with anyio.fail_after(1):
            while dispatcher.resume_calls != ["susp_two_phase"]:
                await anyio.sleep(0.005)

        expired = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "await",
                "wait_handle": wait_handle,
                "timeout": 1,
            },
        )
        assert expired.isError is True
        payload = dict(expired.structuredContent or {})
        assert payload["error_code"] == "WAIT_RESULT_EXPIRED"
        assert payload["invalidated_suspension_id"] == "susp_two_phase"
        assert dispatcher.suspension_active is False

    anyio.run(scenario)


def test_unclaimed_resume_exception_falls_back_to_jdwp_disconnect() -> None:
    class ResumeFailureDispatcher(_TwoPhaseDispatcher):
        def dispatch(
            self,
            tool_name: str,
            arguments: dict[str, Any] | None = None,
            *,
            session_key: str = "default",
            wait_control: WaitControl | None = None,
        ) -> dict[str, Any]:
            if (arguments or {}).get("action") == "resume":
                raise RuntimeError("resume failed")
            return super().dispatch(
                tool_name,
                arguments,
                session_key=session_key,
                wait_control=wait_control,
            )

    dispatcher = ResumeFailureDispatcher()
    boundary = RuntimeMCPBoundary(
        dispatcher,
        unclaimed_suspension_grace_seconds=0.03,
    )

    async def scenario() -> None:
        armed = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "arm",
                "timeout": 5,
            },
        )
        wait_handle = str(armed.structuredContent["wait_handle"])
        dispatcher.trigger.set()
        with anyio.fail_after(1):
            while dispatcher.interrupt_count != 1:
                await anyio.sleep(0.005)

        expired = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "await",
                "wait_handle": wait_handle,
                "timeout": 1,
            },
        )
        assert expired.structuredContent["error_code"] == "WAIT_RESULT_EXPIRED"
        assert expired.structuredContent["retryable"] is True
        assert dispatcher.suspension_active is False

    anyio.run(scenario)


def test_two_phase_wait_rejects_invalid_handle_combinations() -> None:
    boundary = RuntimeMCPBoundary(_TwoPhaseDispatcher())

    async def scenario() -> None:
        missing = await boundary.call_tool(
            "java_runtime",
            {"action": "wait_event", "wait_mode": "await", "timeout": 1},
        )
        assert missing.isError is True
        assert missing.structuredContent["error_code"] == (
            "INVALID_WAIT_ARGUMENTS"
        )
        assert missing.structuredContent["argument"] == "wait_handle"

        unknown = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "await",
                "wait_handle": "wait_missing",
                "timeout": 1,
            },
        )
        assert unknown.isError is True
        assert unknown.structuredContent["error_code"] == (
            "WAIT_HANDLE_NOT_FOUND"
        )

        blocking_handle = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "blocking",
                "wait_handle": "wait_invalid",
                "timeout": 1,
            },
        )
        assert blocking_handle.isError is True
        assert blocking_handle.structuredContent["error_code"] == (
            "INVALID_WAIT_ARGUMENTS"
        )

    anyio.run(scenario)


def test_cancel_after_worker_return_before_publication_settles_suspension(
    monkeypatch: Any,
) -> None:
    dispatcher = _PublishWindowDispatcher()
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario() -> None:
        publication_checkpoint = anyio.Event()
        caller_done = anyio.Event()
        holder: dict[str, anyio.CancelScope] = {}
        returned_payloads: list[dict[str, Any]] = []
        cancellations: list[bool] = []

        async def hold_publication() -> None:
            publication_checkpoint.set()
            await anyio.sleep_forever()

        monkeypatch.setattr(
            mcp_server_module,
            "checkpoint_if_cancelled",
            hold_publication,
        )

        async def wait_caller() -> None:
            with anyio.CancelScope() as scope:
                holder["scope"] = scope
                try:
                    result = await boundary.call_tool(
                        "java_runtime",
                        {"action": "wait_event", "timeout": 30},
                        request_id="request-publish-race",
                    )
                    returned_payloads.append(
                        dict(result.structuredContent or {})
                    )
                except anyio.get_cancelled_exc_class():
                    cancellations.append(True)
            caller_done.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(wait_caller)
            with anyio.fail_after(2):
                await publication_checkpoint.wait()
            assert dispatcher.suspension_active is True
            holder["scope"].cancel()
            with anyio.fail_after(2):
                await caller_done.wait()

        assert cancellations == [True]
        assert returned_payloads == []
        assert dispatcher.settled == [("request-publish-race", 1)]
        assert dispatcher.suspension_active is False
        assert boundary._current_waiter is None

    anyio.run(scenario)


def test_cancelled_wait_settles_before_next_call_enters_dispatcher() -> None:
    dispatcher = _CancellableDispatcher(hold_after_cancel=True)
    boundary = RuntimeMCPBoundary(
        dispatcher,
        cancellation_grace_seconds=1.0,
    )

    async def scenario() -> None:
        holder: dict[str, anyio.CancelScope] = {}
        caller_done = anyio.Event()

        async def wait_caller() -> None:
            with anyio.CancelScope() as scope:
                holder["scope"] = scope
                try:
                    await boundary.call_tool(
                        "java_runtime",
                        {"action": "wait_event", "timeout": 30},
                        request_id="request-a",
                    )
                except anyio.get_cancelled_exc_class():
                    pass
            caller_done.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(wait_caller)
            await anyio.to_thread.run_sync(dispatcher.started.wait)
            holder["scope"].cancel()
            await anyio.to_thread.run_sync(dispatcher.cancel_seen.wait)

            status_done = anyio.Event()

            async def call_status() -> None:
                await boundary.call_tool(
                    "java_runtime",
                    {"action": "status"},
                )
                status_done.set()

            tasks.start_soon(call_status)
            await anyio.sleep(0.05)
            assert dispatcher.calls == ["wait_event"]
            assert not status_done.is_set()

            dispatcher.allow_worker_exit.set()
            with anyio.fail_after(2):
                await caller_done.wait()
                await status_done.wait()

        assert dispatcher.calls == ["wait_event", "status"]
        assert dispatcher.settled == [("request-a", 1)]

    anyio.run(scenario)


def test_cancelled_wait_force_disconnects_after_grace_period() -> None:
    dispatcher = _CancellableDispatcher(hold_after_cancel=True)
    boundary = RuntimeMCPBoundary(
        dispatcher,
        cancellation_grace_seconds=0.05,
    )

    async def scenario() -> None:
        with anyio.CancelScope() as scope:
            async def cancel_soon() -> None:
                await anyio.to_thread.run_sync(dispatcher.started.wait)
                scope.cancel()

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(cancel_soon)
                try:
                    await boundary.call_tool(
                        "java_runtime",
                        {"action": "wait_event", "timeout": 30},
                        request_id="request-force",
                    )
                except anyio.get_cancelled_exc_class():
                    pass

        assert dispatcher.interrupt_count == 1
        assert dispatcher.settled == [("request-force", 1)]

    anyio.run(scenario)


def test_shutdown_actively_cancels_waiter_then_closes_session() -> None:
    dispatcher = _CancellableDispatcher()
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario() -> None:
        wait_finished = anyio.Event()

        async def wait_caller() -> None:
            result = await boundary.call_tool(
                "java_runtime",
                {"action": "wait_event", "timeout": 30},
                request_id="request-shutdown",
            )
            assert result.isError is True
            assert result.structuredContent["error_code"] == (
                "SERVER_SHUTTING_DOWN"
            )
            wait_finished.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(wait_caller)
            await anyio.to_thread.run_sync(dispatcher.started.wait)
            with anyio.fail_after(2):
                await boundary.shutdown()
                await wait_finished.wait()

        assert dispatcher.cancel_seen.is_set()
        assert dispatcher.close_count == 1
        assert dispatcher.settled == [("request-shutdown", 1)]

        rejected = await boundary.call_tool(
            "java_runtime",
            {"action": "status"},
        )
        assert rejected.isError is True
        assert rejected.structuredContent["error_code"] == (
            "SERVER_SHUTTING_DOWN"
        )

        await boundary.shutdown()
        assert dispatcher.close_count == 1

    anyio.run(scenario)


def test_call_queued_before_shutdown_rechecks_state_after_lock() -> None:
    dispatcher = _CancellableDispatcher()
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario() -> None:
        queued_result: dict[str, Any] = {}

        async def wait_caller() -> None:
            await boundary.call_tool(
                "java_runtime",
                {"action": "wait_event", "timeout": 30},
                request_id="request-queue",
            )

        async def queued_status() -> None:
            result = await boundary.call_tool(
                "java_runtime",
                {"action": "status"},
            )
            queued_result.update(result.structuredContent or {})

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(wait_caller)
            await anyio.to_thread.run_sync(dispatcher.started.wait)
            tasks.start_soon(queued_status)
            await anyio.sleep(0.02)
            await boundary.shutdown()

        assert queued_result["error_code"] == "SERVER_SHUTTING_DOWN"
        assert dispatcher.calls == ["wait_event"]

    anyio.run(scenario)


def test_stuck_cancelled_worker_poisoned_boundary_rejects_new_calls() -> None:
    dispatcher = _CancellableDispatcher(
        hold_after_cancel=True,
        ignore_interrupt=True,
    )
    boundary = RuntimeMCPBoundary(
        dispatcher,
        cancellation_grace_seconds=0.03,
    )

    async def scenario() -> None:
        holder: dict[str, anyio.CancelScope] = {}
        caller_done = anyio.Event()

        async def wait_caller() -> None:
            with anyio.CancelScope() as scope:
                holder["scope"] = scope
                try:
                    await boundary.call_tool(
                        "java_runtime",
                        {"action": "wait_event", "timeout": 30},
                        request_id="request-stuck",
                    )
                except anyio.get_cancelled_exc_class():
                    pass
            caller_done.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(wait_caller)
            await anyio.to_thread.run_sync(dispatcher.started.wait)
            holder["scope"].cancel()
            with anyio.fail_after(1):
                await caller_done.wait()

            rejected = await boundary.call_tool(
                "java_runtime",
                {"action": "status"},
            )
            assert rejected.isError is True
            assert rejected.structuredContent["error_code"] == (
                "SERVER_SHUTTING_DOWN"
            )
            assert dispatcher.calls == ["wait_event"]

            dispatcher.allow_worker_exit.set()
            with anyio.fail_after(1):
                while boundary._current_waiter is not None and (
                    not boundary._current_waiter.worker_done
                ):
                    await anyio.sleep(0.005)

        await boundary.shutdown()

    anyio.run(scenario)


def test_shutdown_is_bounded_when_wait_worker_ignores_interrupt() -> None:
    dispatcher = _CancellableDispatcher(
        hold_after_cancel=True,
        ignore_interrupt=True,
    )
    boundary = RuntimeMCPBoundary(
        dispatcher,
        cancellation_grace_seconds=0.03,
    )

    async def scenario() -> None:
        wait_finished = anyio.Event()

        async def wait_caller() -> None:
            await boundary.call_tool(
                "java_runtime",
                {"action": "wait_event", "timeout": 30},
                request_id="request-shutdown-stuck",
            )
            wait_finished.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(wait_caller)
            await anyio.to_thread.run_sync(dispatcher.started.wait)
            started = time.monotonic()
            with anyio.fail_after(0.5):
                await boundary.shutdown()
            assert time.monotonic() - started < 0.5
            assert dispatcher.force_close_count == 1
            assert dispatcher.close_count == 0

            dispatcher.allow_worker_exit.set()
            with anyio.fail_after(1):
                await wait_finished.wait()

    anyio.run(scenario)


def test_shutdown_force_closes_when_normal_close_blocks() -> None:
    dispatcher = _CancellableDispatcher(block_close=True)
    boundary = RuntimeMCPBoundary(
        dispatcher,
        cancellation_grace_seconds=0.03,
    )

    async def scenario() -> None:
        await boundary.call_tool(
            "java_runtime",
            {"action": "status"},
        )

        with anyio.fail_after(0.5):
            await boundary.shutdown()

        assert dispatcher.close_count == 1
        assert dispatcher.force_close_count == 1
        dispatcher.close_release.set()

    anyio.run(scenario)


def test_shutdown_interrupts_then_waits_for_normal_close_before_force() -> None:
    class WakeableCloseDispatcher(_CancellableDispatcher):
        def interrupt_wait(self, session_key: str = "default") -> bool:
            result = super().interrupt_wait(session_key)
            self.close_release.set()
            return result

    dispatcher = WakeableCloseDispatcher(block_close=True)
    boundary = RuntimeMCPBoundary(
        dispatcher,
        cancellation_grace_seconds=0.03,
    )

    async def scenario() -> None:
        await boundary.call_tool(
            "java_runtime",
            {"action": "status"},
        )
        with anyio.fail_after(0.5):
            await boundary.shutdown()

        assert dispatcher.close_count == 1
        assert dispatcher.interrupt_count == 1
        assert dispatcher.force_close_count == 0

    anyio.run(scenario)
