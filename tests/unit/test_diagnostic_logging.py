from __future__ import annotations

import logging
from pathlib import Path

from jolink_runtime.core import diagnostic_logging


def test_private_diagnostic_log_is_bounded_and_discoverable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "private/mcp.log"
    diagnostic_logging._reset_private_diagnostic_logging_for_tests()
    monkeypatch.setattr(
        diagnostic_logging,
        "private_diagnostic_log_path",
        lambda: path,
    )
    try:
        status = diagnostic_logging.configure_private_diagnostic_logging()
        logging.getLogger("jolink.test").warning("diagnostic-marker")
        for handler in logging.getLogger().handlers:
            handler.flush()

        assert status == {
            "status": "active",
            "log_file": str(path),
            "max_bytes": 4 * 1024 * 1024,
            "backup_count": 3,
            "error_type": None,
        }
        assert "diagnostic-marker" in path.read_text(encoding="utf-8")
    finally:
        diagnostic_logging._reset_private_diagnostic_logging_for_tests()


def test_private_diagnostic_log_failure_never_blocks_startup(
    monkeypatch,
) -> None:
    diagnostic_logging._reset_private_diagnostic_logging_for_tests()

    def fail(*_args, **_kwargs):
        raise PermissionError("private log unavailable")

    monkeypatch.setattr(
        diagnostic_logging.logging.handlers,
        "RotatingFileHandler",
        fail,
    )
    try:
        status = diagnostic_logging.configure_private_diagnostic_logging()
        assert status == {
            "status": "stderr_only",
            "log_file": None,
            "error_type": "PermissionError",
        }
    finally:
        diagnostic_logging._reset_private_diagnostic_logging_for_tests()
