"""Completed builds can be resumed before their old Worker exits."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate,
    PersistentJdtCompileSession,
    discover_target_system_entries,
)
from jolink_runtime.launch.jdt_workspace_store import JdtWorkspaceStore


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("level", [8, 11])
def test_completed_build_persists_before_worker_exit(tmp_path: Path, level: int):
    if os.environ.get("JOLINK_RUN_MCP_JAVA_E2E") != "1":
        pytest.skip("set JOLINK_RUN_MCP_JAVA_E2E=1")
    home = os.environ.get(f"JOLINK_TEST_JAVA{level}_HOME")
    if not home:
        pytest.skip(f"set JOLINK_TEST_JAVA{level}_HOME")
    java = Path(home)
    sources = tmp_path / "project/src"
    sources.mkdir(parents=True)
    leaf = sources / "Leaf.java"
    leaf.write_text(
        "public class Leaf { public static int value() { return 1; } }",
        encoding="utf-8",
    )
    (sources / "App.java").write_text(
        "public class App { public static void main(String[] args) { System.out.print(Leaf.value()); } }",
        encoding="utf-8",
    )
    candidate = JdtCandidate.load_product()
    store = JdtWorkspaceStore(tmp_path / "cache")
    sessions = []
    identity = {"worker": candidate.root.name, "source_level": level}

    def open_compiler():
        lease = store.claim(
            project_root=sources.parent, module_root=sources.parent, identity=identity
        )
        compiler = PersistentJdtCompileSession(
            root=lease.root,
            candidate=candidate,
            worker_java_home=candidate.select_worker_java().home,
            source_roots=(sources,),
            classpath_entries=discover_target_system_entries(java, level),
            source_encoding="UTF-8",
            source_level=level,
            preserve_root_on_close=True,
        )
        sessions.append(compiler)
        return lease, compiler

    def assert_saved(compiler, value):
        # The Worker is still alive. Neither close() nor explicit SAVE was called.
        assert compiler._client.process.poll() is None
        state = (
            compiler.root
            / "workspace/.metadata/.plugins/org.eclipse.core.resources/.projects/plain-fixture/org.eclipse.jdt.core/state.dat"
        )
        assert state.is_file() and state.stat().st_size > 0
        index = json.loads(
            (compiler.root / "source-index.json").read_text(encoding="utf-8")
        )
        assert index["compile_state"] == "valid"
        assert index["sources"][str(leaf)][1] == list(compiler._source_stamp(leaf))
        suffix = ".exe" if os.name == "nt" else ""
        result = subprocess.run(
            [
                str(java / f"bin/java{suffix}"),
                "-cp",
                str(compiler.output_directory),
                "App",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        assert result.stdout == str(value)

    try:
        lease, first = open_compiler()
        assert not lease.reusable
        assert first.start().compile_ok
        first.accept_baseline()
        lease.mark_initialized()
        assert_saved(first, 1)

        # No overlapping builds: each prior operation finished before the next.
        # Keep the previous Workers idle, as when changing Agent windows.
        for value in (2, 3):
            leaf.write_text(
                f"public class Leaf {{ public static int value() {{ return {value}; }} }}",
                encoding="utf-8",
            )
            lease, compiler = open_compiler()
            assert lease.reusable
            assert compiler.start(reuse_workspace=True, build_on_reuse=False).compile_ok
            changes = compiler.workspace_source_changes()
            assert changes == (leaf,)
            built = compiler.compile(changes)
            assert built.compile_ok
            assert built.actual_build_kind == "INCREMENTAL"
            assert built.compiled_source_units == ("src/Leaf.java",)
            assert_saved(compiler, value)

        state_file = compiler.root / "source-index.json"
        before = state_file.stat().st_mtime_ns
        assert compiler.compile((leaf,)).compiled_source_count == 0
        assert state_file.stat().st_mtime_ns == before
    finally:
        # Multiple independent writers and reverse shutdown are a separate,
        # documented follow-up; close oldest first for this persistence test.
        for compiler in sessions:
            compiler.close()
