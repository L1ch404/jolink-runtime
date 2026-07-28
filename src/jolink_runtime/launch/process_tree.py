"""Cross-platform, identity-bound process-tree termination."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field

import psutil


_IS_WINDOWS = os.name == "nt"
_CREATE_NEW_PROCESS_GROUP = (
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if _IS_WINDOWS
    else 0
)


@dataclass(frozen=True)
class TerminationReport:
    pid: int
    terminated: bool
    forced: bool
    remaining_pids: tuple[int, ...] = ()
    error_types: tuple[str, ...] = ()


@dataclass
class ProcessTreeHandle:
    """Exact process identity plus a single idempotent termination claim."""

    process: subprocess.Popen[bytes]
    pid: int
    create_time: float
    process_group_id: int | None
    started_at: float
    _termination_lock: threading.Lock = field(
        default_factory=threading.Lock,
        repr=False,
    )
    _termination_done: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
    )
    _termination_claimed: bool = field(default=False, repr=False)
    _force_requested: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
    )
    _termination_report: TerminationReport | None = field(
        default=None,
        repr=False,
    )
    _known_identities: dict[int, float] = field(
        default_factory=dict,
        repr=False,
    )

    @classmethod
    def from_process(
        cls,
        process: subprocess.Popen[bytes],
    ) -> ProcessTreeHandle:
        try:
            create_time = float(psutil.Process(process.pid).create_time())
        except (psutil.Error, OSError):
            create_time = time.time()
        process_group_id = None
        if not _IS_WINDOWS:
            # start_new_session=True makes the child's PID its PGID. Capture
            # it now; querying after the root exits can fail while children
            # from the same group remain alive.
            process_group_id = int(process.pid)
        handle = cls(
            process=process,
            pid=int(process.pid),
            create_time=create_time,
            process_group_id=process_group_id,
            started_at=time.monotonic(),
        )
        handle.refresh_identity_tree()
        return handle

    def refresh_identity_tree(self) -> None:
        """Remember descendants before a short-lived root can disappear."""
        try:
            root = psutil.Process(self.pid)
            processes = [root, *root.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            return
        identities: dict[int, float] = {}
        for process in processes:
            try:
                identities[int(process.pid)] = float(process.create_time())
            except (psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
                continue
        with self._termination_lock:
            self._known_identities.update(identities)

    def request_termination(self, *, force: bool) -> bool:
        """Return True only to the caller that owns the termination work."""
        if force:
            self._force_requested.set()
        with self._termination_lock:
            if self._termination_claimed:
                if (
                    self._termination_done.is_set()
                    and self._termination_report is not None
                    and not self._termination_report.terminated
                ):
                    self._termination_report = None
                    self._termination_done.clear()
                    return True
                return False
            self._termination_claimed = True
            return True

    def publish_termination(self, report: TerminationReport) -> None:
        with self._termination_lock:
            self._termination_report = report
        self._termination_done.set()

    def wait_for_termination(self, deadline: float) -> TerminationReport:
        remaining = max(0.0, deadline - time.monotonic())
        self._termination_done.wait(remaining)
        with self._termination_lock:
            report = self._termination_report
        if report is not None:
            return report
        return TerminationReport(
            pid=self.pid,
            terminated=False,
            forced=self._force_requested.is_set(),
            remaining_pids=(self.pid,),
            error_types=("termination_deadline_exceeded",),
        )


class ProcessTreeTerminator:
    """Terminate one exact process group/tree without holding caller locks."""

    def terminate(
        self,
        handle: ProcessTreeHandle,
        *,
        deadline: float,
        force: bool = False,
    ) -> TerminationReport:
        if not handle.request_termination(force=force):
            return handle.wait_for_termination(deadline)

        try:
            if _IS_WINDOWS:
                report = self._terminate_windows(handle, deadline)
            else:
                report = self._terminate_posix(handle, deadline)
        except Exception as error:
            report = TerminationReport(
                pid=handle.pid,
                terminated=False,
                forced=handle._force_requested.is_set(),
                remaining_pids=self._known_live_pids(handle),
                error_types=(type(error).__name__,),
            )
        handle.publish_termination(report)
        return report

    def _terminate_posix(
        self,
        handle: ProcessTreeHandle,
        deadline: float,
    ) -> TerminationReport:
        pgid = handle.process_group_id
        if pgid is None:
            return self._terminate_with_psutil(handle, deadline, force=True)

        errors: list[str] = []
        forced = handle._force_requested.is_set()
        members = self._posix_group_members(pgid)
        if members:
            first_signal = signal.SIGKILL if forced else signal.SIGTERM
            try:
                os.killpg(pgid, first_signal)
            except ProcessLookupError:
                members = ()
            except OSError as error:
                errors.append(type(error).__name__)

        graceful_deadline = min(deadline, time.monotonic() + 1.5)
        while members and time.monotonic() < graceful_deadline:
            if handle._force_requested.is_set():
                break
            time.sleep(0.05)
            members = self._posix_group_members(pgid)

        members = self._posix_group_members(pgid)
        if members and time.monotonic() < deadline:
            forced = True
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                members = ()
            except OSError as error:
                errors.append(type(error).__name__)
            while members and time.monotonic() < deadline:
                time.sleep(0.05)
                members = self._posix_group_members(pgid)

        self._reap_root(handle.process, deadline)
        members = self._posix_group_members(pgid)
        escaped = self._known_identity_live_pids(handle)
        if escaped and time.monotonic() < deadline:
            # A child can deliberately or indirectly leave the launch PGID
            # (for example through setsid()). The identity snapshot remains
            # bound to PID+create_time, so use it as a safe final fallback.
            forced = True
            fallback = self._terminate_with_psutil(
                handle,
                deadline,
                force=True,
            )
            errors.extend(fallback.error_types)
            members = self._posix_group_members(pgid)
            escaped = self._known_identity_live_pids(handle)
        remaining = tuple(sorted(set((*members, *escaped))))
        return TerminationReport(
            pid=handle.pid,
            terminated=not remaining,
            forced=forced,
            remaining_pids=remaining,
            error_types=tuple(errors),
        )

    def _terminate_windows(
        self,
        handle: ProcessTreeHandle,
        deadline: float,
    ) -> TerminationReport:
        errors: list[str] = []
        forced = handle._force_requested.is_set()
        if not forced and handle.process.poll() is None:
            try:
                handle.process.send_signal(signal.CTRL_BREAK_EVENT)
            except (AttributeError, OSError, ValueError) as error:
                errors.append(type(error).__name__)
            self._wait_root(handle.process, min(deadline, time.monotonic() + 1.0))

        remaining = self._known_live_pids(handle)
        if remaining and time.monotonic() < deadline:
            forced = True
            try:
                subprocess.run(
                    [
                        "taskkill",
                        "/PID",
                        str(handle.pid),
                        "/T",
                        "/F",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=max(0.1, min(5.0, deadline - time.monotonic())),
                    check=False,
                    creationflags=_CREATE_NEW_PROCESS_GROUP,
                )
            except (
                FileNotFoundError,
                OSError,
                subprocess.SubprocessError,
            ) as error:
                errors.append(type(error).__name__)

        remaining = self._known_live_pids(handle)
        if remaining and time.monotonic() < deadline:
            fallback = self._terminate_with_psutil(
                handle,
                deadline,
                force=True,
            )
            errors.extend(fallback.error_types)
            remaining = fallback.remaining_pids
        self._reap_root(handle.process, deadline)
        return TerminationReport(
            pid=handle.pid,
            terminated=not remaining,
            forced=forced,
            remaining_pids=remaining,
            error_types=tuple(dict.fromkeys(errors)),
        )

    def _terminate_with_psutil(
        self,
        handle: ProcessTreeHandle,
        deadline: float,
        *,
        force: bool,
    ) -> TerminationReport:
        processes = self._identity_bound_tree(handle)
        errors: list[str] = []
        for process in reversed(processes):
            try:
                process.kill() if force else process.terminate()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.Error as error:
                errors.append(type(error).__name__)
        timeout = max(0.0, deadline - time.monotonic())
        _gone, alive = psutil.wait_procs(processes, timeout=timeout)
        return TerminationReport(
            pid=handle.pid,
            terminated=not alive,
            forced=force,
            remaining_pids=tuple(sorted(process.pid for process in alive)),
            error_types=tuple(dict.fromkeys(errors)),
        )

    @staticmethod
    def _identity_bound_tree(
        handle: ProcessTreeHandle,
    ) -> list[psutil.Process]:
        handle.refresh_identity_tree()
        with handle._termination_lock:
            identities = dict(handle._known_identities)
        processes: list[psutil.Process] = []
        for pid, expected_create_time in identities.items():
            try:
                process = psutil.Process(pid)
                if abs(process.create_time() - expected_create_time) <= 0.01:
                    processes.append(process)
            except (psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
                continue
        return processes

    @classmethod
    def _known_live_pids(
        cls,
        handle: ProcessTreeHandle,
    ) -> tuple[int, ...]:
        if not _IS_WINDOWS and handle.process_group_id is not None:
            return cls._posix_group_members(handle.process_group_id)
        return tuple(
            sorted(
                process.pid
                for process in cls._identity_bound_tree(handle)
                if process.is_running()
            )
        )

    @classmethod
    def _known_identity_live_pids(
        cls,
        handle: ProcessTreeHandle,
    ) -> tuple[int, ...]:
        live: list[int] = []
        for process in cls._identity_bound_tree(handle):
            try:
                if (
                    process.is_running()
                    and process.status() != psutil.STATUS_ZOMBIE
                ):
                    live.append(process.pid)
            except (psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
                continue
        return tuple(sorted(live))

    @staticmethod
    def _posix_group_members(process_group_id: int) -> tuple[int, ...]:
        members: list[int] = []
        non_live_statuses = {psutil.STATUS_ZOMBIE}
        dead_status = getattr(psutil, "STATUS_DEAD", None)
        if dead_status is not None:
            non_live_statuses.add(dead_status)
        for process in psutil.process_iter(["pid", "status"]):
            try:
                pid = int(process.info["pid"])
                if process.info.get("status") in non_live_statuses:
                    continue
                if os.getpgid(pid) == process_group_id:
                    members.append(pid)
            except (OSError, psutil.Error):
                continue
        return tuple(sorted(members))

    @staticmethod
    def _wait_root(process: subprocess.Popen[bytes], deadline: float) -> None:
        if process.poll() is not None:
            return
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return

    @classmethod
    def _reap_root(
        cls,
        process: subprocess.Popen[bytes],
        deadline: float,
    ) -> None:
        cls._wait_root(process, deadline)


__all__ = [
    "ProcessTreeHandle",
    "ProcessTreeTerminator",
    "TerminationReport",
]
