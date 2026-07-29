from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

from jolink_runtime.adapters.java.jdwp_adapter import (
    JavaRuntime,
    SuspensionSnapshot,
)
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
