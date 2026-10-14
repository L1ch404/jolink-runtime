"""Application JDK selection is shared by direct launches and project restarts."""

import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_project_restart_service import finish, running  # noqa: F401 - shared real coordinator fixture

from jolink_runtime.core.dispatcher import (
    ProjectLaunchArgumentError,
    parse_project_launch_request,
    parse_runtime_action,
)
from jolink_runtime.core.models import RuntimeResult
from jolink_runtime.launch.direct_launch import direct_restart_action
from jolink_runtime.launch.project_launcher import ProjectLaunchRequest
from jolink_runtime.launch.project_restart import (
    application_jdk_changed,
    start_project_restart,
)
from jolink_runtime.launch.toolchain import java_executable_path


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "run", "main_class": "App", "classpath": "."},
        {"action": "run", "jar_path": "app.jar"},
        {"action": "restart"},
    ],
)
def test_java_home_no_longer_requires_project_path(arguments):
    args = {**arguments, "java_home": "JDK with spaces"}
    assert parse_project_launch_request(args) is None
    assert parse_runtime_action(args).java_home == "JDK with spaces"


@pytest.mark.parametrize("value", ["", "  ", None, 17])
def test_empty_or_non_string_java_home_remains_an_error(value):
    with pytest.raises(ProjectLaunchArgumentError) as error:
        parse_project_launch_request({"action": "restart", "java_home": value})
    assert error.value.payload["argument"] == "java_home"


@pytest.mark.parametrize(
    "field", ["launch_name", "maven_home", "maven_settings", "build_java_home"]
)
def test_build_settings_still_require_project_path(field):
    with pytest.raises(ProjectLaunchArgumentError):
        parse_project_launch_request(
            {"action": "run", "main_class": "App", field: "setting"}
        )


@pytest.mark.parametrize("windows,suffix", [(False, "java"), (True, "java.exe")])
def test_application_executable_is_derived_from_the_explicit_home(
    tmp_path, monkeypatch, windows, suffix
):
    monkeypatch.setattr("jolink_runtime.launch.toolchain._IS_WINDOWS", windows)
    monkeypatch.chdir(tmp_path)
    assert java_executable_path("JDK 8 中文") == tmp_path / "JDK 8 中文/bin" / suffix


def test_direct_restart_inherits_and_overrides_without_dropping_metadata():
    previous = parse_runtime_action(
        {
            "action": "run",
            "main_class": "App",
            "classpath": "classes",
            "java_home": "jdk-a",
            "ready_port": 8080,
            "jdwp_port": 5009,
            "timeout": 7,
            "app_args": ["old"],
            "vm_args": ["-Dflag=old"],
        }
    )
    inherited = direct_restart_action(
        parse_runtime_action({"action": "restart"}), previous, None
    )
    assert inherited is not previous and inherited.java_home == "jdk-a"
    assert inherited.main_class == "App" and inherited.classpath == "classes"
    assert inherited.ready_port == 8080 and inherited.startup_wait_timeout_seconds == 7
    assert inherited.jdwp_port == 5009 and inherited.app_args == ["old"]
    requested = parse_runtime_action(
        {
            "action": "restart",
            "java_home": "jdk-b",
            "app_args": [],
            "vm_args": ["-Dflag=new"],
            "jdwp_port": 5010,
            "ready_port": 8081,
            "timeout": 2,
        }
    )
    changed = direct_restart_action(requested, previous, None)
    assert changed.java_home == "jdk-b" and changed.app_args == []
    assert changed.vm_args == ["-Dflag=new"] and changed.jdwp_port == 5010
    assert changed.ready_port == 8081 and changed.startup_wait_timeout_seconds == 2
    assert (
        previous.java_home == "jdk-a"
        and previous.ready_port == 8080
        and previous.app_args == ["old"]
    )
    explicit_target = parse_runtime_action({"action": "restart", "jar_path": "new.jar"})
    assert direct_restart_action(explicit_target, previous, None).java_home == "jdk-a"


def request(root, home):
    return ProjectLaunchRequest(root, None, 5005, 0, 30, java_home=home)


def test_same_or_symlinked_java_executable_does_not_force_restart(tmp_path):
    home = tmp_path / "jdk"
    home.mkdir()
    prepared = SimpleNamespace(
        request=request(tmp_path, home),
        jvm_plan=SimpleNamespace(java_executable=java_executable_path(home)),
    )
    assert not application_jdk_changed(prepared)
    prepared.request = replace(prepared.request, java_home=tmp_path / "different")
    assert application_jdk_changed(prepared)
    try:
        (tmp_path / "alias").symlink_to(home, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    prepared.request = replace(prepared.request, java_home=tmp_path / "alias")
    assert not application_jdk_changed(prepared)


@pytest.mark.parametrize("changed", [True, False])
def test_project_restart_selects_real_restart_only_when_jdk_changes(
    running, tmp_path, changed  # noqa: F811 - pytest requests the imported fixture
):
    first = tmp_path / "jdk-a"
    requested = tmp_path / ("jdk-b" if changed else "jdk-a")
    runtime = running.runtime
    runtime._last_project_request = request(tmp_path, first)
    runtime._project_update_plans["active"] = replace(
        runtime._project_update_plans["active"],
        request=runtime._last_project_request,
        jvm_plan=SimpleNamespace(java_executable=java_executable_path(first)),
    )
    result = finish(running, java_home=str(requested))
    assert result["ok"], result
    running.compiler.compile.assert_called_once_with(running.sources)
    if changed:
        assert (
            result["apply_method"] == "restart"
            and result["restart_reason"] == "APPLICATION_JDK_CHANGED"
        )
        running.jdwp.redefine_classes.assert_not_called()
        assert (
            running.replace.call_args.kwargs["prepared"].request.java_home == requested
        )
    else:
        assert result["apply_method"] == "hotswap"
        running.replace.assert_not_called()
        assert runtime._last_project_request.java_home == requested
        assert runtime._project_update_plans["active"].request.java_home == requested


def test_unavailable_project_session_forwards_java_home_to_existing_launch_path(
    tmp_path,
):
    previous = request(tmp_path, tmp_path / "old")
    runtime = SimpleNamespace(
        _last_project_request=previous,
        _project_state_lock=threading.RLock(),
        _launch_controller=SimpleNamespace(snapshot=lambda: {"attempt_id": "stopped"}),
        _project_update_plans={},
        _project_sessions={},
        restart_project=Mock(return_value=RuntimeResult(ok=True, data={})),
    )
    result = start_project_restart(
        runtime,
        parse_runtime_action({"action": "restart", "java_home": str(tmp_path / "new")}),
    )
    assert result.ok and result.data["apply_method"] == "restart"
    assert runtime.restart_project.call_args.args[1].java_home == tmp_path / "new"
    assert previous.java_home == tmp_path / "old"
