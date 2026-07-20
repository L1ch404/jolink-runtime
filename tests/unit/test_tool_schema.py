from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from jolink_runtime.adapters.java.tool_schema import (
    JAVA_PROCESSES_SCHEMA,
    JAVA_RUNTIME_SCHEMA,
)


RUNTIME_SCHEMA_SHA256 = "264b4899a8bcec75bca2f0ce38e21999ed8356c4e5ed9af325f1dc125f44af54"
PROCESSES_SCHEMA_SHA256 = "0c3739a5a920eab41d5d9d7fe48a1be452de2342a40a2ab474119c4e55b8fbac"


def _canonical_json(schema: dict[str, Any]) -> bytes:
    return json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _schema_from_source(path: Path, variable_name: str) -> dict[str, Any]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == variable_name
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            assert isinstance(value, dict)
            return value
    raise AssertionError(f"{variable_name} was not found in {path}")


def test_runtime_schema_preserves_actions_and_parameters() -> None:
    parameters = JAVA_RUNTIME_SCHEMA["parameters"]
    properties = parameters["properties"]

    assert JAVA_RUNTIME_SCHEMA["name"] == "java_runtime"
    assert parameters["type"] == "object"
    assert parameters["required"] == ["action"]
    assert properties["action"]["enum"] == [
        "run",
        "stop",
        "restart",
        "attach",
        "detach",
        "status",
        "logs",
        "breakpoint",
        "exception",
        "wait_event",
        "wait_breakpoint",
        "threads",
        "stack",
        "variables",
        "resume",
        "cleanup_debug_state",
    ]
    assert list(properties) == [
        "action",
        "classpath",
        "main_class",
        "jar_path",
        "app_args",
        "jdwp_port",
        "pid",
        "host",
        "vm_args",
        "tail",
        "bp_action",
        "breakpoint_id",
        "request_id",
        "exception_action",
        "exception_class",
        "caught",
        "uncaught",
        "allow_broad_caught",
        "class_pattern",
        "include_proxy",
        "include_generated",
        "line",
        "thread_name",
        "frame_index",
        "max_frames",
        "include_this",
        "max_value_depth",
        "semantic_collections",
        "item_limit",
        "map_entry_limit",
        "timeout",
        "suspension_id",
    ]
    assert properties["bp_action"]["enum"] == ["set", "remove", "list"]
    assert properties["exception_action"]["enum"] == ["set", "remove", "list"]
    assert properties["semantic_collections"]["default"] is True
    assert properties["item_limit"]["default"] == 16
    assert properties["map_entry_limit"]["default"] == 16


def test_processes_schema_preserves_input_and_output_contract() -> None:
    parameters = JAVA_PROCESSES_SCHEMA["parameters"]
    output = JAVA_PROCESSES_SCHEMA["output"]

    assert JAVA_PROCESSES_SCHEMA["name"] == "java_processes"
    assert parameters["required"] == []
    assert list(parameters["properties"]) == ["filter", "full"]
    assert parameters["properties"]["full"]["default"] is False
    assert output["required"] == ["message", "count", "processes"]
    assert output["properties"]["processes"]["items"]["required"] == [
        "pid",
        "main_class",
        "runtime",
    ]


def test_schema_serialization_matches_migrated_snapshot() -> None:
    runtime_json = _canonical_json(JAVA_RUNTIME_SCHEMA)
    processes_json = _canonical_json(JAVA_PROCESSES_SCHEMA)

    assert hashlib.sha256(runtime_json).hexdigest() == RUNTIME_SCHEMA_SHA256
    assert hashlib.sha256(processes_json).hexdigest() == PROCESSES_SCHEMA_SHA256
    assert json.loads(runtime_json) == JAVA_RUNTIME_SCHEMA
    assert json.loads(processes_json) == JAVA_PROCESSES_SCHEMA


@pytest.mark.parametrize(
    ("relative_path", "variable_name", "migrated_schema"),
    [
        (
            "plugins/jdwp-debug/__init__.py",
            "JAVA_RUNTIME_SCHEMA",
            JAVA_RUNTIME_SCHEMA,
        ),
        (
            "plugins/java-monitor/__init__.py",
            "JAVA_PROCESSES_SCHEMA",
            JAVA_PROCESSES_SCHEMA,
        ),
    ],
)
def test_schema_deep_equals_hermes_source_when_available(
    relative_path: str,
    variable_name: str,
    migrated_schema: dict[str, Any],
) -> None:
    configured_root = os.environ.get("HERMES_SOURCE_ROOT")
    repository_root = Path(__file__).resolve().parents[2]
    hermes_root = (
        Path(configured_root).expanduser()
        if configured_root
        else repository_root.parent / "hermes-agent"
    )
    source_path = hermes_root / relative_path
    if not source_path.is_file():
        pytest.skip("Hermes source checkout is not available for differential verification")

    assert migrated_schema == _schema_from_source(source_path, variable_name)
