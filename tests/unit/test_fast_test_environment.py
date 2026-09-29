from pathlib import Path

import pytest

from jolink_runtime.core.dispatcher import (
    parse_project_launch_request,
    parse_runtime_action,
)
from jolink_runtime.launch.build_environment import (
    apply_build_preferences,
    maven_local_repository,
)
from jolink_runtime.launch.gradle_probe import ProductGradleProbe
from jolink_runtime.launch.idea_environment import (
    IdeaBuildPreferences,
    build_preferences_key,
)
from jolink_runtime.launch.maven_probe import MavenProbeError
from jolink_runtime.launch.toolchain import JavaToolchainResolver, MavenToolCandidate


def test_explicit_test_choices_do_not_mutate_project_preferences(tmp_path):
    java = tmp_path / "Java 17"
    maven = tmp_path / "Maven 安装"
    java.mkdir()
    maven.mkdir()
    settings = tmp_path / "settings.xml"
    settings.write_text("<settings/>")
    original = IdeaBuildPreferences(project_jdk_name="old", active_profiles=("dev",))
    selected = apply_build_preferences(original, build_java_home=java, maven_home=maven, maven_settings=settings)
    assert original.maven_runner_jdk_name is None
    assert selected.active_profiles == original.active_profiles
    assert selected.custom_maven_home == maven
    assert selected.user_settings_file == settings
    assert build_preferences_key(selected.cache_values(), "maven") != build_preferences_key(original.cache_values(), "maven")
    candidates = JavaToolchainResolver().candidates(preferences=selected, explicit_reference=None, for_build=True)
    assert [candidate.home for candidate in candidates] == [java]
    assert apply_build_preferences(original) == original


def test_public_test_arguments_reach_action(tmp_path):
    args = {"action": "test", "project_path": str(tmp_path), "tests": ["T"],
            "java_home": str(tmp_path), "maven_home": str(tmp_path), "maven_settings": "settings.xml"}
    assert parse_project_launch_request(args) is None
    action = parse_runtime_action(args)
    for name in ("java_home", "maven_home", "maven_settings"):
        assert getattr(action, name) == args[name]


@pytest.mark.parametrize("namespace", ["", ' xmlns="http://maven.apache.org/SETTINGS/1.2.0"'])
def test_local_repository_precedence_and_interpolation(tmp_path, monkeypatch, namespace):
    home = tmp_path / "maven"
    (home / "conf").mkdir(parents=True)
    global_settings = home / "conf/settings.xml"
    global_settings.write_text(f"<settings{namespace}><localRepository>{tmp_path}/global</localRepository></settings>")
    settings = tmp_path / "user.xml"
    settings.write_text(f"<settings{namespace}><localRepository>${{env.REPO_ROOT}}/local</localRepository></settings>")
    monkeypatch.setenv("REPO_ROOT", str(tmp_path))
    maven = MavenToolCandidate(argv_prefix=(str(home / "bin/mvn"),), source="explicit", home=home)
    preferences = IdeaBuildPreferences()
    assert maven_local_repository(preferences, maven, settings) == tmp_path / "local"
    assert maven_local_repository(IdeaBuildPreferences(local_repository=tmp_path / "idea"), maven, settings) == tmp_path / "idea"
    settings.write_text("<settings/>")
    assert maven_local_repository(preferences, maven, settings) == tmp_path / "global"
    assert maven_local_repository(preferences, maven, None) == tmp_path / "global"
    global_settings.unlink()
    assert maven_local_repository(preferences, maven, None) == Path.home() / ".m2/repository"


def test_invalid_explicit_path_is_not_silently_ignored(tmp_path):
    with pytest.raises(FileNotFoundError):
        apply_build_preferences(IdeaBuildPreferences(), build_java_home=tmp_path / "missing")


def test_malformed_settings_report_a_configuration_error(tmp_path):
    settings = tmp_path / "settings.xml"
    settings.write_text("<settings>")
    maven = MavenToolCandidate(argv_prefix=("mvn",), source="PATH", home=None)
    with pytest.raises(MavenProbeError) as error:
        maven_local_repository(IdeaBuildPreferences(), maven, settings)
    assert error.value.error_code == "MAVEN_SETTINGS_UNSUPPORTED"


def test_gradle_probe_does_not_share_an_owned_daemon(tmp_path):
    probe = ProductGradleProbe.load()
    prepared = probe.prepare(tmp_path)
    command = probe.command(wrapper=tmp_path / "gradlew", prepared=prepared, offline=True)
    assert "--no-daemon" in command
    assert "--offline" in command
    assert not {"classes", "testClasses", "test"}.intersection(command)
