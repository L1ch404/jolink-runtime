from __future__ import annotations

import json
import math
import random
import struct

import mcp.types as types
import pytest

from jolink_runtime.adapters.java.evaluation_vm import Reader
from jolink_runtime.adapters.java.jdwp_strings import decode_jdwp_string
from jolink_runtime.core.json_values import json_values
from jolink_runtime.core.models import RuntimeResult
from jolink_runtime.server.mcp_server import _call_tool_result


@pytest.mark.parametrize("value,expected", [
    (float("nan"), "NaN"), (float("inf"), "Infinity"), (float("-inf"), "-Infinity"),
    (0.0, 0.0), (-0.0, -0.0), (1.25, 1.25), (1.7976931348623157e308, 1.7976931348623157e308),
    (5e-324, 5e-324), (None, None), ("中文😀", "中文😀"), ("a\0b", "a\0b"),
    (r"\ud83d", r"\ud83d"),
    ("a\ud83db", {"_kind": "utf16_string", "escaped": "a\\ud83db"}),
    ("\ude00", {"_kind": "utf16_string", "escaped": "\\ude00"}),
])
def test_runtime_and_mcp_preserve_same_json_value(value, expected):
    payload = {"ok": True, "result": {"type": "example", "value": value}, "nested": [value]}
    runtime = json.loads(RuntimeResult(data=payload).to_json())
    result = _call_tool_result(payload)
    # Exercise the actual MCP Pydantic serializer, not only Python json.dumps:
    # previously it silently encoded non-finite numbers as null, and crashed on
    # isolated UTF-16 units even when the text field had JSON escapes.
    delivered = types.CallToolResult.model_validate_json(result.model_dump_json())
    assert json.loads(delivered.content[0].text) == delivered.structuredContent == runtime
    assert runtime["result"]["value"] == expected
    assert runtime["nested"] == [expected]
    json.dumps(runtime, allow_nan=False).encode("utf-8")
    if isinstance(expected, float) and expected == 0:
        assert math.copysign(1, runtime["result"]["value"]) == math.copysign(1, expected)
    # Normalization is idempotent and must not mutate an adapter's live payload.
    assert json_values(runtime) == runtime
    assert payload["result"]["value"] is value


def test_seeded_utf16_values_roundtrip_without_replacement_or_truncation():
    generator = random.Random(20261026)
    for _ in range(100):
        units = [generator.randrange(65536) for _ in range(16)]
        units[generator.randrange(len(units))] = generator.choice([0xD800, 0xDBFF, 0xDC00, 0xDFFF])
        raw = b"".join(struct.pack(">H", unit) for unit in units)
        java_string = raw.decode("utf-16-be", errors="surrogatepass")
        result = _call_tool_result({"ok": True, "value": java_string})
        delivered = types.CallToolResult.model_validate_json(result.model_dump_json())
        displayed = delivered.structuredContent["value"]
        restored = json.loads('"' + displayed["escaped"] + '"') if isinstance(displayed, dict) else displayed
        assert restored.encode("utf-16-be", errors="surrogatepass") == raw
        assert json.loads(delivered.content[0].text) == delivered.structuredContent


@pytest.mark.parametrize("raw,expected", [
    (b"normal\x00text", "normal\0text"),
    ("中文😀".encode("utf-8"), "中文😀"),
    (b"\xed\xa0\xbd", "\ud83d"),
    (b"\xed\xb8\x80", "\ude00"),
    (b"\xed\xa0\xbd\xed\xb8\x80", "😀"),
])
def test_jdwp_and_evaluation_readers_preserve_java_string_units(raw, expected):
    assert decode_jdwp_string(raw) == expected
    assert Reader(struct.pack(">I", len(raw)) + raw).string() == expected


def test_invalid_jdwp_bytes_are_not_hidden_as_replacement_characters():
    with pytest.raises(UnicodeDecodeError):
        decode_jdwp_string(b"\xff")
