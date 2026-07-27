from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from jolink_runtime.launch import (
    AttemptToken,
    BuildOperationSpec,
    ProcessSupervisor,
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
