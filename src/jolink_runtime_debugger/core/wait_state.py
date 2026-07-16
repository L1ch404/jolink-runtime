"""Internal ownership state for cancellable Runtime event waits."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class WaitControl:
    """Coordinate one MCP ``wait_event`` worker without exposing new schema."""

    waiter_id: str
    wait_generation: int
    poll_interval: float = 0.2
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
    _state_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    phase: str = field(default="waiting", init=False)
    cancel_reason: str = field(default="", init=False)
    dirty: bool = field(default=False, init=False)

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

    def mark_dirty(self) -> None:
        with self._state_lock:
            self.dirty = True
            self.phase = "cancelled_dirty"

    def mark_worker_done(self) -> None:
        with self._state_lock:
            if self.phase == "waiting":
                self.phase = "done"
            elif self.phase == "cancel_requested":
                self.phase = "settling"
            self._worker_done.set()

    def wait_until_worker_done(self, timeout: float | None = None) -> bool:
        return self._worker_done.wait(timeout)


__all__ = ["WaitControl"]
