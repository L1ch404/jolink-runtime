from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jolink_runtime.adapters.java import process_manager as process_module
from jolink_runtime.adapters.java.jdwp_adapter import JavaRuntime
from jolink_runtime.adapters.java.process_manager import (
    ProcessInfo,
    ProcessManager,
    ProcessStartupError,
)
from jolink_runtime.core.models import RuntimeAction
from jolink_runtime.launch import (
    JvmLaunchPlan,
    LaunchCancelled,
    LaunchErrorCode,
    LaunchPipelineFailure,
    MaterializedJavaCommand,
    PreparedProjectLaunch,
    ProjectLaunchRequest,
)
from jolink_runtime.launch.project_session import ReloadStage


class _FakeProcess:
    _next_pid = 8100

    def __init__(self) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def _request(
    project_path: Path,
    *,
    ready_port: int = 0,
) -> ProjectLaunchRequest:
    return ProjectLaunchRequest(
        project_path=project_path,
        launch_name="Application",
        jdwp_port=5005,
        ready_port=ready_port,
        startup_wait_timeout_seconds=0,
    )


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not observed before the deadline")


@pytest.mark.parametrize(
    ("line", "sensitive_value"),
    [
        ("DB_PASSWORD=db-value-123\n", "db-value-123"),
        ("MAVEN_PASSWORD=maven-value-123\n", "maven-value-123"),
        ("SONATYPE_TOKEN=sonatype-value-123\n", "sonatype-value-123"),
        (
            "AWS_SECRET_ACCESS_KEY=aws-value-123\n",
            "aws-value-123",
        ),
        ("API_TOKEN: api-value-123\n", "api-value-123"),
        ("-Drepo.password=repo-value-123\n", "repo-value-123"),
        ("--password cli-value-123\n", "cli-value-123"),
        (
            "Authorization: Bearer auth-value-123\n",
            "auth-value-123",
        ),
        (
            "[INFO] Authorization: Bearer prefixed-auth-value-123\n",
            "prefixed-auth-value-123",
        ),
        (
            "Authorization=Bearer assignment-auth-value-123\n",
            "assignment-auth-value-123",
        ),
        (
            "COOKIE=session=cookie-value-123; csrf=csrf-value-456\n",
            "csrf=csrf-value-456",
        ),
        (
            "password=password-value-123,comma-value-456\n",
            "comma-value-456",
        ),
        (
            "https://user:url-value-123@example.invalid/repository\n",
            "url-value-123",
        ),
        ('{"password":"json-value-123"}\n', "json-value-123"),
    ],
)
def test_build_log_redaction_covers_common_secret_names_and_forms(
    line: str,
    sensitive_value: str,
) -> None:
    redacted = JavaRuntime._redact_build_log_line(line)

    assert sensitive_value not in redacted
    assert "<redacted>" in redacted


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "Authorization=Bearer secret-token\n",
            "Authorization=<redacted>\n",
        ),
        (
            "COOKIE=session=abc; csrf=def\n",
            "COOKIE=<redacted>\n",
        ),
        (
            "password=abc,def\n",
            "password=<redacted>\n",
        ),
        (
            "--password abc,def --safe=value\n",
            "--password <redacted>\n",
        ),
    ],
)
def test_sensitive_assignment_redacts_the_rest_of_the_log_line(
    line: str,
    expected: str,
) -> None:
    assert JavaRuntime._redact_build_log_line(line) == expected


class _PreparedPipeline:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.prepare_count = 0

    def create_attempt_directory(self, attempt_id: str) -> Path:
        directory = self.root / attempt_id
        directory.mkdir()
        return directory

    @staticmethod
    def cleanup_attempt_directory(directory: Path | None) -> None:
        if directory is not None and directory.exists():
            directory.rmdir()

    def prepare(self, context, request, *, attempt_directory):
        self.prepare_count += 1
        context.transition("resolving_build")
        context.transition("resolving_runtime")
        context.transition("starting_jvm")
        output = self.root / "target/classes"
        output.mkdir(parents=True, exist_ok=True)
        (output / "Application.class").write_bytes(b"compiled")
        plan = JvmLaunchPlan(
            java_executable=Path("/jdk/bin/java"),
            classpath=(output,),
            main_class="com.example.Application",
            working_directory=self.root,
            ready_port=request.ready_port,
            startup_wait_timeout_seconds=(
                request.startup_wait_timeout_seconds
            ),
        )
        return PreparedProjectLaunch(
            execution=SimpleNamespace(
                module=SimpleNamespace(output_directory=output),
                effective_pom_file=self.root / "pom.xml",
                classpath_file=self.root / "classpath.txt",
                compile_classpath_file=self.root / "compile-classpath.txt",
            ),
            runtime_jdk=SimpleNamespace(java_executable=Path("/jdk/bin/java")),
            jvm_plan=plan,
            command=MaterializedJavaCommand(
                argv=("/jdk/bin/java", "com.example.Application"),
                materialization="direct_classpath",
            ),
            warnings=(),
            attempt_directory=attempt_directory,
        )

    @staticmethod
    def materialize_command(plan, *, jdwp_port, attempt_directory):
        return plan, MaterializedJavaCommand(
            argv=(
                "/jdk/bin/java",
                "-cp",
                str(plan.classpath[0]),
                plan.main_class,
            ),
            materialization="direct_classpath",
        )


def test_project_build_status_is_nonblocking_redacted_and_cancellable(
    tmp_path: Path,
) -> None:
    entered = threading.Event()

    class Pipeline:
        @staticmethod
        def create_attempt_directory(attempt_id: str) -> Path:
            directory = tmp_path / attempt_id
            directory.mkdir()
            return directory

        @staticmethod
        def cleanup_attempt_directory(directory: Path | None) -> None:
            if directory is None:
                return
            for child in directory.iterdir():
                child.unlink()
            directory.rmdir()

        @staticmethod
        def prepare(context, _request, *, attempt_directory):
            context.transition("resolving_build")
            context.transition("compiling")
            (attempt_directory / "build.log").write_text(
                "\x1b[31mcompiling\x1b[0m\n"
                "-Dtoken=company-secret\n"
                "Authorization: Bearer company-secret\n"
                "https://user:password@example.invalid/repository\n",
                encoding="utf-8",
            )
            entered.set()
            while True:
                context.cancel_event.wait(0.01)
                context.check_cancelled()

    runtime = JavaRuntime()
    runtime._project_pipeline = Pipeline()
    result = runtime.run_project(
        RuntimeAction(action="run"),
        _request(tmp_path),
    )

    assert result.ok is True
    assert entered.wait(timeout=1)
    started = time.monotonic()
    status = runtime.status(RuntimeAction(action="status"))
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert status.ok is True
    assert status.data["launch_phase"] == "compiling"
    assert status.data["process_state"] == "absent"
    assert "startup_state" not in status.data
    lines = "".join(status.data["build"]["log_tail"]["lines"])
    assert "compiling" in lines
    assert "\x1b[" not in lines
    assert "company-secret" not in lines
    assert "<redacted>" in lines
    attempt_directory = (
        tmp_path / str(status.data["attempt_id"])
    )
    assert attempt_directory.is_dir()

    stopped = runtime.stop(RuntimeAction(action="stop"))

    assert stopped.ok is True
    assert stopped.data["launch_phase"] == "cancelled"
    assert attempt_directory.exists() is False


def test_failed_build_log_uses_same_redaction_for_both_public_tails(
    tmp_path: Path,
) -> None:
    secret_values = (
        "db-value-123",
        "api-value-123",
        "auth-value-123",
    )

    class Pipeline:
        @staticmethod
        def create_attempt_directory(attempt_id: str) -> Path:
            directory = tmp_path / attempt_id
            directory.mkdir()
            return directory

        @staticmethod
        def cleanup_attempt_directory(directory: Path | None) -> None:
            if directory is None:
                return
            for child in directory.iterdir():
                child.unlink()
            directory.rmdir()

        @staticmethod
        def prepare(context, request, *, attempt_directory):
            context.transition("resolving_build")
            context.transition("compiling")
            (attempt_directory / "build.log").write_text(
                "DB_PASSWORD=db-value-123\n"
                "API_TOKEN: api-value-123\n"
                "[ERROR] Authorization: Bearer auth-value-123\n",
                encoding="utf-8",
            )
            raise LaunchPipelineFailure(
                LaunchErrorCode.BUILD_FAILED,
                "The supervised Maven build failed.",
                retryable=True,
                suggested_next_step="Inspect the redacted build log.",
            )

    runtime = JavaRuntime()
    runtime._project_pipeline = Pipeline()
    started = runtime.run_project(
        RuntimeAction(action="run"),
        _request(tmp_path),
    )

    assert started.ok is True
    _wait_until(
        lambda: runtime._launch_controller.snapshot()["launch_phase"]
        == "failed"
    )
    failed = runtime.status(RuntimeAction(action="status"))

    nested_tail = failed.data["build"]["log_tail"]
    failure_tail = failed.data["build_log_tail"]
    assert nested_tail == failure_tail
    serialized = failed.to_json()
    assert serialized.count("<redacted>") >= 3
    for secret in secret_values:
        assert secret not in serialized
    assert runtime.stop(RuntimeAction(action="stop")).ok is True


def test_project_launch_reaches_runtime_active_and_natural_exit_is_reusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes: list[_FakeProcess] = []
    launch_commands: list[tuple[str, ...]] = []

    runtime = JavaRuntime()
    runtime._project_pipeline = _PreparedPipeline(tmp_path)

    def start(**kwargs: Any) -> ProcessInfo:
        launch_commands.append(tuple(kwargs["command_argv"]))
        process = _FakeProcess()
        processes.append(process)
        info = ProcessInfo(
            process,
            kwargs["jdwp_port"],
            kwargs["main_class"],
            ready_port=kwargs["ready_port"],
            startup_wait_timeout_seconds=(
                kwargs["startup_wait_timeout_seconds"]
            ),
        )
        runtime._proc._publish(info)
        kwargs["on_published"](info)
        return info

    monkeypatch.setattr(runtime._proc, "start", start)
    monkeypatch.setattr(
        runtime._proc,
        "observe_readiness",
        lambda process, refresh=True: {
            "process_state": (
                "running" if process.is_alive() else "exited"
            ),
            "startup_state": (
                "unverified" if process.is_alive() else "failed"
            ),
            "readiness_configured": False,
        },
    )
    monkeypatch.setattr(
        ProcessManager,
        "_stop_posix",
        staticmethod(
            lambda process: setattr(process, "returncode", -15)
        ),
    )
    monkeypatch.setattr(
        ProcessManager,
        "_stop_windows",
        staticmethod(
            lambda process: setattr(process, "returncode", -15)
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_connect",
        lambda: (_ for _ in ()).throw(RuntimeError("not needed")),
    )

    first = runtime.run_project(
        RuntimeAction(action="run"),
        _request(tmp_path),
    )
    assert first.ok is True
    _wait_until(
        lambda: runtime._launch_controller.snapshot()["launch_phase"]
        == "runtime_active"
    )
    first_status = runtime.status(RuntimeAction(action="status"))
    assert first_status.data["generation"] == 1
    assert first_status.data["compile_ready"] is False
    assert first_status.data["last_successful_startup_ms"] is not None
    assert "generation-store" in " ".join(launch_commands[0])
    assert "target/classes" not in " ".join(launch_commands[0])
    active = runtime.status(RuntimeAction(action="status"))
    assert active.data["launch_phase"] == "runtime_active"
    assert active.data["process_state"] == "running"
    assert active.data["startup_state"] == "unverified"
    attempt_id = runtime._launch_controller.snapshot()["attempt_id"]
    project_session = runtime._project_sessions[attempt_id]

    processes[0].returncode = 9
    exited = runtime.status(RuntimeAction(action="status"))
    assert exited.data["launch_phase"] == "failed"
    assert exited.data["process_state"] == "absent"
    assert exited.data["launch_error"]["error_code"] == "JVM_EXITED"
    assert exited.data["launch_error"]["exit_code"] == 9
    assert project_session.generations.runtime_state.value == "absent"
    assert project_session.generations.runtime is None

    second = runtime.run_project(
        RuntimeAction(action="run"),
        _request(tmp_path),
    )
    assert second.ok is True
    assert second.data["generation"] == first.data["generation"] + 1
    _wait_until(
        lambda: runtime._launch_controller.snapshot()["launch_phase"]
        == "runtime_active"
    )
    assert runtime.stop(RuntimeAction(action="stop")).ok is True


def test_project_jvm_start_failure_is_retryable_and_clears_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = JavaRuntime()
    runtime._project_pipeline = _PreparedPipeline(tmp_path)

    def failed_start(**kwargs: Any) -> ProcessInfo:
        process = _FakeProcess()
        info = ProcessInfo(
            process,
            kwargs["jdwp_port"],
            kwargs["main_class"],
        )
        runtime._proc._publish(info)
        kwargs["on_published"](info)
        process.returncode = 7
        runtime._proc.stop_target(info)
        raise ProcessStartupError(
            "Process exited with code 7 before JDWP became ready.",
            failure_type="process_exited_before_jdwp",
            exit_code=7,
            cleanup_settled=True,
        )

    monkeypatch.setattr(runtime._proc, "start", failed_start)

    started = runtime.run_project(
        RuntimeAction(action="run"),
        _request(tmp_path),
    )
    assert started.ok is True
    _wait_until(
        lambda: runtime._launch_controller.snapshot()["launch_phase"]
        == "failed"
    )
    failed = runtime.status(RuntimeAction(action="status"))

    assert failed.data["process_state"] == "absent"
    assert failed.data["launch_error"]["error_code"] == "JVM_START_FAILED"
    assert (
        failed.data["launch_error"]["failure_type"]
        == "process_exited_before_jdwp"
    )
    assert failed.data["launch_error"]["exit_code"] == 7
    assert runtime._proc.current is None


def test_project_restart_uses_current_generation_without_recompiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = JavaRuntime()
    pipeline = _PreparedPipeline(tmp_path)
    runtime._project_pipeline = pipeline
    commands: list[tuple[str, ...]] = []

    def start(**kwargs: Any) -> ProcessInfo:
        process = _FakeProcess()
        commands.append(tuple(kwargs["command_argv"]))
        info = ProcessInfo(
            process,
            kwargs["jdwp_port"],
            kwargs["main_class"],
            ready_port=kwargs["ready_port"],
            startup_wait_timeout_seconds=(
                kwargs["startup_wait_timeout_seconds"]
            ),
        )
        runtime._proc._publish(info)
        kwargs["on_published"](info)
        return info

    monkeypatch.setattr(runtime._proc, "start", start)
    monkeypatch.setattr(
        runtime._proc,
        "observe_readiness",
        lambda process, refresh=True: {
            "process_state": "running" if process.is_alive() else "exited",
            "startup_state": "unverified" if process.is_alive() else "failed",
            "readiness_configured": False,
        },
    )
    monkeypatch.setattr(
        ProcessManager,
        "_stop_posix",
        staticmethod(lambda process: setattr(process, "returncode", -15)),
    )
    monkeypatch.setattr(
        ProcessManager,
        "_stop_windows",
        staticmethod(lambda process: setattr(process, "returncode", -15)),
    )

    runtime.run_project(RuntimeAction(action="run"), _request(tmp_path))
    _wait_until(
        lambda: runtime._launch_controller.snapshot()["launch_phase"]
        == "runtime_active"
    )
    initial_status = runtime.status(RuntimeAction(action="status"))
    assert initial_status.data["generation"] == 1

    restarted = runtime.restart(RuntimeAction(action="restart"))
    assert restarted.ok is True
    assert restarted.data["status"] == "restarting"
    assert restarted.data["applied"] is None
    _wait_until(
        lambda: runtime._launch_controller.snapshot()["launch_phase"]
        == "runtime_active"
    )

    assert pipeline.prepare_count == 1
    assert len(commands) == 2
    assert "generation-store" in " ".join(commands[0])
    assert "generation-store" in " ".join(commands[1])
    final_status = runtime.status(RuntimeAction(action="status"))
    assert final_status.data["generation"] == 1
    assert runtime.stop(RuntimeAction(action="stop")).ok is True


@pytest.mark.parametrize(
    "candidate_outcome",
    ["success", "start_failure", "promotion_failure"],
)
def test_candidate_restart_promotes_or_rolls_back_last_good_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_outcome: str,
) -> None:
    runtime = JavaRuntime()
    pipeline = _PreparedPipeline(tmp_path)
    runtime._project_pipeline = pipeline
    start_count = 0

    def start(**kwargs: Any) -> ProcessInfo:
        nonlocal start_count
        start_count += 1
        process = _FakeProcess()
        info = ProcessInfo(
            process,
            kwargs["jdwp_port"],
            kwargs["main_class"],
            ready_port=kwargs["ready_port"],
            startup_wait_timeout_seconds=(
                kwargs["startup_wait_timeout_seconds"]
            ),
        )
        runtime._proc._publish(info)
        kwargs["on_published"](info)
        if start_count == 2 and candidate_outcome == "start_failure":
            process.returncode = 7
            raise ProcessStartupError(
                "candidate failed",
                failure_type="process_exited_before_jdwp",
                exit_code=7,
                cleanup_settled=True,
            )
        return info

    monkeypatch.setattr(runtime._proc, "start", start)
    monkeypatch.setattr(
        runtime._proc,
        "observe_readiness",
        lambda process, refresh=True: {
            "process_state": "running" if process.is_alive() else "exited",
            "startup_state": "unverified" if process.is_alive() else "failed",
            "readiness_configured": False,
        },
    )
    monkeypatch.setattr(
        ProcessManager,
        "_stop_posix",
        staticmethod(lambda process: setattr(process, "returncode", -15)),
    )
    monkeypatch.setattr(
        ProcessManager,
        "_stop_windows",
        staticmethod(lambda process: setattr(process, "returncode", -15)),
    )

    runtime.run_project(RuntimeAction(action="run"), _request(tmp_path))
    _wait_until(
        lambda: runtime._launch_controller.snapshot()["launch_phase"]
        == "runtime_active"
    )
    old_attempt = runtime._launch_controller.snapshot()["attempt_id"]
    session = runtime._project_sessions[old_attempt]
    candidate_output = tmp_path / "candidate-output"
    candidate_output.mkdir()
    (candidate_output / "Application.class").write_bytes(b"candidate")
    session.generations.prepare_candidate(
        candidate_output,
        build_world_fingerprint=session.build_world_fingerprint,
    )
    session.begin_reload("source-v2")
    session.transition_reload(ReloadStage.PREPARING_CANDIDATE)
    session.transition_reload(ReloadStage.RESTARTING)
    if candidate_outcome == "promotion_failure":
        monkeypatch.setattr(
            session.generations,
            "promote_candidate",
            lambda **_kwargs: (_ for _ in ()).throw(
                OSError("state persistence unavailable")
            ),
        )

    result = runtime._restart_candidate_project(RuntimeAction(action="update"))
    assert result.ok is True
    _wait_until(
        lambda: runtime._launch_controller.snapshot()["launch_phase"]
        == "runtime_active"
    )

    status = runtime.status(RuntimeAction(action="status"))
    if candidate_outcome == "success":
        assert status.data["generation"] == 2
        assert session.generations.current.output_directory.joinpath(
            "Application.class"
        ).read_bytes() == b"candidate"
        assert session.public_status()["last_reload"]["applied"] is True
        assert start_count == 2
    else:
        assert status.data["generation"] == 1
        assert session.generations.candidate is None
        assert session.public_status()["last_reload"]["applied"] is False
        assert session.public_status()["last_reload"]["rolled_back"] is True
        assert start_count == 3
        if candidate_outcome == "promotion_failure":
            assert session.public_status()["last_reload"]["error_code"] == (
                "GENERATION_PROMOTION_FAILED"
            )
    assert runtime.stop(RuntimeAction(action="stop")).ok is True
