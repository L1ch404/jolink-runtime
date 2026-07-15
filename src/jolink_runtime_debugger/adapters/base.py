"""Runtime adapter contract shared by language implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod

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
    def wait_event(self, action: RuntimeAction) -> RuntimeResult:
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


__all__ = ["Runtime"]
