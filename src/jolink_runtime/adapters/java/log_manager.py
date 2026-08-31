"""
Java log manager — reads console output from a rotating log file.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time


logger = logging.getLogger(__name__)

_TAIL_CHUNK_BYTES = 64 * 1024
_MAX_TAIL_SCAN_BYTES = 4 * 1024 * 1024
_MAX_TAIL_RETURN_BYTES = 256 * 1024


def _bounded_return_lines(
    lines: list[str],
    *,
    max_return_bytes: int,
) -> tuple[list[str], int, bool, bool]:
    """Keep the newest complete lines within the MCP output byte budget."""
    selected: list[str] = []
    returned_bytes = 0
    output_limited = False
    first_line_truncated = False

    for line in reversed(lines):
        encoded = line.encode("utf-8", errors="replace")
        remaining = max_return_bytes - returned_bytes
        if len(encoded) <= remaining:
            selected.append(line)
            returned_bytes += len(encoded)
            continue

        output_limited = True
        if not selected and remaining > 0:
            # A single log line can contain a very large payload. Preserve its
            # newest suffix without allowing that line to flood MCP output.
            suffix = encoded[-remaining:]
            suffix_text = suffix.decode("utf-8", errors="replace")
            suffix_bytes = suffix_text.encode("utf-8")
            if len(suffix_bytes) > remaining:
                suffix_text = suffix_bytes[-remaining:].decode(
                    "utf-8",
                    errors="ignore",
                )
                suffix_bytes = suffix_text.encode("utf-8")
            selected.append(suffix_text)
            returned_bytes = len(suffix_bytes)
            first_line_truncated = True
        break

    selected.reverse()
    return selected, returned_bytes, output_limited, first_line_truncated


def read_log_tail_snapshot(
    log_file: str,
    n: int = 50,
    *,
    encoding: str = "utf-8",
    max_scan_bytes: int = _MAX_TAIL_SCAN_BYTES,
    max_return_bytes: int = _MAX_TAIL_RETURN_BYTES,
) -> dict:
    """Read a bounded tail from a fixed file-size snapshot.

    The file may still be receiving Java stdout/stderr while this function
    runs. ``snapshot_size_bytes`` freezes the visible end offset so a noisy
    writer cannot make the reader chase a moving EOF indefinitely.
    """
    requested_lines = max(1, int(n))
    max_scan_bytes = max(1, int(max_scan_bytes))
    max_return_bytes = max(1, int(max_return_bytes))

    with open(log_file, "rb") as handle:
        snapshot_size = os.fstat(handle.fileno()).st_size
        position = snapshot_size
        scanned_bytes = 0
        newline_count = 0
        chunks: list[bytes] = []
        file_changed_during_read = False

        while (
            position > 0
            and scanned_bytes < max_scan_bytes
            and newline_count < requested_lines + 1
        ):
            read_size = min(
                _TAIL_CHUNK_BYTES,
                position,
                max_scan_bytes - scanned_bytes,
            )
            read_start = position - read_size
            handle.seek(read_start)
            chunk = handle.read(read_size)
            if len(chunk) != read_size:
                file_changed_during_read = True
                if not chunk:
                    break
            chunks.append(chunk)
            scanned_bytes += len(chunk)
            newline_count += chunk.count(b"\n")
            position = read_start

        raw = b"".join(reversed(chunks))
        starts_mid_line = False
        if position > 0:
            handle.seek(position - 1)
            starts_mid_line = handle.read(1) != b"\n"

    prefix_was_discarded = False
    first_line_truncated = False
    if starts_mid_line and raw:
        first_newline = raw.find(b"\n")
        if first_newline >= 0:
            raw = raw[first_newline + 1:]
            prefix_was_discarded = True
        else:
            # The newest line alone exceeds the scan budget. Its suffix is
            # still useful evidence as long as the truncation is explicit.
            first_line_truncated = True

    decoded_lines = raw.decode(encoding, errors="replace").splitlines(
        keepends=True
    )
    requested_tail = decoded_lines[-requested_lines:]
    (
        returned_lines,
        returned_bytes,
        output_limited,
        output_first_line_truncated,
    ) = _bounded_return_lines(
        requested_tail,
        max_return_bytes=max_return_bytes,
    )
    first_line_truncated = (
        first_line_truncated or output_first_line_truncated
    )

    total_lines_exact = position == 0 and not file_changed_during_read
    total_lines = len(decoded_lines) if total_lines_exact else None
    has_more_before = position > 0 or len(decoded_lines) > requested_lines
    scan_limit_reached = (
        position > 0 and scanned_bytes >= max_scan_bytes
    )
    scan_truncated = (
        scan_limit_reached
        and (
            len(requested_tail) < requested_lines
            or first_line_truncated
        )
    )
    truncated = scan_truncated or output_limited

    warnings: list[str] = []
    truncation_reasons: list[str] = []
    if scan_truncated:
        truncation_reasons.append("scan_limit")
        warnings.append(
            "The bounded log scan ended before all requested complete lines "
            "were available."
        )
    if output_limited:
        truncation_reasons.append("output_limit")
        warnings.append(
            "The returned log tail was limited to keep the MCP result bounded."
        )
    if file_changed_during_read:
        warnings.append(
            "The log file changed size while its snapshot was being read."
        )

    result = {
        "lines": returned_lines,
        "requested_lines": requested_lines,
        "returned_lines": len(returned_lines),
        "returned_bytes": returned_bytes,
        "total_lines": total_lines,
        "total_lines_exact": total_lines_exact,
        "snapshot_size_bytes": snapshot_size,
        "scanned_bytes": scanned_bytes,
        "max_scan_bytes": max_scan_bytes,
        "max_return_bytes": max_return_bytes,
        "has_more_before": has_more_before,
        "scan_limit_reached": scan_limit_reached,
        "truncated": truncated,
        "first_line_truncated": first_line_truncated,
        "snapshot_consistent": not file_changed_during_read,
        "log_file": log_file,
        "encoding": encoding,
    }
    if prefix_was_discarded:
        result["discarded_partial_prefix"] = True
    if truncation_reasons:
        result["truncation_reasons"] = truncation_reasons
    if warnings:
        result["warnings"] = warnings
    return result


class LogManager:
    """Manage log file creation and reading."""

    def __init__(self, base_dir: str | None = None):
        self._base_dir = base_dir or os.path.join(tempfile.gettempdir(), "jolink-logs")
        os.makedirs(self._base_dir, exist_ok=True)
        self._current_file: str | None = None
        self._last_snapshot_size: int | None = None

    def create(self, main_class: str) -> str:
        """Create a new log file and return its path."""
        ts = int(time.time())
        self._current_file = os.path.join(
            self._base_dir, f"{main_class}-{ts}.log"
        )
        self._last_snapshot_size = None
        logger.info(
            "java_runtime.console_log.created main_class=%s path=%s",
            main_class or "-", self._current_file,
        )
        return self._current_file

    @property
    def path(self) -> str | None:
        return self._current_file

    def tail(self, n: int = 50) -> dict:
        """Return a bounded snapshot of the last N launch-log lines."""
        if not self._current_file:
            logger.warning("java_runtime.console_log.tail.failed reason=no_log_file")
            return {"error": "No log file created"}
        try:
            result = read_log_tail_snapshot(self._current_file, n)
            snapshot_size = result["snapshot_size_bytes"]
            previous_size = self._last_snapshot_size
            if previous_size is None:
                result["growth_state"] = "first_observation"
            elif snapshot_size < previous_size:
                result.update({
                    "growth_state": "truncated_or_replaced",
                    "previous_snapshot_size_bytes": previous_size,
                    "new_bytes_since_previous_read": 0,
                })
            else:
                result.update({
                    "growth_state": (
                        "appended"
                        if snapshot_size > previous_size
                        else "unchanged"
                    ),
                    "previous_snapshot_size_bytes": previous_size,
                    "new_bytes_since_previous_read": (
                        snapshot_size - previous_size
                    ),
                })
            self._last_snapshot_size = snapshot_size
            logger.info(
                "java_runtime.console_log.tail path=%s requested_lines=%s "
                "returned_lines=%s snapshot_size_bytes=%s scanned_bytes=%s "
                "growth_state=%s truncated=%s",
                self._current_file,
                n,
                result["returned_lines"],
                snapshot_size,
                result["scanned_bytes"],
                result["growth_state"],
                result["truncated"],
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
