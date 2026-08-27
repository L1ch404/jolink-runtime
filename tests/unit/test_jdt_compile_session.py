from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate,
    JdtCompileError,
    PersistentJdtCompileSession,
)


class _FakeWorker:
    def __init__(self, session: PersistentJdtCompileSession) -> None:
        self.session = session
        self.closed = False
        self.fail_compile = False
        self.on_build = None

    def command(self, command: str):
        assert command in {"BUILD\tFULL", "BUILD\tINCREMENTAL"}
        if self.on_build is not None:
            self.on_build()
        source = self.session.private_source / "example/App.java"
        output = self.session.output_directory / "example/App.class"
        output.parent.mkdir(parents=True, exist_ok=True)
        before = output.read_bytes() if output.exists() else None
        if not self.fail_compile:
            output.write_bytes(hashlib.sha256(source.read_bytes()).digest())
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
        }

    def close(self) -> bool:
        self.closed = True
        return True


def _candidate(tmp_path: Path) -> JdtCandidate:
    return JdtCandidate(
        candidate_id="test",
        root=tmp_path / "candidate",
        launcher=tmp_path / "candidate/launcher.jar",
        worker_java_sha256="unused",
        lock={},
    )


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
    session, _source, _worker = _session(tmp_path, monkeypatch)
    session.start()
    outside = tmp_path / "Outside.java"
    outside.write_text("class Outside {}", encoding="utf-8")

    with pytest.raises(JdtCompileError) as captured:
        session.compile((outside,))
    assert captured.value.error_code == "SOURCE_OUTSIDE_BUILD_WORLD"


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
