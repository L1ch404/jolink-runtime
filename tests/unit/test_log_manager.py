from __future__ import annotations

from pathlib import Path

from jolink_runtime.adapters.java import log_manager as log_module
from jolink_runtime.adapters.java.log_manager import (
    LogManager,
    _MAX_TAIL_RETURN_BYTES,
    _MAX_TAIL_SCAN_BYTES,
)


def _manager_for(path: Path) -> LogManager:
    manager = LogManager(str(path.parent))
    manager._current_file = str(path)
    return manager


def test_small_tail_keeps_exact_total_and_utf8_lines(tmp_path: Path) -> None:
    log_file = tmp_path / "small.log"
    log_file.write_bytes("第一行\n第二行\n第三行".encode("utf-8"))

    result = _manager_for(log_file).tail(2)

    assert result["lines"] == ["第二行\n", "第三行"]
    assert result["requested_lines"] == 2
    assert result["returned_lines"] == 2
    assert result["total_lines"] == 3
    assert result["total_lines_exact"] is True
    assert result["snapshot_size_bytes"] == log_file.stat().st_size
    assert result["has_more_before"] is True
    assert result["truncated"] is False
    assert result["snapshot_consistent"] is True


def test_tail_reads_only_the_file_size_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "growing.log"
    initial = b"before-1\nbefore-2\nbefore-3\n"
    late = b"late-1\nlate-2\n"
    log_file.write_bytes(initial)
    real_fstat = log_module.os.fstat
    appended = False

    def snapshot_then_append(fd: int):
        nonlocal appended
        snapshot = real_fstat(fd)
        if not appended:
            appended = True
            with open(log_file, "ab") as writer:
                writer.write(late)
        return snapshot

    monkeypatch.setattr(log_module.os, "fstat", snapshot_then_append)

    result = _manager_for(log_file).tail(2)

    assert result["lines"] == ["before-2\n", "before-3\n"]
    assert result["snapshot_size_bytes"] == len(initial)
    assert log_file.stat().st_size == len(initial) + len(late)
    assert all("late-" not in line for line in result["lines"])


def test_large_tail_stops_after_enough_newest_lines(tmp_path: Path) -> None:
    log_file = tmp_path / "many-lines.log"
    lines = [
        f"{index:06d}-{'x' * 64}\n"
        for index in range(20_000)
    ]
    log_file.write_text("".join(lines), encoding="utf-8")

    result = _manager_for(log_file).tail(3)

    assert result["lines"] == lines[-3:]
    assert result["scanned_bytes"] < result["snapshot_size_bytes"]
    assert result["scanned_bytes"] <= 64 * 1024
    assert result["total_lines"] is None
    assert result["total_lines_exact"] is False
    assert result["has_more_before"] is True
    assert result["scan_limit_reached"] is False
    assert result["truncated"] is False


def test_scan_limit_reports_incomplete_huge_line_tail(tmp_path: Path) -> None:
    log_file = tmp_path / "huge-line.log"
    log_file.write_bytes(
        b"x" * (_MAX_TAIL_SCAN_BYTES + 1024) + b"\nlast\n"
    )

    result = _manager_for(log_file).tail(2)

    assert result["lines"] == ["last\n"]
    assert result["scanned_bytes"] == _MAX_TAIL_SCAN_BYTES
    assert result["scan_limit_reached"] is True
    assert result["truncated"] is True
    assert result["truncation_reasons"] == ["scan_limit"]
    assert result["total_lines"] is None
    assert any("bounded log scan" in item for item in result["warnings"])


def test_single_huge_return_line_is_capped(tmp_path: Path) -> None:
    log_file = tmp_path / "huge-output.log"
    log_file.write_bytes(b"y" * (_MAX_TAIL_RETURN_BYTES + 1024))

    result = _manager_for(log_file).tail(1)

    assert result["returned_lines"] == 1
    assert result["returned_bytes"] <= _MAX_TAIL_RETURN_BYTES
    assert len(result["lines"][0].encode("utf-8")) <= _MAX_TAIL_RETURN_BYTES
    assert result["total_lines"] == 1
    assert result["total_lines_exact"] is True
    assert result["truncated"] is True
    assert result["first_line_truncated"] is True
    assert result["truncation_reasons"] == ["output_limit"]
    assert any("MCP result bounded" in item for item in result["warnings"])


def test_empty_log_snapshot_is_complete(tmp_path: Path) -> None:
    log_file = tmp_path / "empty.log"
    log_file.write_bytes(b"")

    result = _manager_for(log_file).tail(50)

    assert result["lines"] == []
    assert result["returned_lines"] == 0
    assert result["total_lines"] == 0
    assert result["total_lines_exact"] is True
    assert result["snapshot_size_bytes"] == 0
    assert result["scanned_bytes"] == 0
    assert result["truncated"] is False


def test_repeated_reads_report_log_growth_without_a_cursor(
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "growth.log"
    log_file.write_bytes(b"first\n")
    manager = _manager_for(log_file)

    first = manager.tail(10)
    with open(log_file, "ab") as writer:
        writer.write(b"second\n")
    appended = manager.tail(10)
    unchanged = manager.tail(10)
    log_file.write_bytes(b"reset\n")
    replaced = manager.tail(10)

    assert first["growth_state"] == "first_observation"
    assert "previous_snapshot_size_bytes" not in first
    assert appended["growth_state"] == "appended"
    assert appended["previous_snapshot_size_bytes"] == len(b"first\n")
    assert appended["new_bytes_since_previous_read"] == len(b"second\n")
    assert unchanged["growth_state"] == "unchanged"
    assert unchanged["new_bytes_since_previous_read"] == 0
    assert replaced["growth_state"] == "truncated_or_replaced"
    assert replaced["new_bytes_since_previous_read"] == 0
