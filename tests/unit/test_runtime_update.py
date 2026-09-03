from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jolink_runtime.adapters.java.classfile import parse_class_file
from jolink_runtime.adapters.java.jdwp_adapter import (
    JavaRuntime,
    ProjectUpdatePlan,
    SuspensionSnapshot,
)
from jolink_runtime.adapters.java.jdwp_client import EventKind
from jolink_runtime.core.models import RuntimeAction
from jolink_runtime.launch.controller import LaunchPipelineFailure
from jolink_runtime.launch.fast_compile import (
    FastCompileError,
    FastCompilePlan,
    fast_compile_fingerprint,
)
from jolink_runtime.launch.project_session import JavaProjectSession
from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate,
    JdtCompileError,
    JdtCompileResult,
    PersistentJdtCompileSession,
)


def _compile_class(tmp_path: Path, variant: str, source: str) -> bytes:
    javac = shutil.which("javac")
    if javac is None:
        pytest.skip("javac is required for runtime update comparison tests")
    root = tmp_path / variant
    output = root / "classes"
    output.mkdir(parents=True)
    source_file = root / "Example.java"
    source_file.write_text(source, encoding="utf-8")
    subprocess.run(
        [
            javac,
            "-encoding",
            "UTF-8",
            "-g",
            "-d",
            str(output),
            str(source_file),
        ],
        check=True,
        capture_output=True,
    )
    return (output / "Example.class").read_bytes()


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


def test_jdt_reload_hotswap_uses_persistent_workspace_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline_raw = _compile_class(
        tmp_path,
        "jdt-before",
        "public class Example { public int value() { return 1; } }",
    )
    staged_raw = _compile_class(
        tmp_path,
        "jdt-after",
        "public class Example { public int value() { return 2; } }",
    )
    project = tmp_path / "project"
    source = project / "src/main/java/Example.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        "public class Example { public int value() { return 2; } }",
        encoding="utf-8",
    )
    initial = tmp_path / "initial"
    initial.mkdir()
    (initial / "Example.class").write_bytes(baseline_raw)
    staged = tmp_path / "jdt-output"
    staged.mkdir()
    (staged / "Example.class").write_bytes(staged_raw)
    session = JavaProjectSession(
        root=tmp_path / "session",
        build_world_fingerprint="world",
    )
    session.generations.prepare_startup(staged)
    session.generations.promote_candidate(runtime_classpath_changed=True)

    class FakeJdt(PersistentJdtCompileSession):
        resource_delta = False

        @property
        def ready(self) -> bool:
            return True

        def compile(self, _sources):
            return JdtCompileResult(
                compile_ok=True,
                actual_build_kind="INCREMENTAL",
                compiled_source_count=1,
                compiled_source_units=("src/Example.java",),
                changed_classes=("Example.class",),
                deleted_classes=(),
                changed_resources=(),
                deleted_resources=(),
                error_count=0,
                warning_count=0,
                diagnostics=(),
                diagnostics_truncated=False,
                elapsed_ms=12.0,
                source_changes_pending=False,
                output_directory=staged,
                runtime_changed_classes=(
                    () if self.resource_delta else ("Example.class",)
                ),
                runtime_changed_resources=(
                    ("application.yml",) if self.resource_delta else ()
                ),
            )

        def close(self) -> bool:
            return True

        def mark_published(self) -> None:
            return None

    compiler = object.__new__(FakeJdt)
    session.attach_compile_session(compiler)
    plan = SimpleNamespace(
        project_root=project,
        source_roots=(project / "src/main/java",),
    )
    prepared = ProjectUpdatePlan(
        fast_compile_plan=None,
        fast_compile_unavailable_reason="DIRECT_DISABLED",
        attempt_directory=tmp_path,
        project_session=session,
        jdt_build_world_plan=plan,
    )
    runtime = JavaRuntime()
    process = SimpleNamespace(is_alive=lambda: True)
    runtime._proc._process = process
    monkeypatch.setattr(
        runtime,
        "_project_update_context",
        lambda: ("launch_1", 1, prepared),
    )

    class FakeJdwp:
        redefined = False

        def classes_by_signature(self, _signature):
            return [SimpleNamespace(reference_type_id=1)]

        def drain_events(self):
            return []

        def capabilities_new(self):
            return SimpleNamespace(can_redefine_classes=True)

        def redefine_classes(self, definitions):
            assert definitions
            self.redefined = True
            source.write_text(
                "public class Example { public int value() { return 3; } }",
                encoding="utf-8",
            )

    jdwp = FakeJdwp()
    monkeypatch.setattr(runtime, "_connect", lambda: jdwp)
    monkeypatch.setattr(
        runtime,
        "_resolve_hotswap_definitions",
        lambda _jdwp, updates, required_class_names: {
            1: updates[0].class_bytes
        },
    )
    action = RuntimeAction(action="update")
    action.source_files = ["src/main/java/Example.java"]

    result = runtime.update(action)

    assert result.ok is True
    assert result.data["status"] == "reload_started"
    assert result.data["applied"] is None
    deadline = time.monotonic() + 5
    while session.public_status()["last_reload"] is None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert session.public_status()["last_reload"]["apply_method"] == "hotswap"
    assert session.public_status()["last_reload"][
        "source_changes_pending"
    ] is False
    assert jdwp.redefined is True
    assert session.generations.current.ordinal == 1
    assert session.generations.current.output_directory.joinpath(
        "Example.class"
    ).read_bytes() == staged_raw
    assert session.generations.candidate is None
    last_reload = session.public_status()["last_reload"]
    assert last_reload["persistence"] == "jdt_workspace"
    assert last_reload["restart_loses_update"] is False
    assert last_reload["runtime_overlay_active"] is True

    compiler.resource_delta = True
    relaunch_started = runtime.update(action)
    assert relaunch_started.ok is True
    relaunch_id = relaunch_started.data["reload_id"]
    deadline = time.monotonic() + 5
    while (
        session.public_status()["last_reload"] is None
        or session.public_status()["last_reload"]["reload_id"] != relaunch_id
    ):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    relaunch = session.public_status()["last_reload"]
    assert relaunch["ok"] is False
    assert relaunch["error_code"] == "RELOAD_REQUIRES_RELAUNCH"
    assert relaunch["reason_code"] == "NON_HOTSWAPPABLE_OUTPUT_DELTA"
    assert relaunch["applied"] is False
    assert session.generations.current.ordinal == 1

    restart_action = RuntimeAction(action="update")
    restart_action.source_files = ["src/main/java/Example.java"]
    restart_action.hotswap = False
    rejected = runtime.update(restart_action)
    assert rejected.ok is False
    assert rejected.data["error_code"] == "RELOAD_REQUIRES_RELAUNCH"
    assert rejected.data["reason_code"] == "HOTSWAP_DISABLED"
    assert session.generations.candidate is None


def test_jdt_reload_returns_attempt_before_background_compile_finishes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    source = project / "src/main/java/Example.java"
    source.parent.mkdir(parents=True)
    source.write_text("public class Example {}\n", encoding="utf-8")
    initial = tmp_path / "initial"
    initial.mkdir()
    (initial / "Example.class").write_bytes(b"baseline")
    session = JavaProjectSession(
        root=tmp_path / "session",
        build_world_fingerprint="world",
    )
    session.generations.initialize(
        initial,
        build_world_fingerprint="world",
        runtime_active=True,
    )
    entered = threading.Event()
    release = threading.Event()

    class BlockingJdt(PersistentJdtCompileSession):
        @property
        def ready(self) -> bool:
            return True

        def compile(self, _sources):
            entered.set()
            assert release.wait(5)
            return JdtCompileResult(
                compile_ok=True,
                actual_build_kind="INCREMENTAL",
                compiled_source_count=0,
                compiled_source_units=(),
                changed_classes=(),
                deleted_classes=(),
                changed_resources=(),
                deleted_resources=(),
                error_count=0,
                warning_count=0,
                diagnostics=(),
                diagnostics_truncated=False,
                elapsed_ms=10.0,
                source_changes_pending=False,
                output_directory=tmp_path / "jdt-output",
            )

        def close(self) -> bool:
            return True

    compiler = object.__new__(BlockingJdt)
    session.attach_compile_session(compiler)
    plan = SimpleNamespace(
        project_root=project,
        source_roots=(project / "src/main/java",),
        is_fresh=lambda: True,
    )
    prepared = ProjectUpdatePlan(
        fast_compile_plan=None,
        fast_compile_unavailable_reason="DIRECT_DISABLED",
        attempt_directory=tmp_path,
        project_session=session,
        jdt_build_world_plan=plan,
    )
    runtime = JavaRuntime()
    runtime._proc._process = SimpleNamespace(is_alive=lambda: True)
    monkeypatch.setattr(
        runtime,
        "_project_update_context",
        lambda: ("launch_1", 1, prepared),
    )

    started_at = time.monotonic()
    result = runtime.update(_action(source, project))

    assert time.monotonic() - started_at < 0.5
    assert result.ok is True
    assert result.data["status"] == "reload_started"
    assert entered.wait(5)
    active = session.public_status()["active_operation"]
    assert active["reload_id"] == result.data["reload_id"]
    assert active["stage"] == "compiling"
    release.set()
    deadline = time.monotonic() + 5
    while session.public_status()["last_reload"] is None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert session.public_status()["last_reload"]["applied"] is True

def test_terminal_jdt_start_failure_is_not_reported_as_initializing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = JavaRuntime()
    session = JavaProjectSession(
        root=tmp_path / "session",
        build_world_fingerprint="world",
    )
    prepared = ProjectUpdatePlan(
        fast_compile_plan=None,
        fast_compile_unavailable_reason="DIRECT_DISABLED",
        attempt_directory=tmp_path,
        project_session=session,
        jdt_build_world_plan=SimpleNamespace(),
        jdt_unavailable_reason="JDT_CANDIDATE_INTEGRITY_MISMATCH",
        jdt_unavailable_details={
            "artifact": "worker.jar",
            "retryable": False,
        },
    )
    runtime._project_update_plans["launch_1"] = prepared
    monkeypatch.setattr(
        runtime,
        "_reconcile_project_process_exit",
        lambda: {
            "attempt_id": "launch_1",
            "generation": 1,
            "launch_phase": "runtime_active",
        },
    )

    result = runtime.update(RuntimeAction(action="update"))

    assert result.ok is False
    assert result.error == (
        "Persistent JDT reload is unavailable for this launch."
    )
    assert result.data["error_code"] == (
        "JDT_CANDIDATE_INTEGRITY_MISMATCH"
    )
    assert result.data["artifact"] == "worker.jar"
    assert result.data["retryable"] is False


def test_poisoned_jdt_reload_clears_ready_product_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    source = project / "src/main/java/Example.java"
    source.parent.mkdir(parents=True)
    source.write_text("public class Example {}\n", encoding="utf-8")
    initial = tmp_path / "initial"
    initial.mkdir()
    (initial / "Example.class").write_bytes(b"baseline")
    session = JavaProjectSession(
        root=tmp_path / "session",
        build_world_fingerprint="world",
    )
    session.generations.initialize(
        initial,
        build_world_fingerprint="world",
        runtime_active=True,
    )

    class PoisonedJdt(PersistentJdtCompileSession):
        is_ready = True
        closed = False

        @property
        def ready(self) -> bool:
            return self.is_ready

        def compile(self, _sources):
            self.is_ready = False
            raise JdtCompileError(
                "JDT_SOURCE_CHANGE_NOT_OBSERVED",
                "The requested source was missed.",
            )

        def close(self) -> bool:
            self.closed = True
            return True

    compiler = object.__new__(PoisonedJdt)
    session.attach_compile_session(compiler)
    session.complete_jdt_bootstrap(10.0)
    plan = SimpleNamespace(
        project_root=project,
        source_roots=(project / "src/main/java",),
        is_fresh=lambda: True,
    )
    prepared = ProjectUpdatePlan(
        fast_compile_plan=None,
        fast_compile_unavailable_reason="DIRECT_DISABLED",
        attempt_directory=tmp_path,
        project_session=session,
        jdt_build_world_plan=plan,
    )
    runtime = JavaRuntime()
    runtime._proc._process = SimpleNamespace(is_alive=lambda: True)
    monkeypatch.setattr(
        runtime,
        "_project_update_context",
        lambda: ("launch_1", 1, prepared),
    )

    result = runtime.update(_action(source, project))

    assert result.ok is True
    assert result.data["status"] == "reload_started"
    deadline = time.monotonic() + 5
    while session.public_status()["last_reload"] is None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert session.public_status()["last_reload"]["applied"] is False
    assert session.public_status()["last_reload"]["error_code"] == (
        "JDT_SOURCE_CHANGE_NOT_OBSERVED"
    )
    assert compiler.closed is True
    assert session.compile_session is None
    assert session.compile_ready is False
    assert session.jdt_bootstrap_state == "unavailable"
    assert session.jdt_unavailable_reason == "JDT_SOURCE_CHANGE_NOT_OBSERVED"




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


def test_static_initializer_change_requires_restart(tmp_path: Path) -> None:
    baseline_raw = _compile_class(
        tmp_path,
        "before-static",
        'public class Example { public static String VALUE = "old"; }',
    )
    staged_raw = _compile_class(
        tmp_path,
        "after-static",
        'public class Example { public static String VALUE = "new"; }',
    )
    baseline = parse_class_file(baseline_raw)
    staged = parse_class_file(staged_raw)
    source_key = "src/main/java/Example.java"
    runtime = JavaRuntime()

    with pytest.raises(FastCompileError) as captured:
        runtime._prepare_class_updates(
            {
                source_key: {
                    "Example": (
                        baseline,
                        baseline_raw,
                        "Example.class",
                    )
                }
            },
            {
                source_key: {
                    "Example": (
                        staged,
                        staged_raw,
                        "Example.class",
                    )
                }
            },
        )

    assert (
        captured.value.error_code
        == "STATIC_INITIALIZER_CHANGE_REQUIRES_RESTART"
    )
    assert captured.value.context["runtime_code_state"] == "unchanged"
    assert captured.value.context["restart_required"] is True


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
