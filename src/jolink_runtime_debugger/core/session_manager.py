"""Own Runtime instances independently from any agent or transport."""

from __future__ import annotations

import logging
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

    def get_runtime(self, session_key: str = "default") -> Runtime:
        """Return the Runtime owned by ``session_key``, creating it if needed."""
        key = session_key or "default"
        runtime = self._runtimes.get(key)
        if runtime is None:
            runtime = self._runtime_factory()
            self._runtimes[key] = runtime
            logger.info("java_runtime.session.created context=%s", key)
        return runtime

    def discard(self, session_key: str = "default") -> Runtime | None:
        """Forget a Runtime without changing the target JVM's state.

        Target cleanup belongs to an explicit Runtime action. Stage one does
        not add implicit detach, resume, or process termination behavior.
        """
        return self._runtimes.pop(session_key or "default", None)

    def clear(self) -> None:
        """Forget all Runtime instances without changing their target JVMs."""
        self._runtimes.clear()

    @property
    def session_keys(self) -> tuple[str, ...]:
        """Return the currently allocated session keys."""
        return tuple(self._runtimes)


__all__ = ["RuntimeFactory", "SessionManager"]
