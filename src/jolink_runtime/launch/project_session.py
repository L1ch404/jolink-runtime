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
from dataclasses import dataclass
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
    source_fingerprint_after: str | None = None
    compile_ms: float | None = None
    startup_ms: float | None = None
    compiled_sources: int | None = None
    apply_method: str | None = None
    applied: bool | None = None
    rolled_back: bool = False
    error_code: str | None = None

    @property
    def source_changes_pending(self) -> bool:
        return bool(
            self.source_fingerprint_after is not None
            and self.source_fingerprint_after
            != self.source_fingerprint_before
        )


class GenerationStore:
    """Own current/previous/candidate immutable module outputs."""

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
            generation = self._materialize(
                output_directory,
                build_world_fingerprint=build_world_fingerprint,
                parent_generation_id=None,
            )
            self._current_id = generation.generation_id
            if runtime_active:
                self._runtime_id = generation.generation_id
                self._runtime_state = RuntimeGenerationState.KNOWN
            self._persist_state()
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
            generation = self._materialize(
                output_directory,
                build_world_fingerprint=build_world_fingerprint,
                parent_generation_id=None,
            )
            self._candidate_id = generation.generation_id
            self._persist_state()
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
            generation = self._materialize(
                output_directory,
                build_world_fingerprint=build_world_fingerprint,
                parent_generation_id=self._current_id,
            )
            self._candidate_id = generation.generation_id
            self._persist_state()
            return generation

    def promote_candidate(self) -> BuildGeneration:
        """Promote only after Runtime application success is known."""

        with self._lock:
            candidate = self._item(self._candidate_id)
            if candidate is None:
                raise ProjectSessionError(
                    "CANDIDATE_UNAVAILABLE",
                    "No candidate generation is ready to promote.",
                )
            obsolete_previous = self._previous_id
            old_current = self._current_id
            self._previous_id = old_current
            self._current_id = candidate.generation_id
            self._runtime_id = candidate.generation_id
            self._runtime_state = RuntimeGenerationState.KNOWN
            self._candidate_id = None
            self._persist_state()
            if obsolete_previous not in {None, old_current}:
                obsolete = self._items.pop(obsolete_previous, None)
                if obsolete is not None:
                    shutil.rmtree(
                        obsolete.output_directory.parent,
                        ignore_errors=True,
                    )
            return candidate

    def discard_candidate(self) -> None:
        with self._lock:
            candidate = self._item(self._candidate_id)
            self._candidate_id = None
            if candidate is not None:
                self._items.pop(candidate.generation_id, None)
                shutil.rmtree(candidate.output_directory.parent, ignore_errors=True)
            self._persist_state()

    def mark_runtime_current(self) -> BuildGeneration:
        with self._lock:
            current = self._item(self._current_id)
            if current is None:
                raise ProjectSessionError(
                    "CURRENT_GENERATION_UNAVAILABLE",
                    "No current generation can be marked as running.",
                )
            self._runtime_id = current.generation_id
            self._runtime_state = RuntimeGenerationState.KNOWN
            self._persist_state()
            return current

    def mark_runtime_absent(self) -> None:
        with self._lock:
            self._runtime_id = None
            self._runtime_state = RuntimeGenerationState.ABSENT
            self._persist_state()

    def mark_runtime_unknown(self) -> None:
        with self._lock:
            self._runtime_id = None
            self._runtime_state = RuntimeGenerationState.UNKNOWN
            self._persist_state()

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
                "runtime_state": self._runtime_state.value,
                "ordinal": self._ordinal,
            },
        )


class JavaProjectSession:
    """One BuildWorld, generation store, and serialized reload lifecycle."""

    def __init__(
        self,
        *,
        root: Path,
        build_world_fingerprint: str,
    ) -> None:
        self.build_world_fingerprint = build_world_fingerprint
        self.generations = GenerationStore(root / "generation-store")
        self.compile_ready = False
        self._active_reload: ReloadAttempt | None = None
        self._last_reload: ReloadAttempt | None = None
        self._last_successful_startup_ms: float | None = None
        self._lock = threading.RLock()

    @property
    def active_reload(self) -> ReloadAttempt | None:
        with self._lock:
            return self._active_reload

    def begin_reload(self, source_fingerprint: str) -> ReloadAttempt:
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
                self._last_reload = attempt
                self._active_reload = None
            return attempt

    def record_successful_startup(self, duration_ms: float) -> None:
        with self._lock:
            self._last_successful_startup_ms = max(0.0, float(duration_ms))

    def public_status(self) -> dict[str, object]:
        with self._lock:
            active = self._active_reload
            last = self._last_reload
            return {
                "compile_ready": self.compile_ready,
                "generation": self.generations.public_summary().get(
                    "current_ordinal"
                ),
                "active_operation": (
                    {
                        "operation": "reload",
                        "stage": active.stage.value,
                        "elapsed_ms": round(
                            max(0.0, time.time() - active.started_at) * 1000,
                            1,
                        ),
                    }
                    if active is not None
                    else None
                ),
                "last_reload": (
                    {
                        "applied": last.applied,
                        "apply_method": last.apply_method,
                        "compile_ms": last.compile_ms,
                        "startup_ms": last.startup_ms,
                        "source_changes_pending": (
                            last.source_changes_pending
                        ),
                        "rolled_back": last.rolled_back,
                    }
                    if last is not None
                    else None
                ),
                "last_successful_startup_ms": (
                    self._last_successful_startup_ms
                ),
            }

    def close(self) -> None:
        self.generations.close()

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
