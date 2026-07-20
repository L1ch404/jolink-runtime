"""Internal ownership state for cancellable Runtime event waits."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class WaitControl:
    """Coordinate one MCP ``wait_event`` worker across blocking or two-phase use."""

    waiter_id: str
    wait_generation: int
    poll_interval: float = 0.2
    wait_handle: str = ""
    background: bool = False
    event_timeout: float = 30.0
    created_at: float = field(default_factory=time.monotonic)
    _cancel_event: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )
    _worker_done: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )
    _ready_event: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )
    _result_event: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )
    _result_claimed_event: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )
    _state_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    phase: str = field(default="waiting", init=False)
    cancel_reason: str = field(default="", init=False)
    dirty: bool = field(default=False, init=False)
    armed_at: float | None = field(default=None, init=False)
    armed_breakpoint_ids: tuple[str, ...] = field(default=(), init=False)
    armed_exception_ids: tuple[int, ...] = field(default=(), init=False)
    result_payload: dict[str, Any] | None = field(default=None, init=False)
    result_disposition: str = field(default="pending", init=False)

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def worker_done(self) -> bool:
        return self._worker_done.is_set()

    def request_cancel(self, reason: str) -> None:
        with self._state_lock:
            if not self._cancel_event.is_set():
                self.cancel_reason = reason
                self.phase = "cancel_requested"
                self._cancel_event.set()

    def mark_phase(self, phase: str) -> None:
        with self._state_lock:
            self.phase = phase

    def mark_armed(
        self,
        *,
        breakpoint_ids: list[str] | tuple[str, ...] = (),
        exception_ids: list[int] | tuple[int, ...] = (),
    ) -> None:
        """Publish the point after every wait-scoped JDWP request is installed."""
        with self._state_lock:
            self.phase = "armed"
            self.armed_at = time.time()
            self.armed_breakpoint_ids = tuple(breakpoint_ids)
            self.armed_exception_ids = tuple(exception_ids)
            self._ready_event.set()

    def publish_result(self, payload: dict[str, Any]) -> None:
        """Publish one immutable worker result to a later ``await`` call."""
        with self._state_lock:
            self.result_payload = dict(payload)
            self._result_event.set()
            # Arm callers must also wake when setup failed before mark_armed().
            self._ready_event.set()

    def replace_result(self, payload: dict[str, Any]) -> None:
        """Replace an unclaimed result after automatic safety recovery."""
        with self._state_lock:
            self.result_payload = dict(payload)
            self._result_event.set()
            self._ready_event.set()

    def result_copy(self) -> dict[str, Any] | None:
        with self._state_lock:
            if self.result_payload is None:
                return None
            return dict(self.result_payload)

    @property
    def result_claimed(self) -> bool:
        with self._state_lock:
            return self.result_disposition == "claimed"

    def claim_result(self) -> bool:
        """Atomically win result ownership against safety expiration."""
        with self._state_lock:
            if self.result_disposition != "pending":
                return False
            self.result_disposition = "claimed"
            self._result_claimed_event.set()
            return True

    def expire_unclaimed_result(self) -> bool:
        """Atomically expire a result only when no await caller owns it."""
        with self._state_lock:
            if self.result_disposition != "pending":
                return False
            self.result_disposition = "expired"
            self._result_claimed_event.set()
            return True

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        return self._ready_event.wait(timeout)

    def wait_until_result(self, timeout: float | None = None) -> bool:
        return self._result_event.wait(timeout)

    def wait_until_result_claimed(self, timeout: float | None = None) -> bool:
        return self._result_claimed_event.wait(timeout)

    def mark_dirty(self) -> None:
        with self._state_lock:
            self.dirty = True
            self.phase = "cancelled_dirty"

    def mark_worker_done(self) -> None:
        with self._state_lock:
            if self.phase in {"waiting", "armed"}:
                self.phase = "done"
            elif self.phase == "cancel_requested":
                self.phase = "settling"
            self._ready_event.set()
            self._result_event.set()
            self._worker_done.set()

    def wait_until_worker_done(self, timeout: float | None = None) -> bool:
        return self._worker_done.wait(timeout)


__all__ = ["WaitControl"]
