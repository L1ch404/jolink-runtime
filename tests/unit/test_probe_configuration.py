"""Probe races use configuration content, never business-source scans or old values."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from jolink_runtime.launch import probe_configuration as module
from jolink_runtime.launch.probe_configuration import (
    ProbeConfiguration,
    ProbeConfigurationChanged,
    recorded_configuration_inputs,
)


@pytest.fixture(params=["maven", "gradle"])
def configuration(tmp_path, request):
    root = tmp_path / "project"
    root.mkdir()
    path = root / ("pom.xml" if request.param == "maven" else "build.gradle")
    path.write_text("<project><name>old</name></project>" if request.param == "maven" else "version='old'")
    return root, request.param, path


@pytest.mark.parametrize("change", ["modify", "delete", "same_metadata"])
def test_changed_known_configuration_is_retryable(configuration, change):
    root, system, path = configuration
    captured = ProbeConfiguration.capture(root, system, (path,))
    before = path.stat()
    if change == "delete":
        path.unlink()
    else:
        path.write_text(path.read_text().replace("old", "new"))
        if change == "same_metadata":
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert path.stat().st_size == before.st_size
            assert path.stat().st_mtime_ns == before.st_mtime_ns
    with pytest.raises(ProbeConfigurationChanged) as error:
        captured.finish((path,))
    assert error.value.error_code == "BUILD_CONFIGURATION_CHANGED"
    assert error.value.retryable is True
    assert str(path) in error.value.context["changed_configuration_files"]
    assert error.value.context["retryable"] is True
    assert "retry" in error.value.suggested_next_step
    # A new request establishes a new baseline; there is no hidden retry loop.
    ProbeConfiguration.capture(root, system, (path,)).finish((path,))


def test_missing_known_configuration_becoming_present_is_a_change(configuration):
    root, system, path = configuration
    extra = root / "settings.xml"
    captured = ProbeConfiguration.capture(root, system, (path, extra))
    extra.write_text("<settings/>")
    with pytest.raises(ProbeConfigurationChanged) as error:
        captured.check()
    assert str(extra) in error.value.context["changed_configuration_files"]


def test_known_directory_detects_added_members_without_source_scan(tmp_path, monkeypatch):
    config = tmp_path / "gradle"
    config.mkdir()
    source = tmp_path / "src/main/java/Example.java"
    source.parent.mkdir(parents=True)
    source.write_text("class Example {}")
    original = Path.read_bytes
    def only_configuration(path):
        assert path.is_relative_to(config), "Probe guard must not hash Java or build outputs"
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", only_configuration)
    captured = ProbeConfiguration.capture(tmp_path, "gradle", (config,))
    source.write_text("class Example {int edited;}")
    captured.check()
    added = config / "versions.toml"
    added.write_text("version='new'")
    with pytest.raises(ProbeConfigurationChanged) as error:
        captured.check()
    assert error.value.context["changed_configuration_files"] == [str(added)]


def test_maven_parent_and_static_modules_are_checked(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    child = project / "child/pom.xml"
    child.parent.mkdir()
    parent = tmp_path / "parent/pom.xml"
    parent.parent.mkdir()
    parent.write_text("<project><name>old</name></project>")
    pom = project / "pom.xml"
    pom.write_text("<project><parent><relativePath>../parent/pom.xml</relativePath></parent>"
                   "<modules><module>child</module></modules></project>")
    child.write_text("<project/>")
    captured = ProbeConfiguration.capture(project, "maven", ())
    parent.write_text("<project><name>new</name></project>")
    child.write_text("<project><name>child</name></project>")
    with pytest.raises(ProbeConfigurationChanged) as error:
        captured.check()
    assert set(error.value.context["changed_configuration_files"]) == {str(child), str(parent)}


def test_finish_has_an_independent_final_input_set(configuration):
    root, system, path = configuration
    old_input = root / "previous.config"
    old_input.write_text("old input")
    captured = ProbeConfiguration.capture(root, system, (path, old_input))
    result = captured.finish((path,))
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert str(old_input) not in result
    result[str(path)] = "caller mutation"
    assert captured.finish((path,))[str(path)] == expected


def test_newly_discovered_input_has_only_a_post_probe_baseline(configuration):
    root, system, path = configuration
    external = root.parent / "external.config"
    external.write_text("before native read")
    captured = ProbeConfiguration.capture(root, system, (path,))
    external.write_text("after native read")
    captured.check()  # It was not known, so this read window is not guarded.
    result = captured.finish((path, external))
    assert result[str(external)] == hashlib.sha256(external.read_bytes()).hexdigest()


def test_finish_never_relabels_known_model_with_a_later_sample(configuration, monkeypatch):
    root, system, path = configuration
    captured = ProbeConfiguration.capture(root, system, (path,))
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    original = module.build_configuration_stamps
    calls = 0
    def change_after_check(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text(path.read_text().replace("old", "new"))
        return original(*args)
    monkeypatch.setattr(module, "build_configuration_stamps", change_after_check)
    result = captured.finish((path,))
    assert result[str(path)] == expected
    assert result[str(path)] != hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ProbeConfigurationChanged):
        captured.check()


@pytest.mark.parametrize("key", ["inputs", "configuration_stamps"])
def test_recorded_paths_ignore_stale_model_and_values(tmp_path, key):
    config = tmp_path / "build.gradle"
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({key: {str(config): {"invalid": "old value"}, "": "not a path"},
                                "world": "not a model", "schema": "obsolete"}))
    assert recorded_configuration_inputs(cache, key) == (config,)
    config.write_text("version='new'")
    captured = ProbeConfiguration.capture(tmp_path, "gradle", recorded_configuration_inputs(cache, key))
    assert captured.finish((config,))[str(config)] == hashlib.sha256(config.read_bytes()).hexdigest()


@pytest.mark.parametrize("raw", [None, "invalid JSON", "[]", '{"inputs":[]}', '{}'])
def test_missing_or_malformed_recorded_inventory_is_empty(tmp_path, raw):
    cache = tmp_path / "cache.json"
    if raw is not None:
        cache.write_text(raw)
    assert recorded_configuration_inputs(cache, "inputs") == ()
