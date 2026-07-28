from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from jolink_runtime.launch import (
    BuildOperationSpec,
    LaunchCancelled,
    LaunchContext,
    LaunchControlError,
    LaunchController,
    LaunchErrorCode,
    LaunchPhase,
    LaunchPipelineFailure,
    ProcessSupervisor,
    RuntimeProcessState,
    TerminationReport,
)


def _wait_for_phase(
    controller: LaunchController,
    phase: LaunchPhase,
    timeout: float = 3.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = controller.snapshot()
        if snapshot["launch_phase"] == phase.value:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(
        f"phase {phase.value} was not reached: {controller.snapshot()}"
    )


def _successful_worker(context: LaunchContext) -> None:
    context.transition(LaunchPhase.RESOLVING_BUILD)
    context.transition(LaunchPhase.COMPILING)
    context.transition(LaunchPhase.RESOLVING_RUNTIME)
    context.transition(LaunchPhase.STARTING_JVM)
    context.set_process_observation(
        process_state=RuntimeProcessState.RUNNING,
        startup_state="unverified",
    )
    context.transition(LaunchPhase.RUNTIME_ACTIVE)


def test_controller_publishes_one_runtime_generation_and_requires_restart() -> None:
    releases: list[tuple[str, bool]] = []
    controller = LaunchController(
        runtime_releaser=lambda attempt, _deadline, force: (
            releases.append((attempt.attempt_id, force)) or True
        )
    )

    started = controller.start(_successful_worker)
    active = _wait_for_phase(controller, LaunchPhase.RUNTIME_ACTIVE)

    assert active["generation"] == started["generation"]
    assert active["process_state"] == "running"
    assert active["startup_state"] == "unverified"
    with pytest.raises(LaunchControlError) as duplicate:
        controller.start(_successful_worker)
    assert (
        duplicate.value.payload["error_code"]
        == "RUNTIME_ALREADY_RUNNING"
    )

    stopped = controller.cancel_current(
        deadline=time.monotonic() + 2.0
    )
    assert stopped["settled"] is True
    assert stopped["launch_phase"] == "stopped"
    assert releases == [(active["attempt_id"], False)]


def test_restart_waits_for_old_worker_and_invalidates_old_context() -> None:
    old_context: list[LaunchContext] = []
    old_started = threading.Event()

    def old_worker(context: LaunchContext) -> None:
        old_context.append(context)
        context.transition(LaunchPhase.RESOLVING_BUILD)
        old_started.set()
        context.cancel_event.wait(5.0)
        context.check_cancelled()

    controller = LaunchController(
        runtime_releaser=lambda _attempt, _deadline, _force: True
    )
    first = controller.start(old_worker)
    assert old_started.wait(2.0)

    second = controller.restart(
        _successful_worker,
        deadline=time.monotonic() + 2.0,
    )
    active = _wait_for_phase(controller, LaunchPhase.RUNTIME_ACTIVE)

    assert second["generation"] == first["generation"] + 1
    assert active["generation"] == second["generation"]
    with pytest.raises(LaunchCancelled):
        old_context[0].transition(LaunchPhase.COMPILING)


def test_cancel_releases_jvm_published_while_waiting_for_readiness() -> None:
    published = threading.Event()
    releases: list[str] = []

    def waiting_worker(context: LaunchContext) -> None:
        context.transition(LaunchPhase.RESOLVING_BUILD)
        context.transition(LaunchPhase.COMPILING)
        context.transition(LaunchPhase.RESOLVING_RUNTIME)
        context.transition(LaunchPhase.STARTING_JVM)
        context.set_process_observation(
            process_state=RuntimeProcessState.RUNNING,
            startup_state="starting",
        )
        context.transition(LaunchPhase.WAITING_READINESS)
        published.set()
        context.cancel_event.wait(5.0)
        context.check_cancelled()

    controller = LaunchController(
        runtime_releaser=lambda attempt, _deadline, _force: (
            releases.append(attempt.attempt_id) or True
        )
    )
    started = controller.start(waiting_worker)
    assert published.wait(2.0)

    stopped = controller.cancel_current(
        deadline=time.monotonic() + 2.0
    )

    assert stopped["settled"] is True
    assert stopped["launch_phase"] == "stopped"
    assert releases == [started["attempt_id"]]


def test_restart_refuses_new_generation_until_stubborn_worker_exits() -> None:
    release = threading.Event()
    entered = threading.Event()

    def stubborn(_context: LaunchContext) -> None:
        entered.set()
        release.wait(5.0)

    controller = LaunchController(
        runtime_releaser=lambda _attempt, _deadline, _force: True
    )
    first = controller.start(stubborn)
    assert entered.wait(2.0)

    with pytest.raises(LaunchControlError) as captured:
        controller.restart(
            _successful_worker,
            deadline=time.monotonic() + 0.05,
        )

    assert (
        captured.value.payload["error_code"]
        == "LAUNCH_CANCELLATION_TIMEOUT"
    )
    assert controller.snapshot()["generation"] == first["generation"]
    release.set()
    # Worker exit alone cannot prove that every process-tree owner settled.
    _wait_for_phase(controller, LaunchPhase.CANCELLING)
    settled = controller.cancel_current(
        deadline=time.monotonic() + 2.0,
    )
    assert settled["settled"] is True
    assert settled["launch_phase"] == LaunchPhase.CANCELLED.value


def test_background_failure_is_status_evidence_not_tool_failure() -> None:
    def failed(_context: LaunchContext) -> None:
        raise LaunchPipelineFailure(
            LaunchErrorCode.BUILD_FAILED,
            "The supervised Maven build failed.",
            retryable=True,
            suggested_next_step="Inspect build.log_tail and retry run.",
        )

    controller = LaunchController()
    controller.start(failed)
    snapshot = _wait_for_phase(controller, LaunchPhase.FAILED)

    assert snapshot["launch_error"] == {
        "error_code": "BUILD_FAILED",
        "message": "The supervised Maven build failed.",
        "retryable": True,
        "suggested_next_step": "Inspect build.log_tail and retry run.",
    }
    assert "error" not in snapshot
    assert "error_code" not in snapshot


def test_unknown_worker_exception_does_not_publish_exception_text() -> None:
    secret = "database-password-from-exception"

    def crashed(_context: LaunchContext) -> None:
        raise RuntimeError(secret)

    controller = LaunchController()
    controller.start(crashed)
    snapshot = _wait_for_phase(controller, LaunchPhase.FAILED)

    assert secret not in str(snapshot)
    assert snapshot["launch_error"]["error_code"] == "LAUNCH_WORKER_FAILED"


def test_terminal_failure_releases_published_runtime_before_replacement() -> None:
    releases: list[str] = []

    def failed_after_publish(context: LaunchContext) -> None:
        context.transition(LaunchPhase.RESOLVING_BUILD)
        context.transition(LaunchPhase.COMPILING)
        context.transition(LaunchPhase.RESOLVING_RUNTIME)
        context.transition(LaunchPhase.STARTING_JVM)
        context.set_process_observation(
            process_state=RuntimeProcessState.RUNNING,
            startup_state="starting",
        )
        raise LaunchPipelineFailure(
            LaunchErrorCode.JVM_START_FAILED,
            "The application exited during startup.",
            retryable=True,
            suggested_next_step="Inspect logs and retry run.",
        )

    controller = LaunchController(
        runtime_releaser=lambda attempt, _deadline, _force: (
            releases.append(attempt.attempt_id) or True
        )
    )
    failed = controller.start(failed_after_publish)
    _wait_for_phase(controller, LaunchPhase.FAILED)

    settled = controller.cancel_current(
        deadline=time.monotonic() + 2.0,
    )

    assert settled["settled"] is True
    assert settled["launch_phase"] == LaunchPhase.FAILED.value
    assert settled["process_state"] == RuntimeProcessState.ABSENT.value
    assert releases == [failed["attempt_id"]]

    replacement = controller.start(_successful_worker)
    assert replacement["generation"] == failed["generation"] + 1
    _wait_for_phase(controller, LaunchPhase.RUNTIME_ACTIVE)


def test_unsettled_probe_tree_fails_attempt_before_candidate_fallback(
    tmp_path: Path,
) -> None:
    termination_calls = 0
    continued_after_probe = threading.Event()

    class Terminator:
        @staticmethod
        def terminate(handle, **_kwargs):
            nonlocal termination_calls
            termination_calls += 1
            terminated = termination_calls >= 2
            return TerminationReport(
                pid=handle.pid,
                terminated=terminated,
                forced=terminated,
                remaining_pids=() if terminated else (handle.pid + 1,),
            )

    def worker(context: LaunchContext) -> None:
        context.transition(LaunchPhase.RESOLVING_BUILD)
        context.run_operation(
            BuildOperationSpec(
                argv=(sys.executable, "-c", "raise SystemExit(0)"),
                cwd=tmp_path,
                operation_name="build_java_probe",
            )
        )
        continued_after_probe.set()

    controller = LaunchController(
        supervisor=ProcessSupervisor(terminator=Terminator())
    )
    controller.start(worker)
    failed = _wait_for_phase(controller, LaunchPhase.FAILED)

    assert continued_after_probe.is_set() is False
    assert failed["launch_error"]["error_code"] == "BUILD_FAILED"
    assert failed["launch_error"]["operation_name"] == "build_java_probe"
    assert failed["launch_error"]["remaining_process_count"] == 1
    assert failed["build"]["running"] is True

    settled = controller.cancel_current(
        deadline=time.monotonic() + 2.0,
    )
    assert settled["settled"] is True
    assert settled["build"]["running"] is False


def test_close_is_idempotent_and_rejects_new_attempts() -> None:
    controller = LaunchController()
    first = controller.close(deadline=time.monotonic() + 1.0)
    second = controller.close(deadline=time.monotonic() + 1.0)

    assert first["settled"] is True
    assert second["settled"] is True
    with pytest.raises(LaunchControlError) as captured:
        controller.start(_successful_worker)
    assert captured.value.payload["error_code"] == "SERVER_SHUTTING_DOWN"
