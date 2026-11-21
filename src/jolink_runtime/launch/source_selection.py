"""Ant-style source selection, applied before package layout mapping."""

import os
from pathlib import Path
import re
from collections.abc import Iterator


def ant_pattern(pattern, flags=0):
    value = pattern.replace("\\", "/")
    if value.endswith("/"):
        value += "**"
    parts = value.split("/")
    expression = ""
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            expression += ".*" if last else "(?:[^/]+/)*"
        else:
            expression += "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in part)
            if not last:
                expression += "/"
    return re.compile(expression, flags)


class SourceSelection:
    def __init__(self, patterns):
        flags = 0 if patterns.get("caseSensitive", True) else re.IGNORECASE
        self.includes = tuple(ant_pattern(p, flags) for p in patterns.get("includes", ()))
        self.also_includes = tuple(ant_pattern(p, flags) for p in patterns.get("also_includes", ()))
        self.excludes = tuple(ant_pattern(p, flags) for p in patterns.get("excludes", ()))

    def accepts(self, relative):
        return (not self.includes or any(p.fullmatch(relative) for p in self.includes)) and (
            not self.also_includes or any(p.fullmatch(relative) for p in self.also_includes)
        ) and not any(p.fullmatch(relative) for p in self.excludes)


def selected_files(root: Path, spec: dict, excluded=()) -> Iterator[Path]:
    selection = SourceSelection(spec)
    includes = spec.get("includes", ())
    # Common root-scoped CopySpecs name individual files, e.g. from(projectDir)
    # { include "LICENSE" }. Do not enumerate the entire repository for them.
    if includes and spec.get("caseSensitive", True) and all(
        not any(c in pattern for c in "*?\\") and not pattern.endswith("/")
        and not Path(pattern).is_absolute() and ".." not in Path(pattern).parts for pattern in includes
    ):
        for pattern in includes:
            path = root / pattern
            if path.is_file() and selection.accepts(pattern) and not any(path.is_relative_to(out) for out in excluded):
                yield path
    elif root.is_file():
        if selection.accepts("."):
            yield root
    else:
        top_level_only = includes and all("/" not in p and "\\" not in p and p != "**" for p in includes)
        for folder, dirs, files in os.walk(root):
            folder = Path(folder)
            dirs[:] = [] if top_level_only else [d for d in dirs if folder / d not in excluded]
            for name in files:
                path = folder / name
                if path not in excluded and selection.accepts(path.relative_to(root).as_posix()) and path.is_file():
                    yield path
