"""Migration-only differential contracts against the Hermes-era handlers."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from jolink_runtime.adapters.java import process_discovery
from jolink_runtime.core.dispatcher import Dispatcher
from jolink_runtime.core.models import RuntimeResult
from jolink_runtime.core.session_manager import SessionManager


def _hermes_root() -> Path:
    configured = os.environ.get("HERMES_SOURCE_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[3] / "hermes-agent"


def _load_source_module(
    module_name: str,
    source_path: Path,
    *,
    package_root: Path | None = None,
) -> ModuleType:
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        module_name,
        source_path,
        submodule_search_locations=[str(package_root)] if package_root else None,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def old_runtime_plugin() -> ModuleType:
    plugin_root = _hermes_root() / "plugins" / "jdwp-debug"
    source_path = plugin_root / "__init__.py"
    if not source_path.is_file():
        pytest.skip("Hermes source checkout is unavailable for migration comparison")
    return _load_source_module(
        "jolink_migration_reference_jdwp_debug",
        source_path,
        package_root=plugin_root,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "status"},
        {"action": "breakpoint", "bp_action": "set"},
        {
            "action": "exception",
            "exception_action": "set",
            "exception_class": "java.lang.Exception",
        },
        {"action": "variables"},
        {"action": "resume"},
        {"action": "does_not_exist"},
    ],
)
def test_java_runtime_results_match_old_handler(
    old_runtime_plugin: ModuleType,
    arguments: dict[str, Any],
) -> None:
    context = f"contract-{arguments['action']}"
    old_runtime_plugin._runtimes.pop(context, None)

    old_result = json.loads(
        old_runtime_plugin._handle_java_runtime(arguments, session_id=context)
    )
    new_result = Dispatcher().dispatch(
        "java_runtime",
        arguments,
        session_key=context,
    )

    assert new_result == old_result


class _RecordingRuntime:
    def __init__(self) -> None:
        self.actions: list[Any] = []

    def _record(self, action: Any) -> RuntimeResult:
        self.actions.append(action)
        return RuntimeResult(data={"status": "recorded", "action": action.action})

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


def test_runtime_action_parsing_matches_old_handler(
    old_runtime_plugin: ModuleType,
) -> None:
    arguments = {
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
    context = "contract-arguments"
    old_runtime = _RecordingRuntime()
    new_runtime = _RecordingRuntime()
    old_runtime_plugin._runtimes[context] = old_runtime
    dispatcher = Dispatcher(SessionManager(lambda: new_runtime))

    old_result = json.loads(
        old_runtime_plugin._handle_java_runtime(arguments, session_id=context)
    )
    new_result = dispatcher.dispatch(
        "java_runtime",
        arguments,
        session_key=context,
    )

    assert new_result == old_result
    assert vars(new_runtime.actions[0]) == vars(old_runtime.actions[0])


def test_java_processes_result_matches_old_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = _hermes_root() / "plugins" / "java-monitor" / "__init__.py"
    if not source_path.is_file():
        pytest.skip("Hermes source checkout is unavailable for migration comparison")
    old_process_plugin = _load_source_module(
        "jolink_migration_reference_java_monitor",
        source_path,
    )
    processes = [
        {
            "pid": 123,
            "main_class": "com.example.DemoApplication",
            "runtime": "OpenJDK 8",
        },
        {"pid": 456, "main_class": "worker.jar", "runtime": "OpenJDK 8"},
    ]
    monkeypatch.setattr(old_process_plugin, "_run_jps", lambda *, full: processes)
    monkeypatch.setattr(process_discovery, "_run_jps", lambda *, full: processes)
    arguments = {"filter": "Demo", "full": True}

    old_result = json.loads(old_process_plugin._handle_java_processes(arguments))
    new_result = Dispatcher().dispatch("java_processes", arguments)

    assert new_result == old_result
