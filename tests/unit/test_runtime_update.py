from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jolink_runtime.adapters.java.jdwp_adapter import (
    JavaRuntime,
    ProjectUpdatePlan,
)
from jolink_runtime.adapters.java.jdwp_client import EventKind
from jolink_runtime.core.models import RuntimeAction
from jolink_runtime.launch.configuration_inputs import build_configuration_stamps
from jolink_runtime.launch.project_session import JavaProjectSession
from jolink_runtime.launch.jdt_compile_session import (
    JdtCompileError,
    JdtCompileResult,
    PersistentJdtCompileSession,
)


def _configuration_plan(project, **fields):
    return SimpleNamespace(
        project_root=project, configuration_inputs=(),
        configuration_stamps=build_configuration_stamps(project, "maven", ()),
        **fields,
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
    assert result.data["error_code"] == "JDT_SESSION_NOT_READY"
    assert result.data["runtime_code_state"] == "unchanged"


@pytest.mark.parametrize("use_alias", [False, True])
def test_jdt_reload_hotswap_uses_persistent_workspace_output(
    tmp_path: Path,
    monkeypatch,
    use_alias: bool,
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
    compiler.output_directory = staged
    session.attach_compile_session(compiler)
    alias = tmp_path / "project-alias"
    if use_alias:
        try:
            alias.symlink_to(project, target_is_directory=True)
        except OSError:
            pytest.skip("Creating directory symlinks is unavailable")
    plan = _configuration_plan(
        alias if use_alias else project,
        source_roots=(project / "src/main/java",),
    )
    prepared = ProjectUpdatePlan(
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
    assert session.public_status()["last_reload"]["source_changes_pending"] is False
    assert jdwp.redefined is True
    assert session.generations.current.ordinal == 1
    assert (
        session.generations.current.output_directory.joinpath(
            "Example.class"
        ).read_bytes()
        == staged_raw
    )
    assert session.generations.candidate is None
    last_reload = session.public_status()["last_reload"]
    assert last_reload["persistence"] == "jdt_workspace"
    assert last_reload["restart_loses_update"] is False
    assert last_reload["runtime_overlay_active"] is True

    for failure in ("published", "breakpoints"):

        def fail_bookkeeping(*_args):
            raise RuntimeError("injected post-apply failure")

        with monkeypatch.context() as patch:
            if failure == "published":
                patch.setattr(compiler, "mark_published", fail_bookkeeping)
            else:
                patch.setattr(runtime, "_refresh_updated_breakpoints", fail_bookkeeping)
            accepted = runtime.update(action)
            deadline = time.monotonic() + 5
            while (
                session.public_status()["last_reload"]["reload_id"]
                != accepted.data["reload_id"]
            ):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            reported = session.public_status()["last_reload"]
            assert reported["ok"] is True
            assert reported["applied"] is True
            assert reported["post_apply_state"] == "incomplete"
            assert reported["warnings"]

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
    plan = _configuration_plan(
        project,
        source_roots=(project / "src/main/java",),
        is_fresh=lambda: True,
    )
    prepared = ProjectUpdatePlan(
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
        attempt_directory=tmp_path,
        project_session=session,
        jdt_build_world_plan=_configuration_plan(tmp_path),
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
    assert result.error == ("Persistent JDT reload is unavailable for this launch.")
    assert result.data["error_code"] == ("JDT_CANDIDATE_INTEGRITY_MISMATCH")
    assert result.data["artifact"] == "worker.jar"
    assert result.data["retryable"] is False


@pytest.mark.parametrize("suspended", [False, True])
def test_jdt_reload_rejects_live_debug_observation_before_build(
    tmp_path, monkeypatch, suspended
):
    class ReadyJdt(PersistentJdtCompileSession):
        @property
        def ready(self):
            return True

        def compile(self, _sources):
            raise AssertionError("debug observation must be settled before compiling")

    compiler = object.__new__(ReadyJdt)
    session = JavaProjectSession(
        root=tmp_path / "session", build_world_fingerprint="world"
    )
    session.attach_compile_session(compiler)
    prepared = ProjectUpdatePlan(
        attempt_directory=tmp_path,
        project_session=session,
        jdt_build_world_plan=_configuration_plan(tmp_path),
    )
    runtime = JavaRuntime()
    monkeypatch.setattr(
        runtime, "_project_update_context", lambda: ("launch_1", 1, prepared)
    )
    if suspended:
        runtime._active_suspension = object()
    else:
        runtime._armed_breakpoint_requests = {1: "bp_1"}
    result = runtime.update(RuntimeAction(action="update"))
    assert result.ok is False
    assert result.data["error_code"] == (
        "ACTIVE_SUSPENSION_EXISTS" if suspended else "ACTIVE_DEBUG_REQUESTS_REMAIN"
    )


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
    plan = _configuration_plan(
        project,
        source_roots=(project / "src/main/java",),
        is_fresh=lambda: True,
    )
    prepared = ProjectUpdatePlan(
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
    assert updated["stale_reason"] == "CLASS_REDEFINED_BREAKPOINT_REQUIRES_RESET"
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
            "stale_reason": ("CLASS_REDEFINED_BREAKPOINT_REQUIRES_RESET"),
        },
    }
    runtime._resolve_breakpoint_class = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("stale preflight must happen before JDWP resolution")
    )

    observation = runtime._breakpoint_observations()[1]
    result = runtime._arm_debug_requests(
        object(),
        {EventKind.BREAKPOINT},
        None,
    )

    assert observation["stale"] is True
    assert observation["stale_reason"] == "CLASS_REDEFINED_BREAKPOINT_REQUIRES_RESET"
    assert result is not None
    assert result.ok is False
    assert result.data["error_code"] == "BREAKPOINT_DEFINITION_STALE"
    assert result.data["breakpoint_id"] == "bp_002"
    assert result.data["stale_breakpoint_ids"] == ["bp_002"]
    assert result.data["stale_reason"] == "CLASS_REDEFINED_BREAKPOINT_REQUIRES_RESET"
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
    assert "partial breakpoint arming" in result.data["suggested_next_step"]
