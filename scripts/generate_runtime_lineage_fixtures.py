#!/usr/bin/env python3
"""Generate the immutable Runtime 2.4.0 migration fixtures from Hermes.

This is a maintainer-only provenance command. Normal tests consume the
checked-in fixtures and never import or require a Hermes checkout.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


LINEAGE = "2.4.0"
HERMES_COMMIT = "cc726310c7d9d7981ef3f0bf9e2d27513d0c9515"
SOURCE_FILES = {
    "plugins/jdwp-debug/__init__.py": (
        "2e3d0ddc6a3341d8d8c49f3918bd339e6b1864821791c59edf3e75af5035bc03"
    ),
    "plugins/java-monitor/__init__.py": (
        "297f0d318d8a021ac4328b59bd38b2ea8de8b65612506137ddad947d891fa86d"
    ),
}

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "tests" / "fixtures" / f"runtime-lineage-{LINEAGE}"


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


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_source_module(
    module_name: str,
    source_path: Path,
    *,
    package_root: Path | None = None,
) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        module_name,
        source_path,
        submodule_search_locations=[str(package_root)] if package_root else None,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load source module: {source_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _verify_source(hermes_root: Path) -> dict[str, str]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(hermes_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Unable to identify Hermes source commit: {exc}") from exc

    if commit != HERMES_COMMIT:
        raise SystemExit(
            f"Hermes source must be checked out at {HERMES_COMMIT}; found {commit}"
        )

    observed: dict[str, str] = {}
    for relative_path, expected_sha in SOURCE_FILES.items():
        source_path = hermes_root / relative_path
        if not source_path.is_file():
            raise SystemExit(f"Required Hermes source is missing: {source_path}")
        source_sha = _sha256(source_path.read_bytes())
        if source_sha != expected_sha:
            raise SystemExit(
                f"Hermes source differs from the pinned commit for {relative_path}: "
                f"expected {expected_sha}, found {source_sha}"
            )
        observed[relative_path] = source_sha
    return observed


class _ActionCaptureRuntime:
    """Capture the RuntimeAction built by the old handler."""

    def __init__(self, result_type: type[Any]) -> None:
        self._result_type = result_type
        self.actions: list[Any] = []

    def _record(self, action: Any) -> Any:
        self.actions.append(action)
        return self._result_type(data={"status": "recorded"})

    run = _record
    stop = _record
    restart = _record
    attach = _record
    detach = _record
    status = _record
    logs = _record
    breakpoint = _record
    exception = _record
    wait_event = _record
    wait_breakpoint = _record
    threads = _record
    stack = _record
    variables = _record
    resume = _record
    cleanup_debug_state = _record


def _runtime_result_type(runtime_plugin: ModuleType) -> type[Any]:
    return sys.modules[runtime_plugin.RuntimeAction.__module__].RuntimeResult


def _capture_handler_results(runtime_plugin: ModuleType) -> dict[str, Any]:
    cases = [
        {"name": "status_without_target", "arguments": {"action": "status"}},
        {
            "name": "breakpoint_without_target",
            "arguments": {"action": "breakpoint", "bp_action": "set"},
        },
        {
            "name": "exception_without_target",
            "arguments": {
                "action": "exception",
                "exception_action": "set",
                "exception_class": "java.lang.Exception",
            },
        },
        {
            "name": "variables_without_suspension",
            "arguments": {"action": "variables"},
        },
        {
            "name": "resume_without_suspension",
            "arguments": {"action": "resume"},
        },
        {"name": "unknown_action", "arguments": {"action": "does_not_exist"}},
    ]
    captured = []
    for case in cases:
        session_key = f"golden-handler-{case['name']}"
        runtime_plugin._runtimes.pop(session_key, None)
        result = json.loads(
            runtime_plugin._handle_java_runtime(
                case["arguments"],
                session_id=session_key,
            )
        )
        captured.append({**case, "result": result})
    return {"cases": captured}


def _capture_runtime_action(
    runtime_plugin: ModuleType,
    arguments: dict[str, Any],
    session_key: str,
) -> dict[str, Any]:
    runtime = _ActionCaptureRuntime(_runtime_result_type(runtime_plugin))
    runtime_plugin._runtimes[session_key] = runtime
    json.loads(
        runtime_plugin._handle_java_runtime(arguments, session_id=session_key)
    )
    if len(runtime.actions) != 1:
        raise RuntimeError(f"Expected one captured action, found {len(runtime.actions)}")
    return vars(runtime.actions[0])


def _capture_runtime_action_results(runtime_plugin: ModuleType) -> dict[str, Any]:
    fully_specified = {
        "action": "wait_event",
        "classpath": "classes",
        "main_class": "example.Main",
        "jar_path": "example.jar",
        "app_args": ["--profile=test"],
        "jdwp_port": 6123,
        "vm_args": ["-Xmx64m"],
        "pid": 123,
        "host": "localhost",
        "tail": 9,
        "bp_action": "list",
        "exception_action": "remove",
        "breakpoint_id": "bp_007",
        "request_id": 17,
        "class_pattern": "example.Service",
        "include_proxy": "yes",
        "include_generated": "on",
        "exception_class": "java.lang.NullPointerException",
        "caught": "false",
        "uncaught": "1",
        "allow_broad_caught": "true",
        "line": 42,
        "thread_name": "worker",
        "frame_index": 2,
        "max_frames": 7,
        "include_this": "y",
        "max_value_depth": "3",
        "semantic_collections": "false",
        "item_limit": "5",
        "map_entry_limit": "6",
        "timeout": "1.5",
        "suspension_id": "suspension-1",
    }
    cases = [
        {"name": "defaults", "arguments": {"action": "status"}},
        {"name": "fully_specified", "arguments": fully_specified},
    ]
    return {
        "cases": [
            {
                **case,
                "parsed": _capture_runtime_action(
                    runtime_plugin,
                    case["arguments"],
                    f"golden-action-{case['name']}",
                ),
            }
            for case in cases
        ]
    }


def _capture_java_process_results(process_plugin: ModuleType) -> dict[str, Any]:
    source_processes = [
        {
            "pid": 123,
            "main_class": "com.example.DemoApplication",
            "runtime": "OpenJDK 8",
            "jvm_args": "-Xmx256m",
        },
        {
            "pid": 456,
            "main_class": "worker.jar",
            "runtime": "OpenJDK 8",
            "jvm_args": "-Dfile.encoding=UTF-8",
        },
    ]
    cases = [
        {"name": "class_filter", "arguments": {"filter": "Demo", "full": True}},
        {"name": "pid_filter", "arguments": {"filter": "456", "full": "false"}},
        {"name": "no_match", "arguments": {"filter": "missing", "full": False}},
    ]
    original_run_jps = process_plugin._run_jps
    try:
        captured = []
        for case in cases:
            observed_full: list[bool] = []

            def fake_run_jps(*, full: bool) -> list[dict[str, Any]]:
                observed_full.append(full)
                return [dict(process) for process in source_processes]

            process_plugin._run_jps = fake_run_jps
            with contextlib.redirect_stdout(io.StringIO()):
                result = json.loads(
                    process_plugin._handle_java_processes(case["arguments"])
                )
            captured.append(
                {
                    **case,
                    "observed_full": observed_full,
                    "result": result,
                }
            )
    finally:
        process_plugin._run_jps = original_run_jps
    return {"source_processes": source_processes, "cases": captured}


def generate(hermes_root: Path, output: Path) -> None:
    hermes_root = hermes_root.resolve()
    source_hashes = _verify_source(hermes_root)

    runtime_root = hermes_root / "plugins" / "jdwp-debug"
    runtime_plugin = _load_source_module(
        "jolink_runtime_lineage_2_4_0_jdwp_debug",
        runtime_root / "__init__.py",
        package_root=runtime_root,
    )
    process_plugin = _load_source_module(
        "jolink_runtime_lineage_2_4_0_java_monitor",
        hermes_root / "plugins" / "java-monitor" / "__init__.py",
    )

    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        artifacts: dict[str, Any] = {
            "java_runtime_schema.json": runtime_plugin.JAVA_RUNTIME_SCHEMA,
            "java_processes_schema.json": process_plugin.JAVA_PROCESSES_SCHEMA,
            "handler_results.json": _capture_handler_results(runtime_plugin),
            "runtime_action_parsing.json": _capture_runtime_action_results(
                runtime_plugin
            ),
            "java_processes_results.json": _capture_java_process_results(
                process_plugin
            ),
        }
    finally:
        logging.disable(previous_logging_disable)

    output.mkdir(parents=True, exist_ok=True)
    for filename, value in artifacts.items():
        _write_json(output / filename, value)

    artifact_hashes = {
        filename: _sha256((output / filename).read_bytes())
        for filename in sorted(artifacts)
    }
    metadata = {
        "format_version": 1,
        "runtime_lineage": LINEAGE,
        "hermes_commit": HERMES_COMMIT,
        "source_files": source_hashes,
        "schemas": {
            "JAVA_RUNTIME_SCHEMA": {
                "artifact": "java_runtime_schema.json",
                "canonical_sha256": _canonical_sha256(
                    runtime_plugin.JAVA_RUNTIME_SCHEMA
                ),
            },
            "JAVA_PROCESSES_SCHEMA": {
                "artifact": "java_processes_schema.json",
                "canonical_sha256": _canonical_sha256(
                    process_plugin.JAVA_PROCESSES_SCHEMA
                ),
            },
        },
        "artifacts": artifact_hashes,
    }
    _write_json(output / "metadata.json", metadata)

    print(f"Generated Runtime lineage {LINEAGE} fixtures in {output}")
    print(f"Hermes commit: {HERMES_COMMIT}")
    for name, details in metadata["schemas"].items():
        print(f"{name}: {details['canonical_sha256']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hermes-source",
        required=True,
        type=Path,
        help="Hermes repository checked out at the pinned Runtime lineage commit",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Fixture output directory (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()
    generate(args.hermes_source, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
