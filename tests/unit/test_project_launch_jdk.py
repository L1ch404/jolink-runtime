"""Current launch settings must win over a persisted compilation model."""

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from jolink_runtime.core.dispatcher import parse_project_launch_request, ProjectLaunchArgumentError
from jolink_runtime.launch.contracts import JvmLaunchPlan
from jolink_runtime.launch.controller import LaunchPipelineFailure
from jolink_runtime.launch.idea_environment import IdeaBuildPreferences, IdeaEnvironmentImporter
from jolink_runtime.launch.jdt_compile_session import JdtBuildWorldPlan
from jolink_runtime.launch.project_launch_cache import ProjectLaunchCache
from jolink_runtime.launch.project_launcher import ProjectLaunchPipeline, ProjectLaunchRequest
from jolink_runtime.launch.toolchain import JavaToolchainResolver


class Context:
    def __init__(self):
        self.operations = []

    def check_cancelled(self): pass
    def set_intent(self, value): self.intent = value
    def transition(self, value): pass
    def set_build_plan(self, value): self.build = value
    def set_jvm_launch_plan(self, value): self.jvm = value

    def run_operation(self, spec):
        self.operations.append(spec.operation_name)
        assert spec.operation_name == "runtime_java_probe", "must not rerun Maven/Gradle"
        return SimpleNamespace(succeeded=True)


def fake_jdk(home, major):
    suffix = ".exe" if os.name == "nt" else ""
    (home / "bin").mkdir(parents=True)
    for name in ("java", "javac"):
        (home / "bin" / (name + suffix)).write_bytes(b"")
    (home / "release").write_text(f'JAVA_VERSION="{major}.0.1"\n')
    return JavaToolchainResolver._to_candidates([(home, "JAVA_HOME")])[0]


def idea_launch(project, jre=None, args="", vm_args="", main="example.App"):
    directory = project / ".idea"
    directory.mkdir(exist_ok=True)
    jre_xml = (f'<option name="ALTERNATIVE_JRE_PATH" value="{jre}"/>'
               '<option name="ALTERNATIVE_JRE_PATH_ENABLED" value="true"/>') if jre else ""
    (directory / "workspace.xml").write_text(
        '<project><component name="RunManager"><configuration name="App" type="Application">'
        f'<option name="MAIN_CLASS_NAME" value="{main}"/>{jre_xml}'
        f'<option name="PROGRAM_PARAMETERS" value="{args}"/>'
        f'<option name="VM_PARAMETERS" value="{vm_args}"/>'
        '</configuration></component></project>'
    )


@pytest.fixture
def cached_launch(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pom.xml").write_text("<project/>")
    jdk8 = fake_jdk(tmp_path / "jdk8", 8)
    jdk17 = fake_jdk(tmp_path / "jdk17", 17)
    monkeypatch.setenv("JAVA_HOME", str(jdk17.home))
    config = tmp_path / "ide-config/options"
    config.mkdir(parents=True)
    (config / "jdk.table.xml").write_text(
        '<application><component name="ProjectJdkTable"><jdk>'
        f'<name value="1.8"/><type value="JavaSDK"/><homePath value="{jdk8.home}"/>'
        '</jdk></component></application>'
    )
    idea_launch(project)
    cache = ProjectLaunchCache(tmp_path / "cache")
    pipeline = ProjectLaunchPipeline(
        launch_cache=cache,
        idea_environment=IdeaEnvironmentImporter(idea_config_roots=(config.parent,)),
    )
    request = ProjectLaunchRequest(project, "App", 5005, 0, 1)
    intent = pipeline._launch_intent(request).intent
    plan = JdtBuildWorldPlan(
        project_root=project, module_root=project, source_roots=(), dependency_entries=(),
        processor_entries=(), lombok_entries=(), target_java_home=jdk8.home,
        source_encoding="UTF-8", source_level=8, target_level=8, fingerprint="unchanged",
        configuration_inputs=(project / "pom.xml",), configuration_environment_names=(),
        javac_executable=jdk17.javac_executable,
    )
    jvm = JvmLaunchPlan(java_executable=jdk17.java_executable, classpath=(project / "target/classes",),
                        main_class=intent.main_class, working_directory=project,
                        jvm_args=("-Dold=true",), program_args=("old",), environment_overrides={"OLD": "old"})
    def save(system="maven"):
        cache.save(project_root=project, intent=intent, build_system=system, build_offline=False,
                   build_jdk=jdk17, runtime_jdk=jdk17, module_output=project / "target/classes",
                   generation_input_roots=(project / "target/classes",), resource_source_roots=(),
                   jvm_plan=jvm, jdt_plan=plan)
    save()
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    return SimpleNamespace(project=project, pipeline=pipeline, request=request, attempt=attempt,
                           jdk8=jdk8, jdk17=jdk17, config=config, save=save, cache=cache)


@pytest.mark.parametrize("system", ["maven", "gradle"])
@pytest.mark.parametrize("choice", ["target", "idea_named", "idea_project", "explicit", "explicit_override"])
def test_cached_world_uses_current_runtime_choice_and_arguments(cached_launch, system, choice):
    f = cached_launch
    f.save(system)
    request = replace(f.request, build_system=system)
    if system == "gradle":
        (f.project / "build.gradle").write_text("plugins { id 'java' }")
        (f.project / "gradlew").write_text("#!/bin/sh\n")
    if choice in {"idea_named", "explicit_override"}:
        idea_launch(f.project, "1.8")
    if choice == "idea_project":
        (f.project / ".idea/misc.xml").write_text('<project><component name="ProjectRootManager" project-jdk-name="1.8"/></project>')
    expected = f.jdk8
    if choice in {"explicit", "explicit_override"}:
        request = replace(request, java_home=f.jdk17.home)
        expected = f.jdk17
    request = replace(request, app_args=("new",), vm_args=("-Dnew=true",))
    context = Context()
    prepared = f.pipeline.prepare(context, request, attempt_directory=f.attempt)
    assert prepared.probe_cache_reused is True
    assert prepared.runtime_jdk.home == expected.home
    assert prepared.jvm_plan.java_executable == expected.java_executable
    assert prepared.jvm_plan.jvm_args == ("-Dnew=true",)
    assert prepared.jvm_plan.program_args == ("new",)
    assert prepared.jvm_plan.environment_overrides == {}
    assert prepared.jdt_build_world_plan.fingerprint == "unchanged"
    assert prepared.build_jdk.home == f.jdk17.home
    assert context.operations == ([] if expected == f.jdk17 else ["runtime_java_probe"])
    # The new JDK is persisted too; the next invocation should not re-probe it.
    f.pipeline.save_cache(prepared, request)
    again = Context()
    reopened = f.pipeline.prepare(again, request, attempt_directory=f.attempt)
    assert reopened.runtime_jdk.home == expected.home
    assert again.operations == []


def test_cached_world_observes_sdk_table_remapping(cached_launch):
    f = cached_launch
    idea_launch(f.project, "1.8")
    first = f.pipeline.prepare(Context(), f.request, attempt_directory=f.attempt)
    f.pipeline.save_cache(first, f.request)
    table = f.config / "jdk.table.xml"
    table.write_text(table.read_text().replace(str(f.jdk8.home), str(f.jdk17.home)))
    second = f.pipeline.prepare(Context(), f.request, attempt_directory=f.attempt)
    assert second.runtime_jdk.home == f.jdk17.home
    assert second.probe_cache_reused


def test_missing_explicit_jdk_is_not_hidden_by_cached_jdk(cached_launch):
    f = cached_launch
    request = replace(f.request, java_home=f.project / "missing-jdk")
    with pytest.raises(LaunchPipelineFailure) as error:
        f.pipeline.prepare(Context(), request, attempt_directory=f.attempt)
    assert error.value.error_code == "JAVA_TOOLCHAIN_NOT_FOUND"


def test_changed_entry_does_not_reuse_wrong_module_world(cached_launch):
    f = cached_launch
    idea_launch(f.project, main="other.App")
    intent = f.pipeline._launch_intent(f.request).intent
    assert f.cache.load(project_root=f.project, intent=intent, build_system="maven",
                        ready_port=0, startup_wait_timeout_seconds=1) is None


def test_headless_intent_and_explicit_empty_overrides(tmp_path):
    pipeline = ProjectLaunchPipeline()
    request = parse_project_launch_request(dict(action="run", project_path=str(tmp_path),
        main_class="example.App", java_home=str(tmp_path / "jdk"), app_args=[], vm_args=[]))
    intent = pipeline._launch_intent(request).intent
    assert intent.source == "arguments"
    assert intent.launch_name == intent.main_class == "example.App"
    assert intent.working_directory == tmp_path
    assert intent.runtime_jdk_reference == str(tmp_path / "jdk")
    assert not (tmp_path / ".idea").exists()
    idea_launch(tmp_path, args="old", vm_args="-Dold=true")
    intent = pipeline._launch_intent(replace(request, launch_name="App")).intent
    assert intent.program_args == intent.jvm_args == ()


@pytest.mark.parametrize("arguments,argument", [
    ({"java_home": "jdk"}, "java_home"),
    ({"project_path": "/project", "main_class": ""}, "main_class"),
    ({"project_path": "/project", "java_home": ""}, "java_home"),
    ({"project_path": "/project", "app_args": "--profile=dev"}, "app_args"),
    ({"project_path": "/project", "vm_args": [4]}, "vm_args"),
    ({"project_path": "/project", "classpath": "."}, "classpath"),
    ({"project_path": "/project", "jar_path": "app.jar"}, "jar_path"),
])
def test_invalid_headless_arguments(arguments, argument):
    with pytest.raises(ProjectLaunchArgumentError) as error:
        parse_project_launch_request({"action": "run", **arguments})
    assert error.value.payload["argument"] == argument


def test_previous_lower_priority_jdk_cannot_override_first_candidate(tmp_path, monkeypatch):
    first, previous = fake_jdk(tmp_path / "first", 8), fake_jdk(tmp_path / "previous", 17)
    monkeypatch.setenv("JAVA_HOME", str(first.home))
    monkeypatch.setattr("jolink_runtime.launch.toolchain.shutil.which", lambda _: str(previous.java_executable))
    selected = ProjectLaunchPipeline()._select_java(Context(), preferences=IdeaBuildPreferences(),
        explicit_reference=None, for_build=False, cwd=tmp_path, build_log=tmp_path / "log",
        already_probed=previous)
    assert selected.home == first.home
