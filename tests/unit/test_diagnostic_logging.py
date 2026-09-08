from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import sys

import pytest

from jolink_runtime.core import diagnostic_logging


def test_private_diagnostic_log_is_bounded_and_discoverable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "private/mcp.log"
    monkeypatch.delenv("JOLINK_LOG_LEVEL", raising=False)
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
            "level": "WARNING",
        }
        assert "diagnostic-marker" in path.read_text(encoding="utf-8")
    finally:
        diagnostic_logging._reset_private_diagnostic_logging_for_tests()


def test_private_diagnostic_log_failure_never_blocks_startup(
    monkeypatch,
) -> None:
    monkeypatch.delenv("JOLINK_LOG_LEVEL", raising=False)
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
            "level": "WARNING",
        }
    finally:
        diagnostic_logging._reset_private_diagnostic_logging_for_tests()


@pytest.mark.parametrize(
    "configured,expected",
    [
        (None, "WARNING"),
        ("DEBUG", "DEBUG"),
        (" info ", "INFO"),
        ("warn", "WARNING"),
        ("ERROR", "ERROR"),
        ("OFF", "OFF"),
        ("typo", "WARNING"),
    ],
)
def test_level_controls_stderr_and_private_file(tmp_path, configured, expected):
    environment = {
        **os.environ,
        "XDG_CACHE_HOME": str(tmp_path),
        "LOCALAPPDATA": str(tmp_path),
    }
    environment.pop("JOLINK_LOG_LEVEL", None)
    if configured is not None:
        environment["JOLINK_LOG_LEVEL"] = configured
    code = """
import logging
from jolink_runtime.transport.stdio import _configure_stderr_logging
_configure_stderr_logging()
logger = logging.getLogger('jolink.test.levels')
for level in ('DEBUG', 'INFO', 'WARNING', 'ERROR'):
    logger.log(getattr(logging, level), 'level-marker-' + level)
logging.getLogger('mcp').debug('private-protocol-must-not-be-dumped')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert result.stdout == ""
    path = tmp_path / "jolink-runtime/logs/mcp.log"
    if expected == "OFF":
        assert not path.exists()
        assert result.stderr == ""
        return
    file_log = path.read_text(encoding="utf-8")
    for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
        enabled = getattr(logging, level) >= getattr(logging, expected)
        assert (f"level-marker-{level}" in file_log) is enabled
        assert (f"level-marker-{level}" in result.stderr) is enabled
    assert "private-protocol-must-not-be-dumped" not in file_log


def test_disabled_diagnostic_does_not_evaluate_fields(monkeypatch):
    logger = logging.getLogger("jolink.test.lazy")
    monkeypatch.setattr(logger, "isEnabledFor", lambda _level: False)

    def expensive():
        raise AssertionError("disabled logging must not read diagnostic state")

    diagnostic_logging.log_diagnostic(logger, logging.INFO, "state=%s", expensive)


def test_enabled_diagnostic_evaluates_fields_once(caplog):
    calls = []

    def field():
        calls.append(True)
        return "saved"

    with caplog.at_level(logging.INFO):
        diagnostic_logging.log_diagnostic(
            logging.getLogger("jolink.test.lazy"), logging.INFO, "state=%s", field
        )
    assert calls == [True]
    assert "state=saved" in caplog.text
