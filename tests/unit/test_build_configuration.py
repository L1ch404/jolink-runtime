"""Behavior contracts: config changes invalidate; application edits do not."""

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from jolink_runtime.adapters.java.jdwp_adapter import JavaRuntime
from jolink_runtime.core.models import RuntimeAction
from jolink_runtime.launch.configuration_inputs import (
    build_configuration_stamps,
    maven_configuration_files,
)
from jolink_runtime.launch.contracts import JvmLaunchPlan, LaunchIntent
from jolink_runtime.launch.fast_test_cache import FastTestCache
from jolink_runtime.launch.jdt_compile_session import JdtBuildWorldPlan
from jolink_runtime.launch.jdt_launch_service import JdtLaunchService
from jolink_runtime.launch.jdt_reload_service import JdtReloadService
from jolink_runtime.launch.project_launch_cache import ProjectLaunchCache
from jolink_runtime.launch.project_launcher import (
    ProjectLaunchPipeline,
    ProjectLaunchRequest,
)
from jolink_runtime.launch.test_build_world import JavaTestBuildWorld
from jolink_runtime.launch.toolchain import JavaToolchainCandidate


@pytest.fixture
def model(tmp_path):
    root = tmp_path / "project"
    for directory in (
        "src/main/java",
        "src/test/java",
        "src/main/resources",
        "target/classes",
        "deps/classes",
    ):
        (root / directory).mkdir(parents=True)
    pom = root / "pom.xml"
    pom.write_text("<project/>")
    jdk = tmp_path / "jdk"
    toolchain = JavaToolchainCandidate(
        jdk, jdk / "bin/java", jdk / "bin/javac", "test", 8, 8
    )
    sources = (root / "src/main/java",)
    resource = root / "src/main/resources"
    output = root / "target/classes"
    dependency = root / "deps/library.jar"
    dependency.write_bytes(b"fixture artifact")
    (root / "deps/classes/Upstream.class").write_bytes(b"fixture class")
    dependencies = (dependency, root / "deps/classes")
    plan = JdtBuildWorldPlan(
        project_root=root,
        module_root=root,
        source_roots=sources,
        dependency_entries=dependencies,
        processor_entries=(),
        lombok_entries=(),
        target_java_home=jdk,
        source_encoding="UTF-8",
        source_level=8,
        target_level=8,
        fingerprint="model",
        configuration_inputs=(pom,),
        configuration_environment_names=(),
        javac_executable=toolchain.javac_executable,
    )
    intent = LaunchIntent(
        source="idea",
        launch_name="App",
        launch_type="java_application",
        main_class="App",
        working_directory=root,
    )
    jvm = JvmLaunchPlan(
        java_executable=toolchain.java_executable,
        classpath=(output,),
        main_class="App",
        working_directory=root,
    )
    world = JavaTestBuildWorld(
        build_system="maven",
        project_root=root,
        module_root=root,
        main_source_roots=sources,
        test_source_roots=(root / "src/test/java",),
        main_output=output,
        test_output=root / "target/test-classes",
        main_dependencies=dependencies,
        test_dependencies=(),
        test_runtime_classpath=(),
        resource_roots=(resource,),
        target_java_home=jdk,
        source_encoding="UTF-8",
        source_level=8,
        method_parameters=False,
        processor_entries=(),
        java_agents=(),
        extra_worker_jvm_arguments=(),
        test_java_executable=toolchain.java_executable,
        test_framework="junit4",
        test_working_directory=root,
        test_classes_directories=(),
        runner_environment={},
        javac_executable=toolchain.javac_executable,
        configuration_inputs=(pom,),
        configuration_environment_names=(),
    )
    launch = ProjectLaunchCache(tmp_path / "launch-cache")
    fast = FastTestCache(tmp_path / "test-cache")
    data = SimpleNamespace(
        root=root,
        pom=pom,
        plan=plan,
        world=world,
        launch=launch,
        fast=fast,
        intent=intent,
        toolchain=toolchain,
    )

    def save(kind):
        if kind == "test":
            fast.save(data.world, toolchain)
        else:
            launch.save(
                project_root=root,
                intent=intent,
                build_system=data.world.build_system,
                build_offline=True,
                build_jdk=toolchain,
                runtime_jdk=toolchain,
                module_output=output,
                generation_input_roots=(output,),
                resource_source_roots=(resource,),
                jvm_plan=jvm,
                jdt_plan=data.plan,
            )

    def load(kind):
        if kind == "test":
            return fast.load(root, data.world.build_system)
        return launch.load(
            project_root=root,
            intent=intent,
            build_system=data.world.build_system,
            ready_port=0,
            startup_wait_timeout_seconds=0,
        )

    data.save, data.load = save, load
    return data


@pytest.mark.parametrize("kind", ["launch", "test"])
@pytest.mark.parametrize("change", ["edit", "delete", "malformed", "same_stat_edit"])
def test_actual_cache_load_rejects_changed_pom(model, kind, change):
    if change == "same_stat_edit":
        model.pom.write_text("<project><name>A</name></project>")
    model.save(kind)
    assert model.load(kind) is not None
    original = model.pom.stat()
    if change == "delete":
        model.pom.unlink()
    else:
        text = {
            "edit": "<project><!-- new dependency --></project>",
            "malformed": "<broken",
            "same_stat_edit": "<project><name>B</name></project>",
        }[change]
        model.pom.write_text(text)
        if change == "same_stat_edit":
            assert model.pom.stat().st_size == original.st_size
            os.utime(model.pom, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert model.load(kind) is None
    if kind == "test":
        assert not model.fast.is_current(model.root, "maven")


@pytest.mark.parametrize("kind", ["launch", "test"])
def test_no_change_reopens_and_application_files_never_invalidate_or_get_scanned(
    model, kind, monkeypatch
):
    model.save(kind)
    for name in (
        "src/main/java/App.java",
        "target/classes/App.class",
        "src/main/resources/value.txt",
    ):
        (model.root / name).write_text("changed")
    os.utime(model.pom, None)
    read = Path.read_bytes

    def config_read(path):
        assert not str(path).endswith((".java", ".class", ".jar", "value.txt")), path
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", config_read)
    monkeypatch.setattr(
        Path, "rglob", lambda *args: pytest.fail("must not scan the project")
    )
    assert model.load(kind) is not None
    if kind == "test":
        assert model.fast.is_current(model.root, "maven")


@pytest.mark.parametrize("kind", ["launch", "test"])
@pytest.mark.parametrize("profile", [False, True])
@pytest.mark.parametrize("changed", ["root", "module", "parent"])
@pytest.mark.parametrize("delete", [False, True])
def test_module_and_parent_changes_invalidate_both_caches(
    model, kind, profile, changed, delete
):
    module = model.root / "app/pom.xml"
    parent = model.root / "parent/pom.xml"
    for path in (module, parent):
        path.parent.mkdir()
    parent.write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"/>')
    module.write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><parent><relativePath>../parent/pom.xml</relativePath></parent></project>'
    )
    modules = "<modules><module>app</module></modules>"
    body = (
        f"<profiles><profile><id>selected</id>{modules}</profile></profiles>"
        if profile
        else modules
    )
    model.pom.write_text(f"<project>{body}</project>")
    # The Probe adds the selected Profile module; its parent must be followed
    # too, rather than just hashing the supplied module POM as an extra file.
    model.plan = replace(model.plan, configuration_inputs=(model.pom, module))
    model.world = replace(model.world, modules=({"module_root": str(module.parent)},))
    model.save(kind)
    assert model.load(kind) is not None
    target = {"root": model.pom, "module": module, "parent": parent}[changed]
    if delete:
        target.unlink()
    else:
        target.write_text(target.read_text() + "\n<!-- changed -->")
    assert model.load(kind) is None
    if kind == "test":
        assert not model.fast.is_current(model.root, "maven")


@pytest.mark.parametrize("kind", ["launch", "test"])
@pytest.mark.parametrize("name", ["maven.config", "jvm.config", "extensions.xml"])
def test_new_maven_configuration_is_detected(model, kind, name):
    model.save(kind)
    path = model.root / ".mvn" / name
    path.parent.mkdir()
    path.write_text("new configuration")
    assert model.load(kind) is None


def test_parent_defaults_empty_relative_path_directories_and_cycles(model):
    parent = model.root.parent / "pom.xml"
    parent.write_text("<project/>")
    model.pom.write_text("<project><parent/></project>")
    assert parent in maven_configuration_files(model.root)
    model.pom.write_text("<project><parent><relativePath/></parent></project>")
    assert parent not in maven_configuration_files(model.root)
    directory = model.root / "parent"
    directory.mkdir()
    (directory / "pom.xml").write_text("<project><parent/></project>")
    model.pom.write_text(
        "<project><parent><relativePath>parent</relativePath></parent></project>"
    )
    inputs = maven_configuration_files(model.root, (model.pom, directory / "pom.xml"))
    assert inputs.count(model.pom) == 1 and inputs.count(directory / "pom.xml") == 1


@pytest.mark.parametrize("kind", ["launch", "test"])
def test_missing_module_created_without_editing_root_invalidates(model, kind):
    model.pom.write_text("<project><modules><module>app</module></modules></project>")
    model.save(kind)
    path = model.root / "app/pom.xml"
    path.parent.mkdir()
    path.write_text("<project/>")
    assert model.load(kind) is None


def test_old_launch_cache_without_snapshot_requires_probe_not_deleting_cache(model):
    model.save("launch")
    file = model.launch._file(model.root, model.intent.launch_name)
    raw = json.loads(file.read_text())
    del raw["configuration_stamps"]
    file.write_text(json.dumps(raw))
    assert model.load("launch") is None
    assert file.is_file()


def test_unreadable_configuration_and_missing_running_baseline_are_not_current(
    model, monkeypatch
):
    prepared = SimpleNamespace(jdt_build_world_plan=model.plan, build_system="maven")
    assert (
        JdtLaunchService.configuration_rejection(prepared).data["error_code"]
        == "BUILD_CONFIGURATION_CHANGED"
    )
    model.save("launch")
    model.save("test")
    prepared.jdt_build_world_plan = model.load("launch").jdt_plan
    read = Path.read_bytes

    def denied(path):
        if path == model.pom:
            raise PermissionError("configuration is not readable")
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    assert model.load("launch") is None
    assert model.load("test") is None
    assert JdtLaunchService.configuration_rejection(prepared).data["applied"] is False


@pytest.mark.parametrize("kind", ["launch", "test"])
def test_saving_after_slow_compile_does_not_bless_a_later_pom_change(model, kind):
    stamps = build_configuration_stamps(model.root, "maven", (model.pom,))
    model.plan = replace(model.plan, configuration_stamps=stamps)
    model.world = replace(model.world, configuration_stamps=stamps)
    model.pom.write_text("<project><!-- edited while compile was running --></project>")
    model.save(kind)
    assert model.load(kind) is None


@pytest.mark.parametrize("build_system", ["maven", "gradle"])
def test_reload_restart_check_uses_running_model_not_new_cache(model, build_system):
    config = model.pom if build_system == "maven" else model.root / "build.gradle"
    if build_system == "gradle":
        config.write_text("plugins { id 'java' }")
    model.world = replace(model.world, build_system=build_system)
    model.plan = replace(model.plan, configuration_inputs=(config,))
    model.save("launch")
    original = model.load("launch").jdt_plan
    prepared = SimpleNamespace(
        jdt_build_world_plan=original,
        build_system=build_system,
        jvm_plan=object(),
        generation_classpath_index=0,
        request=object(),
    )
    assert JdtLaunchService.configuration_rejection(prepared) is None
    config.write_text(config.read_text() + "\nchanged")
    # Another user of the disk cache cannot make the active old JVM current.
    model.save("launch")
    assert model.load("launch") is not None
    rejected = JdtLaunchService.configuration_rejection(prepared)
    assert rejected.data["error_code"] == "BUILD_CONFIGURATION_CHANGED"
    assert rejected.data["applied"] is False
    session = SimpleNamespace(generations=SimpleNamespace(current=object()), refresh_compile_ready=lambda: True)
    runtime = JavaRuntime()
    runtime._project_update_plans["active"] = prepared
    runtime._project_sessions["active"] = session
    runtime._launch_controller = SimpleNamespace(
        snapshot=lambda: {"attempt_id": "active"}
    )
    refreshed = []
    runtime._last_project_request = object()
    def refresh(action, request):
        from jolink_runtime.core.models import RuntimeResult
        refreshed.append(request)
        return RuntimeResult(ok=True, data={"status": "project_launch_restarted"})
    runtime.restart_project = refresh
    # Restart refreshes the model; the old HotSwap-only path cannot bless it.
    restarted = runtime.restart_current_project(RuntimeAction(action="restart"))
    reloaded = JdtReloadService(None).start(
        runtime,
        RuntimeAction(action="update"),
        attempt_id="active",
        generation=1,
        prepared=prepared,
        project_session=session,
    )
    assert refreshed == [runtime._last_project_request]
    assert restarted.ok and restarted.data["apply_method"] == "restart"
    assert reloaded.data["error_code"] == "BUILD_CONFIGURATION_CHANGED"


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("default_exists", [False, True])
@pytest.mark.parametrize("profile_extra", [False, True])
def test_launch_tracks_only_selected_settings_through_probe_and_cache(
    model, tmp_path, monkeypatch, explicit, default_exists, profile_extra
):
    from jolink_runtime.launch import project_launcher as module

    home = tmp_path / "home"
    default = home / ".m2/settings.xml"
    default.parent.mkdir(parents=True)
    if default_exists:
        default.write_text("<settings/>")
    custom = tmp_path / "tools/maven/conf/settings.xml"
    custom.parent.mkdir(parents=True)
    custom.write_text("<settings><!-- custom --></settings>")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    selected = custom if explicit else default
    supplied = []
    directory = tmp_path / "attempt"
    directory.mkdir()

    class Probe:
        def prepare(self, **kwargs):
            supplied.append(kwargs["source_settings"])
            temporary = directory / "probe-settings.xml"
            temporary.write_text("<settings/>")
            return SimpleNamespace(
                settings_file=temporary,
                output_directory=directory,
                goal="probe:export-build-world",
            )

        def load_snapshot(self, *args, **kwargs):
            return {"runtimeClasspathElements": [str(model.root / "target/classes")]}

    monkeypatch.setattr(module.ProductMavenProbe, "load", lambda: Probe())
    facts = {
        "module_root": str(model.root),
        "output_directory": str(model.root / "target/classes"),
        "source_roots": [str(model.root / "src/main/java")],
        "resource_roots": [],
        "classpath": [],
        "processor_entries": [],
        "lombok_entries": [],
        "target_java_home": str(model.toolchain.home),
        "source_encoding": "UTF-8",
        "source_level": 8,
        "method_parameters": False,
        "worker_min_heap_mb": 64,
        "worker_max_heap_mb": 2048,
    }
    probe_modules = [facts]
    if profile_extra:
        optional = model.root / "optional/pom.xml"
        parent = model.root / "optional-parent/pom.xml"
        optional.parent.mkdir()
        parent.parent.mkdir()
        model.pom.write_text(
            "<project><profiles><profile><id>extra</id><modules><module>optional</module></modules></profile></profiles></project>"
        )
        optional.write_text(
            "<project><parent><relativePath>../optional-parent/pom.xml</relativePath></parent></project>"
        )
        parent.write_text("<project/>")
        probe_modules.append({**facts, "module_root": str(optional.parent)})
    monkeypatch.setattr(
        module, "load_module_worlds", lambda *args, **kwargs: probe_modules
    )
    pipeline = ProjectLaunchPipeline()
    monkeypatch.setattr(
        pipeline, "materialize_command", lambda plan, **kwargs: (plan, None)
    )
    context = SimpleNamespace(
        set_build_plan=lambda _: None,
        transition=lambda _: None,
        run_operation=lambda _: SimpleNamespace(succeeded=True),
    )
    target = SimpleNamespace(
        directory=model.root, relative_path=".", pom_file=model.pom
    )
    workspace = SimpleNamespace(
        root_pom=model.pom, build_root=model.root, modules=(target,)
    )
    request = ProjectLaunchRequest(model.root, "App", 5005, 0, 0)
    preferences = SimpleNamespace(
        user_settings_file=custom if explicit else None,
        local_repository=None,
        active_profiles=(),
        project_jdk_name=None,
        maven_runner_jdk_name=None,
        jdk_homes_by_name={},
    )
    prepared = pipeline._prepare_maven_probe(
        context,
        request,
        model.intent,
        preferences,
        workspace,
        target,
        model.toolchain,
        SimpleNamespace(argv_prefix=("mvn",)),
        directory,
        directory / "build.log",
    )
    prepared = pipeline._stabilize(prepared)
    assert supplied == [selected if explicit or default_exists else None]
    model.plan = prepared.jdt_build_world_plan
    assert selected in model.plan.configuration_inputs
    assert (default in model.plan.configuration_inputs) is (not explicit)
    if not explicit and not default_exists:
        assert model.plan.configuration_stamps[str(default)] == "missing"
    model.save("launch")
    assert model.load("launch") is not None
    if profile_extra:
        # No paths injected into plan.configuration_inputs by this test.
        assert optional in model.plan.configuration_inputs
        assert parent in model.plan.configuration_inputs
        parent.write_text(
            "<project><!-- modified inherited configuration --></project>"
        )
        assert model.load("launch") is None
        parent.write_text("<project/>")
        # Old eea26fe snapshots contain these modules but omit their inputs.
        file = model.launch._file(model.root, model.intent.launch_name)
        raw = json.loads(file.read_text())
        omitted = {str(optional), str(parent)}
        raw["jdt_plan"]["configuration_inputs"] = [
            p for p in raw["jdt_plan"]["configuration_inputs"] if p not in omitted
        ]
        raw["configuration_stamps"] = {
            p: v for p, v in raw["configuration_stamps"].items() if p not in omitted
        }
        file.write_text(json.dumps(raw))
        assert model.load("launch") is None
        model.save("launch")
    if explicit:
        default.write_text(
            "<settings><!-- unused default changed or created --></settings>"
        )
        assert model.load("launch") is not None
        assert JdtLaunchService.configuration_rejection(prepared) is None
    selected.write_text(
        "<settings><!-- selected settings changed or created --></settings>"
    )
    assert model.load("launch") is None
    assert (
        JdtLaunchService.configuration_rejection(prepared).data["error_code"]
        == "BUILD_CONFIGURATION_CHANGED"
    )
