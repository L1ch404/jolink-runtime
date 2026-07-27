from __future__ import annotations

from pathlib import Path

import pytest

from jolink_runtime.launch import (
    BuildOperationSpec,
    BuildPlan,
    JvmLaunchPlan,
    LaunchAttempt,
    LaunchContractError,
    LaunchErrorCode,
    LaunchIntent,
    LaunchPhase,
    RuntimeProcessState,
    launch_rejection,
)
from jolink_runtime.server.tool_schema import get_mcp_tools


def test_planned_project_launch_is_not_advertised_before_integration() -> None:
    properties = get_mcp_tools()[0].inputSchema["properties"]

    assert "project_path" not in properties
    assert "launch_name" not in properties


def test_launch_attempt_uses_existing_process_states_and_omits_premature_readiness() -> None:
    attempt = LaunchAttempt(attempt_id="launch_1", generation=1)
    attempt.transition(LaunchPhase.IMPORTING_LAUNCH)

    snapshot = attempt.public_snapshot()

    assert snapshot["launch_phase"] == "importing_launch"
    assert snapshot["process_state"] == "absent"
    assert "startup_state" not in snapshot

    attempt.process_state = RuntimeProcessState.RUNNING
    attempt.startup_state = "unverified"
    running = attempt.public_snapshot()
    assert running["process_state"] == "running"
    assert running["startup_state"] == "unverified"


def test_launch_attempt_accepts_normal_and_cancel_paths_but_rejects_skips() -> None:
    attempt = LaunchAttempt(attempt_id="launch_1", generation=1)
    for phase in (
        LaunchPhase.IMPORTING_LAUNCH,
        LaunchPhase.RESOLVING_BUILD,
        LaunchPhase.COMPILING,
        LaunchPhase.RESOLVING_RUNTIME,
        LaunchPhase.STARTING_JVM,
        LaunchPhase.WAITING_READINESS,
        LaunchPhase.RUNTIME_ACTIVE,
        LaunchPhase.STOPPING,
        LaunchPhase.STOPPED,
    ):
        attempt.transition(phase)

    with pytest.raises(LaunchContractError):
        attempt.transition(LaunchPhase.RUNTIME_ACTIVE)

    cancelled = LaunchAttempt(attempt_id="launch_2", generation=2)
    cancelled.transition(LaunchPhase.IMPORTING_LAUNCH)
    cancelled.transition(LaunchPhase.RESOLVING_BUILD)
    cancelled.request_cancel()
    assert cancelled.phase is LaunchPhase.CANCELLING
    assert cancelled.cancel_requested is True
    cancelled.transition(LaunchPhase.CANCELLED)


def test_background_failure_snapshot_is_an_observation_not_a_tool_error() -> None:
    attempt = LaunchAttempt(
        attempt_id="launch_1",
        generation=1,
        phase=LaunchPhase.FAILED,
        error_code=LaunchErrorCode.BUILD_FAILED.value,
        error_message="Maven exited with a non-zero status.",
        retryable=True,
        suggested_next_step="Inspect the bounded build log and retry run.",
    )

    snapshot = attempt.public_snapshot()

    assert snapshot["launch_error"] == {
        "error_code": "BUILD_FAILED",
        "message": "Maven exited with a non-zero status.",
        "retryable": True,
        "suggested_next_step": (
            "Inspect the bounded build log and retry run."
        ),
    }
    assert "error_code" not in snapshot
    assert "error" not in snapshot
    assert "code" not in snapshot


def test_synchronous_launch_rejection_uses_runtime_error_envelope() -> None:
    payload = launch_rejection(
        error_code=LaunchErrorCode.LAUNCH_ALREADY_IN_PROGRESS,
        error="A project launch is already in progress.",
        retryable=True,
        suggested_next_step="Call status to observe the current attempt.",
        context={
            "attempt_id": "launch_1",
            "launch_phase": "compiling",
            "code": "must-not-leak",
        },
    )

    assert payload == {
        "ok": False,
        "error": "A project launch is already in progress.",
        "error_code": "LAUNCH_ALREADY_IN_PROGRESS",
        "retryable": True,
        "suggested_next_step": (
            "Call status to observe the current attempt."
        ),
        "attempt_id": "launch_1",
        "launch_phase": "compiling",
    }


def test_launch_models_do_not_repr_or_summarize_secret_values() -> None:
    secret = "company-secret-token"
    intent = LaunchIntent(
        source="idea",
        launch_name="Application",
        launch_type="java_application",
        main_class="com.example.Application",
        working_directory=Path("/workspace"),
        jvm_args=(f"-Dtoken={secret}",),
        program_args=(secret,),
        environment={"ACCESS_TOKEN": secret},
    )
    operation = BuildOperationSpec(
        argv=("mvn", f"-Dtoken={secret}", "compile"),
        cwd=Path("/workspace"),
        environment={"ACCESS_TOKEN": secret},
        operation_name="compile",
    )
    build_plan = BuildPlan(
        build_system="maven",
        build_root=Path("/workspace"),
        target_module="app",
        build_java_executable=Path("/jdk/bin/java"),
        provider_options={"password": secret},
    )
    launch = JvmLaunchPlan(
        java_executable=Path("/jdk/bin/java"),
        classpath=(Path("/workspace/target/classes"),),
        main_class="com.example.Application",
        working_directory=Path("/workspace"),
        jvm_args=(f"-Dtoken={secret}",),
        program_args=(secret,),
        environment_overrides={"ACCESS_TOKEN": secret},
    )

    combined = "\n".join(
        [
            repr(intent),
            repr(operation),
            repr(build_plan),
            repr(launch),
            str(intent.redacted_summary()),
            str(operation.diagnostic_summary()),
            str(launch.redacted_summary()),
        ]
    )

    assert secret not in combined
    assert intent.redacted_summary()["environment_names"] == ["ACCESS_TOKEN"]
    assert operation.diagnostic_summary()["argument_count"] == 2
    assert launch.redacted_summary()["classpath_entry_count"] == 1
