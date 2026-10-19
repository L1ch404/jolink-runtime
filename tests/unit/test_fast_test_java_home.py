"""Build-JDK identity is persistent; an explicit test JVM belongs to one run."""

from dataclasses import replace
from types import SimpleNamespace

import anyio
import pytest
from test_build_configuration import model as shared_model
from test_fast_test_model_identity import active_project
from test_test_execution import execution
from test_test_execution import project as execution_project

from jolink_runtime.core.dispatcher import Dispatcher
from jolink_runtime.launch.fast_test_manager import FastTestManager
from jolink_runtime.launch.fast_test_manager import TestAttempt as Attempt
from jolink_runtime.launch.idea_environment import IdeaBuildPreferences
from jolink_runtime.launch.process_supervisor import AttemptToken
from jolink_runtime.launch.test_execution import run_test_executions
from jolink_runtime.launch.toolchain import java_executable_path
from jolink_runtime.server.mcp_server import RuntimeMCPBoundary

model = shared_model
project = execution_project


@pytest.mark.parametrize("explicit_action", [False, True])
def test_public_jdk_arguments_reach_attempt(tmp_path, monkeypatch, explicit_action):
    dispatcher = Dispatcher()
    manager = dispatcher.sessions.get_runtime()._fast_tests
    attempts = []

    def run(attempt):
        attempts.append(attempt)
        attempt.state = "completed"
        attempt.result = {"ok": True, "passed": True}
        attempt.done.set()

    monkeypatch.setattr(manager, "_run_attempt", run)
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario():
        args = {
            "project_path": str(tmp_path),
            "tests": ["example.Test"],
            "build_java_home": str(tmp_path / "build"),
            "java_home": str(tmp_path / "test"),
        }
        if explicit_action:
            args["action"] = "run"
        result = await boundary.call_tool("java_fast_test", args)
        assert result.structuredContent["passed"], result
        assert "ignored_arguments" not in result.structuredContent
        assert attempts[0].build_java_home == args["build_java_home"]
        assert attempts[0].java_home == args["java_home"]

    try:
        anyio.run(scenario)
    finally:
        dispatcher.close_all_sessions()


@pytest.mark.parametrize("value", ["", None, 17, []])
def test_invalid_build_home_does_not_start_runtime(value, tmp_path):
    dispatcher = Dispatcher()
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario():
        result = await boundary.call_tool(
            "java_fast_test",
            {
                "project_path": str(tmp_path),
                "tests": ["example.Test"],
                "build_java_home": value,
            },
        )
        assert result.isError
        assert result.structuredContent["argument"] == "build_java_home"
        assert dispatcher.sessions.session_keys == ()

    anyio.run(scenario)


@pytest.mark.parametrize(
    "build,test,expected",
    [
        ("", "", "idea"),
        ("", "test", "test"),
        ("build", "", "build"),
        ("build", "test", "build"),
    ],
)
def test_build_preference_defaults_and_override(
    tmp_path, monkeypatch, build, test, expected
):
    for name in ("idea", "build", "test"):
        (tmp_path / name).mkdir()
    original = IdeaBuildPreferences(maven_runner_jdk_name=str(tmp_path / "idea"))
    manager = FastTestManager()
    monkeypatch.setattr(manager._idea, "import_preferences", lambda _: original)
    attempt = Attempt(
        "test",
        1,
        AttemptToken("test", 1),
        tmp_path,
        (),
        ("T",),
        300,
        java_home=str(tmp_path / test) if test else "",
        build_java_home=str(tmp_path / build) if build else "",
    )
    selected = manager._preferences(attempt, tmp_path)
    assert selected.maven_runner_jdk_name == str(tmp_path / expected)
    assert original.maven_runner_jdk_name == str(tmp_path / "idea")


def test_runner_jdk_changes_reuse_live_and_disk_models(model, monkeypatch):
    manager = FastTestManager()
    manager._cache = model.fast
    monkeypatch.setattr(
        manager._idea, "import_preferences", lambda _: IdeaBuildPreferences()
    )
    preferences = IdeaBuildPreferences(
        maven_runner_jdk_name=str(model.toolchain.home)
    ).cache_values()
    world = replace(model.world, build_preferences=preferences)
    live, attempt, closed = active_project(model, manager, monkeypatch, world)
    attempt.build_java_home = str(model.toolchain.home)
    for home in ("test8", "test17", ""):
        attempt.java_home = str(model.root / home) if home else ""
        assert manager._ensure_project(attempt) is live
        effective = manager._preferences(attempt, model.root).cache_values()
        restored, _ = model.fast.load(model.root, "maven", build_preferences=effective)
        assert restored.test_java_executable == model.world.test_java_executable
    assert not closed
    other = model.root / "other-build-jdk"
    other.mkdir()
    attempt.build_java_home = str(other)
    replacement = object()
    calls = []
    monkeypatch.setattr(
        manager,
        "_bootstrap_maven",
        lambda attempt: calls.append(attempt) or replacement,
    )
    assert manager._ensure_project(attempt) is replacement
    assert calls == [attempt] and closed == [True]


@pytest.mark.parametrize("system", ["maven", "gradle"])
@pytest.mark.parametrize("configured", [False, True])
def test_runner_override_is_per_call_and_wins_over_project(
    project, tmp_path, system, configured
):
    project.build_system = system
    native_java = project.test_java_executable
    if system == "maven":
        project.test_executions = (
            execution(
                extra=f"<jvm>{tmp_path}/native/bin/java</jvm>" if configured else ""
            ),
        )
        if configured:
            native_java = tmp_path / "native/bin/java"
    elif configured:
        project.test_java_executable = native_java = tmp_path / "launcher/bin/java"
    calls = []
    runner = SimpleNamespace(run=lambda **kwargs: calls.append(kwargs) or object())
    attempt = SimpleNamespace(
        tests=("x.Test",),
        timeout_seconds=30,
        owner=object(),
        output_logs=(),
        java_home=str(tmp_path / "测试 JDK"),
        require_not_cancelled=lambda: None,
    )
    original = project.test_java_executable
    run_test_executions(runner, project, attempt, tmp_path / "first")
    assert calls[-1]["java_executable"] == java_executable_path(attempt.java_home)
    attempt.java_home = ""
    run_test_executions(runner, project, attempt, tmp_path / "second")
    assert calls[-1]["java_executable"] == native_java
    assert project.test_java_executable == original


def test_each_execution_receives_same_explicit_jdk(project, tmp_path):
    project.test_executions = (
        execution(extra=f"<jvm>{tmp_path}/unit-java</jvm>"),
        execution("it", "**/*IT.java", extra=f"<jvm>{tmp_path}/it-java</jvm>"),
    )
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        # Stop after observing both groups; aggregation is covered separately.
        if len(calls) == 2:
            raise RuntimeError("observed both groups")
        return object()

    attempt = SimpleNamespace(
        tests=("x.FooTest", "x.FooIT"),
        timeout_seconds=30,
        owner=object(),
        output_logs=(),
        java_home=str(tmp_path / "jdk"),
        require_not_cancelled=lambda: None,
    )
    with pytest.raises(RuntimeError, match="observed both"):
        run_test_executions(SimpleNamespace(run=run), project, attempt, tmp_path)
    assert [c["java_executable"] for c in calls] == [
        java_executable_path(attempt.java_home)
    ] * 2
