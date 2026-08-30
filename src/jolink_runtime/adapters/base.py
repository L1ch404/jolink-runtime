"""Runtime adapter contract shared by language implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..core.models import RuntimeAction, RuntimeResult


class Runtime(ABC):
    """Agent-facing runtime manager.

    Subclasses implement language-specific lifecycle, observation, and debug.
    The public dispatcher only calls methods on this interface.
    """

    @abstractmethod
    def run(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def stop(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def restart(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def attach(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def detach(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def status(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def logs(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def breakpoint(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def exception(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def wait_breakpoint(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def wait_event(
        self,
        action: RuntimeAction,
        *,
        wait_control: Any | None = None,
    ) -> RuntimeResult:
        ...

    @abstractmethod
    def threads(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def stack(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def variables(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def resume(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def cleanup_debug_state(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def update(self, action: RuntimeAction) -> RuntimeResult:
        """Compile explicit sources and update the current runtime only."""
        ...

    @abstractmethod
    def test(self, action: RuntimeAction) -> RuntimeResult:
        """Incrementally compile and run explicit project tests."""
        ...

    @abstractmethod
    def cancel_test(self, action: RuntimeAction) -> RuntimeResult:
        """Cancel one active Fast Test attempt."""
        ...

    @abstractmethod
    def interrupt_wait(self) -> None:
        """Wake a currently blocked debug-event reader without target cleanup."""
        ...

    def settle_cancelled_wait(self, wait_control: Any) -> bool:
        """Finish adapter-specific cleanup after a cancelled wait worker exits."""
        return True

    @abstractmethod
    def close(self) -> bool | None:
        """Release this Runtime's target and debugger resources.

        This is an internal ownership-aware lifecycle hook for transport or
        session shutdown. Implementations must be idempotent and best-effort:
        they should log isolated cleanup failures rather than raising them.
        """
        ...

    @abstractmethod
    def force_close(self) -> bool | None:
        """Release target ownership without waiting for debugger cleanup."""
        ...


__all__ = ["Runtime"]
