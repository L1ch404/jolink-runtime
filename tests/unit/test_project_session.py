from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jolink_runtime.launch.project_session import (
    GenerationStore,
    JavaProjectSession,
    ProjectSessionError,
    ReloadStage,
    RuntimeGenerationState,
)


def _output(root: Path, values: dict[str, bytes]) -> Path:
    root.mkdir(parents=True)
    for relative, content in values.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return root


def test_candidate_is_durable_but_not_current_until_promoted(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "store")
    initial_source = _output(
        tmp_path / "initial",
        {
            "example/App.class": b"v1",
            "application.yml": b"name: first",
        },
    )
    current = store.initialize(
        initial_source,
        build_world_fingerprint="world-1",
        runtime_active=True,
    )
    candidate_source = _output(
        tmp_path / "candidate",
        {
            "example/App.class": b"v2",
            "application.yml": b"name: second",
        },
    )

    candidate = store.prepare_candidate(
        candidate_source,
        build_world_fingerprint="world-1",
    )

    assert store.current == current
    assert store.runtime == current
    assert store.previous is None
    assert store.candidate == candidate
    assert candidate.output_directory.joinpath(
        "example/App.class"
    ).read_bytes() == b"v2"
    candidate_source.joinpath("example/App.class").write_bytes(b"v3")
    assert candidate.output_directory.joinpath(
        "example/App.class"
    ).read_bytes() == b"v2"

    promoted = store.promote_candidate()

    assert promoted == candidate
    assert store.current == candidate
    assert store.runtime == candidate
    assert store.previous == current
    assert store.candidate is None


def test_discarded_candidate_never_changes_current_or_runtime(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "store")
    current = store.initialize(
        _output(tmp_path / "initial", {"App.class": b"good"}),
        build_world_fingerprint="world",
        runtime_active=True,
    )
    candidate = store.prepare_candidate(
        _output(tmp_path / "candidate", {"App.class": b"bad"}),
        build_world_fingerprint="world",
    )
    candidate_root = candidate.output_directory.parent

    store.discard_candidate()

    assert store.current == current
    assert store.runtime == current
    assert store.candidate is None
    assert not candidate_root.exists()


def test_runtime_state_can_be_absent_or_unknown_without_guessing(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "store")
    current = store.initialize(
        _output(tmp_path / "initial", {"App.class": b"good"}),
        build_world_fingerprint="world",
        runtime_active=True,
    )

    store.mark_runtime_unknown()
    assert store.runtime is None
    assert store.runtime_state is RuntimeGenerationState.UNKNOWN
    assert store.current == current

    store.mark_runtime_absent()
    assert store.runtime_state is RuntimeGenerationState.ABSENT

    assert store.mark_runtime_current() == current
    assert store.runtime_state is RuntimeGenerationState.KNOWN


def test_generation_copy_rejects_links_and_empty_outputs(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "store")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ProjectSessionError) as empty_error:
        store.initialize(
            empty,
            build_world_fingerprint="world",
            runtime_active=False,
        )
    assert empty_error.value.error_code == "GENERATION_OUTPUT_EMPTY"

    source = _output(tmp_path / "source", {"App.class": b"good"})
    link = source / "linked.class"
    try:
        link.symlink_to(source / "App.class")
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(ProjectSessionError) as link_error:
        store.initialize(
            source,
            build_world_fingerprint="world",
            runtime_active=False,
        )
    assert link_error.value.error_code == (
        "GENERATION_OUTPUT_LINK_UNSUPPORTED"
    )


def test_store_persists_only_generation_ids_and_state(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "store")
    current = store.initialize(
        _output(tmp_path / "initial", {"App.class": b"good"}),
        build_world_fingerprint="world",
        runtime_active=True,
    )

    state = json.loads(
        (tmp_path / "store/state.json").read_text(encoding="utf-8")
    )

    assert state["current_generation_id"] == current.generation_id
    assert state["runtime_generation_id"] == current.generation_id
    assert state["runtime_classpath_generation_id"] == current.generation_id
    assert state["candidate_generation_id"] is None
    assert state["runtime_state"] == "known"


def test_project_session_serializes_reload_and_reports_source_drift(
    tmp_path: Path,
) -> None:
    session = JavaProjectSession(
        root=tmp_path / "session",
        build_world_fingerprint="world",
    )
    attempt = session.begin_reload("source-v1")
    attempt.compile_ms = 120.0
    attempt.compiled_sources = 1
    session.transition_reload(ReloadStage.COMPILING)

    with pytest.raises(ProjectSessionError) as concurrent:
        session.begin_reload("source-v2")
    assert concurrent.value.error_code == "RELOAD_ALREADY_IN_PROGRESS"

    finished = session.finish_reload(
        applied=True,
        source_fingerprint_after="source-v2",
    )

    assert finished.source_changes_pending is True
    assert session.active_reload is None
    status = session.public_status()
    assert status["active_operation"] is None
    assert status["last_reload"] == {
        "applied": True,
        "apply_method": None,
        "compile_ms": 120.0,
        "startup_ms": None,
        "source_changes_pending": True,
        "rolled_back": False,
        "error_code": None,
    }


def test_nonterminal_reload_keeps_applied_unknown_and_active(
    tmp_path: Path,
) -> None:
    session = JavaProjectSession(
        root=tmp_path / "session",
        build_world_fingerprint="world",
    )
    session.begin_reload("source")
    session.transition_reload(ReloadStage.WAITING_READINESS)

    attempt = session.finish_reload(
        applied=None,
        source_fingerprint_after="source",
    )

    assert attempt.applied is None
    assert session.active_reload is attempt
    assert session.public_status()["active_operation"]["stage"] == (
        "waiting_readiness"
    )


def test_project_session_owns_one_compile_session(tmp_path: Path) -> None:
    session = JavaProjectSession(
        root=tmp_path / "session",
        build_world_fingerprint="world",
    )
    closed: list[bool] = []
    compiler = SimpleNamespace(
        ready=True,
        close=lambda: closed.append(True),
    )

    session.attach_compile_session(compiler)

    assert session.compile_ready is True
    with pytest.raises(ProjectSessionError) as duplicate:
        session.attach_compile_session(compiler)
    assert duplicate.value.error_code == "COMPILE_SESSION_ALREADY_ATTACHED"

    session.close()
    assert closed == [True]
    assert session.compile_ready is False


def test_compile_failure_creates_no_candidate_generation(
    tmp_path: Path,
) -> None:
    session = JavaProjectSession(
        root=tmp_path / "session",
        build_world_fingerprint="world",
    )
    current = session.generations.initialize(
        _output(tmp_path / "initial", {"App.class": b"last-good"}),
        build_world_fingerprint="world",
        runtime_active=True,
    )
    session.begin_reload("bad-source")
    session.transition_reload(ReloadStage.COMPILING)

    session.finish_reload(
        applied=False,
        source_fingerprint_after="bad-source",
        error_code="COMPILE_FAILED",
    )

    assert session.generations.candidate is None
    assert session.generations.current == current
    assert session.generations.runtime == current


def test_restart_semantics_can_read_current_without_candidate(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "store")
    current = store.initialize(
        _output(tmp_path / "initial", {"App.class": b"stable"}),
        build_world_fingerprint="world",
        runtime_active=True,
    )
    store.prepare_candidate(
        _output(tmp_path / "candidate", {"App.class": b"unapplied"}),
        build_world_fingerprint="world",
    )

    assert store.current == current
    assert store.current.output_directory.joinpath("App.class").read_bytes() == (
        b"stable"
    )


def test_store_keeps_only_current_and_one_previous_after_promotions(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "store")
    first = store.initialize(
        _output(tmp_path / "one", {"App.class": b"one"}),
        build_world_fingerprint="world",
        runtime_active=True,
    )
    store.prepare_candidate(
        _output(tmp_path / "two", {"App.class": b"two"}),
        build_world_fingerprint="world",
    )
    second = store.promote_candidate()
    first_root = first.output_directory.parent
    assert first_root.is_dir()

    store.prepare_candidate(
        _output(tmp_path / "three", {"App.class": b"three"}),
        build_world_fingerprint="world",
    )
    third = store.promote_candidate()

    assert store.current == third
    assert store.previous == second
    assert store.runtime_classpath == first
    assert first_root.exists()

    store.mark_runtime_current()

    assert store.runtime_classpath == third
    assert not first_root.exists()


def test_generation_integrity_is_checked_before_promotion_and_restart(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "store")
    store.initialize(
        _output(tmp_path / "initial", {"App.class": b"one"}),
        build_world_fingerprint="world",
        runtime_active=True,
    )
    candidate = store.prepare_candidate(
        _output(tmp_path / "candidate", {"App.class": b"two"}),
        build_world_fingerprint="world",
    )
    candidate.output_directory.joinpath("App.class").write_bytes(b"tampered")

    with pytest.raises(ProjectSessionError) as promoted:
        store.promote_candidate()
    assert promoted.value.error_code == "GENERATION_INTEGRITY_MISMATCH"
    assert store.candidate == candidate

    store.discard_candidate()
    current = store.current
    assert current is not None
    current.output_directory.joinpath("App.class").write_bytes(b"tampered")
    with pytest.raises(ProjectSessionError) as restarted:
        store.mark_runtime_current()
    assert restarted.value.error_code == "GENERATION_INTEGRITY_MISMATCH"


def test_prepare_candidate_persist_failure_rolls_back_memory_and_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GenerationStore(tmp_path / "store")
    current = store.initialize(
        _output(tmp_path / "initial", {"App.class": b"one"}),
        build_world_fingerprint="world",
        runtime_active=True,
    )
    monkeypatch.setattr(
        store,
        "_persist_state",
        lambda: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError):
        store.prepare_candidate(
            _output(tmp_path / "candidate", {"App.class": b"two"}),
            build_world_fingerprint="world",
        )

    assert store.current == current
    assert store.candidate is None
    assert sorted(path.name for path in (store.root / "generations").iterdir()) == [
        current.generation_id
    ]


def test_promote_and_discard_persist_failures_restore_previous_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GenerationStore(tmp_path / "store")
    current = store.initialize(
        _output(tmp_path / "initial", {"App.class": b"one"}),
        build_world_fingerprint="world",
        runtime_active=True,
    )
    candidate = store.prepare_candidate(
        _output(tmp_path / "candidate", {"App.class": b"two"}),
        build_world_fingerprint="world",
    )
    original_persist = store._persist_state
    monkeypatch.setattr(
        store,
        "_persist_state",
        lambda: (_ for _ in ()).throw(OSError("readonly")),
    )

    with pytest.raises(OSError):
        store.promote_candidate()
    assert store.current == current
    assert store.runtime == current
    assert store.candidate == candidate

    with pytest.raises(OSError):
        store.discard_candidate()
    assert store.candidate == candidate
    assert candidate.output_directory.is_dir()

    monkeypatch.setattr(store, "_persist_state", original_persist)
    store.discard_candidate()
    assert store.candidate is None


def test_project_session_close_removes_frozen_source_snapshots(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private-session"
    snapshot = root / "launch-source-snapshot/0/example/App.java"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("class App {}", encoding="utf-8")
    session = JavaProjectSession(
        root=root,
        build_world_fingerprint="world",
    )

    session.close()

    assert not root.exists()
