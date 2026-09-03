from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import shutil
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate,
    JdtCompileError,
    PersistentJdtCompileSession,
    WorkerJavaRuntime,
    lombok_worker_jvm_arguments,
    select_target_system_home,
)
from jolink_runtime.launch.jdt_workspace_store import JdtWorkspaceStore


class _FakeWorker:
    def __init__(self, session: PersistentJdtCompileSession) -> None:
        self.session = session
        self.process = SimpleNamespace(poll=lambda: None)
        self.closed = False
        self.fail_compile = False
        self.omit_compilation = False
        self.compiled_units_override = None
        self.on_build = None
        self.command_error: JdtCompileError | None = None
        self.command_count = 0
        self.resource_value: bytes | None = None
        self.delete_resource = False
        self.diagnostics = []
        self.diagnostics_truncated = False

    def command(self, command: str):
        self.command_count += 1
        if self.command_error is not None:
            raise self.command_error
        assert command.split("\t")[1] in {"FULL", "INCREMENTAL"}
        if self.on_build is not None:
            self.on_build()
        source = self.session.private_source / "example/App.java"
        output = self.session.output_directory / "example/App.class"
        output.parent.mkdir(parents=True, exist_ok=True)
        before = output.read_bytes() if output.exists() else None
        if not self.fail_compile and not self.omit_compilation:
            output.write_bytes(hashlib.sha256(source.read_bytes()).digest())
        resource = self.session.output_directory / "META-INF/generated.json"
        old_resource = resource.read_bytes() if resource.is_file() else None
        if self.delete_resource:
            for existing in self.session.output_directory.rglob("*"):
                if existing.is_file() and existing.suffix != ".class":
                    existing.unlink()
        elif self.resource_value is not None:
            resource.parent.mkdir(parents=True, exist_ok=True)
            resource.write_bytes(self.resource_value)
        after = output.read_bytes() if output.exists() else None
        return {
            "operation_ok": True,
            "compile_ok": not self.fail_compile,
            "actual_build_kind": (
                "FULL" if command.endswith("FULL") else "INCREMENTAL"
            ),
            "compiled_source_units": (
                list(self.compiled_units_override)
                if self.compiled_units_override is not None
                else ([] if self.omit_compilation else ["src/example/App.java"])
            ),
            "changed_classes": (
                ["example/App.class"] if before != after else []
            ),
            "deleted_classes": [],
            "changed_resources": (
                ["META-INF/generated.json"]
                if self.resource_value is not None and self.resource_value != old_resource
                else []
            ),
            "deleted_resources": [],
            "error_count": 1 if self.fail_compile else 0,
            "warning_count": 0,
            "diagnostic_details": list(self.diagnostics),
            "diagnostics_truncated": self.diagnostics_truncated,
        }

    def close(self) -> bool:
        self.closed = True
        return True

    def force_close(self) -> bool:
        return self.close()


def _candidate(tmp_path: Path) -> JdtCandidate:
    return JdtCandidate(
        candidate_id="test",
        root=tmp_path / "candidate",
        launcher=tmp_path / "candidate/launcher.jar",
        worker_java_sha256="unused",
        lock={},
    )


def test_portable_product_candidate_accepts_verified_minimum_worker_jdk(
    tmp_path: Path,
    monkeypatch,
) -> None:
    java_home = tmp_path / "jdk"
    java = java_home / "bin/java"
    java.parent.mkdir(parents=True)
    java.write_bytes(b"portable-java")
    candidate = JdtCandidate(
        candidate_id="product",
        root=tmp_path / "candidate",
        launcher=tmp_path / "launcher.jar",
        worker_java_sha256=None,
        lock={},
        worker_java_minimum=17,
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stderr=(
                'openjdk version "17.0.16" 2025-07-15\n'
                "    sun.arch.data.model = 64\n"
            ),
            stdout="",
        ),
    )

    runtime = candidate.verify_worker_java(java_home)
    assert runtime.executable == java
    assert runtime.major == 17
    assert runtime.data_model == 64


def test_worker_selection_prefers_build_jdk_and_skips_32_bit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    build = tmp_path / "build-jdk"
    target = tmp_path / "target-jdk"
    fallback = tmp_path / "fallback-jdk"
    for home in (build, target, fallback):
        java = home / "bin/java"
        java.parent.mkdir(parents=True)
        java.write_bytes(b"java")
    candidate = JdtCandidate(
        candidate_id="product",
        root=tmp_path / "candidate",
        launcher=tmp_path / "launcher.jar",
        worker_java_sha256=None,
        lock={},
        worker_java_minimum=8,
        worker_class_major=52,
    )
    observed = {
        build: WorkerJavaRuntime(build, build / "bin/java", 17, 64),
        target: WorkerJavaRuntime(target, target / "bin/java", 8, 64),
        fallback: WorkerJavaRuntime(fallback, fallback / "bin/java", 8, 64),
    }
    monkeypatch.setattr(
        JdtCandidate,
        "verify_worker_java",
        lambda self, home: observed[home],
    )
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setenv("JAVA_HOME", str(fallback))

    selected = candidate.select_worker_java((build, target))

    assert selected.home == build
    assert selected.major == 17


def test_lombok_add_opens_depends_on_worker_java_major() -> None:
    assert lombok_worker_jvm_arguments(8, lombok_enabled=True) == ()
    assert lombok_worker_jvm_arguments(11, lombok_enabled=True) == (
        "--add-opens=java.base/java.lang=ALL-UNNAMED",
    )
    assert lombok_worker_jvm_arguments(17, lombok_enabled=False) == ()


def test_target_java8_platform_is_selected_separately_from_runtime_jdk(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime-11"
    runtime.joinpath("lib").mkdir(parents=True)
    runtime.joinpath("lib/jrt-fs.jar").write_bytes(b"jrt")
    runtime.joinpath("release").write_text('JAVA_VERSION="11"\n')
    target = tmp_path / ".jdks/target-8"
    target.joinpath("jre/lib").mkdir(parents=True)
    target.joinpath("jre/lib/rt.jar").write_bytes(b"rt")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("JAVA_HOME", raising=False)

    assert select_target_system_home((runtime,), 8) == target


def _session(tmp_path: Path, monkeypatch) -> tuple[
    PersistentJdtCompileSession, Path, _FakeWorker
]:
    source_root = tmp_path / "project/src/main/java"
    source = source_root / "example/App.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        "package example; class App { int value() { return 1; } }",
        encoding="utf-8",
    )
    session = PersistentJdtCompileSession(
        root=tmp_path / "session",
        candidate=_candidate(tmp_path),
        worker_java_home=tmp_path / "jdk",
        source_roots=(source_root,),
        classpath_entries=(tmp_path / "dependency.jar",),
        source_encoding="UTF-8",
    )
    worker = _FakeWorker(session)
    monkeypatch.setattr(
        session,
        "_start_worker",
        lambda **_kwargs: worker,
    )
    return session, source, worker


def test_persistent_jdt_session_full_then_incremental(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dependency = tmp_path / "dependency.jar"
    dependency.write_bytes(b"dependency")
    session, source, worker = _session(tmp_path, monkeypatch)
    assert session.min_heap_mb == 64
    assert session.max_heap_mb == 2048

    full = session.start()
    session.accept_baseline()
    source.write_text(
        "package example; class App { int value() { return 2; } }",
        encoding="utf-8",
    )
    incremental = session.compile((source,))

    assert full.compile_ok is True
    assert full.actual_build_kind == "FULL"
    assert incremental.compile_ok is True
    assert incremental.actual_build_kind == "INCREMENTAL"
    assert incremental.compiled_source_count == 1
    assert incremental.changed_classes == ("example/App.class",)
    assert incremental.source_changes_pending is False
    assert session.close() is True
    assert worker.closed is True


def test_local_workspace_reopens_then_compiles_detected_source_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    source_root = project / "src/main/java"
    source = source_root / "example/App.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        "package example; class App { int value() { return 1; } }",
        encoding="utf-8",
    )
    dependency = tmp_path / "dependency.jar"
    dependency.write_bytes(b"dependency")
    store = JdtWorkspaceStore(tmp_path / "cache")
    identity = {"build_world_fingerprint": "world", "candidate": "test"}

    first_lease = store.claim(
        project_root=project,
        module_root=project,
        identity=identity,
    )
    assert first_lease.reusable is False
    first = PersistentJdtCompileSession(
        root=first_lease.root,
        candidate=_candidate(tmp_path),
        worker_java_home=tmp_path / "jdk",
        source_roots=(source_root,),
        classpath_entries=(dependency,),
        source_encoding="UTF-8",
        preserve_root_on_close=True,
    )
    first_worker = _FakeWorker(first)
    monkeypatch.setattr(first, "_start_worker", lambda **_kwargs: first_worker)
    assert first.start().actual_build_kind == "FULL"
    first.accept_baseline()
    first_lease.mark_initialized()
    first_lease.checkpoint()
    assert json.loads(
        first_lease.state_file.read_text(encoding="utf-8")
    )["clean_shutdown"] is True
    first_lease.mark_dirty()
    assert json.loads(
        first_lease.state_file.read_text(encoding="utf-8")
    )["clean_shutdown"] is False
    first_lease.checkpoint()
    assert first.close() is True
    first_lease.release(clean=first.last_close_clean)

    source.write_text(
        "package example; class App { int value() { return 2; } }",
        encoding="utf-8",
    )
    second_lease = store.claim(
        project_root=project,
        module_root=project,
        identity=identity,
    )
    assert second_lease.reusable is True
    second = PersistentJdtCompileSession(
        root=second_lease.root,
        candidate=_candidate(tmp_path),
        worker_java_home=tmp_path / "jdk",
        source_roots=(source_root,),
        classpath_entries=(dependency,),
        source_encoding="UTF-8",
        preserve_root_on_close=True,
    )
    second_worker = _FakeWorker(second)

    def reopen(**_kwargs):
        second._worker_ready_frame = {
            "workspace_project_state": "reopened"
        }
        return second_worker

    monkeypatch.setattr(second, "_start_worker", reopen)
    restored = second.start(
        reuse_workspace=True,
        build_on_reuse=False,
    )
    assert restored.actual_build_kind is None
    second.accept_baseline()
    assert second.workspace_source_changes() == (source.resolve(),)
    incremental = second.compile(second.workspace_source_changes())
    assert incremental.actual_build_kind == "INCREMENTAL"
    assert incremental.compiled_source_units == ("src/example/App.java",)
    second_lease.mark_initialized()
    assert second.close() is True
    second_lease.release(clean=second.last_close_clean)

    state = json.loads(
        second_lease.state_file.read_text(encoding="utf-8")
    )
    assert state["clean_shutdown"] is True


def test_saved_workspace_is_reused_without_unclean_shutdown_audit(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = JdtWorkspaceStore(tmp_path / "cache")
    identity = {"build_world_fingerprint": "world"}
    first = store.claim(
        project_root=project,
        module_root=project,
        identity=identity,
    )
    first.root.joinpath("workspace/plain-fixture").mkdir(parents=True)
    first.mark_initialized()
    first.release(clean=False)

    second = store.claim(
        project_root=project,
        module_root=project,
        identity=identity,
    )

    assert second.reusable is True
    assert second.root.exists() is True


def test_persistent_jdt_session_supports_test_only_source_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    test_root = tmp_path / "project/test"
    source = test_root / "example/AppTest.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        "package example; class AppTest { int value() { return 1; } }",
        encoding="utf-8",
    )
    dependency = tmp_path / "dependency.jar"
    dependency.write_bytes(b"dependency")
    session = PersistentJdtCompileSession(
        root=tmp_path / "session",
        candidate=_candidate(tmp_path),
        worker_java_home=tmp_path / "jdk",
        source_roots=(),
        classpath_entries=(dependency,),
        source_encoding="UTF-8",
        test_source_roots=(test_root,),
    )

    class TestOnlyWorker(_FakeWorker):
        def command(self, command: str):
            self.command_count += 1
            private = self.session.private_test_source / "example/AppTest.java"
            output = self.session.test_output_directory / "example/AppTest.class"
            output.parent.mkdir(parents=True, exist_ok=True)
            before = output.read_bytes() if output.exists() else None
            output.write_bytes(hashlib.sha256(private.read_bytes()).digest())
            after = output.read_bytes()
            return {
                "operation_ok": True,
                "compile_ok": True,
                "actual_build_kind": (
                    "FULL" if command.endswith("FULL") else "INCREMENTAL"
                ),
                "compiled_source_units": ["test-src/example/AppTest.java"],
                "changed_classes": (
                    ["example/AppTest.class"] if before != after else []
                ),
                "deleted_source_units": [],
                "deleted_classes": [],
                "error_count": 0,
                "warning_count": 0,
                "diagnostic_details": [],
                "diagnostics_truncated": False,
                "main_compile_ok": True,
                "test_compile_ok": True,
            }

    worker = TestOnlyWorker(session)
    monkeypatch.setattr(session, "_start_worker", lambda **_kwargs: worker)

    full = session.start()
    session.accept_baseline()
    source.write_text(
        "package example; class AppTest { int value() { return 2; } }",
        encoding="utf-8",
    )
    incremental = session.compile((source,))

    assert full.compile_ok is True
    assert full.compiled_source_units == ("test-src/example/AppTest.java",)
    assert incremental.compile_ok is True
    assert incremental.compiled_source_units == (
        "test-src/example/AppTest.java",
    )
    assert session.close() is True


def test_compile_ready_is_false_until_initial_full_build_completes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dependency = tmp_path / "dependency.jar"
    dependency.write_bytes(b"dependency")
    session, _source, worker = _session(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    def block_full() -> None:
        entered.set()
        assert release.wait(5)

    worker.on_build = block_full
    result: list[object] = []
    thread = threading.Thread(target=lambda: result.append(session.start()))
    thread.start()
    assert entered.wait(5)

    assert session.ready is False

    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert result
    assert session.ready is False
    session.accept_baseline()
    assert session.ready is True


def test_initial_full_uses_frozen_launch_sources_then_incremental_reads_live_edit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "project/src/main/java"
    source = source_root / "example/App.java"
    source.parent.mkdir(parents=True)
    baseline_text = "package example; class App { int value() { return 1; } }"
    edited_text = "package example; class App { int value() { return 2; } }"
    source.write_text(baseline_text, encoding="utf-8")
    snapshot_root = tmp_path / "launch-snapshot"
    snapshot = snapshot_root / "example/App.java"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(baseline_text, encoding="utf-8")
    source.write_text(edited_text, encoding="utf-8")
    dependency = tmp_path / "dependency.jar"
    dependency.write_bytes(b"dependency")
    session = PersistentJdtCompileSession(
        root=tmp_path / "session",
        candidate=_candidate(tmp_path),
        worker_java_home=tmp_path / "jdk",
        source_roots=(source_root,),
        baseline_source_roots=(snapshot_root,),
        classpath_entries=(dependency,),
        source_encoding="UTF-8",
    )
    worker = _FakeWorker(session)
    monkeypatch.setattr(session, "_start_worker", lambda **_kwargs: worker)

    full = session.start()
    session.accept_baseline()
    full_bytes = (session.output_directory / "example/App.class").read_bytes()
    incremental = session.compile((source,))
    incremental_bytes = (
        session.output_directory / "example/App.class"
    ).read_bytes()

    assert full.compile_ok is True
    assert full_bytes == hashlib.sha256(baseline_text.encode()).digest()
    assert incremental.compile_ok is True
    assert incremental.changed_classes == ("example/App.class",)
    assert incremental_bytes == hashlib.sha256(edited_text.encode()).digest()


def test_publication_delta_accumulates_until_runtime_apply_is_confirmed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dependency = tmp_path / "dependency.jar"
    dependency.write_bytes(b"dependency")
    session, source, _worker = _session(tmp_path, monkeypatch)
    session.start()
    session.accept_baseline()

    source.write_text(
        "package example; class App { int value() { return 2; } }",
        encoding="utf-8",
    )
    first = session.compile((source,))
    source.write_text(
        "package example; class App { int value() { return 3; } }",
        encoding="utf-8",
    )
    second = session.compile((source,))

    assert first.runtime_changed_classes == ("example/App.class",)
    assert second.runtime_changed_classes == ("example/App.class",)

    session.mark_published()
    third = session.compile((source,))
    assert third.changed_classes == ()
    assert third.runtime_changed_classes == ()


def test_accept_and_apply_baselines_do_not_hash_output_files(tmp_path, monkeypatch):
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, _source, _worker = _session(tmp_path, monkeypatch)
    session.start()
    monkeypatch.setattr(
        "jolink_runtime.launch.jdt_compile_session._sha256_file",
        lambda _path: (_ for _ in ()).throw(AssertionError("unexpected file hash")),
    )
    session.accept_baseline()
    session.mark_published()
    session.reset_publication_baseline(tmp_path / "unused-output")


def test_unchanged_source_does_not_call_worker_build(tmp_path, monkeypatch):
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, source, worker = _session(tmp_path, monkeypatch)
    session.start()
    session.accept_baseline()
    worker.command = lambda _command: (_ for _ in ()).throw(
        AssertionError("unexpected BUILD for unchanged source")
    )
    result = session.compile((source,))
    assert result.compile_ok
    assert result.compiled_source_count == 0
    assert result.runtime_changed_classes == ()


def test_saved_source_index_remembers_actual_compile_failure(tmp_path, monkeypatch):
    session, source, worker = _session(tmp_path, monkeypatch)
    session.start()
    session.accept_baseline()
    source.write_text("package example; this does not compile", encoding="utf-8")
    worker.fail_compile = True
    worker.diagnostics = [{"message": "Syntax error", "severity_name": "error"}]
    assert session.compile((source,)).compile_ok is False
    session.save_source_index()

    # Simulate loading the persisted facts in a new compiler instance.
    session._working_compile_state = "unknown"
    session._last_compile_error_count = 0
    session._last_compile_diagnostics = ()
    session._restore_persisted_source_map()
    assert session.working_compile_state == "failed"
    assert session.last_compile_error_count == 1
    assert session.last_compile_diagnostics[0]["message"] == "Syntax error"
    before = worker.command_count
    assert session.compile((source,)).compile_ok is False
    assert worker.command_count == before


def test_compile_failure_keeps_mutable_output_non_publishable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, source, worker = _session(tmp_path, monkeypatch)
    session.start()
    session.accept_baseline()
    baseline = (
        session.output_directory / "example/App.class"
    ).read_bytes()
    source.write_text("broken source", encoding="utf-8")
    worker.fail_compile = True
    worker.diagnostics = [
        {
            "resource": "src/example/App.java",
            "line": 1,
            "severity_name": "ERROR",
            "message": "syntax error",
        }
    ]

    failed = session.compile((source,))

    assert failed.compile_ok is False
    assert failed.error_count == 1
    assert session.working_compile_state == "failed"
    assert session.last_compile_error_count == 1
    assert session.last_compile_diagnostics[0]["message"] == "syntax error"
    assert failed.output_directory.joinpath(
        "example/App.class"
    ).read_bytes() == baseline
    worker.fail_compile = False
    worker.diagnostics = []
    source.write_text(
        "package example; class App { int value() { return 2; } }",
        encoding="utf-8",
    )
    recovered = session.compile((source,))
    assert recovered.compile_ok is True
    assert session.working_compile_state == "valid"
    assert session.last_compile_error_count == 0
    assert recovered.runtime_changed_classes == ("example/App.class",)


def test_source_edit_during_compile_is_detected_on_next_source_scan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, source, worker = _session(tmp_path, monkeypatch)
    session.start()
    session.accept_baseline()
    source.write_text(
        "package example; class App { int value() { return 2; } }",
        encoding="utf-8",
    )
    worker.on_build = lambda: source.write_text(
        "package example; class App { int value() { return 3; } }",
        encoding="utf-8",
    )

    result = session.compile((source,))

    assert result.compile_ok is True
    assert result.source_changes_pending is False
    assert session.workspace_source_changes() == (source.resolve(),)






def test_workspace_source_changes_tracks_unpublished_edits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, source, _worker = _session(tmp_path, monkeypatch)
    session.start()
    session.accept_baseline()
    assert session.workspace_source_changes() == ()

    source.write_text(
        "package example; class App { int value() { return 2; } }",
        encoding="utf-8",
    )
    assert session.workspace_source_changes() == (source.resolve(),)

    session.compile((source,))
    assert session.workspace_source_changes() == ()


def test_source_addition_and_deletion_update_private_mirror_and_outputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, source, worker = _session(tmp_path, monkeypatch)
    session.start()
    session.accept_baseline()
    added = source.with_name("Added.java")
    added.write_text(
        "package example; class Added { int value() { return 7; } }",
        encoding="utf-8",
    )
    original_command = worker.command

    def lifecycle_command(command: str):
        if command.endswith("FULL"):
            return original_command(command)
        private = session.private_source / "example/Added.java"
        output = session.output_directory / "example/Added.class"
        if private.is_file():
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(hashlib.sha256(private.read_bytes()).digest())
            return {
                "operation_ok": True,
                "compile_ok": True,
                "actual_build_kind": "INCREMENTAL",
                "compiled_source_units": ["src/example/Added.java"],
                "changed_classes": ["example/Added.class"],
                "deleted_classes": [],
                "error_count": 0,
                "warning_count": 0,
                "diagnostic_details": [],
                "diagnostics_truncated": False,
            }
        output.unlink(missing_ok=True)
        return {
            "operation_ok": True,
            "compile_ok": True,
            "actual_build_kind": "INCREMENTAL",
            "compiled_source_units": [],
            "deleted_source_units": ["src/example/Added.java"],
            "changed_classes": [],
            "deleted_classes": ["example/Added.class"],
            "error_count": 0,
            "warning_count": 0,
            "diagnostic_details": [],
            "diagnostics_truncated": False,
        }

    worker.command = lifecycle_command

    added_result = session.compile((added,))
    assert added_result.changed_classes == ("example/Added.class",)
    assert session.workspace_source_changes() == ()

    added.unlink()
    deleted_result = session.compile((added,))
    assert deleted_result.deleted_classes == ("example/Added.class",)
    assert deleted_result.deleted_source_units == (
        "src/example/Added.java",
    )
    assert session.workspace_source_changes() == ()




def test_interrupt_poison_closes_worker_without_waiting_for_operation_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, _source, worker = _session(tmp_path, monkeypatch)
    session.start()
    session.accept_baseline()

    session.interrupt("TEST_CANCELLED")

    assert session.ready is False
    assert worker.closed is True


def test_source_outside_frozen_build_world_is_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, source, _worker = _session(tmp_path, monkeypatch)
    session.start()
    session.accept_baseline()
    private = session.private_source / "example/App.java"
    private_before = private.read_bytes()
    source.write_text(
        "package example; class App { int value() { return 2; } }",
        encoding="utf-8",
    )
    outside = tmp_path / "Outside.java"
    outside.write_text("class Outside {}", encoding="utf-8")

    with pytest.raises(JdtCompileError) as captured:
        session.compile((source, outside))
    assert captured.value.error_code == "SOURCE_OUTSIDE_BUILD_WORLD"
    assert private.read_bytes() == private_before


def test_timeout_poisons_session_and_rejects_later_build(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, source, worker = _session(tmp_path, monkeypatch)
    session.start()
    session.accept_baseline()
    worker.command_error = JdtCompileError(
        "JDT_WORKER_TIMEOUT", "late result"
    )
    source.write_text("package example; class App { int value() { return 2; } }")

    with pytest.raises(JdtCompileError) as timed_out:
        session.compile((source,))
    assert timed_out.value.error_code == "JDT_WORKER_TIMEOUT"
    calls_after_timeout = worker.command_count

    worker.command_error = None
    with pytest.raises(JdtCompileError) as poisoned:
        session.compile((source,))
    assert poisoned.value.error_code == "JDT_SESSION_POISONED"
    assert worker.command_count == calls_after_timeout
    assert worker.closed is True
    assert session.ready is False


def test_start_aborted_closes_worker_and_removes_failed_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, _source, worker = _session(tmp_path, monkeypatch)
    original = worker.command

    def aborted(command: str):
        frame = original(command)
        frame["operation_ok"] = False
        return frame

    worker.command = aborted

    with pytest.raises(JdtCompileError) as captured:
        session.start()
    assert captured.value.error_code == "JDT_BUILD_ABORTED"
    assert session.ready is False
    assert worker.closed is True
    assert not session.root.exists()


def test_compile_reports_resource_delta_and_bounded_diagnostics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, source, worker = _session(tmp_path, monkeypatch)
    worker.resource_value = b"one"
    session.start()
    session.accept_baseline()
    source.write_text(
        "package example; class App { int value() { return 2; } }",
        encoding="utf-8",
    )
    worker.resource_value = b"two"
    worker.diagnostics = [
        {
            "resource": "example/App.java",
            "line": 1,
            "severity_name": "WARNING",
            "message": "bounded warning",
            "private": "not returned",
        }
    ]
    worker.diagnostics_truncated = True

    result = session.compile((source,))

    assert result.changed_resources == ("META-INF/generated.json",)
    assert result.deleted_resources == ()
    assert result.diagnostics == (
        {
            "resource": "example/App.java",
            "line": 1,
            "severity_name": "WARNING",
            "message": "bounded warning",
        },
    )
    assert result.diagnostics_truncated is True


def test_full_captures_native_resources_before_formal_overlay(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, _source, worker = _session(tmp_path, monkeypatch)
    formal = tmp_path / "formal-output/META-INF/generated.json"
    formal.parent.mkdir(parents=True)
    formal.write_bytes(b"formal")
    session.baseline_main_output = formal.parents[1]
    worker.resource_value = b"native"

    session.start()

    native_hash = hashlib.sha256(b"native").hexdigest()
    assert session.native_full_resource_manifest == {
        "main/META-INF/generated.json": native_hash
    }
    assert (
        session.output_directory / "META-INF/generated.json"
    ).read_bytes() == b"formal"


def test_incremental_restores_frozen_formal_resources_before_delta(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, source, worker = _session(tmp_path, monkeypatch)
    formal = tmp_path / "formal-output/message.txt"
    formal.parent.mkdir(parents=True)
    formal.write_bytes(b"formal-resource")
    session.baseline_main_output = formal.parent
    session.start()
    session.accept_baseline()
    source.write_text(
        "package example; class App { int value() { return 2; } }",
        encoding="utf-8",
    )
    worker.delete_resource = True

    result = session.compile((source,))

    assert result.runtime_changed_resources == ()
    assert result.runtime_deleted_resources == ()
    assert (session.output_directory / "message.txt").read_bytes() == (
        b"formal-resource"
    )
    formal.unlink()
    session._restore_frozen_resources()
    assert not (session.output_directory / "message.txt").exists()


def test_close_force_cancels_inflight_compile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, source, worker = _session(tmp_path, monkeypatch)
    session.start()
    session.accept_baseline()
    entered = threading.Event()
    released = threading.Event()

    def blocked(_command: str):
        entered.set()
        released.wait(5)
        raise JdtCompileError("JDT_WORKER_EXITED", "closed")

    worker.command = blocked
    source.write_text("package example; class App { int value() { return 2; } }")
    worker.force_close = lambda: (released.set() or worker.close())
    errors: list[str] = []

    def compile_in_thread() -> None:
        try:
            session.compile((source,))
        except JdtCompileError as error:
            errors.append(error.error_code)

    thread = threading.Thread(target=compile_in_thread)
    thread.start()
    assert entered.wait(1)
    started = time.monotonic()
    assert session.close() is True
    thread.join(1)

    assert time.monotonic() - started < 1
    assert errors == ["JDT_WORKER_EXITED"]
    assert not thread.is_alive()


def test_candidate_lock_verifies_every_artifact(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    root = cache / "candidates/test"
    plugins = root / "plugins"
    plugins.mkdir(parents=True)
    configuration = root / "configuration"
    configuration.mkdir()
    plugin = plugins / "worker.jar"
    launcher = plugins / "launcher.jar"
    with zipfile.ZipFile(plugin, "w") as archive:
        archive.writestr(
            "Worker.class",
            b"\xca\xfe\xba\xbe\x00\x00\x00=",
        )
    worker_bytes = plugin.read_bytes()
    launcher.write_bytes(b"launcher")
    config = configuration / "config.ini"
    config.write_bytes(b"config")
    lock = {
        "candidate_id": "test",
        "artifacts": [
            {
                "filename": "launcher.jar",
                "sha256": hashlib.sha256(b"launcher").hexdigest(),
            }
        ],
        "worker_artifact": {
            "filename": "worker.jar",
            "sha256": hashlib.sha256(worker_bytes).hexdigest(),
        },
        "worker_build": {
            "java_home_identity": {"java_binary_sha256": "java-sha"}
        },
        "equinox": {
            "launcher_filename": "launcher.jar",
            "configuration_sha256": hashlib.sha256(b"config").hexdigest(),
        },
    }
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    candidate = JdtCandidate.load(lock_path, cache)
    assert candidate.candidate_id == "test"

    plugin.write_bytes(b"changed")
    with pytest.raises(JdtCompileError) as captured:
        JdtCandidate.load(lock_path, cache)
    assert captured.value.error_code == "JDT_CANDIDATE_INTEGRITY_MISMATCH"
    assert captured.value.context == {"artifact": "worker.jar"}


def test_product_candidate_installs_bundles_worker_and_config_atomically(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module_root = Path(__file__).resolve().parents[2] / (
        "src/jolink_runtime/launch"
    )
    worker = base64.b64decode(
        "".join(
            module_root.joinpath(
                "jdt-product-worker.jar.b64"
            ).read_text(encoding="ascii").split()
        ),
        validate=True,
    )
    config = module_root.joinpath("jdt-product-config.ini").read_bytes()
    bundle = b"official-bundle"
    lock = {
        "schema_version": 1,
        "candidate_id": "product-test",
        "worker_java_minimum": 17,
        "worker_class_major": 52,
        "repository_url": "https://example.invalid/eclipse",
        "artifacts": [
            {
                "filename": "launcher.jar",
                "sha256": hashlib.sha256(bundle).hexdigest(),
            }
        ],
        "worker_artifact": {
            "filename": "net.jolink.runtime.jdt.worker_0.1.0.jar",
            "sha256": hashlib.sha256(worker).hexdigest(),
        },
        "equinox": {
            "launcher_filename": "launcher.jar",
            "configuration_sha256": hashlib.sha256(config).hexdigest(),
        },
    }
    downloads: list[str] = []

    def download(url: str, destination: Path, *, artifact: str) -> None:
        downloads.append(url)
        assert artifact == "launcher.jar"
        destination.write_bytes(bundle)

    monkeypatch.setattr(
        JdtCandidate,
        "_download_product_artifact",
        staticmethod(download),
    )
    original_read_bytes = Path.read_bytes

    def crlf_product_config(path: Path) -> bytes:
        value = original_read_bytes(path)
        if path == module_root / "jdt-product-config.ini":
            return value.replace(b"\n", b"\r\n")
        return value

    monkeypatch.setattr(Path, "read_bytes", crlf_product_config)
    product_root = tmp_path / "content-addressed/product-test/lock-sha"
    original_rename = Path.rename
    raced = False

    def publish_from_other_process(source: Path, target: Path) -> Path:
        nonlocal raced
        if target == product_root and not raced:
            raced = True
            shutil.copytree(source, target)
            raise FileExistsError("concurrent product candidate publication")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", publish_from_other_process)

    JdtCandidate._install_product_candidate(
        lock,
        product_root=product_root,
        legacy_roots=(),
    )
    candidate = JdtCandidate._load_root(lock, product_root)

    assert candidate.root == product_root
    assert raced is True
    assert downloads == [
        "https://example.invalid/eclipse/plugins/launcher.jar"
    ]
    assert product_root.joinpath(
        "plugins/net.jolink.runtime.jdt.worker_0.1.0.jar"
    ).read_bytes() == worker
    assert product_root.joinpath(
        "configuration/config.ini"
    ).read_bytes() == config
