from __future__ import annotations

import threading
import time

import pytest

from jolink_runtime.launch import (
    LaunchCancelled,
    LaunchContext,
    LaunchControlError,
    LaunchController,
    LaunchErrorCode,
    LaunchPhase,
    LaunchPipelineFailure,
    RuntimeProcessState,
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
    _wait_for_phase(controller, LaunchPhase.CANCELLED)


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


def test_close_is_idempotent_and_rejects_new_attempts() -> None:
    controller = LaunchController()
    first = controller.close(deadline=time.monotonic() + 1.0)
    second = controller.close(deadline=time.monotonic() + 1.0)

    assert first["settled"] is True
    assert second["settled"] is True
    with pytest.raises(LaunchControlError) as captured:
        controller.start(_successful_worker)
    assert captured.value.payload["error_code"] == "SERVER_SHUTTING_DOWN"
