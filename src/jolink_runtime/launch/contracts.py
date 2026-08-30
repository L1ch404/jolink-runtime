"""Executable P0 contract for asynchronous Java project launch.

This module deliberately contains no IDE, Maven, subprocess, or JDWP logic.
It freezes the small vocabulary shared by those later implementation layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LaunchPhase(StrEnum):
    """Lifecycle of one project-launch attempt."""

    IDLE = "idle"
    IMPORTING_LAUNCH = "importing_launch"
    RESOLVING_BUILD = "resolving_build"
    COMPILING = "compiling"
    RESOLVING_RUNTIME = "resolving_runtime"
    STARTING_JVM = "starting_jvm"
    WAITING_READINESS = "waiting_readiness"
    RUNTIME_ACTIVE = "runtime_active"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


class RuntimeProcessState(StrEnum):
    """Existing public JVM process-state vocabulary."""

    ABSENT = "absent"
    RUNNING = "running"
    EXITED = "exited"


class LaunchErrorCode(StrEnum):
    """Stable structured failures for the project-launch control plane."""

    LAUNCH_ALREADY_IN_PROGRESS = "LAUNCH_ALREADY_IN_PROGRESS"
    LAUNCH_CANCELLATION_TIMEOUT = "LAUNCH_CANCELLATION_TIMEOUT"
    LAUNCH_WORKER_FAILED = "LAUNCH_WORKER_FAILED"
    RUNTIME_ALREADY_RUNNING = "RUNTIME_ALREADY_RUNNING"
    NO_RESTARTABLE_LAUNCH = "NO_RESTARTABLE_LAUNCH"
    INVALID_PROJECT_PATH = "INVALID_PROJECT_PATH"
    LAUNCH_CONFIGURATION_NOT_FOUND = "LAUNCH_CONFIGURATION_NOT_FOUND"
    AMBIGUOUS_LAUNCH_CONFIGURATION = "AMBIGUOUS_LAUNCH_CONFIGURATION"
    UNSUPPORTED_LAUNCH_CONFIGURATION = "UNSUPPORTED_LAUNCH_CONFIGURATION"
    IDEA_CONFIGURATION_READ_FAILED = "IDEA_CONFIGURATION_READ_FAILED"
    UNRESOLVED_IDEA_MACRO = "UNRESOLVED_IDEA_MACRO"
    BUILD_SYSTEM_NOT_FOUND = "BUILD_SYSTEM_NOT_FOUND"
    BUILD_MODULE_NOT_FOUND = "BUILD_MODULE_NOT_FOUND"
    AMBIGUOUS_BUILD_MODULE = "AMBIGUOUS_BUILD_MODULE"
    UNSUPPORTED_BUILD_MODEL = "UNSUPPORTED_BUILD_MODEL"
    JAVA_TOOLCHAIN_NOT_FOUND = "JAVA_TOOLCHAIN_NOT_FOUND"
    MAVEN_NOT_FOUND = "MAVEN_NOT_FOUND"
    BUILD_FAILED = "BUILD_FAILED"
    BUILD_CANCELLED = "BUILD_CANCELLED"
    BUILD_TIMEOUT = "BUILD_TIMEOUT"
    SOURCE_CHANGED_DURING_BUILD = "SOURCE_CHANGED_DURING_BUILD"
    JDT_RELOAD_REQUIRES_FRESH_MAVEN_BASELINE = (
        "JDT_RELOAD_REQUIRES_FRESH_MAVEN_BASELINE"
    )
    RUNTIME_RESOLUTION_FAILED = "RUNTIME_RESOLUTION_FAILED"
    FAST_COMPILE_MODEL_UNVERIFIED = "FAST_COMPILE_MODEL_UNVERIFIED"
    FAST_COMPILE_JDK_INCOMPATIBLE = "FAST_COMPILE_JDK_INCOMPATIBLE"
    ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED = (
        "ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED"
    )
    JVM_START_FAILED = "JVM_START_FAILED"
    JVM_EXITED = "JVM_EXITED"


IN_PROGRESS_LAUNCH_PHASES = frozenset(
    {
        LaunchPhase.IMPORTING_LAUNCH,
        LaunchPhase.RESOLVING_BUILD,
        LaunchPhase.COMPILING,
        LaunchPhase.RESOLVING_RUNTIME,
        LaunchPhase.STARTING_JVM,
        LaunchPhase.WAITING_READINESS,
        LaunchPhase.CANCELLING,
        LaunchPhase.STOPPING,
    }
)

TERMINAL_LAUNCH_PHASES = frozenset(
    {
        LaunchPhase.CANCELLED,
        LaunchPhase.FAILED,
        LaunchPhase.STOPPED,
    }
)

_NORMAL_TRANSITIONS: dict[LaunchPhase, frozenset[LaunchPhase]] = {
    LaunchPhase.IDLE: frozenset({LaunchPhase.IMPORTING_LAUNCH}),
    LaunchPhase.IMPORTING_LAUNCH: frozenset(
        {LaunchPhase.RESOLVING_BUILD}
    ),
    LaunchPhase.RESOLVING_BUILD: frozenset(
        {LaunchPhase.COMPILING, LaunchPhase.RESOLVING_RUNTIME}
    ),
    LaunchPhase.COMPILING: frozenset({LaunchPhase.RESOLVING_RUNTIME}),
    LaunchPhase.RESOLVING_RUNTIME: frozenset(
        {LaunchPhase.STARTING_JVM}
    ),
    LaunchPhase.STARTING_JVM: frozenset(
        {LaunchPhase.WAITING_READINESS, LaunchPhase.RUNTIME_ACTIVE}
    ),
    LaunchPhase.WAITING_READINESS: frozenset(
        {LaunchPhase.RUNTIME_ACTIVE}
    ),
    LaunchPhase.RUNTIME_ACTIVE: frozenset({LaunchPhase.STOPPING}),
    LaunchPhase.CANCELLING: frozenset({LaunchPhase.CANCELLED}),
    LaunchPhase.STOPPING: frozenset({LaunchPhase.STOPPED}),
    LaunchPhase.CANCELLED: frozenset(),
    LaunchPhase.FAILED: frozenset(),
    LaunchPhase.STOPPED: frozenset(),
}


class LaunchContractError(ValueError):
    """Raised when an implementation attempts an invalid state transition."""


def launch_rejection(
    *,
    error_code: LaunchErrorCode | str,
    error: str,
    retryable: bool,
    suggested_next_step: str,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the existing Runtime Tool-error envelope for a rejected action."""
    payload: dict[str, Any] = {
        "ok": False,
        "error": error,
        "error_code": str(error_code),
        "retryable": bool(retryable),
        "suggested_next_step": suggested_next_step,
    }
    if context:
        payload.update(
            {
                key: value
                for key, value in context.items()
                if key not in payload and key != "code"
            }
        )
    return payload


@dataclass(frozen=True)
class LaunchIntent:
    """IDE-neutral description of what the user normally launches."""

    source: str
    launch_name: str
    launch_type: str
    main_class: str
    working_directory: Path
    ide_module_name: str | None = None
    jvm_args: tuple[str, ...] = field(default_factory=tuple, repr=False)
    program_args: tuple[str, ...] = field(default_factory=tuple, repr=False)
    environment: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
    )
    build_before_run: bool = True
    runtime_jdk_reference: str | None = None

    @property
    def environment_names(self) -> tuple[str, ...]:
        """Return stable names without exposing imported environment values."""
        return tuple(sorted(str(name) for name in self.environment))

    def redacted_summary(self) -> dict[str, Any]:
        """Return the serializable part safe for Tool results and persistence."""
        return {
            "source": self.source,
            "launch_name": self.launch_name,
            "launch_type": self.launch_type,
            "main_class": self.main_class,
            "working_directory": str(self.working_directory),
            "ide_module_name": self.ide_module_name,
            "jvm_arg_count": len(self.jvm_args),
            "program_arg_count": len(self.program_args),
            "environment_names": list(self.environment_names),
            "build_before_run": self.build_before_run,
            "runtime_jdk_reference": self.runtime_jdk_reference,
        }


@dataclass(frozen=True)
class BuildPlan:
    """Build-system-neutral plan selected for one launch intent."""

    build_system: str
    build_root: Path
    target_module: str
    build_java_executable: Path
    compile_required: bool = True
    provider_options: Mapping[str, Any] = field(
        default_factory=dict,
        repr=False,
    )


@dataclass(frozen=True)
class BuildOperationSpec:
    """One cancellable external build-tool operation.

    Arguments and environment values are deliberately excluded from repr so
    exception logging cannot accidentally expose imported credentials.
    """

    argv: tuple[str, ...] = field(repr=False)
    cwd: Path
    environment: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
    )
    timeout_seconds: float | None = None
    output_capture: Path | None = None
    max_output_bytes: int | None = None
    operation_name: str = "build"

    def diagnostic_summary(self) -> dict[str, Any]:
        return {
            "operation_name": self.operation_name,
            "executable": Path(self.argv[0]).name if self.argv else "",
            "argument_count": max(0, len(self.argv) - 1),
            "cwd": str(self.cwd),
            "timeout_seconds": self.timeout_seconds,
            "output_capture": (
                str(self.output_capture)
                if self.output_capture is not None
                else None
            ),
            "max_output_bytes": self.max_output_bytes,
            "environment_names": sorted(str(key) for key in self.environment),
        }


@dataclass(frozen=True)
class JvmLaunchPlan:
    """Fully resolved facts needed to start one JVM."""

    java_executable: Path
    classpath: tuple[Path, ...] = field(repr=False)
    main_class: str
    working_directory: Path
    jvm_args: tuple[str, ...] = field(default_factory=tuple, repr=False)
    program_args: tuple[str, ...] = field(default_factory=tuple, repr=False)
    environment_overrides: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
    )
    ready_port: int = 0
    startup_wait_timeout_seconds: float = 30.0
    command_materialization: str = "direct_classpath"

    def redacted_summary(self) -> dict[str, Any]:
        return {
            "java_executable": str(self.java_executable),
            "classpath_entry_count": len(self.classpath),
            "main_class": self.main_class,
            "working_directory": str(self.working_directory),
            "jvm_arg_count": len(self.jvm_args),
            "program_arg_count": len(self.program_args),
            "environment_names": sorted(
                str(key) for key in self.environment_overrides
            ),
            "ready_port": self.ready_port or None,
            "startup_wait_timeout_seconds": (
                self.startup_wait_timeout_seconds
            ),
            "command_materialization": self.command_materialization,
        }


@dataclass
class LaunchAttempt:
    """Mutable state for one generation, protected by its owner's lock."""

    attempt_id: str
    generation: int
    phase: LaunchPhase = LaunchPhase.IDLE
    process_state: RuntimeProcessState = RuntimeProcessState.ABSENT
    startup_state: str | None = None
    cancel_requested: bool = False
    intent: LaunchIntent | None = None
    build_plan: BuildPlan | None = None
    jvm_launch_plan: JvmLaunchPlan | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool | None = None
    suggested_next_step: str | None = None
    error_context: dict[str, Any] = field(default_factory=dict, repr=False)
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)

    def transition(self, target: LaunchPhase) -> None:
        """Move to a legal phase without encoding worker implementation."""
        target = LaunchPhase(target)
        if target == self.phase:
            return

        allowed = set(_NORMAL_TRANSITIONS[self.phase])
        if (
            self.phase not in TERMINAL_LAUNCH_PHASES
            and self.phase not in {
                LaunchPhase.CANCELLING,
                LaunchPhase.STOPPING,
            }
        ):
            allowed.add(LaunchPhase.FAILED)
            if (
                self.phase is LaunchPhase.RUNTIME_ACTIVE
                or self.process_state is RuntimeProcessState.RUNNING
            ):
                allowed.add(LaunchPhase.STOPPING)
            else:
                allowed.add(LaunchPhase.CANCELLING)

        if target not in allowed:
            raise LaunchContractError(
                f"Invalid launch transition: {self.phase.value} -> "
                f"{target.value}"
            )
        self.phase = target
        self.updated_at = _utc_now()

    def request_cancel(self) -> None:
        """Enter the common cancellation path idempotently."""
        self.cancel_requested = True
        if self.phase in TERMINAL_LAUNCH_PHASES:
            return
        if (
            self.phase is LaunchPhase.RUNTIME_ACTIVE
            or self.process_state is RuntimeProcessState.RUNNING
        ):
            self.transition(LaunchPhase.STOPPING)
        elif self.phase not in {
            LaunchPhase.CANCELLING,
            LaunchPhase.STOPPING,
        }:
            self.transition(LaunchPhase.CANCELLING)

    def public_snapshot(self) -> dict[str, Any]:
        """Return the bounded state shape shared by run and status."""
        snapshot: dict[str, Any] = {
            "attempt_id": self.attempt_id,
            "generation": self.generation,
            "launch_phase": self.phase.value,
            "process_state": self.process_state.value,
            "cancel_requested": self.cancel_requested,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        if (
            self.process_state is not RuntimeProcessState.ABSENT
            and self.startup_state is not None
        ):
            snapshot["startup_state"] = self.startup_state
        if self.error_code is not None:
            snapshot["launch_error"] = {
                "error_code": self.error_code,
                "message": self.error_message or "",
                "retryable": bool(self.retryable),
                "suggested_next_step": self.suggested_next_step or "",
            }
            snapshot["launch_error"].update(
                {
                    key: value
                    for key, value in self.error_context.items()
                    if key
                    not in {
                        "error",
                        "error_code",
                        "message",
                        "retryable",
                        "suggested_next_step",
                        "code",
                    }
                }
            )
        return snapshot


__all__ = [
    "IN_PROGRESS_LAUNCH_PHASES",
    "TERMINAL_LAUNCH_PHASES",
    "BuildOperationSpec",
    "BuildPlan",
    "JvmLaunchPlan",
    "LaunchAttempt",
    "LaunchContractError",
    "LaunchErrorCode",
    "LaunchIntent",
    "LaunchPhase",
    "RuntimeProcessState",
    "launch_rejection",
]
