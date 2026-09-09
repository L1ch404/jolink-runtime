"""No-build reopen/shutdown must retain the Resources builder's saved tree."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate,
    PersistentJdtCompileSession,
    discover_target_system_entries,
)
from jolink_runtime.launch.jdt_modules import ModuleCompileSession, module_name
from jolink_runtime.launch.jdt_workspace_store import JdtWorkspaceStore


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("level", [8, 11])
@pytest.mark.parametrize("multi_module", [False, True], ids=["single", "modules"])
def test_noop_reopen_preserves_incremental_tree(
    tmp_path: Path, level: int, multi_module: bool
) -> None:
    if os.environ.get("JOLINK_RUN_MCP_JAVA_E2E") != "1":
        pytest.skip("set JOLINK_RUN_MCP_JAVA_E2E=1")
    home = os.environ.get(f"JOLINK_TEST_JAVA{level}_HOME")
    if not home:
        pytest.skip(f"set JOLINK_TEST_JAVA{level}_HOME")
    java = Path(home)
    project = tmp_path / "工程 with spaces"
    base = project / "base" if multi_module else project
    app = project / "app" if multi_module else project
    for root in {base, app}:
        (root / "src").mkdir(parents=True)
    leaf = base / "src/Leaf.java"

    def edit(value: int) -> None:
        leaf.write_text(
            f"public class Leaf {{ public static int value() {{ return {value}; }} }}",
            encoding="utf-8",
        )

    edit(1)
    (app / "src/App.java").write_text(
        "public class App { public static void main(String[] args) {"
        " System.out.print(Leaf.value()); } }",
        encoding="utf-8",
    )
    (app / "src/Unrelated.java").write_text(
        "public class Unrelated {}", encoding="utf-8"
    )
    candidate = JdtCandidate.load_product()
    libraries = discover_target_system_entries(java, level)
    store = JdtWorkspaceStore(tmp_path / "cache")
    identity = {"worker": candidate.root.name, "source_level": level}

    def open_compiler():
        lease = store.claim(project_root=project, module_root=app, identity=identity)
        kwargs = dict(
            root=lease.root,
            candidate=candidate,
            worker_java_home=java,
            source_roots=(app / "src",),
            classpath_entries=libraries,
            source_encoding="UTF-8",
            source_level=level,
            preserve_root_on_close=True,
        )
        if not multi_module:
            return lease, PersistentJdtCompileSession(**kwargs)
        modules = tuple(
            dict(
                module_root=str(root),
                source_roots=[str(root / "src")],
                output_directory=str(root / "target/classes"),
                test_output_directory=str(root / "target/test-classes"),
                target_java_home=str(java),
                source_level=level,
                source_encoding="UTF-8",
                classpath=[] if root == base else [str(base / "target/classes")],
            )
            for root in (base, app)
        )
        return lease, ModuleCompileSession(modules=modules, target_module=app, **kwargs)

    def assert_value(compiler, value):
        outputs = (
            [
                compiler.private_project / module_name(root) / "bin"
                for root in (base, app)
            ]
            if multi_module
            else [compiler.output_directory]
        )
        suffix = ".exe" if os.name == "nt" else ""
        actual = subprocess.run(
            [
                str(java / f"bin/java{suffix}"),
                "-cp",
                os.pathsep.join(map(str, outputs)),
                "App",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        assert actual.stdout == str(value)

    lease, compiler = open_compiler()
    try:
        assert not lease.reusable
        full = compiler.start()
        assert full.compile_ok, full.diagnostics
        assert full.actual_build_kind == "FULL"
        assert full.compiled_source_count == 3
        compiler.accept_baseline()
        lease.mark_initialized()
        assert_value(compiler, 1)
    finally:
        assert compiler.close()

    # No BUILD at all: these Workers only restore and save the old build tree.
    # The old Resources implementation dropped its builder record on shutdown.
    for _ in range(3):
        lease, compiler = open_compiler()
        try:
            assert lease.reusable
            reopened = compiler.start(reuse_workspace=True, build_on_reuse=False)
            assert reopened.compile_ok
            assert reopened.actual_build_kind is None
            assert reopened.compiled_source_count == 0
            assert compiler._worker_ready_frame["workspace_project_state"] == "reopened"
            assert compiler.workspace_source_changes() == ()
            assert_value(compiler, 1)
        finally:
            assert compiler.close()

    # Prove actual incremental work and runtime output, not just state.dat presence.
    for value in (2, 1):
        edit(value)
        lease, compiler = open_compiler()
        try:
            assert lease.reusable
            assert compiler.start(reuse_workspace=True, build_on_reuse=False).compile_ok
            changes = compiler.workspace_source_changes()
            assert changes == (leaf,)
            built = compiler.compile(changes)
            assert built.compile_ok, built.diagnostics
            assert built.actual_build_kind == "INCREMENTAL"
            assert built.compiled_source_count == 1, built.compiled_source_units
            assert built.compiled_source_units[0].endswith("src/Leaf.java")
            assert_value(compiler, value)
        finally:
            assert compiler.close()
