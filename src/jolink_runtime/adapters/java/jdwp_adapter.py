"""
JavaRuntime — Agent-facing Java runtime manager.

Implements the Runtime ABC. Internally composits:
  - JDWPClient  (pure protocol transport)
  - ProcessManager (lifecycle)
  - LogManager    (console output)

LLM never sees JDWP, thread IDs, or protocol details.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...core.models import RuntimeAction, RuntimeResult, Variable
from ...core.wait_state import WaitControl
from ..base import Runtime
from .jdwp_client import JDWPClient, JDWPError, Cmd, EventKind, SuspendPolicy, Tag
from .process_manager import ProcessManager
from .log_manager import LogManager

logger = logging.getLogger(__name__)


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
        self._force_closed = False
        self._debug_connection_dirty = False
        self._debug_connection_warning = ""
        self._target_release_lock = threading.Lock()
        self._released_target_tokens: set[tuple[int, int, int, bool]] = set()

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
    ) -> None:
        if current is None:
            return
        token = (
            int(getattr(current, "generation", 0)),
            id(current),
            int(current.pid),
            bool(current.owned),
        )
        with self._target_release_lock:
            if token in self._released_target_tokens:
                return
            try:
                if current.owned:
                    stop_target = getattr(self._proc, "stop_target", None)
                    result = (
                        stop_target(current)
                        if callable(stop_target)
                        else self._proc.stop()
                    )
                else:
                    detach_target = getattr(self._proc, "detach_target", None)
                    result = (
                        detach_target(current)
                        if callable(detach_target)
                        else self._proc.detach()
                    )
                self._released_target_tokens.add(token)
                logger.info(
                    "java_runtime.shutdown.target_released "
                    "pid=%s ownership=%s status=%s forced=%s",
                    current.pid,
                    ownership,
                    result.get("status", "-"),
                    forced,
                )
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
        if not (self._closed or self._force_closed):
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

    def force_close(self) -> None:
        """Bounded ownership-aware release that performs no JDWP commands."""
        if self._force_closed:
            return
        self._force_closed = True
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
        self._release_target_for_shutdown(
            current,
            ownership,
            forced=True,
        )
        logger.warning(
            "java_runtime.shutdown.force_finish pid=%s ownership=%s",
            current.pid if current is not None else "-",
            ownership,
        )

    def close(self) -> None:
        """Best-effort, ownership-aware shutdown for an internal session."""
        if self._closed:
            logger.debug("java_runtime.shutdown.skipped reason=already_closed")
            return
        self._closed = True
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

        self._release_target_for_shutdown(
            current,
            ownership,
            forced=False,
        )

        logger.info(
            "java_runtime.shutdown.finish pid=%s ownership=%s",
            current.pid if current is not None else "-",
            ownership,
        )

    def run(self, action: RuntimeAction) -> RuntimeResult:
        launch_mode = "jar" if action.jar_path else "class"
        logger.info(
            "java_runtime.jvm.run.request launch_mode=%s main_class=%s jar_path=%s "
            "classpath=%s jdwp_port=%s "
            "app_args_count=%s vm_args_count=%s",
            launch_mode,
            action.main_class or "-",
            action.jar_path or "-",
            action.classpath,
            action.jdwp_port,
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
            )
            closing = self._release_late_published_target(
                info,
                operation="run",
            )
            if closing is not None:
                return closing
            data = {
                "status": "started",
                "pid": info.pid,
                "jdwp_port": info.jdwp_port,
                "log_file": log_file,
                "launch_mode": info.launch_mode,
            }
            if info.launch_mode == "jar":
                data["jar_path"] = info.jar_path
            else:
                data["main_class"] = info.main_class
            result = RuntimeResult(ok=True, data=data)
            logger.info(
                "java_runtime.jvm.run.ready pid=%s launch_mode=%s target=%s "
                "jdwp_port=%s log_file=%s",
                info.pid, info.launch_mode, info.jar_path or info.main_class,
                info.jdwp_port, log_file,
            )
            closing = self._release_late_published_target(
                info,
                operation="run",
            )
            if closing is not None:
                return closing
            return result
        except Exception as e:
            logger.error(
                "java_runtime.jvm.run.failed launch_mode=%s target=%s jdwp_port=%s "
                "error_type=%s error=%s",
                launch_mode, action.jar_path or action.main_class or "-", action.jdwp_port,
                type(e).__name__, _error_summary(e),
            )
            return RuntimeResult(ok=False, error=str(e))

    def stop(self, action: RuntimeAction) -> RuntimeResult:
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
        logger.info(
            "java_runtime.jvm.stop.finish status=%s pid=%s",
            data.get("status", "-"), data.get("pid", "-"),
        )
        return RuntimeResult(ok=True, data=data)

    def restart(self, action: RuntimeAction) -> RuntimeResult:
        logger.info(
            "java_runtime.jvm.restart.request launch_mode=%s target=%s",
            "jar" if action.jar_path else "class",
            action.jar_path or action.main_class or "-",
        )
        self.stop(action)
        time.sleep(1)
        return self.run(action)

    def attach(self, action: RuntimeAction) -> RuntimeResult:
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

    def status(self, action: RuntimeAction) -> RuntimeResult:
        proc = self._proc.current
        if proc is None:
            return RuntimeResult(ok=True, data={
                "process_state": "absent",
                "debug_state": "detached",
                "running": False,
                "message": "No application is managed by this runtime",
            })
        if not proc.is_alive():
            self._invalidate_suspension()
            return RuntimeResult(ok=True, data={
                "process_state": "exited",
                "debug_state": "detached",
                "running": False,
                "pid": proc.pid,
                "exit_code": proc.exit_code,
                "message": "Managed application has exited",
            })

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
        }
        if self._debug_connection_dirty:
            info["debug_requests_invalidated"] = True
            info.setdefault("warnings", []).append(self._debug_connection_warning)
            info["suggested_next_step"] = (
                "Retry wait_event; stable breakpoint and exception definitions "
                "will be re-armed on the current connection."
            )
        if proc.launch_mode == "jar":
            info["jar_path"] = proc.jar_path
        else:
            info["main_class"] = proc.main_class

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
                info["suggested_next_step"] = (
                    "Start wait_event before triggering the next target scenario."
                )

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
                "Retry wait_event; stable breakpoint and exception definitions "
                "will be re-armed on the current connection."
            )

        return RuntimeResult(ok=True, data=info)

    def logs(self, action: RuntimeAction) -> RuntimeResult:
        data = self._log.tail(action.tail)
        if "error" in data:
            return RuntimeResult(ok=False, error=data["error"])
        return RuntimeResult(ok=True, data=data)

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
                        "Start wait_event, then trigger the target scenario while that wait is active."
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
                        "Start wait_event, then trigger the target scenario while that wait is active."
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
                        "Call cleanup_debug_state, then set the required events and wait again."
                    ),
                },
            )

        if EventKind.BREAKPOINT in accepted_kinds:
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
                                "then retry wait_event."
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
                                "Retry wait_event, or call cleanup_debug_state and set the breakpoint again."
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
                                "Trigger class loading without relying on this watch, then retry wait_event."
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
                                "Retry wait_event, or call cleanup_debug_state and set the exception watch again."
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
                    "Call status, then retry wait_event; definitions will be re-armed on the new connection."
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
                    "suggested_next_step": "Retry wait_event.",
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
                "suggested_next_step": (
                    "Arrange the next trigger, start wait_event, and fire the trigger "
                    "while that wait is active."
                ),
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
                "suggested_next_step": "Start or attach to an application before setting debug events again.",
            })

        warnings: list[str] = []
        drained_events = 0
        resumed_active_suspension = False
        emergency_vm_resume = False
        cleared_breakpoints: list[str] = list(self._breakpoints)
        cleared_exceptions: list[int] = list(self._exceptions)
        clear_failures: list[dict[str, Any]] = []

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
            if clear_failures:
                # Clear failures cannot be left behind: a future event could
                # otherwise suspend a JVM after local definitions are gone.
                self._disconnect()
                debug_state = "detached"
                warnings.append(
                    "JDWP was disconnected because one or more event requests could not be cleared."
                )
            if not warnings and not clear_failures:
                self._debug_connection_dirty = False
                self._debug_connection_warning = ""

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
                "suggested_next_step": (
                    "Call status to confirm counts are 0, then set the needed "
                    "breakpoint or exception event again."
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
                "then start a new wait_event before triggering the scenario again."
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
                "No active debug suspension. Call wait_event or wait_breakpoint after triggering the debug event."
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
        return {
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
        matches = [
            thread_id for thread_id in self._all_thread_ids(jdwp)
            if thread_name.lower() in self._thread_name(jdwp, thread_id).lower()
        ]
        if not matches:
            raise RuntimeError(f"Thread matching '{thread_name}' not found")
        if len(matches) > 1:
            names = [self._thread_name(jdwp, thread_id) for thread_id in matches[:5]]
            raise RuntimeError(
                f"Thread name '{thread_name}' is ambiguous; matches: {names}"
            )
        return matches[0]

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
                        "retry wait_event to re-arm stable event definitions."
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
