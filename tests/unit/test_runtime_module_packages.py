"""Runtime module snapshots are explicit, per-JVM, and include upstream outputs."""

from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jolink_runtime.launch.jdt_compile_session import JdtCompileError, PersistentJdtCompileSession


def session(metadata, reply):
    compiler = object.__new__(PersistentJdtCompileSession)
    compiler._worker_ready_frame = {"module_metadata": metadata}
    compiler._operation_lock = RLock()
    compiler._client = SimpleNamespace(command=Mock(return_value=reply))
    return compiler


@pytest.mark.parametrize("metadata", [{}, {"plain": {"module_name": None}},
                                     {"patched": {"module_name": None, "patched_module_name": "example.app"}}])
def test_classpath_or_patch_only_projects_need_no_package_scan(metadata):
    compiler = session(metadata, None)
    assert compiler.capture_runtime_module_packages() == {}
    compiler._client.command.assert_not_called()


def test_named_snapshot_maps_all_module_outputs_and_excludes_patch_projects(tmp_path):
    reply = {"ok": True, "modules": {
        "app": {"module_name": "example.app", "packages": ["example", "example.internal"]},
        "lib": {"module_name": "example.lib", "packages": ["library"]},
        "test": None,
    }}
    compiler = session({"app": {"module_name": "example.app"}}, reply)
    compiler.module_runtime_scopes = lambda: {name: {"output": tmp_path / name / "bin"}
                                            for name in ("app", "lib", "test")}
    captured = compiler.capture_runtime_module_packages()
    assert captured == {tmp_path / "app/bin": ("example", "example.internal"),
                        tmp_path / "lib/bin": ("library",)}
    compiler._client.command.assert_called_once_with("MODULE_PACKAGES")
    # This is a detached snapshot, not a view that changes with later compilation.
    reply["modules"]["app"]["packages"].append("new.package")
    assert captured[tmp_path / "app/bin"] == ("example", "example.internal")


def test_failed_snapshot_does_not_return_a_partial_module_baseline():
    compiler = session({"app": {"module_name": "example.app"}},
                       {"ok": False, "message": "unreadable module descriptor"})
    with pytest.raises(JdtCompileError, match="unreadable module descriptor"):
        compiler.capture_runtime_module_packages()
