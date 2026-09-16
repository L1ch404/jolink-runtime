"""Active model identity must not be inferred from a replaceable disk cache."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_build_configuration import model as shared_model

from jolink_runtime.launch.fast_test_cache import FastTestCache
from jolink_runtime.launch.fast_test_manager import FastTestManager
from jolink_runtime.launch.fast_test_manager import TestAttempt as Attempt
from jolink_runtime.launch.process_supervisor import AttemptToken

model = shared_model


def active_project(model, manager, monkeypatch, world):
    """Publish via the real finish method so the test cannot prefill a missing baseline."""
    attempt = Attempt(
        "test", 1, AttemptToken("test", 1), model.root, (), ("example.Test",), 300
    )
    source = model.root / "src/test/java/example/Test.java"
    source.parent.mkdir(exist_ok=True)
    source.write_text("package example; public class Test {}")
    closed = []
    compiler = SimpleNamespace(
        ready=True,
        test_source_roots=world.test_source_roots,
        output_directory=world.main_output,
        test_output_directory=world.test_output,
        accept_baseline=lambda: None,
        save_source_index=lambda: None,
        close=lambda: closed.append(True) or True,
    )
    workspace = SimpleNamespace(
        root=model.root / "workspace",
        mark_initialized=lambda: None,
        release=lambda **kwargs: None,
    )
    project = manager._finish_build_world(
        attempt=attempt,
        full=SimpleNamespace(compile_ok=True),
        compiler=compiler,
        world=world,
        build_jdk=model.toolchain,
        workspace=workspace,
        configuration_snapshot=manager._cache.model_snapshot(world),
    )
    manager._project = project
    return project, attempt, closed


@pytest.mark.parametrize("change", ["pom", "settings"])
def test_live_old_model_does_not_reuse_new_disk_baseline(model, monkeypatch, change):
    settings = model.root / "settings.xml"
    settings.write_text("<settings><!-- A --></settings>")
    world = replace(
        model.world,
        configuration_inputs=(model.pom, settings),
        test_jvm_arguments=("-Dmodel=A",),
    )
    manager = FastTestManager()
    manager._cache = model.fast
    live, attempt, closed = active_project(model, manager, monkeypatch, world)
    assert manager._ensure_project(attempt) is live
    (model.pom if change == "pom" else settings).write_text(
        "<project><!-- B --></project>"
        if change == "pom"
        else "<settings><!-- B --></settings>"
    )
    model.fast.save(replace(world, test_jvm_arguments=("-Dmodel=B",)), model.toolchain)
    assert model.fast.is_current(model.root, "maven") is True
    replacement = object()
    observed = []

    def restore(**kwargs):
        observed.append(kwargs["world"].test_jvm_arguments)
        return replacement

    monkeypatch.setattr(manager, "_start_build_world", restore)
    assert manager._ensure_project(attempt) is replacement
    assert observed == [("-Dmodel=B",)] and closed == [True]
    assert live.runner_jvm_arguments == ("-Dmodel=A",)


def test_current_live_model_does_not_require_a_disk_cache(model, monkeypatch):
    manager = FastTestManager()
    manager._cache = model.fast
    live, attempt, closed = active_project(model, manager, monkeypatch, model.world)
    (model.fast._directory(model.root, "maven") / "build-world.json").unlink()
    monkeypatch.setattr(
        model.fast, "load", lambda *args: pytest.fail("live reuse must not reload disk")
    )
    assert manager._ensure_project(attempt) is live
    assert closed == []


@pytest.mark.parametrize("change", ["environment", "generator"])
def test_live_model_preserves_environment_and_preparation_checks(
    model, monkeypatch, change
):
    script = model.root / "build.gradle"
    script.write_text("plugins { id 'java' }")
    grammar = model.root / "Grammar.g4"
    grammar.write_text("A")
    monkeypatch.setenv("JOLINK_FIXTURE_OPTION", "A")
    world = replace(
        model.world,
        build_system="gradle",
        configuration_inputs=(script,),
        configuration_environment_names=("JOLINK_FIXTURE_OPTION",),
        modules=(
            {"module_root": str(model.root), "preparation_inputs": [str(grammar)]},
        ),
    )
    snapshot = FastTestCache.model_snapshot(world)
    assert FastTestCache.snapshot_is_current(model.root, "gradle", snapshot)
    if change == "environment":
        monkeypatch.setenv("JOLINK_FIXTURE_OPTION", "B")
    else:
        grammar.write_text("B changed")
    assert not FastTestCache.snapshot_is_current(model.root, "gradle", snapshot)


def test_v4_fast_test_cache_cannot_hide_missing_settings_inputs(model):
    model.save("test")
    path = model.fast._directory(model.root, "maven") / "build-world.json"
    raw = json.loads(path.read_text())
    raw["schema"] = "jolink.fast-test-world.v4"
    path.write_text(json.dumps(raw))
    assert model.fast.load(model.root, "maven") is None
    assert not model.fast.is_current(model.root, "maven")
    assert path.exists()


@pytest.mark.parametrize("exists", [False, True])
def test_selected_settings_creation_or_edit_invalidates_persisted_test_world(
    model, tmp_path, exists
):
    selected = tmp_path / "settings.xml"
    if exists:
        selected.write_text("<settings/>")
    world = replace(model.world, configuration_inputs=(model.pom, selected))
    model.fast.save(world, model.toolchain)
    loaded, _ = model.fast.load(model.root, "maven")
    assert selected in loaded.configuration_inputs
    assert str(selected) in loaded.configuration_stamps
    selected.write_text("<settings><!-- changed --></settings>")
    assert not model.fast.is_current(model.root, "maven")
    assert model.fast.load(model.root, "maven") is None


@pytest.mark.parametrize("custom", [False, True])
def test_maven_producer_keeps_settings_but_not_effective_pom_or_resources(
    model, tmp_path, monkeypatch, custom
):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    selected = tmp_path / "chosen-settings.xml" if custom else home / ".m2/settings.xml"
    if custom:
        selected.write_text("<settings/>")
    session_root = tmp_path / "bootstrap"
    session_root.mkdir()
    effective = session_root / "effective-pom.xml"
    effective.write_text("""<project><groupId>example</groupId><artifactId>app</artifactId><version>1</version><properties>
<maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
</properties></project>""")
    resource = model.root / "src/main/resources/value.txt"
    resource.write_text("A")
    snapshot = {
        "compileSourceRoots": [str(model.root / "src/main/java")],
        "testCompileSourceRoots": [str(model.root / "src/test/java")],
        "outputDirectory": str(model.root / "target/classes"),
        "testOutputDirectory": str(model.root / "target/test-classes"),
        "compileClasspathElements": [],
        "testClasspathElements": [],
        "resourceDirectories": [str(resource.parent)],
        "testResourceDirectories": [],
        "annotationProcessing": {"discoveryMode": "DISABLED"},
        "testAnnotationProcessing": {"discoveryMode": "DISABLED"},
        "testRuntime": {},
    }
    manager = FastTestManager()
    monkeypatch.setattr(
        manager, "_select_target_java", lambda *args, **kwargs: model.toolchain.home
    )
    # Only the Worker is replaced; the real Probe-to-World conversion runs.
    monkeypatch.setattr(manager, "_start_build_world", lambda **kwargs: kwargs["world"])
    module = SimpleNamespace(
        directory=model.root,
        pom_file=model.pom,
        relative_path=".",
        group_id="example",
        artifact_id="app",
    )
    world = manager._create_project_from_snapshot(
        attempt=SimpleNamespace(require_not_cancelled=lambda: None),
        session_root=session_root,
        workspace=SimpleNamespace(
            project_root=model.root,
            build_root=model.root,
            root_pom=model.pom,
            modules=(module,),
        ),
        module=module,
        build_jdk=model.toolchain,
        preferences=SimpleNamespace(),
        effective_pom=effective,
        source_settings=selected if custom else None,
        snapshot=snapshot,
    )
    assert selected in world.configuration_inputs
    assert (
        effective not in world.configuration_inputs
        and resource.parent not in world.configuration_inputs
    )
    assert not effective.exists()  # The actual bootstrap cleanup has run.
    model.fast.save(world, model.toolchain)
    assert model.fast.is_current(model.root, "maven")
    resource.write_text("B")
    assert model.fast.is_current(model.root, "maven")
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text("<settings><!-- edited or newly created --></settings>")
    assert not model.fast.is_current(model.root, "maven")
