import os
from types import SimpleNamespace

import pytest

from jolink_runtime.launch.gradle_native_session import GradleNativeSession
from jolink_runtime.launch.jdt_compile_session import JdtCompileError


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
