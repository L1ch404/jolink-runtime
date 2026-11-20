"""Bounded diagnostic output from native build tools."""
import locale
import re
from pathlib import Path

_SECRET = re.compile(
    r"(?i)(password|passwd|token|secret|authorization|cookie|credential|"
    r"api[_-]?key|access[_-]?key|private[_-]?key)"
)
_USERINFO = re.compile(r"(://)([^/@\s]+)@")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def redacted_build_log_tail(path: Path) -> list[str]:
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, stream.seek(0, 2) - 32 * 1024))
            raw = stream.read(32 * 1024)
    except OSError:
        return []
    text = raw.decode(locale.getencoding(), errors="replace")
    lines = (_ANSI.sub("", line) for line in text.splitlines()[-80:])
    return ["<redacted sensitive build log line>" if _SECRET.search(line)
            else _USERINFO.sub(r"\1<redacted>@", line) for line in lines]
