from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

from jolink_runtime.adapters.java.jdwp_adapter import (
    JavaRuntime,
    SuspensionSnapshot,
)
from jolink_runtime.adapters.java.jdwp_client import EventKind
from jolink_runtime.core.models import RuntimeAction
from jolink_runtime.launch.fast_compile import (
    FastCompilePlan,
    fast_compile_fingerprint,
)


def _plan(tmp_path: Path) -> tuple[FastCompilePlan, Path, Path]:
    source_root = tmp_path / "src" / "main" / "java"
    source = source_root / "example" / "Example.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        "package example; public class Example {}\n",
        encoding="utf-8",
    )
    output_root = tmp_path / "target" / "classes"
    output_root.mkdir(parents=True)
    config = tmp_path / "pom.xml"
    config.write_text("<project/>\n", encoding="utf-8")
    javac_identity = Path(sys.executable)
    fingerprint = fast_compile_fingerprint(
        configuration_inputs=(config,),
        javac_executable=javac_identity,
        compile_classpath=(output_root,),
    )
    return (
        FastCompilePlan(
            project_root=tmp_path,
            module_root=tmp_path,
            source_root=source_root,
            output_root=output_root,
            javac_executable=javac_identity,
            compile_classpath=(output_root,),
            build_jdk_major=8,
            runtime_jdk_major=8,
            source_level=8,
            target_level=8,
            release_level=None,
            javac_platform_args=("-source", "8", "-target", "8"),
            configuration_inputs=(config,),
            configuration_fingerprint=fingerprint,
        ),
        source,
        config,
    )


def _action(source: Path, project_root: Path) -> RuntimeAction:
    action = RuntimeAction(action="update")
    action.source_files = [source.relative_to(project_root).as_posix()]
    return action


def test_update_requires_an_active_project_launch() -> None:
    runtime = JavaRuntime()

    action = RuntimeAction(action="update")
    action.source_files = ["src/main/java/example/Example.java"]
    result = runtime.update(action)

    assert result.ok is False
    assert result.data["error_code"] == "FAST_COMPILE_UNSUPPORTED"
    assert result.data["runtime_code_state"] == "unchanged"


def test_update_rejects_a_stale_cached_compile_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = JavaRuntime()
    plan, source, config = _plan(tmp_path)
    prepared = SimpleNamespace(
        fast_compile_plan=plan,
        attempt_directory=tmp_path,
    )
    monkeypatch.setattr(
        runtime,
        "_project_update_context",
        lambda: ("launch_1", 1, prepared),
    )
    config.write_text("<project><changed/></project>\n", encoding="utf-8")

    result = runtime.update(_action(source, tmp_path))

    assert result.ok is False
    assert result.data["error_code"] == "FAST_COMPILE_PLAN_STALE"
    assert result.data["runtime_code_state"] == "unchanged"


def test_update_rejects_an_active_debug_suspension_before_compiling(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = JavaRuntime()
    plan, source, _config = _plan(tmp_path)
    prepared = SimpleNamespace(
        fast_compile_plan=plan,
        attempt_directory=tmp_path,
    )
    monkeypatch.setattr(
        runtime,
        "_project_update_context",
        lambda: ("launch_1", 1, prepared),
    )
    runtime._active_suspension = SuspensionSnapshot(
        suspension_id="susp_1",
        generation=1,
        request_id=1,
        thread_id=2,
        location={},
        observed_at="now",
    )

    result = runtime.update(_action(source, tmp_path))

    assert result.ok is False
    assert result.data["error_code"] == "ACTIVE_SUSPENSION_EXISTS"
    assert result.data["suspension_id"] == "susp_1"
    assert result.data["runtime_code_state"] == "unchanged"


def test_update_rejects_live_debug_requests_before_compiling(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = JavaRuntime()
    plan, source, _config = _plan(tmp_path)
    prepared = SimpleNamespace(
        fast_compile_plan=plan,
        attempt_directory=tmp_path,
    )
    monkeypatch.setattr(
        runtime,
        "_project_update_context",
        lambda: ("launch_1", 1, prepared),
    )
    runtime._armed_breakpoint_requests[17] = "bp_001"

    result = runtime.update(_action(source, tmp_path))

    assert result.ok is False
    assert result.data["error_code"] == "ACTIVE_DEBUG_REQUESTS_REMAIN"
    assert result.data["runtime_code_state"] == "unchanged"


def test_update_rejects_an_unknown_previous_hotswap_outcome(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = JavaRuntime()
    plan, source, _config = _plan(tmp_path)
    prepared = SimpleNamespace(
        fast_compile_plan=plan,
        attempt_directory=tmp_path,
    )
    monkeypatch.setattr(
        runtime,
        "_project_update_context",
        lambda: ("launch_1", 1, prepared),
    )
    runtime._runtime_overlay_state = "unknown"
    runtime._runtime_overlay_sources.add(
        "src/main/java/example/Example.java"
    )

    result = runtime.update(_action(source, tmp_path))

    assert result.ok is False
    assert result.data["error_code"] == "HOT_SWAP_OUTCOME_UNKNOWN"
    assert result.data["runtime_code_state"] == "unknown"
    assert result.data["restart_required"] is True
    assert result.data["verification_state"] == "unknown"


def test_formal_output_guard_detects_external_build_change(
    tmp_path: Path,
) -> None:
    runtime = JavaRuntime()
    plan, _source, _config = _plan(tmp_path)
    class_file = plan.output_root / "example" / "Example.class"
    class_file.parent.mkdir(parents=True)
    class_file.write_bytes(b"launch-bytes")
    baseline_hash = hashlib.sha256(b"launch-bytes").hexdigest()
    plan.baseline_class_hashes["example/Example.class"] = baseline_hash

    assert runtime._formal_outputs_match_launch(plan) is True
    class_file.write_bytes(b"external-build-bytes")
    assert runtime._formal_outputs_match_launch(plan) is False


def test_formal_output_guard_checks_unselected_classes_and_class_set(
    tmp_path: Path,
) -> None:
    runtime = JavaRuntime()
    plan, _source, _config = _plan(tmp_path)
    selected = plan.output_root / "example" / "Example.class"
    dependency = plan.output_root / "example" / "Dependency.class"
    selected.parent.mkdir(parents=True)
    selected.write_bytes(b"selected-launch-bytes")
    dependency.write_bytes(b"dependency-launch-bytes")
    plan.baseline_class_hashes.update(
        {
            "example/Example.class": hashlib.sha256(
                b"selected-launch-bytes"
            ).hexdigest(),
            "example/Dependency.class": hashlib.sha256(
                b"dependency-launch-bytes"
            ).hexdigest(),
        }
    )

    assert runtime._formal_outputs_match_launch(plan) is True
    dependency.write_bytes(b"dependency-external-build")
    assert runtime._formal_outputs_match_launch(plan) is False
    dependency.write_bytes(b"dependency-launch-bytes")
    (plan.output_root / "example" / "Added.class").write_bytes(b"added")
    assert runtime._formal_outputs_match_launch(plan) is False


def test_redefined_class_breakpoints_become_stale_without_line_refresh() -> None:
    runtime = JavaRuntime()
    updated = {
        "breakpoint_id": "bp_001",
        "class": "Lexample/Updated;",
        "matched_class": "example.Updated",
        "source_file": "Updated.java",
        "method": "run",
        "method_signature": "()V",
        "line": 10,
        "code_index": 4,
    }
    untouched = {
        "breakpoint_id": "bp_002",
        "class": "Lexample/Untouched;",
        "matched_class": "example.Untouched",
        "source_file": "Untouched.java",
        "method": "run",
        "method_signature": "()V",
        "line": 20,
        "code_index": 8,
    }
    runtime._breakpoints = {
        "bp_001": updated,
        "bp_002": untouched,
    }

    result = runtime._refresh_updated_breakpoints(
        object(),
        {"Lexample/Updated;"},
    )

    assert result == {
        "state": "partial",
        "refreshed": [],
        "stale": ["bp_001"],
        "newly_stale": ["bp_001"],
        "reason": "CLASS_REDEFINED_BREAKPOINT_REQUIRES_RESET",
        "warnings": [
            "Stale logical breakpoints remain after class redefinition; "
            "remove and set the listed breakpoint ids again against the "
            "current source before arming another breakpoint wait."
        ],
    }
    assert updated["stale"] is True
    assert (
        updated["stale_reason"]
        == "CLASS_REDEFINED_BREAKPOINT_REQUIRES_RESET"
    )
    assert "redefined" in updated["refresh_error"]
    assert untouched == {
        "breakpoint_id": "bp_002",
        "class": "Lexample/Untouched;",
        "matched_class": "example.Untouched",
        "source_file": "Untouched.java",
        "method": "run",
        "method_signature": "()V",
        "line": 20,
        "code_index": 8,
    }


def test_stale_redefined_breakpoint_is_listed_and_cannot_be_armed() -> None:
    runtime = JavaRuntime()
    runtime._breakpoints = {
        "bp_001": {
            "breakpoint_id": "bp_001",
            "class": "Lexample/Untouched;",
            "matched_class": "example.Untouched",
            "source_file": "Untouched.java",
            "method": "run",
            "method_signature": "()V",
            "line": 5,
            "code_index": 2,
        },
        "bp_002": {
            "breakpoint_id": "bp_002",
            "class": "Lexample/Updated;",
            "matched_class": "example.Updated",
            "source_file": "Updated.java",
            "method": "run",
            "method_signature": "()V",
            "line": 10,
            "code_index": 4,
            "stale": True,
            "stale_reason": (
                "CLASS_REDEFINED_BREAKPOINT_REQUIRES_RESET"
            ),
        },
    }
    runtime._resolve_breakpoint_class = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(
            AssertionError("stale preflight must happen before JDWP resolution")
        )
    )

    observation = runtime._breakpoint_observations()[1]
    result = runtime._arm_debug_requests(
        object(),
        {EventKind.BREAKPOINT},
        None,
    )

    assert observation["stale"] is True
    assert (
        observation["stale_reason"]
        == "CLASS_REDEFINED_BREAKPOINT_REQUIRES_RESET"
    )
    assert result is not None
    assert result.ok is False
    assert result.data["error_code"] == "BREAKPOINT_DEFINITION_STALE"
    assert result.data["breakpoint_id"] == "bp_002"
    assert result.data["stale_breakpoint_ids"] == ["bp_002"]
    assert (
        result.data["stale_reason"]
        == "CLASS_REDEFINED_BREAKPOINT_REQUIRES_RESET"
    )
    assert runtime._armed_breakpoint_requests == {}


def test_breakpoint_observation_omits_stale_fields_for_active_definition() -> None:
    runtime = JavaRuntime()
    runtime._breakpoints = {
        "bp_001": {
            "breakpoint_id": "bp_001",
            "class": "Lexample/Active;",
            "matched_class": "example.Active",
            "source_file": "Active.java",
            "method": "run",
            "method_signature": "()V",
            "line": 10,
            "code_index": 4,
        }
    }

    observation = runtime._breakpoint_observations()[0]

    assert "stale" not in observation
    assert "stale_reason" not in observation


def test_later_update_keeps_prior_stale_breakpoint_visible() -> None:
    runtime = JavaRuntime()
    runtime._breakpoints = {
        "bp_001": {
            "breakpoint_id": "bp_001",
            "class": "Lexample/First;",
            "matched_class": "example.First",
            "line": 10,
        },
        "bp_002": {
            "breakpoint_id": "bp_002",
            "class": "Lexample/Second;",
            "matched_class": "example.Second",
            "line": 20,
        },
    }

    first = runtime._refresh_updated_breakpoints(
        object(),
        {"Lexample/First;"},
    )
    second = runtime._refresh_updated_breakpoints(
        object(),
        {"Lexample/Unrelated;"},
    )

    assert first["stale"] == ["bp_001"]
    assert first["newly_stale"] == ["bp_001"]
    assert second["state"] == "partial"
    assert second["stale"] == ["bp_001"]
    assert second["newly_stale"] == []


def test_stale_arm_error_reports_every_stale_breakpoint() -> None:
    runtime = JavaRuntime()
    runtime._breakpoints = {
        "bp_001": {
            "breakpoint_id": "bp_001",
            "class": "Lexample/First;",
            "matched_class": "example.First",
            "line": 10,
            "stale": True,
        },
        "bp_002": {
            "breakpoint_id": "bp_002",
            "class": "Lexample/Active;",
            "matched_class": "example.Active",
            "line": 20,
        },
        "bp_003": {
            "breakpoint_id": "bp_003",
            "class": "Lexample/Third;",
            "matched_class": "example.Third",
            "line": 30,
            "stale": True,
        },
    }

    result = runtime._arm_debug_requests(
        object(),
        {EventKind.BREAKPOINT},
        None,
    )

    assert result is not None
    assert result.ok is False
    assert result.data["stale_breakpoint_ids"] == [
        "bp_001",
        "bp_003",
    ]
    assert "partial breakpoint arming" in result.data[
        "suggested_next_step"
    ]
