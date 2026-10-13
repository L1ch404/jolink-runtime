"""Bounded console output for a retained Fast Test attempt."""

from __future__ import annotations

import locale
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MAX_OUTPUT_BYTES = 16 * 1024
_MAX_SCAN_BYTES = 512 * 1024


@dataclass
class TestOutputLog:
    path: Path
    execution_id: str = ""
    # Fallback for JVM startup errors before its Runner reports the actual encoding.
    encoding: str = field(default_factory=locale.getencoding)


def read_test_output(logs: Sequence[TestOutputLog]) -> dict:
    if not logs:
        return {}
    # adapters.java exports JavaRuntime eagerly; avoid a launch -> adapter import cycle.
    from ..adapters.java.log_manager import read_log_tail_snapshot

    remaining_lines = 50
    remaining_bytes = _MAX_OUTPUT_BYTES
    remaining_scan = _MAX_SCAN_BYTES
    blocks: list[str] = []
    truncated = False
    unavailable = False
    for log in reversed(logs):
        try:
            if not log.path.stat().st_size:
                continue
            if min(remaining_lines, remaining_bytes, remaining_scan) <= 0:
                truncated = True
                continue
            snapshot = read_log_tail_snapshot(
                str(log.path),
                remaining_lines,
                encoding=log.encoding,
                max_scan_bytes=remaining_scan,
                max_return_bytes=remaining_bytes,
            )
        except FileNotFoundError:
            # A live group is registered before its log file is created.
            continue
        except OSError:
            unavailable = True
            continue
        lines = snapshot["lines"]
        text = _ANSI.sub("", "".join(lines))
        if len(logs) > 1 and text:
            text = f"[{log.execution_id or log.path.parent.name}]\n" + text
            if not text.endswith("\n"):
                text += "\n"
        blocks.append(text)
        remaining_lines -= len(lines)
        remaining_bytes -= len(text.encode("utf-8"))
        remaining_scan -= snapshot["scanned_bytes"]
        truncated |= snapshot["has_more_before"] or snapshot["truncated"]
        if snapshot["has_more_before"] or snapshot["truncated"]:
            break  # Do not fill spare bytes with older groups after dropping newer text.
    output = "".join(reversed(blocks))
    encoded = output.encode("utf-8")
    if len(encoded) > _MAX_OUTPUT_BYTES:
        output = encoded[-_MAX_OUTPUT_BYTES:].decode("utf-8", errors="ignore")
        truncated = True
    result = {
        "output": output,
        "output_truncated": truncated,
        "log_files": [str(log.path.absolute()) for log in logs],
    }
    if unavailable:
        result["output_unavailable"] = True
    return result
