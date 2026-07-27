"""Generation-safe ownership of one asynchronous project launch."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .contracts import (
    TERMINAL_LAUNCH_PHASES,
    BuildPlan,
    JvmLaunchPlan,
    LaunchAttempt,
    LaunchErrorCode,
    LaunchIntent,
    LaunchPhase,
    RuntimeProcessState,
    launch_rejection,
)
from .process_supervisor import AttemptToken, OperationResult, ProcessSupervisor
from .contracts import BuildOperationSpec


class LaunchCancelled(RuntimeError):
    """Internal cooperative-cancellation signal."""


class LaunchPipelineFailure(RuntimeError):
    """A redacted, structured failure safe to publish to the Agent."""

    def __init__(
        self,
        error_code: LaunchErrorCode | str,
        message: str,
        *,
        retryable: bool,
        suggested_next_step: str,
    ) -> None:
        super().__init__(message)
        self.error_code = str(error_code)
        self.retryable = bool(retryable)
        self.suggested_next_step = suggested_next_step


class LaunchControlError(RuntimeError):
    """Synchronous control-plane rejection."""

    def __init__(self, payload: dict[str, object]) -> None:
        super().__init__(str(payload.get("error", "Launch control failed.")))
        self.payload = payload


@dataclass
class _WorkerRecord:
    attempt: LaunchAttempt
    token: AttemptToken
    cancel_event: threading.Event
    done_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


RuntimeReleaser = Callable[[LaunchAttempt, float, bool], bool]
LaunchWorker = Callable[["LaunchContext"], None]


class LaunchContext:
    """Capabilities granted to exactly one current worker generation."""

    def __init__(
        self,
        controller: LaunchController,
        record: _WorkerRecord,
    ) -> None:
        self._controller = controller
        self._record = record

    @property
    def attempt_id(self) -> str:
        return self._record.attempt.attempt_id

    @property
    def generation(self) -> int:
        return self._record.attempt.generation

    @property
    def token(self) -> AttemptToken:
        return self._record.token

    @property
    def cancel_event(self) -> threading.Event:
        return self._record.cancel_event

    def check_cancelled(self) -> None:
        if self._record.cancel_event.is_set():
            raise LaunchCancelled("Project launch was cancelled.")
        if not self._controller._is_current(self._record):
            raise LaunchCancelled("Project launch generation is stale.")

    def transition(self, phase: LaunchPhase) -> None:
        self.check_cancelled()
        self._controller._transition_current(self._record, phase)

    def set_intent(self, intent: LaunchIntent) -> None:
        self._controller._set_current_field(self._record, "intent", intent)

    def set_build_plan(self, plan: BuildPlan) -> None:
        self._controller._set_current_field(
            self._record,
            "build_plan",
            plan,
        )

    def set_jvm_launch_plan(self, plan: JvmLaunchPlan) -> None:
        self._controller._set_current_field(
            self._record,
            "jvm_launch_plan",
            plan,
        )

    def set_process_observation(
        self,
        *,
        process_state: RuntimeProcessState,
        startup_state: str | None,
    ) -> None:
        self._controller._set_process_observation(
            self._record,
            process_state=process_state,
            startup_state=startup_state,
        )

    def run_operation(self, spec: BuildOperationSpec) -> OperationResult:
        self.check_cancelled()
        result = self._controller.supervisor.run(
            spec,
            owner=self._record.token,
        )
        if result.cancelled:
            raise LaunchCancelled("Project launch was cancelled.")
        self.check_cancelled()
        return result


class LaunchController:
    """Own one attempt/worker for one Runtime slot."""

    def __init__(
        self,
        *,
        supervisor: ProcessSupervisor | None = None,
        runtime_releaser: RuntimeReleaser | None = None,
    ) -> None:
        self.supervisor = supervisor or ProcessSupervisor()
        self._runtime_releaser = runtime_releaser
        self._generation = 0
        self._current: _WorkerRecord | None = None
        self._closing = False
        self._lock = threading.Lock()

    def start(self, worker: LaunchWorker) -> dict[str, object]:
        """Create one background generation and return its current snapshot."""
        with self._lock:
            if self._closing:
                raise LaunchControlError(
                    launch_rejection(
                        error_code="SERVER_SHUTTING_DOWN",
                        error="Runtime session is shutting down.",
                        retryable=True,
                        suggested_next_step=(
                            "Reconnect to a new Runtime MCP server and retry."
                        ),
                    )
                )
            current = self._current
            if current is not None and (
                current.attempt.phase not in TERMINAL_LAUNCH_PHASES
                or (
                    current.thread is not None
                    and current.thread.is_alive()
                )
            ):
                if (
                    current.attempt.phase
                    is LaunchPhase.RUNTIME_ACTIVE
                ):
                    error_code = LaunchErrorCode.RUNTIME_ALREADY_RUNNING
                    error = "A managed Runtime is already running."
                    next_step = (
                        "Call status to inspect the current Runtime, or call "
                        "restart to replace it explicitly."
                    )
                else:
                    error_code = (
                        LaunchErrorCode.LAUNCH_ALREADY_IN_PROGRESS
                    )
                    error = "A project launch is already in progress."
                    next_step = (
                        "Call status to observe the current attempt, or call "
                        "restart to replace it explicitly."
                    )
                raise LaunchControlError(
                    launch_rejection(
                        error_code=error_code,
                        error=error,
                        retryable=True,
                        suggested_next_step=next_step,
                        context=current.attempt.public_snapshot(),
                    )
                )

            self._generation += 1
            attempt = LaunchAttempt(
                attempt_id=f"launch_{uuid.uuid4().hex[:12]}",
                generation=self._generation,
            )
            attempt.transition(LaunchPhase.IMPORTING_LAUNCH)
            record = _WorkerRecord(
                attempt=attempt,
                token=AttemptToken(
                    attempt_id=attempt.attempt_id,
                    generation=attempt.generation,
                ),
                cancel_event=threading.Event(),
            )
            thread = threading.Thread(
                target=self._run_worker,
                args=(record, worker),
                name=f"jolink-launch-{attempt.generation}",
                daemon=True,
            )
            record.thread = thread
            self._current = record

        thread.start()
        return self.snapshot()

    def restart(
        self,
        worker: LaunchWorker,
        *,
        deadline: float,
    ) -> dict[str, object]:
        settlement = self.cancel_current(deadline=deadline)
        if not settlement["settled"]:
            raise LaunchControlError(
                launch_rejection(
                    error_code=(
                        LaunchErrorCode.LAUNCH_CANCELLATION_TIMEOUT
                    ),
                    error=(
                        "The previous project launch did not settle before "
                        "the restart deadline."
                    ),
                    retryable=True,
                    suggested_next_step=(
                        "Call stop again or restart the Runtime server before "
                        "starting another project launch."
                    ),
                    context={
                        "attempt_id": settlement.get("attempt_id"),
                        "launch_phase": settlement.get("launch_phase"),
                    },
                )
            )
        return self.start(worker)

    def cancel_current(self, *, deadline: float) -> dict[str, object]:
        with self._lock:
            record = self._current
            if record is None:
                return {
                    "requested": False,
                    "settled": True,
                    "launch_phase": LaunchPhase.IDLE.value,
                }
            attempt = record.attempt
            if attempt.phase in TERMINAL_LAUNCH_PHASES:
                thread = record.thread
                settled = thread is None or not thread.is_alive()
                return {
                    "requested": False,
                    "settled": settled,
                    **attempt.public_snapshot(),
                }
            was_runtime_active = (
                attempt.phase is LaunchPhase.RUNTIME_ACTIVE
            )
            attempt.request_cancel()
            record.cancel_event.set()
            thread = record.thread

        cancellation = self.supervisor.cancel(
            record.token,
            deadline=deadline,
        )
        runtime_released = True
        if was_runtime_active:
            runtime_released = self._release_runtime(
                attempt,
                deadline,
                force=False,
            )
        if thread is not None and thread.is_alive():
            thread.join(max(0.0, deadline - time.monotonic()))

        settled = bool(
            (thread is None or not thread.is_alive())
            and cancellation.settled
            and runtime_released
        )
        if settled:
            with self._lock:
                if self._current is record:
                    if attempt.phase is LaunchPhase.CANCELLING:
                        attempt.transition(LaunchPhase.CANCELLED)
                    elif attempt.phase is LaunchPhase.STOPPING:
                        attempt.transition(LaunchPhase.STOPPED)
        return {
            "requested": True,
            "settled": settled,
            **self.snapshot(),
        }

    def close(self, *, deadline: float) -> dict[str, object]:
        with self._lock:
            self._closing = True
        result = self.cancel_current(deadline=deadline)
        supervisor = self.supervisor.close(deadline=deadline)
        result["supervisor_settled"] = supervisor.settled
        result["settled"] = bool(
            result.get("settled", False) and supervisor.settled
        )
        return result

    def force_close(self, *, deadline: float) -> dict[str, object]:
        with self._lock:
            self._closing = True
            record = self._current
            if record is not None:
                record.cancel_event.set()
                if record.attempt.phase not in TERMINAL_LAUNCH_PHASES:
                    record.attempt.request_cancel()
        supervisor = self.supervisor.force_close(deadline=deadline)
        runtime_released = True
        if (
            record is not None
            and record.attempt.phase is LaunchPhase.STOPPING
        ):
            runtime_released = self._release_runtime(
                record.attempt,
                deadline,
                force=True,
            )
        worker_settled = True
        if record is not None and record.thread is not None:
            if record.thread.is_alive():
                record.thread.join(max(0.0, deadline - time.monotonic()))
            worker_settled = not record.thread.is_alive()
        if record is not None and runtime_released and worker_settled:
            with self._lock:
                if self._current is record:
                    if record.attempt.phase is LaunchPhase.STOPPING:
                        record.attempt.transition(LaunchPhase.STOPPED)
                    elif record.attempt.phase is LaunchPhase.CANCELLING:
                        record.attempt.transition(LaunchPhase.CANCELLED)
        return {
            "requested": record is not None,
            "settled": (
                supervisor.settled
                and runtime_released
                and worker_settled
            ),
            **self.snapshot(),
        }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            record = self._current
            if record is None:
                return {
                    "launch_phase": LaunchPhase.IDLE.value,
                    "process_state": RuntimeProcessState.ABSENT.value,
                }
            snapshot: dict[str, object] = record.attempt.public_snapshot()
            token = record.token
        snapshot["build"] = self.supervisor.snapshot(token)
        return snapshot

    def _run_worker(
        self,
        record: _WorkerRecord,
        worker: LaunchWorker,
    ) -> None:
        context = LaunchContext(self, record)
        try:
            worker(context)
            with self._lock:
                if self._current is not record:
                    return
                if record.cancel_event.is_set():
                    self._finish_cancelled_locked(record)
                elif record.attempt.phase not in (
                    LaunchPhase.RUNTIME_ACTIVE,
                    *TERMINAL_LAUNCH_PHASES,
                ):
                    self._fail_locked(
                        record,
                        LaunchPipelineFailure(
                            LaunchErrorCode.LAUNCH_WORKER_FAILED,
                            (
                                "The project-launch worker ended before "
                                "publishing a live Runtime."
                            ),
                            retryable=True,
                            suggested_next_step=(
                                "Inspect build progress and retry run."
                            ),
                        ),
                    )
        except LaunchCancelled:
            with self._lock:
                if self._current is record:
                    self._finish_cancelled_locked(record)
        except LaunchPipelineFailure as error:
            with self._lock:
                if self._current is record:
                    self._fail_locked(record, error)
        except Exception:
            with self._lock:
                if self._current is record:
                    self._fail_locked(
                        record,
                        LaunchPipelineFailure(
                            LaunchErrorCode.LAUNCH_WORKER_FAILED,
                            "The project-launch worker failed unexpectedly.",
                            retryable=True,
                            suggested_next_step=(
                                "Inspect the bounded build log and retry run."
                            ),
                        ),
                    )
        finally:
            record.done_event.set()

    def _transition_current(
        self,
        record: _WorkerRecord,
        phase: LaunchPhase,
    ) -> None:
        with self._lock:
            self._require_current_locked(record)
            if record.cancel_event.is_set():
                raise LaunchCancelled("Project launch was cancelled.")
            record.attempt.transition(phase)

    def _set_current_field(
        self,
        record: _WorkerRecord,
        name: str,
        value: object,
    ) -> None:
        with self._lock:
            self._require_current_locked(record)
            if record.cancel_event.is_set():
                raise LaunchCancelled("Project launch was cancelled.")
            setattr(record.attempt, name, value)

    def _set_process_observation(
        self,
        record: _WorkerRecord,
        *,
        process_state: RuntimeProcessState,
        startup_state: str | None,
    ) -> None:
        with self._lock:
            self._require_current_locked(record)
            if record.cancel_event.is_set():
                raise LaunchCancelled("Project launch was cancelled.")
            record.attempt.process_state = process_state
            record.attempt.startup_state = startup_state

    def _is_current(self, record: _WorkerRecord) -> bool:
        with self._lock:
            return (
                self._current is record
                and record.attempt.generation == self._generation
            )

    def _require_current_locked(self, record: _WorkerRecord) -> None:
        if (
            self._current is not record
            or record.attempt.generation != self._generation
        ):
            raise LaunchCancelled("Project launch generation is stale.")

    @staticmethod
    def _finish_cancelled_locked(record: _WorkerRecord) -> None:
        attempt = record.attempt
        if attempt.phase is LaunchPhase.CANCELLING:
            attempt.transition(LaunchPhase.CANCELLED)
        elif attempt.phase is LaunchPhase.STOPPING:
            # The runtime releaser owns the final STOPPED transition.
            return

    @staticmethod
    def _fail_locked(
        record: _WorkerRecord,
        error: LaunchPipelineFailure,
    ) -> None:
        attempt = record.attempt
        if record.cancel_event.is_set():
            LaunchController._finish_cancelled_locked(record)
            return
        if attempt.phase not in TERMINAL_LAUNCH_PHASES:
            attempt.transition(LaunchPhase.FAILED)
        attempt.error_code = error.error_code
        attempt.error_message = str(error)
        attempt.retryable = error.retryable
        attempt.suggested_next_step = error.suggested_next_step

    def _release_runtime(
        self,
        attempt: LaunchAttempt,
        deadline: float,
        *,
        force: bool,
    ) -> bool:
        releaser = self._runtime_releaser
        if releaser is None:
            return False
        return bool(releaser(attempt, deadline, force))


__all__ = [
    "LaunchCancelled",
    "LaunchContext",
    "LaunchControlError",
    "LaunchController",
    "LaunchPipelineFailure",
    "LaunchWorker",
    "RuntimeReleaser",
]
