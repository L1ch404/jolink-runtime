"""Build selection participates in cache identity; runtime-only choices do not."""

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_build_configuration import model as shared_model
from test_fast_test_model_identity import active_project
from test_project_launch_jdk import Context
from test_project_launch_jdk import cached_launch as shared_cached_launch

from jolink_runtime.launch.contracts import JvmLaunchPlan
from jolink_runtime.launch.fast_test_manager import FastTestManager
from jolink_runtime.launch.idea_environment import (
    IdeaBuildPreferences,
    IdeaEnvironmentImporter,
    build_preferences_key,
)
from jolink_runtime.launch.jdt_launch_service import JdtLaunchService

model = shared_model
cached_launch = shared_cached_launch


def baseline():
    return {
        "custom_maven_home": "maven-A", "user_settings_file": "settings-A.xml",
        "local_repository": "repository-A", "active_profiles": ["A"],
        "maven_runner_jdk_name": "build", "project_jdk_name": "runtime",
        "jdk_homes_by_name": {"build": ["jdk-build-A"], "runtime": ["jdk-runtime-A"]},
    }


def save(model, kind, preferences, system="maven"):
    if kind == "test":
        model.fast.save(replace(model.world, build_system=system, build_preferences=preferences), model.toolchain)
    else:
        model.launch.save(
            project_root=model.root, intent=model.intent, build_system=system, build_offline=True,
            build_jdk=model.toolchain, runtime_jdk=model.toolchain, module_output=model.world.main_output,
            generation_input_roots=(), resource_source_roots=(), jdt_plan=model.plan,
            jvm_plan=JvmLaunchPlan(java_executable=model.toolchain.java_executable,
                                  classpath=(), main_class="App", working_directory=model.root),
            build_preferences=preferences,
        )


def load(model, kind, preferences, system="maven"):
    if kind == "test":
        return model.fast.load(model.root, system, build_preferences=preferences)
    return model.launch.load(project_root=model.root, intent=model.intent, build_system=system,
                             ready_port=0, startup_wait_timeout_seconds=1, build_preferences=preferences)


@pytest.mark.parametrize("kind", ["launch", "test"])
@pytest.mark.parametrize("change", ["custom_maven_home", "user_settings_file", "local_repository",
                                     "active_profiles", "maven_runner_jdk_name", "jdk_mapping", "removed"])
def test_build_choice_change_invalidates_and_new_choice_is_persisted(model, kind, change):
    before = baseline()
    save(model, kind, before)
    assert load(model, kind, before) is not None
    after = copy.deepcopy(before)
    if change == "jdk_mapping":
        after["jdk_homes_by_name"]["build"] = ["jdk-build-B"]
    elif change == "removed":
        after = {}
    else:
        after[change] = ["B"] if change == "active_profiles" else "B"
    assert load(model, kind, after) is None
    save(model, kind, after)
    restored = load(model, kind, after)
    assert restored is not None
    if kind == "test":
        assert restored[0].build_preferences == after


@pytest.mark.parametrize("kind", ["launch", "test"])
@pytest.mark.parametrize("system", ["maven", "gradle"])
def test_runtime_and_unrelated_sdk_edits_keep_build_cache(model, kind, system):
    before = baseline()
    save(model, kind, before, system)
    after = copy.deepcopy(before)
    after.update(project_jdk_name="new-runtime", warnings=["different warning"], known_jdk_names=["new"])
    after["jdk_homes_by_name"]["runtime"] = ["other-runtime-home"]
    after["jdk_homes_by_name"]["unrelated"] = ["another-installed-sdk"]
    if system == "gradle":
        after.update(custom_maven_home="B", user_settings_file="B", local_repository="B", active_profiles=["B"])
    assert load(model, kind, after, system) is not None


@pytest.mark.parametrize("runner", [None, "#USE_PROJECT_JDK", "Project JDK"])
@pytest.mark.parametrize("system", ["maven", "gradle"])
def test_project_sdk_changes_matter_when_it_is_also_the_build_jdk(runner, system):
    before = baseline()
    before["maven_runner_jdk_name"] = runner
    after = copy.deepcopy(before)
    after["project_jdk_name"] = "other-project-sdk"
    assert build_preferences_key(before, system) != build_preferences_key(after, system)
    after = copy.deepcopy(before)
    after["jdk_homes_by_name"]["runtime"] = ["changed-sdk-home"]
    assert build_preferences_key(before, system) != build_preferences_key(after, system)


def test_defaults_and_noisy_summary_fields_do_not_force_launch_cache_migration(model):
    save(model, "launch", {})
    assert load(model, "launch", IdeaBuildPreferences().cache_values()) is not None


def test_live_test_model_cannot_borrow_new_disk_preferences(model, monkeypatch):
    manager = FastTestManager()
    manager._cache = model.fast
    old = IdeaBuildPreferences(active_profiles=("A",)).cache_values()
    new = IdeaBuildPreferences(active_profiles=("B",)).cache_values()
    world = replace(model.world, build_preferences=old)
    live, attempt, closed = active_project(model, manager, monkeypatch, world)
    monkeypatch.setattr(manager._idea, "import_preferences", lambda root: IdeaBuildPreferences(active_profiles=("A",)))
    assert manager._ensure_project(attempt) is live
    model.fast.save(replace(world, build_preferences=new), model.toolchain)
    monkeypatch.setattr(manager._idea, "import_preferences", lambda root: IdeaBuildPreferences(active_profiles=("B",)))
    seen = []
    replacement = SimpleNamespace()
    monkeypatch.setattr(manager, "_start_build_world", lambda **kw: seen.append(kw["world"].build_preferences) or replacement)
    assert manager._ensure_project(attempt) is replacement
    assert closed == [True] and seen == [new]
    assert live.configuration_snapshot["build_preferences"] == old


def test_workspace_identity_follows_resolved_configuration_not_build_choice(cached_launch):
    f = cached_launch
    prepared = f.pipeline.prepare(Context(), f.request, attempt_directory=f.attempt)
    preferences = baseline()
    prepared = replace(prepared, build_preferences_identity=preferences)
    first = f.pipeline._stabilize(prepared).jdt_build_world_plan.fingerprint
    assert f.pipeline._stabilize(prepared).jdt_build_world_plan.fingerprint == first
    changed = {**preferences, "active_profiles": ["B"]}
    second = f.pipeline._stabilize(replace(prepared, build_preferences_identity=changed))
    # A profile selection requires re-Probe, but identical exported compiler
    # settings must not discard the incremental workspace.
    assert second.jdt_build_world_plan.fingerprint == first
    changed_plan = replace(prepared.jdt_build_world_plan, source_level=17, target_level=17)
    changed_compiler = f.pipeline._stabilize(replace(prepared, jdt_build_world_plan=changed_plan))
    assert changed_compiler.jdt_build_world_plan.fingerprint != first
    runtime_only = {**preferences, "project_jdk_name": "different-runtime"}
    same = f.pipeline._stabilize(replace(prepared, build_preferences_identity=runtime_only))
    assert same.jdt_build_world_plan.fingerprint == first


def test_running_model_uses_its_own_build_preferences_snapshot(model, monkeypatch):
    old = IdeaBuildPreferences(active_profiles=("A",))
    new = IdeaBuildPreferences(active_profiles=("B",))
    plan = replace(model.plan, configuration_stamps=model.fast.input_snapshot(model.world))
    prepared = SimpleNamespace(jdt_build_world_plan=plan, build_system="maven",
                               build_preferences_identity=old.cache_values())
    monkeypatch.setattr(IdeaEnvironmentImporter, "import_preferences", lambda self, root: old)
    assert JdtLaunchService.configuration_rejection(prepared) is None
    # A new disk model cannot make the original running model current.
    save(model, "launch", new.cache_values())
    monkeypatch.setattr(IdeaEnvironmentImporter, "import_preferences", lambda self, root: new)
    assert JdtLaunchService.configuration_rejection(prepared).data["error_code"] == "BUILD_CONFIGURATION_CHANGED"
