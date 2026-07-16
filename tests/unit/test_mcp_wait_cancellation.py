from __future__ import annotations

import threading
import time
from typing import Any

import anyio

from jolink_runtime_debugger.core.wait_state import WaitControl
from jolink_runtime_debugger.server.mcp_server import RuntimeMCPBoundary


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
