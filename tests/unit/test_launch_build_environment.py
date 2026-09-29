from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_build_configuration import model as shared_model

from jolink_runtime.core.dispatcher import (
    ProjectLaunchArgumentError,
    parse_project_launch_request,
)
from jolink_runtime.launch.build_environment import apply_build_preferences
from jolink_runtime.launch.idea_environment import (
    IdeaBuildPreferences,
    IdeaEnvironmentImporter,
)
from jolink_runtime.launch.jdt_launch_service import JdtLaunchService
from jolink_runtime.launch.toolchain import MavenToolResolver
from jolink_runtime.server.tool_schema import get_mcp_tools

model = shared_model


def test_launch_build_arguments_are_separate_from_application_jdk(tmp_path):
    request = parse_project_launch_request({"action": "run", "project_path": str(tmp_path), "main_class": "App",
        "java_home": str(tmp_path / "jdk8"), "build_java_home": str(tmp_path / "jdk17"),
        "maven_home": str(tmp_path / "maven"), "maven_settings": str(tmp_path / "settings.xml")})
    assert request.java_home == tmp_path / "jdk8"
    assert request.build_java_home == tmp_path / "jdk17"
    assert request.maven_home == tmp_path / "maven"
    assert request.maven_settings == tmp_path / "settings.xml"
    assert replace(request, ready_port=8080).build_java_home == request.build_java_home


@pytest.mark.parametrize("field", ["build_java_home", "maven_home", "maven_settings"])
def test_build_choices_require_a_project_launch(field):
    with pytest.raises(ProjectLaunchArgumentError) as error:
        parse_project_launch_request({"action": "run", "jar_path": "app.jar", field: "/some/path"})
    assert error.value.payload["argument"] == field


def test_launch_and_test_share_maven_parameter_schema_without_exposing_it_to_debugger():
    tools = {tool.name: tool.inputSchema["properties"] for tool in get_mcp_tools()}
    for name in ("maven_home", "maven_settings"):
        assert tools["java_application"][name] == tools["java_fast_test"][name]
        assert name not in tools["java_debugger"]
    assert "build_java_home" in tools["java_application"]
    assert "build_java_home" not in tools["java_debugger"]


def test_explicit_maven_does_not_fall_back_to_wrapper_or_path(tmp_path):
    (tmp_path / "mvnw").touch()
    home = tmp_path / "explicit"
    candidates = MavenToolResolver().candidates(project_root=tmp_path, preferences=IdeaBuildPreferences(), explicit_home=home)
    assert len(candidates) == 1
    assert candidates[0].home == home
    assert candidates[0].source == "explicit_maven"


def test_restart_compares_current_inputs_with_retained_explicit_choices(model, monkeypatch):
    jdk = model.root / "build-jdk"
    jdk.mkdir()
    maven = model.root / "maven"
    maven.mkdir()
    settings = model.root / "settings.xml"
    settings.write_text("<settings/>")
    request = SimpleNamespace(build_java_home=jdk, maven_home=maven, maven_settings=settings)
    preferences = IdeaBuildPreferences()
    selected = apply_build_preferences(preferences, **vars(request))
    plan = replace(model.plan, configuration_stamps=model.fast.input_snapshot(model.world))
    retained = SimpleNamespace(request=request, jdt_build_world_plan=plan, build_system="maven",
                               build_preferences_identity=selected.cache_values())
    monkeypatch.setattr(IdeaEnvironmentImporter, "import_preferences", lambda self, root: preferences)
    assert JdtLaunchService.configuration_rejection(retained) is None
    # Changes to overridden IDE choices do not defeat the explicit launch.
    preferences = replace(preferences, custom_maven_home=model.root / "other", maven_runner_jdk_name="other")
    assert JdtLaunchService.configuration_rejection(retained) is None
    # Profiles were not overridden: their actual changes must still be noticed.
    preferences = replace(preferences, active_profiles=("different",))
    assert JdtLaunchService.configuration_rejection(retained).data["error_code"] == "BUILD_CONFIGURATION_CHANGED"
