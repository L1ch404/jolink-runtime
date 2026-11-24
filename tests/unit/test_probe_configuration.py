"""Probe races use configuration content, never business-source scans or old values."""

import hashlib
import json
import os
from dataclasses import dataclass
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


def test_gradle_build_under_configuration_directory_tracks_inputs_not_its_outputs(tmp_path):
    directory = tmp_path / "gradle"
    included = directory / "plugins"
    settings = included / "settings.gradle.kts"
    source = included / "src/Convention.kt"
    lock = included / ".gradle/history.lock"
    output = included / "build/plugin.jar"
    explicit = included / "build/generated.gradle"
    for file in (settings, source, lock, output, explicit):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("original")
    # An applied script is still tracked as an explicit input wherever it is.
    inputs = (directory, explicit)
    capture = ProbeConfiguration.capture(tmp_path, "gradle", inputs)
    lock.write_text("new lock")
    output.write_text("new output")
    assert str(lock) not in capture.finish(inputs)
    for path in (source, explicit, settings):
        path.write_text("changed")
        with pytest.raises(ProbeConfigurationChanged) as error:
            capture.finish(inputs)
        assert str(path) in error.value.context["changed_configuration_files"]
        path.write_text("original")


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


@pytest.mark.parametrize("change", ["modify", "delete", "same_metadata"])
def test_native_discovery_baseline_rejects_changes_before_python_knew_the_path(configuration, change):
    root, system, path = configuration
    external = root.parent / "external.config"
    external.write_text("old native input")
    before = external.stat()
    native = {"configurationStamps": {str(external): hashlib.sha256(external.read_bytes()).hexdigest()}}
    captured = ProbeConfiguration.capture(root, system, (path,))
    if change == "delete":
        external.unlink()
    else:
        external.write_text("new native input")
        if change == "same_metadata":
            os.utime(external, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ProbeConfigurationChanged) as error:
        captured.finish((path,), native=(native,))
    assert error.value.context["changed_configuration_files"] == [str(external)]


def test_native_model_mismatch_is_not_erased_when_current_bytes_match(configuration):
    root, system, path = configuration
    native = {"configurationStamps": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()},
              "configurationChanges": [str(path)]}
    with pytest.raises(ProbeConfigurationChanged):
        ProbeConfiguration.capture(root, system, (path,)).finish((path,), native=(native,))


@pytest.mark.parametrize("scenario", [
    "stable", "not-known-before", "missing-native-stamp", "conflicting-native-stamp",
    "bytes-changed", "file-replaced", "hard-error-in-second-snapshot",
])
def test_only_a_corroborated_maven_model_difference_is_not_a_file_change(tmp_path, scenario):
    root = tmp_path / "project"
    root.mkdir()
    path = (tmp_path if scenario == "not-known-before" else root) / "parent.pom"
    path.write_text("<project/>")
    initial = ProbeConfiguration.capture(root, "maven", () if scenario == "not-known-before" else (path,))
    key = str(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    model = {"configurationChanges": [key], "configurationModelMismatches": [key],
             "configurationStamps": {key: digest}}
    snapshots = [model]
    if scenario == "stable":
        assert initial.finish((path,), native=snapshots)[key] == digest
        return
    if scenario == "missing-native-stamp":
        model["configurationStamps"].clear()
    elif scenario == "conflicting-native-stamp":
        model["configurationStamps"][key] = "0" * 64
    elif scenario == "bytes-changed":
        path.write_text("<changed/>")
    elif scenario == "file-replaced":
        # Same contents and mtime do not establish that Maven read this file.
        other = path.with_suffix(".replacement")
        other.write_bytes(path.read_bytes())
        stamp = path.stat()
        os.utime(other, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        other.replace(path)
    elif scenario == "hard-error-in-second-snapshot":
        snapshots.append({"configurationChanges": [key], "configurationStamps": {key: digest}})
    with pytest.raises(ProbeConfigurationChanged) as error:
        initial.finish((path,), native=snapshots)
    assert error.value.context["changed_configuration_files"] == [key]


def test_conflicting_native_observations_are_not_last_wins(configuration):
    root, system, path = configuration
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ProbeConfigurationChanged):
        ProbeConfiguration.capture(root, system, (path,)).finish((path,), native=(
            {"configurationStamps": {str(path): "earlier different input"}},
            {"configurationStamps": {str(path): digest}},
        ))


def test_native_paths_remain_with_the_model_for_later_cache_checks(configuration):
    root, system, path = configuration
    external = root.parent / "external.config"
    external.write_text("native input")
    @dataclass(frozen=True)
    class Model:
        configuration_inputs: tuple
        configuration_stamps: dict | None = None
    original = Model((path,))
    native = {"configurationStamps": {str(external): hashlib.sha256(external.read_bytes()).hexdigest()}}
    result = ProbeConfiguration.capture(root, system, (path,)).bind(original, native=(native,))
    assert result.configuration_inputs == (path, external)
    assert result.configuration_stamps[str(external)] == native["configurationStamps"][str(external)]
    assert original.configuration_stamps is None and original.configuration_inputs == (path,)
    native["configurationStamps"][str(external)] = "later mutation"
    assert result.configuration_stamps[str(external)] != "later mutation"


def test_native_consumption_can_follow_legitimate_generation(configuration):
    root, system, path = configuration
    sources = root / "build-logic-source-set"
    sources.mkdir()
    generated = sources / "build-logic-input.txt"
    generated.write_text("old generated configuration")
    captured = ProbeConfiguration.capture(root, system, (path, generated))
    generated.write_text("new configuration consumed by native build")
    native = {"configurationStamps": {str(sources): "directory", str(generated): hashlib.sha256(generated.read_bytes()).hexdigest()},
              "configurationSourceDirectories": [str(sources)]}
    result = captured.finish((path, generated), native=(native,))
    assert result[str(generated)] == native["configurationStamps"][str(generated)]
    generated.write_text("edited after native consumption")
    with pytest.raises(ProbeConfigurationChanged):
        captured.finish((path, generated), native=(native,))


def test_late_native_config_stamp_cannot_override_known_properties(configuration):
    root, system, path = configuration
    captured = ProbeConfiguration.capture(root, system, (path,))
    path.write_text(path.read_text().replace("old", "new"))
    native = {"configurationStamps": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}}
    with pytest.raises(ProbeConfigurationChanged):
        captured.finish((path,), native=(native,))


@pytest.mark.parametrize("change", ["add", "delete"])
def test_declared_source_set_generation_before_consumption_is_valid(tmp_path, change):
    source = tmp_path / "source-set"
    source.mkdir()
    file = source / "generated.input"
    if change == "delete":
        file.write_text("old generated input")
    captured = ProbeConfiguration.capture(tmp_path, "gradle", (source,))
    if change == "delete":
        file.unlink()
    else:
        file.write_text("new generated input")
    stamps = module.build_configuration_stamps(tmp_path, "gradle", (source,))
    result = captured.finish((source,), native=({"configurationStamps": stamps,
        "configurationSourceDirectories": [str(source)]},))
    assert result == stamps


@pytest.mark.parametrize("change", ["add", "delete", "rename"])
def test_cold_native_directory_keeps_its_consumed_file_set(tmp_path, change):
    root = tmp_path / "discovered-source-set"
    root.mkdir()
    source = root / "One.txt"
    source.write_text("first")
    captured = ProbeConfiguration.capture(tmp_path, "gradle", ())
    native = {"configurationStamps": {str(root): "directory", str(source): hashlib.sha256(source.read_bytes()).hexdigest()}}
    if change == "delete":
        source.unlink()
    elif change == "rename":
        source.rename(root / "Two.txt")
    else:
        (root / "Two.txt").write_text("new member")
    with pytest.raises(ProbeConfigurationChanged):
        captured.finish((), native=(native,))


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
