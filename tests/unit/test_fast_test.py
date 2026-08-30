from __future__ import annotations

import zipfile
import threading
import time
import socket
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jolink_runtime.adapters.java.jdwp_adapter import JavaRuntime
from jolink_runtime.core.models import RuntimeAction
from jolink_runtime.core.dispatcher import Dispatcher
from jolink_runtime.launch.fast_test import (
    FastTestAssets,
    FastTestError,
    complete_test_runtime_classpath,
    detect_test_framework,
    FastTestRunner,
)
from jolink_runtime.launch.fast_test_manager import (
    FastTestManager,
    FastTestManagerError,
    TestAttempt as FastTestAttempt,
)
from jolink_runtime.launch.jdt_compile_session import JdtCompileError
from jolink_runtime.launch.process_supervisor import (
    AttemptToken,
    OperationResult,
)
from jolink_runtime.launch.process_tree import TerminationReport
from jolink_runtime.launch.maven_probe import ProductMavenProbe
from jolink_runtime.launch.gradle_probe import ProductGradleProbe
from jolink_runtime.server.tool_schema import JAVA_APPLICATION_INPUT_SCHEMA


def test_fast_test_assets_are_locked_java8_bytecode() -> None:
    assets = FastTestAssets.load()

    with zipfile.ZipFile(assets.runner_jar) as archive:
        majors = {
            int.from_bytes(archive.read(name)[6:8], "big")
            for name in archive.namelist()
            if name.endswith(".class")
        }

    assert majors == {52}
    assert assets.java_minimum == 8
    assert assets.runner_main_class.endswith(".TestRunner")


def test_product_probe_creates_private_mirror_safe_settings(
    tmp_path: Path,
) -> None:
    source = tmp_path / "settings.xml"
    source.write_text(
        """<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <mirrors><mirror><id>corp</id><url>https://example.invalid</url>
    <mirrorOf>*</mirrorOf></mirror></mirrors>
</settings>\n""",
        encoding="utf-8",
    )
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    probe = ProductMavenProbe.load()

    prepared = probe.prepare(
        attempt_directory=attempt,
        source_settings=source,
        local_repository=tmp_path / "repo",
        offline=False,
    )

    rendered = prepared.settings_file.read_text(encoding="utf-8")
    assert f"!{prepared.repository_id}" in rendered
    assert prepared.repository_id in rendered
    assert probe.goal == prepared.goal
    assert prepared.output_directory.is_dir()
    assert (
        tmp_path
        / "repo/io/jolink/jolink-maven-probe"
        / probe.version
        / f"jolink-maven-probe-{probe.version}.jar"
    ).is_file()


def test_product_gradle_probe_assets_are_content_checked(
    tmp_path: Path,
) -> None:
    probe = ProductGradleProbe.load()

    prepared = probe.prepare(tmp_path / "attempt")

    assert prepared.probe_jar.is_file()
    assert prepared.probe_sha256 == probe.sha256
    assert prepared.task_name.endswith(probe.sha256[:12])
    assert prepared.init_script.stat().st_mode & 0o777 == 0o600


def test_framework_detection_uses_project_runtime_classes(
    tmp_path: Path,
) -> None:
    junit4 = tmp_path / "junit4"
    (junit4 / "org/junit/runner").mkdir(parents=True)
    (junit4 / "org/junit/runner/JUnitCore.class").write_bytes(b"class")
    assert detect_test_framework((junit4,)) == "junit4"

    junit5 = tmp_path / "junit5"
    for relative in (
        "org/junit/platform/launcher/core/LauncherFactory.class",
        "org/junit/platform/engine/discovery/DiscoverySelectors.class",
        "org/junit/platform/launcher/listeners/SummaryGeneratingListener.class",
    ):
        output = junit5 / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"class")
    assert detect_test_framework((junit4, junit5)) == "auto"

    testng = tmp_path / "testng"
    for relative in (
        "org/testng/TestNG.class",
        "org/testng/TestListenerAdapter.class",
        "org/testng/xml/XmlSuite.class",
    ):
        output = testng / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"class")
    assert detect_test_framework((testng,)) == "testng"
    assert detect_test_framework((junit4, testng)) == "auto"

    with pytest.raises(FastTestError) as captured:
        detect_test_framework((tmp_path / "missing",))
    assert captured.value.error_code == "TEST_FRAMEWORK_UNAVAILABLE"


def test_fast_test_adds_exact_maven_junit_platform_launcher_companion(
    tmp_path: Path,
) -> None:
    platform = tmp_path / "repository/org/junit/platform"
    engine = (
        platform
        / "junit-platform-engine/1.7.2/junit-platform-engine-1.7.2.jar"
    )
    launcher = (
        platform
        / "junit-platform-launcher/1.7.2/junit-platform-launcher-1.7.2.jar"
    )
    engine.parent.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    with zipfile.ZipFile(engine, "w") as archive:
        archive.writestr(
            "org/junit/platform/engine/TestEngine.class", b"class"
        )
        archive.writestr(
            "org/junit/platform/engine/discovery/DiscoverySelectors.class",
            b"class",
        )
    with zipfile.ZipFile(launcher, "w") as archive:
        archive.writestr(
            "org/junit/platform/launcher/core/LauncherFactory.class",
            b"class",
        )
        archive.writestr(
            "org/junit/platform/launcher/listeners/"
            "SummaryGeneratingListener.class",
            b"class",
        )

    completed = complete_test_runtime_classpath((engine,))

    assert completed == (engine.resolve(), launcher.resolve())
    assert detect_test_framework(completed) == "junit5"


def test_fast_test_uses_the_nearest_compatible_junit_launcher_version(
    tmp_path: Path,
) -> None:
    platform = tmp_path / "repository/org/junit/platform"
    engine = (
        platform
        / "junit-platform-engine/1.7.2/junit-platform-engine-1.7.2.jar"
    )
    launcher = (
        platform
        / "junit-platform-launcher/1.8.2/junit-platform-launcher-1.8.2.jar"
    )
    engine.parent.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    with zipfile.ZipFile(engine, "w") as archive:
        archive.writestr(
            "org/junit/platform/engine/TestEngine.class", b"class"
        )
    with zipfile.ZipFile(launcher, "w") as archive:
        archive.writestr(
            "org/junit/platform/launcher/core/LauncherFactory.class",
            b"class",
        )
        archive.writestr(
            "org/junit/platform/launcher/listeners/"
            "SummaryGeneratingListener.class",
            b"class",
        )

    assert complete_test_runtime_classpath((engine,)) == (
        engine.resolve(),
        launcher.resolve(),
    )


def test_fast_test_rejects_a_different_junit_platform_major(
    tmp_path: Path,
) -> None:
    platform = tmp_path / "repository/org/junit/platform"
    engine = (
        platform
        / "junit-platform-engine/1.7.2/junit-platform-engine-1.7.2.jar"
    )
    launcher = (
        platform
        / "junit-platform-launcher/2.0.0/junit-platform-launcher-2.0.0.jar"
    )
    engine.parent.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    with zipfile.ZipFile(engine, "w") as archive:
        archive.writestr(
            "org/junit/platform/engine/TestEngine.class", b"class"
        )
    with zipfile.ZipFile(launcher, "w") as archive:
        archive.writestr(
            "org/junit/platform/launcher/core/LauncherFactory.class",
            b"class",
        )
        archive.writestr(
            "org/junit/platform/launcher/listeners/"
            "SummaryGeneratingListener.class",
            b"class",
        )

    assert complete_test_runtime_classpath((engine,)) == (engine.resolve(),)


def test_fast_test_manager_starts_idle_and_closes() -> None:
    manager = FastTestManager()
    assert manager.status() == {
        "ok": True,
        "status": "idle",
        "test_compile_ready": False,
    }
    assert manager.close() is True


def test_reactor_module_is_selected_by_explicit_test_class(
    tmp_path: Path,
) -> None:
    lib = tmp_path / "lib"
    app = tmp_path / "app"
    test = app / "src/test/java/example/AppTest.java"
    test.parent.mkdir(parents=True)
    test.write_text("package example; class AppTest {}\n", encoding="utf-8")
    modules = (
        SimpleNamespace(
            packaging="pom", directory=tmp_path, relative_path="."
        ),
        SimpleNamespace(
            packaging="jar", directory=lib, relative_path="lib"
        ),
        SimpleNamespace(
            packaging="jar", directory=app, relative_path="app"
        ),
    )
    attempt = SimpleNamespace(
        project_path=tmp_path,
        source_files=(),
        tests=("example.AppTest#works",),
    )

    selected = FastTestManager._select_test_module(
        SimpleNamespace(modules=modules), attempt
    )

    assert selected.relative_path == "app"


def test_reactor_test_selector_wins_over_upstream_source_owner(
    tmp_path: Path,
) -> None:
    lib = tmp_path / "lib"
    app = tmp_path / "app"
    upstream = lib / "src/main/java/example/Value.java"
    test = app / "src/test/java/example/AppTest.java"
    upstream.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    upstream.write_text("package example; class Value {}\n", encoding="utf-8")
    test.write_text("package example; class AppTest {}\n", encoding="utf-8")
    modules = tuple(
        SimpleNamespace(
            packaging="jar",
            directory=directory,
            relative_path=name,
        )
        for name, directory in (("lib", lib), ("app", app))
    )
    attempt = SimpleNamespace(
        project_path=tmp_path,
        source_files=("lib/src/main/java/example/Value.java",),
        tests=("example.AppTest#works",),
    )

    selected = FastTestManager._select_test_module(
        SimpleNamespace(modules=modules), attempt
    )

    assert selected.relative_path == "app"


def test_reactor_module_selection_fails_closed_when_ambiguous(
    tmp_path: Path,
) -> None:
    modules = tuple(
        SimpleNamespace(
            packaging="jar",
            directory=tmp_path / name,
            relative_path=name,
        )
        for name in ("one", "two")
    )
    attempt = SimpleNamespace(
        project_path=tmp_path,
        source_files=(),
        tests=("example.MissingTest",),
    )

    with pytest.raises(FastTestManagerError) as captured:
        FastTestManager._select_test_module(
            SimpleNamespace(modules=modules), attempt
        )

    assert captured.value.error_code == "FAST_TEST_MODULE_AMBIGUOUS"


def test_java_application_schema_exposes_fast_test_without_new_tool() -> None:
    actions = JAVA_APPLICATION_INPUT_SCHEMA["properties"]["action"]["enum"]
    assert "test" in actions
    assert "cancel_test" in actions
    assert "tests" in JAVA_APPLICATION_INPUT_SCHEMA["properties"]
    assert "test_run_id" in JAVA_APPLICATION_INPUT_SCHEMA["properties"]
    description = JAVA_APPLICATION_INPUT_SCHEMA["properties"]["project_path"][
        "description"
    ]
    assert "Gradle Wrapper" in description


def test_runtime_status_exposes_headless_fast_test_state() -> None:
    runtime = JavaRuntime()
    try:
        action = RuntimeAction(action="status")
        action._product_status = True
        payload = runtime.status(action).data
        assert payload["process_state"] == "absent"
        assert payload["fast_test"]["status"] == "idle"
    finally:
        runtime.close()


def test_fast_test_status_is_product_only_not_legacy_lineage() -> None:
    dispatcher = Dispatcher()
    product = dispatcher.dispatch("java_status", {"action": "status"})
    legacy = dispatcher.dispatch("java_runtime", {"action": "status"})
    try:
        assert product["fast_test"]["status"] == "idle"
        assert "fast_test" not in legacy
    finally:
        dispatcher.close_all_sessions()


def test_cancel_interrupts_an_inflight_jdt_compile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "src/main/java"
    source = source_root / "example/App.java"
    source.parent.mkdir(parents=True)
    source.write_text("package example; class App {}\n", encoding="utf-8")
    entered = threading.Event()
    interrupted = threading.Event()

    class Compiler:
        ready = True
        source_roots = (source_root,)
        test_source_roots = ()

        @staticmethod
        def workspace_source_changes():
            return (source.resolve(),)

        def compile(self, _sources):
            entered.set()
            assert interrupted.wait(5)
            self.ready = False
            raise JdtCompileError("FAST_TEST_CANCELLED", "cancelled")

        @staticmethod
        def interrupt(_reason):
            interrupted.set()

        @staticmethod
        def close():
            return True

    compiler = Compiler()
    project = SimpleNamespace(
        project_root=tmp_path,
        module_root=tmp_path,
        compiler=compiler,
        close=lambda: True,
    )
    manager = FastTestManager()
    manager._project = project
    monkeypatch.setattr(manager, "_ensure_project", lambda _attempt: project)

    started = manager.start(
        project_path=tmp_path,
        source_files=("src/main/java/example/App.java",),
        tests=("example.AppTest#works",),
        timeout_seconds=30,
        short_wait_seconds=0,
    )
    assert entered.wait(5)

    cancelled = manager.cancel(started["test_run_id"])
    deadline = time.monotonic() + 5
    while manager.status()["status"] not in {"cancelled", "failed"}:
        assert time.monotonic() < deadline
        time.sleep(0.01)

    assert interrupted.is_set()
    assert cancelled["status"] == "cancel_requested"
    assert manager.status()["status"] == "cancelled"
    manager.close()


def test_runner_rejects_an_unsettled_process_tree_and_uses_classpath_file(
    tmp_path: Path,
) -> None:
    junit = tmp_path / "junit"
    (junit / "org/junit/runner").mkdir(parents=True)
    (junit / "org/junit/runner/JUnitCore.class").write_bytes(b"class")

    class Supervisor:
        observed_argv = ()
        observed_spec = None

        def run(self, spec, *, owner):
            self.observed_spec = spec
            self.observed_argv = spec.argv
            values = list(spec.argv)

            def argument(name: str) -> str:
                return values[values.index(name) + 1]

            with socket.create_connection(
                (argument("--host"), int(argument("--port")))
            ) as connection, connection.makefile("w", encoding="utf-8") as stream:
                identity = {
                    "token": argument("--token"),
                    "test_run_id": argument("--run-id"),
                }
                stream.write(json.dumps({"event": "runner_ready", **identity}) + "\n")
                stream.write(
                    json.dumps(
                        {
                            "event": "run_finished",
                            **identity,
                            "framework": "junit4",
                            "tests": 1,
                            "passed_count": 1,
                            "failed_count": 0,
                            "failed_test_count": 0,
                            "failed_container_count": 0,
                            "skipped_count": 0,
                            "passed": True,
                            "failures": [],
                        }
                    )
                    + "\n"
                )
            now = time.monotonic()
            return OperationResult(
                operation_name=spec.operation_name,
                return_code=0,
                cancelled=False,
                timed_out=False,
                started_at=now,
                finished_at=now,
                output_capture=spec.output_capture,
                termination=TerminationReport(
                    pid=123,
                    terminated=False,
                    forced=True,
                    remaining_pids=(124,),
                ),
            )

        @staticmethod
        def close(*, deadline):
            return SimpleNamespace(settled=True)

    supervisor = Supervisor()
    runner = FastTestRunner(supervisor)

    with pytest.raises(FastTestError) as captured:
        runner.run(
            java_executable=Path("/fake/java"),
            classpath=(junit,),
            selectors=("example.Test#works",),
            working_directory=tmp_path,
            attempt_directory=tmp_path / "attempt",
            timeout_seconds=5,
            owner=AttemptToken("test_tree", 1),
            environment={"JOLINK_TEST_ENV": "preserved"},
        )

    assert captured.value.error_code == "TEST_PROCESS_TREE_NOT_SETTLED"
    assert captured.value.context["remaining_process_count"] == 1
    assert "-cp" not in supervisor.observed_argv
    assert "--classpath-file" in supervisor.observed_argv
    assert supervisor.observed_spec.environment == {
        "JOLINK_TEST_ENV": "preserved"
    }
    pathing = Path(
        supervisor.observed_argv[
            supervisor.observed_argv.index("-jar") + 1
        ]
    )
    with zipfile.ZipFile(pathing) as archive:
        manifest = archive.read("META-INF/MANIFEST.MF")
    assert b"Main-Class: net.jolink.runtime.test.TestRunner" in manifest
    assert b"Class-Path:" in manifest
    assert all(len(line) <= 70 for line in manifest.split(b"\r\n") if line)


def test_cancel_settled_waits_for_the_whole_attempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class Compiler:
        ready = True
        source_roots = ()
        test_source_roots = ()

        @staticmethod
        def workspace_source_changes():
            return ()

        @staticmethod
        def interrupt(_reason):
            return None

        @staticmethod
        def close():
            return True

    project = SimpleNamespace(
        project_root=tmp_path,
        module_root=tmp_path,
        compiler=Compiler(),
        close=lambda: True,
    )

    class Runner:
        calls = 0

        def run(self, **_kwargs):
            self.calls += 1
            raise AssertionError("cancelled attempt must not start Runner")

        @staticmethod
        def close(*, deadline=None):
            return True

    runner = Runner()
    manager = FastTestManager()
    manager._project = project
    manager._runner = runner

    def blocked(_attempt):
        entered.set()
        assert release.wait(5)
        return project

    monkeypatch.setattr(manager, "_ensure_project", blocked)
    started = manager.start(
        project_path=tmp_path,
        source_files=(),
        tests=("example.Test#works",),
        timeout_seconds=30,
        short_wait_seconds=0,
    )
    assert entered.wait(5)

    cancelled = manager.cancel(started["test_run_id"])
    assert cancelled["settled"] is False
    release.set()
    deadline = time.monotonic() + 5
    while manager.status()["status"] != "cancelled":
        assert time.monotonic() < deadline
        time.sleep(0.01)

    assert runner.calls == 0
    manager.close()


def test_probe_settings_are_deleted_when_maven_bootstrap_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = tmp_path / "maven-probe-settings.xml"
    settings.write_text("<settings/>\n", encoding="utf-8")
    output = tmp_path / "probe-output"
    output.mkdir()
    pom = tmp_path / "pom.xml"
    pom.write_text("<project/>\n", encoding="utf-8")
    attempt = SimpleNamespace(
        owner=AttemptToken("test_settings", 1),
        timeout_seconds=30.0,
        require_not_cancelled=lambda: None,
    )
    manager = FastTestManager()
    now = time.monotonic()
    monkeypatch.setattr(
        manager._supervisor,
        "run",
        lambda *_args, **_kwargs: OperationResult(
            operation_name="maven_fast_test_bootstrap",
            return_code=1,
            cancelled=False,
            timed_out=False,
            started_at=now,
            finished_at=now,
            output_capture=tmp_path / "build.log",
        ),
    )

    with pytest.raises(Exception):
        manager._run_maven_probe_bootstrap(
            attempt=attempt,
            probe=SimpleNamespace(load_snapshot=lambda *_args, **_kwargs: {}),
            prepared_probe=SimpleNamespace(
                settings_file=settings,
                output_directory=output,
                goal="io.jolink:probe:1:export",
            ),
            maven=SimpleNamespace(argv_prefix=("mvn",)),
            preferences=SimpleNamespace(active_profiles=()),
            workspace=SimpleNamespace(root_pom=pom, build_root=tmp_path),
            module=SimpleNamespace(directory=tmp_path),
            build_jdk=SimpleNamespace(
                java_executable=tmp_path / "java",
                source="PATH",
                home=tmp_path,
            ),
            local_repository=tmp_path / "repo",
            offline=False,
            effective_pom=tmp_path / "effective.xml",
            log=tmp_path / "build.log",
        )

    assert settings.exists() is False
    manager.close()


@pytest.mark.parametrize("failure_point", ["cancel", "tier1", "resource"])
def test_initializing_compiler_is_closed_until_project_ownership_transfers(
    tmp_path: Path,
    failure_point: str,
) -> None:
    class Compiler:
        closed = False

        @staticmethod
        def start():
            return SimpleNamespace(compile_ok=True)

        def close(self):
            self.closed = True
            return True

    compiler = Compiler()
    attempt = FastTestAttempt(
        test_run_id="test_ownership",
        generation=1,
        owner=AttemptToken("test_ownership", 1),
        project_path=tmp_path,
        source_files=(),
        tests=("example.Test#works",),
        timeout_seconds=30,
    )
    manager = FastTestManager()

    def fail(_full):
        if failure_point == "cancel":
            attempt.cancel_requested = True
            return SimpleNamespace()
        raise RuntimeError(failure_point)

    expected = FastTestManagerError if failure_point == "cancel" else RuntimeError
    with pytest.raises(expected):
        manager._run_compiler_initialization_transaction(
            attempt=attempt,
            compiler=compiler,
            finish=fail,
        )

    assert compiler.closed is True
    assert manager._initializing_compiler is None
    manager.close()


def test_successful_compiler_initialization_transfers_ownership(
    tmp_path: Path,
) -> None:
    class Compiler:
        closed = False

        @staticmethod
        def start():
            return SimpleNamespace(compile_ok=True)

        def close(self):
            self.closed = True
            return True

    compiler = Compiler()
    attempt = FastTestAttempt(
        test_run_id="test_publish",
        generation=1,
        owner=AttemptToken("test_publish", 1),
        project_path=tmp_path,
        source_files=(),
        tests=("example.Test#works",),
        timeout_seconds=30,
    )
    manager = FastTestManager()
    project = SimpleNamespace(compiler=compiler)

    result = manager._run_compiler_initialization_transaction(
        attempt=attempt,
        compiler=compiler,
        finish=lambda _full: project,
    )

    assert result is project
    assert compiler.closed is False
    assert manager._initializing_compiler is None
    compiler.close()
    manager.close()
