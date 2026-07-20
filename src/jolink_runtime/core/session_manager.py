"""Own Runtime instances independently from any agent or transport."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from ..adapters.base import Runtime


logger = logging.getLogger(__name__)

RuntimeFactory = Callable[[], Runtime]


class SessionManager:
    """Create and retain one Runtime instance for each explicit session key.

    Stage one keeps the Hermes-era isolation behavior while removing Hermes as
    the source of the key. The future stdio server will use the default key and
    therefore maintain one active Runtime target per server process.
    """

    def __init__(self, runtime_factory: RuntimeFactory) -> None:
        self._runtime_factory = runtime_factory
        self._runtimes: dict[str, Runtime] = {}
        self._closing_runtimes: dict[str, Runtime] = {}
        self._closing_done: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    def get_runtime(self, session_key: str = "default") -> Runtime:
        """Return the Runtime owned by ``session_key``, creating it if needed."""
        key = session_key or "default"
        with self._lock:
            runtime = self._runtimes.get(key)
            if runtime is None:
                if key in self._closing_runtimes:
                    raise RuntimeError(
                        f"Runtime session '{key}' is still closing"
                    )
                runtime = self._runtime_factory()
                self._runtimes[key] = runtime
                logger.info("java_runtime.session.created context=%s", key)
            return runtime

    def get_existing_runtime(
        self,
        session_key: str = "default",
    ) -> Runtime | None:
        """Return an allocated Runtime without creating a new session."""
        with self._lock:
            return self._runtimes.get(session_key or "default")

    def interrupt_wait(self, session_key: str = "default") -> bool:
        """Wake an existing Runtime's blocked event reader, if any."""
        key = session_key or "default"
        with self._lock:
            runtime = (
                self._runtimes.get(key)
                or self._closing_runtimes.get(key)
            )
        if runtime is None:
            return False
        try:
            runtime.interrupt_wait()
        except Exception:
            logger.exception(
                "java_runtime.session.wait_interrupt_failed context=%s",
                key,
            )
            return False
        return True

    def settle_cancelled_wait(
        self,
        wait_control: object,
        session_key: str = "default",
    ) -> bool:
        """Settle a cancelled wait on an existing Runtime without allocation."""
        key = session_key or "default"
        runtime = self.get_existing_runtime(key)
        if runtime is None:
            return False
        try:
            return bool(runtime.settle_cancelled_wait(wait_control))
        except Exception:
            logger.exception(
                "java_runtime.session.wait_settle_failed context=%s",
                key,
            )
            return False

    def close_session(self, session_key: str = "default") -> bool:
        """Remove and close an existing Runtime without allocating one."""
        key = session_key or "default"
        with self._lock:
            runtime = self._runtimes.pop(key, None)
            if runtime is None:
                # Shutdown is idempotent. A genuinely absent session is
                # already closed; an in-progress close is not yet complete.
                return key not in self._closing_runtimes
            self._closing_runtimes[key] = runtime
            close_done = threading.Event()
            self._closing_done[key] = close_done
        closed = False
        try:
            runtime.close()
            closed = True
        except Exception:
            # Runtime.close is specified as best-effort, but isolate a broken
            # adapter so one session cannot block server shutdown.
            logger.exception("java_runtime.session.close_failed context=%s", key)
            return False
        finally:
            close_done.set()
            if closed:
                with self._lock:
                    if self._closing_runtimes.get(key) is runtime:
                        self._closing_runtimes.pop(key, None)
                    if self._closing_done.get(key) is close_done:
                        self._closing_done.pop(key, None)
        return True

    def wait_for_close_session(
        self,
        session_key: str = "default",
        timeout: float | None = None,
    ) -> bool:
        """Wait for a normal close and report whether it completed cleanly."""
        key = session_key or "default"
        with self._lock:
            close_done = self._closing_done.get(key)
        if close_done is None:
            return True
        if not close_done.wait(timeout):
            return False
        with self._lock:
            return key not in self._closing_runtimes

    def force_close_session(self, session_key: str = "default") -> bool:
        """Release target ownership without waiting for debugger cleanup."""
        key = session_key or "default"
        with self._lock:
            runtime = self._runtimes.pop(key, None)
            if runtime is None:
                runtime = self._closing_runtimes.get(key)
            if runtime is None:
                return True
        try:
            runtime.force_close()
        except Exception:
            logger.exception(
                "java_runtime.session.force_close_failed context=%s",
                key,
            )
            return False
        with self._lock:
            if self._closing_runtimes.get(key) is runtime:
                self._closing_runtimes.pop(key, None)
            self._closing_done.pop(key, None)
        return True

    def close_all(self) -> None:
        """Remove and independently close every allocated Runtime."""
        with self._lock:
            keys = tuple(self._runtimes)
        for key in keys:
            self.close_session(key)

    def discard(self, session_key: str = "default") -> Runtime | None:
        """Forget a Runtime without changing the target JVM's state.

        Target cleanup belongs to an explicit Runtime action. Stage one does
        not add implicit detach, resume, or process termination behavior.
        """
        with self._lock:
            return self._runtimes.pop(session_key or "default", None)

    def clear(self) -> None:
        """Forget all Runtime instances without changing their target JVMs."""
        with self._lock:
            self._runtimes.clear()

    @property
    def session_keys(self) -> tuple[str, ...]:
        """Return the currently allocated session keys."""
        with self._lock:
            return tuple(self._runtimes)


__all__ = ["RuntimeFactory", "SessionManager"]
