from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate,
    JdtCompileError,
    PersistentJdtCompileSession,
)


class _FakeWorker:
    def __init__(self, session: PersistentJdtCompileSession) -> None:
        self.session = session
        self.process = SimpleNamespace(poll=lambda: None)
        self.closed = False
        self.fail_compile = False
        self.on_build = None
        self.command_error: JdtCompileError | None = None
        self.command_count = 0
        self.resource_value: bytes | None = None
        self.diagnostics = []
        self.diagnostics_truncated = False

    def command(self, command: str):
        self.command_count += 1
        if self.command_error is not None:
            raise self.command_error
        assert command in {"BUILD\tFULL", "BUILD\tINCREMENTAL"}
        if self.on_build is not None:
            self.on_build()
        source = self.session.private_source / "example/App.java"
        output = self.session.output_directory / "example/App.class"
        output.parent.mkdir(parents=True, exist_ok=True)
        before = output.read_bytes() if output.exists() else None
        if not self.fail_compile:
            output.write_bytes(hashlib.sha256(source.read_bytes()).digest())
        if self.resource_value is not None:
            resource = self.session.output_directory / "META-INF/generated.json"
            resource.parent.mkdir(parents=True, exist_ok=True)
            resource.write_bytes(self.resource_value)
        after = output.read_bytes() if output.exists() else None
        return {
            "operation_ok": True,
            "compile_ok": not self.fail_compile,
            "actual_build_kind": (
                "FULL" if command.endswith("FULL") else "INCREMENTAL"
            ),
            "compiled_source_units": ["example/App.java"],
            "changed_classes": (
                ["example/App.class"] if before != after else []
            ),
            "deleted_classes": [],
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
            stderr='openjdk version "17.0.16" 2025-07-15\n',
            stdout="",
        ),
    )

    assert candidate.verify_worker_java(java_home) == java


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

    full = session.start()
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

    assert first.candidate_changed_classes == ("example/App.class",)
    assert second.candidate_changed_classes == ("example/App.class",)

    session.mark_published()
    third = session.compile((source,))
    assert third.changed_classes == ()
    assert third.candidate_changed_classes == ()


def test_compile_failure_keeps_mutable_output_non_publishable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, source, worker = _session(tmp_path, monkeypatch)
    session.start()
    baseline = (
        session.output_directory / "example/App.class"
    ).read_bytes()
    source.write_text("broken source", encoding="utf-8")
    worker.fail_compile = True

    failed = session.compile((source,))

    assert failed.compile_ok is False
    assert failed.error_count == 1
    assert failed.output_directory.joinpath(
        "example/App.class"
    ).read_bytes() == baseline
    worker.fail_compile = False
    source.write_text(
        "package example; class App { int value() { return 2; } }",
        encoding="utf-8",
    )
    recovered = session.compile((source,))
    assert recovered.compile_ok is True
    assert recovered.candidate_changed_classes == ("example/App.class",)


def test_source_drift_is_reported_after_incremental_build(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, source, worker = _session(tmp_path, monkeypatch)
    session.start()
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
    assert result.source_changes_pending is True


def test_source_outside_frozen_build_world_is_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, source, _worker = _session(tmp_path, monkeypatch)
    session.start()
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
    worker.command_error = JdtCompileError(
        "JDT_WORKER_TIMEOUT", "late result"
    )

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


def test_close_force_cancels_inflight_compile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "dependency.jar").write_bytes(b"dependency")
    session, source, worker = _session(tmp_path, monkeypatch)
    session.start()
    entered = threading.Event()
    released = threading.Event()

    def blocked(_command: str):
        entered.set()
        released.wait(5)
        raise JdtCompileError("JDT_WORKER_EXITED", "closed")

    worker.command = blocked
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
    plugin.write_bytes(b"worker")
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
            "sha256": hashlib.sha256(b"worker").hexdigest(),
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
