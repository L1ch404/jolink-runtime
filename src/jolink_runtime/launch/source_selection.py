"""Ant-style source selection, applied before package layout mapping."""

import re


def ant_pattern(pattern):
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
    return re.compile(expression)


class SourceSelection:
    def __init__(self, patterns):
        self.includes = tuple(ant_pattern(p) for p in patterns.get("includes", ()))
        self.excludes = tuple(ant_pattern(p) for p in patterns.get("excludes", ()))

    def accepts(self, relative):
        return (not self.includes or any(p.fullmatch(relative) for p in self.includes)) and not any(
            p.fullmatch(relative) for p in self.excludes
        )
