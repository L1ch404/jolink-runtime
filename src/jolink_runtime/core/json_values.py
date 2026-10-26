"""Explicit JSON-facing representations for values JSON cannot express directly."""

from __future__ import annotations

import json
import math
import re
from typing import Any


_SURROGATE = re.compile("[\ud800-\udfff]")


def json_values(value: Any) -> Any:
    """Keep text and structured MCP output identical, including Java edge values.

    JSON has no NaN/infinities. Java type metadata still identifies these numeric
    values when they are rendered using Java's spellings. Java Strings/chars may
    contain isolated UTF-16 code units, which the MCP UTF-8 serializer rejects;
    use an explicitly tagged, reversible JSON string escape representation.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, str) and _SURROGATE.search(value):
        return {"_kind": "utf16_string", "escaped": json.dumps(value, ensure_ascii=True)[1:-1]}
    if isinstance(value, dict):
        return {key: json_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_values(item) for item in value]
    return value
