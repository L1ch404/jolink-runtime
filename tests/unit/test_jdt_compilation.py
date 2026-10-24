"""Launch and tests must derive identity from the same actual constructor inputs."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jolink_runtime.launch import jdt_compilation as shared
from jolink_runtime.launch.jdt_modules import module_name
from jolink_runtime.launch.jdt_workspace_store import JdtWorkspaceStore
from test_launch_workspace_identity import plan, modules_plan
from test_build_configuration import model


def world_for(plan, **overrides):
    return SimpleNamespace(
        **{
            "module_root": plan.module_root, "target_java_home": plan.target_java_home,
            "main_source_roots": plan.source_roots, "main_dependencies": plan.dependency_entries,
            "test_source_roots": (), "test_dependencies": (),
            "source_level": plan.source_level, "source_encoding": plan.source_encoding,
            "method_parameters": plan.method_parameters,
            "processor_entries": plan.processor_entries, "processor_names": plan.processor_names,
            "processor_options": plan.processor_options,
            "java_agents": tuple(f"{p}=ECJ" for p in plan.lombok_entries),
            "extra_worker_jvm_arguments": (), "worker_min_heap_mb": plan.worker_min_heap_mb,
            "worker_max_heap_mb": plan.worker_max_heap_mb, "modules": plan.modules,
            **overrides,
        }
    )


@pytest.mark.parametrize("field,value", [
    ("source_roots", (Path("new-sources"),)), ("dependency_entries", (Path("new.jar"),)),
    ("source_encoding", "GBK"), ("source_level", 17),
    ("target_java_home", Path("different-platform")), ("method_parameters", True),
    ("processor_entries", (Path("processor.jar"),)), ("processor_names", ("example.Processor",)),
    ("processor_options", {"flag": None}), ("lombok_entries", (Path("lombok.jar"),)),
    ("worker_max_heap_mb", 1024),
])
def test_both_consumers_use_the_same_compiler_identity_rules(plan, field, value):
    old_launch = shared.launch_compilation_configuration(plan)
    old_test = shared.test_compilation_configuration(world_for(plan))
    assert old_launch == old_test
    changed = replace(plan, **{field: value})
    new_launch = shared.launch_compilation_configuration(changed)
    new_test = shared.test_compilation_configuration(world_for(changed))
    assert new_launch == new_test
    assert new_launch != old_launch


@pytest.mark.parametrize("scope", ["main", "test"])
def test_split_project_settings_reach_identity_and_constructor(plan, tmp_path, scope):
    plan = modules_plan(plan)
    target = {**plan.modules[-1], "test_source_roots": [str(tmp_path / "test")],
              "test_compiler": {"source_level": 17, "source_encoding": "GBK",
                                "processor_names": ["example.TestProcessor"],
                                "processor_options": {"test": "yes"}, "classpath": []}}
    world = world_for(replace(plan, modules=(*plan.modules[:-1], target)))
    before = shared.test_compilation_configuration(world)
    assert before["options"]["modules"][module_name(target["module_root"])]["test_compiler"]["source_level"] == 17
    changed = dict(target)
    if scope == "main":
        changed["processor_options"] = {"main": "changed"}
    else:
        changed["test_compiler"] = {**target["test_compiler"], "processor_options": {"test": "changed"}}
    world.modules = (*world.modules[:-1], changed)
    after = shared.test_compilation_configuration(world)
    assert after != before
    _, compiler = shared.open_compile_session(
        after, store=JdtWorkspaceStore(tmp_path / "cache"), project_root=plan.project_root,
        module_root=Path(target["module_root"]), candidate=SimpleNamespace(root=tmp_path / "candidate"),
        worker_java=SimpleNamespace(home=tmp_path / "worker-jdk", major=21), resource_modules=world.modules,
    )
    main, test = compiler.projects[-2:]
    assert main["source_level"] == 8 and test["source_level"] == 17
    assert main["source_encoding"] == "UTF-8" and test["source_encoding"] == "GBK"
    assert test["processor_names"] == ["example.TestProcessor"]
    assert main["processor_options"] == ({"main": "changed"} if scope == "main" else {})
    assert test["processor_options"] == {"test": "changed" if scope == "test" else "yes"}


def test_runtime_metadata_never_enters_either_compilation_configuration(plan):
    plan = modules_plan(plan)
    changed = tuple({**m, "resource_roots": ["new-resources"], "test_resource_roots": ["test-resources"],
                     "runtime_classpath": ["runtime-only.jar"], "preparation_inputs": ["changed-input"],
                     "skipped_build_steps": ["formatter"], "skipped_compiler_checks": ["lint"]} for m in plan.modules)
    assert shared.launch_compilation_configuration(plan) == shared.launch_compilation_configuration(replace(plan, modules=changed))
    assert shared.test_compilation_configuration(world_for(plan)) == shared.test_compilation_configuration(
        world_for(replace(plan, modules=changed), test_jvm_arguments=("-Dlabel=new",),
                   test_run_order="alphabetical", tests=("OtherTest",)))


@pytest.mark.parametrize("tests", [False, True])
def test_created_compiler_uses_exactly_the_identity_options_without_rescanning(plan, tmp_path, monkeypatch, tests):
    config = (shared.test_compilation_configuration(world_for(plan)) if tests
              else shared.launch_compilation_configuration(plan))
    # Any option passed to the constructor is automatically part of identity.
    config["options"]["timeout"] = 42.0
    saved = json.loads(json.dumps(config))
    seen = []
    monkeypatch.setattr(shared, "PersistentJdtCompileSession", lambda **kw: seen.append(kw) or object())
    def unexpected(*args, **kwargs):
        raise AssertionError("restoring saved compiler configuration must not re-scan libraries")
    monkeypatch.setattr(shared, "discover_target_system_entries", unexpected)
    monkeypatch.setattr(shared, "build_world_fingerprint", unexpected)
    store = JdtWorkspaceStore(tmp_path / "cache")
    args = dict(store=store, project_root=plan.project_root, module_root=plan.module_root,
                candidate=SimpleNamespace(root=tmp_path / "candidate"),
                worker_java=SimpleNamespace(home=tmp_path / "worker-jdk", major=21))
    lease, _ = shared.open_compile_session(saved, **args)
    (lease.root / "workspace").mkdir(parents=True)
    lease.mark_initialized()
    again, _ = shared.open_compile_session(saved, **args)
    assert again.reusable
    for actual in seen:
        options = {k: v for k, v in actual.items() if k not in {"root", "candidate", "worker_java_home", "preserve_root_on_close"}}
        assert json.loads(json.dumps(options, default=str)) == lease.identity["options"]
    assert config == saved  # No mutation of the cached dictionary.


def test_fast_test_cache_restores_compiler_options_without_recalculating(model, monkeypatch):
    configuration = shared.test_compilation_configuration(model.world)
    world = replace(model.world, compilation_configuration=configuration)
    model.fast.save(world, model.toolchain)
    def unexpected(*args, **kwargs):
        raise AssertionError("reading a cached model must not rediscover compilation inputs")
    monkeypatch.setattr(shared, "capture_compilation_configuration", unexpected)
    monkeypatch.setattr(shared, "discover_target_system_entries", unexpected)
    monkeypatch.setattr(shared, "build_world_fingerprint", unexpected)
    original_read = Path.read_bytes
    def small_configuration_only(path):
        assert path.suffix not in {".java", ".class", ".jar"}, path
        return original_read(path)
    monkeypatch.setattr(Path, "read_bytes", small_configuration_only)
    restored, _ = model.fast.load(world.project_root, world.build_system)
    assert restored.compilation_configuration == configuration


def test_shared_acceptance_saves_the_source_index_before_workspace_identity():
    events = []
    compiler = SimpleNamespace(accept_baseline=lambda: events.append("baseline"),
                               save_source_index=lambda: events.append("source-index"))
    workspace = SimpleNamespace(mark_initialized=lambda: events.append("identity"))
    shared.accept_compile_session(compiler, workspace)
    assert events == ["baseline", "source-index", "identity"]


@pytest.mark.parametrize("reuse,changed,ok", [(False, False, True), (False, False, False),
                                             (True, False, True), (True, False, False),
                                             (True, True, True), (True, True, False)])
def test_shared_initialization_uses_one_build_and_preserves_errors(reuse, changed, ok):
    events = []
    opened = SimpleNamespace(compile_ok=ok, actual_build_kind=None if reuse else "FULL")
    increment = SimpleNamespace(compile_ok=ok, actual_build_kind="INCREMENTAL")
    compiler = SimpleNamespace(
        start=lambda **kw: events.append(("start", kw)) or opened,
        workspace_source_changes=lambda: events.append("scan") or ((Path("A.java"),) if changed else ()),
        compile=lambda paths: events.append(("compile", paths)) or increment,
    )
    result = shared.initialize_compile_session(compiler, reuse_workspace=reuse)
    assert result is (increment if reuse and changed else opened)
    assert result.compile_ok is ok
    assert events == [("start", {"reuse_workspace": True, "build_on_reuse": False} if reuse else {})] + (
        ["scan"] + ([("compile", (Path("A.java"),))] if changed else []) if reuse else [])
