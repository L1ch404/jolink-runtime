"""Durable last-good generations for one joLink-owned Java project.

The compiler workspace is mutable working state.  This module owns the
separate, immutable outputs that are allowed to start or represent a JVM.
It deliberately contains no Maven, JDT, JDWP, or subprocess logic.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable


class ProjectSessionError(RuntimeError):
    """A generation or reload invariant was rejected."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class ReloadStage(StrEnum):
    PREPARING = "preparing"
    COMPILING = "compiling"
    PREPARING_CANDIDATE = "preparing_candidate"
    APPLYING_HOTSWAP = "applying_hotswap"
    RESTARTING = "restarting"
    WAITING_READINESS = "waiting_readiness"
    ROLLING_BACK = "rolling_back"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RuntimeGenerationState(StrEnum):
    ABSENT = "absent"
    KNOWN = "known"
    UNKNOWN = "unknown"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _output_manifest(root: Path) -> tuple[tuple[str, str, int], ...]:
    if not root.is_dir():
        raise ProjectSessionError(
            "GENERATION_OUTPUT_UNAVAILABLE",
            "Generation source output is unavailable.",
        )
    items: list[tuple[str, str, int]] = []
    for child in sorted(root.rglob("*")):
        if child.is_symlink():
            raise ProjectSessionError(
                "GENERATION_OUTPUT_LINK_UNSUPPORTED",
                "Generation output may not contain symbolic links.",
            )
        if not child.is_file():
            continue
        relative = child.relative_to(root).as_posix()
        size = child.stat().st_size
        items.append((relative, _sha256_file(child), size))
    if not items:
        raise ProjectSessionError(
            "GENERATION_OUTPUT_EMPTY",
            "Generation output contains no files.",
        )
    return tuple(items)


def _manifest_fingerprint(
    manifest: tuple[tuple[str, str, int], ...],
) -> str:
    payload = json.dumps(
        manifest,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=True, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class BuildGeneration:
    generation_id: str
    ordinal: int
    parent_generation_id: str | None
    build_world_fingerprint: str
    output_directory: Path
    artifact_count: int
    output_fingerprint: str
    created_at: float

    def public_summary(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "artifact_count": self.artifact_count,
        }


@dataclass
class ReloadAttempt:
    attempt_id: str
    stage: ReloadStage
    started_at: float
    source_fingerprint_before: str
    source_files: tuple[Path, ...] = ()
    source_fingerprint_after: str | None = None
    compile_ms: float | None = None
    startup_ms: float | None = None
    compiled_sources: int | None = None
    apply_method: str | None = None
    applied: bool | None = None
    rolled_back: bool = False
    error_code: str | None = None
    finished_at: float | None = None
    cancel_requested: bool = False
    cancel_reason: str | None = None
    result_data: dict[str, Any] = field(default_factory=dict)

    @property
    def source_changes_pending(self) -> bool:
        return bool(
            self.source_fingerprint_after is not None
            and self.source_fingerprint_after
            != self.source_fingerprint_before
        )


class GenerationStore:
    """Own immutable outputs for one live joLink server session.

    Generations survive managed JVM restart and are independent from Maven or
    JDT working output.  They are intentionally deleted when this server-side
    project session closes; crash recovery across MCP server processes is not
    part of the first productization slice.
    """

    _STATE_SCHEMA = "jolink.generation-store.v1"

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=False)
        self._generations = self.root / "generations"
        self._staging = self.root / "staging"
        self._state_file = self.root / "state.json"
        self._lock = threading.RLock()
        self._items: dict[str, BuildGeneration] = {}
        self._current_id: str | None = None
        self._previous_id: str | None = None
        self._candidate_id: str | None = None
        self._runtime_id: str | None = None
        self._runtime_classpath_id: str | None = None
        self._runtime_state = RuntimeGenerationState.ABSENT
        self._ordinal = 0
        self._generations.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._staging.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def current(self) -> BuildGeneration | None:
        with self._lock:
            return self._item(self._current_id)

    @property
    def previous(self) -> BuildGeneration | None:
        with self._lock:
            return self._item(self._previous_id)

    @property
    def candidate(self) -> BuildGeneration | None:
        with self._lock:
            return self._item(self._candidate_id)

    @property
    def runtime(self) -> BuildGeneration | None:
        with self._lock:
            return self._item(self._runtime_id)

    @property
    def runtime_state(self) -> RuntimeGenerationState:
        with self._lock:
            return self._runtime_state

    @property
    def runtime_classpath(self) -> BuildGeneration | None:
        with self._lock:
            return self._item(self._runtime_classpath_id)

    def initialize(
        self,
        output_directory: Path,
        *,
        build_world_fingerprint: str,
        runtime_active: bool,
    ) -> BuildGeneration:
        with self._lock:
            if self._current_id is not None:
                raise ProjectSessionError(
                    "GENERATION_ALREADY_INITIALIZED",
                    "The project generation store is already initialized.",
                )
            old_ordinal = self._ordinal
            generation = self._materialize(
                output_directory,
                build_world_fingerprint=build_world_fingerprint,
                parent_generation_id=None,
            )
            self._current_id = generation.generation_id
            if runtime_active:
                self._runtime_id = generation.generation_id
                self._runtime_classpath_id = generation.generation_id
                self._runtime_state = RuntimeGenerationState.KNOWN
            try:
                self._persist_state()
            except Exception:
                self._current_id = None
                self._runtime_id = None
                self._runtime_classpath_id = None
                self._runtime_state = RuntimeGenerationState.ABSENT
                self._ordinal = old_ordinal
                self._remove_generation(generation)
                raise
            return generation

    def prepare_initial_candidate(
        self,
        output_directory: Path,
        *,
        build_world_fingerprint: str,
    ) -> BuildGeneration:
        """Seal the first launch output before any Runtime is trusted."""

        with self._lock:
            if self._current_id is not None or self._candidate_id is not None:
                raise ProjectSessionError(
                    "GENERATION_ALREADY_INITIALIZED",
                    "The project generation store is already initialized.",
                )
            old_ordinal = self._ordinal
            generation = self._materialize(
                output_directory,
                build_world_fingerprint=build_world_fingerprint,
                parent_generation_id=None,
            )
            self._candidate_id = generation.generation_id
            try:
                self._persist_state()
            except Exception:
                self._candidate_id = None
                self._ordinal = old_ordinal
                self._remove_generation(generation)
                raise
            return generation

    def prepare_candidate(
        self,
        output_directory: Path,
        *,
        build_world_fingerprint: str,
    ) -> BuildGeneration:
        """Seal a full candidate output without changing current/runtime."""

        with self._lock:
            if self._current_id is None:
                raise ProjectSessionError(
                    "GENERATION_NOT_INITIALIZED",
                    "No current generation exists.",
                )
            if self._candidate_id is not None:
                raise ProjectSessionError(
                    "CANDIDATE_ALREADY_EXISTS",
                    "A candidate generation is already pending.",
                )
            current = self._item(self._current_id)
            if current is None:
                raise ProjectSessionError(
                    "CURRENT_GENERATION_UNAVAILABLE",
                    "No current generation exists.",
                )
            self.verify_generation(current)
            old_ordinal = self._ordinal
            generation = self._materialize(
                output_directory,
                build_world_fingerprint=build_world_fingerprint,
                parent_generation_id=self._current_id,
            )
            self._candidate_id = generation.generation_id
            try:
                self._persist_state()
            except Exception:
                self._candidate_id = None
                self._ordinal = old_ordinal
                self._remove_generation(generation)
                raise
            return generation

    def promote_candidate(
        self,
        *,
        runtime_classpath_changed: bool = False,
    ) -> BuildGeneration:
        """Promote only after Runtime application success is known."""

        with self._lock:
            candidate = self._item(self._candidate_id)
            if candidate is None:
                raise ProjectSessionError(
                    "CANDIDATE_UNAVAILABLE",
                    "No candidate generation is ready to promote.",
                )
            self.verify_generation(candidate)
            old_state = self._state_tuple()
            obsolete_previous = self._previous_id
            old_current = self._current_id
            self._previous_id = old_current
            self._current_id = candidate.generation_id
            self._runtime_id = candidate.generation_id
            if runtime_classpath_changed:
                self._runtime_classpath_id = candidate.generation_id
            self._runtime_state = RuntimeGenerationState.KNOWN
            self._candidate_id = None
            try:
                self._persist_state()
            except Exception:
                self._restore_state_tuple(old_state)
                raise
            self._prune_unreferenced(
                preferred=(obsolete_previous,)
            )
            return candidate

    def discard_candidate(self) -> None:
        with self._lock:
            candidate = self._item(self._candidate_id)
            old_state = self._state_tuple()
            self._candidate_id = None
            try:
                self._persist_state()
            except Exception:
                self._restore_state_tuple(old_state)
                raise
            if candidate is not None:
                self._remove_generation(candidate)

    def mark_runtime_current(self) -> BuildGeneration:
        with self._lock:
            current = self._item(self._current_id)
            if current is None:
                raise ProjectSessionError(
                    "CURRENT_GENERATION_UNAVAILABLE",
                    "No current generation can be marked as running.",
                )
            self.verify_generation(current)
            old_state = self._state_tuple()
            self._runtime_id = current.generation_id
            self._runtime_classpath_id = current.generation_id
            self._runtime_state = RuntimeGenerationState.KNOWN
            try:
                self._persist_state()
            except Exception:
                self._restore_state_tuple(old_state)
                raise
            self._prune_unreferenced()
            return current

    def mark_runtime_absent(self) -> None:
        with self._lock:
            old_state = self._state_tuple()
            self._runtime_id = None
            self._runtime_classpath_id = None
            self._runtime_state = RuntimeGenerationState.ABSENT
            try:
                self._persist_state()
            except Exception:
                self._restore_state_tuple(old_state)
                raise
            self._prune_unreferenced()

    def mark_runtime_unknown(self) -> None:
        with self._lock:
            old_state = self._state_tuple()
            self._runtime_id = None
            self._runtime_state = RuntimeGenerationState.UNKNOWN
            try:
                self._persist_state()
            except Exception:
                self._restore_state_tuple(old_state)
                raise

    def public_summary(self) -> dict[str, object]:
        with self._lock:
            return {
                "initialized": self._current_id is not None,
                "candidate_pending": self._candidate_id is not None,
                "runtime_state": self._runtime_state.value,
                "current_ordinal": (
                    self._item(self._current_id).ordinal
                    if self._item(self._current_id) is not None
                    else None
                ),
            }

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def verify_generation(self, generation: BuildGeneration) -> None:
        """Reject mutation of a sealed output before it is trusted or copied."""

        manifest = _output_manifest(generation.output_directory)
        if (
            len(manifest) != generation.artifact_count
            or _manifest_fingerprint(manifest)
            != generation.output_fingerprint
        ):
            raise ProjectSessionError(
                "GENERATION_INTEGRITY_MISMATCH",
                "A sealed generation output changed unexpectedly.",
            )

    def _item(self, generation_id: str | None) -> BuildGeneration | None:
        return self._items.get(generation_id) if generation_id else None

    def _materialize(
        self,
        source: Path,
        *,
        build_world_fingerprint: str,
        parent_generation_id: str | None,
    ) -> BuildGeneration:
        source = source.expanduser().resolve(strict=True)
        for generation in self._items.values():
            if generation.output_directory == source:
                self.verify_generation(generation)
                break
        manifest = _output_manifest(source)
        output_fingerprint = _manifest_fingerprint(manifest)
        self._ordinal += 1
        generation_id = f"gen_{self._ordinal}_{uuid.uuid4().hex[:12]}"
        staging = self._staging / generation_id
        final = self._generations / generation_id
        try:
            staging.mkdir(parents=False, exist_ok=False)
            staged_output = staging / "output"
            shutil.copytree(source, staged_output)
            copied = _output_manifest(staged_output)
            if copied != manifest:
                raise ProjectSessionError(
                    "GENERATION_COPY_CHANGED",
                    "Generation output changed while it was copied.",
                )
            metadata = {
                "schema": "jolink.build-generation.v1",
                "generation_id": generation_id,
                "ordinal": self._ordinal,
                "parent_generation_id": parent_generation_id,
                "build_world_fingerprint": build_world_fingerprint,
                "artifact_count": len(manifest),
                "output_fingerprint": output_fingerprint,
                "created_at": time.time(),
            }
            _atomic_json(staging / "manifest.json", metadata)
            staging.replace(final)
            generation = BuildGeneration(
                generation_id=generation_id,
                ordinal=self._ordinal,
                parent_generation_id=parent_generation_id,
                build_world_fingerprint=build_world_fingerprint,
                output_directory=final / "output",
                artifact_count=len(manifest),
                output_fingerprint=output_fingerprint,
                created_at=float(metadata["created_at"]),
            )
            self._items[generation_id] = generation
            return generation
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(final, ignore_errors=True)
            self._ordinal -= 1
            raise

    def _persist_state(self) -> None:
        _atomic_json(
            self._state_file,
            {
                "schema": self._STATE_SCHEMA,
                "current_generation_id": self._current_id,
                "previous_generation_id": self._previous_id,
                "candidate_generation_id": self._candidate_id,
                "runtime_generation_id": self._runtime_id,
                "runtime_classpath_generation_id": (
                    self._runtime_classpath_id
                ),
                "runtime_state": self._runtime_state.value,
                "ordinal": self._ordinal,
            },
        )

    def _state_tuple(self) -> tuple[object, ...]:
        return (
            self._current_id,
            self._previous_id,
            self._candidate_id,
            self._runtime_id,
            self._runtime_classpath_id,
            self._runtime_state,
            self._ordinal,
        )

    def _restore_state_tuple(self, state: tuple[object, ...]) -> None:
        (
            self._current_id,
            self._previous_id,
            self._candidate_id,
            self._runtime_id,
            self._runtime_classpath_id,
            self._runtime_state,
            self._ordinal,
        ) = state  # type: ignore[assignment]

    def _remove_generation(self, generation: BuildGeneration) -> None:
        self._items.pop(generation.generation_id, None)
        shutil.rmtree(generation.output_directory.parent, ignore_errors=True)

    def _prune_unreferenced(
        self,
        *,
        preferred: Iterable[str | None] = (),
    ) -> None:
        retained = {
            value
            for value in (
                self._current_id,
                self._previous_id,
                self._candidate_id,
                self._runtime_id,
                self._runtime_classpath_id,
            )
            if value is not None
        }
        ordered = [
            value for value in preferred if value is not None
        ] + sorted(set(self._items) - retained)
        for generation_id in ordered:
            if generation_id in retained:
                continue
            generation = self._items.get(generation_id)
            if generation is not None:
                self._remove_generation(generation)


class JavaProjectSession:
    """One BuildWorld, generation store, and serialized reload lifecycle."""

    def __init__(
        self,
        *,
        root: Path,
        build_world_fingerprint: str,
    ) -> None:
        self.root = root.expanduser().resolve(strict=False)
        self.build_world_fingerprint = build_world_fingerprint
        self.generations = GenerationStore(self.root / "generation-store")
        self.compile_ready = False
        self.compile_session: Any | None = None
        self.jdt_bootstrap_state = "not_configured"
        self.jdt_unavailable_reason: str | None = None
        self.jdt_unavailable_details: dict[str, object] = {}
        self._jdt_worker_java_major: int | None = None
        self._jdt_worker_data_model: int | None = None
        self._active_reload: ReloadAttempt | None = None
        self._last_reload: ReloadAttempt | None = None
        self._last_successful_startup_ms: float | None = None
        self._generation_seal_ms: float | None = None
        self._source_manifest_before_ms: float | None = None
        self._source_manifest_after_ms: float | None = None
        self._source_snapshot_ms: float | None = None
        self._jdt_bootstrap_ms: float | None = None
        self._jdt_bootstrap_reused = False
        self._jdt_bootstrap_build_kind: str | None = None
        self._jdt_workspace_lease: Any | None = None
        self._reload_worker: threading.Thread | None = None
        self._retained_directories: set[Path] = set()
        self._closed = False
        self._lock = threading.RLock()

    @property
    def active_reload(self) -> ReloadAttempt | None:
        with self._lock:
            return self._active_reload

    def begin_reload(
        self,
        source_fingerprint: str,
        *,
        source_files: Iterable[Path] = (),
    ) -> ReloadAttempt:
        with self._lock:
            if self._active_reload is not None:
                raise ProjectSessionError(
                    "RELOAD_ALREADY_IN_PROGRESS",
                    "A reload operation is already active.",
                )
            attempt = ReloadAttempt(
                attempt_id=f"reload_{uuid.uuid4().hex[:12]}",
                stage=ReloadStage.PREPARING,
                started_at=time.time(),
                source_fingerprint_before=source_fingerprint,
                source_files=tuple(source_files),
            )
            self._active_reload = attempt
            return attempt

    def transition_reload(self, stage: ReloadStage) -> ReloadAttempt:
        with self._lock:
            attempt = self._require_active_reload()
            if attempt.stage in {ReloadStage.SUCCEEDED, ReloadStage.FAILED}:
                raise ProjectSessionError(
                    "RELOAD_ALREADY_FINISHED",
                    "The reload operation is already terminal.",
                )
            attempt.stage = stage
            return attempt

    def finish_reload(
        self,
        *,
        applied: bool | None,
        source_fingerprint_after: str,
        error_code: str | None = None,
        rolled_back: bool = False,
    ) -> ReloadAttempt:
        with self._lock:
            attempt = self._require_active_reload()
            attempt.applied = applied
            attempt.source_fingerprint_after = source_fingerprint_after
            attempt.error_code = error_code
            attempt.rolled_back = rolled_back
            attempt.stage = (
                ReloadStage.SUCCEEDED
                if applied is True
                else ReloadStage.FAILED
                if applied is False
                else attempt.stage
            )
            if applied is not None:
                attempt.finished_at = time.time()
                self._last_reload = attempt
                self._active_reload = None
            return attempt

    def attach_reload_worker(
        self,
        attempt: ReloadAttempt,
        worker: threading.Thread,
    ) -> None:
        with self._lock:
            if self._active_reload is not attempt:
                raise ProjectSessionError(
                    "RELOAD_OWNERSHIP_LOST",
                    "The reload attempt is no longer active.",
                )
            self._reload_worker = worker

    def request_reload_cancel(self, reason: str) -> None:
        with self._lock:
            if self._active_reload is not None:
                self._active_reload.cancel_requested = True
                self._active_reload.cancel_reason = str(reason)

    def reload_cancel_requested(self, attempt: ReloadAttempt) -> bool:
        with self._lock:
            return bool(
                self._active_reload is attempt
                and attempt.cancel_requested
            )

    def record_reload_result(
        self,
        attempt: ReloadAttempt,
        result: dict[str, Any],
    ) -> None:
        with self._lock:
            if self._active_reload is attempt or self._last_reload is attempt:
                attempt.result_data = dict(result)

    def record_successful_startup(self, duration_ms: float) -> None:
        with self._lock:
            self._last_successful_startup_ms = max(0.0, float(duration_ms))

    def record_generation_preparation(
        self,
        *,
        generation_seal_ms: float,
        source_snapshot_ms: float,
        source_manifest_before_ms: float | None = None,
        source_manifest_after_ms: float | None = None,
    ) -> None:
        with self._lock:
            self._generation_seal_ms = max(0.0, float(generation_seal_ms))
            self._source_manifest_before_ms = (
                max(0.0, float(source_manifest_before_ms))
                if source_manifest_before_ms is not None
                else None
            )
            self._source_manifest_after_ms = (
                max(0.0, float(source_manifest_after_ms))
                if source_manifest_after_ms is not None
                else None
            )
            self._source_snapshot_ms = max(0.0, float(source_snapshot_ms))

    def record_jdt_bootstrap(self, duration_ms: float) -> None:
        with self._lock:
            self._jdt_bootstrap_ms = max(0.0, float(duration_ms))

    def begin_jdt_bootstrap(self) -> None:
        with self._lock:
            if self._closed:
                raise ProjectSessionError(
                    "PROJECT_SESSION_CLOSED",
                    "The Java project session is already closed.",
                )
            self.jdt_bootstrap_state = "initializing"
            self.jdt_unavailable_reason = None
            self.jdt_unavailable_details = {}

    def record_jdt_worker_runtime(
        self,
        *,
        java_major: int,
        data_model: int,
    ) -> None:
        with self._lock:
            self._jdt_worker_java_major = int(java_major)
            self._jdt_worker_data_model = int(data_model)

    def complete_jdt_bootstrap(
        self,
        duration_ms: float,
        *,
        reused: bool = False,
        build_kind: str | None = None,
    ) -> None:
        with self._lock:
            self._jdt_bootstrap_ms = max(0.0, float(duration_ms))
            self._jdt_bootstrap_reused = bool(reused)
            self._jdt_bootstrap_build_kind = (
                str(build_kind) if build_kind is not None else None
            )
            self.jdt_bootstrap_state = "ready"
            self.jdt_unavailable_reason = None
            self.jdt_unavailable_details = {}

    def fail_jdt_bootstrap(
        self,
        *,
        reason: str,
        details: dict[str, object] | None = None,
        duration_ms: float | None = None,
    ) -> None:
        with self._lock:
            if duration_ms is not None:
                self._jdt_bootstrap_ms = max(0.0, float(duration_ms))
            self.compile_ready = False
            self.jdt_bootstrap_state = "unavailable"
            self.jdt_unavailable_reason = str(reason)
            self.jdt_unavailable_details = dict(details or {})

    def jdt_bootstrap_snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "state": self.jdt_bootstrap_state,
                "reason": self.jdt_unavailable_reason,
                "details": dict(self.jdt_unavailable_details),
            }

    def attach_compile_session(self, compile_session: Any) -> None:
        with self._lock:
            if self._closed:
                raise ProjectSessionError(
                    "PROJECT_SESSION_CLOSED",
                    "The Java project session is already closed.",
                )
            if self.compile_session is not None:
                raise ProjectSessionError(
                    "COMPILE_SESSION_ALREADY_ATTACHED",
                    "A CompileSession is already attached to this project.",
                )
            self.compile_session = compile_session
            self.compile_ready = bool(getattr(compile_session, "ready", False))

    def attach_jdt_workspace_lease(self, lease: Any) -> None:
        with self._lock:
            if self._jdt_workspace_lease is not None:
                raise ProjectSessionError(
                    "JDT_WORKSPACE_ALREADY_ATTACHED",
                    "A persistent JDT workspace is already attached.",
                )
            self._jdt_workspace_lease = lease

    def invalidate_jdt_workspace(self) -> None:
        with self._lock:
            lease = self._jdt_workspace_lease
            self._jdt_workspace_lease = None
        if lease is not None:
            lease.release(clean=False)

    def refresh_compile_ready(self) -> bool:
        with self._lock:
            self.compile_ready = bool(
                self.compile_session is not None
                and getattr(self.compile_session, "ready", False)
            )
            if (
                not self.compile_ready
                and self.compile_session is not None
                and self.jdt_bootstrap_state == "ready"
            ):
                self.jdt_bootstrap_state = "unavailable"
                self.jdt_unavailable_reason = "JDT_COMPILE_SESSION_UNAVAILABLE"
                self.jdt_unavailable_details = {}
            return self.compile_ready

    def clear_compile_session(self, expected: Any) -> None:
        with self._lock:
            if self.compile_session is expected:
                self.compile_session = None
                self.compile_ready = False

    def retain_directory(self, directory: Path) -> None:
        """Keep launch metadata used by the active compile plan until close."""

        with self._lock:
            self._retained_directories.add(
                directory.expanduser().resolve(strict=False)
            )

    def public_status(self) -> dict[str, object]:
        with self._lock:
            active = self._active_reload
            last = self._last_reload
            last_payload: dict[str, object] | None = None
            if last is not None:
                last_payload = dict(last.result_data)
                last_payload.update({
                    "applied": last.applied,
                    "reload_id": last.attempt_id,
                    "stage": last.stage.value,
                    "apply_method": last.apply_method,
                    "compile_ms": last.compile_ms,
                    "startup_ms": last.startup_ms,
                    "source_changes_pending": last.source_changes_pending,
                    "rolled_back": last.rolled_back,
                    "error_code": last.error_code,
                    "total_ms": round(
                        max(
                            0.0,
                            (last.finished_at or time.time())
                            - last.started_at,
                        )
                        * 1000,
                        1,
                    ),
                })
            return {
                "compile_ready": self.compile_ready,
                "jdt_bootstrap_state": self.jdt_bootstrap_state,
                "jdt_bootstrap_reused": self._jdt_bootstrap_reused,
                "jdt_bootstrap_build_kind": self._jdt_bootstrap_build_kind,
                "jdt_worker": (
                    {
                        "java_major": self._jdt_worker_java_major,
                        "data_model": self._jdt_worker_data_model,
                    }
                    if self._jdt_worker_java_major is not None
                    else None
                ),
                "generation": self.generations.public_summary().get(
                    "current_ordinal"
                ),
                "active_operation": (
                    {
                        "operation": "reload",
                        "reload_id": active.attempt_id,
                        "stage": active.stage.value,
                        "elapsed_ms": round(
                            max(0.0, time.time() - active.started_at) * 1000,
                            1,
                        ),
                        "cancel_requested": active.cancel_requested,
                    }
                    if active is not None
                    else None
                ),
                "last_reload": last_payload,
                "last_successful_startup_ms": (
                    self._last_successful_startup_ms
                ),
                "product_timing_ms": {
                    "generation_seal": self._generation_seal_ms,
                    "source_manifest_before": (
                        self._source_manifest_before_ms
                    ),
                    "source_manifest_after": self._source_manifest_after_ms,
                    "source_snapshot": self._source_snapshot_ms,
                    "jdt_bootstrap": self._jdt_bootstrap_ms,
                },
            }

    def close(self, *, cleanup_retained: bool = True) -> None:
        with self._lock:
            self._closed = True
            active_reload = self._active_reload
            if active_reload is not None:
                active_reload.cancel_requested = True
                active_reload.cancel_reason = "PROJECT_SESSION_CLOSED"
            retained = tuple(self._retained_directories)
            self._retained_directories.clear()
            compile_session = self.compile_session
            self.compile_session = None
            self.compile_ready = False
            reload_worker = self._reload_worker
            workspace_lease = self._jdt_workspace_lease
            self._jdt_workspace_lease = None
        if compile_session is not None:
            try:
                compile_session.close()
            except Exception:
                pass
        if (
            reload_worker is not None
            and reload_worker is not threading.current_thread()
            and reload_worker.is_alive()
        ):
            reload_worker.join(5.0)
        if workspace_lease is not None:
            workspace_lease.release(
                clean=bool(
                    active_reload is None
                    and compile_session is not None
                    and getattr(compile_session, "last_close_clean", False)
                )
            )
        if cleanup_retained:
            for directory in retained:
                shutil.rmtree(directory, ignore_errors=True)
        self.generations.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _require_active_reload(self) -> ReloadAttempt:
        if self._active_reload is None:
            raise ProjectSessionError(
                "NO_ACTIVE_RELOAD",
                "No reload operation is active.",
            )
        return self._active_reload


__all__ = [
    "BuildGeneration",
    "GenerationStore",
    "JavaProjectSession",
    "ProjectSessionError",
    "ReloadAttempt",
    "ReloadStage",
    "RuntimeGenerationState",
]
