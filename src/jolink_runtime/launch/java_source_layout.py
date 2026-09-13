"""Read only the Java header needed to place a source in a JavaBuilder project."""

from __future__ import annotations

import re
from pathlib import Path

_UNICODE_ESCAPE = re.compile(r"\\u+([0-9a-fA-F]{4})")
_TOKEN = re.compile(
    r"\s+|//[^\r\n]*|/\*[\s\S]*?(?:\*/|$)"
    r'|"(?:\\[\s\S]|[^"\\])*"|\'(?:\\[\s\S]|[^\'\\])*\''
    r"|[\w$]+|.",
    re.DOTALL,
)


def _translate_unicode(text: str) -> str:
    """Java translates eligible Unicode escapes before recognizing comments."""
    if "\\u" not in text:
        return text
    result = []
    end = 0
    for match in _UNICODE_ESCAPE.finditer(text):
        backslashes = 0
        cursor = match.start() - 1
        while cursor >= end and text[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2:
            continue
        result.append(text[end : match.start()])
        result.append(chr(int(match[1], 16)))
        end = match.end()
    result.append(text[end:])
    return "".join(result)


def source_relative_path(content: bytes, filename: str, encoding: str) -> Path | None:
    """Return declaration-based placement; leave malformed headers to JDT.

    This is not an AST parse or a type resolver. Stop at the first import/type,
    skipping comments, literals and package annotations. Original bytes stay
    untouched, including encoding and line numbers.
    """
    text = _translate_unicode(content.decode(encoding, errors="replace"))
    tokens = (
        match[0]
        for match in _TOKEN.finditer(text)
        if not match[0].isspace() and not match[0].startswith(("//", "/*"))
    )
    depth = 0
    for token in tokens:
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0 and token == "package":
            parts = []
            identifier = True
            for part in tokens:
                if identifier:
                    if not re.fullmatch(r"[^\W\d][\w$]*|\$[\w$]*", part):
                        return None
                    parts.append(part)
                elif part == ";":
                    return Path(*parts, filename)
                elif part != ".":
                    return None
                identifier = not identifier
            return None
        elif depth == 0 and token in {"import", "class", "interface", "enum", "record", "module"}:
            return Path(filename)
    return Path(filename)
