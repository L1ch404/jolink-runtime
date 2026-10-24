"""Probe freshness and JDT workspace reuse are separate decisions."""

from dataclasses import replace
import logging
import os
from pathlib import Path

import pytest

from jolink_runtime.launch.jdt_compile_session import JdtBuildWorldPlan
from jolink_runtime.launch.jdt_modules import module_name
from jolink_runtime.launch.jdt_workspace_store import JdtWorkspaceStore
from jolink_runtime.launch.project_launch_cache import stabilize_jdt_plan


@pytest.fixture
def plan(tmp_path, monkeypatch):
    monkeypatch.setattr("jolink_runtime.launch.jdt_compilation.discover_target_system_entries",
                        lambda home, level: (Path(home) / "lib" / f"platform-{level}.jar",))
    project = tmp_path / "project"
    source = project / "src/main/java/example/App.java"
    source.parent.mkdir(parents=True)
    source.write_text("package example; class App { int value = 1; }")
    (project / "pom.xml").write_text("<project/>")
    (project / "settings.xml").write_text("<settings/>")
    home = tmp_path / "jdk"
    (home / "bin").mkdir(parents=True)
    javac = home / "bin" / ("javac.exe" if os.name == "nt" else "javac")
    javac.write_bytes(b"compiler")
    dependency = tmp_path / "library.jar"
    dependency.write_bytes(b"library")
    return JdtBuildWorldPlan(
        project_root=project, module_root=project,
        source_roots=(source.parent.parent,), dependency_entries=(dependency,),
        processor_entries=(), lombok_entries=(), target_java_home=home,
        source_encoding="UTF-8", source_level=8, target_level=8,
        fingerprint="probe-inputs", configuration_inputs=(project / "pom.xml", project / "settings.xml"),
        configuration_environment_names=(), javac_executable=javac,
    )


def stabilized(plan):
    return stabilize_jdt_plan(plan, attempt_directory=plan.project_root / "attempt")


@pytest.mark.parametrize("change", ["source", "pom_comment", "settings_comment", "temporary_json", "ambient_java_home", "build_jdk"])
def test_non_compiler_changes_keep_workspace(plan, tmp_path, monkeypatch, change):
    before = stabilized(plan)
    store = JdtWorkspaceStore(tmp_path / "cache")
    lease = store.claim(project_root=plan.project_root, module_root=plan.module_root,
                        identity=before.compilation_configuration)
    marker = lease.root / "workspace" / "builder.state"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"saved incremental state")
    lease.mark_initialized()
    lease.release(clean=True)
    if change == "source":
        (plan.source_roots[0] / "example/App.java").write_text("package example; class App { int value = 2; }")
    elif change == "pom_comment":
        (plan.project_root / "pom.xml").write_text("<project><!-- comment --></project>")
    elif change == "settings_comment":
        (plan.project_root / "settings.xml").write_text("<settings><!-- comment --></settings>")
    elif change == "temporary_json":
        (plan.project_root / "scratch.json").write_text('{}')
    elif change == "ambient_java_home":
        monkeypatch.setenv("JAVA_HOME", str(tmp_path / "unselected-jdk"))
    else:
        plan = replace(plan, javac_executable=tmp_path / "different-build-jdk/bin/javac")
    after = stabilized(plan)
    assert after.fingerprint == before.fingerprint
    reopened = store.claim(project_root=plan.project_root, module_root=plan.module_root,
                           identity=after.compilation_configuration)
    assert reopened.reusable and marker.read_bytes() == b"saved incremental state"


@pytest.mark.parametrize("field,value", [
    ("source_roots", (Path("other-sources"),)),
    ("dependency_entries", (Path("other-library.jar"),)),
    ("source_encoding", "GBK"), ("source_level", 17),
    ("target_java_home", Path("other-target-jdk")),
    ("method_parameters", True),
    ("processor_entries", (Path("processor.jar"),)),
    ("processor_names", ("example.Processor",)),
    ("processor_options", {"format": "other"}),
    ("lombok_entries", (Path("lombok.jar"),)),
    ("worker_max_heap_mb", 1024),
])
def test_actual_compiler_changes_invalidate_identity(plan, field, value):
    assert stabilized(replace(plan, **{field: value})).fingerprint != stabilized(plan).fingerprint


def test_dependency_replacement_still_invalidates(plan):
    before = stabilized(plan)
    plan.dependency_entries[0].write_bytes(b"replacement library")
    assert stabilized(plan).fingerprint != before.fingerprint


def modules_plan(plan):
    root = plan.project_root
    lib = {
        "module_root": str(root / "lib"), "source_roots": [str(root / "lib/src")],
        "test_source_roots": [], "classpath": [], "test_classpath": [],
        "output_directory": str(root / "lib/target/classes"),
        "test_output_directory": str(root / "lib/target/test-classes"),
        "source_level": 8, "source_encoding": "UTF-8", "target_java_home": str(plan.target_java_home),
        "processor_entries": [], "processor_names": [], "processor_options": {},
    }
    app = {**lib, "module_root": str(root / "app"),
           "source_roots": [str(p) for p in plan.source_roots],
           "output_directory": str(root / "app/target/classes"),
           "test_output_directory": str(root / "app/target/test-classes"),
           "classpath": [lib["output_directory"], *map(str, plan.dependency_entries)]}
    return replace(plan, modules=(lib, app), dependency_entries=tuple(map(Path, app["classpath"])))


def test_managed_module_outputs_are_not_dependency_replacements(plan):
    plan = modules_plan(plan)
    before = stabilized(plan)
    output = Path(plan.modules[0]["output_directory"]) / "Value.class"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"incremental output")
    assert stabilized(plan).fingerprint == before.fingerprint
    output.write_bytes(b"changed incremental output")
    assert stabilized(plan).fingerprint == before.fingerprint


@pytest.mark.parametrize("field,value", [
    ("source_level", 17), ("source_encoding", "GBK"),
    ("source_roots", ["generated"]), ("source_patterns", {"excludes": ["skip/**"]}),
    ("classpath", ["extra.jar"]), ("processor_entries", ["extra-processor.jar"]),
    ("processor_names", ["example.Processor"]), ("processor_options", {"key": "value"}),
    ("processor_options", {"flag": None}),
])
def test_upstream_module_configuration_is_part_of_identity(plan, field, value, caplog, tmp_path):
    plan = modules_plan(plan)
    before = stabilized(plan)
    store = JdtWorkspaceStore(tmp_path / "cache")
    lease = store.claim(project_root=plan.project_root, module_root=plan.module_root,
                        identity=before.compilation_configuration)
    (lease.root / "workspace").mkdir(parents=True)
    lease.mark_initialized()
    changed = {**plan.modules[0], field: value}
    after = stabilized(replace(plan, modules=(changed, plan.modules[1])))
    assert after.fingerprint != before.fingerprint
    with caplog.at_level(logging.INFO):
        assert not store.claim(project_root=plan.project_root, module_root=plan.module_root,
                               identity=after.compilation_configuration).reusable
    expected = f'options.modules.{module_name(changed["module_root"])}.{field}'
    assert expected in caplog.text
    assert "changed_identity_fields" in caplog.text


def test_preparation_and_runtime_settings_do_not_change_compiler_identity(plan):
    plan = modules_plan(plan)
    before = stabilized(plan)
    changed = {**plan.modules[0], "resource_roots": ["different resources"],
               "runtime_classpath": ["runtime-only.jar"],
               "preparation_inputs": ["generator.input"], "skipped_build_steps": ["formatter"]}
    assert stabilized(replace(plan, modules=(changed, plan.modules[1]))).fingerprint == before.fingerprint


def test_dependency_order_is_not_sorted_away(plan, tmp_path):
    other = tmp_path / "another.jar"
    other.write_bytes(b"library")
    first = replace(plan, dependency_entries=(*plan.dependency_entries, other))
    second = replace(first, dependency_entries=tuple(reversed(first.dependency_entries)))
    assert stabilized(first).fingerprint != stabilized(second).fingerprint


def test_identity_diagnostics_are_lazy_and_only_report_field_names(plan, tmp_path, caplog, monkeypatch):
    store = JdtWorkspaceStore(tmp_path / "cache")
    first = store.claim(project_root=plan.project_root, module_root=plan.module_root,
                        identity={"processor_options": {"credential": "old-private-value"}})
    (first.root / "workspace").mkdir(parents=True)
    first.mark_initialized()
    with caplog.at_level(logging.INFO):
        second = store.claim(project_root=plan.project_root, module_root=plan.module_root,
                             identity={"processor_options": {"credential": "new-private-value"}})
    assert "processor_options.credential" in caplog.text
    assert "old-private-value" not in caplog.text and "new-private-value" not in caplog.text
    (second.root / "workspace").mkdir(parents=True)
    second.mark_initialized()
    def unexpected(*args):
        raise AssertionError("disabled diagnostics must not calculate field differences")
    monkeypatch.setattr("jolink_runtime.launch.jdt_workspace_store._changed_fields", unexpected)
    with caplog.at_level(logging.WARNING):
        assert store.claim(project_root=plan.project_root, module_root=plan.module_root,
                           identity=second.identity).reusable
