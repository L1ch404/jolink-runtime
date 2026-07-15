"""Offline golden contracts for the Hermes Runtime 2.4.0 lineage.

Unlike the migration differential suite, these tests never import Hermes and
therefore run in a clean CI checkout after the source repository is gone.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from jolink_runtime_debugger.adapters.java import process_discovery
from jolink_runtime_debugger.adapters.java.tool_schema import (
    JAVA_PROCESSES_SCHEMA,
    JAVA_RUNTIME_SCHEMA,
)
from jolink_runtime_debugger.core.dispatcher import Dispatcher, parse_runtime_action


FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "runtime-lineage-2.4.0"
)
REQUIRED_FILES = {
    "handler_results.json",
    "java_processes_results.json",
    "java_processes_schema.json",
    "java_runtime_schema.json",
    "metadata.json",
    "runtime_action_parsing.json",
}

EXPECTED_HERMES_COMMIT = "cc726310c7d9d7981ef3f0bf9e2d27513d0c9515"
EXPECTED_METADATA_SHA256 = (
    "95979bdd52e10139fbbd3fcb125b7a068ca7698c64f7f659b8e5de267a154bff"
)
EXPECTED_SCHEMA_SHA256 = {
    "JAVA_RUNTIME_SCHEMA": (
        "264b4899a8bcec75bca2f0ce38e21999ed8356c4e5ed9af325f1dc125f44af54"
    ),
    "JAVA_PROCESSES_SCHEMA": (
        "0c3739a5a920eab41d5d9d7fe48a1be452de2342a40a2ab474119c4e55b8fbac"
    ),
}
EXPECTED_SOURCE_SHA256 = {
    "plugins/jdwp-debug/__init__.py": (
        "2e3d0ddc6a3341d8d8c49f3918bd339e6b1864821791c59edf3e75af5035bc03"
    ),
    "plugins/java-monitor/__init__.py": (
        "297f0d318d8a021ac4328b59bd38b2ea8de8b65612506137ddad947d891fa86d"
    ),
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(payload)


def _load_json(filename: str) -> dict[str, Any]:
    path = FIXTURE_ROOT / filename
    assert path.is_file(), f"Required Runtime lineage fixture is missing: {path}"
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), f"Fixture root must be an object: {path}"
    return value


def test_lineage_metadata_and_all_artifact_hashes_are_pinned() -> None:
    assert FIXTURE_ROOT.is_dir(), f"Fixture directory is missing: {FIXTURE_ROOT}"
    actual_files = {path.name for path in FIXTURE_ROOT.iterdir() if path.is_file()}
    assert actual_files == REQUIRED_FILES

    metadata_path = FIXTURE_ROOT / "metadata.json"
    metadata_bytes = metadata_path.read_bytes()
    assert _sha256(metadata_bytes) == EXPECTED_METADATA_SHA256

    metadata = json.loads(metadata_bytes)
    assert metadata["format_version"] == 1
    assert metadata["runtime_lineage"] == "2.4.0"
    assert metadata["hermes_commit"] == EXPECTED_HERMES_COMMIT
    assert metadata["source_files"] == EXPECTED_SOURCE_SHA256
    assert set(metadata["artifacts"]) == REQUIRED_FILES - {"metadata.json"}

    for filename, expected_sha in metadata["artifacts"].items():
        artifact_path = FIXTURE_ROOT / filename
        assert artifact_path.is_file(), f"Metadata artifact is missing: {artifact_path}"
        assert _sha256(artifact_path.read_bytes()) == expected_sha


def test_golden_schemas_match_the_migrated_production_schemas() -> None:
    metadata = _load_json("metadata.json")
    schemas = {
        "JAVA_RUNTIME_SCHEMA": JAVA_RUNTIME_SCHEMA,
        "JAVA_PROCESSES_SCHEMA": JAVA_PROCESSES_SCHEMA,
    }

    for name, production_schema in schemas.items():
        schema_metadata = metadata["schemas"][name]
        golden_schema = _load_json(schema_metadata["artifact"])
        assert schema_metadata["canonical_sha256"] == EXPECTED_SCHEMA_SHA256[name]
        assert _canonical_sha256(golden_schema) == EXPECTED_SCHEMA_SHA256[name]
        assert _canonical_sha256(production_schema) == EXPECTED_SCHEMA_SHA256[name]
        assert production_schema == golden_schema


def test_dispatcher_results_match_the_old_handler_golden_results() -> None:
    golden = _load_json("handler_results.json")

    for case in golden["cases"]:
        result = Dispatcher().dispatch(
            "java_runtime",
            case["arguments"],
            session_key=f"golden-{case['name']}",
        )
        assert result == case["result"], case["name"]


def test_runtime_action_parsing_matches_the_old_handler_golden_results() -> None:
    golden = _load_json("runtime_action_parsing.json")

    for case in golden["cases"]:
        parsed = asdict(parse_runtime_action(case["arguments"]))
        assert parsed == case["parsed"], case["name"]
        assert set(parsed) == set(case["parsed"]), case["name"]


def test_java_processes_matches_the_old_handler_golden_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    golden = _load_json("java_processes_results.json")
    source_processes = golden["source_processes"]

    for case in golden["cases"]:
        observed_full: list[bool] = []

        def fake_run_jps(*, full: bool) -> list[dict[str, Any]]:
            observed_full.append(full)
            return [dict(process) for process in source_processes]

        monkeypatch.setattr(process_discovery, "_run_jps", fake_run_jps)
        result = Dispatcher().dispatch("java_processes", case["arguments"])

        assert observed_full == case["observed_full"], case["name"]
        assert result == case["result"], case["name"]
