from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jolink_runtime.adapters.java.jdwp_adapter import JavaRuntime
from jolink_runtime.adapters.java.process_manager import (
    ProcessInfo,
    ProcessManager,
    ProcessStartupError,
    ReadyPortAlreadyInUseError,
    RuntimeAlreadyRunningError,
)
from jolink_runtime.core.dispatcher import parse_runtime_action
from jolink_runtime.core.models import RuntimeAction, RuntimeResult


class _FakeProcess:
    def __init__(self, pid: int = 4101) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


def test_unconfigured_readiness_is_never_reported_as_ready() -> None:
    process = ProcessInfo(_FakeProcess(), 5005, "Example")
    observed = ProcessManager().observe_readiness(process)

    assert observed["process_state"] == "running"
    assert observed["startup_state"] == "unverified"
    assert observed["readiness_configured"] is False
    assert "readiness" not in observed


def test_direct_runtime_readiness_is_not_hidden_by_idle_project_controller() -> None:
    runtime = JavaRuntime()
    process = ProcessInfo(_FakeProcess(), 5005, "Example")
    runtime._proc._process = process

    observed = runtime.startup_observation()

    assert observed["pid"] == process.pid
    assert observed["process_state"] == "running"
    assert observed["startup_state"] == "unverified"
    assert observed["readiness_configured"] is False
    assert "launch_phase" not in observed


def test_readiness_timeout_keeps_process_alive_then_status_reaches_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = ProcessInfo(
        _FakeProcess(),
        5005,
        "Example",
        ready_port=8080,
    )
    manager = ProcessManager()
    probe_results = iter([False, True])
    monkeypatch.setattr(
        manager,
        "_check_tcp_port",
        lambda *_args, **_kwargs: next(probe_results),
    )

    waiting = manager.wait_for_readiness(process, 0)
    ready = manager.observe_readiness(process)
    ready_again = manager.observe_readiness(process)

    assert process.is_alive() is True
    assert waiting["startup_state"] == "starting"
    assert waiting["startup_wait_timed_out"] is True
    assert waiting["readiness"]["verified"] is False
    assert ready["startup_state"] == "ready"
    assert ready["readiness"]["verified"] is True
    assert ready["readiness"]["last_result"] == "connection_accepted"
    assert ready_again["ready_observed_at"] == ready["ready_observed_at"]
    assert (
        ready_again["startup_elapsed_ms"]
        == ready["startup_elapsed_ms"]
    )


def test_exited_process_is_a_successful_status_observation_of_failed_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _FakeProcess()
    process = ProcessInfo(
        target,
        5005,
        "Example",
        ready_port=8080,
    )
    runtime = JavaRuntime()
    runtime._proc._process = process
    target.returncode = 1

    result = runtime.status(RuntimeAction(action="status"))

    assert result.ok is True
    assert result.error == ""
    assert result.data["process_state"] == "exited"
    assert result.data["startup_state"] == "failed"
    assert result.data["failure_type"] == "process_exited_before_ready"
    assert result.data["exit_code"] == 1


def test_ready_port_preflight_rejects_existing_listener_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ProcessManager()
    spawned = False
    monkeypatch.setattr(
        manager,
        "_check_tcp_port",
        lambda *_args, **_kwargs: True,
    )

    def unexpected_spawn(*_args: Any, **_kwargs: Any) -> None:
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(
        "jolink_runtime.adapters.java.process_manager.subprocess.Popen",
        unexpected_spawn,
    )

    with pytest.raises(ReadyPortAlreadyInUseError):
        manager.start(
            classpath=".",
            main_class="Example",
            ready_port=8080,
        )

    assert spawned is False
    assert manager.current is None


def test_run_never_replaces_previous_target_or_probes_its_ready_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ProcessManager()
    previous_target = _FakeProcess(pid=4102)
    previous = manager._publish(
        ProcessInfo(previous_target, 5005, "Previous")
    )
    probed = False
    spawned = False

    def unexpected_probe(*_args: Any, **_kwargs: Any) -> bool:
        nonlocal probed
        probed = True
        return False

    def unexpected_spawn(*_args: Any, **_kwargs: Any) -> None:
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(manager, "_check_tcp_port", unexpected_probe)
    monkeypatch.setattr(
        "jolink_runtime.adapters.java.process_manager.subprocess.Popen",
        unexpected_spawn,
    )

    with pytest.raises(RuntimeAlreadyRunningError):
        manager.start(
            classpath=".",
            main_class="Replacement",
            jdwp_port=5006,
            ready_port=8080,
        )

    assert probed is False
    assert spawned is False
    assert previous.is_alive() is True
    assert manager.current is previous


def test_run_timeout_returns_starting_and_directs_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = JavaRuntime()
    process = ProcessInfo(
        _FakeProcess(),
        5005,
        "Example",
        ready_port=8080,
        startup_wait_timeout_seconds=0,
        readiness_config_source="explicit",
    )
    process.record_readiness_probe(False)
    process.mark_startup_wait_timed_out()
    captured: dict[str, Any] = {}

    def fake_start(**kwargs: Any) -> ProcessInfo:
        captured.update(kwargs)
        runtime._proc._process = process
        return process

    monkeypatch.setattr(runtime._proc, "start", fake_start)
    monkeypatch.setattr(
        runtime._proc,
        "wait_for_readiness",
        lambda *_args, **_kwargs: process.readiness_snapshot(),
    )
    monkeypatch.setattr(
        runtime._log,
        "create",
        lambda _label: str(tmp_path / "application.log"),
    )
    action = parse_runtime_action({
        "action": "run",
        "main_class": "Example",
        "ready_port": 8080,
        "startup_wait_timeout_seconds": 0,
    })

    result = runtime.run(action)

    assert result.ok is True
    assert result.data["status"] == "process_started"
    assert result.data["process_state"] == "running"
    assert result.data["startup_state"] == "starting"
    assert result.data["startup_wait_timed_out"] is True
    assert result.data["next_action"] == "status"
    assert captured["ready_port"] == 8080
    assert captured["startup_wait_timeout_seconds"] == 0


def test_direct_jvm_start_failure_is_structured_without_log_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = JavaRuntime()

    def fail_start(**_kwargs: Any) -> ProcessInfo:
        raise ProcessStartupError(
            "Process exited with code 7 before JDWP became ready.",
            failure_type="process_exited_before_jdwp",
            exit_code=7,
            cleanup_settled=True,
        )

    monkeypatch.setattr(runtime._proc, "start", fail_start)

    result = runtime.run(RuntimeAction(
        action="run",
        main_class="Example",
    ))

    assert result.ok is False
    assert result.data["error_code"] == "JVM_START_FAILED"
    assert result.data["failure_type"] == "process_exited_before_jdwp"
    assert result.data["exit_code"] == 7
    assert result.data["cleanup_settled"] is True
    assert "Last log lines" not in result.error


def test_restart_reuses_previous_readiness_unless_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = JavaRuntime()
    previous = ProcessInfo(
        _FakeProcess(),
        5005,
        "Example",
        ready_port=8080,
        startup_wait_timeout_seconds=45,
        readiness_config_source="explicit",
    )
    runtime._proc._process = previous
    observed: list[RuntimeAction] = []
    monkeypatch.setattr(
        runtime,
        "stop",
        lambda _action: RuntimeResult(ok=True, data={"status": "stopped"}),
    )
    monkeypatch.setattr(
        runtime,
        "run",
        lambda action: (
            observed.append(action)
            or RuntimeResult(ok=True, data={"status": "process_started"})
        ),
    )
    monkeypatch.setattr(
        "jolink_runtime.adapters.java.jdwp_adapter.time.sleep",
        lambda _seconds: None,
    )

    result = runtime.restart(RuntimeAction(
        action="restart",
        main_class="Example",
    ))

    assert result.ok is True
    assert observed[0].ready_port == 8080
    assert observed[0].startup_wait_timeout_seconds == 45
    assert observed[0].readiness_config_source == "previous_run"


def test_restart_explicit_readiness_overrides_previous_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = JavaRuntime()
    runtime._proc._process = ProcessInfo(
        _FakeProcess(),
        5005,
        "Example",
        ready_port=8080,
        startup_wait_timeout_seconds=45,
        readiness_config_source="explicit",
    )
    observed: list[RuntimeAction] = []
    monkeypatch.setattr(
        runtime,
        "stop",
        lambda _action: RuntimeResult(ok=True, data={"status": "stopped"}),
    )
    monkeypatch.setattr(
        runtime,
        "run",
        lambda action: (
            observed.append(action)
            or RuntimeResult(ok=True, data={"status": "process_started"})
        ),
    )
    monkeypatch.setattr(
        "jolink_runtime.adapters.java.jdwp_adapter.time.sleep",
        lambda _seconds: None,
    )
    action = parse_runtime_action({
        "action": "restart",
        "main_class": "Example",
        "ready_port": 9090,
        "startup_wait_timeout_seconds": 12,
    })

    result = runtime.restart(action)

    assert result.ok is True
    assert observed[0].ready_port == 9090
    assert observed[0].startup_wait_timeout_seconds == 12
    assert observed[0].readiness_config_source == "explicit"


def test_ready_port_cannot_reuse_jdwp_port() -> None:
    runtime = JavaRuntime()
    action = parse_runtime_action({
        "action": "run",
        "main_class": "Example",
        "jdwp_port": 5005,
        "ready_port": 5005,
    })

    result = runtime.run(action)

    assert result.ok is False
    assert result.data["error_code"] == "READY_PORT_CONFLICTS_WITH_JDWP"
    assert runtime._proc.current is None
