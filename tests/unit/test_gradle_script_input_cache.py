"""Probe-supplied local scripts use small snapshots in launch and test caches."""

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_build_configuration import model as shared_model

from jolink_runtime.launch.jdt_launch_service import JdtLaunchService

model = shared_model


@pytest.mark.parametrize("kind", ["launch", "test"])
@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("change", ["edit", "same_stat", "delete"])
def test_observed_script_changes_invalidate_model_without_scanning_sources(model, monkeypatch, kind, external, change):
    root_script = model.root / "build.gradle"
    root_script.write_text("plugins { id 'java' }\n")
    parent = model.root.parent if external else model.root
    script = parent / "custom scripts/配置.inc"
    script.parent.mkdir()
    script.write_text("resourcesA\n")
    inputs = (root_script, script)
    model.world = replace(model.world, build_system="gradle", configuration_inputs=inputs)
    model.plan = replace(model.plan, configuration_inputs=inputs)
    model.save(kind)
    assert model.load(kind) is not None
    source = model.world.main_source_roots[0] / "Unrelated.java"
    source.write_text("class Unrelated {}")
    unused = model.root / "unused.gradle"
    unused.write_text("not applied")
    assert model.load(kind) is not None
    read = Path.read_bytes

    def config_only(path):
        assert path in inputs, f"unexpected scan: {path}"
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", config_only)
    stat = script.stat()
    if change == "delete":
        script.unlink()
    else:
        script.write_text("resourcesB\n")
        if change == "same_stat":
            os.utime(script, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert model.load(kind) is None
    script.write_text("resourcesA\n")
    assert model.load(kind) is not None


def test_running_runtime_retains_original_applied_script_baseline(model):
    script = model.root / "resources.gradle"
    script.write_text("resourcesA")
    world = replace(model.world, build_system="gradle", configuration_inputs=(script,))
    snapshot = model.fast.input_snapshot(world)
    plan = replace(model.plan, configuration_inputs=(script,), configuration_stamps=snapshot)
    active = SimpleNamespace(build_system="gradle", jdt_build_world_plan=plan)
    assert JdtLaunchService.configuration_rejection(active) is None
    script.write_text("resourcesB")
    model.world = world
    model.fast.save(world, model.toolchain)
    assert model.fast.is_current(model.root, "gradle")
    assert JdtLaunchService.configuration_rejection(active).data["error_code"] == "BUILD_CONFIGURATION_CHANGED"
