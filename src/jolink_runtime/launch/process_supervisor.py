"""Cancellable execution of short-lived project-build operations."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .contracts import BuildOperationSpec
from .process_tree import (
    ProcessTreeHandle,
    ProcessTreeTerminator,
    TerminationReport,
)


_IS_WINDOWS = os.name == "nt"
_CREATE_NEW_PROCESS_GROUP = (
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if _IS_WINDOWS
    else 0
)


@dataclass(frozen=True)
class AttemptToken:
    attempt_id: str
    generation: int


@dataclass(frozen=True)
class OperationResult:
    operation_name: str
    return_code: int | None
    cancelled: bool
    timed_out: bool
    started_at: float
    finished_at: float
    output_capture: Path | None
    termination: TerminationReport | None = None

    @property
    def succeeded(self) -> bool:
        return (
            self.return_code == 0
            and not self.cancelled
            and not self.timed_out
        )


@dataclass(frozen=True)
class CancellationReport:
    owner: AttemptToken | None
    requested: bool
    settled: bool
    terminations: tuple[TerminationReport, ...] = ()


@dataclass
class _OwnerState:
    cancelled: threading.Event = field(default_factory=threading.Event)
    handles: dict[int, ProcessTreeHandle] = field(default_factory=dict)


class ProcessSupervisor:
    """Own every build-tool process by exact attempt identity."""

    def __init__(
        self,
        terminator: ProcessTreeTerminator | None = None,
    ) -> None:
        self._terminator = terminator or ProcessTreeTerminator()
        self._owners: dict[AttemptToken, _OwnerState] = {}
        self._closing = False
        self._lock = threading.Lock()

    def run(
        self,
        spec: BuildOperationSpec,
        *,
        owner: AttemptToken,
    ) -> OperationResult:
        """Run one operation without holding the registry lock while waiting."""
        started_at = time.monotonic()
        state = self._owner_state(owner)
        if state is None or state.cancelled.is_set():
            return self._cancelled_before_spawn(spec, started_at)

        output_file = None
        process: subprocess.Popen[bytes] | None = None
        handle: ProcessTreeHandle | None = None
        try:
            if spec.output_capture is not None:
                spec.output_capture.parent.mkdir(parents=True, exist_ok=True)
                output_file = spec.output_capture.open("ab")
            environment = os.environ.copy()
            environment.update(
                {
                    str(key): str(value)
                    for key, value in spec.environment.items()
                }
            )
            popen_kwargs: dict[str, Any] = {
                "cwd": str(spec.cwd),
                "env": environment,
                "stdin": subprocess.DEVNULL,
                "stdout": output_file or subprocess.DEVNULL,
                "stderr": subprocess.STDOUT,
            }
            if _IS_WINDOWS:
                popen_kwargs["creationflags"] = _CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
            process = subprocess.Popen(list(spec.argv), **popen_kwargs)
            handle = ProcessTreeHandle.from_process(process)

            if not self._publish_handle(owner, state, handle):
                termination = self._terminator.terminate(
                    handle,
                    deadline=time.monotonic() + 5.0,
                    force=True,
                )
                return OperationResult(
                    operation_name=spec.operation_name,
                    return_code=process.poll(),
                    cancelled=True,
                    timed_out=False,
                    started_at=started_at,
                    finished_at=time.monotonic(),
                    output_capture=spec.output_capture,
                    termination=termination,
                )

            deadline = (
                started_at + spec.timeout_seconds
                if spec.timeout_seconds is not None
                else None
            )
            cancelled = False
            timed_out = False
            termination = None
            while process.poll() is None:
                handle.refresh_identity_tree()
                if state.cancelled.wait(0.05):
                    cancelled = True
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    break
            if cancelled or timed_out:
                termination = self._terminator.terminate(
                    handle,
                    deadline=time.monotonic() + 5.0,
                    force=False,
                )
            return OperationResult(
                operation_name=spec.operation_name,
                return_code=process.poll(),
                cancelled=cancelled,
                timed_out=timed_out,
                started_at=started_at,
                finished_at=time.monotonic(),
                output_capture=spec.output_capture,
                termination=termination,
            )
        finally:
            if handle is not None:
                self._forget_handle(owner, state, handle)
            if output_file is not None:
                output_file.close()

    def cancel(
        self,
        owner: AttemptToken,
        *,
        deadline: float,
    ) -> CancellationReport:
        with self._lock:
            state = self._owners.get(owner)
            if state is None:
                return CancellationReport(
                    owner=owner,
                    requested=False,
                    settled=True,
                )
            state.cancelled.set()
            handles = tuple(state.handles.values())

        terminations = tuple(
            self._terminator.terminate(
                handle,
                deadline=deadline,
                force=False,
            )
            for handle in handles
        )
        settled = self._wait_owner_handles_empty(owner, deadline)
        return CancellationReport(
            owner=owner,
            requested=True,
            settled=settled and all(
                report.terminated for report in terminations
            ),
            terminations=terminations,
        )

    def close(self, *, deadline: float) -> CancellationReport:
        with self._lock:
            self._closing = True
            items = tuple(self._owners.items())
            for _owner, state in items:
                state.cancelled.set()
            handles = tuple(
                handle
                for _owner, state in items
                for handle in state.handles.values()
            )
        terminations = tuple(
            self._terminator.terminate(
                handle,
                deadline=deadline,
                force=False,
            )
            for handle in handles
        )
        return CancellationReport(
            owner=None,
            requested=bool(items),
            settled=all(report.terminated for report in terminations),
            terminations=terminations,
        )

    def force_close(self, *, deadline: float) -> CancellationReport:
        with self._lock:
            self._closing = True
            items = tuple(self._owners.items())
            for _owner, state in items:
                state.cancelled.set()
            handles = tuple(
                handle
                for _owner, state in items
                for handle in state.handles.values()
            )
        terminations = tuple(
            self._terminator.terminate(
                handle,
                deadline=deadline,
                force=True,
            )
            for handle in handles
        )
        return CancellationReport(
            owner=None,
            requested=bool(items),
            settled=all(report.terminated for report in terminations),
            terminations=terminations,
        )

    def snapshot(self, owner: AttemptToken) -> dict[str, Any]:
        with self._lock:
            state = self._owners.get(owner)
            handles = tuple(state.handles.values()) if state else ()
            cancelled = bool(state and state.cancelled.is_set())
        return {
            "running": bool(handles),
            "running_operation_count": len(handles),
            "cancel_requested": cancelled,
            "pids": [handle.pid for handle in handles],
        }

    def _owner_state(self, owner: AttemptToken) -> _OwnerState | None:
        with self._lock:
            if self._closing:
                return None
            return self._owners.setdefault(owner, _OwnerState())

    def _publish_handle(
        self,
        owner: AttemptToken,
        expected: _OwnerState,
        handle: ProcessTreeHandle,
    ) -> bool:
        with self._lock:
            current = self._owners.get(owner)
            if (
                self._closing
                or current is not expected
                or expected.cancelled.is_set()
            ):
                return False
            expected.handles[handle.pid] = handle
            return True

    def _forget_handle(
        self,
        owner: AttemptToken,
        expected: _OwnerState,
        handle: ProcessTreeHandle,
    ) -> None:
        with self._lock:
            current = self._owners.get(owner)
            if current is not expected:
                return
            expected.handles.pop(handle.pid, None)
            if not expected.handles and expected.cancelled.is_set():
                self._owners.pop(owner, None)

    def _wait_owner_handles_empty(
        self,
        owner: AttemptToken,
        deadline: float,
    ) -> bool:
        while True:
            with self._lock:
                current = self._owners.get(owner)
                if current is None or not current.handles:
                    if current is not None and current.cancelled.is_set():
                        self._owners.pop(owner, None)
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    @staticmethod
    def _cancelled_before_spawn(
        spec: BuildOperationSpec,
        started_at: float,
    ) -> OperationResult:
        return OperationResult(
            operation_name=spec.operation_name,
            return_code=None,
            cancelled=True,
            timed_out=False,
            started_at=started_at,
            finished_at=time.monotonic(),
            output_capture=spec.output_capture,
        )


__all__ = [
    "AttemptToken",
    "CancellationReport",
    "OperationResult",
    "ProcessSupervisor",
]
