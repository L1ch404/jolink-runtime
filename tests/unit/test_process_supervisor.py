from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from jolink_runtime import launch as launch_module
from jolink_runtime.launch import process_tree as process_tree_module
from jolink_runtime.launch import (
    AttemptToken,
    BuildOperationSpec,
    ProcessSupervisor,
    ProcessTreeTerminator,
    TerminationReport,
)


def _wait_for_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                if path.read_text(encoding="utf-8").strip():
                    return
            except OSError:
                pass
        time.sleep(0.02)
    raise AssertionError(f"{path} was not populated")


def _pid_is_effectively_alive(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group assertion")
def test_posix_group_members_exclude_non_running_processes(
    monkeypatch,
) -> None:
    class ObservedProcess:
        def __init__(self, pid: int, status: str) -> None:
            self.info = {"pid": pid, "status": status}

    dead_status = getattr(psutil, "STATUS_DEAD", "dead")
    observed = (
        ObservedProcess(701, psutil.STATUS_ZOMBIE),
        ObservedProcess(702, dead_status),
        ObservedProcess(703, psutil.STATUS_SLEEPING),
        ObservedProcess(704, psutil.STATUS_RUNNING),
    )
    monkeypatch.setattr(
        process_tree_module.psutil,
        "process_iter",
        lambda attrs: observed,
    )
    monkeypatch.setattr(
        process_tree_module.os,
        "getpgid",
        lambda pid: 91 if pid != 704 else 92,
    )

    assert ProcessTreeTerminator._posix_group_members(91) == (703,)


def test_supervisor_runs_without_pipe_and_captures_binary_output(
    tmp_path: Path,
) -> None:
    log = tmp_path / "build.log"
    spec = BuildOperationSpec(
        argv=(
            sys.executable,
            "-c",
            "import sys; print('build-ok'); print('warn', file=sys.stderr)",
        ),
        cwd=tmp_path,
        output_capture=log,
        operation_name="probe",
    )

    result = ProcessSupervisor().run(
        spec,
        owner=AttemptToken("launch_1", 1),
    )

    assert result.succeeded is True
    assert result.return_code == 0
    assert set(log.read_text(encoding="utf-8").splitlines()) == {
        "build-ok",
        "warn",
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group assertion")
def test_cancel_terminates_parent_and_child_process_group(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "pids.txt"
    log = tmp_path / "build.log"
    script = (
        "import os,pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import time; time.sleep(60)']);"
        f"pathlib.Path({str(pid_file)!r}).write_text("
        "f'{os.getpid()} {child.pid}',encoding='utf-8');"
        "time.sleep(60)"
    )
    spec = BuildOperationSpec(
        argv=(sys.executable, "-c", script),
        cwd=tmp_path,
        output_capture=log,
        operation_name="compile",
    )
    owner = AttemptToken("launch_1", 1)
    supervisor = ProcessSupervisor()
    result_box: list[object] = []
    worker = threading.Thread(
        target=lambda: result_box.append(
            supervisor.run(spec, owner=owner)
        )
    )
    worker.start()
    _wait_for_file(pid_file)
    parent_pid, child_pid = [
        int(value)
        for value in pid_file.read_text(encoding="utf-8").split()
    ]

    cancellation = supervisor.cancel(
        owner,
        deadline=time.monotonic() + 5.0,
    )
    worker.join(5.0)

    assert worker.is_alive() is False
    assert cancellation.requested is True
    assert cancellation.settled is True
    assert result_box and result_box[0].cancelled is True
    assert _pid_is_effectively_alive(parent_pid) is False
    assert _pid_is_effectively_alive(child_pid) is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX escaped-session assertion")
def test_cancel_terminates_identity_bound_child_that_escapes_process_group(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "escaped-pids.txt"
    script = (
        "import os,pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import os,time; os.setsid(); time.sleep(60)']);"
        f"pathlib.Path({str(pid_file)!r}).write_text("
        "f'{os.getpid()} {child.pid}',encoding='utf-8');"
        "time.sleep(60)"
    )
    owner = AttemptToken("launch_escaped_child", 1)
    supervisor = ProcessSupervisor()
    result_box: list[object] = []
    worker = threading.Thread(
        target=lambda: result_box.append(
            supervisor.run(
                BuildOperationSpec(
                    argv=(sys.executable, "-c", script),
                    cwd=tmp_path,
                    operation_name="compile",
                ),
                owner=owner,
            )
        )
    )
    worker.start()
    _wait_for_file(pid_file)
    parent_pid, child_pid = [
        int(value)
        for value in pid_file.read_text(encoding="utf-8").split()
    ]
    # Ensure the running supervisor has observed the escaped descendant.
    time.sleep(0.1)

    cancellation = supervisor.cancel(
        owner,
        deadline=time.monotonic() + 5.0,
    )
    worker.join(5.0)

    assert cancellation.settled is True
    assert worker.is_alive() is False
    assert _pid_is_effectively_alive(parent_pid) is False
    assert _pid_is_effectively_alive(child_pid) is False


def test_operation_timeout_terminates_the_process(tmp_path: Path) -> None:
    result = ProcessSupervisor().run(
        BuildOperationSpec(
            argv=(
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
            ),
            cwd=tmp_path,
            timeout_seconds=0.05,
            output_capture=tmp_path / "timeout.log",
            operation_name="probe",
        ),
        owner=AttemptToken("launch_1", 1),
    )

    assert result.timed_out is True
    assert result.cancelled is False
    assert result.termination is not None
    assert result.termination.terminated is True


def test_unsettled_successful_command_retains_owner_until_retry(
    tmp_path: Path,
) -> None:
    calls = 0

    class Terminator:
        @staticmethod
        def terminate(handle, **_kwargs):
            nonlocal calls
            calls += 1
            terminated = calls >= 2
            return TerminationReport(
                pid=handle.pid,
                terminated=terminated,
                forced=terminated,
                remaining_pids=() if terminated else (handle.pid + 1,),
            )

    owner = AttemptToken("launch_retry", 1)
    supervisor = ProcessSupervisor(terminator=Terminator())
    result = supervisor.run(
        BuildOperationSpec(
            argv=(sys.executable, "-c", "raise SystemExit(0)"),
            cwd=tmp_path,
            operation_name="probe",
        ),
        owner=owner,
    )

    assert result.return_code == 0
    assert result.succeeded is False
    assert result.termination is not None
    assert result.termination.terminated is False
    assert supervisor.snapshot(owner)["running"] is True
    assert supervisor.release_owner(owner) is False

    retried = supervisor.cancel(
        owner,
        deadline=time.monotonic() + 1.0,
    )

    assert retried.settled is True
    assert calls == 2
    assert supervisor.snapshot(owner)["running"] is False
    assert supervisor.release_owner(owner) is True


def test_spawn_failure_preserves_original_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SpawnFailure(RuntimeError):
        pass

    def fail_spawn(*_args, **_kwargs):
        raise SpawnFailure("spawn failed")

    monkeypatch.setattr(
        launch_module.process_supervisor.subprocess,
        "Popen",
        fail_spawn,
    )

    with pytest.raises(SpawnFailure, match="spawn failed"):
        ProcessSupervisor().run(
            BuildOperationSpec(
                argv=("missing",),
                cwd=tmp_path,
                operation_name="probe",
            ),
            owner=AttemptToken("launch_spawn_failure", 1),
        )


def test_close_does_not_settle_before_in_flight_spawn_is_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_entered = threading.Event()
    release_spawn = threading.Event()

    class Process:
        pid = 7401
        returncode = None

        def poll(self):
            return self.returncode

    process = Process()
    termination_calls = 0

    class Terminator:
        @staticmethod
        def terminate(handle, **_kwargs):
            nonlocal termination_calls
            termination_calls += 1
            terminated = termination_calls >= 2
            if terminated:
                handle.process.returncode = -9
            return TerminationReport(
                pid=handle.pid,
                terminated=terminated,
                forced=terminated,
                remaining_pids=() if terminated else (handle.pid,),
            )

    def blocked_spawn(*_args, **_kwargs):
        spawn_entered.set()
        assert release_spawn.wait(2.0)
        return process

    monkeypatch.setattr(
        launch_module.process_supervisor.subprocess,
        "Popen",
        blocked_spawn,
    )
    owner = AttemptToken("launch_in_flight", 1)
    supervisor = ProcessSupervisor(terminator=Terminator())
    result_box: list[object] = []
    worker = threading.Thread(
        target=lambda: result_box.append(
            supervisor.run(
                BuildOperationSpec(
                    argv=("java", "-version"),
                    cwd=tmp_path,
                    operation_name="probe",
                ),
                owner=owner,
            )
        )
    )
    worker.start()
    assert spawn_entered.wait(1.0)

    closing = supervisor.close(deadline=time.monotonic() + 0.05)

    assert closing.settled is False
    assert supervisor.snapshot(owner)["in_flight_operation_count"] == 1
    release_spawn.set()
    worker.join(2.0)
    assert worker.is_alive() is False
    assert supervisor.snapshot(owner)["running"] is True

    forced = supervisor.force_close(deadline=time.monotonic() + 1.0)

    assert forced.settled is True
    assert termination_calls == 2
    assert supervisor.snapshot(owner)["running"] is False


def test_close_prevents_late_spawn(tmp_path: Path) -> None:
    supervisor = ProcessSupervisor()
    supervisor.close(deadline=time.monotonic() + 1.0)

    result = supervisor.run(
        BuildOperationSpec(
            argv=(sys.executable, "-c", "raise SystemExit(0)"),
            cwd=tmp_path,
            operation_name="probe",
        ),
        owner=AttemptToken("launch_1", 1),
    )

    assert result.cancelled is True
    assert result.return_code is None


def test_supervisor_models_do_not_repr_secret_argv_or_environment(
    tmp_path: Path,
) -> None:
    secret = "private-build-password"
    spec = BuildOperationSpec(
        argv=("mvn", f"-Dpassword={secret}", "compile"),
        cwd=tmp_path,
        environment={"TOKEN": secret},
    )

    assert secret not in repr(spec)
