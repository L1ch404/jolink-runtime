"""Private, bounded diagnostics that must never prevent MCP startup."""

from __future__ import annotations

import logging
import logging.handlers
import os
import threading
from pathlib import Path
from typing import Any


_MAX_BYTES = 4 * 1024 * 1024
_BACKUP_COUNT = 3
_lock = threading.Lock()
_state: dict[str, Any] = {
    "status": "stderr_only",
    "log_file": None,
    "error_type": None,
}


def private_diagnostic_log_path() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    elif os.environ.get("XDG_CACHE_HOME"):
        base = Path(os.environ["XDG_CACHE_HOME"])
    else:
        base = Path.home() / ".cache"
    return base / "jolink-runtime" / "logs" / "mcp.log"


def configure_private_diagnostic_logging() -> dict[str, Any]:
    """Add one rotating file handler, falling back to stderr on any error."""

    global _state
    with _lock:
        root = logging.getLogger()
        for handler in root.handlers:
            if getattr(handler, "_jolink_private_diagnostic", False):
                return dict(_state)
        path = private_diagnostic_log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
                delay=False,
            )
            handler.setLevel(logging.INFO)
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)s %(name)s %(message)s"
                )
            )
            handler._jolink_private_diagnostic = True
            root.addHandler(handler)
            try:
                path.parent.chmod(0o700)
                path.chmod(0o600)
            except OSError:
                pass
            _state = {
                "status": "active",
                "log_file": str(path),
                "max_bytes": _MAX_BYTES,
                "backup_count": _BACKUP_COUNT,
                "error_type": None,
            }
        except Exception as error:
            _state = {
                "status": "stderr_only",
                "log_file": None,
                "error_type": type(error).__name__,
            }
            logging.getLogger(__name__).warning(
                "jolink.private_diagnostic_log.unavailable error_type=%s",
                type(error).__name__,
            )
        return dict(_state)


def private_diagnostic_logging_status() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def _reset_private_diagnostic_logging_for_tests() -> None:
    global _state
    with _lock:
        root = logging.getLogger()
        for handler in tuple(root.handlers):
            if getattr(handler, "_jolink_private_diagnostic", False):
                root.removeHandler(handler)
                handler.close()
        _state = {
            "status": "stderr_only",
            "log_file": None,
            "error_type": None,
        }


__all__ = [
    "configure_private_diagnostic_logging",
    "private_diagnostic_log_path",
    "private_diagnostic_logging_status",
]
