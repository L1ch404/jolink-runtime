"""Small local-file store for reusable product JDT workspaces."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any
from ..core.diagnostic_logging import log_diagnostic

logger = logging.getLogger(__name__)


class JdtWorkspaceStoreError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def jolink_cache_root() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    elif os.environ.get("XDG_CACHE_HOME"):
        base = Path(os.environ["XDG_CACHE_HOME"])
    else:
        base = Path.home() / ".cache"
    return base / "jolink-runtime"


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(_canonical_json(value) + b"\n")
            stream.flush()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class JdtWorkspaceLease:
    _SCHEMA = "jolink.jdt-workspace-state.v1"

    def __init__(
        self,
        *,
        root: Path,
        state_file: Path,
        identity: dict[str, Any],
        reusable: bool,
    ) -> None:
        self.root = root
        self.state_file = state_file
        self.identity = dict(identity)
        self.identity_fingerprint = _fingerprint(self.identity)
        self.reusable = bool(reusable)
        self._initialized = reusable
        self._released = False

    def _write_state(self, *, clean_shutdown: bool) -> None:
        _atomic_json(
            self.state_file,
            {
                "schema": self._SCHEMA,
                "identity_fingerprint": self.identity_fingerprint,
                "identity": self.identity,
                "clean_shutdown": bool(clean_shutdown),
                "updated_at": time.time(),
            },
        )

    def reset_for_full(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        self.reusable = False
        self._initialized = False

    def mark_initialized(self) -> None:
        if not self.root.is_dir():
            raise JdtWorkspaceStoreError(
                "JDT_WORKSPACE_UNAVAILABLE",
                "The local JDT workspace is unavailable.",
            )
        self._write_state(clean_shutdown=False)
        self._initialized = True

    def mark_dirty(self) -> None:
        if self._initialized and self.root.is_dir():
            self._write_state(clean_shutdown=False)

    def checkpoint(self) -> None:
        if not self._initialized or not self.root.is_dir():
            raise JdtWorkspaceStoreError(
                "JDT_WORKSPACE_UNAVAILABLE",
                "The local JDT workspace is unavailable.",
            )
        self._write_state(clean_shutdown=True)

    def release(self, *, clean: bool) -> None:
        if self._released:
            return
        self._released = True
        if self._initialized and self.root.is_dir():
            self._write_state(clean_shutdown=clean)


class JdtWorkspaceStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (
            root.expanduser().resolve(strict=False)
            if root is not None
            else jolink_cache_root() / "jdt-workspaces"
        )

    def claim(
        self,
        *,
        project_root: Path,
        module_root: Path,
        identity: dict[str, Any],
    ) -> JdtWorkspaceLease:
        project_key = hashlib.sha256(
            (
                os.path.normcase(str(project_root.resolve(strict=False)))
                + "\0"
                + os.path.normcase(str(module_root.resolve(strict=False)))
            ).encode("utf-8", errors="surrogateescape")
        ).hexdigest()[:24]
        root = self.root / project_key
        state_file = root / "state.json"
        identity_fingerprint = _fingerprint(identity)
        state = self._read_json(state_file)
        reusable = bool(
            state.get("schema") == JdtWorkspaceLease._SCHEMA
            and state.get("identity_fingerprint") == identity_fingerprint
            and root.joinpath("workspace").is_dir()
        )
        if reusable:
            reason = "identity_matched"
        elif not state:
            reason = "state_unavailable"
        elif state.get("schema") != JdtWorkspaceLease._SCHEMA:
            reason = "schema_changed"
        elif state.get("identity_fingerprint") != identity_fingerprint:
            reason = "identity_changed"
        else:
            reason = "workspace_missing"
        previous_fields = state.get("identity")
        if not isinstance(previous_fields, dict):
            previous_fields = {}
        changed_fields = sorted(
            key for key in set(previous_fields) | set(identity)
            if previous_fields.get(key) != identity.get(key)
        )
        log_diagnostic(
            logger, logging.INFO,
            "jdt.workspace.claim workspace=%r reusable=%s reason=%s "
            "previous_identity=%s current_identity=%s changed_identity_fields=%s",
            str(root), reusable, reason, state.get("identity_fingerprint"), identity_fingerprint,
            lambda: json.dumps(changed_fields, ensure_ascii=True),
        )
        if not reusable:
            shutil.rmtree(root, ignore_errors=True)
        lease = JdtWorkspaceLease(
            root=root,
            state_file=state_file,
            identity=identity,
            reusable=reusable,
        )
        return lease

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}


__all__ = [
    "JdtWorkspaceLease",
    "JdtWorkspaceStore",
    "JdtWorkspaceStoreError",
    "jolink_cache_root",
]
