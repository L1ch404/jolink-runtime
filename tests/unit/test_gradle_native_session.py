import os
from types import SimpleNamespace

import pytest

from jolink_runtime.launch.gradle_native_session import GradleNativeSession
from jolink_runtime.launch.jdt_compile_session import JdtCompileError


def test_versioned_descriptor_is_not_a_classpath_root_or_redefinable_class(tmp_path):
    from jolink_runtime.launch.gradle_module_world import _runtime_output
    from jolink_runtime.launch.jpms_runtime import module_runtime

    classes = tmp_path / "classes"
    versioned = classes / "META-INF/versions/9"
    versioned.mkdir(parents=True)
    (versioned / "module-info.class").write_bytes(b"versioned descriptor")
    (classes / "Main.class").write_bytes(b"base class")
    primary = _runtime_output({"classesDirectories": [str(classes)]}, str(versioned))
    model = {"module_root": str(tmp_path), "source_roots": [], "test_source_roots": [],
             "module_name": "demo", "output_directory": primary, "test_output_directory": str(tmp_path / "test"),
             "native_compile": {"build_root": str(tmp_path), "tasks": [":classes"], "outputs": [str(classes)],
                                "resource_inputs": []}}
    compiler = GradleNativeSession(tmp_path / "private", (model,), tmp_path, tmp_path / "jdk")
    try:
        result = compiler.start(run_operation=lambda spec: SimpleNamespace(succeeded=True))
        private, = compiler.runtime_classpath([classes])
        assert private == compiler.output_directory and (private / "Main.class").exists()
        assert module_runtime(compiler, (private,)).name is None
        assert result.runtime_changed_classes == (private.name + "/Main.class",)
        assert result.runtime_changed_resources == (private.name + "/META-INF/versions/9/module-info.class",)
    finally:
        compiler.close()


def test_native_snapshots_follow_successful_builds_and_keep_gradle_output_order(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "gradlew").touch()
    (project / "gradlew.bat").touch()
    java, groovy, resources = [project / "build" / n for n in ("java", "groovy", "resources")]
    src = project / "mixed"
    src.mkdir()
    source = src / "Foreign.groovy"
    source.write_text("one")
    model = {"module_root": str(project), "source_roots": [str(src)], "test_source_roots": [],
             "output_directory": str(java), "test_output_directory": str(project/"test"),
             "native_compile": {"build_root": str(tmp_path), "tasks": [":classes"], "outputs": list(map(str,(java,groovy,resources))),
                                "resource_inputs": []}}
    def session():
        return GradleNativeSession(tmp_path/"private", (model,), project, tmp_path/"jdk")
    compiler = session()
    def success(spec):
        # The selected directory even has its own wrapper, but the Probe's
        # evaluated Gradle root owns this task graph.
        assert spec.cwd == tmp_path and spec.argv[-1] == ":classes"
        assert "clean" not in spec.argv and "test" not in spec.argv
        return SimpleNamespace(succeeded=True)
    groovy.mkdir(parents=True)
    binary = groovy / "Foreign.class"
    binary.write_bytes(b"one")
    result = compiler.start(run_operation=success)
    assert result.actual_build_kind == "GRADLE_NATIVE" and result.compiled_source_count is None
    key, = result.runtime_changed_classes
    name, private = compiler.class_file(key)
    assert name == binary.name and private.read_bytes() == b"one" and private != binary
    external = project/"third-party.jar"
    assert compiler.runtime_classpath([groovy,java,external]) == (private.parent,compiler.output_directory,external)
    compiler.mark_published()
    compiler.close()

    restored = session()
    def unexpected(_):
        pytest.fail("Gradle must not start on an unchanged warm workspace")
    assert restored.start(reuse_workspace=True, run_operation=unexpected).actual_build_kind is None
    source.write_text("new version")
    binary.write_bytes(b"partial native output")
    def failed(spec):
        spec.output_capture.write_text("compile error")
        return SimpleNamespace(succeeded=False,return_code=1)
    with pytest.raises(JdtCompileError, match="Gradle compilation"):
        restored._build(failed)
    assert private.read_bytes() == b"one" and not restored.pending
    assert restored.workspace_source_changes() == (source,)
    binary.write_bytes(b"new version")
    assert restored._build(success).runtime_changed_classes == (key,)
    assert private.read_bytes() == b"new version"
    restored.mark_published()
    source.unlink()
    binary.unlink()
    assert restored._build(success).runtime_deleted_classes == (key,)
    assert not private.exists()
    restored.close()


def test_successful_build_can_replace_same_size_and_timestamp(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    (project/"gradlew").touch()
    (project/"gradlew.bat").touch()
    out = project/"classes"
    out.mkdir()
    binary = out/"Work.class"
    binary.write_bytes(b"old")
    model = {"module_root": str(project), "source_roots": [], "test_source_roots": [],
             "output_directory": str(out), "test_output_directory": str(project/"test"),
             "native_compile": {"build_root": str(project), "tasks": [":classes"], "outputs": [str(out)], "resource_inputs": []}}
    c = GradleNativeSession(tmp_path/"private", (model,), project, tmp_path/"jdk")
    ok = lambda spec: SimpleNamespace(succeeded=True)
    key, = c.start(run_operation=ok).runtime_changed_classes
    c.mark_published()
    stamp = binary.stat()
    binary.write_bytes(b"new")
    os.utime(binary, ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    assert c._build(ok).runtime_changed_classes == (key,)
    assert c.class_file(key)[1].read_bytes() == b"new"
    c.close()


def test_declared_output_tracks_static_siblings_not_producer_writes(tmp_path):
    project = tmp_path / "project"
    classes = project / "build/classes"
    extra = project / "build/extra"
    nested = extra / "generated"
    inputs = project / "payload"
    plan = {"build_root": str(project), "tasks": [":classes"],
            "resource_inputs": [], "outputs": list(map(str, (classes, extra))),
            "extraUnownedDirs": [str(extra)], "extraGeneratedPaths": [str(nested)],
            "extraInputTrees": [{"directory": str(inputs), "includes": ["**/*.txt"],
                                 "excludes": ["ignored/**"]}],
            "extraInputFiles": [str(classes), str(nested)]}
    model = {"module_root": str(project), "source_roots": [], "test_source_roots": [],
             "output_directory": str(classes), "test_output_directory": str(project / "test"),
             "native_compile": plan}
    native = GradleNativeSession(tmp_path / "private", (model,), project, tmp_path / "jdk")
    def generate(_):
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "payload.raw").write_text("generated")
        return SimpleNamespace(succeeded=True)
    try:
        native.start(run_operation=generate)
        assert not native.workspace_source_changes()  # building didn't invalidate its own inputs
        private, = native.runtime_classpath([extra])
        sibling = extra / "static.txt"
        sibling.write_text("original")
        included = inputs / "new.txt"
        ignored = inputs / "ignored/also.txt"
        ignored.parent.mkdir(parents=True)
        ignored.write_text("unrelated")
        included.write_text("new")
        assert set(native.workspace_source_changes()) == {sibling, included}
        native._build(generate)
        assert (private / "static.txt").read_text() == "original"
        sibling.unlink()
        included.unlink()
        assert set(native.workspace_source_changes()) == {sibling, included}
        native._build(generate)
        assert not (private / "static.txt").exists() and not native.workspace_source_changes()
    finally:
        native.close()


def test_literal_root_scoped_copy_inputs_do_not_walk_project(tmp_path, monkeypatch):
    from jolink_runtime.launch.native_compile_session import _producer_files

    license_file = tmp_path / "LICENSE"
    license_file.write_text("notice")
    output = tmp_path / "build"
    output.mkdir()
    (output / "NOTICE").touch()
    def unexpected_walk(*args):
        pytest.fail("a literal root-scoped input must not enumerate unrelated directories")
    monkeypatch.setattr(os, "walk", unexpected_walk)
    spec = {"includes": ["LICENSE", "build/NOTICE", "not-created-yet"]}
    assert list(_producer_files(tmp_path, spec, {output})) == [license_file]


def test_case_insensitive_producer_tracks_real_filenames_and_honors_excludes(tmp_path):
    from jolink_runtime.launch.native_compile_session import _producer_files

    for name in ("license", "new.txt", "ignored.txt"):
        (tmp_path / name).touch()
    for patterns, expected in [(["LICENSE"], {"license"}), (["*.TXT"], {"new.txt"})]:
        spec = {"caseSensitive": False, "includes": patterns, "excludes": ["IGNORED.TXT"]}
        assert {p.name for p in _producer_files(tmp_path, spec, set())} == expected
