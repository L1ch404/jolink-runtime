from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from jolink_runtime.adapters.java import process_manager as process_module
from jolink_runtime.adapters.java.process_manager import (
    ProcessInfo,
    ProcessManager,
    ProcessStartCancelledError,
)
from jolink_runtime.launch.process_tree import ProcessTreeHandle, TerminationReport


class _FakeProcess:
    def __init__(self, pid: int = 7201) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class _ExitAfterPollsProcess(_FakeProcess):
    def __init__(self, *, exit_after_polls: int, pid: int = 7202) -> None:
        super().__init__(pid)
        self._poll_count = 0
        self._exit_after_polls = exit_after_polls

    def poll(self) -> int | None:
        self._poll_count += 1
        if self._poll_count >= self._exit_after_polls:
            self.returncode = 1
        return self.returncode


def _advance_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    value = 0.0

    def monotonic() -> float:
        nonlocal value
        value += 3.0
        return value

    monkeypatch.setattr(process_module.time, "monotonic", monotonic)


def test_start_accepts_exact_java_command_cwd_and_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    process = _FakeProcess()

    def fake_popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(process_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        ProcessManager,
        "_check_jdwp_port",
        staticmethod(lambda *_args, **_kwargs: True),
    )
    _advance_clock(monkeypatch)
    command = (
        str(tmp_path / "jdk8" / "bin" / "java"),
        "-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,"
        "address=127.0.0.1:5005",
        "-cp",
        str(tmp_path / "runtime-classpath.jar"),
        "com.example.Application",
    )

    info = ProcessManager().start(
        classpath="unused",
        main_class="com.example.Application",
        command_argv=command,
        working_directory=tmp_path,
        environment_overrides={
            "SPRING_PROFILES_ACTIVE": "dogfood",
            "PRIVATE_TOKEN": "do-not-log",
        },
    )

    assert captured["command"] == list(command)
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert (
        captured["kwargs"]["env"]["SPRING_PROFILES_ACTIVE"]
        == "dogfood"
    )
    assert captured["kwargs"]["env"]["PRIVATE_TOKEN"] == "do-not-log"
    assert info.pid == process.pid


def test_start_cancellation_before_spawn_is_non_destructive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned = False

    def unexpected_spawn(*_args: Any, **_kwargs: Any) -> None:
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(process_module.subprocess, "Popen", unexpected_spawn)
    manager = ProcessManager()

    with pytest.raises(ProcessStartCancelledError):
        manager.start(
            classpath=".",
            main_class="Example",
            should_stop=lambda: True,
        )

    assert spawned is False
    assert manager.current is None


def test_cancelled_spawn_cleans_retained_pathing_jar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained = tmp_path / "runtime-classpath.jar"
    retained.write_bytes(b"jar")
    process = _FakeProcess()
    checks = iter((False, True))
    stopped: list[int] = []
    monkeypatch.setattr(
        process_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    class Terminator:
        @staticmethod
        def terminate(handle, **_kwargs):
            stopped.append(handle.pid)
            handle.process.returncode = -15
            return TerminationReport(
                pid=handle.pid,
                terminated=True,
                forced=False,
            )

    manager = ProcessManager(terminator=Terminator())

    with pytest.raises(ProcessStartCancelledError):
        manager.start(
            classpath=".",
            main_class="Example",
            should_stop=lambda: next(checks),
            retained_files=(retained,),
        )

    assert stopped == [process.pid]
    assert retained.exists() is False
    assert manager.current is None


def test_stop_removes_only_managed_retained_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained = tmp_path / "runtime-classpath.jar"
    retained.write_bytes(b"jar")
    process = _FakeProcess()
    manager = ProcessManager()
    info = manager._publish(
        ProcessInfo(
            process,
            5005,
            "Example",
            retained_files=(retained,),
        )
    )
    monkeypatch.setattr(
        ProcessManager,
        "_stop_posix",
        staticmethod(lambda target: setattr(target, "returncode", -15)),
    )
    monkeypatch.setattr(
        ProcessManager,
        "_stop_windows",
        staticmethod(lambda target: setattr(target, "returncode", -15)),
    )

    result = manager.stop_target(info)

    assert result == {"status": "stopped", "pid": process.pid}
    assert retained.exists() is False


def test_unsettled_stop_retains_exact_ownership_and_retries(
    tmp_path: Path,
) -> None:
    retained = tmp_path / "runtime-classpath.jar"
    retained.write_bytes(b"jar")
    process = _FakeProcess()
    outcomes = iter((False, True))

    class Terminator:
        @staticmethod
        def terminate(handle, **_kwargs):
            terminated = next(outcomes)
            if terminated:
                handle.process.returncode = -9
            return TerminationReport(
                pid=handle.pid,
                terminated=terminated,
                forced=terminated,
                remaining_pids=() if terminated else (handle.pid,),
            )

    manager = ProcessManager(terminator=Terminator())
    info = manager._publish(
        ProcessInfo(
            process,
            5005,
            "Example",
            retained_files=(retained,),
            process_tree=ProcessTreeHandle.from_process(process),
        )
    )

    first = manager.stop_target(info)

    assert first["status"] == "stop_failed"
    assert first["settled"] is False
    assert manager.current is info
    assert retained.is_file()

    second = manager.stop_target(info, force=True)

    assert second == {"status": "stopped", "pid": process.pid}
    assert manager.current is None
    assert retained.exists() is False


def test_cancelled_spawn_keeps_owner_when_rollback_is_unsettled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained = tmp_path / "runtime-classpath.jar"
    retained.write_bytes(b"jar")
    process = _FakeProcess()
    checks = iter((False, True))
    terminate_succeeds = False

    class Terminator:
        @staticmethod
        def terminate(handle, **_kwargs):
            if terminate_succeeds:
                handle.process.returncode = -9
            return TerminationReport(
                pid=handle.pid,
                terminated=terminate_succeeds,
                forced=terminate_succeeds,
                remaining_pids=(
                    () if terminate_succeeds else (handle.pid,)
                ),
            )

    monkeypatch.setattr(
        process_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    manager = ProcessManager(terminator=Terminator())

    with pytest.raises(ProcessStartCancelledError):
        manager.start(
            classpath=".",
            main_class="Example",
            should_stop=lambda: next(checks),
            retained_files=(retained,),
        )

    assert manager.current is not None
    assert manager.current.pid == process.pid
    assert retained.is_file()

    terminate_succeeds = True
    result = manager.stop_target(manager.current, force=True)

    assert result == {"status": "stopped", "pid": process.pid}
    assert manager.current is None
    assert retained.exists() is False


def test_dead_root_does_not_release_unsettled_descendant_tree() -> None:
    process = _FakeProcess()
    handle = ProcessTreeHandle.from_process(process)
    process.returncode = 0
    attempts = 0

    class Terminator:
        @staticmethod
        def terminate(_handle, **_kwargs):
            nonlocal attempts
            attempts += 1
            return TerminationReport(
                pid=process.pid,
                terminated=False,
                forced=True,
                remaining_pids=(process.pid + 1,),
            )

    manager = ProcessManager(terminator=Terminator())
    info = manager._publish(
        ProcessInfo(
            process,
            5005,
            "Example",
            process_tree=handle,
        )
    )
    stopped = manager.stop_target(info, force=True)

    assert stopped["status"] == "stop_failed"
    assert stopped["remaining_pids"] == [process.pid + 1]
    assert manager.current is info
    assert attempts == 1


@pytest.mark.parametrize(
    ("process", "jdwp_ready"),
    [
        (_ExitAfterPollsProcess(exit_after_polls=1, pid=7301), False),
        (_ExitAfterPollsProcess(exit_after_polls=3, pid=7302), True),
    ],
    ids=("before-jdwp", "during-stability-window"),
)
def test_start_early_exit_retains_unsettled_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process: _ExitAfterPollsProcess,
    jdwp_ready: bool,
) -> None:
    outcomes = iter((False, True))
    published: list[ProcessInfo] = []

    class Terminator:
        @staticmethod
        def terminate(handle, **_kwargs):
            terminated = next(outcomes)
            return TerminationReport(
                pid=handle.pid,
                terminated=terminated,
                forced=True,
                remaining_pids=() if terminated else (handle.pid + 1,),
            )

    monkeypatch.setattr(
        process_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        ProcessManager,
        "_check_jdwp_port",
        staticmethod(lambda *_args, **_kwargs: jdwp_ready),
    )
    manager = ProcessManager(terminator=Terminator())

    with pytest.raises(RuntimeError, match="Process exited with code 1"):
        manager.start(
            classpath=".",
            main_class="Example",
            command_argv=("java", "Example"),
            startup_timeout=0.2,
            log_file=str(tmp_path / "startup.log"),
            on_published=published.append,
        )

    assert len(published) == 1
    assert manager.current is published[0]
    assert manager.current.process_tree is not None

    stopped = manager.stop_target(manager.current, force=True)

    assert stopped == {"status": "stopped", "pid": process.pid}
    assert manager.current is None
