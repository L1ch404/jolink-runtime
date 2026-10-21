"""Maven resource selection must not turn ordinary edits into Probe invalidations."""

import json
from dataclasses import replace

import pytest
from test_build_configuration import model

from jolink_runtime.launch.configuration_inputs import preparation_stamps
from jolink_runtime.launch.jdt_modules import compilation_modules


@pytest.mark.parametrize("kind", ["test", "launch"])
def test_both_caches_follow_resource_patterns_and_preserve_them_on_disk(model, kind):
    root = model.root / "src/main/java"
    selected = root / "mapper.xml"
    selected.write_text("<mapper/>", encoding="utf-8")
    resources = ({"directory": str(root), "includes": ["**/*.xml"],
                  "excludes": ["ignored/**", "**/.git/**"]},)
    model.plan = replace(model.plan, preparation_resource_sets=resources)
    model.world = replace(model.world, modules=({
        "module_root": str(model.root), "preparation_resource_sets": resources,
    },))
    model.save(kind)
    for name in ("App.java", "debug.py", "results.json", "ignored/new.xml", ".git/config.xml"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        for content in ("new", "edited temporary content"):
            path.write_text(content, encoding="utf-8")
            assert model.load(kind) is not None, name
        path.unlink()
        assert model.load(kind) is not None, name

    restored = model.load(kind)
    if kind == "launch":
        assert restored.jdt_plan.preparation_resource_sets == resources
    else:
        world, _ = restored
        assert world.modules[0]["preparation_resource_sets"] == list(resources)

    selected.write_text("<mapper>changed</mapper>", encoding="utf-8")
    assert model.load(kind) is None
    model.save(kind)
    added = root / "nested/new.xml"
    added.parent.mkdir()
    added.write_text("<new/>", encoding="utf-8")
    assert model.load(kind) is None
    model.save(kind)
    added.unlink()
    assert model.load(kind) is None


def test_resource_selection_is_not_an_extension_denylist(tmp_path):
    root = tmp_path / "resources"
    resources = ({"directory": str(root), "includes": ["**/*"], "excludes": []},)
    empty = preparation_stamps((), resource_sets=resources)
    root.mkdir()
    assert preparation_stamps((), resource_sets=resources) == empty
    for name in ("sample.java", "probe.py", "fixture.json"):
        before = preparation_stamps((), resource_sets=resources)
        path = root / name
        path.write_text("resource content", encoding="utf-8")
        assert preparation_stamps((), resource_sets=resources) != before


def test_resource_patterns_do_not_override_generator_inputs(tmp_path):
    path = tmp_path / "GeneratorInput.java"
    path.write_text("old", encoding="utf-8")
    resources = ({"directory": str(tmp_path), "includes": ["*.xml"]},)
    before = preparation_stamps((path,), resource_sets=resources)
    path.write_text("new generator input", encoding="utf-8")
    assert preparation_stamps((path,), resource_sets=resources) != before


def test_resource_bookkeeping_is_not_a_compiler_configuration_change():
    original = ({"module_root": "app", "source_roots": ["src"]},)
    changed = ({**original[0], "preparation_resource_sets": [{
        "directory": "src", "includes": ["*.xml"], "excludes": [],
    }]},)
    assert compilation_modules(original) == compilation_modules(changed)


@pytest.mark.parametrize("kind,old_schema", [
    ("test", "jolink.fast-test-world.v11"), ("launch", "jolink.project-launch-cache.v7"),
])
def test_old_unfiltered_model_is_refreshed_without_deleting_compiled_outputs(model, kind, old_schema):
    model.save(kind)
    file = (model.fast._directory(model.root, "maven") / "build-world.json"
            if kind == "test" else model.launch._file(model.root, model.intent.launch_name))
    data = json.loads(file.read_text(encoding="utf-8"))
    data["schema"] = old_schema
    file.write_text(json.dumps(data), encoding="utf-8")
    compiled = model.root / "target/classes/Keep.class"
    compiled.write_bytes(b"existing bytecode")
    assert model.load(kind) is None
    assert compiled.read_bytes() == b"existing bytecode"
