"""
Java log manager — reads console output from a rotating log file.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time


logger = logging.getLogger(__name__)


class LogManager:
    """Manage log file creation and reading."""

    def __init__(self, base_dir: str | None = None):
        self._base_dir = base_dir or os.path.join(tempfile.gettempdir(), "jolink-logs")
        os.makedirs(self._base_dir, exist_ok=True)
        self._current_file: str | None = None

    def create(self, main_class: str) -> str:
        """Create a new log file and return its path."""
        ts = int(time.time())
        self._current_file = os.path.join(
            self._base_dir, f"{main_class}-{ts}.log"
        )
        logger.info(
            "java_runtime.console_log.created main_class=%s path=%s",
            main_class or "-", self._current_file,
        )
        return self._current_file

    @property
    def path(self) -> str | None:
        return self._current_file

    def tail(self, n: int = 50) -> dict:
        """Return the last N lines of the current log."""
        if not self._current_file:
            logger.warning("java_runtime.console_log.tail.failed reason=no_log_file")
            return {"error": "No log file created"}
        try:
            with open(
                self._current_file,
                "r",
                encoding="utf-8",
                errors="replace",
            ) as f:
                lines = f.readlines()
            result = {
                "lines": lines[-n:] if n > 0 else lines,
                "total_lines": len(lines),
                "log_file": self._current_file,
            }
            logger.info(
                "java_runtime.console_log.tail path=%s requested_lines=%s "
                "returned_lines=%s total_lines=%s",
                self._current_file, n, len(result["lines"]), len(lines),
            )
            return result
        except OSError as exc:
            logger.warning(
                "java_runtime.console_log.tail.failed path=%s error_type=%s error=%s",
                self._current_file, type(exc).__name__, exc,
            )
            return {
                "error": (
                    f"Unable to read log file '{self._current_file}': "
                    f"{type(exc).__name__}: {exc}"
                )
            }
