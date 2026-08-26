"""
JavaRuntime — Agent-facing Java runtime manager.

Implements the Runtime ABC. Internally composits:
  - JDWPClient  (pure protocol transport)
  - ProcessManager (lifecycle)
  - LogManager    (console output)

LLM never sees JDWP, thread IDs, or protocol details.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import struct
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...core.models import RuntimeAction, RuntimeResult, Variable
from ...core.wait_state import WaitControl
from ...launch import (
    FastCompileError,
    FastCompilePlan,
    FastCompiler,
    JavaProjectSession,
    LaunchCancelled,
    LaunchContext,
    LaunchControlError,
    LaunchController,
    LaunchErrorCode,
    LaunchPhase,
    LaunchPipelineFailure,
    ProjectLaunchPipeline,
    ProjectLaunchRequest,
    ProjectSessionError,
    RuntimeProcessState,
)
from ..base import Runtime
from .classfile import (
    ClassFileChangeKind,
    ClassFileFormatError,
    ParsedClassFile,
    compare_class_files,
    parse_class_file,
)
from .jdwp_client import (
    Cmd,
    EventKind,
    JDWPClient,
    JDWPCommandOutcomeUnknown,
    JDWPCommandRejected,
    JDWPError,
    SuspendPolicy,
    Tag,
)
from .log_manager import LogManager, read_log_tail_snapshot
from .process_manager import (
    ProcessManager,
    ProcessStartCancelledError,
    ProcessStartupError,
    ReadyPortAlreadyInUseError,
    RuntimeAlreadyRunningError,
)

logger = logging.getLogger(__name__)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_BUILD_SECRET_NAME = (
    r"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|credential|cookie|authorization)"
)
_BUILD_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"((?:-D)?[\"']?[A-Za-z0-9_.-]*"
    + _BUILD_SECRET_NAME
    + r"[A-Za-z0-9_.-]*[\"']?)"
    r"(\s*[:=]\s*)"
    r"[^\r\n]*"
)
_BUILD_SECRET_FLAG = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"(--[A-Za-z0-9_.-]*"
    + _BUILD_SECRET_NAME
    + r"[A-Za-z0-9_.-]*)"
    r"(\s+)"
    r"[^\r\n]*"
)
_BUILD_SECRET_HEADER = re.compile(
    r"(?i)^((?:.*?)\b(?:authorization|cookie)\s*:\s*).*$"
)
_URL_USERINFO = re.compile(r"(://)([^/@\s]+)@")
_MAX_UPDATE_PACKAGE_CLASS_FILES = 4096
_MAX_UPDATE_CLASSES = 256
_MAX_UPDATE_CLASS_BYTES = 32 * 1024 * 1024
_BREAKPOINT_STALE_AFTER_REDEFINE = (
    "CLASS_REDEFINED_BREAKPOINT_REQUIRES_RESET"
)

_TWO_PHASE_WAIT_NEXT_STEP = (
    "Call wait_event with wait_mode='arm'. After it returns status='armed', "
    "trigger the target scenario, then call wait_event with wait_mode='await' "
    "and the returned wait_handle."
)


def _error_summary(error: Exception) -> str:
    """Return one safe diagnostic line, excluding captured application logs."""
    message = str(error)
    return message.splitlines()[0][:240] if message else "-"


@dataclass
class SuspensionSnapshot:
    """A VM suspension generation whose frame/object ids are still valid."""

    suspension_id: str
    generation: int
    request_id: int
    thread_id: int
    location: dict[str, int]
    observed_at: str
    jdwp_request_id: int = 0
    breakpoint_id: str = ""
    created_at: str = ""
    event_kind: str = "breakpoint"
    event_type: str = "breakpoint"
    suspend_policy: int = SuspendPolicy.EVENT_THREAD
    event: dict[str, Any] | None = None
    waiter_id: str = ""
    wait_generation: int = 0
    suspended_thread_ids: tuple[int, ...] = ()
    resumed: bool = False
    valid: bool = True


@dataclass
class BreakpointClassCandidate:
    type_tag: int
    class_id: int
    signature: str
    name: str
    simple_name: str
    source_file: str | None
    is_proxy: bool
    proxy_type: str | None
    is_generated: bool
    match_type: str
    match_rank: int
    warnings: list[str]


@dataclass
class BreakpointLocation:
    method_id: int
    method: str
    method_signature: str
    line: int
    code_index: int


@dataclass(frozen=True)
class StagedClassUpdate:
    binary_name: str
    signature: str
    baseline: ParsedClassFile
    staged: ParsedClassFile
    class_bytes: bytes
    source_key: str


@dataclass(frozen=True)
class ProjectUpdatePlan:
    """Minimal retained state; excludes launch arguments and environment values."""

    fast_compile_plan: FastCompilePlan | None
    fast_compile_unavailable_reason: str | None
    attempt_directory: Path
    project_session: JavaProjectSession | None = None
    jvm_plan: Any | None = None
    generation_classpath_index: int | None = None
    request: ProjectLaunchRequest | None = None


class JavaRuntime(Runtime):
    """Agent-facing Java runtime. One instance manages one application."""

    _BROAD_EXCEPTION_SIGNATURES = {
        "Ljava/lang/Throwable;",
        "Ljava/lang/Exception;",
        "Ljava/lang/RuntimeException;",
        "Ljava/lang/Error;",
    }
    _JAVA_LANG_SIMPLE_EXCEPTIONS = {
        "ArithmeticException",
        "ArrayIndexOutOfBoundsException",
        "ClassCastException",
        "Error",
        "Exception",
        "IllegalArgumentException",
        "IllegalStateException",
        "IndexOutOfBoundsException",
        "NullPointerException",
        "NegativeArraySizeException",
        "NumberFormatException",
        "RuntimeException",
        "SecurityException",
        "StringIndexOutOfBoundsException",
        "Throwable",
        "UnsupportedOperationException",
    }

    def __init__(self, host: str = "localhost"):
        self._host = host
        self._proc = ProcessManager(host)
        self._log = LogManager()
        # These dictionaries contain stable Runtime definitions, not live
        # JDWP EventRequest ids.  A suspending request exists only while one
        # wait_event call owns the session; see _arm_debug_requests().
        self._breakpoints: dict[str, dict[str, Any]] = {}
        self._exceptions: dict[int, dict[str, Any]] = {}
        self._armed_breakpoint_requests: dict[int, str] = {}
        self._armed_exception_requests: dict[int, int] = {}
        self._exception_counter = 0
        self._jdwp: JDWPClient | None = None  # persistent debugger connection
        self._active_suspension: SuspensionSnapshot | None = None
        self._suspension_generation = 0
        self._breakpoint_counter = 0
        self._max_array_elements = 64
        self._max_value_depth = 5
        self._closed = False
        self._close_settled = False
        self._force_closed = False
        self._closing_requested = False
        self._debug_connection_dirty = False
        self._debug_connection_warning = ""
        self._target_release_lock = threading.Lock()
        self._released_target_tokens: set[tuple[int, int, int, bool]] = set()
        self._project_pipeline = ProjectLaunchPipeline()
        self._launch_controller = LaunchController(
            runtime_releaser=self._release_project_runtime,
        )
        self._project_state_lock = threading.Lock()
        self._project_attempt_directories: dict[str, Path] = {}
        self._project_processes: dict[str, Any] = {}
        self._project_warnings: dict[str, tuple[str, ...]] = {}
        self._project_update_plans: dict[str, ProjectUpdatePlan] = {}
        self._project_sessions: dict[str, JavaProjectSession] = {}
        self._preserve_project_sessions: set[str] = set()
        self._last_project_request: ProjectLaunchRequest | None = None
        self._last_direct_action: RuntimeAction | None = None
        self._fast_compiler = FastCompiler()
        self._update_lock = threading.Lock()
        self._code_revision = 0
        self._runtime_overlay_sources: set[str] = set()
        self._runtime_overlay_state = "none"
        self._runtime_overlay_class_hashes: dict[str, str] = {}
        self._runtime_overlay_source_classes: dict[str, set[str]] = {}

    # ── Lifecycle ──────────────────────────────────────

    def interrupt_wait(self) -> None:
        """Force-close JDWP to wake a reader and invalidate remote requests."""
        jdwp = self._jdwp
        if jdwp is None:
            return
        logger.info("java_runtime.shutdown.wait_interrupt.request")
        try:
            jdwp.close()
        except Exception as exc:
            logger.warning(
                "java_runtime.shutdown.wait_interrupt.failed "
                "error_type=%s error=%s",
                type(exc).__name__,
                _error_summary(exc),
            )
        finally:
            if self._jdwp is jdwp:
                self._jdwp = None
            self._invalidate_connection_scoped_requests(
                "The JDWP connection was force-closed while cancelling "
                "wait_event; live requests were invalidated while stable "
                "breakpoint and exception definitions were preserved.",
                force_warning=True,
            )

    def settle_cancelled_wait(self, wait_control: WaitControl) -> bool:
        """Resume only state owned by a cancelled waiter.

        The MCP boundary calls this only after the wait worker has exited and
        before another Runtime action may acquire the session. Breakpoint and
        exception *definitions* remain available, while the worker has already
        disarmed their connection-scoped JDWP requests.
        """
        if not wait_control.cancelled:
            return True

        jdwp = self._jdwp
        snapshot = self._active_suspension
        settled_safely = True
        if (
            jdwp is None
            and snapshot is not None
            and self._snapshot_owned_by_waiter(snapshot, wait_control)
        ):
            # A forced connection close is itself the final JDWP fallback:
            # the VM resumes debugger suspensions and every frame/object id
            # from that connection becomes invalid.
            snapshot.resumed = True
            snapshot.valid = False
            self._active_suspension = None
            wait_control.mark_phase("connection_closed_auto_resumed")
            return True
        try:
            if (
                jdwp is not None
                and snapshot is not None
                and self._snapshot_owned_by_waiter(snapshot, wait_control)
            ):
                self._resume_cancelled_snapshot(
                    jdwp,
                    snapshot,
                    wait_control,
                    wait_label="debug_event",
                )

            if jdwp is not None:
                for composite in jdwp.drain_events():
                    self._settle_cancelled_composite(
                        jdwp,
                        composite,
                        wait_control,
                        wait_label="debug_event",
                    )
        except (JDWPError, OSError) as exc:
            wait_control.mark_dirty()
            logger.warning(
                "java_runtime.wait.cancel.settle_failed waiter_id=%s "
                "generation=%s error_type=%s error=%s",
                wait_control.waiter_id,
                wait_control.wait_generation,
                type(exc).__name__,
                _error_summary(exc),
            )
            # No newer action can run while cancellation is settling, so this
            # last-resort resume cannot race a later suspension generation.
            settled_safely = self._emergency_resume_for_close()
            if not settled_safely:
                wait_control.mark_dirty()

        snapshot = self._active_suspension
        if (
            snapshot is not None
            and self._snapshot_owned_by_waiter(snapshot, wait_control)
            and (snapshot.resumed or not snapshot.valid)
        ):
            self._active_suspension = None
        return settled_safely

    def _emergency_resume_for_close(self) -> bool:
        """Best-effort fallback when normal debug cleanup could not finish."""
        jdwp = self._jdwp
        if jdwp is None:
            return False

        resumed = False
        snapshot = self._active_suspension
        if snapshot is not None and snapshot.valid and not snapshot.resumed:
            try:
                err, scope = self._resume_snapshot(jdwp, snapshot)
                if err:
                    logger.warning(
                        "java_runtime.shutdown.snapshot_resume_failed "
                        "scope=%s error_code=%s",
                        scope,
                        err,
                    )
                else:
                    snapshot.resumed = True
                    snapshot.valid = False
                    resumed = True
            except Exception as exc:
                logger.warning(
                    "java_runtime.shutdown.snapshot_resume_crashed "
                    "error_type=%s error=%s",
                    type(exc).__name__,
                    _error_summary(exc),
                )

        try:
            err, _ = jdwp.command(Cmd.VM, 9)
            if err:
                logger.warning(
                    "java_runtime.shutdown.vm_resume_failed error_code=%s",
                    err,
                )
            else:
                resumed = True
        except Exception as exc:
            logger.warning(
                "java_runtime.shutdown.vm_resume_crashed "
                "error_type=%s error=%s",
                type(exc).__name__,
                _error_summary(exc),
            )
        if resumed and snapshot is not None:
            snapshot.resumed = True
            snapshot.valid = False
            if self._active_suspension is snapshot:
                self._active_suspension = None
        return resumed

    def _release_target_for_shutdown(
        self,
        current: Any,
        ownership: str,
        *,
        forced: bool,
        deadline: float | None = None,
    ) -> bool:
        if current is None:
            return True
        token = (
            int(getattr(current, "generation", 0)),
            id(current),
            int(current.pid),
            bool(current.owned),
        )
        with self._target_release_lock:
            if token in self._released_target_tokens:
                return True
            try:
                if current.owned:
                    stop_target = getattr(self._proc, "stop_target", None)
                    if callable(stop_target):
                        try:
                            result = stop_target(
                                current,
                                deadline=deadline,
                                force=forced,
                            )
                        except TypeError as error:
                            if "unexpected keyword argument" not in str(error):
                                raise
                            result = stop_target(current)
                    else:
                        result = self._proc.stop()
                else:
                    detach_target = getattr(self._proc, "detach_target", None)
                    result = (
                        detach_target(current)
                        if callable(detach_target)
                        else self._proc.detach()
                    )
                if current.owned:
                    liveness = getattr(current, "is_alive", None)
                    settled = (
                        result.get("status") in {"stopped", "not_running"}
                        and (
                            not bool(liveness())
                            if callable(liveness)
                            else True
                        )
                    )
                else:
                    settled = result.get("status") in {
                        "detached",
                        "already_detached",
                        "not_attached",
                    }
                if settled:
                    self._released_target_tokens.add(token)
                logger.info(
                    "java_runtime.shutdown.target_release_observed "
                    "pid=%s ownership=%s status=%s forced=%s settled=%s",
                    current.pid,
                    ownership,
                    result.get("status", "-"),
                    forced,
                    settled,
                )
                return settled
            except Exception as exc:
                logger.warning(
                    "java_runtime.shutdown.target_release_failed "
                    "pid=%s ownership=%s forced=%s error_type=%s error=%s",
                    current.pid,
                    ownership,
                    forced,
                    type(exc).__name__,
                    _error_summary(exc),
                )
                return False

    def _closing_result(self, operation: str) -> RuntimeResult:
        return RuntimeResult(
            ok=False,
            error="Runtime session is shutting down",
            data={
                "error_code": "SERVER_SHUTTING_DOWN",
                "operation": operation,
                "retryable": True,
                "suggested_next_step": (
                    "Reconnect to a new Runtime MCP server and retry."
                ),
            },
        )

    def _release_late_published_target(
        self,
        current: Any,
        *,
        operation: str,
    ) -> RuntimeResult | None:
        if not self._closing_requested:
            return None
        ownership = "launched" if current.owned else "attached"
        logger.warning(
            "java_runtime.shutdown.late_target_publication "
            "operation=%s pid=%s ownership=%s",
            operation,
            current.pid,
            ownership,
        )
        self._disconnect()
        self._release_target_for_shutdown(
            current,
            ownership,
            forced=True,
        )
        return self._closing_result(operation)

    def force_close(self) -> bool:
        """Bounded ownership-aware release that performs no JDWP commands."""
        if self._force_closed:
            return True
        self._closing_requested = True
        deadline = time.monotonic() + 2.0
        try:
            fast_compile_settled = self._fast_compiler.close(
                deadline=deadline,
                force=True,
            )
        except Exception:
            logger.exception("java_runtime.fast_compile.force_close_failed")
            fast_compile_settled = False
        controller_settled = True
        try:
            controller_result = self._launch_controller.force_close(
                deadline=deadline
            )
            controller_settled = bool(
                controller_result.get("settled", False)
            )
        except Exception:
            logger.exception("java_runtime.project_launch.force_close_failed")
            controller_settled = False
        prevent_new_targets = getattr(self._proc, "prevent_new_targets", None)
        if callable(prevent_new_targets):
            prevent_new_targets()
        try:
            current = self._proc.current
        except Exception:
            current = None
        ownership = (
            "launched" if current is not None and current.owned
            else "attached" if current is not None
            else "absent"
        )
        logger.warning(
            "java_runtime.shutdown.force_start pid=%s ownership=%s",
            current.pid if current is not None else "-",
            ownership,
        )
        self.interrupt_wait()
        self._breakpoints.clear()
        self._exceptions.clear()
        self._armed_breakpoint_requests.clear()
        self._armed_exception_requests.clear()
        self._invalidate_suspension()
        target_settled = self._release_target_for_shutdown(
            current,
            ownership,
            forced=True,
            deadline=deadline,
        )
        settled = (
            fast_compile_settled
            and controller_settled
            and target_settled
        )
        self._force_closed = settled
        if settled:
            self._discard_terminal_project_attempt()
        logger.warning(
            "java_runtime.shutdown.force_finish pid=%s ownership=%s settled=%s",
            current.pid if current is not None else "-",
            ownership,
            settled,
        )
        return settled

    def close(self) -> bool:
        """Best-effort, ownership-aware shutdown for an internal session."""
        if self._close_settled:
            logger.debug("java_runtime.shutdown.skipped reason=already_closed")
            return True
        if self._closed:
            return False
        self._closing_requested = True
        self._closed = True
        deadline = time.monotonic() + 2.0
        try:
            fast_compile_settled = self._fast_compiler.close(
                deadline=deadline,
            )
        except Exception:
            logger.exception("java_runtime.fast_compile.close_failed")
            fast_compile_settled = False
        controller_settled = True
        try:
            controller_result = self._launch_controller.close(
                deadline=deadline
            )
            controller_settled = bool(
                controller_result.get("settled", False)
            )
        except Exception:
            logger.exception("java_runtime.project_launch.close_failed")
            controller_settled = False
        prevent_new_targets = getattr(self._proc, "prevent_new_targets", None)
        if callable(prevent_new_targets):
            prevent_new_targets()

        try:
            current = self._proc.current
        except Exception as exc:
            current = None
            logger.warning(
                "java_runtime.shutdown.target_lookup_failed "
                "error_type=%s error=%s",
                type(exc).__name__,
                _error_summary(exc),
            )
        ownership = (
            "launched" if current is not None and current.owned
            else "attached" if current is not None
            else "absent"
        )
        logger.info(
            "java_runtime.shutdown.start pid=%s ownership=%s suspended=%s",
            current.pid if current is not None else "-",
            ownership,
            self._active_suspension is not None,
        )

        try:
            target_running = self._proc.is_running
        except Exception as exc:
            target_running = False
            logger.warning(
                "java_runtime.shutdown.liveness_check_failed "
                "pid=%s error_type=%s error=%s",
                current.pid if current is not None else "-",
                type(exc).__name__,
                _error_summary(exc),
            )

        emergency_resume_needed = False
        if target_running:
            try:
                cleanup = self.cleanup_debug_state(
                    RuntimeAction(action="cleanup_debug_state")
                )
                if not cleanup.ok or cleanup.error:
                    emergency_resume_needed = True
                    logger.warning(
                        "java_runtime.shutdown.debug_cleanup_failed "
                        "pid=%s error=%s",
                        current.pid if current is not None else "-",
                        cleanup.error or "cleanup returned ok=false",
                    )
                elif cleanup.data.get("warnings") or cleanup.data.get("clear_failures"):
                    warnings = cleanup.data.get("warnings", [])
                    emergency_resume_needed = any(
                        "resume failed" in str(warning).lower()
                        for warning in warnings
                    )
                    logger.warning(
                        "java_runtime.shutdown.debug_cleanup_partial "
                        "pid=%s warnings=%s clear_failures=%s",
                        current.pid if current is not None else "-",
                        len(cleanup.data.get("warnings", [])),
                        len(cleanup.data.get("clear_failures", [])),
                    )
            except Exception as exc:
                emergency_resume_needed = True
                logger.warning(
                    "java_runtime.shutdown.debug_cleanup_crashed "
                    "pid=%s error_type=%s error=%s",
                    current.pid if current is not None else "-",
                    type(exc).__name__,
                    _error_summary(exc),
                )

        if emergency_resume_needed:
            self._emergency_resume_for_close()

        try:
            self._disconnect()
        except Exception as exc:
            logger.warning(
                "java_runtime.shutdown.disconnect_failed "
                "pid=%s error_type=%s error=%s",
                current.pid if current is not None else "-",
                type(exc).__name__,
                _error_summary(exc),
            )

        # Local request/suspension ids are invalid after the debugger closes,
        # even when remote request cleanup failed.
        self._breakpoints.clear()
        self._exceptions.clear()
        self._armed_breakpoint_requests.clear()
        self._armed_exception_requests.clear()
        self._invalidate_suspension()
        self._host = "127.0.0.1"

        target_settled = self._release_target_for_shutdown(
            current,
            ownership,
            forced=False,
            deadline=deadline,
        )

        settled = (
            fast_compile_settled
            and controller_settled
            and target_settled
        )
        self._close_settled = settled
        if settled:
            self._discard_terminal_project_attempt()
        logger.info(
            "java_runtime.shutdown.finish pid=%s ownership=%s settled=%s",
            current.pid if current is not None else "-",
            ownership,
            settled,
        )
        return settled

    def run_project(
        self,
        action: RuntimeAction,
        request: ProjectLaunchRequest,
    ) -> RuntimeResult:
        """Start one IDEA/Maven launch attempt without blocking the Tool call."""
        invalid = self._validate_project_request(action, request)
        if invalid is not None:
            return invalid
        self._reconcile_project_process_exit()
        current = self._proc.current
        if current is not None and current.is_alive():
            return RuntimeResult(
                ok=False,
                error="A managed Runtime is already running.",
                data={
                    "error_code": "RUNTIME_ALREADY_RUNNING",
                    "process_state": "running",
                    "pid": current.pid,
                    "retryable": True,
                    "suggested_next_step": (
                        "Call status to inspect the current Runtime, or call "
                        "restart to replace it explicitly."
                    ),
                },
            )
        self._discard_terminal_project_attempt()
        worker = lambda context: self._project_launch_worker(
            context,
            request,
        )
        try:
            snapshot = self._launch_controller.start(worker)
        except LaunchControlError as error:
            return self._launch_control_result(error)
        self._last_project_request = request
        self._last_direct_action = None
        return RuntimeResult(
            ok=True,
            data={
                "status": "project_launch_started",
                **snapshot,
                "suggested_next_step": (
                    "Call status to observe launch_phase and application "
                    "readiness. Do not set breakpoints or trigger requests "
                    "until process_state is running."
                ),
            },
        )

    def restart_project(
        self,
        action: RuntimeAction,
        request: ProjectLaunchRequest,
    ) -> RuntimeResult:
        """Explicitly replace a direct target or prior project attempt."""
        invalid = self._validate_project_request(action, request)
        if invalid is not None:
            return invalid
        current = self._proc.current
        project_snapshot = self._launch_controller.snapshot()
        if (
            current is not None
            and current.is_alive()
            and project_snapshot.get("launch_phase")
            in {
                LaunchPhase.IDLE.value,
                LaunchPhase.FAILED.value,
                LaunchPhase.CANCELLED.value,
                LaunchPhase.STOPPED.value,
            }
        ):
            self._disconnect()
            stop_result = self._proc.stop_target(current)
            if current.is_alive():
                return RuntimeResult(
                    ok=False,
                    error="The current Runtime process could not be stopped.",
                    data={
                        "error_code": "PROCESS_STOP_FAILED",
                        **stop_result,
                        "retryable": True,
                        "suggested_next_step": (
                            "Retry restart. If the process remains alive, "
                            "stop it explicitly before launching a replacement."
                        ),
                    },
                )
        worker = lambda context: self._project_launch_worker(
            context,
            request,
        )
        try:
            snapshot = self._launch_controller.restart(
                worker,
                deadline=time.monotonic() + 5.0,
            )
        except LaunchControlError as error:
            return self._launch_control_result(error)
        self._last_project_request = request
        self._last_direct_action = None
        return RuntimeResult(
            ok=True,
            data={
                "status": "project_launch_restarted",
                **snapshot,
                "suggested_next_step": (
                    "Call status to observe the new launch generation."
                ),
            },
        )

    def restart_current_project(
        self,
        action: RuntimeAction,
    ) -> RuntimeResult:
        """Restart the current sealed Generation without compiling sources."""

        snapshot = self._launch_controller.snapshot()
        attempt_id = snapshot.get("attempt_id")
        if not isinstance(attempt_id, str):
            return RuntimeResult(
                ok=False,
                error="No active project generation can be restarted.",
                data={
                    "error_code": "NO_RESTARTABLE_LAUNCH",
                    "retryable": True,
                    "suggested_next_step": (
                        "Launch a Maven project before calling restart."
                    ),
                },
            )
        with self._project_state_lock:
            prepared = self._project_update_plans.get(attempt_id)
            session = self._project_sessions.get(attempt_id)
        if (
            prepared is None
            or prepared.jvm_plan is None
            or prepared.generation_classpath_index is None
            or prepared.request is None
            or session is None
            or session.generations.current is None
        ):
            return RuntimeResult(
                ok=False,
                error="The current project generation is unavailable.",
                data={
                    "error_code": "CURRENT_GENERATION_UNAVAILABLE",
                    "retryable": True,
                    "suggested_next_step": (
                        "Launch the project again to establish a stable generation."
                    ),
                },
            )
        request = prepared.request
        if action.ready_port > 0:
            request = replace(
                request,
                ready_port=action.ready_port,
                startup_wait_timeout_seconds=(
                    action.startup_wait_timeout_seconds
                ),
            )
        worker = lambda context: self._generation_restart_worker(
            context,
            request=request,
            retained=prepared,
            session=session,
        )
        with self._project_state_lock:
            self._preserve_project_sessions.add(attempt_id)
        try:
            restarted = self._launch_controller.restart(
                worker,
                deadline=time.monotonic() + 5.0,
            )
        except LaunchControlError as error:
            with self._project_state_lock:
                self._preserve_project_sessions.discard(attempt_id)
            return self._launch_control_result(error)
        return RuntimeResult(
            ok=True,
            data={
                "status": "restarting",
                "applied": None,
                **restarted,
                "suggested_next_step": (
                    "Call status until the current stable generation is running."
                ),
            },
        )

    def _generation_restart_worker(
        self,
        context: LaunchContext,
        *,
        request: ProjectLaunchRequest,
        retained: ProjectUpdatePlan,
        session: JavaProjectSession,
    ) -> None:
        attempt_directory = self._project_pipeline.create_attempt_directory(
            context.attempt_id
        )
        process = None
        runtime_active = False
        with self._project_state_lock:
            self._project_attempt_directories[context.attempt_id] = (
                attempt_directory
            )
            self._project_sessions[context.attempt_id] = session
        try:
            context.transition(LaunchPhase.RESOLVING_BUILD)
            context.transition(LaunchPhase.RESOLVING_RUNTIME)
            current = session.generations.current
            if current is None:
                raise ProjectSessionError(
                    "CURRENT_GENERATION_UNAVAILABLE",
                    "The current generation disappeared before restart.",
                )
            classpath = list(retained.jvm_plan.classpath)
            classpath[retained.generation_classpath_index] = (
                current.output_directory
            )
            plan = replace(retained.jvm_plan, classpath=tuple(classpath))
            plan, command = self._project_pipeline.materialize_command(
                plan,
                jdwp_port=request.jdwp_port,
                attempt_directory=attempt_directory,
            )
            context.set_jvm_launch_plan(plan)
            with self._project_state_lock:
                self._project_update_plans[context.attempt_id] = replace(
                    retained,
                    attempt_directory=attempt_directory,
                    project_session=session,
                    jvm_plan=plan,
                    request=request,
                )
            context.transition(LaunchPhase.STARTING_JVM)
            context.check_cancelled()
            self._reset_debug_state()
            self._host = "127.0.0.1"
            log_file = self._log.create(context.attempt_id)
            startup_started = time.monotonic()
            process = self._proc.start(
                classpath=os.pathsep.join(str(path) for path in plan.classpath),
                main_class=plan.main_class,
                app_args=list(plan.program_args),
                jdwp_port=request.jdwp_port,
                vm_args=list(plan.jvm_args),
                log_file=log_file,
                ready_port=request.ready_port,
                startup_wait_timeout_seconds=(
                    request.startup_wait_timeout_seconds
                ),
                readiness_config_source=(
                    "explicit" if request.ready_port else "not_configured"
                ),
                java_executable=str(plan.java_executable),
                working_directory=plan.working_directory,
                environment_overrides=plan.environment_overrides,
                should_stop=lambda: context.cancel_event.is_set(),
                on_published=lambda item: self._publish_project_process(
                    context, item
                ),
                command_argv=command.argv,
                retained_files=command.retained_files,
            )
            context.check_cancelled()
            readiness = self._proc.observe_readiness(process)
            context.set_process_observation(
                process_state=RuntimeProcessState.RUNNING,
                startup_state=str(readiness["startup_state"]),
            )
            if request.ready_port <= 0:
                session.generations.mark_runtime_current()
                session.record_successful_startup(
                    (time.monotonic() - startup_started) * 1000
                )
                context.transition(LaunchPhase.RUNTIME_ACTIVE)
                runtime_active = True
                return
            context.transition(LaunchPhase.WAITING_READINESS)
            deadline = time.monotonic() + request.startup_wait_timeout_seconds
            timeout_marked = False
            while True:
                context.check_cancelled()
                readiness = self._proc.observe_readiness(process)
                startup_state = str(readiness["startup_state"])
                process_state = str(readiness.get("process_state", "running"))
                context.set_process_observation(
                    process_state=(
                        RuntimeProcessState.RUNNING
                        if process_state == "running"
                        else RuntimeProcessState.EXITED
                    ),
                    startup_state=startup_state,
                )
                if startup_state == "ready":
                    session.generations.mark_runtime_current()
                    session.record_successful_startup(
                        (time.monotonic() - startup_started) * 1000
                    )
                    context.transition(LaunchPhase.RUNTIME_ACTIVE)
                    runtime_active = True
                    return
                if startup_state == "failed" or process_state == "exited":
                    raise LaunchPipelineFailure(
                        LaunchErrorCode.JVM_START_FAILED,
                        "The current generation failed during restart.",
                        retryable=True,
                        suggested_next_step=(
                            "Inspect logs, then launch the project again."
                        ),
                    )
                if not timeout_marked and time.monotonic() >= deadline:
                    process.mark_startup_wait_timed_out()
                    timeout_marked = True
                context.cancel_event.wait(0.2)
        except ProcessStartCancelledError as error:
            raise LaunchCancelled(str(error)) from error
        except ProcessStartupError as error:
            raise LaunchPipelineFailure(
                LaunchErrorCode.JVM_START_FAILED,
                str(error),
                retryable=True,
                suggested_next_step="Inspect logs before retrying restart.",
                context={
                    "failure_type": error.failure_type,
                    "cleanup_settled": error.cleanup_settled,
                },
            ) from error
        finally:
            if process is None:
                with self._project_state_lock:
                    process = self._project_processes.get(context.attempt_id)
            if not runtime_active:
                session.generations.mark_runtime_absent()
                if process is not None:
                    stop_result = self._proc.stop_target(process)
                    if (
                        not process.is_alive()
                        and stop_result.get("status")
                        in {"stopped", "not_running"}
                    ):
                        with self._project_state_lock:
                            self._project_processes.pop(
                                context.attempt_id, None
                            )

    def _validate_project_request(
        self,
        action: RuntimeAction,
        request: ProjectLaunchRequest,
    ) -> RuntimeResult | None:
        if self._closing_requested:
            return self._closing_result(action.action)
        if request.jdwp_port == request.ready_port and request.ready_port:
            return RuntimeResult(
                ok=False,
                error="ready_port must differ from jdwp_port",
                data={
                    "error_code": "READY_PORT_CONFLICTS_WITH_JDWP",
                    "argument": "ready_port",
                    "retryable": True,
                    "suggested_next_step": (
                        "Use the application service port for ready_port."
                    ),
                },
            )
        if not 0 <= request.startup_wait_timeout_seconds <= 60:
            return RuntimeResult(
                ok=False,
                error=(
                    "startup_wait_timeout_seconds must be between 0 and 60"
                ),
                data={
                    "error_code": "INVALID_ARGUMENT",
                    "argument": "startup_wait_timeout_seconds",
                    "retryable": True,
                    "suggested_next_step": (
                        "Use a readiness observation window from 0 to 60 "
                        "seconds; project launch continues in the background."
                    ),
                },
            )
        return None

    @staticmethod
    def _launch_control_result(
        error: LaunchControlError,
    ) -> RuntimeResult:
        payload = dict(error.payload)
        message = str(payload.pop("error", str(error)))
        payload.pop("ok", None)
        return RuntimeResult(ok=False, error=message, data=payload)

    @staticmethod
    def _project_build_world_fingerprint(prepared: Any) -> str:
        plan = prepared.fast_compile_plan
        if plan is not None and plan.configuration_fingerprint:
            return str(plan.configuration_fingerprint)
        digest = hashlib.sha256()
        for path in (
            prepared.execution.effective_pom_file,
            prepared.execution.classpath_file,
            prepared.execution.compile_classpath_file,
        ):
            digest.update(str(path.name).encode("utf-8"))
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"<unavailable>")
        digest.update(
            str(prepared.runtime_jdk.java_executable).encode(
                "utf-8", errors="surrogateescape"
            )
        )
        return digest.hexdigest()

    def _prepare_initial_project_generation(
        self,
        context: LaunchContext,
        request: ProjectLaunchRequest,
        prepared: Any,
    ) -> tuple[Any, JavaProjectSession, int]:
        session_root = Path(
            tempfile.mkdtemp(prefix=f"jolink-project-{context.attempt_id}-")
        )
        session = JavaProjectSession(
            root=session_root,
            build_world_fingerprint=(
                self._project_build_world_fingerprint(prepared)
            ),
        )
        try:
            module_output = (
                prepared.execution.module.output_directory.expanduser().resolve(
                    strict=True
                )
            )
            candidate = session.generations.prepare_initial_candidate(
                module_output,
                build_world_fingerprint=session.build_world_fingerprint,
            )
            rewritten: list[Path] = []
            replacement_count = 0
            replacement_index = -1
            for entry in prepared.jvm_plan.classpath:
                normalized = entry.expanduser().resolve(strict=False)
                if normalized == module_output:
                    rewritten.append(candidate.output_directory)
                    replacement_index = len(rewritten) - 1
                    replacement_count += 1
                else:
                    rewritten.append(entry)
            if replacement_count != 1:
                raise ProjectSessionError(
                    "GENERATION_CLASSPATH_UNREPRESENTABLE",
                    "The module output could not be replaced exactly once.",
                )
            generation_plan = replace(
                prepared.jvm_plan,
                classpath=tuple(rewritten),
            )
            generation_plan, command = (
                self._project_pipeline.materialize_command(
                    generation_plan,
                    jdwp_port=request.jdwp_port,
                    attempt_directory=prepared.attempt_directory,
                )
            )
            context.set_jvm_launch_plan(generation_plan)
            return (
                replace(
                    prepared,
                    jvm_plan=generation_plan,
                    command=command,
                ),
                session,
                replacement_index,
            )
        except Exception:
            session.close()
            raise

    def _promote_initial_project_generation(
        self,
        attempt_id: str,
        *,
        startup_ms: float,
    ) -> None:
        with self._project_state_lock:
            session = self._project_sessions.get(attempt_id)
        if session is None:
            raise ProjectSessionError(
                "PROJECT_SESSION_UNAVAILABLE",
                "The project generation session is unavailable.",
            )
        session.generations.promote_candidate()
        session.record_successful_startup(startup_ms)

    def _project_launch_worker(
        self,
        context: LaunchContext,
        request: ProjectLaunchRequest,
    ) -> None:
        attempt_directory = self._project_pipeline.create_attempt_directory(
            context.attempt_id
        )
        with self._project_state_lock:
            self._project_attempt_directories[
                context.attempt_id
            ] = attempt_directory
        process = None
        runtime_active = False
        project_session: JavaProjectSession | None = None
        try:
            prepared = self._project_pipeline.prepare(
                context,
                request,
                attempt_directory=attempt_directory,
            )
            prepared, project_session, generation_classpath_index = (
                self._prepare_initial_project_generation(
                    context,
                    request,
                    prepared,
                )
            )
            with self._project_state_lock:
                self._project_warnings[context.attempt_id] = (
                    prepared.warnings
                )
                self._project_sessions[context.attempt_id] = project_session
                self._project_update_plans[context.attempt_id] = (
                    ProjectUpdatePlan(
                        fast_compile_plan=prepared.fast_compile_plan,
                        fast_compile_unavailable_reason=(
                            prepared.fast_compile_unavailable_reason
                        ),
                        attempt_directory=prepared.attempt_directory,
                        project_session=project_session,
                        jvm_plan=prepared.jvm_plan,
                        generation_classpath_index=(
                            generation_classpath_index
                        ),
                        request=request,
                    )
                )
            context.transition(LaunchPhase.STARTING_JVM)
            context.check_cancelled()
            self._reset_debug_state()
            self._host = "127.0.0.1"
            log_file = self._log.create(context.attempt_id)
            jvm_plan = prepared.jvm_plan
            startup_started = time.monotonic()
            process = self._proc.start(
                classpath=os.pathsep.join(
                    str(path) for path in jvm_plan.classpath
                ),
                main_class=jvm_plan.main_class,
                app_args=list(jvm_plan.program_args),
                jdwp_port=request.jdwp_port,
                vm_args=list(jvm_plan.jvm_args),
                log_file=log_file,
                ready_port=request.ready_port,
                startup_wait_timeout_seconds=(
                    request.startup_wait_timeout_seconds
                ),
                readiness_config_source=(
                    "explicit"
                    if request.ready_port
                    else "not_configured"
                ),
                java_executable=str(jvm_plan.java_executable),
                working_directory=jvm_plan.working_directory,
                environment_overrides=(
                    jvm_plan.environment_overrides
                ),
                should_stop=lambda: context.cancel_event.is_set(),
                on_published=lambda item: self._publish_project_process(
                    context,
                    item,
                ),
                command_argv=prepared.command.argv,
                retained_files=prepared.command.retained_files,
            )
            context.check_cancelled()
            startup_ms = (time.monotonic() - startup_started) * 1000
            readiness = self._proc.observe_readiness(process)
            context.set_process_observation(
                process_state=RuntimeProcessState.RUNNING,
                startup_state=str(readiness["startup_state"]),
            )
            if request.ready_port <= 0:
                self._promote_initial_project_generation(
                    context.attempt_id,
                    startup_ms=startup_ms,
                )
                context.transition(LaunchPhase.RUNTIME_ACTIVE)
                runtime_active = True
                return

            context.transition(LaunchPhase.WAITING_READINESS)
            observation_deadline = time.monotonic() + (
                request.startup_wait_timeout_seconds
            )
            timeout_marked = False
            while True:
                context.check_cancelled()
                readiness = self._proc.observe_readiness(process)
                startup_state = str(readiness["startup_state"])
                process_state = str(
                    readiness.get("process_state", "running")
                )
                context.set_process_observation(
                    process_state=(
                        RuntimeProcessState.RUNNING
                        if process_state == "running"
                        else RuntimeProcessState.EXITED
                    ),
                    startup_state=startup_state,
                )
                if startup_state == "ready":
                    self._promote_initial_project_generation(
                        context.attempt_id,
                        startup_ms=(
                            (time.monotonic() - startup_started) * 1000
                        ),
                    )
                    context.transition(LaunchPhase.RUNTIME_ACTIVE)
                    runtime_active = True
                    return
                if startup_state == "failed" or process_state == "exited":
                    raise LaunchPipelineFailure(
                        LaunchErrorCode.JVM_START_FAILED,
                        "The application exited before readiness was observed.",
                        retryable=True,
                        suggested_next_step=(
                            "Inspect Runtime logs, correct the startup failure, "
                            "and retry run."
                        ),
                    )
                if (
                    not timeout_marked
                    and time.monotonic() >= observation_deadline
                ):
                    process.mark_startup_wait_timed_out()
                    timeout_marked = True
                context.cancel_event.wait(0.2)
        except ProcessStartCancelledError as error:
            raise LaunchCancelled(str(error)) from error
        except ProcessStartupError as error:
            raise LaunchPipelineFailure(
                LaunchErrorCode.JVM_START_FAILED,
                str(error),
                retryable=True,
                suggested_next_step=(
                    "Inspect Runtime logs, correct the JVM startup failure, "
                    "then retry run. If cleanup is unsettled, call stop first."
                ),
                context={
                    "failure_type": error.failure_type,
                    "cleanup_settled": error.cleanup_settled,
                    **(
                        {"exit_code": error.exit_code}
                        if error.exit_code is not None
                        else {}
                    ),
                },
            ) from error
        except ReadyPortAlreadyInUseError as error:
            raise LaunchPipelineFailure(
                "READY_PORT_ALREADY_IN_USE",
                "The configured application readiness port is already in use.",
                retryable=True,
                suggested_next_step=(
                    "Stop the process already using ready_port or choose the "
                    "correct application port, then retry run."
                ),
                context={"ready_port": request.ready_port},
            ) from error
        except RuntimeAlreadyRunningError as error:
            raise LaunchPipelineFailure(
                LaunchErrorCode.RUNTIME_ALREADY_RUNNING,
                "Another Runtime target appeared during project launch.",
                retryable=True,
                suggested_next_step=(
                    "Call status, then use restart to replace the target "
                    "explicitly."
                ),
            ) from error
        finally:
            if process is None:
                # ProcessManager publishes before waiting for JDWP stability.
                # If that wait fails, assignment above never completes, but
                # the exact ProcessInfo still belongs to this attempt.
                with self._project_state_lock:
                    process = self._project_processes.get(
                        context.attempt_id
                    )
            if not runtime_active and process is not None:
                stop_result = self._proc.stop_target(process)
                process_settled = (
                    not process.is_alive()
                    and stop_result.get("status")
                    in {"stopped", "not_running"}
                )
                if process_settled and not context.cancel_event.is_set():
                    try:
                        context.set_process_observation(
                            process_state=RuntimeProcessState.ABSENT,
                            startup_state=None,
                        )
                    except LaunchCancelled:
                        pass
                if process_settled:
                    with self._project_state_lock:
                        if (
                            self._project_processes.get(context.attempt_id)
                            is process
                        ):
                            self._project_processes.pop(
                                context.attempt_id,
                                None,
                            )
            if not runtime_active and project_session is not None:
                with self._project_state_lock:
                    if (
                        self._project_sessions.get(context.attempt_id)
                        is project_session
                    ):
                        self._project_sessions.pop(context.attempt_id, None)
                project_session.close()

    def _publish_project_process(
        self,
        context: LaunchContext,
        process: Any,
    ) -> None:
        """Bind a spawned JVM to its attempt before cancellation can race it."""
        with self._project_state_lock:
            self._project_processes[context.attempt_id] = process
        readiness = self._proc.observe_readiness(process, refresh=False)
        context.set_process_observation(
            process_state=RuntimeProcessState.RUNNING,
            startup_state=str(readiness["startup_state"]),
        )

    def _release_project_runtime(
        self,
        attempt,
        _deadline: float,
        force: bool,
    ) -> bool:
        with self._project_state_lock:
            process = self._project_processes.get(attempt.attempt_id)
            directory = self._project_attempt_directories.get(
                attempt.attempt_id
            )
            preserve_session = (
                attempt.attempt_id in self._preserve_project_sessions
            )
        if process is None:
            current = self._proc.current
            if current is not None and current.is_alive():
                process = current
            else:
                with self._project_state_lock:
                    self._project_warnings.pop(attempt.attempt_id, None)
                    self._project_update_plans.pop(
                        attempt.attempt_id,
                        None,
                    )
                    session = self._project_sessions.pop(
                        attempt.attempt_id,
                        None,
                    )
                    self._project_attempt_directories.pop(
                        attempt.attempt_id,
                        None,
                    )
                if session is not None and not preserve_session:
                    session.close()
                elif session is not None:
                    session.generations.mark_runtime_absent()
                with self._project_state_lock:
                    self._preserve_project_sessions.discard(
                        attempt.attempt_id
                    )
                self._project_pipeline.cleanup_attempt_directory(directory)
                return True
        try:
            self._disconnect()
            self._breakpoints.clear()
            self._exceptions.clear()
            self._armed_breakpoint_requests.clear()
            self._armed_exception_requests.clear()
            self._invalidate_suspension()
            ownership = "launched" if process.owned else "attached"
            settled = self._release_target_for_shutdown(
                process,
                ownership,
                forced=force,
                deadline=_deadline,
            )
        finally:
            if "settled" in locals() and settled:
                self._reset_runtime_overlay()
                with self._project_state_lock:
                    if (
                        self._project_processes.get(attempt.attempt_id)
                        is process
                    ):
                        self._project_processes.pop(attempt.attempt_id, None)
                    self._project_warnings.pop(attempt.attempt_id, None)
                    self._project_update_plans.pop(
                        attempt.attempt_id,
                        None,
                    )
                    session = self._project_sessions.pop(
                        attempt.attempt_id,
                        None,
                    )
                    self._project_attempt_directories.pop(
                        attempt.attempt_id,
                        None,
                    )
                if session is not None and not preserve_session:
                    session.close()
                elif session is not None:
                    session.generations.mark_runtime_absent()
                with self._project_state_lock:
                    self._preserve_project_sessions.discard(
                        attempt.attempt_id
                    )
                self._project_pipeline.cleanup_attempt_directory(directory)
        return settled

    def _discard_terminal_project_attempt(self) -> None:
        snapshot = self._launch_controller.snapshot()
        attempt_id = snapshot.get("attempt_id")
        if isinstance(attempt_id, str):
            with self._project_state_lock:
                process = self._project_processes.get(attempt_id)
            if process is not None and process.is_alive():
                return
        if not self._launch_controller.discard_terminal():
            return
        if not isinstance(attempt_id, str):
            return
        with self._project_state_lock:
            directory = self._project_attempt_directories.pop(
                attempt_id,
                None,
            )
            self._project_warnings.pop(attempt_id, None)
            self._project_update_plans.pop(attempt_id, None)
            self._project_processes.pop(attempt_id, None)
            session = self._project_sessions.pop(attempt_id, None)
        if session is not None:
            session.close()
        self._project_pipeline.cleanup_attempt_directory(directory)

    def _reconcile_project_process_exit(self) -> dict[str, Any]:
        snapshot = self._launch_controller.snapshot()
        if snapshot.get("launch_phase") != LaunchPhase.RUNTIME_ACTIVE.value:
            return snapshot
        attempt_id = snapshot.get("attempt_id")
        if not isinstance(attempt_id, str):
            return snapshot
        with self._project_state_lock:
            process = self._project_processes.get(attempt_id)
        if process is None or process.is_alive():
            return snapshot
        exit_code = process.exit_code
        release = self._proc.stop_target(process)
        if release.get("status") == "stop_failed":
            return snapshot
        self._reset_debug_state()
        with self._project_state_lock:
            if self._project_processes.get(attempt_id) is process:
                self._project_processes.pop(attempt_id, None)
        self._launch_controller.mark_runtime_exited(
            attempt_id=attempt_id,
            exit_code=exit_code,
        )
        return self._launch_controller.snapshot()

    def _active_project_launch_rejection(
        self,
        *,
        operation: str,
    ) -> RuntimeResult | None:
        snapshot = self._reconcile_project_process_exit()
        phase = str(snapshot.get("launch_phase", LaunchPhase.IDLE.value))
        if phase == LaunchPhase.IDLE.value:
            return None
        if phase in {
            LaunchPhase.CANCELLED.value,
            LaunchPhase.FAILED.value,
            LaunchPhase.STOPPED.value,
        }:
            self._discard_terminal_project_attempt()
            return None
        error_code = (
            "RUNTIME_ALREADY_RUNNING"
            if phase == LaunchPhase.RUNTIME_ACTIVE.value
            else "LAUNCH_ALREADY_IN_PROGRESS"
        )
        return RuntimeResult(
            ok=False,
            error=(
                "A managed Runtime is already running."
                if error_code == "RUNTIME_ALREADY_RUNNING"
                else "A project launch is already in progress."
            ),
            data={
                "error_code": error_code,
                "operation": operation,
                **snapshot,
                "retryable": True,
                "suggested_next_step": (
                    "Call status to inspect the current attempt, or call "
                    "restart to replace it explicitly."
                ),
            },
        )

    def run(self, action: RuntimeAction) -> RuntimeResult:
        if self._closing_requested:
            return self._closing_result(action.action)
        launch_mode = "jar" if action.jar_path else "class"
        logger.info(
            "java_runtime.jvm.run.request launch_mode=%s main_class=%s jar_path=%s "
            "classpath=%s jdwp_port=%s ready_port=%s readiness_wait=%s "
            "app_args_count=%s vm_args_count=%s",
            launch_mode,
            action.main_class or "-",
            action.jar_path or "-",
            action.classpath,
            action.jdwp_port,
            action.ready_port or "-",
            action.startup_wait_timeout_seconds,
            len(action.app_args or []),
            len(action.vm_args or []),
        )
        if action.jar_path and action.main_class:
            error = "Provide either jar_path or main_class, not both"
            logger.warning("java_runtime.jvm.run.invalid error=%s", error)
            return RuntimeResult(ok=False, error=error)
        if not action.jar_path and not action.main_class:
            error = "run requires either jar_path or main_class"
            logger.warning("java_runtime.jvm.run.invalid error=%s", error)
            return RuntimeResult(ok=False, error=error)
        if action.ready_port and action.ready_port == action.jdwp_port:
            error = "ready_port must differ from jdwp_port"
            logger.warning("java_runtime.jvm.run.invalid error=%s", error)
            return RuntimeResult(
                ok=False,
                error=error,
                data={
                    "error_code": "READY_PORT_CONFLICTS_WITH_JDWP",
                    "argument": "ready_port",
                    "retryable": True,
                    "suggested_next_step": (
                        "Use the local application service port for ready_port, "
                        "not the JDWP debugger port."
                    ),
                },
            )
        if not 0 <= action.startup_wait_timeout_seconds <= 60:
            error = (
                "startup_wait_timeout_seconds must be between 0 and 60"
            )
            logger.warning("java_runtime.jvm.run.invalid error=%s", error)
            return RuntimeResult(
                ok=False,
                error=error,
                data={
                    "error_code": "INVALID_ARGUMENT",
                    "argument": "startup_wait_timeout_seconds",
                    "retryable": True,
                    "suggested_next_step": (
                        "Use a readiness wait timeout from 0 to 60 seconds; "
                        "call status for applications that take longer."
                    ),
                },
            )
        current = self._proc.current
        if current is not None and current.is_alive():
            return RuntimeResult(
                ok=False,
                error="A managed Runtime is already running.",
                data={
                    "error_code": "RUNTIME_ALREADY_RUNNING",
                    "process_state": "running",
                    "pid": current.pid,
                    "retryable": True,
                    "suggested_next_step": (
                        "Call status to inspect the current Runtime, or call "
                        "restart to replace it explicitly."
                    ),
                },
            )
        launch_rejection = self._active_project_launch_rejection(
            operation="run"
        )
        if launch_rejection is not None:
            return launch_rejection
        info = None
        try:
            self._reset_debug_state()
            self._host = "127.0.0.1"
            log_label = (
                Path(action.jar_path).stem if action.jar_path
                else action.main_class
            )
            log_file = self._log.create(log_label)
            info = self._proc.start(
                classpath=action.classpath,
                main_class=action.main_class,
                jar_path=action.jar_path,
                app_args=action.app_args,
                jdwp_port=action.jdwp_port,
                vm_args=action.vm_args,
                log_file=log_file,
                ready_port=action.ready_port,
                startup_wait_timeout_seconds=(
                    action.startup_wait_timeout_seconds
                ),
                readiness_config_source=(
                    action.readiness_config_source
                    or ("explicit" if action.ready_port else "not_configured")
                ),
                should_stop=lambda: self._closing_requested,
            )
            closing = self._release_late_published_target(
                info,
                operation="run",
            )
            if closing is not None:
                return closing
            readiness = self._proc.wait_for_readiness(
                info,
                action.startup_wait_timeout_seconds,
                should_stop=lambda: self._closing_requested,
            )
            closing = self._release_late_published_target(
                info,
                operation="run",
            )
            if closing is not None:
                return closing
            startup_state = str(readiness["startup_state"])
            data = {
                "status": "process_started",
                "pid": info.pid,
                "jdwp_port": info.jdwp_port,
                "log_file": log_file,
                "launch_mode": info.launch_mode,
                "process_state": (
                    "exited" if startup_state == "failed" else "running"
                ),
                "startup_wait_timeout_seconds": (
                    info.startup_wait_timeout_seconds
                ),
                **readiness,
            }
            if info.launch_mode == "jar":
                data["jar_path"] = info.jar_path
            else:
                data["main_class"] = info.main_class
            if startup_state == "failed":
                data.update({
                    "error_code": "APPLICATION_EXITED_BEFORE_READY",
                    "exit_code": info.exit_code,
                    "retryable": True,
                    "next_action": "logs",
                    "suggested_next_step": (
                        "The application process exited before TCP readiness "
                        "was observed. Inspect logs, correct the startup "
                        "failure, then run it again."
                    ),
                })
                return RuntimeResult(
                    ok=False,
                    error=(
                        "The application process exited before readiness "
                        "was observed."
                    ),
                    data=data,
                )
            if startup_state == "starting":
                data.update({
                    "next_action": "status",
                    "suggested_next_step": (
                        "The application is still starting. Keep it running "
                        "and call status to check startup_state again before "
                        "using an HTTP trigger."
                    ),
                })
            elif startup_state == "unverified":
                data.update({
                    "warnings": [
                        "Application readiness was not configured; JVM launch "
                        "success does not prove that a business service is ready."
                    ],
                    "suggested_next_step": (
                        "For an HTTP application, provide ready_port on a future "
                        "run/restart or verify service readiness before using "
                        "an HTTP trigger."
                    ),
                })
            result = RuntimeResult(ok=True, data=data)
            logger.info(
                "java_runtime.jvm.run.observed pid=%s launch_mode=%s target=%s "
                "jdwp_port=%s startup_state=%s ready_port=%s log_file=%s",
                info.pid, info.launch_mode, info.jar_path or info.main_class,
                info.jdwp_port, startup_state, info.ready_port or "-", log_file,
            )
            closing = self._release_late_published_target(
                info,
                operation="run",
            )
            if closing is not None:
                return closing
            self._last_direct_action = replace(action)
            self._last_project_request = None
            return result
        except Exception as e:
            logger.error(
                "java_runtime.jvm.run.failed launch_mode=%s target=%s jdwp_port=%s "
                "error_type=%s error=%s",
                launch_mode, action.jar_path or action.main_class or "-", action.jdwp_port,
                type(e).__name__, _error_summary(e),
            )
            if self._closing_requested:
                return self._closing_result(action.action)
            if isinstance(e, ProcessStartupError):
                return RuntimeResult(
                    ok=False,
                    error=str(e),
                    data={
                        "error_code": "JVM_START_FAILED",
                        "failure_type": e.failure_type,
                        "cleanup_settled": e.cleanup_settled,
                        **(
                            {"exit_code": e.exit_code}
                            if e.exit_code is not None
                            else {}
                        ),
                        "retryable": True,
                        "suggested_next_step": (
                            "Inspect Runtime logs, correct the JVM startup "
                            "failure, then retry run. If cleanup_settled is "
                            "false, call stop first."
                        ),
                    },
                )
            if isinstance(e, ReadyPortAlreadyInUseError):
                return RuntimeResult(
                    ok=False,
                    error=str(e),
                    data={
                        "error_code": "READY_PORT_ALREADY_IN_USE",
                        "ready_port": action.ready_port,
                        "retryable": True,
                        "suggested_next_step": (
                            "Inspect or stop the process already using this "
                            "local application port, then retry run."
                        ),
                    },
                )
            if isinstance(e, RuntimeAlreadyRunningError):
                return RuntimeResult(
                    ok=False,
                    error="A managed Runtime is already running.",
                    data={
                        "error_code": "RUNTIME_ALREADY_RUNNING",
                        "retryable": True,
                        "suggested_next_step": (
                            "Call status to inspect the current Runtime, or "
                            "call restart to replace it explicitly."
                        ),
                    },
                )
            return RuntimeResult(ok=False, error=str(e))

    def stop(self, action: RuntimeAction) -> RuntimeResult:
        launch_snapshot = self._launch_controller.snapshot()
        launch_phase = str(
            launch_snapshot.get("launch_phase", LaunchPhase.IDLE.value)
        )
        try:
            settlement = self._launch_controller.cancel_current(
                deadline=time.monotonic() + 5.0,
            )
        except Exception as error:
            logger.exception("java_runtime.project_launch.stop_failed")
            return RuntimeResult(
                ok=False,
                error="The project launch could not be stopped safely.",
                data={
                    "error_code": "LAUNCH_CANCELLATION_FAILED",
                    "retryable": True,
                    "suggested_next_step": (
                        "Retry stop. If it remains unsettled, restart the "
                        "Runtime MCP server."
                    ),
                },
            )
        if not bool(settlement.get("settled", False)):
            return RuntimeResult(
                ok=False,
                error="The project launch did not settle before the deadline.",
                data={
                    "error_code": "LAUNCH_CANCELLATION_TIMEOUT",
                    **settlement,
                    "retryable": True,
                    "suggested_next_step": (
                        "Retry stop or restart the Runtime MCP server."
                    ),
                },
            )
        current = self._proc.current
        logger.info(
            "java_runtime.jvm.stop.request pid=%s ownership=%s suspended=%s",
            current.pid if current is not None else "-",
            (
                "launched" if current is not None and current.owned
                else "attached" if current is not None
                else "absent"
            ),
            self._active_suspension is not None,
        )
        self._disconnect()
        self._breakpoints.clear()
        self._exceptions.clear()
        self._armed_breakpoint_requests.clear()
        self._armed_exception_requests.clear()
        self._invalidate_suspension()
        data = self._proc.stop()
        if data.get("status") == "stop_failed":
            if launch_phase != LaunchPhase.IDLE.value:
                data.update(self._launch_controller.snapshot())
            data.update(
                {
                    "error_code": "PROCESS_STOP_FAILED",
                    "retryable": True,
                    "suggested_next_step": (
                        "Retry stop. joLink retained ownership of the live "
                        "process and will not start a replacement."
                    ),
                }
            )
            return RuntimeResult(
                ok=False,
                error="The managed Runtime process could not be stopped.",
                data=data,
            )
        if launch_phase != LaunchPhase.IDLE.value:
            data.update(self._launch_controller.snapshot())
            data["status"] = "stopped"
        logger.info(
            "java_runtime.jvm.stop.finish status=%s pid=%s",
            data.get("status", "-"), data.get("pid", "-"),
        )
        return RuntimeResult(ok=True, data=data)

    def restart(self, action: RuntimeAction) -> RuntimeResult:
        if self._closing_requested:
            return self._closing_result(action.action)
        if (
            not action.jar_path
            and not action.main_class
            and self._last_project_request is not None
        ):
            return self.restart_current_project(action)
        if (
            not action.jar_path
            and not action.main_class
            and self._last_direct_action is not None
        ):
            replacement = replace(self._last_direct_action)
            replacement.action = "restart"
            if action.ready_port > 0:
                replacement.configure_startup_readiness(
                    ready_port=action.ready_port,
                    wait_timeout_seconds=(
                        action.startup_wait_timeout_seconds
                    ),
                    wait_timeout_provided=(
                        action.startup_wait_timeout_provided
                    ),
                    config_source="explicit",
                )
            action = replacement
        if (
            not action.jar_path
            and not action.main_class
            and self._last_project_request is None
            and self._last_direct_action is None
        ):
            return RuntimeResult(
                ok=False,
                error="No prior launch can be restarted.",
                data={
                    "error_code": "NO_RESTARTABLE_LAUNCH",
                    "retryable": True,
                    "suggested_next_step": (
                        "Call run with project_path, jar_path, or main_class."
                    ),
                },
            )
        previous = self._proc.current
        if (
            action.ready_port <= 0
            and previous is not None
            and previous.ready_port > 0
        ):
            replacement = replace(action)
            replacement.configure_startup_readiness(
                ready_port=previous.ready_port,
                wait_timeout_seconds=(
                    action.startup_wait_timeout_seconds
                    if action.startup_wait_timeout_provided
                    else previous.startup_wait_timeout_seconds
                ),
                wait_timeout_provided=(
                    action.startup_wait_timeout_provided
                ),
                config_source="previous_run",
            )
            action = replacement
        elif action.ready_port > 0 and not action.readiness_config_source:
            replacement = replace(action)
            replacement.configure_startup_readiness(
                ready_port=action.ready_port,
                wait_timeout_seconds=action.startup_wait_timeout_seconds,
                wait_timeout_provided=(
                    action.startup_wait_timeout_provided
                ),
                config_source="explicit",
            )
            action = replacement
        logger.info(
            "java_runtime.jvm.restart.request launch_mode=%s target=%s "
            "ready_port=%s readiness_source=%s",
            "jar" if action.jar_path else "class",
            action.jar_path or action.main_class or "-",
            action.ready_port or "-",
            action.readiness_config_source or "not_configured",
        )
        stopped = self.stop(action)
        if not stopped.ok or stopped.error:
            return stopped
        time.sleep(1)
        return self.run(action)

    def attach(self, action: RuntimeAction) -> RuntimeResult:
        if self._closing_requested:
            return self._closing_result(action.action)
        logger.info(
            "java_runtime.jvm.attach.request pid=%s endpoint=%s:%s main_class=%s",
            action.pid, action.host, action.jdwp_port, action.main_class or "-",
        )
        current = self._proc.current
        if current is not None and current.is_alive():
            return RuntimeResult(
                ok=False,
                error=(
                    f"Runtime already manages process {current.pid}; "
                    "stop or detach it before attaching another process"
                ),
            )
        launch_rejection = self._active_project_launch_rejection(
            operation="attach"
        )
        if launch_rejection is not None:
            return launch_rejection
        info = None
        try:
            self._reset_debug_state()
            self._host = action.host or "127.0.0.1"
            if self._host not in {"localhost", "127.0.0.1"}:
                return RuntimeResult(
                    ok=False,
                    error="Remote attach is not supported yet; use a local JDWP endpoint",
                )
            info = self._proc.attach(
                pid=action.pid,
                jdwp_port=action.jdwp_port,
                main_class=action.main_class or "attached",
                host=self._host,
            )
            closing = self._release_late_published_target(
                info,
                operation="attach",
            )
            if closing is not None:
                return closing
            self._connect()
            closing = self._release_late_published_target(
                info,
                operation="attach",
            )
            if closing is not None:
                return closing
            logger.info(
                "java_runtime.jvm.attach.ready pid=%s endpoint=%s:%s",
                info.pid, self._host, info.jdwp_port,
            )
            return RuntimeResult(ok=True, data={
                "status": "attached",
                "pid": info.pid,
                "jdwp_host": self._host,
                "jdwp_port": info.jdwp_port,
                "main_class": info.main_class,
            })
        except Exception as e:
            logger.error(
                "java_runtime.jvm.attach.failed pid=%s endpoint=%s:%s "
                "error_type=%s error=%s",
                action.pid, action.host, action.jdwp_port,
                type(e).__name__, _error_summary(e),
            )
            self._disconnect()
            if info is not None:
                detach_target = getattr(self._proc, "detach_target", None)
                if callable(detach_target):
                    detach_target(info)
                else:
                    self._proc.detach()
            return RuntimeResult(ok=False, error=str(e))

    def detach(self, action: RuntimeAction) -> RuntimeResult:
        current = self._proc.current
        logger.info(
            "java_runtime.jvm.detach.request pid=%s suspended=%s",
            current.pid if current is not None else "-",
            self._active_suspension is not None,
        )
        if self._active_suspension is not None and self._jdwp is not None:
            try:
                err, _ = self._jdwp.command(Cmd.VM, 9)
                if err:
                    return RuntimeResult(ok=False, error=f"VM resume before detach failed (err {err})")
            except Exception as e:
                return RuntimeResult(ok=False, error=f"VM resume before detach failed: {e}")
        self._invalidate_suspension()
        self._breakpoints.clear()
        self._exceptions.clear()
        self._armed_breakpoint_requests.clear()
        self._armed_exception_requests.clear()
        self._disconnect()
        current = self._proc.current
        if current is not None and current.owned and current.is_alive():
            data = {
                "status": "debugger_detached",
                "pid": current.pid,
                "process_state": "running",
            }
        else:
            data = self._proc.detach()
        self._host = "127.0.0.1"
        logger.info(
            "java_runtime.jvm.detach.finish status=%s pid=%s",
            data.get("status", "-"), data.get("pid", "-"),
        )
        return RuntimeResult(ok=True, data=data)

    # ── Observation ────────────────────────────────────

    @staticmethod
    def _redact_build_log_line(line: str) -> str:
        clean = _ANSI_ESCAPE.sub("", line)
        clean = _BUILD_SECRET_HEADER.sub(r"\1<redacted>", clean)
        clean = _BUILD_SECRET_ASSIGNMENT.sub(
            r"\1\2<redacted>",
            clean,
        )
        clean = _BUILD_SECRET_FLAG.sub(r"\1\2<redacted>", clean)
        return _URL_USERINFO.sub(r"\1<redacted>@", clean)

    def _project_launch_snapshot(self) -> dict[str, Any] | None:
        snapshot = self._reconcile_project_process_exit()
        if snapshot.get("launch_phase") == LaunchPhase.IDLE.value:
            return None
        snapshot.update(self._runtime_overlay_snapshot())
        attempt_id = snapshot.get("attempt_id")
        if isinstance(attempt_id, str):
            with self._project_state_lock:
                directory = self._project_attempt_directories.get(attempt_id)
                warnings = self._project_warnings.get(attempt_id, ())
                prepared = self._project_update_plans.get(attempt_id)
                project_session = self._project_sessions.get(attempt_id)
            if project_session is not None:
                snapshot.update(project_session.public_status())
            if warnings:
                snapshot["warnings"] = list(warnings)
            if prepared is not None:
                if prepared.fast_compile_plan is not None:
                    snapshot["fast_update"] = (
                        prepared.fast_compile_plan.redacted_summary()
                    )
                else:
                    snapshot["fast_update"] = {
                        "available": False,
                        "reason": (
                            prepared.fast_compile_unavailable_reason
                            or "FAST_COMPILE_UNSUPPORTED"
                        ),
                    }
            build_log = (
                directory / "build.log"
                if directory is not None
                else None
            )
            if build_log is not None and build_log.is_file():
                try:
                    tail = read_log_tail_snapshot(
                        str(build_log),
                        50,
                        max_scan_bytes=512 * 1024,
                        max_return_bytes=32 * 1024,
                    )
                    tail["lines"] = [
                        self._redact_build_log_line(line)
                        for line in tail["lines"]
                    ]
                    tail["returned_bytes"] = sum(
                        len(line.encode("utf-8"))
                        for line in tail["lines"]
                    )
                    snapshot.setdefault("build", {})["log_tail"] = tail
                    if snapshot.get("launch_error") is not None:
                        snapshot["build_log_tail"] = tail
                except OSError as error:
                    logger.warning(
                        "java_runtime.project_launch.log_tail_failed "
                        "attempt_id=%s error_type=%s",
                        attempt_id,
                        type(error).__name__,
                    )
        return snapshot

    def status(self, action: RuntimeAction) -> RuntimeResult:
        project = self._project_launch_snapshot()
        project_phase = (
            str(project.get("launch_phase"))
            if project is not None
            else LaunchPhase.IDLE.value
        )
        if project is not None and project_phase != LaunchPhase.RUNTIME_ACTIVE.value:
            info: dict[str, Any] = {
                **project,
                "debug_state": "detached",
                "running": project.get("process_state") == "running",
            }
            proc = self._proc.current
            if (
                project_phase == LaunchPhase.WAITING_READINESS.value
                and proc is not None
                and proc.is_alive()
            ):
                readiness = self._proc.observe_readiness(proc)
                info.update(readiness)
                info["pid"] = proc.pid
                info["process_state"] = "running"
                info["running"] = True
                info["next_action"] = "status"
                if readiness["startup_state"] == "ready":
                    info["suggested_next_step"] = (
                        "TCP readiness was just observed. Call status again "
                        "until launch_phase is runtime_active before debugging."
                    )
                else:
                    info["suggested_next_step"] = (
                        "The project JVM is running but the application is not "
                        "ready yet. Keep it running and call status again."
                    )
            elif project_phase in {
                LaunchPhase.IMPORTING_LAUNCH.value,
                LaunchPhase.RESOLVING_BUILD.value,
                LaunchPhase.COMPILING.value,
                LaunchPhase.RESOLVING_RUNTIME.value,
                LaunchPhase.STARTING_JVM.value,
            }:
                info["suggested_next_step"] = (
                    "The project launch is still in progress. Call status "
                    "again; do not set breakpoints or trigger requests yet."
                )
            elif project_phase == LaunchPhase.FAILED.value:
                proc = self._proc.current
                if proc is None or not proc.is_alive():
                    info["process_state"] = "absent"
                    info["running"] = False
                    info.pop("startup_state", None)
                info["suggested_next_step"] = (
                    project.get("launch_error", {}).get(
                        "suggested_next_step",
                        "Inspect build.log_tail and retry run.",
                    )
                )
            return RuntimeResult(ok=True, data=info)

        proc = self._proc.current
        if proc is None:
            data: dict[str, Any] = {
                "process_state": "absent",
                "debug_state": "detached",
                "running": False,
                "message": "No application is managed by this runtime",
            }
            if project is not None:
                data.update(project)
            return RuntimeResult(ok=True, data=data)
        if not proc.is_alive():
            self._invalidate_suspension()
            readiness = self._proc.observe_readiness(proc, refresh=False)
            data = {
                "process_state": "exited",
                "debug_state": "detached",
                "running": False,
                "pid": proc.pid,
                "exit_code": proc.exit_code,
                "message": "Managed application has exited",
                **readiness,
            }
            if project is not None:
                data.update(project)
                data["process_state"] = "exited"
                data["running"] = False
            return RuntimeResult(ok=True, data=data)

        readiness = self._proc.observe_readiness(proc)
        info: dict[str, Any] = {
            "process_state": "running",
            "debug_state": (
                "suspended" if self._active_suspension is not None
                else "attached" if self._jdwp is not None
                else "detached"
            ),
            "running": True,
            "pid": proc.pid,
            "jdwp_port": proc.jdwp_port,
            "launch_mode": proc.launch_mode,
            "ownership": "launched" if proc.owned else "attached",
            "log_file": self._log.path,
            "breakpoint_count": len(self._breakpoints),
            "exception_count": len(self._exceptions),
            "suspension_id": (
                self._active_suspension.suspension_id
                if self._active_suspension is not None else None
            ),
            **readiness,
        }
        startup_state = str(readiness["startup_state"])
        if startup_state == "starting":
            info["next_action"] = "status"
            info["suggested_next_step"] = (
                "The application is still starting. Keep it running and call "
                "status again before using an HTTP trigger."
            )
        elif startup_state == "unverified":
            info.setdefault("warnings", []).append(
                "Application readiness is unverified because no ready_port "
                "was configured."
            )
            info["suggested_next_step"] = (
                "Do not infer business-service readiness from process or JDWP "
                "state alone. Configure ready_port on run/restart when possible."
            )
        if self._debug_connection_dirty:
            info["debug_requests_invalidated"] = True
            info.setdefault("warnings", []).append(self._debug_connection_warning)
            info["suggested_next_step"] = (
                "Stable breakpoint and exception definitions will be re-armed "
                "on the current connection. " + _TWO_PHASE_WAIT_NEXT_STEP
            )
        if proc.launch_mode == "jar":
            info["jar_path"] = proc.jar_path
        else:
            info["main_class"] = proc.main_class
        if project is not None:
            info.update(project)
            info["process_state"] = "running"
            info["running"] = True
            info["startup_state"] = readiness["startup_state"]
            if self._runtime_overlay_state == "unknown":
                info["suggested_next_step"] = (
                    "Restart the application before further runtime updates "
                    "or conclusions because the last HotSwap outcome is "
                    "unknown."
                )

        # Try JDWP for extra info. A queued event without an owning waiter is
        # stale by definition and must be resumed, never promoted to a new
        # public suspension.
        try:
            jdwp = self._connect()
            orphan_events_resumed = 0
            if self._active_suspension is None:
                for composite in jdwp.drain_events():
                    self._resume_ignored_suspending_event(
                        jdwp,
                        "status_without_waiter",
                        composite,
                    )
                    orphan_events_resumed += 1

            info["debug_state"] = (
                "suspended" if self._active_suspension is not None else "attached"
            )
            info["suspension_id"] = (
                self._active_suspension.suspension_id
                if self._active_suspension is not None else None
            )
            if orphan_events_resumed:
                info["orphan_events_resumed"] = orphan_events_resumed
                info.setdefault("warnings", []).append(
                    "Stale debug events without an active waiter were automatically resumed."
                )
                info["suggested_next_step"] = _TWO_PHASE_WAIT_NEXT_STEP

            err, data = jdwp.command(Cmd.VM, 1)  # Version
            if err == 0:
                offset = 0
                desc_len = struct.unpack_from(">I", data, offset)[0]; offset += 4
                offset += desc_len
                offset += 8  # jdwpMajor, jdwpMinor
                vm_ver_len = struct.unpack_from(">I", data, offset)[0]; offset += 4
                vm_ver = data[offset:offset+vm_ver_len].decode("utf-8", errors="replace"); offset += vm_ver_len
                vm_name_len = struct.unpack_from(">I", data, offset)[0]; offset += 4
                vm_name = data[offset:offset+vm_name_len].decode("utf-8", errors="replace")
                info["jvm"] = f"{vm_name} {vm_ver}"
            # Keep connection alive (now managed by _connect / _disconnect)
        except Exception:
            info["jvm"] = "unreachable"
            info["debug_state"] = "detached"
            info["suspension_id"] = None

        # _connect() may have discovered and replaced a stale JDWP transport.
        # Refresh connection-scoped counts and warnings after that probe so
        # status never reports request ids that the JVM has already discarded.
        info["breakpoint_count"] = len(self._breakpoints)
        info["exception_count"] = len(self._exceptions)
        if self._debug_connection_dirty:
            info["debug_requests_invalidated"] = True
            if self._debug_connection_warning not in info.get("warnings", []):
                info.setdefault("warnings", []).append(self._debug_connection_warning)
            info["suggested_next_step"] = (
                "Stable breakpoint and exception definitions will be re-armed "
                "on the current connection. " + _TWO_PHASE_WAIT_NEXT_STEP
            )

        return RuntimeResult(ok=True, data=info)

    def startup_observation(self) -> dict[str, Any]:
        """Return application readiness without connecting to JDWP."""
        project = self._reconcile_project_process_exit()
        project_phase = (
            project.get("launch_phase")
            if project is not None
            else None
        )
        if (
            project is not None
            and project_phase
            not in {
                LaunchPhase.IDLE.value,
                LaunchPhase.RUNTIME_ACTIVE.value,
            }
        ):
            observation: dict[str, Any] = {
                "process_state": project.get("process_state", "absent"),
                "launch_phase": project["launch_phase"],
            }
            if "startup_state" in project:
                observation["startup_state"] = project["startup_state"]
            if observation["process_state"] == "running":
                proc = self._proc.current
                if proc is not None and proc.is_alive():
                    observation.update(self._proc.observe_readiness(proc))
            return observation
        proc = self._proc.current
        if proc is None:
            return {
                "process_state": "absent",
                "readiness_configured": False,
            }
        readiness = self._proc.observe_readiness(proc)
        return {
            "pid": proc.pid,
            **readiness,
        }

    def logs(self, action: RuntimeAction) -> RuntimeResult:
        data = self._log.tail(action.tail)
        if "error" in data:
            return RuntimeResult(ok=False, error=data["error"])
        return RuntimeResult(ok=True, data=data)

    @staticmethod
    def _prepare_fast_compile_candidate(
        session: JavaProjectSession,
        staged_classes: Path,
    ) -> None:
        current = session.generations.current
        if current is None:
            raise ProjectSessionError(
                "CURRENT_GENERATION_UNAVAILABLE",
                "No current generation can be extended by reload.",
            )
        scratch = Path(
            tempfile.mkdtemp(
                prefix="jolink-candidate-",
                dir=str(staged_classes.parent),
            )
        )
        candidate_output = scratch / "output"
        try:
            shutil.copytree(current.output_directory, candidate_output)
            for staged in sorted(
                path for path in staged_classes.rglob("*") if path.is_file()
            ):
                relative = staged.relative_to(staged_classes)
                destination = candidate_output / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(staged, destination)
            session.generations.prepare_candidate(
                candidate_output,
                build_world_fingerprint=session.build_world_fingerprint,
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def update(self, action: RuntimeAction) -> RuntimeResult:
        """Compile explicit project sources and HotSwap compatible classes."""
        if not self._update_lock.acquire(blocking=False):
            return RuntimeResult(
                ok=False,
                error="Another runtime update is already in progress.",
                data={
                    "error_code": "UPDATE_ALREADY_IN_PROGRESS",
                    "retryable": True,
                    "suggested_next_step": (
                        "Wait for the current update to finish, then retry."
                    ),
                },
            )
        compile_attempt = None
        candidate_prepared = False
        candidate_promoted = False
        candidate_retained = False
        candidate_session: JavaProjectSession | None = None
        try:
            context = self._project_update_context()
            if isinstance(context, RuntimeResult):
                return context
            attempt_id, generation, prepared = context
            plan = prepared.fast_compile_plan
            assert plan is not None

            try:
                source_files = plan.resolve_sources(
                    getattr(action, "source_files", None) or ()
                )
            except FastCompileError as error:
                return self._fast_compile_error_result(error, plan)

            if not plan.is_fresh():
                return self._fast_compile_plan_stale_result()
            if self._runtime_overlay_state == "unknown":
                return RuntimeResult(
                    ok=False,
                    error=(
                        "The current JVM code state is unknown after an "
                        "unconfirmed HotSwap attempt."
                    ),
                    data={
                        "error_code": "HOT_SWAP_OUTCOME_UNKNOWN",
                        "runtime_code_state": "unknown",
                        **self._runtime_overlay_snapshot(),
                        "restart_required": True,
                        "retryable": False,
                        "suggested_next_step": (
                            "Restart the application before compiling or "
                            "applying another runtime update."
                        ),
                    },
                )

            if self._active_suspension is not None:
                return RuntimeResult(
                    ok=False,
                    error="A debug suspension is active.",
                    data={
                        "error_code": "ACTIVE_SUSPENSION_EXISTS",
                        "suspension_id": (
                            self._active_suspension.suspension_id
                        ),
                        "runtime_code_state": "unchanged",
                        **self._runtime_overlay_snapshot(),
                        "retryable": True,
                        "suggested_next_step": (
                            "Resume or clean up the active suspension before "
                            "updating runtime code."
                        ),
                    },
                )
            if (
                self._armed_breakpoint_requests
                or self._armed_exception_requests
            ):
                return RuntimeResult(
                    ok=False,
                    error="A debug-event wait is still armed.",
                    data={
                        "error_code": "ACTIVE_DEBUG_REQUESTS_REMAIN",
                        "runtime_code_state": "unchanged",
                        **self._runtime_overlay_snapshot(),
                        "retryable": True,
                        "suggested_next_step": (
                            "Await or clean up the active wait_event before "
                            "updating runtime code."
                        ),
                    },
                )

            try:
                baseline_by_source = self._collect_source_classes(
                    plan,
                    source_files,
                    classes_root=plan.output_root,
                    require_launch_baseline=True,
                )
            except FastCompileError as error:
                return self._fast_compile_error_result(error, plan)

            baseline_classes = [
                parsed
                for classes in baseline_by_source.values()
                for parsed, _raw, _relative in classes.values()
            ]
            try:
                releases = {
                    self._java_release_for_class_major(item.major_version)
                    for item in baseline_classes
                }
            except FastCompileError as error:
                return self._fast_compile_error_result(error, plan)
            if releases != {plan.target_level}:
                return RuntimeResult(
                    ok=False,
                    error=(
                        "The formal class output does not match the cached "
                        "Maven compiler target."
                    ),
                    data={
                        "error_code": "FAST_COMPILE_MODEL_UNVERIFIED",
                        "runtime_code_state": "unchanged",
                        **self._runtime_overlay_snapshot(),
                        "expected_target": plan.target_level,
                        "retryable": False,
                        "suggested_next_step": (
                            "Use the formal Maven build and restart so joLink "
                            "can resolve one consistent compiler model."
                        ),
                    },
                )
            include_parameters = any(
                any(
                    name == "MethodParameters"
                    for name, _value in method.metadata
                )
                for item in baseline_classes
                for method in item.methods
            )

            try:
                compile_attempt = self._fast_compiler.compile(
                    plan,
                    source_files,
                    attempt_directory=prepared.attempt_directory,
                    include_parameters=include_parameters,
                )
            except FastCompileError as error:
                return self._fast_compile_error_result(error, plan)

            current = self._project_update_context()
            if (
                isinstance(current, RuntimeResult)
                or current[0] != attempt_id
                or current[1] != generation
                or current[2] is not prepared
            ):
                return RuntimeResult(
                    ok=False,
                    error="The managed JVM changed while sources were compiled.",
                    data={
                        "error_code": "RUNTIME_CHANGED_DURING_UPDATE",
                        "runtime_code_state": "unchanged",
                        **self._runtime_overlay_snapshot(),
                        "retryable": True,
                        "suggested_next_step": (
                            "Call status and start a new update against the "
                            "current project JVM."
                        ),
                    },
                )
            if not plan.is_fresh():
                return self._fast_compile_plan_stale_result()
            if not compile_attempt.sources_unchanged():
                return RuntimeResult(
                    ok=False,
                    error="A requested source changed while it was compiled.",
                    data={
                        "error_code": "SOURCE_CHANGED_DURING_UPDATE",
                        "runtime_code_state": "unchanged",
                        **self._runtime_overlay_snapshot(),
                        "staging_discarded": True,
                        "retryable": True,
                        "suggested_next_step": (
                            "Wait for edits to finish, then retry update with "
                            "the current explicit source files."
                        ),
                    },
                )
            if not self._formal_outputs_match_launch(plan):
                return self._formal_output_changed_result()

            try:
                staged_by_source = self._collect_source_classes(
                    plan,
                    source_files,
                    classes_root=compile_attempt.classes_directory,
                    require_launch_baseline=False,
                )
                updates, all_source_classes = self._prepare_class_updates(
                    baseline_by_source,
                    staged_by_source,
                )
            except (FastCompileError, ClassFileFormatError) as error:
                if isinstance(error, FastCompileError):
                    return self._fast_compile_error_result(error, plan)
                return RuntimeResult(
                    ok=False,
                    error="A compiled class file could not be validated.",
                    data={
                        "error_code": "INVALID_COMPILED_CLASS",
                        "runtime_code_state": "unchanged",
                        **self._runtime_overlay_snapshot(),
                        "retryable": True,
                        "suggested_next_step": (
                            "Use the formal Maven build and restart for this "
                            "change."
                        ),
                    },
                )

            if not updates:
                stale_breakpoint_ids = [
                    breakpoint_id
                    for breakpoint_id, definition in self._breakpoints.items()
                    if definition.get("stale")
                ]
                if stale_breakpoint_ids:
                    warnings = [
                        "Stale logical breakpoints remain; remove and set the "
                        "listed breakpoint ids again before arming another "
                        "breakpoint wait."
                    ]
                    next_step = (
                        "No runtime bytecode change was required. Remove and "
                        "set the listed stale breakpoint ids again against "
                        "the current source before triggering a fresh request."
                    )
                else:
                    warnings = []
                    next_step = (
                        "No runtime bytecode change was required. Continue "
                        "with a fresh request only if other evidence is needed."
                    )
                return RuntimeResult(
                    ok=True,
                    data={
                        "status": "no_changes",
                        "update_strategy": "fast_compile_hotswap",
                        "compiled_sources": sorted(all_source_classes),
                        "redefined_classes": [],
                        "selection_coverage": "caller_provided",
                        "breakpoint_refresh_state": (
                            "partial"
                            if stale_breakpoint_ids
                            else "complete"
                        ),
                        "refreshed_breakpoint_ids": [],
                        "stale_breakpoint_ids": stale_breakpoint_ids,
                        "newly_stale_breakpoint_ids": [],
                        **(
                            {
                                "breakpoint_stale_reason": (
                                    _BREAKPOINT_STALE_AFTER_REDEFINE
                                )
                            }
                            if stale_breakpoint_ids
                            else {}
                        ),
                        "warnings": warnings,
                        **self._runtime_overlay_snapshot(),
                        "suggested_next_step": next_step,
                    },
                )

            candidate_session = prepared.project_session
            if candidate_session is None:
                return RuntimeResult(
                    ok=False,
                    error="The project Generation session is unavailable.",
                    data={
                        "error_code": "PROJECT_SESSION_UNAVAILABLE",
                        "runtime_code_state": "unchanged",
                        "retryable": True,
                        "suggested_next_step": (
                            "Launch the project again before retrying reload."
                        ),
                    },
                )
            try:
                self._prepare_fast_compile_candidate(
                    candidate_session,
                    compile_attempt.classes_directory,
                )
                candidate_prepared = True
            except (OSError, ProjectSessionError) as error:
                return RuntimeResult(
                    ok=False,
                    error="A durable candidate Generation could not be prepared.",
                    data={
                        "error_code": getattr(
                            error,
                            "error_code",
                            "CANDIDATE_PREPARE_FAILED",
                        ),
                        "runtime_code_state": "unchanged",
                        "retryable": True,
                        "suggested_next_step": (
                            "Retry reload after local generation storage is available."
                        ),
                    },
                )

            try:
                jdwp = self._connect()
                for composite in jdwp.drain_events():
                    self._resume_ignored_suspending_event(
                        jdwp,
                        "update_without_waiter",
                        composite,
                    )
                capabilities = jdwp.capabilities_new()
            except (JDWPError, RuntimeError, OSError) as error:
                return RuntimeResult(
                    ok=False,
                    error="The target JVM could not be prepared for HotSwap.",
                    data={
                        "error_code": "HOT_SWAP_PREPARE_FAILED",
                        "runtime_code_state": "unchanged",
                        **self._runtime_overlay_snapshot(),
                        "retryable": True,
                        "suggested_next_step": (
                            "Call status and retry update while the JVM is "
                            "reachable and no debug wait is active."
                        ),
                    },
                )
            if not capabilities.can_redefine_classes:
                return RuntimeResult(
                    ok=False,
                    error="The target JVM does not support class redefinition.",
                    data={
                        "error_code": "HOT_SWAP_UNSUPPORTED",
                        "runtime_code_state": "unchanged",
                        **self._runtime_overlay_snapshot(),
                        "retryable": False,
                        "suggested_next_step": (
                            "Use the formal Maven build and restart the "
                            "application."
                        ),
                    },
                )

            try:
                definitions = self._resolve_hotswap_definitions(
                    jdwp,
                    updates,
                    required_class_names={
                        name
                        for class_names in all_source_classes.values()
                        for name in class_names
                    },
                )
            except FastCompileError as error:
                return self._fast_compile_error_result(error, plan)
            except (JDWPError, OSError):
                return RuntimeResult(
                    ok=False,
                    error="Loaded class identity could not be resolved safely.",
                    data={
                        "error_code": "HOT_SWAP_PREPARE_FAILED",
                        "runtime_code_state": "unchanged",
                        **self._runtime_overlay_snapshot(),
                        "retryable": True,
                        "suggested_next_step": (
                            "Call status and retry while the JVM is reachable, "
                            "or use a formal build and restart."
                        ),
                    },
                )

            # Check the generation immediately before the state-changing JDWP
            # command. A process can exit independently of MCP serialization.
            current = self._project_update_context()
            if (
                isinstance(current, RuntimeResult)
                or current[0] != attempt_id
                or current[1] != generation
                or current[2] is not prepared
            ):
                return RuntimeResult(
                    ok=False,
                    error="The managed JVM changed before HotSwap was applied.",
                    data={
                        "error_code": "RUNTIME_CHANGED_DURING_UPDATE",
                        "runtime_code_state": "unchanged",
                        **self._runtime_overlay_snapshot(),
                        "retryable": True,
                        "suggested_next_step": (
                            "Call status and start a new update against the "
                            "current JVM."
                        ),
                    },
                )
            if not plan.is_fresh():
                return self._fast_compile_plan_stale_result()
            if not compile_attempt.sources_unchanged():
                return RuntimeResult(
                    ok=False,
                    error="A requested source changed before HotSwap was applied.",
                    data={
                        "error_code": "SOURCE_CHANGED_DURING_UPDATE",
                        "runtime_code_state": "unchanged",
                        **self._runtime_overlay_snapshot(),
                        "staging_discarded": True,
                        "retryable": True,
                        "suggested_next_step": (
                            "Wait for edits to finish, then retry update with "
                            "the current explicit source files."
                        ),
                    },
                )
            if not self._formal_outputs_match_launch(plan):
                return self._formal_output_changed_result()

            try:
                jdwp.redefine_classes(definitions)
            except JDWPCommandRejected as error:
                return RuntimeResult(
                    ok=False,
                    error="The target JVM rejected the class redefinition.",
                    data={
                        "error_code": "HOT_SWAP_REJECTED",
                        "jdwp_error_code": error.code,
                        "runtime_code_state": "unchanged",
                        **self._runtime_overlay_snapshot(),
                        "staging_discarded": True,
                        "retryable": False,
                        "possible_causes": [
                            "the JVM rejected a class schema change",
                            "the class file version is unsupported",
                            "bytecode verification failed",
                        ],
                        "suggested_next_step": (
                            "Use the formal Maven build and restart the "
                            "application."
                        ),
                    },
                )
            except JDWPCommandOutcomeUnknown:
                self._runtime_overlay_state = "unknown"
                self._runtime_overlay_sources.update(
                    all_source_classes.keys()
                )
                self._disconnect()
                return RuntimeResult(
                    ok=False,
                    error=(
                        "The JDWP connection failed after HotSwap transmission "
                        "began; the runtime code outcome is unknown."
                    ),
                    data={
                        "error_code": "HOT_SWAP_OUTCOME_UNKNOWN",
                        "runtime_code_state": "unknown",
                        **self._runtime_overlay_snapshot(),
                        "restart_required": True,
                        "retryable": False,
                        "suggested_next_step": (
                            "Restart the application before making any claim "
                            "about which code is running."
                        ),
                    },
                )
            except (JDWPError, OSError) as error:
                self._runtime_overlay_state = "unknown"
                self._runtime_overlay_sources.update(
                    all_source_classes.keys()
                )
                self._disconnect()
                return RuntimeResult(
                    ok=False,
                    error="The HotSwap result could not be confirmed.",
                    data={
                        "error_code": "HOT_SWAP_OUTCOME_UNKNOWN",
                        "runtime_code_state": "unknown",
                        **self._runtime_overlay_snapshot(),
                        "restart_required": True,
                        "retryable": False,
                        "suggested_next_step": (
                            "Restart the application before continuing runtime "
                            "verification."
                        ),
                    },
                )

            try:
                candidate_session.generations.promote_candidate()
                candidate_promoted = True
            except (OSError, ProjectSessionError):
                candidate_retained = True
                self._runtime_overlay_state = "unknown"
                self._runtime_overlay_sources.update(
                    all_source_classes.keys()
                )
                return RuntimeResult(
                    ok=False,
                    error=(
                        "HotSwap succeeded but the durable Generation could "
                        "not be promoted."
                    ),
                    data={
                        "error_code": "GENERATION_PROMOTION_FAILED",
                        "runtime_code_state": "unknown",
                        "applied": None,
                        "retryable": False,
                        "suggested_next_step": (
                            "Restart the application before further runtime "
                            "verification or reload attempts."
                        ),
                    },
                )
            self._record_applied_updates(updates, all_source_classes)
            breakpoint_refresh = self._refresh_updated_breakpoints(
                jdwp,
                {update.signature for update in updates},
            )
            self._code_revision += 1
            self._runtime_overlay_state = (
                "active"
                if self._runtime_overlay_class_hashes
                else "none"
            )
            warnings = list(breakpoint_refresh["warnings"])
            if breakpoint_refresh["stale"]:
                next_step = (
                    "Remove and set the listed stale breakpoint ids again "
                    "against the current source, then trigger a new request "
                    "and verify the expected runtime behavior. Treat HotSwap "
                    "acceptance as code-loading evidence, not proof that "
                    "Spring metadata, existing objects, or business "
                    "semantics were refreshed."
                )
            else:
                next_step = (
                    "Trigger a new request and verify the expected runtime "
                    "behavior. Treat HotSwap acceptance as code-loading "
                    "evidence, not proof that Spring metadata, existing "
                    "objects, or business semantics were refreshed."
                )
            return RuntimeResult(
                ok=True,
                data={
                    "status": "updated",
                    "applied": True,
                    "apply_method": "hotswap",
                    "compile_ms": round(
                        compile_attempt.elapsed_seconds * 1000,
                        1,
                    ),
                    "compiled_source_count": len(source_files),
                    "source_changes_pending": False,
                    "update_strategy": "fast_compile_hotswap",
                    "compiled_sources": sorted(all_source_classes),
                    "redefined_classes": sorted(
                        update.binary_name for update in updates
                    ),
                    "selection_coverage": "caller_provided",
                    "persistence": "committed_generation",
                    "restart_loses_update": False,
                    "framework_state_refreshed": False,
                    "runtime_code_state": "updated",
                    "breakpoint_refresh_state": breakpoint_refresh["state"],
                    "refreshed_breakpoint_ids": breakpoint_refresh[
                        "refreshed"
                    ],
                    "stale_breakpoint_ids": breakpoint_refresh["stale"],
                    "newly_stale_breakpoint_ids": breakpoint_refresh[
                        "newly_stale"
                    ],
                    **(
                        {
                            "breakpoint_stale_reason": (
                                breakpoint_refresh["reason"]
                            )
                        }
                        if breakpoint_refresh["reason"]
                        else {}
                    ),
                    "warnings": warnings,
                    **self._runtime_overlay_snapshot(),
                    "suggested_next_step": next_step,
                },
            )
        finally:
            if (
                candidate_prepared
                and not candidate_promoted
                and not candidate_retained
                and candidate_session is not None
            ):
                candidate_session.generations.discard_candidate()
            if compile_attempt is not None:
                self._fast_compiler.discard(compile_attempt)
            self._update_lock.release()

    def _project_update_context(
        self,
    ) -> tuple[str, int, ProjectUpdatePlan] | RuntimeResult:
        snapshot = self._reconcile_project_process_exit()
        if snapshot.get("launch_phase") != LaunchPhase.RUNTIME_ACTIVE.value:
            return RuntimeResult(
                ok=False,
                error="No active joLink project launch supports source update.",
                data={
                    "error_code": "FAST_COMPILE_UNSUPPORTED",
                    "runtime_code_state": "unchanged",
                    "retryable": True,
                    "suggested_next_step": (
                        "Run the application with project_path and wait until "
                        "launch_phase is runtime_active before calling update."
                    ),
                },
            )
        attempt_id = snapshot.get("attempt_id")
        generation = snapshot.get("generation")
        if not isinstance(attempt_id, str) or not isinstance(generation, int):
            return RuntimeResult(
                ok=False,
                error="The active project generation cannot be identified.",
                data={
                    "error_code": "FAST_COMPILE_UNSUPPORTED",
                    "runtime_code_state": "unchanged",
                    "retryable": True,
                    "suggested_next_step": (
                        "Restart the project launch before calling update."
                    ),
                },
            )
        with self._project_state_lock:
            prepared = self._project_update_plans.get(attempt_id)
        if prepared is None or prepared.fast_compile_plan is None:
            return RuntimeResult(
                ok=False,
                error="Fast source update is unavailable for this project launch.",
                data={
                    "error_code": (
                        prepared.fast_compile_unavailable_reason
                        if prepared is not None
                        and prepared.fast_compile_unavailable_reason
                        else "FAST_COMPILE_UNSUPPORTED"
                    ),
                    "runtime_code_state": "unchanged",
                    "retryable": False,
                    "suggested_next_step": (
                        "Use the formal Maven build and restart the application."
                    ),
                },
            )
        proc = self._proc.current
        if proc is None or not proc.is_alive():
            return RuntimeResult(
                ok=False,
                error="The managed project JVM is not running.",
                data={
                    "error_code": "PROCESS_NOT_RUNNING",
                    "runtime_code_state": "unchanged",
                    "retryable": True,
                    "suggested_next_step": (
                        "Call status, then run or restart the project."
                    ),
                },
            )
        return attempt_id, generation, prepared

    def _fast_compile_error_result(
        self,
        error: FastCompileError,
        plan,
    ) -> RuntimeResult:
        context = dict(error.context)
        raw_tail = context.pop("compile_log_tail", None)
        if isinstance(raw_tail, list):
            project_root = str(plan.project_root)
            context["compile_log_tail"] = [
                self._redact_build_log_line(
                    str(line).replace(project_root, "<project>")
                )
                for line in raw_tail
            ]
        return RuntimeResult(
            ok=False,
            error=str(error),
            data={
                "error_code": error.error_code,
                "applied": False,
                "stage": "compile",
                "runtime_code_state": "unchanged",
                "staging_discarded": True,
                **self._runtime_overlay_snapshot(),
                "retryable": error.retryable,
                **context,
                "suggested_next_step": error.suggested_next_step,
            },
        )

    def _fast_compile_plan_stale_result(self) -> RuntimeResult:
        return RuntimeResult(
            ok=False,
            error="The cached fast compile plan is stale.",
            data={
                "error_code": "FAST_COMPILE_PLAN_STALE",
                "runtime_code_state": "unchanged",
                **self._runtime_overlay_snapshot(),
                "retryable": True,
                "suggested_next_step": (
                    "Restart the project so joLink resolves the current "
                    "Maven/JDK compile environment before retrying update."
                ),
            },
        )

    def _formal_outputs_match_launch(
        self,
        plan,
    ) -> bool:
        try:
            current_files = sorted(plan.output_root.rglob("*.class"))
        except OSError:
            return False
        if len(current_files) != len(plan.baseline_class_hashes):
            return False
        current_relatives: set[str] = set()
        for class_file in current_files:
            try:
                relative = class_file.relative_to(
                    plan.output_root
                ).as_posix()
                current_relatives.add(relative)
                expected = plan.baseline_class_hashes.get(relative)
                if expected is None:
                    return False
                current = hashlib.sha256(class_file.read_bytes()).hexdigest()
            except OSError:
                return False
            if current != expected:
                return False
        return current_relatives == set(plan.baseline_class_hashes)

    def _formal_output_changed_result(self) -> RuntimeResult:
        return RuntimeResult(
            ok=False,
            error="Maven class output changed after this JVM was launched.",
            data={
                "error_code": "FORMAL_OUTPUT_CHANGED_SINCE_LAUNCH",
                "runtime_code_state": "unchanged",
                **self._runtime_overlay_snapshot(),
                "staging_discarded": True,
                "retryable": True,
                "suggested_next_step": (
                    "Restart the project so the running JVM and formal class "
                    "output share one baseline, then retry update."
                ),
            },
        )

    @staticmethod
    def _java_release_for_class_major(major: int) -> int:
        release = major - 44
        if release < 8 or release > 30:
            raise FastCompileError(
                "FAST_COMPILE_UNSUPPORTED",
                "The existing class-file version is outside the supported range.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build for this Java target."
                ),
                context={"class_major_version": major},
            )
        return release

    @staticmethod
    def _class_source_file(parsed: ParsedClassFile) -> str | None:
        for name, value in parsed.metadata:
            if name == "SourceFile" and isinstance(value, str):
                return value
        return None

    def _collect_source_classes(
        self,
        plan,
        source_files: tuple[Path, ...],
        *,
        classes_root: Path,
        require_launch_baseline: bool,
    ) -> dict[str, dict[str, tuple[ParsedClassFile, bytes, str]]]:
        result: dict[
            str,
            dict[str, tuple[ParsedClassFile, bytes, str]],
        ] = {}
        for source in source_files:
            source_key = source.relative_to(plan.project_root).as_posix()
            package_path = source.parent.relative_to(plan.source_root)
            class_directory = classes_root / package_path
            classes: dict[str, tuple[ParsedClassFile, bytes, str]] = {}
            if class_directory.is_dir():
                candidates = sorted(class_directory.glob("*.class"))
                if len(candidates) > _MAX_UPDATE_PACKAGE_CLASS_FILES:
                    raise FastCompileError(
                        "FAST_COMPILE_LIMIT_EXCEEDED",
                        "The source package contains too many class files.",
                        retryable=False,
                        suggested_next_step=(
                            "Use the formal Maven build and restart for this "
                            "large generated package."
                        ),
                        context={
                            "package_class_file_limit": (
                                _MAX_UPDATE_PACKAGE_CLASS_FILES
                            ),
                        },
                    )
                for class_file in candidates:
                    try:
                        raw = class_file.read_bytes()
                        parsed = parse_class_file(raw)
                    except (OSError, ClassFileFormatError) as error:
                        raise FastCompileError(
                            (
                                "INVALID_BASELINE_CLASS"
                                if require_launch_baseline
                                else "INVALID_COMPILED_CLASS"
                            ),
                            "A class file could not be read and validated.",
                            retryable=True,
                            suggested_next_step=(
                                "Wait for external build activity to finish. "
                                "If the problem remains, use the formal Maven "
                                "build and restart."
                            ),
                        ) from error
                    if self._class_source_file(parsed) != source.name:
                        continue
                    if len(classes) >= _MAX_UPDATE_CLASSES:
                        raise FastCompileError(
                            "FAST_COMPILE_LIMIT_EXCEEDED",
                            "One source generated too many class files.",
                            retryable=False,
                            suggested_next_step=(
                                "Use the formal Maven build and restart for "
                                "this generated source."
                            ),
                            context={
                                "generated_class_limit": _MAX_UPDATE_CLASSES,
                            },
                        )
                    relative = class_file.relative_to(classes_root).as_posix()
                    if require_launch_baseline:
                        expected_hash = plan.baseline_class_hashes.get(relative)
                        if (
                            expected_hash is None
                            or expected_hash != parsed.byte_sha256
                        ):
                            raise FastCompileError(
                                "FORMAL_OUTPUT_CHANGED_SINCE_LAUNCH",
                                (
                                    "Maven class output changed after this JVM "
                                    "was launched."
                                ),
                                retryable=True,
                                suggested_next_step=(
                                    "Restart the project so the running JVM and "
                                    "formal class output share one baseline, "
                                    "then retry update."
                                ),
                            )
                    classes[parsed.binary_name] = (
                        parsed,
                        raw,
                        relative,
                    )
            if not classes:
                raise FastCompileError(
                    (
                        "BASELINE_CLASS_NOT_FOUND"
                        if require_launch_baseline
                        else "COMPILED_CLASS_NOT_FOUND"
                    ),
                    (
                        "No compiled class could be mapped to a requested "
                        "Java source."
                    ),
                    retryable=True,
                    suggested_next_step=(
                        "Use the formal Maven build and restart, then retry "
                        "with a source under the selected src/main/java root."
                    ),
                    context={"source_file": source_key},
                )
            result[source_key] = classes
        return result

    def _prepare_class_updates(
        self,
        baseline_by_source,
        staged_by_source,
    ) -> tuple[list[StagedClassUpdate], dict[str, set[str]]]:
        updates: list[StagedClassUpdate] = []
        all_source_classes: dict[str, set[str]] = {}
        total_class_bytes = 0
        total_generated_classes = 0
        for source_key, baseline_classes in baseline_by_source.items():
            staged_classes = staged_by_source.get(source_key, {})
            baseline_names = set(baseline_classes)
            staged_names = set(staged_classes)
            total_generated_classes += len(staged_names)
            if total_generated_classes > _MAX_UPDATE_CLASSES:
                raise FastCompileError(
                    "FAST_COMPILE_LIMIT_EXCEEDED",
                    "The selected sources generate too many class files.",
                    retryable=False,
                    suggested_next_step=(
                        "Split method-body edits into smaller updates or use "
                        "the formal Maven build and restart."
                    ),
                    context={
                        "generated_class_limit": _MAX_UPDATE_CLASSES,
                    },
                )
            all_source_classes[source_key] = staged_names
            if baseline_names != staged_names:
                raise FastCompileError(
                    "GENERATED_CLASS_SET_CHANGED",
                    "The source now generates a different set of classes.",
                    retryable=False,
                    suggested_next_step=(
                        "Use the formal Maven build and restart because adding "
                        "or deleting top-level, inner, local, or anonymous "
                        "classes is outside standard HotSwap."
                    ),
                    context={
                        "source_file": source_key,
                        "baseline_class_count": len(baseline_names),
                        "staged_class_count": len(staged_names),
                    },
                )
            for binary_name in sorted(staged_names):
                baseline, _baseline_raw, _relative = baseline_classes[
                    binary_name
                ]
                staged, staged_raw, _staged_relative = staged_classes[
                    binary_name
                ]
                comparison = compare_class_files(baseline, staged)
                if comparison.kind is ClassFileChangeKind.UNSUPPORTED:
                    if "static_initializer_changed" in comparison.reasons:
                        raise FastCompileError(
                            "STATIC_INITIALIZER_CHANGE_REQUIRES_RESTART",
                            (
                                "The source change modifies static "
                                "initialization, which HotSwap does not run "
                                "again."
                            ),
                            retryable=False,
                            suggested_next_step=(
                                "Use the formal Maven build and restart the "
                                "application so static state is initialized "
                                "from the new code."
                            ),
                            context={
                                "class": binary_name,
                                "change_reasons": list(comparison.reasons),
                                "runtime_code_state": "unchanged",
                                "restart_required": True,
                            },
                        )
                    raise FastCompileError(
                        "CLASS_SCHEMA_CHANGED",
                        (
                            "The source change modifies class structure or "
                            "framework-visible metadata."
                        ),
                        retryable=False,
                        suggested_next_step=(
                            "Use the formal Maven build and restart the "
                            "application for structural, annotation, constant, "
                            "signature, or hierarchy changes."
                        ),
                        context={
                            "class": binary_name,
                            "change_reasons": list(comparison.reasons),
                        },
                    )
                current_hash = self._runtime_overlay_class_hashes.get(
                    binary_name,
                    baseline.byte_sha256,
                )
                if staged.byte_sha256 == current_hash:
                    continue
                total_class_bytes += len(staged_raw)
                if (
                    len(updates) >= _MAX_UPDATE_CLASSES
                    or total_class_bytes > _MAX_UPDATE_CLASS_BYTES
                ):
                    raise FastCompileError(
                        "FAST_COMPILE_LIMIT_EXCEEDED",
                        "The HotSwap class batch exceeds the update limit.",
                        retryable=False,
                        suggested_next_step=(
                            "Split method-body edits into smaller updates or "
                            "use the formal Maven build and restart."
                        ),
                        context={
                            "redefine_class_limit": _MAX_UPDATE_CLASSES,
                            "redefine_byte_limit": _MAX_UPDATE_CLASS_BYTES,
                        },
                    )
                updates.append(
                    StagedClassUpdate(
                        binary_name=binary_name,
                        signature=(
                            "L"
                            + staged.internal_name
                            + ";"
                        ),
                        baseline=baseline,
                        staged=staged,
                        class_bytes=staged_raw,
                        source_key=source_key,
                    )
                )
        return updates, all_source_classes

    def _resolve_hotswap_definitions(
        self,
        jdwp: JDWPClient,
        updates: list[StagedClassUpdate],
        *,
        required_class_names: set[str],
    ) -> dict[int, bytes]:
        candidates: dict[
            str,
            list[tuple[int, int]],
        ] = {}
        common_loaders: set[int] | None = None
        updates_by_name = {
            update.binary_name: update
            for update in updates
        }
        for binary_name in sorted(required_class_names):
            signature = "L" + binary_name.replace(".", "/") + ";"
            loaded = jdwp.classes_by_signature(signature)
            if not loaded:
                raise FastCompileError(
                    "CLASS_NOT_LOADED",
                    "A class generated by the source is not loaded in the current JVM.",
                    retryable=True,
                    suggested_next_step=(
                        "Trigger the code path that loads the class and retry. "
                        "If the source adds a new class, use a formal build and "
                        "restart instead."
                    ),
                    context={"class": binary_name},
                )
            per_class: list[tuple[int, int]] = []
            for item in loaded:
                loader = jdwp.reference_type_class_loader(
                    item.reference_type_id
                )
                per_class.append((item.reference_type_id, loader))
            candidates[binary_name] = per_class
            loaders = {loader for _reference, loader in per_class}
            common_loaders = (
                loaders
                if common_loaders is None
                else common_loaders & loaders
            )
        if common_loaders is None or len(common_loaders) != 1:
            raise FastCompileError(
                "AMBIGUOUS_CLASS_LOADER",
                "Changed classes do not map to one unique loaded class loader.",
                retryable=False,
                suggested_next_step=(
                    "Use the formal Maven build and restart, or narrow the "
                    "change to classes loaded once by the application."
                ),
            )
        selected_loader = next(iter(common_loaders))
        definitions: dict[int, bytes] = {}
        for binary_name in sorted(required_class_names):
            matching = [
                reference
                for reference, loader in candidates[binary_name]
                if loader == selected_loader
            ]
            if len(matching) != 1:
                raise FastCompileError(
                    "AMBIGUOUS_CLASS_LOADER",
                    "A changed class has multiple definitions in one loader.",
                    retryable=False,
                    suggested_next_step=(
                        "Use the formal Maven build and restart this JVM."
                    ),
                    context={"class": binary_name},
                )
            update = updates_by_name.get(binary_name)
            if update is not None:
                definitions[matching[0]] = update.class_bytes
        return definitions

    def _record_applied_updates(
        self,
        updates: list[StagedClassUpdate],
        all_source_classes: dict[str, set[str]],
    ) -> None:
        for update in updates:
            if (
                update.staged.byte_sha256
                == update.baseline.byte_sha256
            ):
                self._runtime_overlay_class_hashes.pop(
                    update.binary_name,
                    None,
                )
            else:
                self._runtime_overlay_class_hashes[
                    update.binary_name
                ] = update.staged.byte_sha256
        for source_key, class_names in all_source_classes.items():
            self._runtime_overlay_source_classes[source_key] = set(
                class_names
            )
            if any(
                name in self._runtime_overlay_class_hashes
                for name in class_names
            ):
                self._runtime_overlay_sources.add(source_key)
            else:
                self._runtime_overlay_sources.discard(source_key)

    def _refresh_updated_breakpoints(
        self,
        _jdwp: JDWPClient,
        signatures: set[str],
    ) -> dict[str, Any]:
        newly_stale = [
            breakpoint_id
            for breakpoint_id, definition in self._breakpoints.items()
            if definition.get("class") in signatures
            and not definition.get("stale")
        ]
        for breakpoint_id in newly_stale:
            definition = self._breakpoints[breakpoint_id]
            definition["stale"] = True
            definition["stale_reason"] = _BREAKPOINT_STALE_AFTER_REDEFINE
            definition["refresh_error"] = (
                "Breakpoint location must be set again after its class was "
                "redefined."
            )
        stale = [
            breakpoint_id
            for breakpoint_id, definition in self._breakpoints.items()
            if definition.get("stale")
        ]
        warnings = (
            [
                "Stale logical breakpoints remain after class redefinition; "
                "remove and set the listed breakpoint ids again against the "
                "current source before arming another breakpoint wait."
            ]
            if stale
            else []
        )
        return {
            "state": "partial" if stale else "complete",
            "refreshed": [],
            "stale": stale,
            "newly_stale": newly_stale,
            "reason": (
                _BREAKPOINT_STALE_AFTER_REDEFINE if stale else None
            ),
            "warnings": warnings,
        }

    # ── Debug ──────────────────────────────────────────

    def breakpoint(self, action: RuntimeAction) -> RuntimeResult:
        if not self._proc.is_running:
            return RuntimeResult(ok=False, error="No application running")

        logger.info(
            "java_runtime.breakpoint.request operation=%s request_id=%s class_pattern=%s line=%s active_count=%s",
            action.bp_action, action.request_id or "-", action.class_pattern or "-", action.line or "-",
            len(self._breakpoints),
        )
        try:
            if action.bp_action == "list":
                breakpoints = self._breakpoint_observations()
                logger.info(
                    "java_runtime.breakpoint.list count=%s",
                    len(breakpoints),
                )
                return RuntimeResult(ok=True, data={
                    "bp_action": "list",
                    "count": len(breakpoints),
                    "breakpoints": breakpoints,
                })

            if action.bp_action == "set":
                if not action.class_pattern:
                    return RuntimeResult(
                        ok=False,
                        error="class_pattern is required for breakpoint set",
                        data={
                            "error_code": "invalid_argument",
                            "argument": "class_pattern",
                            "bp_action": "set",
                        },
                    )
                if action.line <= 0:
                    return RuntimeResult(
                        ok=False,
                        error="line is required for breakpoint set",
                        data={
                            "error_code": "invalid_argument",
                            "argument": "line",
                            "bp_action": "set",
                        },
                    )

            if action.bp_action == "remove" and action.request_id:
                return RuntimeResult(
                    ok=False,
                    error=(
                        "request_id is an exception-watch identifier; "
                        "breakpoint removal requires breakpoint_id"
                    ),
                    data={
                        "error_code": "invalid_argument",
                        "argument": "request_id",
                        "bp_action": "remove",
                        "retryable": True,
                        "suggested_next_step": (
                            "Use the stable breakpoint_id returned by breakpoint set/list, "
                            "or remove by class_pattern and line."
                        ),
                    },
                )

            if action.bp_action == "set":
                jdwp = self._connect()
                candidate, resolution_error, _ignored_proxy = self._resolve_breakpoint_class(
                    jdwp, action
                )
                if resolution_error is not None:
                    return resolution_error
                assert candidate is not None

                locations, _nearby, location_error = self._line_locations_for_class(
                    jdwp, candidate, action.line
                )
                if location_error is not None:
                    return location_error
                location = locations[0]
                breakpoint_id = self._next_breakpoint_id()
                self._breakpoints[breakpoint_id] = {
                    "breakpoint_id": breakpoint_id,
                    "requested_class_pattern": action.class_pattern,
                    "include_proxy": action.include_proxy,
                    "include_generated": action.include_generated,
                    "class": candidate.signature,
                    "matched_class": candidate.name,
                    "source_file": candidate.source_file,
                    "is_proxy": candidate.is_proxy,
                    "proxy_type": candidate.proxy_type,
                    "method": location.method,
                    "method_signature": location.method_signature,
                    "line": action.line,
                    "code_index": location.code_index,
                    "suspend_policy": SuspendPolicy.EVENT_THREAD,
                }
                self._debug_connection_dirty = False
                self._debug_connection_warning = ""

                logger.info(
                    "java_runtime.breakpoint.set breakpoint_id=%s class=%s method=%s line=%s "
                    "code_index=%s armed=false",
                    breakpoint_id, candidate.name, location.method,
                    action.line, location.code_index,
                )

                return RuntimeResult(ok=True, data={
                    "breakpoint_id": breakpoint_id,
                    "matched_class": candidate.name,
                    "source_file": candidate.source_file,
                    "is_proxy": candidate.is_proxy,
                    "line": action.line,
                    "method": location.method,
                    "suspend_policy": self._suspend_policy_name(SuspendPolicy.EVENT_THREAD),
                    "warnings": candidate.warnings,
                    # Compatibility / diagnostics fields below. LLMs should use
                    # breakpoint_id; a JDWP request does not exist until wait_event.
                    "bp_action": "set",
                    "class": candidate.signature,
                    "method_signature": location.method_signature,
                    "proxy_type": candidate.proxy_type,
                    "jdwp": {
                        "suspend_policy": self._suspend_policy_name(SuspendPolicy.EVENT_THREAD),
                        "armed": False,
                    },
                    "suggested_next_step": (
                        "The breakpoint definition is configured but not armed. "
                        + _TWO_PHASE_WAIT_NEXT_STEP
                    ),
                })

            elif action.bp_action == "remove":
                if not self._breakpoints:
                    return RuntimeResult(ok=False, error="No breakpoints set")

                target_ids = self._breakpoint_remove_targets(action)
                if not target_ids:
                    return RuntimeResult(
                        ok=False,
                        error="No active breakpoints matched the remove selector",
                        data={
                            "bp_action": "remove",
                            "selector": self._breakpoint_selector(action),
                            "breakpoints": self._breakpoint_observations(),
                        },
                    )

                removed: list[str] = []
                removed_breakpoint_ids = []
                cleared_remote_ids: list[int] = []
                failed = []
                for breakpoint_id in target_ids:
                    remote_ids = [
                        remote_id
                        for remote_id, logical_id in self._armed_breakpoint_requests.items()
                        if logical_id == breakpoint_id
                    ]
                    clear_error = ""
                    for remote_id in remote_ids:
                        jdwp = self._connect()
                        payload = struct.pack(">BI", EventKind.BREAKPOINT, remote_id)
                        err, _ = jdwp.command(Cmd.EVENT, 2, payload)
                        if err:
                            clear_error = f"Clear breakpoint failed (err {err})"
                            break
                        self._armed_breakpoint_requests.pop(remote_id, None)
                        cleared_remote_ids.append(remote_id)
                    if clear_error:
                        failed.append({
                            "breakpoint_id": breakpoint_id,
                            "error": clear_error,
                        })
                        continue
                    self._breakpoints.pop(breakpoint_id, None)
                    removed.append(breakpoint_id)
                    removed_breakpoint_ids.append(breakpoint_id)

                if not removed:
                    return RuntimeResult(
                        ok=False,
                        error="Failed to clear any breakpoints",
                        data={
                            "bp_action": "remove",
                            "selector": self._breakpoint_selector(action),
                            "failed": failed,
                            "breakpoints": self._breakpoint_observations(),
                        },
                    )
                logger.info(
                    "java_runtime.breakpoint.removed breakpoint_ids=%s failed=%s remaining=%s",
                    removed, len(failed), len(self._breakpoints),
                )
                return RuntimeResult(ok=True, data={
                    "bp_action": "remove",
                    "selector": self._breakpoint_selector(action),
                    "cleared_breakpoint_ids": removed_breakpoint_ids,
                    "cleared_ids": removed_breakpoint_ids,
                    "failed": failed,
                    "partial": bool(failed),
                    "cleared_all": (
                        not action.breakpoint_id
                        and not action.class_pattern
                        and not action.line
                    ),
                    "remaining": len(self._breakpoints),
                    "breakpoints": self._breakpoint_observations(),
                    "jdwp": {"cleared_request_ids": cleared_remote_ids},
                })

            else:
                return RuntimeResult(ok=False, error=f"Unknown bp_action: {action.bp_action}")

        except JDWPError as e:
            logger.warning(
                "java_runtime.breakpoint.failed operation=%s class_pattern=%s line=%s error=%s",
                action.bp_action, action.class_pattern or "-", action.line or "-", e,
            )
            return RuntimeResult(ok=False, error=str(e))

    def exception(self, action: RuntimeAction) -> RuntimeResult:
        if not self._proc.is_running:
            return RuntimeResult(ok=False, error="No application running")

        logger.info(
            "java_runtime.exception.request operation=%s request_id=%s exception_class=%s "
            "caught=%s uncaught=%s active_count=%s",
            action.exception_action,
            action.request_id or "-",
            action.exception_class or "-",
            action.caught,
            action.uncaught,
            len(self._exceptions),
        )

        try:
            if action.exception_action == "list":
                exceptions = self._exception_observations()
                logger.info("java_runtime.exception.list count=%s", len(exceptions))
                return RuntimeResult(ok=True, data={
                    "exception_action": "list",
                    "count": len(exceptions),
                    "exceptions": exceptions,
                })

            if action.exception_action == "set":
                normalized_class, validation_error = self._validated_exception_signature(action)
                if validation_error:
                    return RuntimeResult(ok=False, error=validation_error)

                jdwp = self._connect()
                found = self._find_loaded_class_by_signature(jdwp, normalized_class)
                if found is None:
                    return RuntimeResult(
                        ok=False,
                        error=(
                            f"Exception class '{normalized_class}' is not loaded in the target VM"
                        ),
                        data={
                            "error_code": "exception_class_not_loaded",
                            "exception_class": normalized_class,
                            "signature": normalized_class,
                            "retryable": True,
                            "next_action": "trigger_code_path_then_retry_exception_set",
                            "suggestions": [
                                (
                                    "Trigger the code path once so the JVM loads this "
                                    "exception class, then set the exception event again."
                                ),
                                (
                                    "For framework conversion exceptions, send the "
                                    "request that causes the conversion once, then retry."
                                ),
                            ],
                        },
                    )
                request_id = self._next_exception_request_id()
                self._exceptions[request_id] = {
                    "exception_class": normalized_class,
                    "caught": action.caught,
                    "uncaught": action.uncaught,
                    "suspend_policy": SuspendPolicy.EVENT_THREAD,
                }
                self._debug_connection_dirty = False
                self._debug_connection_warning = ""
                logger.info(
                    "java_runtime.exception.set request_id=%s exception_class=%s caught=%s "
                    "uncaught=%s armed=false",
                    request_id, normalized_class, action.caught, action.uncaught,
                )
                return RuntimeResult(ok=True, data={
                    "exception_action": "set",
                    "request_id": request_id,
                    "exception_class": normalized_class,
                    "signature": normalized_class,
                    "caught": action.caught,
                    "uncaught": action.uncaught,
                    "suspend_policy": self._suspend_policy_name(SuspendPolicy.EVENT_THREAD),
                    "suggested_next_step": (
                        "The exception watch is configured but not armed. "
                        + _TWO_PHASE_WAIT_NEXT_STEP
                    ),
                })

            if action.exception_action == "remove":
                if not self._exceptions:
                    return RuntimeResult(ok=False, error="No exception events set")

                target_ids = self._exception_remove_targets(action)
                if not target_ids:
                    return RuntimeResult(
                        ok=False,
                        error="No active exception events matched the remove selector",
                        data={
                            "exception_action": "remove",
                            "selector": self._exception_selector(action),
                            "exceptions": self._exception_observations(),
                        },
                    )

                removed: list[int] = []
                cleared_remote_ids: list[int] = []
                failed = []
                for request_id in target_ids:
                    remote_ids = [
                        remote_id
                        for remote_id, logical_id in self._armed_exception_requests.items()
                        if logical_id == request_id
                    ]
                    clear_error = ""
                    for remote_id in remote_ids:
                        jdwp = self._connect()
                        payload = struct.pack(">BI", EventKind.EXCEPTION, remote_id)
                        err, _ = jdwp.command(Cmd.EVENT, 2, payload)
                        if err:
                            clear_error = f"Clear exception event failed (err {err})"
                            break
                        self._armed_exception_requests.pop(remote_id, None)
                        cleared_remote_ids.append(remote_id)
                    if clear_error:
                        failed.append({
                            "request_id": request_id,
                            "error": clear_error,
                        })
                        continue
                    self._exceptions.pop(request_id, None)
                    removed.append(request_id)

                if not removed:
                    return RuntimeResult(
                        ok=False,
                        error="Failed to clear any exception events",
                        data={
                            "exception_action": "remove",
                            "selector": self._exception_selector(action),
                            "failed": failed,
                            "exceptions": self._exception_observations(),
                        },
                    )

                logger.info(
                    "java_runtime.exception.removed request_ids=%s failed=%s remaining=%s",
                    removed, len(failed), len(self._exceptions),
                )
                return RuntimeResult(ok=True, data={
                    "exception_action": "remove",
                    "selector": self._exception_selector(action),
                    "cleared_ids": removed,
                    "failed": failed,
                    "partial": bool(failed),
                    "cleared_all": not action.request_id and not action.exception_class,
                    "remaining": len(self._exceptions),
                    "exceptions": self._exception_observations(),
                    "jdwp": {"cleared_request_ids": cleared_remote_ids},
                })

            return RuntimeResult(ok=False, error=f"Unknown exception_action: {action.exception_action}")

        except JDWPError as e:
            logger.warning(
                "java_runtime.exception.failed operation=%s exception_class=%s error=%s",
                action.exception_action, action.exception_class or "-", e,
            )
            return RuntimeResult(ok=False, error=str(e))

    def _arm_debug_requests(
        self,
        jdwp: JDWPClient,
        accepted_kinds: set[int],
        wait_control: WaitControl | None,
    ) -> RuntimeResult | None:
        """Create suspend-capable requests owned by exactly one active wait."""
        if self._armed_breakpoint_requests or self._armed_exception_requests:
            return RuntimeResult(
                ok=False,
                error="ACTIVE_DEBUG_REQUESTS_REMAIN",
                data={
                    "error_code": "active_debug_requests_remain",
                    "retryable": True,
                    "suggested_next_step": (
                        "Call cleanup_debug_state, set the required breakpoint or "
                        "exception watch again, then call wait_event with "
                        "wait_mode='arm'."
                    ),
                },
            )

        if EventKind.BREAKPOINT in accepted_kinds:
            if self._wait_cancelled(wait_control):
                return self._cancelled_wait_result(
                    "debug_event",
                    wait_control,
                )
            stale_breakpoints = [
                (breakpoint_id, definition)
                for breakpoint_id, definition in self._breakpoints.items()
                if definition.get("stale")
            ]
            if stale_breakpoints:
                breakpoint_id, definition = stale_breakpoints[0]
                stale_breakpoint_ids = [
                    item_id for item_id, _item in stale_breakpoints
                ]
                return RuntimeResult(
                    ok=False,
                    error="BREAKPOINT_DEFINITION_STALE",
                    data={
                        "error_code": "BREAKPOINT_DEFINITION_STALE",
                        "breakpoint_id": breakpoint_id,
                        "stale_breakpoint_ids": stale_breakpoint_ids,
                        "matched_class": definition.get(
                            "matched_class",
                            "",
                        ),
                        "line": definition.get("line", 0),
                        "stale_reason": definition.get(
                            "stale_reason",
                            _BREAKPOINT_STALE_AFTER_REDEFINE,
                        ),
                        "retryable": True,
                        "suggested_next_step": (
                            "Remove every listed stale breakpoint, set the "
                            "needed locations again against the current "
                            "source and bytecode, then call wait_event with "
                            "wait_mode='arm'. joLink refuses partial "
                            "breakpoint arming while stale definitions remain."
                        ),
                    },
                )
            for breakpoint_id, definition in self._breakpoints.items():
                if self._wait_cancelled(wait_control):
                    return self._cancelled_wait_result("debug_event", wait_control)

                resolve_action = RuntimeAction(
                    action="breakpoint",
                    bp_action="set",
                    class_pattern=str(definition.get("matched_class", "")),
                    line=int(definition.get("line", 0)),
                    include_proxy=bool(definition.get("include_proxy", False)),
                    include_generated=bool(definition.get("include_generated", False)),
                )
                candidate, resolution_error, _ = self._resolve_breakpoint_class(
                    jdwp,
                    resolve_action,
                )
                if resolution_error is not None:
                    return resolution_error
                assert candidate is not None
                locations, _nearby, location_error = self._line_locations_for_class(
                    jdwp,
                    candidate,
                    resolve_action.line,
                )
                if location_error is not None:
                    return location_error

                expected_signature = str(definition.get("class", ""))
                expected_method_signature = str(
                    definition.get("method_signature", "")
                )
                expected_code_index = int(definition.get("code_index", -1))
                location = next((
                    item
                    for item in locations
                    if item.method_signature == expected_method_signature
                    and item.code_index == expected_code_index
                ), None)
                if candidate.signature != expected_signature or location is None:
                    return RuntimeResult(
                        ok=False,
                        error="BREAKPOINT_DEFINITION_STALE",
                        data={
                            "error_code": "BREAKPOINT_DEFINITION_STALE",
                            "breakpoint_id": breakpoint_id,
                            "matched_class": definition.get("matched_class", ""),
                            "line": definition.get("line", 0),
                            "retryable": True,
                            "suggested_next_step": (
                                "Remove this breakpoint, set it again against the current bytecode, "
                                "then call wait_event with wait_mode='arm'."
                            ),
                        },
                    )

                payload = struct.pack(
                    ">BBI", EventKind.BREAKPOINT, SuspendPolicy.EVENT_THREAD, 1
                )
                payload += struct.pack(">B", 7)
                payload += struct.pack(">B", candidate.type_tag)
                payload += jdwp.ids.pack_ref(candidate.class_id)
                payload += jdwp.ids.pack_method(location.method_id)
                payload += struct.pack(">Q", location.code_index)
                err, data = jdwp.command(Cmd.EVENT, 1, payload)
                if not err and len(data) < 4:
                    # The VM may already have installed the request, but a
                    # malformed success reply gives us no id with which to
                    # clear it. Disconnecting is the only safe way to avoid
                    # an unowned future suspension.
                    self._disconnect()
                    self._invalidate_connection_scoped_requests(
                        "A malformed breakpoint EventRequest.Set reply forced "
                        "the JDWP connection to close; stable definitions were preserved.",
                        force_warning=True,
                    )
                if err or len(data) < 4:
                    return RuntimeResult(
                        ok=False,
                        error=f"Arm breakpoint failed (err {err})",
                        data={
                            "error_code": "ARM_BREAKPOINT_FAILED",
                            "breakpoint_id": breakpoint_id,
                            "retryable": True,
                            "suggested_next_step": (
                                "Retry wait_event with wait_mode='arm', or call "
                                "cleanup_debug_state and set the breakpoint again."
                            ),
                        },
                    )
                remote_id = struct.unpack_from(">I", data, 0)[0]
                self._armed_breakpoint_requests[remote_id] = breakpoint_id

        if EventKind.EXCEPTION in accepted_kinds:
            for request_id, definition in self._exceptions.items():
                if self._wait_cancelled(wait_control):
                    return self._cancelled_wait_result("debug_event", wait_control)
                signature = str(definition.get("exception_class", ""))
                found = self._find_loaded_class_by_signature(jdwp, signature)
                if found is None:
                    return RuntimeResult(
                        ok=False,
                        error=f"Exception class '{signature}' is not loaded in the target VM",
                        data={
                            "error_code": "exception_class_not_loaded",
                            "request_id": request_id,
                            "exception_class": signature,
                            "retryable": True,
                            "suggested_next_step": (
                                "Trigger class loading without relying on this watch, then "
                                "retry wait_event with wait_mode='arm'."
                            ),
                        },
                    )
                _type_tag, class_id, _signature = found
                payload = struct.pack(
                    ">BBI", EventKind.EXCEPTION, SuspendPolicy.EVENT_THREAD, 1
                )
                payload += struct.pack(">B", 8)
                payload += jdwp.ids.pack_ref(class_id)
                payload += struct.pack(
                    ">BB",
                    int(definition.get("caught", False)),
                    int(definition.get("uncaught", False)),
                )
                err, data = jdwp.command(Cmd.EVENT, 1, payload)
                if not err and len(data) < 4:
                    self._disconnect()
                    self._invalidate_connection_scoped_requests(
                        "A malformed exception EventRequest.Set reply forced "
                        "the JDWP connection to close; stable definitions were preserved.",
                        force_warning=True,
                    )
                if err or len(data) < 4:
                    return RuntimeResult(
                        ok=False,
                        error=f"Arm exception event failed (err {err})",
                        data={
                            "error_code": "ARM_EXCEPTION_EVENT_FAILED",
                            "request_id": request_id,
                            "retryable": True,
                            "suggested_next_step": (
                                "Retry wait_event with wait_mode='arm', or call "
                                "cleanup_debug_state and set the exception watch again."
                            ),
                        },
                    )
                remote_id = struct.unpack_from(">I", data, 0)[0]
                self._armed_exception_requests[remote_id] = request_id

        self._debug_connection_dirty = False
        self._debug_connection_warning = ""
        logger.info(
            "java_runtime.debug_requests.armed waiter_id=%s wait_generation=%s "
            "breakpoints=%s exceptions=%s",
            wait_control.waiter_id if wait_control is not None else "legacy",
            wait_control.wait_generation if wait_control is not None else 0,
            len(self._armed_breakpoint_requests),
            len(self._armed_exception_requests),
        )
        if wait_control is not None:
            wait_control.mark_armed(
                breakpoint_ids=list(self._armed_breakpoint_requests.values()),
                exception_ids=list(self._armed_exception_requests.values()),
            )
        return None

    def _disarm_debug_requests(
        self,
        jdwp: JDWPClient,
        *,
        wait_label: str,
    ) -> RuntimeResult | None:
        """Clear one wait generation and auto-resume every late event."""
        if not self._armed_breakpoint_requests and not self._armed_exception_requests:
            return None

        failures: list[dict[str, Any]] = []
        armed_breakpoints = dict(self._armed_breakpoint_requests)
        armed_exceptions = dict(self._armed_exception_requests)
        try:
            for remote_id, breakpoint_id in armed_breakpoints.items():
                err, _ = jdwp.command(
                    Cmd.EVENT,
                    2,
                    struct.pack(">BI", EventKind.BREAKPOINT, remote_id),
                )
                if err:
                    failures.append({
                        "event_kind": "breakpoint",
                        "breakpoint_id": breakpoint_id,
                        "jdwp_request_id": remote_id,
                        "error": f"Clear failed (err {err})",
                    })

            for remote_id, request_id in armed_exceptions.items():
                err, _ = jdwp.command(
                    Cmd.EVENT,
                    2,
                    struct.pack(">BI", EventKind.EXCEPTION, remote_id),
                )
                if err:
                    failures.append({
                        "event_kind": "exception",
                        "request_id": request_id,
                        "jdwp_request_id": remote_id,
                        "error": f"Clear failed (err {err})",
                    })

            # A command/reply round trip is a protocol barrier: events already
            # emitted while requests were being cleared are queued by command().
            err, _ = jdwp.command(Cmd.VM, 1)
            if err:
                failures.append({
                    "event_kind": "barrier",
                    "error": f"VM.Version barrier failed (err {err})",
                })

            for composite in jdwp.drain_events():
                events = composite.get("events", [])
                if any(
                    event.get("kind") in {
                        EventKind.VM_DEATH,
                        EventKind.VM_DISCONNECTED,
                    }
                    for event in events
                ):
                    self._invalidate_suspension()
                    continue
                try:
                    self._resume_ignored_suspending_event(
                        jdwp,
                        f"{wait_label}_disarm",
                        composite,
                    )
                except (JDWPError, OSError) as exc:
                    failures.append({
                        "event_kind": "late_event",
                        "error": str(exc),
                    })
        except (JDWPError, OSError) as exc:
            failures.append({
                "event_kind": "disarm",
                "error": str(exc),
            })
        finally:
            self._armed_breakpoint_requests.clear()
            self._armed_exception_requests.clear()

        logger.info(
            "java_runtime.debug_requests.disarmed wait=%s breakpoints=%s "
            "exceptions=%s failures=%s",
            wait_label,
            len(armed_breakpoints),
            len(armed_exceptions),
            len(failures),
        )
        if not failures:
            return None

        # A failed clear means a future hit could suspend a JVM without a
        # waiter. Closing the debugger connection is the only safe fallback.
        self._disconnect()
        self._invalidate_connection_scoped_requests(
            "JDWP request cleanup failed; the debugger connection was closed. "
            "Logical breakpoint and exception definitions were preserved.",
            force_warning=True,
        )
        return RuntimeResult(
            ok=False,
            error="DEBUG_REQUEST_DISARM_FAILED",
            data={
                "error_code": "DEBUG_REQUEST_DISARM_FAILED",
                "failures": failures,
                "retryable": True,
                "suggested_next_step": (
                    "Call status, then retry wait_event with wait_mode='arm'; "
                    "definitions will be re-armed on the new connection."
                ),
            },
        )

    def wait_breakpoint(self, action: RuntimeAction) -> RuntimeResult:
        if not self._proc.is_running:
            return RuntimeResult(ok=False, error="No application running")
        if not self._breakpoints:
            return RuntimeResult(ok=False, error="No breakpoints set")
        return self._wait_debug_event(
            action,
            accepted_kinds={EventKind.BREAKPOINT},
            wait_label="breakpoint",
        )

    def wait_event(
        self,
        action: RuntimeAction,
        *,
        wait_control: WaitControl | None = None,
    ) -> RuntimeResult:
        if not self._proc.is_running:
            return RuntimeResult(ok=False, error="No application running")
        accepted_kinds = set()
        if self._breakpoints:
            accepted_kinds.add(EventKind.BREAKPOINT)
        if self._exceptions:
            accepted_kinds.add(EventKind.EXCEPTION)
        if not accepted_kinds:
            return RuntimeResult(ok=False, error="No breakpoint or exception events set")
        return self._wait_debug_event(
            action,
            accepted_kinds=accepted_kinds,
            wait_label="debug_event",
            wait_control=wait_control,
        )

    def _wait_debug_event(
        self,
        action: RuntimeAction,
        *,
        accepted_kinds: set[int],
        wait_label: str,
        wait_control: WaitControl | None = None,
    ) -> RuntimeResult:
        if (
            self._active_suspension is not None
            and self._active_suspension.valid
            and not self._active_suspension.resumed
        ):
            logger.info(
                "java_runtime.%s.wait.active_suspension_exists "
                "suspension=%s generation=%s event_kind=%s suspend_policy=%s",
                wait_label,
                self._active_suspension.suspension_id,
                self._active_suspension.generation,
                self._active_suspension.event_kind,
                self._active_suspension.suspend_policy,
            )
            return RuntimeResult(ok=False, error="ACTIVE_SUSPENSION_EXISTS", data={
                "error_code": "active_suspension_exists",
                "status": "active_suspension_exists",
                **self._snapshot_context(self._active_suspension),
                "suggested_next_step": (
                    "Call resume with this suspension_id before waiting for another event. "
                    "If the JVM state looks dirty, call cleanup_debug_state."
                ),
            })

        jdwp: JDWPClient | None = None
        result: RuntimeResult | None = None
        disarm_error: RuntimeResult | None = None
        try:
            logger.info(
                "java_runtime.%s.wait.start timeout_seconds=%s active_breakpoints=%s active_exceptions=%s",
                wait_label, action.timeout, len(self._breakpoints), len(self._exceptions),
            )
            jdwp = self._connect()

            # Defensive recovery only. With the arm/disarm invariant there
            # should be no suspending event while no waiter owns the session.
            for composite in jdwp.drain_events():
                self._resume_ignored_suspending_event(
                    jdwp,
                    f"{wait_label}_pre_arm",
                    composite,
                )

            if self._wait_cancelled(wait_control):
                result = self._cancelled_wait_result(wait_label, wait_control)
            else:
                arm_error = self._arm_debug_requests(
                    jdwp,
                    accepted_kinds,
                    wait_control,
                )
                if arm_error is not None:
                    result = arm_error
                else:
                    promoted = self._drain_pending_debug_events(
                        jdwp,
                        wait_label,
                        accepted_kinds,
                        wait_control=wait_control,
                    )
                    if promoted is not None:
                        result = promoted
                    else:
                        deadline = time.monotonic() + max(action.timeout, 0.1)
                        while result is None:
                            if self._wait_cancelled(wait_control):
                                result = self._cancelled_wait_result(
                                    wait_label,
                                    wait_control,
                                )
                                break
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                result = self._debug_event_timeout(
                                    wait_label,
                                    action.timeout,
                                )
                                break
                            wait_slice = remaining
                            if wait_control is not None:
                                wait_slice = min(
                                    remaining,
                                    max(wait_control.poll_interval, 0.01),
                                )
                            composite = jdwp.wait_for_event(wait_slice)
                            if composite is None:
                                if wait_control is None:
                                    result = self._debug_event_timeout(
                                        wait_label,
                                        action.timeout,
                                    )
                                continue
                            result = self._handle_debug_composite(
                                jdwp,
                                composite,
                                accepted_kinds,
                                wait_label,
                                wait_control=wait_control,
                            )
        except (JDWPError, OSError, RuntimeError) as e:
            if self._wait_cancelled(wait_control):
                if wait_control is not None:
                    wait_control.mark_dirty()
                logger.info(
                    "java_runtime.%s.wait.cancelled_after_reader_wakeup "
                    "waiter_id=%s generation=%s error_type=%s",
                    wait_label,
                    wait_control.waiter_id if wait_control is not None else "-",
                    wait_control.wait_generation if wait_control is not None else "-",
                    type(e).__name__,
                )
                result = self._cancelled_wait_result(wait_label, wait_control)
            else:
                logger.warning("java_runtime.%s.wait.failed error=%s", wait_label, e)
                result = RuntimeResult(ok=False, error=str(e))
        finally:
            if jdwp is not None and (
                self._armed_breakpoint_requests
                or self._armed_exception_requests
            ):
                disarm_error = self._disarm_debug_requests(
                    jdwp,
                    wait_label=wait_label,
                )

        if disarm_error is not None:
            return disarm_error
        if result is None:
            return RuntimeResult(
                ok=False,
                error="wait_event ended without a result",
                data={
                    "error_code": "WAIT_EVENT_INCOMPLETE",
                    "retryable": True,
                    "suggested_next_step": (
                        "Start a new observation with wait_event and wait_mode='arm'."
                    ),
                },
            )
        return result

    def threads(self, action: RuntimeAction) -> RuntimeResult:
        try:
            snapshot = self._require_suspension(action)
            jdwp = self._connect()
            rows = []
            for thread_id in self._all_thread_ids(jdwp):
                err, status_data = jdwp.command(
                    Cmd.THREAD, 4, jdwp.ids.pack_obj(thread_id)
                )
                row: dict[str, Any] = {
                    "name": self._thread_name(jdwp, thread_id),
                    "is_breakpoint_thread": thread_id == snapshot.thread_id,
                    "is_suspension_thread": thread_id == snapshot.thread_id,
                }
                if err == 0 and len(status_data) >= 8:
                    thread_status, suspend_status = struct.unpack(">II", status_data[:8])
                    row["state"] = self._thread_status_name(thread_status)
                    row["suspended"] = bool(suspend_status & 1)
                else:
                    row["state"] = "unknown"
                    row["suspended"] = None
                rows.append(row)
            logger.info(
                "java_runtime.threads.observed suspension=%s count=%s breakpoint_thread=%s",
                snapshot.suspension_id, len(rows), self._thread_name(jdwp, snapshot.thread_id),
            )
            return RuntimeResult(ok=True, data={
                **self._snapshot_context(snapshot),
                "thread_count": len(rows),
                "threads": rows,
            })
        except (JDWPError, RuntimeError) as e:
            return RuntimeResult(ok=False, error=str(e))

    def stack(self, action: RuntimeAction) -> RuntimeResult:
        try:
            snapshot = self._require_suspension(action)
            jdwp = self._connect()
            thread_id = self._resolve_thread_id(jdwp, snapshot, action.thread_name)
            frames = self._read_frames(jdwp, thread_id, action.max_frames)
            logger.info(
                "java_runtime.stack.observed suspension=%s thread=%s frame_count=%s requested_max=%s",
                snapshot.suspension_id,
                self._thread_name(jdwp, thread_id),
                len(frames),
                action.max_frames,
            )
            return RuntimeResult(ok=True, data={
                **self._snapshot_context(snapshot),
                "thread": {"name": self._thread_name(jdwp, thread_id)},
                "frame_count": len(frames),
                "frames": [self._public_frame(frame) for frame in frames],
            })
        except (JDWPError, RuntimeError) as e:
            return RuntimeResult(ok=False, error=str(e))

    def variables(self, action: RuntimeAction) -> RuntimeResult:
        try:
            snapshot = self._require_suspension(action)
            jdwp = self._connect()
            ids = jdwp.ids
            thread_id = self._resolve_thread_id(jdwp, snapshot, action.thread_name)
            frames = self._read_frames(
                jdwp, thread_id, 1, start_index=action.frame_index
            )
            if not frames:
                return RuntimeResult(
                    ok=False,
                    error=f"Frame index {action.frame_index} does not exist",
                )
            frame = frames[0]
            if frame["is_native"]:
                return RuntimeResult(
                    ok=False,
                    error=f"Frame index {action.frame_index} is native and has no local variables",
                    data={"frame": self._public_frame(frame)},
                )

            payload = ids.pack_ref(frame["class_id"]) + ids.pack_method(frame["method_id"])
            err, variable_data = jdwp.command(Cmd.METHOD, 2, payload)
            if err:
                return RuntimeResult(
                    ok=False,
                    error=(
                        f"VariableTable failed (err {err}); compile the class "
                        "with debug variable information (-g)"
                    ),
                    data={"frame": self._public_frame(frame)},
                )
            variables = self._visible_variables_for_location(
                variable_data, frame["location_index"]
            )
            skipped_variables: list[dict[str, Any]] = []
            if not action.include_this:
                kept_variables: list[Variable] = []
                for variable in variables:
                    if variable.name == "this":
                        skipped_variables.append({
                            "name": variable.name,
                            "type": variable.type_name,
                            "slot": variable.slot,
                            "reason": "excluded_by_default",
                            "hint": "Pass include_this=true to inspect the receiver object.",
                        })
                    else:
                        kept_variables.append(variable)
                variables = kept_variables

            value_depth = self._value_depth(action.max_value_depth)
            item_limit = self._collection_item_limit(action.item_limit)
            map_entry_limit = self._collection_item_limit(action.map_entry_limit)

            getvalues_error = None
            if variables:
                values_payload = ids.pack_obj(thread_id) + ids.pack_frame(frame["frame_id"])
                values_payload += struct.pack(">I", len(variables))
                for variable in variables:
                    values_payload += struct.pack(">I", variable.slot)
                    values_payload += struct.pack(">B", Tag.from_sig(variable.type_name))
                err, values_data = jdwp.command(Cmd.STACK, 1, values_payload)
                if err == 0:
                    value_count = struct.unpack_from(">I", values_data, 0)[0]
                    offset = 4
                    for index in range(min(value_count, len(variables))):
                        try:
                            tag = values_data[offset]
                            offset += 1
                            variables[index].value, offset = self._read_value(
                                jdwp, ids, tag, values_data, offset,
                                depth=value_depth,
                                visited=set(),
                                semantic_collections=action.semantic_collections,
                                item_limit=item_limit,
                                map_entry_limit=map_entry_limit,
                            )
                            variables[index].value_observed = True
                        except Exception as exc:
                            variables[index].error = (
                                f"Failed to decode JVM value: {type(exc).__name__}: {exc}"
                            )
                            for remaining in variables[index + 1:]:
                                remaining.error = (
                                    "Value was not decoded because an earlier value "
                                    "made the JDWP response boundary unreliable"
                                )
                            break
                    if value_count < len(variables):
                        for variable in variables[value_count:]:
                            if not variable.error:
                                variable.error = (
                                    "JVM returned no value for this variable "
                                    f"({value_count} value(s) for {len(variables)} variable(s))"
                                )
                else:
                    getvalues_error = f"StackFrame/GetValues failed (err {err})"

                    for variable in variables:
                        variable.error = getvalues_error

            variable_results = [
                self._variable_observation(variable) for variable in variables
            ]
            complete = all(variable.value_observed for variable in variables)
            observed_count = sum(variable.value_observed for variable in variables)
            unavailable_count = len(variables) - observed_count
            logger.info(
                "java_runtime.variables.observed suspension=%s thread=%s frame_index=%s "
                "total=%s skipped=%s observed=%s unavailable=%s complete=%s "
                "include_this=%s max_value_depth=%s",
                snapshot.suspension_id,
                self._thread_name(jdwp, thread_id),
                action.frame_index,
                len(variables),
                len(skipped_variables),
                observed_count,
                unavailable_count,
                complete,
                action.include_this,
                value_depth,
            )

            return RuntimeResult(ok=True, data={
                **self._snapshot_context(snapshot),
                "thread": {"name": self._thread_name(jdwp, thread_id)},
                "frame": self._public_frame(frame),
                "variable_count": len(variables),
                "skipped_variable_count": len(skipped_variables),
                "complete": complete,
                "partial": not complete,
                "variables": variable_results,
                "skipped_variables": skipped_variables,
                "include_this": action.include_this,
                "max_value_depth": value_depth,
                "semantic_collections": action.semantic_collections,
                "item_limit": item_limit,
                "map_entry_limit": map_entry_limit,
                "getvalues_error": getvalues_error,
            })
        except (JDWPError, RuntimeError) as e:
            return RuntimeResult(ok=False, error=str(e))

    def resume(self, action: RuntimeAction) -> RuntimeResult:
        try:
            snapshot = self._require_suspension(action)
            jdwp = self._connect()
            err, resume_scope = self._resume_snapshot(jdwp, snapshot)
            if err:
                return RuntimeResult(
                    ok=False,
                    error=f"{resume_scope} resume failed (err {err})",
                    data={
                        "error_code": "resume_failed",
                        **self._snapshot_context(snapshot),
                        "resume_scope": resume_scope,
                        "suggested_next_step": (
                            "Call cleanup_debug_state to clear local debug requests "
                            "and emergency-resume the VM."
                        ),
                    },
                )
            suspension_id = snapshot.suspension_id
            snapshot.resumed = True
            self._invalidate_suspension()
            logger.info(
                "java_runtime.suspension.resumed suspension=%s generation=%s "
                "resume_scope=%s suspend_policy=%s thread_id=%s",
                suspension_id, snapshot.generation, resume_scope,
                snapshot.suspend_policy, snapshot.thread_id,
            )
            return RuntimeResult(ok=True, data={
                "status": "resumed",
                "invalidated_suspension_id": suspension_id,
                "resume_scope": resume_scope,
                "suspend_policy": snapshot.suspend_policy,
                "suspend_policy_name": self._suspend_policy_name(snapshot.suspend_policy),
                "thread_id": snapshot.thread_id,
                "process_state": "running",
                "debug_state": "attached",
                "suggested_next_step": _TWO_PHASE_WAIT_NEXT_STEP,
            })
        except (JDWPError, RuntimeError) as e:
            return RuntimeResult(ok=False, error=str(e))

    def cleanup_debug_state(self, action: RuntimeAction) -> RuntimeResult:
        """Best-effort recovery for dirty dogfood debug state."""
        if not self._proc.is_running:
            self._reset_debug_state()
            return RuntimeResult(ok=True, data={
                "status": "debug_state_cleaned",
                "process_state": "absent",
                "debug_state": "detached",
                "message": "No running application; local debug state was cleared.",
                "verification_state": "complete",
                "verification": {
                    "active_suspension": False,
                    "logical_breakpoint_count": 0,
                    "logical_exception_count": 0,
                    "runtime_tracked_breakpoint_request_count": 0,
                    "runtime_tracked_exception_request_count": 0,
                },
                "suggested_next_step": (
                    "Debug state is clean. Start or attach to an application "
                    "before configuring another observation."
                ),
            })

        warnings: list[str] = []
        drained_events = 0
        resumed_active_suspension = False
        emergency_vm_resume = False
        cleared_breakpoints: list[str] = list(self._breakpoints)
        cleared_exceptions: list[int] = list(self._exceptions)
        clear_failures: list[dict[str, Any]] = []
        had_active_suspension = (
            self._active_suspension is not None
            and self._active_suspension.valid
            and not self._active_suspension.resumed
        )

        try:
            jdwp = self._connect()

            for composite in jdwp.drain_events():
                drained_events += 1
                try:
                    self._resume_ignored_suspending_event(
                        jdwp, "cleanup_debug_state", composite
                    )
                except JDWPError as exc:
                    warnings.append(str(exc))

            for remote_id, breakpoint_id in list(
                self._armed_breakpoint_requests.items()
            ):
                payload = struct.pack(">BI", EventKind.BREAKPOINT, remote_id)
                err, _ = jdwp.command(Cmd.EVENT, 2, payload)
                if err:
                    clear_failures.append({
                        "event_kind": "breakpoint",
                        "breakpoint_id": breakpoint_id,
                        "jdwp_request_id": remote_id,
                        "error": f"Clear breakpoint failed (err {err})",
                    })

            for remote_id, request_id in list(
                self._armed_exception_requests.items()
            ):
                payload = struct.pack(">BI", EventKind.EXCEPTION, remote_id)
                err, _ = jdwp.command(Cmd.EVENT, 2, payload)
                if err:
                    clear_failures.append({
                        "event_kind": "exception",
                        "request_id": request_id,
                        "jdwp_request_id": remote_id,
                        "error": f"Clear exception event failed (err {err})",
                    })

            if (
                self._active_suspension is not None
                and self._active_suspension.valid
                and not self._active_suspension.resumed
            ):
                err, scope = self._resume_snapshot(jdwp, self._active_suspension)
                if err:
                    warnings.append(f"{scope} resume failed (err {err})")
                else:
                    self._active_suspension.resumed = True
                    resumed_active_suspension = True

            err, _ = jdwp.command(Cmd.VM, 9)
            if err:
                warnings.append(f"Emergency VM.Resume failed (err {err})")
            else:
                emergency_vm_resume = True

            self._breakpoints.clear()
            self._exceptions.clear()
            self._armed_breakpoint_requests.clear()
            self._armed_exception_requests.clear()
            self._invalidate_suspension()
            debug_state = "attached"
            if not had_active_suspension:
                suspension_recovery = "not_needed"
            elif resumed_active_suspension:
                suspension_recovery = "thread_resumed"
            elif emergency_vm_resume:
                suspension_recovery = "vm_resumed"
            else:
                suspension_recovery = "unverified"
            if clear_failures:
                # Clear failures cannot be left behind: a future event could
                # otherwise suspend a JVM after local definitions are gone.
                self._disconnect()
                debug_state = "detached"
                warnings.append(
                    "JDWP was disconnected because one or more event requests could not be cleared."
                )
            if had_active_suspension and not (
                resumed_active_suspension or emergency_vm_resume
            ):
                if debug_state != "detached":
                    self._disconnect()
                    debug_state = "detached"
                suspension_recovery = "jdwp_disconnected"
                warnings.append(
                    "JDWP was disconnected because suspension recovery could "
                    "not be confirmed by ThreadReference.Resume or VM.Resume."
                )
            if not warnings and not clear_failures:
                self._debug_connection_dirty = False
                self._debug_connection_warning = ""

            verification_state = (
                "complete" if not warnings and not clear_failures else "partial"
            )
            verification = {
                "active_suspension": False,
                "logical_breakpoint_count": len(self._breakpoints),
                "logical_exception_count": len(self._exceptions),
                "runtime_tracked_breakpoint_request_count": len(
                    self._armed_breakpoint_requests
                ),
                "runtime_tracked_exception_request_count": len(
                    self._armed_exception_requests
                ),
                "remote_request_release": (
                    "jdwp_disconnected"
                    if debug_state == "detached"
                    else "cleared"
                ),
                "suspension_recovery": suspension_recovery,
            }

            logger.info(
                "java_runtime.cleanup_debug_state.finish drained_events=%s "
                "cleared_breakpoints=%s cleared_exceptions=%s failures=%s "
                "resumed_active=%s emergency_vm_resume=%s warnings=%s",
                drained_events, len(cleared_breakpoints), len(cleared_exceptions),
                len(clear_failures), resumed_active_suspension,
                emergency_vm_resume, len(warnings),
            )
            return RuntimeResult(ok=True, data={
                "status": "debug_state_cleaned",
                "process_state": "running",
                "debug_state": debug_state,
                "drained_events": drained_events,
                "resumed_active_suspension": resumed_active_suspension,
                "emergency_vm_resume": emergency_vm_resume,
                "cleared_breakpoint_ids": cleared_breakpoints,
                "cleared_exception_ids": cleared_exceptions,
                "cleared_local_breakpoint_count": len(cleared_breakpoints),
                "cleared_local_exception_count": len(cleared_exceptions),
                "clear_failures": clear_failures,
                "warnings": warnings,
                "verification_state": verification_state,
                "verification": verification,
                "suggested_next_step": (
                    "Debug state is clean. Configure another breakpoint or "
                    "exception observation when needed."
                    if verification_state == "complete"
                    else
                    "Debug state was released with warnings. Review the returned "
                    "warnings before starting another observation; attach again "
                    "first if debug_state is detached."
                ),
            })
        except (JDWPError, OSError) as e:
            logger.warning("java_runtime.cleanup_debug_state.failed error=%s", e)
            return RuntimeResult(ok=False, error=str(e), data={
                "error_code": "cleanup_debug_state_failed",
                "suggested_next_step": (
                    "If the target process is still running, detach/attach or restart it; "
                    "otherwise call stop and run again."
                ),
            })

    # ── Internal ───────────────────────────────────────

    def _capture_breakpoint_event(
        self,
        jdwp: JDWPClient,
        event: dict[str, Any],
        breakpoint_id: str,
        suspend_policy: int = SuspendPolicy.EVENT_THREAD,
        wait_control: WaitControl | None = None,
        suspended_thread_ids: tuple[int, ...] = (),
    ) -> RuntimeResult:
        jdwp_request_id = int(event.get("request_id", 0))
        self._suspension_generation += 1
        observed_at = datetime.now(timezone.utc).isoformat()
        snapshot = SuspensionSnapshot(
            suspension_id=f"susp_{uuid.uuid4().hex[:12]}",
            generation=self._suspension_generation,
            request_id=0,
            thread_id=int(event["thread_id"]),
            location=event.get("location") or {},
            observed_at=observed_at,
            jdwp_request_id=jdwp_request_id,
            breakpoint_id=breakpoint_id,
            created_at=observed_at,
            event_kind="breakpoint",
            event_type="breakpoint",
            suspend_policy=suspend_policy,
            event=event,
            waiter_id=wait_control.waiter_id if wait_control is not None else "",
            wait_generation=(
                wait_control.wait_generation if wait_control is not None else 0
            ),
            suspended_thread_ids=suspended_thread_ids,
        )
        self._active_suspension = snapshot
        if wait_control is not None:
            wait_control.mark_phase("suspension_created")
        location_description = self._describe_location(jdwp, snapshot.location)
        thread_name = self._thread_name(jdwp, snapshot.thread_id)
        logger.info(
            "java_runtime.breakpoint.hit suspension=%s generation=%s breakpoint_id=%s "
            "jdwp_request_id=%s "
            "thread=%s class=%s method=%s line=%s",
            snapshot.suspension_id,
            snapshot.generation,
            breakpoint_id,
            jdwp_request_id,
            thread_name,
            location_description.get("class", "-"),
            location_description.get("method", "-"),
            location_description.get("line", "-"),
        )
        return RuntimeResult(ok=True, data={
            "status": "breakpoint_hit",
            **self._snapshot_context(snapshot),
            "breakpoint": self._breakpoint_observation(
                breakpoint_id,
                self._breakpoints[breakpoint_id],
            ),
            "thread": {"name": thread_name},
            "location": location_description,
            "suggested_next_step": (
                "Inspect stack or variables with this suspension_id and omit "
                "thread_name to use the event-hit thread. Call resume with the "
                "same suspension_id after inspection."
            ),
            "suggested_next_actions": self._suggested_suspension_actions(
                snapshot
            ),
        })

    def _capture_exception_event(
        self,
        jdwp: JDWPClient,
        event: dict[str, Any],
        request_id: int,
        suspend_policy: int = SuspendPolicy.EVENT_THREAD,
        wait_control: WaitControl | None = None,
        suspended_thread_ids: tuple[int, ...] = (),
    ) -> RuntimeResult:
        jdwp_request_id = int(event.get("request_id", 0))
        exception_request = self._exceptions[request_id]
        self._suspension_generation += 1
        observed_at = datetime.now(timezone.utc).isoformat()
        snapshot = SuspensionSnapshot(
            suspension_id=f"susp_{uuid.uuid4().hex[:12]}",
            generation=self._suspension_generation,
            request_id=request_id,
            thread_id=int(event["thread_id"]),
            location=event.get("location") or {},
            observed_at=observed_at,
            jdwp_request_id=jdwp_request_id,
            created_at=observed_at,
            event_kind="exception",
            event_type="exception",
            suspend_policy=suspend_policy,
            event=event,
            waiter_id=wait_control.waiter_id if wait_control is not None else "",
            wait_generation=(
                wait_control.wait_generation if wait_control is not None else 0
            ),
            suspended_thread_ids=suspended_thread_ids,
        )
        self._active_suspension = snapshot
        if wait_control is not None:
            wait_control.mark_phase("suspension_created")

        location_description = self._describe_location(jdwp, snapshot.location)
        catch_location = event.get("catch_location") or {}
        catch_description = (
            None
            if self._is_empty_location(catch_location)
            else self._describe_location(jdwp, catch_location)
        )
        thread_name = self._thread_name(jdwp, snapshot.thread_id)
        exception_object = event.get("exception") or {}
        object_id = int(exception_object.get("object_id", 0) or 0)
        thrown_class = self._object_class_signature(jdwp, object_id) if object_id else "unknown"
        logger.info(
            "java_runtime.exception.hit suspension=%s generation=%s request_id=%s "
            "jdwp_request_id=%s "
            "thread=%s exception_class=%s thrown_class=%s class=%s method=%s line=%s caught=%s",
            snapshot.suspension_id,
            snapshot.generation,
            request_id,
            jdwp_request_id,
            thread_name,
            exception_request.get("exception_class", "-"),
            thrown_class,
            location_description.get("class", "-"),
            location_description.get("method", "-"),
            location_description.get("line", "-"),
            catch_description is not None,
        )
        return RuntimeResult(ok=True, data={
            "status": "exception_hit",
            "event_type": "exception",
            **self._snapshot_context(snapshot),
            "exception": {
                "request_id": request_id,
                "exception_class": exception_request.get("exception_class", ""),
                "signature": exception_request.get("exception_class", ""),
                "thrown_class": thrown_class,
                "value": self._reference_value(object_id, "object") if object_id else None,
                "caught": catch_description is not None,
                "request_caught": exception_request.get("caught", False),
                "request_uncaught": exception_request.get("uncaught", False),
            },
            "jdwp": {"request_id": jdwp_request_id},
            "thread": {"name": thread_name},
            "throw_location": location_description,
            "location": location_description,
            "catch_location": catch_description,
            "hint": (
                "throw_location may be inside JDK or framework code. Inspect the stack "
                "and the first application frame to find the business root cause."
            ),
            "suggested_next_step": (
                "Inspect stack or variables with this suspension_id and omit "
                "thread_name to use the event-hit thread. Call resume with the "
                "same suspension_id after inspection."
            ),
            "suggested_next_actions": self._suggested_suspension_actions(
                snapshot
            ),
        })

    def _debug_event_timeout(self, wait_label: str, timeout: float) -> RuntimeResult:
        logger.info(
            "java_runtime.%s.wait.timeout timeout_seconds=%s process_running=%s",
            wait_label, timeout, self._proc.is_running,
        )
        return RuntimeResult(ok=True, data={
            "status": "timeout",
            "wait": wait_label,
            "timeout_seconds": timeout,
            "process_state": "running",
            "debug_state": "attached",
            "suggested_next_step": (
                "Confirm the logical breakpoint or exception definition with list, "
                "then start a new observation. " + _TWO_PHASE_WAIT_NEXT_STEP
            ),
        })

    def _drain_pending_debug_events(
        self,
        jdwp: JDWPClient,
        wait_label: str,
        accepted_kinds: set[int] | None = None,
        *,
        wait_control: WaitControl | None = None,
    ) -> RuntimeResult | None:
        accepted = accepted_kinds if accepted_kinds is not None else self._accepted_event_kinds()
        for composite in jdwp.drain_events():
            handled = self._handle_debug_composite(
                jdwp,
                composite,
                accepted,
                wait_label,
                wait_control=wait_control,
            )
            if handled is not None:
                return handled
        return None

    def _accepted_event_kinds(self) -> set[int]:
        accepted_kinds: set[int] = set()
        if self._breakpoints:
            accepted_kinds.add(EventKind.BREAKPOINT)
        if self._exceptions:
            accepted_kinds.add(EventKind.EXCEPTION)
        return accepted_kinds

    def _handle_debug_composite(
        self,
        jdwp: JDWPClient,
        composite: dict[str, Any],
        accepted_kinds: set[int],
        wait_label: str,
        *,
        wait_control: WaitControl | None = None,
    ) -> RuntimeResult | None:
        if self._wait_cancelled(wait_control):
            self._settle_cancelled_composite(
                jdwp,
                composite,
                wait_control,
                wait_label=wait_label,
            )
            return self._cancelled_wait_result(wait_label, wait_control)

        suspend_policy = int(composite.get("suspend_policy", SuspendPolicy.NONE) or 0)
        suspended_thread_ids = ()
        if suspend_policy == SuspendPolicy.EVENT_THREAD:
            suspended_thread_ids = tuple(sorted({
                int(event.get("thread_id", 0) or 0)
                for event in composite.get("events", [])
                if int(event.get("thread_id", 0) or 0) > 0
            }))
        handled = False
        for event in composite.get("events", []):
            if event.get("kind") in {
                EventKind.VM_DEATH, EventKind.VM_DISCONNECTED,
            }:
                self._invalidate_suspension()
                return RuntimeResult(
                    ok=False,
                    error=f"Target VM exited while waiting for {wait_label}",
                    data={
                        "error_code": "target_vm_exited",
                        "suggested_next_step": "Call status, then run or attach to a live JVM again.",
                    },
                )
            request_id = int(event.get("request_id", 0))
            event_kind = int(event.get("kind", 0))
            if event_kind not in accepted_kinds:
                continue
            breakpoint_id = self._armed_breakpoint_requests.get(request_id)
            if (
                event_kind == EventKind.BREAKPOINT
                and breakpoint_id is not None
                and breakpoint_id in self._breakpoints
            ):
                handled = True
                result = self._capture_breakpoint_event(
                    jdwp,
                    event,
                    breakpoint_id,
                    suspend_policy,
                    wait_control,
                    suspended_thread_ids,
                )
                if self._wait_cancelled(wait_control):
                    snapshot = self._active_suspension
                    if snapshot is not None:
                        self._resume_cancelled_snapshot(
                            jdwp,
                            snapshot,
                            wait_control,
                            wait_label=wait_label,
                        )
                    return self._cancelled_wait_result(wait_label, wait_control)
                return result
            exception_request_id = self._armed_exception_requests.get(request_id)
            if (
                event_kind == EventKind.EXCEPTION
                and exception_request_id is not None
                and exception_request_id in self._exceptions
            ):
                handled = True
                result = self._capture_exception_event(
                    jdwp,
                    event,
                    exception_request_id,
                    suspend_policy,
                    wait_control,
                    suspended_thread_ids,
                )
                if self._wait_cancelled(wait_control):
                    snapshot = self._active_suspension
                    if snapshot is not None:
                        self._resume_cancelled_snapshot(
                            jdwp,
                            snapshot,
                            wait_control,
                            wait_label=wait_label,
                        )
                    return self._cancelled_wait_result(wait_label, wait_control)
                return result
        if not handled:
            self._resume_ignored_suspending_event(jdwp, wait_label, composite)
        return None

    @staticmethod
    def _wait_cancelled(wait_control: WaitControl | None) -> bool:
        return wait_control is not None and wait_control.cancelled

    def _cancelled_wait_result(
        self,
        wait_label: str,
        wait_control: WaitControl | None,
    ) -> RuntimeResult:
        if wait_control is not None:
            wait_control.mark_phase("settling")
        logger.info(
            "java_runtime.%s.wait.cancelled waiter_id=%s generation=%s reason=%s",
            wait_label,
            wait_control.waiter_id if wait_control is not None else "-",
            wait_control.wait_generation if wait_control is not None else "-",
            wait_control.cancel_reason if wait_control is not None else "-",
        )
        return RuntimeResult(ok=True, data={
            "status": "wait_cancelled",
            "wait": wait_label,
        })

    @staticmethod
    def _snapshot_owned_by_waiter(
        snapshot: SuspensionSnapshot,
        wait_control: WaitControl,
    ) -> bool:
        return (
            snapshot.waiter_id == wait_control.waiter_id
            and snapshot.wait_generation == wait_control.wait_generation
        )

    def _resume_cancelled_snapshot(
        self,
        jdwp: JDWPClient,
        snapshot: SuspensionSnapshot,
        wait_control: WaitControl | None,
        *,
        wait_label: str,
    ) -> None:
        if wait_control is None:
            return
        if not self._snapshot_owned_by_waiter(snapshot, wait_control):
            wait_control.mark_dirty()
            logger.error(
                "java_runtime.%s.wait.cancel.snapshot_owner_mismatch "
                "snapshot=%s snapshot_waiter=%s snapshot_generation=%s "
                "waiter_id=%s generation=%s",
                wait_label,
                snapshot.suspension_id,
                snapshot.waiter_id or "-",
                snapshot.wait_generation,
                wait_control.waiter_id,
                wait_control.wait_generation,
            )
            return
        if not snapshot.valid or snapshot.resumed:
            return

        err, scope = self._resume_snapshot(jdwp, snapshot)
        if err:
            wait_control.mark_dirty()
            raise JDWPError(
                err,
                f"Resume cancelled {wait_label} suspension failed ({scope})",
            )
        snapshot.resumed = True
        snapshot.valid = False
        if self._active_suspension is snapshot:
            self._active_suspension = None
        wait_control.mark_phase("event_auto_resumed")
        logger.info(
            "java_runtime.%s.wait.cancel.suspension_resumed "
            "suspension=%s waiter_id=%s generation=%s resume_scope=%s",
            wait_label,
            snapshot.suspension_id,
            wait_control.waiter_id,
            wait_control.wait_generation,
            scope,
        )

    def _settle_cancelled_composite(
        self,
        jdwp: JDWPClient,
        composite: dict[str, Any],
        wait_control: WaitControl | None,
        *,
        wait_label: str,
    ) -> None:
        events = composite.get("events", [])
        if any(
            event.get("kind") in {
                EventKind.VM_DEATH,
                EventKind.VM_DISCONNECTED,
            }
            for event in events
        ):
            self._invalidate_suspension()
            return
        try:
            self._resume_ignored_suspending_event(jdwp, wait_label, composite)
        except (JDWPError, OSError) as thread_resume_error:
            if wait_control is not None:
                wait_control.mark_dirty()
            logger.warning(
                "java_runtime.%s.wait.cancel.event_thread_resume_failed "
                "waiter_id=%s generation=%s error=%s",
                wait_label,
                wait_control.waiter_id if wait_control is not None else "-",
                wait_control.wait_generation if wait_control is not None else "-",
                _error_summary(thread_resume_error),
            )
            try:
                err, _ = jdwp.command(Cmd.VM, 9)
            except (JDWPError, OSError) as vm_resume_error:
                err = -1
                logger.warning(
                    "java_runtime.%s.wait.cancel.vm_resume_crashed error=%s",
                    wait_label,
                    _error_summary(vm_resume_error),
                )
            if err:
                logger.error(
                    "java_runtime.%s.wait.cancel.vm_resume_failed "
                    "error_code=%s action=disconnect",
                    wait_label,
                    err,
                )
                try:
                    jdwp.close()
                except Exception as disconnect_error:
                    raise JDWPError(
                        err,
                        "Cancelled event resume and forced disconnect failed: "
                        f"{disconnect_error}",
                    ) from thread_resume_error
                if self._jdwp is jdwp:
                    self._jdwp = None
                self._invalidate_connection_scoped_requests(
                    "The JDWP connection was force-closed after a cancelled "
                    "event could not be resumed; stable event definitions "
                    "were preserved for the next wait_event.",
                    force_warning=True,
                )
                if wait_control is not None:
                    wait_control.mark_phase("connection_closed_auto_resumed")
                return
        if wait_control is not None:
            wait_control.mark_phase("event_auto_resumed")
        logger.info(
            "java_runtime.%s.wait.cancel.event_resumed waiter_id=%s "
            "generation=%s event_kinds=%s request_ids=%s",
            wait_label,
            wait_control.waiter_id if wait_control is not None else "-",
            wait_control.wait_generation if wait_control is not None else "-",
            [event.get("kind") for event in events],
            [event.get("request_id") for event in events],
        )

    def _resume_ignored_suspending_event(
        self,
        jdwp: JDWPClient,
        wait_label: str,
        composite: dict[str, Any],
    ) -> None:
        suspend_policy = int(composite.get("suspend_policy", 0) or 0)
        events = composite.get("events", [])
        event_kinds = [event.get("kind") for event in events]
        request_ids = [event.get("request_id") for event in events]
        if suspend_policy == SuspendPolicy.NONE:
            logger.debug(
                "java_runtime.%s.wait.ignored_event event_kinds=%s request_ids=%s suspend_policy=%s",
                wait_label, event_kinds, request_ids, suspend_policy,
            )
            return

        logger.warning(
            "java_runtime.%s.wait.ignored_suspending_event_resume "
            "suspend_policy=%s event_kinds=%s request_ids=%s "
            "active_breakpoints=%s active_exceptions=%s",
            wait_label,
            suspend_policy,
            event_kinds,
            request_ids,
            sorted(self._breakpoints),
            sorted(self._exceptions),
        )
        if suspend_policy == SuspendPolicy.EVENT_THREAD:
            thread_ids = sorted({
                int(event.get("thread_id", 0) or 0)
                for event in events
                if int(event.get("thread_id", 0) or 0) > 0
            })
            if thread_ids:
                for thread_id in thread_ids:
                    err, _ = jdwp.command(
                        Cmd.THREAD, 3, jdwp.ids.pack_obj(thread_id)
                    )
                    if err:
                        raise JDWPError(
                            err,
                            "Thread resume after ignored stale event failed",
                        )
                return

        err, _ = jdwp.command(Cmd.VM, 9)
        if err:
            raise JDWPError(err, "VM resume after ignored stale event failed")

    def _resume_snapshot(
        self,
        jdwp: JDWPClient,
        snapshot: SuspensionSnapshot,
    ) -> tuple[int, str]:
        if snapshot.suspend_policy == SuspendPolicy.NONE:
            return 0, "none"
        if snapshot.suspend_policy == SuspendPolicy.EVENT_THREAD:
            thread_ids = snapshot.suspended_thread_ids or (snapshot.thread_id,)
            for thread_id in thread_ids:
                err, _ = jdwp.command(
                    Cmd.THREAD, 3, jdwp.ids.pack_obj(thread_id)
                )
                if err:
                    return err, "event_thread"
            return 0, "event_thread"
        err, _ = jdwp.command(Cmd.VM, 9)
        return err, "vm"

    def _suspend_policy_name(self, suspend_policy: int) -> str:
        if suspend_policy == SuspendPolicy.NONE:
            return "NONE"
        if suspend_policy == SuspendPolicy.EVENT_THREAD:
            return "EVENT_THREAD"
        if suspend_policy == SuspendPolicy.ALL:
            return "SUSPEND_ALL"
        return f"UNKNOWN_{suspend_policy}"

    def _reset_debug_state(self) -> None:
        self._disconnect()
        self._breakpoints.clear()
        self._exceptions.clear()
        self._armed_breakpoint_requests.clear()
        self._armed_exception_requests.clear()
        self._breakpoint_counter = 0
        self._exception_counter = 0
        self._invalidate_suspension()
        self._debug_connection_dirty = False
        self._debug_connection_warning = ""
        self._reset_runtime_overlay()

    def _reset_runtime_overlay(self) -> None:
        self._code_revision = 0
        self._runtime_overlay_sources.clear()
        self._runtime_overlay_state = "none"
        self._runtime_overlay_class_hashes.clear()
        self._runtime_overlay_source_classes.clear()

    def _runtime_overlay_snapshot(self) -> dict[str, Any]:
        active = self._runtime_overlay_state in {"active", "unknown"}
        with self._project_state_lock:
            committed = any(
                session.generations.current is not None
                and session.generations.current.ordinal > 1
                for session in self._project_sessions.values()
            )
        snapshot = {
            "runtime_overlay_active": active,
            "runtime_overlay_state": self._runtime_overlay_state,
            "code_revision": self._code_revision,
            "overlay_sources": sorted(self._runtime_overlay_sources),
            "restart_will_discard_overlay": active and not committed,
            "verification_state": (
                "unknown"
                if self._runtime_overlay_state == "unknown"
                else "not_verified"
                if active
                else "not_applicable"
            ),
        }
        if self._runtime_overlay_state == "unknown":
            snapshot["restart_required"] = True
        return snapshot

    def _invalidate_suspension(self) -> None:
        if self._active_suspension is not None:
            logger.info(
                "java_runtime.suspension.invalidated suspension=%s generation=%s",
                self._active_suspension.suspension_id,
                self._active_suspension.generation,
            )
            self._active_suspension.valid = False
        self._active_suspension = None

    def _require_suspension(self, action: RuntimeAction) -> SuspensionSnapshot:
        snapshot = self._active_suspension
        if snapshot is None or not snapshot.valid:
            raise RuntimeError(
                "No active debug suspension. Call wait_event or wait_breakpoint "
                "after triggering the debug event."
            )
        if action.suspension_id and action.suspension_id != snapshot.suspension_id:
            raise RuntimeError(
                f"Stale suspension_id '{action.suspension_id}'. "
                f"The active suspension is '{snapshot.suspension_id}'."
            )
        if not self._proc.is_running:
            self._invalidate_suspension()
            raise RuntimeError("Target process exited; the suspension is no longer valid")
        return snapshot

    def _snapshot_context(self, snapshot: SuspensionSnapshot) -> dict[str, Any]:
        context: dict[str, Any] = {
            "suspension_id": snapshot.suspension_id,
            "generation": snapshot.generation,
            "thread_id": snapshot.thread_id,
            "observed_at": snapshot.observed_at,
            "created_at": snapshot.created_at or snapshot.observed_at,
            "valid_while_suspended": snapshot.valid and not snapshot.resumed,
            "process_state": "running",
            "debug_state": "suspended",
            "event_kind": snapshot.event_kind,
            "event_type": snapshot.event_type,
            "suspend_policy": snapshot.suspend_policy,
            "suspend_policy_name": self._suspend_policy_name(snapshot.suspend_policy),
            "resumed": snapshot.resumed,
            "jdwp": {"request_id": snapshot.jdwp_request_id},
        }
        if snapshot.breakpoint_id:
            context["breakpoint_id"] = snapshot.breakpoint_id
        if snapshot.request_id:
            context["request_id"] = snapshot.request_id
        return context

    @staticmethod
    def _suggested_suspension_actions(
        snapshot: SuspensionSnapshot,
    ) -> dict[str, dict[str, Any]]:
        """Return copyable calls bound to the exact active suspension."""
        suspension_id = snapshot.suspension_id
        return {
            "variables": {
                "action": "variables",
                "suspension_id": suspension_id,
                "frame_index": 0,
            },
            "stack": {
                "action": "stack",
                "suspension_id": suspension_id,
            },
            "resume": {
                "action": "resume",
                "suspension_id": suspension_id,
            },
        }

    def _variable_observation(self, variable: Variable) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": variable.name,
            "type": variable.type_name,
            "slot": variable.slot,
        }
        if variable.value_observed:
            result["value_state"] = self._observed_value_state(variable.value)
            result["value"] = variable.value
        else:
            result["value_state"] = "unavailable"
            result["error"] = variable.error or "Variable value was not returned by the JVM"
        return result

    def _observed_value_state(self, value: Any) -> str:
        if isinstance(value, dict):
            state = value.get("value_state")
            if state in {"observed", "partial", "unavailable"}:
                return state
        return "observed"

    def _next_breakpoint_id(self) -> str:
        self._breakpoint_counter += 1
        return f"bp_{self._breakpoint_counter:03d}"

    def _next_exception_request_id(self) -> int:
        self._exception_counter = max(
            self._exception_counter,
            max(self._exceptions, default=0),
        ) + 1
        return self._exception_counter

    def _breakpoint_observations(self) -> list[dict[str, Any]]:
        return [
            self._breakpoint_observation(breakpoint_id, breakpoint)
            for breakpoint_id, breakpoint in sorted(self._breakpoints.items())
        ]

    def _breakpoint_observation(
        self,
        breakpoint_id: str,
        breakpoint: dict[str, Any],
    ) -> dict[str, Any]:
        observation = {
            "breakpoint_id": breakpoint.get("breakpoint_id", breakpoint_id),
            "class": breakpoint.get("class", ""),
            "matched_class": breakpoint.get("matched_class", breakpoint.get("class", "")),
            "source_file": breakpoint.get("source_file"),
            "is_proxy": breakpoint.get("is_proxy", False),
            "method": breakpoint.get("method", ""),
            "line": breakpoint.get("line", 0),
            "jdwp": {
                "suspend_policy": self._suspend_policy_name(
                    int(breakpoint.get("suspend_policy", SuspendPolicy.EVENT_THREAD))
                ),
                "armed": False,
            },
        }
        if breakpoint.get("stale"):
            observation["stale"] = True
            observation["stale_reason"] = breakpoint.get(
                "stale_reason",
                _BREAKPOINT_STALE_AFTER_REDEFINE,
            )
        return observation

    def _breakpoint_selector(self, action: RuntimeAction) -> dict[str, Any]:
        selector: dict[str, Any] = {}
        if action.breakpoint_id:
            selector["breakpoint_id"] = action.breakpoint_id
        if action.class_pattern:
            selector["class_pattern"] = action.class_pattern
        if action.line:
            selector["line"] = action.line
        if not selector:
            selector["all"] = True
        return selector

    def _breakpoint_remove_targets(self, action: RuntimeAction) -> list[str]:
        if action.breakpoint_id:
            return [action.breakpoint_id] if action.breakpoint_id in self._breakpoints else []
        if not action.class_pattern and not action.line:
            return list(self._breakpoints)

        class_pattern = action.class_pattern.lower()
        targets: list[str] = []
        for breakpoint_id, breakpoint in self._breakpoints.items():
            class_matches = (
                not class_pattern
                or class_pattern in str(breakpoint.get("class", "")).lower()
            )
            line_matches = not action.line or breakpoint.get("line") == action.line
            if class_matches and line_matches:
                targets.append(breakpoint_id)
        return targets

    def _breakpoint_class_name(self, signature: str) -> str:
        return self._java_class_name(signature)

    def _breakpoint_simple_name(self, class_name: str) -> str:
        return class_name.rsplit(".", 1)[-1].split("$", 1)[0]

    def _normalize_class_pattern(self, class_pattern: str) -> str:
        raw = (class_pattern or "").strip()
        if raw.startswith("L") and raw.endswith(";"):
            raw = raw[1:-1]
        return raw.replace("/", ".").strip(".")

    def _breakpoint_match_type(
        self,
        signature: str,
        class_pattern: str,
    ) -> tuple[str, int] | None:
        raw_pattern = (class_pattern or "").strip()
        if not raw_pattern:
            return None
        class_name = self._breakpoint_class_name(signature)
        simple_name = self._breakpoint_simple_name(class_name)
        normalized_pattern = self._normalize_class_pattern(raw_pattern)
        normalized_signature = signature.replace("/", ".")

        if normalized_pattern == class_name:
            return "fully_qualified_exact", 10
        if raw_pattern == signature or normalized_signature == raw_pattern:
            return "signature_exact", 20
        if normalized_pattern == simple_name:
            return "simple_name_exact", 30
        if "." in normalized_pattern and class_name.endswith(f".{normalized_pattern}"):
            return "suffix_package_match", 40
        if (
            normalized_pattern.lower() in class_name.lower()
            or raw_pattern.lower() in signature.lower()
        ):
            return "fuzzy_contains", 50
        return None

    def _source_file_for_class(
        self,
        jdwp: JDWPClient,
        ids,
        class_id: int,
    ) -> tuple[str | None, str | None]:
        err, data = jdwp.command(Cmd.REF_TYPE, 7, ids.pack_ref(class_id))
        if err:
            return None, f"ReferenceType.SourceFile failed (err {err})"
        if len(data) < 4:
            return None, "ReferenceType.SourceFile reply too short"
        length = struct.unpack_from(">I", data, 0)[0]
        if len(data) < 4 + length:
            return None, "ReferenceType.SourceFile reply truncated"
        return data[4:4 + length].decode("utf-8", errors="replace"), None

    def _proxy_type_for_signature(self, signature: str) -> str | None:
        lowered = signature.lower()
        if "$$springcglib$$" in lowered:
            return "spring_cglib"
        if "$$enhancerbyspringcglib$$" in lowered:
            return "spring_cglib_enhancer"
        if "$$fastclassbyspringcglib$$" in lowered:
            return "spring_cglib_fastclass"
        if "$$enhancerbycglib$$" in lowered:
            return "cglib_enhancer"
        if "$$fastclassbycglib$$" in lowered:
            return "cglib_fastclass"
        if "$proxy" in lowered or "jdk/proxy" in lowered or "jdk.proxy" in lowered:
            return "jdk_proxy"
        if "com/sun/proxy" in lowered or "com.sun.proxy" in lowered:
            return "jdk_proxy"
        if "bytebuddy" in lowered:
            return "byte_buddy"
        if "hibernateproxy" in lowered:
            return "hibernate_proxy"
        if "javassist" in lowered:
            return "javassist"
        return None

    def _resolve_breakpoint_class(
        self,
        jdwp: JDWPClient,
        action: RuntimeAction,
    ) -> tuple[BreakpointClassCandidate | None, RuntimeResult | None, list[dict[str, Any]]]:
        ids = jdwp.ids
        err, data = jdwp.command(Cmd.VM, 3)  # AllClasses
        if err:
            return None, RuntimeResult(
                ok=False,
                error=f"AllClasses failed (err {err})",
                data={
                    "error_code": "ALL_CLASSES_FAILED",
                    "retryable": True,
                    "suggested_next_step": "Retry after the JVM is reachable, or restart/attach again.",
                },
            ), []

        count = struct.unpack_from(">I", data, 0)[0]
        offset = 4
        candidates: list[BreakpointClassCandidate] = []
        ignored_proxy_candidates: list[dict[str, Any]] = []
        ignored_generated_candidates: list[dict[str, Any]] = []
        for _ in range(count):
            tag = data[offset]
            offset += 1
            cid = int.from_bytes(data[offset:offset + ids.reference_type_id_size], "big")
            offset += ids.reference_type_id_size
            slen = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            sig = data[offset:offset + slen].decode("utf-8", errors="replace")
            offset += slen
            offset += 4  # status
            match = self._breakpoint_match_type(sig, action.class_pattern)
            if match is None:
                continue
            match_type, match_rank = match
            class_name = self._breakpoint_class_name(sig)
            proxy_type = self._proxy_type_for_signature(sig)
            is_proxy = proxy_type is not None
            is_generated = self._is_generated_signature(sig)
            source_file, source_warning = self._source_file_for_class(jdwp, ids, cid)
            warning_list = [source_warning] if source_warning else []
            observation = {
                "class": class_name,
                "signature": sig,
                "source_file": source_file,
                "proxy_type": proxy_type,
                "match_type": match_type,
            }
            if is_proxy and not action.include_proxy:
                ignored_proxy_candidates.append(observation)
                continue
            if is_generated and not action.include_generated:
                ignored_generated_candidates.append(observation)
                continue
            candidates.append(BreakpointClassCandidate(
                type_tag=tag,
                class_id=cid,
                signature=sig,
                name=class_name,
                simple_name=self._breakpoint_simple_name(class_name),
                source_file=source_file,
                is_proxy=is_proxy,
                proxy_type=proxy_type,
                is_generated=is_generated,
                match_type=match_type,
                match_rank=match_rank,
                warnings=warning_list,
            ))

        if not candidates:
            if ignored_proxy_candidates and not ignored_generated_candidates:
                return None, RuntimeResult(
                    ok=False,
                    error="ONLY_PROXY_CLASSES_MATCHED",
                    data={
                        "error_code": "ONLY_PROXY_CLASSES_MATCHED",
                        "class_pattern": action.class_pattern,
                        "matched_proxy_classes": ignored_proxy_candidates[:10],
                        "retryable": False,
                        "suggested_next_step": (
                            "Use the concrete application class pattern, or set include_proxy=true "
                            "only if you intentionally want to debug the proxy class."
                        ),
                    },
                ), ignored_proxy_candidates
            return None, RuntimeResult(
                ok=False,
                error=f"Class matching '{action.class_pattern}' not found",
                data={
                    "error_code": "CLASS_NOT_FOUND",
                    "class_pattern": action.class_pattern,
                    "ignored_proxy_candidates": ignored_proxy_candidates[:10],
                    "ignored_generated_candidates": ignored_generated_candidates[:10],
                    "retryable": True,
                    "suggested_next_step": (
                        "Check class_pattern, trigger the code path so the class loads, "
                        "or rebuild/restart the target if source and bytecode diverge."
                    ),
                },
            ), ignored_proxy_candidates

        best_rank = min(candidate.match_rank for candidate in candidates)
        best = [candidate for candidate in candidates if candidate.match_rank == best_rank]
        if len(best) > 1:
            return None, RuntimeResult(
                ok=False,
                error="AMBIGUOUS_CLASS_MATCH",
                data={
                    "error_code": "AMBIGUOUS_CLASS_MATCH",
                    "class_pattern": action.class_pattern,
                    "candidates": [
                        self._breakpoint_candidate_observation(candidate)
                        for candidate in best[:10]
                    ],
                    "retryable": False,
                    "suggested_next_step": (
                        "Use a more specific class_pattern, preferably a fully qualified "
                        "class name such as com.example.service.FooService."
                    ),
                },
            ), ignored_proxy_candidates
        return best[0], None, ignored_proxy_candidates

    def _breakpoint_candidate_observation(
        self,
        candidate: BreakpointClassCandidate,
    ) -> dict[str, Any]:
        return {
            "class": candidate.name,
            "signature": candidate.signature,
            "source_file": candidate.source_file,
            "is_proxy": candidate.is_proxy,
            "proxy_type": candidate.proxy_type,
            "match_type": candidate.match_type,
        }

    def _methods_for_class(
        self,
        jdwp: JDWPClient,
        candidate: BreakpointClassCandidate,
    ) -> tuple[list[tuple[int, str, str]], RuntimeResult | None]:
        ids = jdwp.ids
        err, data = jdwp.command(Cmd.REF_TYPE, 5, ids.pack_ref(candidate.class_id))
        if err:
            return [], RuntimeResult(
                ok=False,
                error=f"Methods failed (err {err})",
                data={
                    "error_code": "METHODS_FAILED",
                    "matched_class": candidate.name,
                    "line": 0,
                    "retryable": True,
                    "suggested_next_step": "Retry, or restart/attach to refresh JDWP state.",
                },
            )

        method_count = struct.unpack_from(">I", data, 0)[0]
        offset = 4
        methods = []
        for _ in range(method_count):
            mid = int.from_bytes(data[offset:offset + ids.method_id_size], "big")
            offset += ids.method_id_size
            nlen = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            mname = data[offset:offset + nlen].decode("utf-8", errors="replace")
            offset += nlen
            slen = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            msig = data[offset:offset + slen].decode("utf-8", errors="replace")
            offset += slen
            offset += 4  # modBits
            methods.append((mid, mname, msig))
        return methods, None

    def _line_locations_for_class(
        self,
        jdwp: JDWPClient,
        candidate: BreakpointClassCandidate,
        line: int,
    ) -> tuple[list[BreakpointLocation], list[dict[str, Any]], RuntimeResult | None]:
        ids = jdwp.ids
        methods, error = self._methods_for_class(jdwp, candidate)
        if error is not None:
            return [], [], error

        locations: list[BreakpointLocation] = []
        nearby: list[dict[str, Any]] = []
        for mid, mname, msig in methods:
            err, lt_data = jdwp.command(
                Cmd.METHOD,
                1,
                ids.pack_ref(candidate.class_id) + ids.pack_method(mid),
            )
            if err or len(lt_data) < 20:
                continue
            line_count = struct.unpack_from(">I", lt_data, 16)[0]
            offset = 20
            for _ in range(line_count):
                code_idx = struct.unpack_from(">Q", lt_data, offset)[0]
                offset += 8
                line_num = struct.unpack_from(">I", lt_data, offset)[0]
                offset += 4
                row = {
                    "line": line_num,
                    "method": mname,
                    "code_index": code_idx,
                }
                if line_num == line:
                    locations.append(BreakpointLocation(
                        method_id=mid,
                        method=mname,
                        method_signature=msig,
                        line=line_num,
                        code_index=code_idx,
                    ))
                elif abs(line_num - line) <= 5:
                    nearby.append(row)

        if not locations:
            nearby = sorted(
                nearby,
                key=lambda item: (abs(int(item["line"]) - line), int(item["line"])),
            )[:10]
            return [], nearby, RuntimeResult(
                ok=False,
                error="NO_EXECUTABLE_LOCATION_AT_LINE",
                data={
                    "error_code": "NO_EXECUTABLE_LOCATION_AT_LINE",
                    "matched_class": candidate.name,
                    "source_file": candidate.source_file,
                    "line": line,
                    "nearby_locations": nearby,
                    "possible_causes": [
                        "line is not executable",
                        "source does not match running bytecode",
                    ],
                    "retryable": False,
                    "suggested_next_step": (
                        "Set the breakpoint on a nearby executable line or rebuild/restart the target."
                    ),
                },
            )
        return locations, nearby, None

    def _class_match_skip_reason(
        self,
        signature: str,
        action: RuntimeAction,
    ) -> str:
        if self._is_proxy_signature(signature) and not action.include_proxy:
            return "proxy_class_excluded"
        if self._is_generated_signature(signature) and not action.include_generated:
            return "generated_class_excluded"
        return ""

    def _is_proxy_signature(self, signature: str) -> bool:
        return self._proxy_type_for_signature(signature) is not None

    def _is_generated_signature(self, signature: str) -> bool:
        lowered = signature.lower()
        markers = (
            "$$lambda$",
            "$lambda$",
            "$generated",
            "/generated/",
            "/generatedsources/",
            "/generated-sources/",
        )
        return any(marker in lowered for marker in markers)

    def _exception_observations(self) -> list[dict[str, Any]]:
        return [
            self._exception_observation(request_id, exception)
            for request_id, exception in sorted(self._exceptions.items())
        ]

    def _exception_observation(
        self,
        request_id: int,
        exception: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "request_id": request_id,
            "exception_class": exception.get("exception_class", ""),
            "caught": exception.get("caught", False),
            "uncaught": exception.get("uncaught", False),
        }

    def _exception_selector(self, action: RuntimeAction) -> dict[str, Any]:
        selector: dict[str, Any] = {}
        if action.request_id:
            selector["request_id"] = action.request_id
        if action.exception_class:
            normalized, error = self._normalize_exception_signature(action.exception_class)
            selector["exception_class"] = action.exception_class if error else normalized
        if not selector:
            selector["all"] = True
        return selector

    def _exception_remove_targets(self, action: RuntimeAction) -> list[int]:
        if action.request_id:
            return [action.request_id] if action.request_id in self._exceptions else []
        if not action.exception_class:
            return list(self._exceptions)

        normalized, error = self._normalize_exception_signature(action.exception_class)
        if error:
            return []
        return [
            request_id
            for request_id, exception in self._exceptions.items()
            if exception.get("exception_class") == normalized
        ]

    def _validated_exception_signature(
        self,
        action: RuntimeAction,
    ) -> tuple[str, str]:
        normalized, error = self._normalize_exception_signature(action.exception_class)
        if error:
            return "", error
        if not action.caught and not action.uncaught:
            return "", "At least one of caught or uncaught must be true"
        if (
            action.caught
            and normalized in self._BROAD_EXCEPTION_SIGNATURES
            and not action.allow_broad_caught
        ):
            return "", (
                f"Refusing broad caught exception watch for {normalized}; "
                "use a specific exception class or set allow_broad_caught=true."
            )
        return normalized, ""

    def _normalize_exception_signature(self, exception_class: str) -> tuple[str, str]:
        raw = (exception_class or "").strip()
        if not raw:
            return "", "exception_class is required"

        candidate = raw
        if candidate.startswith("L"):
            candidate = candidate[1:]
        if candidate.endswith(";"):
            candidate = candidate[:-1]
        candidate = candidate.replace(".", "/").strip("/")

        if "/" not in candidate:
            if candidate in self._JAVA_LANG_SIMPLE_EXCEPTIONS:
                candidate = f"java/lang/{candidate}"
            else:
                return "", (
                    f"Exception class '{raw}' is not fully qualified; use a name like "
                    "java.lang.NullPointerException"
                )

        if not candidate:
            return "", "exception_class is required"
        return f"L{candidate};", ""

    def _value_depth(self, requested_depth: int) -> int:
        try:
            depth = int(requested_depth)
        except (TypeError, ValueError):
            depth = 1
        return max(0, min(depth, self._max_value_depth))

    def _collection_item_limit(self, requested_limit: int) -> int:
        try:
            limit = int(requested_limit)
        except (TypeError, ValueError):
            limit = 16
        return max(0, min(limit, self._max_array_elements))

    def _all_thread_ids(self, jdwp: JDWPClient) -> list[int]:
        ids = jdwp.ids
        err, data = jdwp.command(Cmd.VM, 4)
        if err:
            raise JDWPError(err, "VirtualMachine/AllThreads failed")
        count = struct.unpack_from(">I", data, 0)[0]
        offset = 4
        thread_ids = []
        for _ in range(count):
            thread_ids.append(int.from_bytes(
                data[offset:offset + ids.object_id_size], "big"
            ))
            offset += ids.object_id_size
        return thread_ids

    def _thread_name(self, jdwp: JDWPClient, thread_id: int) -> str:
        err, data = jdwp.command(Cmd.THREAD, 1, jdwp.ids.pack_obj(thread_id))
        if err or len(data) < 4:
            return "<unknown>"
        length = struct.unpack_from(">I", data, 0)[0]
        return data[4:4 + length].decode("utf-8", errors="replace")

    def _resolve_thread_id(
        self,
        jdwp: JDWPClient,
        snapshot: SuspensionSnapshot,
        thread_name: str,
    ) -> int:
        if not thread_name:
            return snapshot.thread_id

        requested = thread_name.strip()
        if not requested:
            return snapshot.thread_id
        requested_lower = requested.lower()
        threads = [
            (thread_id, self._thread_name(jdwp, thread_id))
            for thread_id in self._all_thread_ids(jdwp)
        ]
        ranked_matches = (
            [
                (thread_id, name)
                for thread_id, name in threads
                if name == requested
            ],
            [
                (thread_id, name)
                for thread_id, name in threads
                if name.lower() == requested_lower
            ],
            [
                (thread_id, name)
                for thread_id, name in threads
                if name.lower().startswith(requested_lower)
            ],
            [
                (thread_id, name)
                for thread_id, name in threads
                if requested_lower in name.lower()
            ],
        )
        matches: list[tuple[int, str]] = []
        for candidates in ranked_matches:
            if candidates:
                matches = candidates
                break

        if not matches:
            raise RuntimeError(f"Thread matching '{thread_name}' not found")
        if len(matches) > 1:
            names = [name for _, name in matches[:5]]
            raise RuntimeError(
                f"Thread name '{thread_name}' is ambiguous; matches: {names}"
            )
        return matches[0][0]

    def _read_frames(
        self,
        jdwp: JDWPClient,
        thread_id: int,
        max_frames: int,
        *,
        start_index: int = 0,
    ) -> list[dict[str, Any]]:
        ids = jdwp.ids
        thread_bytes = ids.pack_obj(thread_id)
        err, count_data = jdwp.command(Cmd.THREAD, 7, thread_bytes)
        if err:
            raise JDWPError(err, "ThreadReference/FrameCount failed")
        total_frames = struct.unpack_from(">I", count_data, 0)[0]
        if start_index >= total_frames:
            return []
        requested_frames = min(max(1, max_frames), total_frames - start_index)
        payload = ids.pack_obj(thread_id) + struct.pack(
            ">II", max(start_index, 0), requested_frames
        )
        err, data = jdwp.command(Cmd.THREAD, 6, payload)
        if err:
            raise JDWPError(err, "ThreadReference/Frames failed")
        count = struct.unpack_from(">I", data, 0)[0]
        offset = 4
        frames = []
        for relative_index in range(count):
            frame_id = int.from_bytes(data[offset:offset + ids.frame_id_size], "big")
            offset += ids.frame_id_size
            type_tag = data[offset]
            offset += 1
            class_id = int.from_bytes(
                data[offset:offset + ids.reference_type_id_size], "big"
            )
            offset += ids.reference_type_id_size
            method_id = int.from_bytes(
                data[offset:offset + ids.method_id_size], "big"
            )
            offset += ids.method_id_size
            location_index = struct.unpack_from(">Q", data, offset)[0]
            offset += 8
            is_native = location_index == 0xFFFFFFFFFFFFFFFF
            frames.append({
                "index": start_index + relative_index,
                "frame_id": frame_id,
                "type_tag": type_tag,
                "class_id": class_id,
                "method_id": method_id,
                "location_index": location_index,
                "class": self._class_signature(jdwp, class_id),
                "method": self._method_name(jdwp, class_id, method_id),
                "line": None if is_native else self._source_line_for_location(
                    jdwp, ids, class_id, method_id, location_index
                ),
                "is_native": is_native,
            })
        return frames

    def _public_frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        return {
            "index": frame["index"],
            "class": frame["class"],
            "method": frame["method"],
            "line": frame["line"],
            "is_native": frame["is_native"],
        }

    def _class_signature(self, jdwp: JDWPClient, class_id: int) -> str:
        err, data = jdwp.command(Cmd.REF_TYPE, 1, jdwp.ids.pack_ref(class_id))
        if err or len(data) < 4:
            return "unknown"
        length = struct.unpack_from(">I", data, 0)[0]
        return data[4:4 + length].decode("utf-8", errors="replace")

    def _find_loaded_class_by_signature(
        self,
        jdwp: JDWPClient,
        signature: str,
    ) -> tuple[int, int, str] | None:
        ids = jdwp.ids
        err, data = jdwp.command(Cmd.VM, 3)  # AllClasses
        if err:
            raise JDWPError(err, "AllClasses failed")

        count = struct.unpack_from(">I", data, 0)[0]
        offset = 4
        for _ in range(count):
            type_tag = data[offset]
            offset += 1
            class_id = int.from_bytes(
                data[offset:offset + ids.reference_type_id_size], "big"
            )
            offset += ids.reference_type_id_size
            sig_len = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            loaded_signature = data[offset:offset + sig_len].decode(
                "utf-8", errors="replace"
            )
            offset += sig_len
            offset += 4  # status
            if loaded_signature == signature:
                return type_tag, class_id, loaded_signature
        return None

    def _object_class_signature(self, jdwp: JDWPClient, obj_id: int) -> str:
        if not obj_id:
            return "unknown"
        err, data = jdwp.command(Cmd.OBJ_REF, 1, jdwp.ids.pack_obj(obj_id))
        if err or len(data) < 1 + jdwp.ids.reference_type_id_size:
            return "unknown"
        ref_type_id = int.from_bytes(
            data[1:1 + jdwp.ids.reference_type_id_size], "big"
        )
        return self._class_signature(jdwp, ref_type_id)

    def _method_name(self, jdwp: JDWPClient, class_id: int, method_id: int) -> str:
        ids = jdwp.ids
        err, data = jdwp.command(Cmd.REF_TYPE, 5, ids.pack_ref(class_id))
        if err or len(data) < 4:
            return "unknown"
        count = struct.unpack_from(">I", data, 0)[0]
        offset = 4
        for _ in range(count):
            current_id = int.from_bytes(data[offset:offset + ids.method_id_size], "big")
            offset += ids.method_id_size
            name_length = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            name = data[offset:offset + name_length].decode("utf-8", errors="replace")
            offset += name_length
            signature_length = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            signature = data[offset:offset + signature_length].decode(
                "utf-8", errors="replace"
            )
            offset += signature_length + 4
            if current_id == method_id:
                return f"{name}{signature}"
        return "unknown"

    def _describe_location(
        self, jdwp: JDWPClient, location: dict[str, int]
    ) -> dict[str, Any]:
        class_id = int(location.get("class_id", 0))
        method_id = int(location.get("method_id", 0))
        index = int(location.get("index", 0))
        return {
            "class": self._class_signature(jdwp, class_id),
            "method": self._method_name(jdwp, class_id, method_id),
            "line": self._source_line_for_location(
                jdwp, jdwp.ids, class_id, method_id, index
            ),
        }

    def _is_empty_location(self, location: dict[str, int]) -> bool:
        return (
            int(location.get("type_tag", 0) or 0) == 0
            and int(location.get("class_id", 0) or 0) == 0
            and int(location.get("method_id", 0) or 0) == 0
            and int(location.get("index", 0) or 0) == 0
        )

    def _thread_status_name(self, status: int) -> str:
        return {
            0: "zombie",
            1: "running",
            2: "sleeping",
            3: "monitor",
            4: "waiting",
        }.get(status, "unknown")

    def _source_line_for_location(
        self,
        jdwp: JDWPClient,
        ids,
        class_id: int,
        method_id: int,
        location_index: int,
    ) -> int | None:
        """Map a JDWP location index back to the closest source line."""
        err, data = jdwp.command(
            Cmd.METHOD,
            1,
            ids.pack_ref(class_id) + ids.pack_method(method_id),
        )
        if err:
            return None

        line_count = struct.unpack_from(">I", data, 16)[0]
        offset = 20
        first_line = None
        resolved_line = None

        for _ in range(line_count):
            code_idx = struct.unpack_from(">Q", data, offset)[0]
            offset += 8
            line_num = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            if first_line is None:
                first_line = line_num
            if code_idx > location_index:
                break
            resolved_line = line_num

        return resolved_line if resolved_line is not None else first_line

    def _visible_variables_for_location(self, variable_table: bytes, location_index: int) -> list[Variable]:
        """Return only the variables visible at the current frame location."""
        slot_count = struct.unpack_from(">I", variable_table, 4)[0]
        offset = 8
        entries: list[dict[str, int | str]] = []

        for order in range(slot_count):
            code_index = struct.unpack_from(">Q", variable_table, offset)[0]
            offset += 8
            nlen = struct.unpack_from(">I", variable_table, offset)[0]
            offset += 4
            vname = variable_table[offset:offset+nlen].decode("utf-8")
            offset += nlen
            slen = struct.unpack_from(">I", variable_table, offset)[0]
            offset += 4
            vsig = variable_table[offset:offset+slen].decode("utf-8")
            offset += slen
            scope_length = struct.unpack_from(">I", variable_table, offset)[0]
            offset += 4
            slot = struct.unpack_from(">I", variable_table, offset)[0]
            offset += 4
            entries.append({
                "order": order,
                "code_index": code_index,
                "scope_length": scope_length,
                "name": vname,
                "type_name": vsig,
                "slot": slot,
            })

        visible_by_slot: dict[int, dict[str, int | str]] = {}
        for entry in entries:
            scope_start = int(entry["code_index"])
            scope_length = int(entry["scope_length"])
            if not self._is_variable_visible(scope_start, scope_length, location_index):
                continue

            slot = int(entry["slot"])
            prev = visible_by_slot.get(slot)
            if prev is None or int(entry["code_index"]) >= int(prev["code_index"]):
                visible_by_slot[slot] = entry

        visible_entries = sorted(
            visible_by_slot.values(),
            key=lambda entry: int(entry["order"]),
        )
        return [
            Variable(
                name=str(entry["name"]),
                type_name=str(entry["type_name"]),
                slot=int(entry["slot"]),
                value=None,
            )
            for entry in visible_entries
        ]

    def _is_variable_visible(self, scope_start: int, scope_length: int, location_index: int) -> bool:
        if scope_length <= 0:
            return location_index >= scope_start
        return scope_start <= location_index < scope_start + scope_length

    def _reference_value(self, obj_id: int, kind: str) -> dict[str, str]:
        return {"_ref": f"0x{obj_id:x}", "_kind": kind}

    def _expanded_value_base(self, obj_id: int, kind: str) -> dict[str, str]:
        return self._reference_value(obj_id, kind)

    def _object_error_value(
        self,
        obj_id: int,
        *,
        error: str,
        class_name: str | None = None,
    ) -> dict[str, str]:
        result = self._reference_value(obj_id, "object")
        result["_error"] = error
        if class_name is not None:
            result["_class"] = class_name
        return result

    def _java_class_name(self, signature: str) -> str:
        if not signature:
            return ""
        if signature.startswith("["):
            return signature.replace("/", ".")
        if signature.startswith("L") and signature.endswith(";"):
            return signature[1:-1].replace("/", ".")
        return signature.replace("/", ".")

    def _semantic_value_base(
        self,
        obj_id: int,
        kind: str,
        signature: str,
    ) -> dict[str, Any]:
        result: dict[str, Any] = self._expanded_value_base(obj_id, kind)
        result["_class"] = self._java_class_name(signature)
        return result

    def _mark_value_state(
        self,
        result: dict[str, Any],
        state: str,
        error: str,
    ) -> dict[str, Any]:
        result["value_state"] = state
        result["error"] = error
        return result

    def _object_reference_type(
        self,
        jdwp: JDWPClient,
        ids,
        obj_id: int,
    ) -> tuple[int, int, str]:
        err, rt_data = jdwp.command(Cmd.OBJ_REF, 1, ids.pack_obj(obj_id))
        if err:
            return 0, 0, f"ObjectReference.ReferenceType failed (err {err})"
        expected_len = 1 + ids.reference_type_id_size
        if len(rt_data) < expected_len:
            return 0, 0, "ObjectReference.ReferenceType reply too short"
        type_tag = rt_data[0]
        ref_type_id = int.from_bytes(
            rt_data[1:1 + ids.reference_type_id_size],
            "big",
        )
        return type_tag, ref_type_id, ""

    def _reference_type_signature(
        self,
        jdwp: JDWPClient,
        ids,
        ref_type_id: int,
    ) -> tuple[str, str]:
        err, sig_data = jdwp.command(Cmd.REF_TYPE, 1, ids.pack_ref(ref_type_id))
        if err:
            return "", f"ReferenceType.Signature failed (err {err})"
        if len(sig_data) < 4:
            return "", "ReferenceType.Signature reply too short"
        slen = struct.unpack_from(">I", sig_data, 0)[0]
        if len(sig_data) < 4 + slen:
            return "", "ReferenceType.Signature reply truncated"
        return sig_data[4:4 + slen].decode("utf-8"), ""

    def _object_signature(
        self,
        jdwp: JDWPClient,
        ids,
        obj_id: int,
    ) -> tuple[int, str, str]:
        _type_tag, ref_type_id, error = self._object_reference_type(jdwp, ids, obj_id)
        if error:
            return 0, "", error
        signature, error = self._reference_type_signature(jdwp, ids, ref_type_id)
        if error:
            return ref_type_id, "", error
        return ref_type_id, signature, ""

    def _declared_instance_fields(
        self,
        jdwp: JDWPClient,
        ids,
        ref_type_id: int,
    ) -> tuple[list[dict[str, Any]], str]:
        err, f_data = jdwp.command(Cmd.REF_TYPE, 4, ids.pack_ref(ref_type_id))
        if err:
            return [], f"ReferenceType.Fields failed (err {err})"
        if len(f_data) < 4:
            return [], "ReferenceType.Fields reply too short"

        field_count = struct.unpack_from(">I", f_data, 0)[0]
        offset = 4
        fields: list[dict[str, Any]] = []
        try:
            for _ in range(field_count):
                fid = int.from_bytes(
                    f_data[offset:offset + ids.field_id_size],
                    "big",
                )
                offset += ids.field_id_size
                nlen = struct.unpack_from(">I", f_data, offset)[0]
                offset += 4
                fname = f_data[offset:offset + nlen].decode("utf-8")
                offset += nlen
                slen = struct.unpack_from(">I", f_data, offset)[0]
                offset += 4
                fsig = f_data[offset:offset + slen].decode("utf-8")
                offset += slen
                mod_bits = struct.unpack_from(">I", f_data, offset)[0]
                offset += 4
                if self._is_instance_field(mod_bits):
                    fields.append({
                        "id": fid,
                        "name": fname,
                        "signature": fsig,
                        "declaring_type_id": ref_type_id,
                    })
        except (IndexError, struct.error, UnicodeDecodeError) as exc:
            return fields, f"ReferenceType.Fields decode failed: {exc}"
        return fields, ""

    def _superclass_id(self, jdwp: JDWPClient, ids, ref_type_id: int) -> int:
        err, data = jdwp.command(Cmd.CLASS_TYPE, 1, ids.pack_ref(ref_type_id))
        if err or len(data) < ids.reference_type_id_size:
            return 0
        return int.from_bytes(data[:ids.reference_type_id_size], "big")

    def _instance_field_lookup(
        self,
        jdwp: JDWPClient,
        ids,
        ref_type_id: int,
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        lookup: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        seen: set[int] = set()
        current = ref_type_id
        while current and current not in seen:
            seen.add(current)
            fields, error = self._declared_instance_fields(jdwp, ids, current)
            if error:
                errors.append(error)
            for field in fields:
                lookup.setdefault(field["name"], field)
            current = self._superclass_id(jdwp, ids, current)
        return lookup, errors

    def _read_raw_tagged_value(
        self,
        ids,
        tag: int,
        data: bytes,
        offset: int,
    ) -> tuple[dict[str, Any], int]:
        if tag == Tag.DOUBLE:
            return {"tag": tag, "value": struct.unpack_from(">d", data, offset)[0]}, offset + 8
        if tag == Tag.FLOAT:
            return {"tag": tag, "value": struct.unpack_from(">f", data, offset)[0]}, offset + 4
        if tag == Tag.LONG:
            return {"tag": tag, "value": struct.unpack_from(">q", data, offset)[0]}, offset + 8
        if tag == Tag.BYTE:
            return {"tag": tag, "value": struct.unpack_from(">b", data, offset)[0]}, offset + 1
        if tag == Tag.BOOLEAN:
            return {"tag": tag, "value": data[offset] != 0}, offset + 1
        if tag == Tag.CHAR:
            return {"tag": tag, "value": chr(struct.unpack_from(">H", data, offset)[0])}, offset + 2
        if tag == Tag.SHORT:
            return {"tag": tag, "value": struct.unpack_from(">h", data, offset)[0]}, offset + 2
        if tag == Tag.INT:
            return {"tag": tag, "value": struct.unpack_from(">i", data, offset)[0]}, offset + 4

        obj_id = int.from_bytes(data[offset:offset + ids.object_id_size], "big")
        return {"tag": tag, "value": obj_id}, offset + ids.object_id_size

    def _expand_raw_tagged_value(
        self,
        jdwp: JDWPClient,
        ids,
        raw: dict[str, Any],
        depth: int,
        visited: set[int],
        *,
        semantic_collections: bool,
        item_limit: int,
        map_entry_limit: int,
    ) -> Any:
        tag = int(raw.get("tag", 0))
        value = raw.get("value")
        if tag not in {
            Tag.ARRAY,
            Tag.OBJECT,
            Tag.STRING,
            Tag.THREAD,
            Tag.THREAD_GROUP,
            Tag.CLASS_LOADER,
            Tag.CLASS_OBJECT,
        }:
            return value
        obj_id = int(value or 0)
        if obj_id == 0:
            return None
        payload = ids.pack_obj(obj_id)
        expanded, _offset = self._read_value(
            jdwp,
            ids,
            tag,
            payload,
            0,
            depth=depth,
            visited=visited,
            semantic_collections=semantic_collections,
            item_limit=item_limit,
            map_entry_limit=map_entry_limit,
        )
        return expanded

    def _read_named_fields_raw(
        self,
        jdwp: JDWPClient,
        ids,
        obj_id: int,
        field_lookup: dict[str, dict[str, Any]],
        names: list[str],
    ) -> tuple[dict[str, dict[str, Any]], str]:
        missing = [name for name in names if name not in field_lookup]
        selected = [field_lookup[name] for name in names if name in field_lookup]
        if not selected:
            return {}, f"Missing required field(s): {', '.join(missing)}"

        payload = ids.pack_obj(obj_id) + struct.pack(">I", len(selected))
        for field in selected:
            payload += ids.pack_field(int(field["id"]))

        err, values_data = jdwp.command(Cmd.OBJ_REF, 2, payload)
        if err:
            return {}, f"ObjectReference.GetValues failed (err {err})"
        if len(values_data) < 4:
            return {}, "ObjectReference.GetValues reply too short"

        result: dict[str, dict[str, Any]] = {}
        value_count = struct.unpack_from(">I", values_data, 0)[0]
        offset = 4
        try:
            for index in range(min(value_count, len(selected))):
                tag = values_data[offset]
                offset += 1
                raw, offset = self._read_raw_tagged_value(
                    ids, tag, values_data, offset
                )
                result[selected[index]["name"]] = raw
        except (IndexError, struct.error) as exc:
            return result, f"ObjectReference.GetValues decode failed: {exc}"

        errors = []
        if missing:
            errors.append(f"Missing field(s): {', '.join(missing)}")
        if value_count < len(selected):
            errors.append(
                f"ObjectReference.GetValues returned {value_count} value(s) "
                f"for {len(selected)} field(s)"
            )
        return result, "; ".join(errors)

    def _read_value(
        self,
        jdwp: JDWPClient,
        ids,
        tag: int,
        data: bytes,
        offset: int,
        depth: int = 3,
        visited: set[int] | None = None,
        semantic_collections: bool = True,
        item_limit: int = 16,
        map_entry_limit: int = 16,
    ):
        """Read a single JDWP tagged value. Returns (value, new_offset).
        depth: remaining object-expansion budget. Arrays and strings do not consume it."""
        if visited is None:
            visited = set()

        if tag == Tag.DOUBLE:
            val = struct.unpack_from(">d", data, offset)[0]
            return val, offset + 8
        if tag == Tag.FLOAT:
            val = struct.unpack_from(">f", data, offset)[0]
            return val, offset + 4
        if tag == Tag.LONG:
            val = struct.unpack_from(">q", data, offset)[0]
            return val, offset + 8
        if tag == Tag.BYTE:
            val = struct.unpack_from(">b", data, offset)[0]
            return val, offset + 1
        if tag == Tag.BOOLEAN:
            return data[offset] != 0, offset + 1
        if tag == Tag.CHAR:
            code_unit = struct.unpack_from(">H", data, offset)[0]
            return chr(code_unit), offset + 2
        if tag == Tag.SHORT:
            val = struct.unpack_from(">h", data, offset)[0]
            return val, offset + 2
        if tag == Tag.INT:
            val = struct.unpack_from(">i", data, offset)[0]
            return val, offset + 4

        # Object / Array / String — need object_id
        obj_id = int.from_bytes(data[offset:offset + ids.object_id_size], "big")
        offset += ids.object_id_size

        if obj_id == 0:
            return None, offset

        if tag == Tag.STRING:
            err, sv = jdwp.command(Cmd.STRING_REF, 1, ids.pack_obj(obj_id))
            if err == 0:
                slen = struct.unpack_from(">I", sv, 0)[0]
                return sv[4:4 + slen].decode("utf-8"), offset
            return f"<String 0x{obj_id:x}>", offset

        if tag == Tag.ARRAY:
            if obj_id in visited:
                return self._reference_value(obj_id, "array"), offset
            visited.add(obj_id)
            return self._read_array(
                jdwp,
                ids,
                obj_id,
                depth,
                visited,
                semantic_collections=semantic_collections,
                item_limit=item_limit,
                map_entry_limit=map_entry_limit,
            ), offset

        if depth <= 0:
            return self._reference_value(obj_id, "object"), offset

        if obj_id in visited:
            return self._reference_value(obj_id, "object"), offset

        visited.add(obj_id)

        # tag == Tag.OBJECT (or anything else)
        return self._read_object(
            jdwp,
            ids,
            obj_id,
            depth - 1,
            visited,
            semantic_collections=semantic_collections,
            item_limit=item_limit,
            map_entry_limit=map_entry_limit,
        ), offset

    def _read_object(
        self,
        jdwp: JDWPClient,
        ids,
        obj_id: int,
        depth: int = 3,
        visited: set[int] | None = None,
        semantic_collections: bool = True,
        item_limit: int = 16,
        map_entry_limit: int = 16,
    ) -> dict[str, Any]:
        """Read an object's class name and field values. Returns a structured dict."""
        try:
            if visited is None:
                visited = set()

            ref_type_id, sig, error = self._object_signature(jdwp, ids, obj_id)
            if error:
                return self._object_error_value(obj_id, error=error)

            if semantic_collections:
                semantic_value = self._read_semantic_collection(
                    jdwp,
                    ids,
                    obj_id,
                    ref_type_id,
                    sig,
                    depth,
                    visited,
                    item_limit=item_limit,
                    map_entry_limit=map_entry_limit,
                )
                if semantic_value is not None:
                    return semantic_value

            # Get fields
            err, f_data = jdwp.command(Cmd.REF_TYPE, 4, ids.pack_ref(ref_type_id))
            if err or struct.unpack_from(">I", f_data, 0)[0] == 0:
                result: dict[str, Any] = self._expanded_value_base(obj_id, "object")
                result["_class"] = sig
                if err:
                    result["_error"] = f"ReferenceType.Fields failed (err {err})"
                return result

            field_count = struct.unpack_from(">I", f_data, 0)[0]
            offset = 4
            fields = []
            for _ in range(field_count):
                fid = int.from_bytes(f_data[offset:offset+ids.field_id_size], "big")
                offset += ids.field_id_size
                nlen = struct.unpack_from(">I", f_data, offset)[0]; offset += 4
                fname = f_data[offset:offset+nlen].decode("utf-8"); offset += nlen
                slen2 = struct.unpack_from(">I", f_data, offset)[0]; offset += 4
                fsig = f_data[offset:offset+slen2].decode("utf-8"); offset += slen2
                mod_bits = struct.unpack_from(">I", f_data, offset)[0]; offset += 4
                if self._is_instance_field(mod_bits):
                    fields.append((fid, fname, fsig))

            if not fields:
                return {"_class": sig}

            # Read field values
            gv_payload = ids.pack_obj(obj_id)
            gv_payload += struct.pack(">I", len(fields))
            for fid, _, _ in fields:
                gv_payload += ids.pack_field(fid)

            err, gv_data = jdwp.command(Cmd.OBJ_REF, 2, gv_payload)
            if err:
                result = self._expanded_value_base(obj_id, "object")
                result["_class"] = sig
                result["_error"] = f"ObjectReference.GetValues failed (err {err})"
                return result

            value_count = struct.unpack_from(">I", gv_data, 0)[0]
            gv_offset = 4
            result: dict[str, Any] = self._expanded_value_base(obj_id, "object")
            result["_class"] = sig
            for i in range(value_count):
                if i >= len(fields):
                    break
                _, fname, _fsig = fields[i]
                tag = gv_data[gv_offset]; gv_offset += 1
                val, gv_offset = self._read_value(
                    jdwp,
                    ids,
                    tag,
                    gv_data,
                    gv_offset,
                    depth,
                    visited,
                    semantic_collections=semantic_collections,
                    item_limit=item_limit,
                    map_entry_limit=map_entry_limit,
                )
                result[fname] = val
            return result
        except Exception as e:
            return self._object_error_value(obj_id, error=str(e))

    def _array_length(
        self,
        jdwp: JDWPClient,
        ids,
        arr_id: int,
    ) -> tuple[int, str]:
        err, len_data = jdwp.command(Cmd.ARRAY, 1, ids.pack_obj(arr_id))
        if err:
            return 0, f"ArrayReference.Length failed (err {err})"
        if len(len_data) < 4:
            return 0, "ArrayReference.Length reply too short"
        return struct.unpack_from(">I", len_data, 0)[0], ""

    def _read_array_raw_values(
        self,
        jdwp: JDWPClient,
        ids,
        arr_id: int,
        requested_len: int,
    ) -> tuple[list[dict[str, Any]], int, str]:
        if requested_len <= 0:
            return [], 0, ""
        payload = ids.pack_obj(arr_id) + struct.pack(">II", 0, requested_len)
        err, ev_data = jdwp.command(Cmd.ARRAY, 2, payload)
        if err:
            return [], 0, f"ArrayReference.GetValues failed (err {err})"
        if len(ev_data) < 5:
            return [], 0, "ArrayReference.GetValues reply too short"

        element_tag = ev_data[0]
        returned_count = struct.unpack_from(">I", ev_data, 1)[0]
        offset = 5
        read_len = min(returned_count, requested_len)
        values: list[dict[str, Any]] = []
        try:
            for _ in range(read_len):
                if self._array_elements_are_tagged(element_tag):
                    tag = ev_data[offset]
                    offset += 1
                else:
                    tag = element_tag
                raw, offset = self._read_raw_tagged_value(ids, tag, ev_data, offset)
                values.append(raw)
        except (IndexError, struct.error) as exc:
            return values, returned_count, f"ArrayReference.GetValues decode failed: {exc}"
        return values, returned_count, ""

    def _read_array_items(
        self,
        jdwp: JDWPClient,
        ids,
        arr_id: int,
        requested_len: int,
        depth: int,
        visited: set[int],
        *,
        semantic_collections: bool,
        item_limit: int,
        map_entry_limit: int,
    ) -> tuple[list[Any], int, str]:
        raw_values, returned_count, error = self._read_array_raw_values(
            jdwp, ids, arr_id, requested_len
        )
        items = [
            self._expand_raw_tagged_value(
                jdwp,
                ids,
                raw,
                depth,
                visited,
                semantic_collections=semantic_collections,
                item_limit=item_limit,
                map_entry_limit=map_entry_limit,
            )
            for raw in raw_values
        ]
        return items, returned_count, error

    def _read_semantic_collection(
        self,
        jdwp: JDWPClient,
        ids,
        obj_id: int,
        ref_type_id: int,
        signature: str,
        depth: int,
        visited: set[int],
        *,
        item_limit: int,
        map_entry_limit: int,
    ) -> dict[str, Any] | None:
        class_name = self._java_class_name(signature)
        if class_name == "java.util.ArrayList":
            return self._read_array_list_semantic(
                jdwp, ids, obj_id, ref_type_id, signature, depth, visited,
                item_limit=item_limit, map_entry_limit=map_entry_limit,
            )
        if class_name == "java.util.LinkedList":
            return self._read_linked_list_semantic(
                jdwp, ids, obj_id, ref_type_id, signature, depth, visited,
                item_limit=item_limit, map_entry_limit=map_entry_limit,
            )
        if class_name in {"java.util.HashMap", "java.util.LinkedHashMap"}:
            return self._read_hash_map_semantic(
                jdwp, ids, obj_id, ref_type_id, signature, depth, visited,
                entry_limit=map_entry_limit, item_limit=item_limit,
            )
        if class_name in {"java.util.HashSet", "java.util.LinkedHashSet"}:
            return self._read_hash_set_semantic(
                jdwp, ids, obj_id, ref_type_id, signature, depth, visited,
                item_limit=item_limit, map_entry_limit=map_entry_limit,
            )
        if class_name == "java.util.Optional":
            return self._read_optional_semantic(
                jdwp, ids, obj_id, ref_type_id, signature, depth, visited,
                item_limit=item_limit, map_entry_limit=map_entry_limit,
            )
        return None

    def _raw_int_value(self, raw: dict[str, Any] | None) -> int | None:
        if raw is None:
            return None
        if int(raw.get("tag", 0)) == Tag.INT and isinstance(raw.get("value"), int):
            return int(raw["value"])
        return None

    def _raw_object_id(self, raw: dict[str, Any] | None) -> int:
        if raw is None:
            return 0
        if int(raw.get("tag", 0)) in {
            Tag.ARRAY,
            Tag.OBJECT,
            Tag.STRING,
            Tag.THREAD,
            Tag.THREAD_GROUP,
            Tag.CLASS_LOADER,
            Tag.CLASS_OBJECT,
        }:
            return int(raw.get("value") or 0)
        return 0

    def _read_array_list_semantic(
        self,
        jdwp: JDWPClient,
        ids,
        obj_id: int,
        ref_type_id: int,
        signature: str,
        depth: int,
        visited: set[int],
        *,
        item_limit: int,
        map_entry_limit: int,
    ) -> dict[str, Any]:
        result = self._semantic_value_base(obj_id, "list", signature)
        result.update({
            "size": None,
            "items": [],
            "truncated": False,
            "item_limit": item_limit,
        })
        fields, lookup_errors = self._instance_field_lookup(jdwp, ids, ref_type_id)
        raw, error = self._read_named_fields_raw(
            jdwp, ids, obj_id, fields, ["size", "elementData"]
        )
        size = self._raw_int_value(raw.get("size"))
        if size is None:
            return self._mark_value_state(
                result,
                "unavailable",
                error or "ArrayList.size field was not readable",
            )
        result["size"] = size
        limit = min(size, item_limit)
        result["truncated"] = size > limit
        if lookup_errors and not error:
            error = "; ".join(lookup_errors)
        if size == 0 or limit == 0:
            if error:
                self._mark_value_state(result, "partial", error)
            return result

        element_data_id = self._raw_object_id(raw.get("elementData"))
        if element_data_id == 0:
            return self._mark_value_state(
                result,
                "unavailable",
                error or "ArrayList.elementData was null or unreadable",
            )
        array_len, array_error = self._array_length(jdwp, ids, element_data_id)
        if array_error:
            return self._mark_value_state(result, "unavailable", array_error)

        requested_len = min(limit, array_len)
        items, returned_count, items_error = self._read_array_items(
            jdwp,
            ids,
            element_data_id,
            requested_len,
            depth,
            visited,
            semantic_collections=True,
            item_limit=item_limit,
            map_entry_limit=map_entry_limit,
        )
        result["items"] = items
        errors = [message for message in (error, items_error) if message]
        if array_len < limit:
            errors.append(
                f"ArrayList.elementData length {array_len} is smaller than size {size}"
            )
        if returned_count != requested_len:
            errors.append(
                f"ArrayReference.GetValues returned {returned_count} value(s), "
                f"expected {requested_len}"
            )
        if errors:
            self._mark_value_state(result, "partial" if items else "unavailable", "; ".join(errors))
        return result

    def _read_linked_list_semantic(
        self,
        jdwp: JDWPClient,
        ids,
        obj_id: int,
        ref_type_id: int,
        signature: str,
        depth: int,
        visited: set[int],
        *,
        item_limit: int,
        map_entry_limit: int,
    ) -> dict[str, Any]:
        result = self._semantic_value_base(obj_id, "list", signature)
        result.update({
            "size": None,
            "items": [],
            "truncated": False,
            "item_limit": item_limit,
        })
        fields, lookup_errors = self._instance_field_lookup(jdwp, ids, ref_type_id)
        raw, error = self._read_named_fields_raw(
            jdwp, ids, obj_id, fields, ["size", "first"]
        )
        size = self._raw_int_value(raw.get("size"))
        if size is None:
            return self._mark_value_state(
                result,
                "unavailable",
                error or "LinkedList.size field was not readable",
            )
        result["size"] = size
        limit = min(size, item_limit)
        result["truncated"] = size > limit
        if lookup_errors and not error:
            error = "; ".join(lookup_errors)
        if size == 0 or limit == 0:
            if error:
                self._mark_value_state(result, "partial", error)
            return result

        node_id = self._raw_object_id(raw.get("first"))
        if node_id == 0:
            return self._mark_value_state(
                result,
                "unavailable",
                error or "LinkedList.first was null before size items were read",
            )

        items: list[Any] = []
        errors = [message for message in (error,) if message]
        seen_nodes: set[int] = set()
        while node_id and len(items) < limit:
            if node_id in seen_nodes:
                errors.append("LinkedList node cycle detected")
                break
            seen_nodes.add(node_id)
            node_ref_type_id, _node_sig, node_error = self._object_signature(
                jdwp, ids, node_id
            )
            if node_error:
                errors.append(node_error)
                break
            node_fields, node_lookup_errors = self._instance_field_lookup(
                jdwp, ids, node_ref_type_id
            )
            errors.extend(node_lookup_errors)
            node_raw, node_read_error = self._read_named_fields_raw(
                jdwp, ids, node_id, node_fields, ["item", "next"]
            )
            if node_read_error:
                errors.append(node_read_error)
            if "item" not in node_raw:
                break
            items.append(self._expand_raw_tagged_value(
                jdwp,
                ids,
                node_raw["item"],
                depth,
                visited,
                semantic_collections=True,
                item_limit=item_limit,
                map_entry_limit=map_entry_limit,
            ))
            node_id = self._raw_object_id(node_raw.get("next"))

        result["items"] = items
        if len(items) < limit:
            errors.append(f"LinkedList traversal returned {len(items)} item(s), expected {limit}")
        if errors:
            self._mark_value_state(result, "partial" if items else "unavailable", "; ".join(errors))
        return result

    def _read_hash_map_semantic(
        self,
        jdwp: JDWPClient,
        ids,
        obj_id: int,
        ref_type_id: int,
        signature: str,
        depth: int,
        visited: set[int],
        *,
        entry_limit: int,
        item_limit: int,
    ) -> dict[str, Any]:
        result = self._semantic_value_base(obj_id, "map", signature)
        result.update({
            "size": None,
            "entries": [],
            "truncated": False,
            "entry_limit": entry_limit,
        })
        entries, size, truncated, state, error = self._read_hash_map_entries(
            jdwp,
            ids,
            obj_id,
            ref_type_id,
            depth,
            visited,
            entry_limit=entry_limit,
            item_limit=item_limit,
        )
        result["size"] = size
        result["entries"] = entries
        result["truncated"] = truncated
        if state:
            self._mark_value_state(result, state, error)
        return result

    def _read_hash_set_semantic(
        self,
        jdwp: JDWPClient,
        ids,
        obj_id: int,
        ref_type_id: int,
        signature: str,
        depth: int,
        visited: set[int],
        *,
        item_limit: int,
        map_entry_limit: int,
    ) -> dict[str, Any]:
        result = self._semantic_value_base(obj_id, "set", signature)
        result.update({
            "size": None,
            "items": [],
            "truncated": False,
            "item_limit": item_limit,
        })
        fields, lookup_errors = self._instance_field_lookup(jdwp, ids, ref_type_id)
        raw, error = self._read_named_fields_raw(jdwp, ids, obj_id, fields, ["map"])
        map_id = self._raw_object_id(raw.get("map"))
        if map_id == 0:
            message = error or "HashSet.map field was null or unreadable"
            if lookup_errors:
                message = "; ".join([message, *lookup_errors])
            return self._mark_value_state(result, "unavailable", message)

        map_ref_type_id, _map_sig, map_error = self._object_signature(jdwp, ids, map_id)
        if map_error:
            return self._mark_value_state(result, "unavailable", map_error)
        entries, size, truncated, state, map_entries_error = self._read_hash_map_entries(
            jdwp,
            ids,
            map_id,
            map_ref_type_id,
            depth,
            visited | {map_id},
            entry_limit=item_limit,
            item_limit=item_limit,
        )
        result["size"] = size
        result["items"] = [entry["key"] for entry in entries]
        result["truncated"] = truncated
        errors = [message for message in (error, map_entries_error) if message]
        errors.extend(lookup_errors)
        if state or errors:
            self._mark_value_state(
                result,
                state or "partial",
                "; ".join(errors) if errors else map_entries_error,
            )
        return result

    def _read_optional_semantic(
        self,
        jdwp: JDWPClient,
        ids,
        obj_id: int,
        ref_type_id: int,
        signature: str,
        depth: int,
        visited: set[int],
        *,
        item_limit: int,
        map_entry_limit: int,
    ) -> dict[str, Any]:
        result = self._semantic_value_base(obj_id, "optional", signature)
        fields, lookup_errors = self._instance_field_lookup(jdwp, ids, ref_type_id)
        raw, error = self._read_named_fields_raw(jdwp, ids, obj_id, fields, ["value"])
        if "value" not in raw:
            message = error or "Optional.value field was not readable"
            if lookup_errors:
                message = "; ".join([message, *lookup_errors])
            result["present"] = None
            return self._mark_value_state(result, "unavailable", message)
        value_id = self._raw_object_id(raw["value"])
        result["present"] = value_id != 0
        if value_id == 0:
            result["value"] = None
            return result
        result["value"] = self._expand_raw_tagged_value(
            jdwp,
            ids,
            raw["value"],
            depth,
            visited,
            semantic_collections=True,
            item_limit=item_limit,
            map_entry_limit=map_entry_limit,
        )
        if error or lookup_errors:
            self._mark_value_state(
                result,
                "partial",
                "; ".join([message for message in [error, *lookup_errors] if message]),
            )
        return result

    def _read_hash_map_entries(
        self,
        jdwp: JDWPClient,
        ids,
        map_id: int,
        ref_type_id: int,
        depth: int,
        visited: set[int],
        *,
        entry_limit: int,
        item_limit: int,
    ) -> tuple[list[dict[str, Any]], int | None, bool, str, str]:
        fields, lookup_errors = self._instance_field_lookup(jdwp, ids, ref_type_id)
        raw, error = self._read_named_fields_raw(
            jdwp, ids, map_id, fields, ["size", "table"]
        )
        size = self._raw_int_value(raw.get("size"))
        if size is None:
            message = error or "HashMap.size field was not readable"
            if lookup_errors:
                message = "; ".join([message, *lookup_errors])
            return [], None, False, "unavailable", message

        limit = min(size, entry_limit)
        truncated = size > limit
        if size == 0 or limit == 0:
            return [], size, truncated, "", error or "; ".join(lookup_errors)

        table_id = self._raw_object_id(raw.get("table"))
        if table_id == 0:
            message = error or "HashMap.table field was null or unreadable"
            if lookup_errors:
                message = "; ".join([message, *lookup_errors])
            return [], size, truncated, "unavailable", message

        table_len, table_error = self._array_length(jdwp, ids, table_id)
        if table_error:
            return [], size, truncated, "unavailable", table_error

        bucket_scan_limit = min(
            table_len,
            max(self._max_array_elements, entry_limit * 4, 16),
        )
        bucket_raw_values, returned_count, bucket_error = self._read_array_raw_values(
            jdwp, ids, table_id, bucket_scan_limit
        )
        entries: list[dict[str, Any]] = []
        errors = [message for message in (error, bucket_error) if message]
        errors.extend(lookup_errors)
        if returned_count != bucket_scan_limit:
            errors.append(
                f"HashMap.table returned {returned_count} bucket(s), "
                f"expected {bucket_scan_limit}"
            )

        seen_nodes: set[int] = set()
        for bucket in bucket_raw_values:
            node_id = self._raw_object_id(bucket)
            while node_id and len(entries) < limit:
                if node_id in seen_nodes:
                    errors.append("HashMap node cycle detected")
                    node_id = 0
                    break
                seen_nodes.add(node_id)
                node_ref_type_id, _node_sig, node_error = self._object_signature(
                    jdwp, ids, node_id
                )
                if node_error:
                    errors.append(node_error)
                    break
                node_fields, node_lookup_errors = self._instance_field_lookup(
                    jdwp, ids, node_ref_type_id
                )
                errors.extend(node_lookup_errors)
                node_raw, node_read_error = self._read_named_fields_raw(
                    jdwp, ids, node_id, node_fields, ["key", "value", "next"]
                )
                if node_read_error:
                    errors.append(node_read_error)
                if "key" not in node_raw or "value" not in node_raw:
                    break
                entries.append({
                    "key": self._expand_raw_tagged_value(
                        jdwp,
                        ids,
                        node_raw["key"],
                        depth,
                        visited,
                        semantic_collections=True,
                        item_limit=item_limit,
                        map_entry_limit=entry_limit,
                    ),
                    "value": self._expand_raw_tagged_value(
                        jdwp,
                        ids,
                        node_raw["value"],
                        depth,
                        visited,
                        semantic_collections=True,
                        item_limit=item_limit,
                        map_entry_limit=entry_limit,
                    ),
                })
                node_id = self._raw_object_id(node_raw.get("next"))
            if len(entries) >= limit:
                break

        if bucket_scan_limit < table_len and len(entries) < limit:
            errors.append(
                f"HashMap.table scan capped at {bucket_scan_limit}/{table_len} buckets"
            )
        if len(entries) < limit:
            errors.append(
                f"HashMap traversal returned {len(entries)} entry(s), expected {limit}"
            )
        state = "partial" if errors else ""
        if not entries and size > 0 and errors:
            state = "unavailable"
        return entries, size, truncated, state, "; ".join(errors)

    def _read_array(
        self,
        jdwp: JDWPClient,
        ids,
        arr_id: int,
        depth: int = 3,
        visited: set[int] | None = None,
        semantic_collections: bool = True,
        item_limit: int = 16,
        map_entry_limit: int = 16,
    ) -> dict:
        """Read an array's length and elements."""
        try:
            if visited is None:
                visited = set()

            _ref_type_id, signature, signature_error = self._object_signature(
                jdwp, ids, arr_id
            )

            # Get length
            err, len_data = jdwp.command(Cmd.ARRAY, 1, ids.pack_obj(arr_id))
            if err:
                if semantic_collections:
                    result = self._semantic_value_base(arr_id, "array", signature)
                    result["length"] = None
                    result["items"] = []
                    result["truncated"] = False
                    result["item_limit"] = item_limit
                    return self._mark_value_state(
                        result, "unavailable", f"ArrayReference.Length failed (err {err})"
                    )
                result: dict[str, Any] = self._expanded_value_base(arr_id, "array")
                result["_length"] = "?"
                result["_error"] = f"length failed (err {err})"
                return result
            total_len = struct.unpack_from(">I", len_data, 0)[0]

            if semantic_collections:
                result = self._semantic_value_base(arr_id, "array", signature)
                result["length"] = total_len
                result["items"] = []
                result["truncated"] = total_len > item_limit
                result["item_limit"] = item_limit
                if signature_error:
                    self._mark_value_state(result, "partial", signature_error)
                if total_len == 0 or item_limit == 0:
                    return result

                requested_len = min(total_len, item_limit)
                items, returned_count, error = self._read_array_items(
                    jdwp,
                    ids,
                    arr_id,
                    requested_len,
                    depth,
                    visited,
                    semantic_collections=semantic_collections,
                    item_limit=item_limit,
                    map_entry_limit=map_entry_limit,
                )
                result["items"] = items
                if error or returned_count != requested_len:
                    message = error or (
                        f"ArrayReference.GetValues returned {returned_count} "
                        f"value(s), expected {requested_len}"
                    )
                    state = "partial" if items else "unavailable"
                    self._mark_value_state(result, state, message)
                return result

            if total_len == 0:
                result = self._expanded_value_base(arr_id, "array")
                result["_length"] = 0
                result["elements"] = []
                return result

            requested_len = min(total_len, self._max_array_elements)

            # Read all elements (JDWP GetValues: firstIndex=0, length=arr_len)
            elements, returned_count, error = self._read_array_items(
                jdwp,
                ids,
                arr_id,
                requested_len,
                depth,
                visited,
                semantic_collections=semantic_collections,
                item_limit=item_limit,
                map_entry_limit=map_entry_limit,
            )

            result: dict[str, Any] = self._expanded_value_base(arr_id, "array")
            result["_length"] = total_len
            result["elements"] = elements
            if error:
                result["_error"] = error
            if total_len > requested_len:
                result["_truncated"] = True
                result["_remaining_count"] = total_len - requested_len
            if returned_count != requested_len:
                result["_warning"] = (
                    f"arrayregion returned {returned_count} values (expected {requested_len})"
                )
            return result
        except Exception as e:
            result = self._expanded_value_base(arr_id, "array")
            result["_length"] = "?"
            result["_error"] = str(e)
            return result

    def _is_instance_field(self, mod_bits: int) -> bool:
        return (mod_bits & 0x0008) == 0

    def _array_elements_are_tagged(self, element_tag: int) -> bool:
        return element_tag in {
            Tag.ARRAY,
            Tag.OBJECT,
            Tag.STRING,
            Tag.THREAD,
            Tag.THREAD_GROUP,
            Tag.CLASS_LOADER,
            Tag.CLASS_OBJECT,
        }

    def _connect(self, timeout: float = 5.0) -> JDWPClient:
        """Get a JDWP connection. Reuses persistent connection if alive."""
        if self._jdwp is not None:
            try:
                # ``command`` multiplexes interleaved events into its event
                # queue, so probing the connection cannot discard a hit.
                self._jdwp.command(Cmd.VM, 1)  # Version
                logger.debug("java_runtime.jdwp.connection.reused")
                return self._jdwp
            except Exception as exc:
                logger.warning(
                    "java_runtime.jdwp.connection.stale error_type=%s error=%s",
                    type(exc).__name__,
                    str(exc).splitlines()[0] if str(exc) else "-",
                )
                try:
                    self._jdwp.close()
                except Exception:
                    pass
                self._jdwp = None
                invalidated = self._invalidate_connection_scoped_requests(
                    "The previous JDWP connection became unreachable; "
                    "live JDWP requests were invalidated while stable Runtime "
                    "definitions were preserved."
                )
                if invalidated:
                    raise RuntimeError(
                        "JDWP connection changed while debug state was active; "
                        "start a new wait_event with wait_mode='arm' to re-arm "
                        "stable event definitions."
                    ) from exc
        jdwp = JDWPClient()
        proc = self._proc.current
        if proc is None or not proc.is_alive():
            raise RuntimeError("No application running — cannot connect debugger")
        logger.info(
            "java_runtime.jdwp.connection.open pid=%s host=%s port=%s timeout=%s",
            proc.pid, self._host, proc.jdwp_port, timeout,
        )
        jdwp.connect(self._host, proc.jdwp_port, timeout)
        self._jdwp = jdwp
        return jdwp

    def _disconnect(self) -> None:
        """Close the persistent JDWP connection."""
        if self._jdwp is not None:
            logger.info("java_runtime.jdwp.connection.close")
            try:
                self._jdwp.close()
            except Exception:
                pass
            self._jdwp = None

    def _invalidate_connection_scoped_requests(
        self,
        warning: str,
        *,
        force_warning: bool = False,
    ) -> bool:
        """Forget live JDWP ids while preserving stable Runtime definitions."""
        had_remote_state = bool(
            self._armed_breakpoint_requests
            or self._armed_exception_requests
            or self._active_suspension is not None
        )
        self._armed_breakpoint_requests.clear()
        self._armed_exception_requests.clear()
        self._invalidate_suspension()
        if had_remote_state or force_warning:
            self._debug_connection_dirty = True
            self._debug_connection_warning = warning
        return had_remote_state or force_warning
