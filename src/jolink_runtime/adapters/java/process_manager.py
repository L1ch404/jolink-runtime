"""
Java process lifecycle manager — pure subprocess, no JDWP dependency.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Optional

import psutil


logger = logging.getLogger(__name__)
_IS_WINDOWS = os.name == "nt"
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


class ReadyPortAlreadyInUseError(RuntimeError):
    """Raised when readiness would observe a listener that predates launch."""


def _iso_timestamp(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


class ProcessInfo:
    """Snapshot of a running Java process."""
    def __init__(
        self,
        proc: subprocess.Popen | None,
        jdwp_port: int,
        main_class: str,
        *,
        jar_path: str = "",
        pid: int | None = None,
        owned: bool = True,
        generation: int = 0,
        ready_port: int = 0,
        startup_wait_timeout_seconds: float = 30.0,
        readiness_config_source: str = "not_configured",
    ):
        self.proc = proc
        self._pid = proc.pid if proc is not None else int(pid or 0)
        self.jdwp_port = jdwp_port
        self.main_class = main_class
        self.jar_path = jar_path
        self.launch_mode = "jar" if jar_path else "class" if owned else "attached"
        self.owned = owned
        self.generation = generation
        self.ready_port = int(ready_port or 0)
        self.startup_wait_timeout_seconds = float(
            startup_wait_timeout_seconds
        )
        self.readiness_config_source = readiness_config_source
        self._started_monotonic = time.monotonic()
        self._readiness_lock = threading.Lock()
        self._startup_state = (
            "starting" if self.ready_port > 0 else "unverified"
        )
        self._readiness_last_checked_at: float | None = None
        self._readiness_last_result = "not_checked"
        self._ready_observed_at: float | None = None
        self._ready_observed_monotonic: float | None = None
        self._failed_at: float | None = None
        self._failed_monotonic: float | None = None
        self._failure_type = ""
        self._startup_wait_timed_out = False

    @property
    def pid(self) -> int:
        return self._pid

    def is_alive(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        if self._pid <= 0:
            return False
        return psutil.pid_exists(self._pid)

    @property
    def exit_code(self) -> int | None:
        return self.proc.poll() if self.proc is not None else None

    def record_readiness_probe(self, ready: bool) -> None:
        """Record one TCP probe while preserving the first ready observation."""
        now = time.time()
        monotonic_now = time.monotonic()
        with self._readiness_lock:
            self._readiness_last_checked_at = now
            self._readiness_last_result = (
                "connection_accepted" if ready else "connection_refused"
            )
            if ready and self._startup_state != "ready":
                self._startup_state = "ready"
                self._ready_observed_at = now
                self._ready_observed_monotonic = monotonic_now
                self._startup_wait_timed_out = False

    def mark_startup_wait_timed_out(self) -> None:
        with self._readiness_lock:
            if self._startup_state == "starting":
                self._startup_wait_timed_out = True

    def mark_startup_failed(self, failure_type: str) -> None:
        now = time.time()
        monotonic_now = time.monotonic()
        with self._readiness_lock:
            self._startup_state = "failed"
            self._failure_type = failure_type
            if self._failed_at is None:
                self._failed_at = now
                self._failed_monotonic = monotonic_now

    def readiness_snapshot(self) -> dict:
        """Return a consistent, protocol-free application readiness snapshot."""
        now_monotonic = time.monotonic()
        with self._readiness_lock:
            state = self._startup_state
            elapsed_until = (
                self._ready_observed_monotonic
                if state == "ready"
                else self._failed_monotonic
                if state == "failed"
                else now_monotonic
            )
            result: dict = {
                "startup_state": state,
                "readiness_configured": self.ready_port > 0,
                "readiness_config_source": self.readiness_config_source,
            }
            if self.owned:
                result["startup_elapsed_ms"] = max(
                    0,
                    int((elapsed_until - self._started_monotonic) * 1000),
                )
                result["startup_wait_timeout_seconds"] = (
                    self.startup_wait_timeout_seconds
                )
            if self.ready_port <= 0:
                return result

            readiness = {
                "type": "tcp_port",
                "host": "127.0.0.1",
                "port": self.ready_port,
                "verified": state == "ready",
                "last_result": self._readiness_last_result,
            }
            if self._readiness_last_checked_at is not None:
                readiness["last_checked_at"] = _iso_timestamp(
                    self._readiness_last_checked_at
                )
            result["readiness"] = readiness
            if self._ready_observed_at is not None:
                result["ready_observed_at"] = _iso_timestamp(
                    self._ready_observed_at
                )
            if self._failed_at is not None:
                result["startup_failed_at"] = _iso_timestamp(
                    self._failed_at
                )
            if self._failure_type:
                result["failure_type"] = self._failure_type
            if self._startup_wait_timed_out and state == "starting":
                result["startup_wait_timed_out"] = True
            return result


class ProcessManager:
    """Start, stop, and monitor a Java process."""

    JDWP_HANDSHAKE = b"JDWP-Handshake"

    def __init__(self, host: str = "localhost"):
        self._host = host
        self._process: Optional[ProcessInfo] = None
        self._generation = 0
        self._state_lock = threading.RLock()
        self._accept_new_targets = True

    def _publish(self, process: ProcessInfo) -> ProcessInfo:
        """Publish a new target with a manager-local monotonic generation."""
        with self._state_lock:
            self._generation += 1
            process.generation = self._generation
            self._process = process
        return process

    def _forget_target(self, process: ProcessInfo) -> None:
        """Forget a failed target without clearing a newer replacement."""
        with self._state_lock:
            if self._process is process:
                self._process = None

    def prevent_new_targets(self) -> None:
        """Close the spawn/attach gate before Runtime shutdown inspects state."""
        with self._state_lock:
            self._accept_new_targets = False

    # -- helpers --

    @staticmethod
    def _read_log_tail(log_file: str | None, n: int = 20) -> str:
        """Read a UTF-8 log tail, returning a visible diagnostic on failure."""
        if not log_file:
            return "[No log file configured]"
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return "".join(lines[-n:])
        except OSError as exc:
            logger.warning(
                "java_runtime.process.log_tail.failed path=%s error_type=%s error=%s",
                log_file, type(exc).__name__, exc,
            )
            return f"[Unable to read log file: {type(exc).__name__}: {exc}]"

    @staticmethod
    def _check_jdwp_port(host: str, port: int, timeout: float = 0.5) -> bool:
        """Check if a JDWP port is accepting connections AND replies with handshake."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.sendall(ProcessManager.JDWP_HANDSHAKE)
            reply = sock.recv(14)
            sock.close()
            return reply == ProcessManager.JDWP_HANDSHAKE
        except (ConnectionRefusedError, socket.timeout, OSError):
            return False

    @staticmethod
    def _check_tcp_port(host: str, port: int, timeout: float = 0.2) -> bool:
        """Return whether a local TCP listener accepts a connection."""
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            return False

    # -- lifecycle --

    def start(
        self,
        classpath: str,
        main_class: str,
        *,
        jar_path: str = "",
        app_args: list[str] | None = None,
        jdwp_port: int = 5005,
        vm_args: list[str] | None = None,
        log_file: str | None = None,
        startup_timeout: float = 30.0,
        ready_port: int = 0,
        startup_wait_timeout_seconds: float = 30.0,
        readiness_config_source: str = "not_configured",
    ) -> ProcessInfo:
        """Launch a Java process with JDWP enabled, return ProcessInfo.

        Waits up to ``startup_timeout`` seconds only for JDWP reachability
        (handshake verified + process survives 2s after). Optional application
        TCP readiness is observed separately and never terminates the process
        when its bounded wait expires.
        """
        started_at = time.monotonic()
        launch_mode = "jar" if jar_path else "class"
        if jar_path and main_class:
            raise RuntimeError("Provide either jar_path or main_class, not both")
        if not jar_path and not main_class:
            raise RuntimeError("run requires either jar_path or main_class")
        if ready_port < 0 or ready_port > 65535:
            raise RuntimeError("ready_port must be between 1 and 65535")

        logger.info(
            "java_runtime.process.start.request launch_mode=%s main_class=%s "
            "jar_path=%s classpath=%s "
            "jdwp_port=%s ready_port=%s app_args_count=%s vm_args_count=%s "
            "jdwp_startup_timeout=%s readiness_wait_timeout=%s",
            launch_mode, main_class or "-", jar_path or "-", classpath, jdwp_port,
            ready_port or "-", len(app_args or []), len(vm_args or []),
            startup_timeout, startup_wait_timeout_seconds,
        )
        # Auto-restart: stop old process first
        with self._state_lock:
            previous = self._process
        if previous and previous.is_alive():
            logger.info(
                "java_runtime.process.start.replacing pid=%s",
                previous.pid,
            )
            self.stop_target(previous)
        elif previous is not None:
            self._forget_target(previous)

        if ready_port and self._check_tcp_port("127.0.0.1", ready_port):
            logger.warning(
                "java_runtime.process.readiness.port_in_use port=%s",
                ready_port,
            )
            raise ReadyPortAlreadyInUseError(
                f"Readiness port {ready_port} is already accepting connections"
            )

        log_fp = None
        proc: subprocess.Popen | None = None
        process: ProcessInfo | None = None
        try:
            if log_file:
                # The Java child writes bytes directly to this file descriptor.
                # Binary mode avoids applying the host's Windows text encoding.
                log_fp = open(log_file, "wb")

            cmd = [
                "java",
                f"-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,"
                f"address=127.0.0.1:{jdwp_port}",
            ]
            if vm_args:
                cmd.extend(vm_args)
            if jar_path:
                cmd.extend(["-jar", jar_path])
            else:
                cmd.extend(["-cp", classpath, main_class])
            if app_args:
                cmd.extend(app_args)

            popen_kwargs = {
                "stdout": log_fp or subprocess.DEVNULL,
                "stderr": subprocess.STDOUT,
            }
            if _IS_WINDOWS:
                popen_kwargs["creationflags"] = _CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
            # Spawn and publish under the same lock. Shutdown must never
            # observe an empty manager after the OS process exists, even while
            # this method is still waiting for JDWP readiness.
            with self._state_lock:
                if not self._accept_new_targets:
                    raise RuntimeError("Process manager is shutting down")
                proc = subprocess.Popen(
                    cmd,
                    **popen_kwargs,
                )
                process = ProcessInfo(
                    proc,
                    jdwp_port,
                    main_class,
                    jar_path=jar_path,
                    ready_port=ready_port,
                    startup_wait_timeout_seconds=(
                        startup_wait_timeout_seconds
                    ),
                    readiness_config_source=readiness_config_source,
                )
                self._publish(process)
            logger.info(
                "java_runtime.process.spawned pid=%s launch_mode=%s target=%s jdwp_port=%s",
                proc.pid, launch_mode, jar_path or main_class, jdwp_port,
            )
        except Exception as exc:
            if log_fp:
                log_fp.close()
            if process is not None:
                try:
                    self.stop_target(process)
                except Exception:
                    logger.exception(
                        "java_runtime.process.spawn.rollback_failed pid=%s",
                        process.pid,
                    )
            elif proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            logger.error(
                "java_runtime.process.spawn.failed launch_mode=%s target=%s jdwp_port=%s "
                "error_type=%s error=%s",
                launch_mode, jar_path or main_class, jdwp_port, type(exc).__name__,
                str(exc).splitlines()[0] if str(exc) else "-",
            )
            raise

        # Wait for process to confirm ready (JDWP handshake verified)
        assert process is not None
        assert proc is not None
        deadline = time.time() + startup_timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                self._forget_target(process)
                if log_fp:
                    log_fp.close()
                log_tail = self._read_log_tail(log_file)
                logger.warning(
                    "java_runtime.process.start.exited pid=%s exit_code=%s "
                    "elapsed_ms=%.1f captured_log_chars=%s",
                    proc.pid, proc.returncode,
                    (time.monotonic() - started_at) * 1000, len(log_tail),
                )
                raise RuntimeError(
                    f"Process exited with code {proc.returncode}. "
                    f"Last log lines:\n{log_tail}"
                )

            if self._check_jdwp_port("127.0.0.1", jdwp_port):
                # JDWP handshake verified — wait 2s and confirm process stayed alive
                time.sleep(2.0)
                if proc.poll() is not None:
                    self._forget_target(process)
                    if log_fp:
                        log_fp.close()
                    log_tail = self._read_log_tail(log_file)
                    logger.warning(
                        "java_runtime.process.start.unstable pid=%s exit_code=%s "
                        "elapsed_ms=%.1f captured_log_chars=%s",
                        proc.pid, proc.returncode,
                        (time.monotonic() - started_at) * 1000, len(log_tail),
                    )
                    raise RuntimeError(
                        f"Process exited with code {proc.returncode} shortly after startup. "
                        f"Last log lines:\n{log_tail}"
                    )
                break

            time.sleep(0.5)
        else:
            # Timeout: JDWP never ready
            if log_fp:
                log_fp.close()
            log_tail = self._read_log_tail(log_file)
            try:
                proc.kill()
            except Exception:
                pass
            self._forget_target(process)
            logger.warning(
                "java_runtime.process.start.timeout pid=%s jdwp_port=%s "
                "timeout_seconds=%s captured_log_chars=%s",
                proc.pid, jdwp_port, startup_timeout, len(log_tail),
            )
            raise RuntimeError(
                f"Startup timed out after {startup_timeout}s. "
                f"Last log lines:\n{log_tail}"
            )

        if log_fp:
            log_fp.close()
        logger.info(
            "java_runtime.process.start.ready pid=%s launch_mode=%s target=%s jdwp_port=%s "
            "elapsed_ms=%.1f log_file=%s",
            proc.pid, launch_mode, jar_path or main_class, jdwp_port,
            (time.monotonic() - started_at) * 1000, log_file or "-",
        )
        return process

    def observe_readiness(
        self,
        process: ProcessInfo,
        *,
        refresh: bool = True,
    ) -> dict:
        """Observe process/TCP readiness without reading application logs."""
        alive = process.is_alive()
        if not alive:
            prior_state = process.readiness_snapshot()["startup_state"]
            failure_type = (
                "process_exited_after_ready"
                if prior_state == "ready"
                else "process_exited_without_readiness"
                if prior_state == "unverified"
                else "process_exited_before_ready"
            )
            process.mark_startup_failed(failure_type)
            snapshot = process.readiness_snapshot()
            snapshot["process_state"] = "exited"
            return snapshot

        snapshot = process.readiness_snapshot()
        if (
            refresh
            and process.ready_port > 0
            and snapshot["startup_state"] == "starting"
        ):
            process.record_readiness_probe(
                self._check_tcp_port("127.0.0.1", process.ready_port)
            )
            if not process.is_alive():
                prior_state = process.readiness_snapshot()["startup_state"]
                process.mark_startup_failed(
                    "process_exited_after_ready"
                    if prior_state == "ready"
                    else "process_exited_before_ready"
                )
                snapshot = process.readiness_snapshot()
                snapshot["process_state"] = "exited"
                return snapshot
        snapshot = process.readiness_snapshot()
        snapshot["process_state"] = "running"
        return snapshot

    def wait_for_readiness(
        self,
        process: ProcessInfo,
        timeout: float,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict:
        """Wait only for the configured TCP probe; never terminate on timeout."""
        if process.ready_port <= 0:
            return process.readiness_snapshot()

        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            snapshot = self.observe_readiness(process)
            if snapshot["startup_state"] in {"ready", "failed"}:
                return snapshot
            if should_stop is not None and should_stop():
                return snapshot
            if time.monotonic() >= deadline:
                process.mark_startup_wait_timed_out()
                return process.readiness_snapshot()
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def attach(
        self,
        pid: int,
        jdwp_port: int,
        main_class: str = "attached",
        host: str | None = None,
    ) -> ProcessInfo:
        """Track an existing local JVM after verifying its process exists.

        The debugger layer performs the one and only JDWP handshake. Performing
        a probe handshake here would briefly consume the JVM's single debugger
        connection and race the real attach that immediately follows.
        """
        target_host = host or self._host
        logger.info(
            "java_runtime.process.attach.request pid=%s jdwp=%s:%s main_class=%s",
            pid, target_host, jdwp_port, main_class or "-",
        )
        if pid <= 0:
            raise RuntimeError("attach requires a positive pid")
        if not psutil.pid_exists(pid):
            raise RuntimeError(f"Java process {pid} is not running")
        with self._state_lock:
            if not self._accept_new_targets:
                raise RuntimeError("Process manager is shutting down")
            process = ProcessInfo(
                None,
                jdwp_port,
                main_class,
                pid=pid,
                owned=False,
            )
            self._publish(process)
        logger.info(
            "java_runtime.process.attach.ready pid=%s jdwp=%s:%s main_class=%s",
            pid, target_host, jdwp_port, main_class or "-",
        )
        return process

    def detach(self) -> dict:
        """Forget an attached process without terminating it."""
        with self._state_lock:
            process = self._process
        return self.detach_target(process)

    def detach_target(self, process: ProcessInfo | None) -> dict:
        """Forget ``process`` without clearing a newer published target."""
        if process is None:
            logger.info("java_runtime.process.detach.skipped reason=not_attached")
            return {"status": "not_attached"}
        pid = process.pid
        with self._state_lock:
            if self._process is process:
                self._process = None
                status = "detached"
            else:
                # The expected target is already outside this manager. Treat
                # that as an idempotent release without forgetting its
                # replacement.
                status = "already_detached"
        logger.info("java_runtime.process.detached pid=%s", pid)
        return {"status": status, "pid": pid}

    def stop(self) -> dict:
        """Stop the process. Returns {'status': ..., 'pid': ...}."""
        with self._state_lock:
            process = self._process
        return self.stop_target(process)

    def stop_target(self, process: ProcessInfo | None) -> dict:
        """Stop exactly ``process`` without clearing a newer target.

        Shutdown can race a slow ``run``/``restart``.  Operating on an
        expected ProcessInfo object prevents a close that captured target A
        from accidentally stopping or forgetting a later target B.
        """
        if process is None:
            logger.info("java_runtime.process.stop.skipped reason=not_running")
            return {"status": "not_running"}

        if not process.is_alive():
            with self._state_lock:
                if self._process is process:
                    self._process = None
            logger.info(
                "java_runtime.process.stop.skipped reason=not_running pid=%s",
                process.pid,
            )
            return {"status": "not_running", "pid": process.pid}

        if not process.owned:
            return self.detach_target(process)

        pid = process.pid
        proc = process.proc
        if proc is None:
            return self.detach_target(process)
        if _IS_WINDOWS:
            self._stop_windows(proc)
        else:
            self._stop_posix(proc)

        with self._state_lock:
            if self._process is process:
                self._process = None
        logger.info(
            "java_runtime.process.stop.finish pid=%s exit_code=%s",
            pid, proc.poll(),
        )
        return {"status": "stopped", "pid": pid}

    @staticmethod
    def _stop_posix(proc: subprocess.Popen) -> None:
        """Request graceful group shutdown, then escalate on POSIX."""
        try:
            logger.info(
                "java_runtime.process.stop.signal pid=%s signal=SIGTERM",
                proc.pid,
            )
            os.killpg(  # windows-footgun: ok — _stop_posix is never called on Windows
                os.getpgid(proc.pid),
                signal.SIGTERM,
            )
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            logger.warning(
                "java_runtime.process.stop.escalate pid=%s signal=SIGKILL",
                proc.pid,
            )
            try:
                proc.kill()
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except OSError as exc:
            logger.warning(
                "java_runtime.process.stop.signal_failed pid=%s error=%s",
                proc.pid, exc,
            )
            try:
                proc.kill()
            except OSError:
                pass

    @staticmethod
    def _stop_windows(proc: subprocess.Popen) -> None:
        """Request a Windows process-tree stop, then force it if needed."""
        graceful = ["taskkill", "/PID", str(proc.pid), "/T"]
        force = [*graceful, "/F"]
        try:
            logger.info(
                "java_runtime.process.stop.signal pid=%s signal=taskkill_tree",
                proc.pid,
            )
            result = subprocess.run(
                graceful,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            if result.returncode != 0:
                logger.warning(
                    "java_runtime.process.stop.graceful_failed pid=%s returncode=%s",
                    proc.pid, result.returncode,
                )
            proc.wait(timeout=3)
            return
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            logger.warning(
                "java_runtime.process.stop.escalate pid=%s signal=taskkill_tree_force",
                proc.pid,
            )

        try:
            subprocess.run(
                force,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            proc.wait(timeout=3)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

    # -- query --

    @property
    def current(self) -> Optional[ProcessInfo]:
        with self._state_lock:
            return self._process

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            process = self._process
        return process is not None and process.is_alive()
