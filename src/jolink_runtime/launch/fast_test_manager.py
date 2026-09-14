"""Persistent JDT Fast Test session for one headless Maven project."""

from __future__ import annotations

import hashlib
import json
import locale
import os
import re
import shlex
import shutil
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from .contracts import BuildOperationSpec
from .compiler_profile import parse_maven_memory_megabytes
from .fast_test import FastTestError, FastTestRunner
from .fast_test_cache import FastTestCache
from .jdt_modules import ModuleCompileSession, compilation_modules
from .maven_module_world import load_module_worlds, combine_effective_poms
from .maven_compile_scope import compiler_scope, compiler_parameters
from .processor_path import processor_path, jdt_processor_paths, processor_settings
from .idea_environment import IdeaEnvironmentImporter
from .jdt_compile_session import (
    SUPPORTED_JAVA_LEVELS,
    JdtCompileError,
    PersistentJdtCompileSession,
    discover_target_system_entries,
    lombok_worker_jvm_arguments,
    select_target_system_home,
)
from .maven import MavenBuildSystemAdapter, MavenModule, MavenResolutionError, MavenWorkspace
from .maven_probe import MavenProbeError, ProductMavenProbe
from .gradle_probe import (
    GradleProbeError,
    ProductGradleProbe,
    gradle_configuration_environment_names,
    gradle_configuration_inputs,
)
from .gradle_module_world import GradleModuleError
from .gradle_test_build_world import (
    GradleBuildWorldError,
    create_gradle_test_build_world,
)
from .process_supervisor import AttemptToken, ProcessSupervisor
from .toolchain import JavaToolchainCandidate, JavaToolchainResolver, MavenToolResolver
from .test_build_world import (
    GradleTestBuildWorldBootstrap,
    JavaTestBuildWorld,
    MavenTestBuildWorldBootstrap,
    TestBuildWorldBootstrap,
)


_DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS = 900.0
_TEST_DISCOVERY_FILTERS = frozenset({"includes", "excludes"})
_BUILD_LOG_SECRET = re.compile(
    r"(?i)(password|passwd|token|secret|authorization|cookie|credential|"
    r"api[_-]?key|access[_-]?key|private[_-]?key)"
)
_BUILD_LOG_USERINFO = re.compile(r"(://)([^/@\s]+)@")
_BUILD_LOG_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _redacted_build_log_tail(path: Path) -> list[str]:
    try:
        raw = path.read_bytes()[-32 * 1024 :]
    except OSError:
        return []
    get_encoding = getattr(locale, "getencoding", None)
    encoding = (
        str(get_encoding())
        if callable(get_encoding)
        else locale.getpreferredencoding(False)
    )
    text = raw.decode(encoding, errors="replace")
    result: list[str] = []
    for line in text.splitlines()[-80:]:
        clean = _BUILD_LOG_ANSI.sub("", line)
        if _BUILD_LOG_SECRET.search(clean):
            result.append("<redacted sensitive build log line>")
        else:
            result.append(_BUILD_LOG_USERINFO.sub(r"\1<redacted>@", clean))
    return result


class FastTestManagerError(RuntimeError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.context = dict(context or {})


@dataclass
class _FastTestProject:
    build_system: str
    project_root: Path
    module_root: Path
    build_jdk: JavaToolchainCandidate
    test_java_executable: Path
    test_framework: str | None
    test_working_directory: Path
    runner_environment: dict[str, str]
    runner_jvm_arguments: tuple[str, ...]
    compiler: PersistentJdtCompileSession
    runtime_classpath: tuple[Path, ...]
    upstream_source_roots: tuple[Path, ...]
    runner_support_provenance: dict[str, Any]
    session_root: Path
    workspace_lease: Any
    test_run_order: str = ""
    test_attempts: list[tuple[Path, bool]] = field(default_factory=list)

    def close(self) -> bool:
        settled = self.compiler.close()
        self.workspace_lease.release(clean=settled)
        return settled


@dataclass
class TestAttempt:
    test_run_id: str
    generation: int
    owner: AttemptToken
    project_path: Path
    source_files: tuple[str, ...]
    tests: tuple[str, ...]
    timeout_seconds: float
    build_system: str = ""
    bootstrap_timeout_seconds: float = _DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS
    state: str = "starting"
    started_at: float = field(default_factory=time.time)
    started_monotonic: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    total_ms: float | None = None
    bootstrap_ms: float | None = None
    freshness_ms: float | None = None
    source_scan_ms: float | None = None
    compile_ms: float | None = None
    runner_ms: float | None = None
    post_test_freshness_ms: float | None = None
    compiled_source_count: int = 0
    compiled_source_units: tuple[str, ...] = ()
    result: dict[str, Any] | None = None
    cancel_requested: bool = False
    thread: threading.Thread | None = None
    done: threading.Event = field(default_factory=threading.Event)

    def require_not_cancelled(self) -> None:
        if self.cancel_requested:
            raise FastTestManagerError(
                "TEST_CANCELLED",
                "The Fast Test attempt was cancelled.",
            )

    def snapshot(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": True,
            "status": self.state,
            "test_run_id": self.test_run_id,
            "project_path": str(self.project_path),
            "source_file_count": len(self.source_files),
            "selected_test_count": len(self.tests),
            "build_system": self.build_system or "auto",
            "bootstrap_timeout_seconds": self.bootstrap_timeout_seconds,
            "cancel_requested": self.cancel_requested,
            "runtime_unchanged": True,
        }
        for name in (
            "total_ms",
            "bootstrap_ms",
            "freshness_ms",
            "source_scan_ms",
            "compile_ms",
            "runner_ms",
            "post_test_freshness_ms",
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        payload["compiled_source_count"] = self.compiled_source_count
        payload["compiled_source_units"] = list(
            self.compiled_source_units
        )
        if self.result is not None:
            payload.update(self.result)
        return payload


class FastTestManager:
    """Own one persistent Test Build World and one active TestAttempt."""

    def __init__(self) -> None:
        self._supervisor = ProcessSupervisor()
        self._runner = FastTestRunner(self._supervisor)
        self._cache = FastTestCache()
        self._maven = MavenBuildSystemAdapter()
        self._idea = IdeaEnvironmentImporter()
        self._java = JavaToolchainResolver()
        self._maven_tools = MavenToolResolver()
        self._bootstraps: tuple[TestBuildWorldBootstrap, ...] = (
            MavenTestBuildWorldBootstrap(),
            GradleTestBuildWorldBootstrap(),
        )
        self._project: _FastTestProject | None = None
        self._initializing_compiler: PersistentJdtCompileSession | None = None
        self._active: TestAttempt | None = None
        self._last: TestAttempt | None = None
        self._generation = 0
        self._pending_roots: set[Path] = set()
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._closed = False

    def start(
        self,
        *,
        project_path: Path,
        source_files: Sequence[str],
        tests: Sequence[str],
        timeout_seconds: float,
        build_system: str = "",
        bootstrap_timeout_seconds: float = (
            _DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS
        ),
        short_wait_seconds: float = 2.0,
    ) -> dict[str, Any]:
        root = project_path.expanduser().resolve(strict=True)
        timeout = min(max(float(timeout_seconds), 0.1), 600.0)
        bootstrap_timeout = min(
            max(float(bootstrap_timeout_seconds), 120.0),
            3600.0,
        )
        authority = str(build_system or "").strip().lower()
        if authority not in {"", "maven", "gradle"}:
            raise FastTestManagerError(
                "INVALID_ARGUMENT",
                "Fast Test build_system must be 'maven' or 'gradle'.",
                context={"argument": "build_system"},
            )
        with self._lock:
            if self._closed:
                raise FastTestManagerError(
                    "SERVER_SHUTTING_DOWN",
                    "The Fast Test manager is shutting down.",
                )
            if self._active is not None and not self._active.done.is_set():
                raise FastTestManagerError(
                    "TEST_ALREADY_RUNNING",
                    "Another Fast Test attempt is active.",
                    context={"test_run_id": self._active.test_run_id},
                )
            self._generation += 1
            test_run_id = f"test_{uuid.uuid4().hex[:12]}"
            attempt = TestAttempt(
                test_run_id=test_run_id,
                generation=self._generation,
                owner=AttemptToken(test_run_id, self._generation),
                project_path=root,
                source_files=tuple(str(value) for value in source_files),
                tests=tuple(str(value) for value in tests),
                build_system=authority,
                timeout_seconds=timeout,
                bootstrap_timeout_seconds=bootstrap_timeout,
            )
            thread = threading.Thread(
                target=self._run_attempt,
                args=(attempt,),
                name=f"jolink-fast-test-{self._generation}",
                daemon=True,
            )
            attempt.thread = thread
            self._active = attempt
            thread.start()
        attempt.done.wait(min(max(short_wait_seconds, 0.0), timeout))
        return attempt.snapshot()

    def status(self) -> dict[str, Any]:
        with self._lock:
            attempt = self._active or self._last
            project = self._project
            if attempt is None:
                return {
                    "ok": True,
                    "status": "idle",
                    "test_compile_ready": bool(
                        project is not None and project.compiler.ready
                    ),
                }
            result = attempt.snapshot()
            result["test_compile_ready"] = bool(
                project is not None and project.compiler.ready
            )
            return result

    def cancel(self, test_run_id: str) -> dict[str, Any]:
        with self._lock:
            attempt = self._active
            if (
                attempt is None
                or attempt.done.is_set()
                or attempt.test_run_id != test_run_id
            ):
                raise FastTestManagerError(
                    "TEST_RUN_NOT_FOUND",
                    "The requested active Fast Test was not found.",
                )
            attempt.cancel_requested = True
            owner = attempt.owner
            compiler = (
                self._project.compiler
                if self._project is not None
                else self._initializing_compiler
            )
            should_interrupt_compile = attempt.state in {
                "bootstrapping",
                "compiling",
            }
        if compiler is not None and should_interrupt_compile:
            compiler.interrupt("FAST_TEST_CANCELLED")
        self._supervisor.cancel(
            owner, deadline=time.monotonic() + 5.0
        )
        return {
            "ok": True,
            "status": "cancel_requested",
            "test_run_id": test_run_id,
            "settled": attempt.done.is_set(),
            "runtime_unchanged": True,
        }

    def _run_attempt(self, attempt: TestAttempt) -> None:
        try:
            with self._operation_lock:
                attempt.require_not_cancelled()
                ensure_started = time.monotonic()
                previous_project = self._project
                project = self._ensure_project(attempt)
                ensure_ms = round(
                    (time.monotonic() - ensure_started) * 1000, 1
                )
                if previous_project is project and previous_project is not None:
                    attempt.freshness_ms = ensure_ms
                else:
                    attempt.bootstrap_ms = ensure_ms
                attempt.require_not_cancelled()
                source_scan_started = time.monotonic()
                selected_sources = self._resolve_sources(
                    project, attempt.source_files
                )
                attempt.require_not_cancelled()
                selected_sources = tuple(dict.fromkeys((
                    *selected_sources,
                    *project.compiler.workspace_source_changes(),
                )))
                attempt.source_scan_ms = round(
                    (time.monotonic() - source_scan_started) * 1000, 1
                )
                if selected_sources:
                    attempt.state = "compiling"
                    attempt.require_not_cancelled()
                    compiled = project.compiler.compile(selected_sources)
                    attempt.require_not_cancelled()
                    attempt.compile_ms = compiled.elapsed_ms
                    attempt.compiled_source_count = (
                        compiled.compiled_source_count
                    )
                    attempt.compiled_source_units = tuple(
                        compiled.compiled_source_units
                    )
                    if not compiled.main_compile_ok or not compiled.test_compile_ok:
                        attempt.state = "compile_failed"
                        attempt.result = {
                            "ok": False,
                            "passed": False,
                            "error_code": "JDT_TEST_COMPILE_FAILED",
                            "main_compile_ok": compiled.main_compile_ok,
                            "test_compile_ok": compiled.test_compile_ok,
                            "error_count": compiled.error_count,
                            "diagnostics": list(compiled.diagnostics),
                            "suggested_next_step": (
                                "Fix the reported main/test diagnostics, then "
                                "retry test with every edited source_file."
                            ),
                        }
                        return
                if project.compiler.working_compile_state != "valid":
                    attempt.state = "compile_failed"
                    attempt.result = {
                        "ok": False,
                        "passed": False,
                        "error_code": "FAST_TEST_COMPILE_STATE_INVALID",
                        "working_compile_state": (
                            project.compiler.working_compile_state
                        ),
                        "error_count": (
                            project.compiler.last_compile_error_count
                        ),
                        "diagnostics": list(
                            project.compiler.last_compile_diagnostics
                        ),
                        "suggested_next_step": (
                            "Fix the prior compile errors and retry test with "
                            "every edited source_file before running tests."
                        ),
                    }
                    return
                attempt.require_not_cancelled()
                attempt.state = "running"
                test_attempt = (
                    project.session_root
                    / f"test-attempt-{attempt.test_run_id}"
                )
                runner_started = time.monotonic()
                try:
                    result = self._runner.run(
                        java_executable=project.test_java_executable,
                        framework=project.test_framework,
                        classpath=(
                            project.compiler.test_output_directory,
                            project.compiler.output_directory,
                            *project.runtime_classpath,
                        ),
                        selectors=attempt.tests,
                        run_order=project.test_run_order,
                        working_directory=project.test_working_directory,
                        environment=project.runner_environment,
                        jvm_arguments=project.runner_jvm_arguments,
                        attempt_directory=test_attempt,
                        timeout_seconds=attempt.timeout_seconds,
                        owner=attempt.owner,
                    )
                except Exception:
                    if test_attempt.exists():
                        self._retain_test_attempt(
                            project, test_attempt, failed=True
                        )
                    raise
                self._retain_test_attempt(
                    project, test_attempt, failed=not result.passed
                )
                attempt.runner_ms = round(
                    (time.monotonic() - runner_started) * 1000, 1
                )
                attempt.require_not_cancelled()
                attempt.state = "completed"
                attempt.result = {
                    "ok": True,
                    "passed": result.passed,
                    "framework": result.framework,
                    "tests": result.tests,
                    "passed_count": result.passed_count,
                    "failed_count": result.failed_count,
                    "failed_test_count": result.failed_test_count,
                    "failed_container_count": result.failed_container_count,
                    "skipped_count": result.skipped_count,
                    "test_ms": result.duration_ms,
                    "failed_tests": list(result.failures),
                    "failed_tests_truncated": result.failures_truncated,
                    "source_changes_pending": False,
                    "build_world_changes_pending": False,
                    "suggested_next_step": (
                        "Inspect failed_tests, edit the relevant source, and "
                        "retry the same explicit test selection."
                        if not result.passed
                        else "The selected tests passed; reload the edited "
                        "sources before Runtime verification if needed."
                    ),
                }
                provenance = getattr(
                    project, "runner_support_provenance", {}
                )
                if provenance:
                    attempt.result["test_runtime_support"] = dict(
                        provenance
                    )
        except (
            FastTestError,
            FastTestManagerError,
            JdtCompileError,
            GradleProbeError,
            GradleBuildWorldError,
            GradleModuleError,
            MavenProbeError,
            MavenResolutionError,
        ) as error:
            attempt.state = (
                "cancelled"
                if attempt.cancel_requested
                else "failed"
            )
            attempt.result = {
                "ok": False,
                "passed": False,
                "error_code": getattr(
                    getattr(error, "error_code", "FAST_TEST_FAILED"),
                    "value",
                    getattr(error, "error_code", "FAST_TEST_FAILED"),
                ),
                "error": str(error),
                **dict(getattr(error, "context", {}) or {}),
                "suggested_next_step": self._error_next_step(error),
            }
            if self._project is not None and not self._project.compiler.ready:
                self._drop_project()
            self._cleanup_pending_roots()
        except Exception as error:
            attempt.state = "failed"
            attempt.result = {
                "ok": False,
                "passed": False,
                "error_code": "FAST_TEST_FAILED",
                "error": f"{type(error).__name__}: {error}",
                "suggested_next_step": (
                    "Call java_status(status), correct the Fast Test setup, "
                    "then retry one explicit test."
                ),
            }
            self._drop_project()
            self._cleanup_pending_roots()
        finally:
            if attempt.cancel_requested:
                attempt.state = "cancelled"
                attempt.result = {
                    "ok": False,
                    "passed": False,
                    "error_code": "TEST_CANCELLED",
                    "error": "Fast Test was cancelled by the caller.",
                    "suggested_next_step": "Start another explicit test when ready.",
                }
            attempt.finished_at = time.time()
            attempt.total_ms = round(
                (time.monotonic() - attempt.started_monotonic) * 1000, 1
            )
            attempt.done.set()
            self._supervisor.release_owner(attempt.owner)
            with self._lock:
                if self._active is attempt:
                    self._last = attempt
                    self._active = None

    def _ensure_project(self, attempt: TestAttempt) -> _FastTestProject:
        provider = self._select_bootstrap(attempt)
        project = self._project
        if (
            project is not None
            and project.project_root == attempt.project_path
            and project.build_system == provider.kind
            and project.compiler.ready
            and self._test_selection_in_module(project.compiler.test_source_roots, attempt.tests)
            and self._cache.is_current(attempt.project_path, provider.kind)
        ):
            return project
        self._drop_project()
        attempt.state = "bootstrapping"
        cached = self._cache.load(attempt.project_path, provider.kind)
        if cached is not None:
            world, build_jdk = cached
            if self._test_selection_in_module(world.test_source_roots, attempt.tests):
                project = self._start_build_world(
                    attempt=attempt, world=world, build_jdk=build_jdk
                )
                self._project = project
                return project
        project = provider.bootstrap(self, attempt)
        self._project = project
        return project

    @staticmethod
    def _test_selection_in_module(roots, tests):
        return all(any((root / (test.partition("#")[0].split("$")[0].replace(".", "/") + ".java")).is_file()
                       for root in roots) for test in tests)

    def _bootstrap(self, attempt: TestAttempt) -> _FastTestProject:
        return self._select_bootstrap(attempt).bootstrap(self, attempt)

    def _select_bootstrap(
        self,
        attempt: TestAttempt,
    ) -> TestBuildWorldBootstrap:
        root = attempt.project_path
        matches = tuple(
            provider for provider in self._bootstraps if provider.detect(root)
        )
        requested = str(getattr(attempt, "build_system", "") or "")
        if requested:
            selected = tuple(
                provider for provider in matches if provider.kind == requested
            )
            if len(selected) == 1:
                return selected[0]
            raise FastTestManagerError(
                "BUILD_SYSTEM_NOT_FOUND",
                f"Fast Test did not find the requested {requested} build.",
                context={
                    "build_system": requested,
                    "detected_build_systems": [item.kind for item in matches],
                },
            )
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise FastTestManagerError(
                "BUILD_SYSTEM_AMBIGUOUS",
                "Fast Test found multiple supported build systems.",
                context={"build_systems": [item.kind for item in matches]},
            )
        raise FastTestManagerError(
            "BUILD_SYSTEM_NOT_FOUND",
            "Fast Test requires a Maven POM or Gradle Wrapper project.",
        )

    def _bootstrap_maven(self, attempt: TestAttempt) -> _FastTestProject:
        attempt.require_not_cancelled()
        session_root = Path(
            tempfile.mkdtemp(prefix="jolink-fast-test-session-")
        )
        with self._lock:
            self._pending_roots.add(session_root)
        bootstrap = session_root / "bootstrap"
        bootstrap.mkdir(mode=0o700)
        log = bootstrap / "maven-test-bootstrap.log"
        preferences = self._idea.import_preferences(attempt.project_path)
        root = attempt.project_path.resolve(strict=True)
        workspace = MavenWorkspace(root, root, root / "pom.xml", ())
        attempt.require_not_cancelled()
        build_jdk = self._select_build_jdk(
            preferences, workspace.build_root, log, attempt
        )
        maven = self._select_maven(
            preferences, workspace.project_root, build_jdk, log, attempt
        )
        attempt.require_not_cancelled()
        source_settings = preferences.user_settings_file
        if source_settings is None:
            default_settings = Path.home() / ".m2/settings.xml"
            source_settings = default_settings if default_settings.is_file() else None
        local_repository = (
            preferences.local_repository
            or Path.home() / ".m2/repository"
        ).expanduser().resolve(strict=False)
        offline = any(
            token in {"-o", "--offline"}
            for token in shlex.split(os.environ.get("MAVEN_ARGS", ""))
        )
        probe = ProductMavenProbe.load()
        prepared_probe = probe.prepare(
            attempt_directory=bootstrap,
            source_settings=source_settings,
            local_repository=local_repository,
            offline=offline,
        )
        effective_pom = bootstrap / "effective-pom.xml"
        try:
            snapshot = self._run_maven_probe_bootstrap(
                attempt=attempt,
                probe=probe,
                prepared_probe=prepared_probe,
                maven=maven,
                preferences=preferences,
                workspace=workspace,
                build_jdk=build_jdk,
                local_repository=local_repository,
                offline=offline,
                effective_pom=effective_pom,
                log=log,
            )
        finally:
            prepared_probe.settings_file.unlink(missing_ok=True)
        attempt.require_not_cancelled()
        native = snapshot["project"]
        directory = Path(native["baseDirectory"])
        module = MavenModule(
            relative_path=Path(os.path.relpath(directory, workspace.project_root)).as_posix(),
            directory=directory, pom_file=directory / "pom.xml",
            group_id=native["groupId"], artifact_id=native["artifactId"],
            name=native.get("name"), packaging=native["packaging"],
            output_directory=Path(snapshot["outputDirectory"]),
        )
        project = self._create_project_from_snapshot(
            attempt=attempt,
            session_root=session_root,
            workspace=workspace,
            module=module,
            build_jdk=build_jdk,
            preferences=preferences,
            effective_pom=effective_pom,
            source_settings=source_settings,
            snapshot=snapshot,
        )
        with self._lock:
            self._pending_roots.discard(session_root)
        return project

    def _bootstrap_gradle(self, attempt: TestAttempt) -> _FastTestProject:
        from .gradle_probe import gradle_build_root
        attempt.require_not_cancelled()
        session_root = Path(
            tempfile.mkdtemp(prefix="jolink-gradle-fast-test-session-")
        )
        with self._lock:
            self._pending_roots.add(session_root)
        bootstrap = session_root / "bootstrap"
        bootstrap.mkdir(mode=0o700)
        log = bootstrap / "gradle-test-bootstrap.log"
        project = gradle_build_root(attempt.project_path) or attempt.project_path
        preferences = self._idea.import_preferences(project)
        build_jdk = self._select_build_jdk(
            preferences, project, log, attempt
        )
        probe = ProductGradleProbe.load()
        wrapper = project / ("gradlew.bat" if os.name == "nt" else "gradlew")
        if not wrapper.is_file():
            raise FastTestManagerError(
                "GRADLE_WRAPPER_UNAVAILABLE",
                "The Gradle Wrapper executable is unavailable.",
            )
        environment = JavaToolchainResolver.maven_environment(build_jdk)
        gradle_args = shlex.split(os.environ.get("GRADLE_ARGS", ""))
        if any(value not in {"-o", "--offline"} for value in gradle_args):
            raise FastTestManagerError(
                "GRADLE_ARGUMENTS_UNSUPPORTED",
                "Only Gradle offline mode is supported through GRADLE_ARGS.",
            )
        offline = any(value in {"-o", "--offline"} for value in gradle_args)
        prepared = probe.prepare(bootstrap)
        try:
            operation = self._supervisor.run(
                BuildOperationSpec(
                    argv=probe.command(
                        wrapper=wrapper,
                        prepared=prepared,
                        offline=offline,
                        tests=attempt.tests,
                        target_directory=attempt.project_path,
                    ),
                    cwd=project,
                    environment=environment,
                    timeout_seconds=getattr(
                        attempt,
                        "bootstrap_timeout_seconds",
                        _DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS,
                    ),
                    output_capture=log,
                    max_output_bytes=16 * 1024 * 1024,
                    operation_name="gradle_fast_test_bootstrap",
                ),
                owner=attempt.owner,
            )
            attempt.require_not_cancelled()
            if operation.timed_out:
                raise self._bootstrap_timeout_error(
                    operation,
                    attempt=attempt,
                    stage="gradle_classes_test_classes",
                    log=log,
                )
            if operation.output_limit_exceeded:
                raise FastTestManagerError(
                    "FAST_TEST_BOOTSTRAP_OUTPUT_LIMIT_EXCEEDED",
                    "The Gradle Fast Test Bootstrap exceeded the 16 MiB log limit.",
                )
            if not operation.succeeded:
                if prepared.output_file.is_file():
                    probe.load_model(prepared)
                raise FastTestManagerError(
                    "FAST_TEST_BOOTSTRAP_FAILED",
                    "The Gradle Build World Probe failed.",
                    context={
                        "return_code": operation.return_code,
                        "bootstrap_log_tail": _redacted_build_log_tail(log),
                    },
                )
            model = probe.load_model(prepared)
        finally:
            probe.cleanup(prepared)
        configuration_inputs = gradle_configuration_inputs(project)
        configuration_environment_names = (
            gradle_configuration_environment_names()
        )
        world = create_gradle_test_build_world(
            model=model,
            project_root=attempt.project_path,
            configuration_inputs=configuration_inputs,
            runner_environment=environment,
            configuration_environment_names=(
                configuration_environment_names
            ),
        )
        result = self._start_build_world(
            attempt=attempt,
            world=world,
            build_jdk=build_jdk,
        )
        shutil.rmtree(session_root, ignore_errors=True)
        with self._lock:
            self._pending_roots.discard(session_root)
        return result

    def _run_maven_probe_bootstrap(
        self,
        *,
        attempt: TestAttempt,
        probe: ProductMavenProbe,
        prepared_probe: Any,
        maven: Any,
        preferences: Any,
        workspace: Any,
        build_jdk: JavaToolchainCandidate,
        local_repository: Path,
        offline: bool,
        effective_pom: Path,
        log: Path,
    ) -> dict[str, Any]:
        attempt.require_not_cancelled()
        command = [
            *maven.argv_prefix,
            "--batch-mode",
            "--fail-fast",
            "-Dstyle.color=never",
            "-f",
            str(workspace.root_pom),
            "-s",
            str(prepared_probe.settings_file),
            f"-Dmaven.repo.local={local_repository}",
        ]
        if preferences.active_profiles:
            command.extend(["-P", ",".join(preferences.active_profiles)])
        if offline:
            command.append("--offline")
        command.extend(
            [
                prepared_probe.goal.replace("export-build-world", "export-reactor-world"),
                f"-Djolink.probe.targetDirectory={attempt.project_path}",
                (
                    "-Djolink.probe.outputDirectory="
                    f"{prepared_probe.output_directory}"
                ),
            ]
        )
        command.append("-Djolink.probe.testClasses=" + ",".join(attempt.tests))
        if attempt.source_files:
            command.append("-Djolink.probe.sourceFiles=" + ",".join(attempt.source_files))
        try:
            operation = self._supervisor.run(
                BuildOperationSpec(
                    argv=tuple(command),
                    cwd=workspace.build_root,
                    environment=JavaToolchainResolver.maven_environment(build_jdk),
                    timeout_seconds=getattr(
                        attempt,
                        "bootstrap_timeout_seconds",
                        _DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS,
                    ),
                    output_capture=log,
                    max_output_bytes=16 * 1024 * 1024,
                    operation_name="maven_fast_test_bootstrap",
                ),
                owner=attempt.owner,
            )
            attempt.require_not_cancelled()
            if operation.timed_out:
                raise self._bootstrap_timeout_error(
                    operation,
                    attempt=attempt,
                    stage="maven_test_probe",
                    log=log,
                )
            if operation.output_limit_exceeded:
                raise FastTestManagerError(
                    "FAST_TEST_BOOTSTRAP_OUTPUT_LIMIT_EXCEEDED",
                    "The Maven Fast Test Bootstrap exceeded the 16 MiB log limit.",
                )
            if not operation.succeeded:
                selection = prepared_probe.output_directory / "selection-error.txt"
                if selection.is_file():
                    code, *candidates = selection.read_text(encoding="utf-8").splitlines()
                    raise FastTestManagerError(code,
                        "Select test classes from one Maven jar module, or specify its project_path.",
                        context={"candidate_modules": candidates})
                raise FastTestManagerError(
                    "FAST_TEST_BOOTSTRAP_FAILED",
                    "The Maven Test Build World Probe failed.",
                    context={
                        "return_code": operation.return_code,
                        "bootstrap_log_tail": _redacted_build_log_tail(log),
                    },
                )
            module_root = Path(
                (prepared_probe.output_directory / "selected-module.txt").read_text(encoding="utf-8"))
            snapshot = probe.load_snapshot(prepared_probe, module_root=module_root)
            combine_effective_poms(prepared_probe.output_directory, effective_pom)
            worlds = load_module_worlds(
                prepared_probe.output_directory, module_root, build_jdk, tests=True
            )
            snapshot["moduleWorlds"] = worlds
            attempt.require_not_cancelled()
            return snapshot
        finally:
            prepared_probe.settings_file.unlink(missing_ok=True)

    def _create_project_from_snapshot(
        self,
        *,
        attempt: TestAttempt,
        session_root: Path,
        workspace: Any,
        module: Any,
        build_jdk: JavaToolchainCandidate,
        preferences: Any,
        effective_pom: Path,
        source_settings: Path | None,
        snapshot: dict[str, Any],
    ) -> _FastTestProject:
        attempt.require_not_cancelled()
        main_roots = self._paths(snapshot, "compileSourceRoots", module.directory)
        test_roots = self._paths(
            snapshot, "testCompileSourceRoots", module.directory
        )
        if not test_roots:
            raise FastTestManagerError(
                "FAST_TEST_SOURCE_ROOTS_UNAVAILABLE",
                "The Maven Probe found no test Java source roots.",
                context={
                    "main_source_root_count": len(main_roots),
                    "test_source_root_count": len(test_roots),
                },
            )
        attempt.require_not_cancelled()
        main_output = Path(str(snapshot["outputDirectory"])).resolve(
            strict=False
        )
        test_output = Path(str(snapshot["testOutputDirectory"])).resolve(strict=False)
        main_classpath = self._paths(
            snapshot, "compileClasspathElements", module.directory
        )
        test_classpath = self._paths(
            snapshot, "testClasspathElements", module.directory
        )
        resource_roots = (
            *self._paths(snapshot, "resourceDirectories", module.directory),
            *self._paths(
                snapshot, "testResourceDirectories", module.directory
            ),
        )
        output_keys = {
            os.path.normcase(str(main_output)),
            os.path.normcase(str(test_output)),
        }
        main_dependencies = tuple(
            path
            for path in main_classpath
            if os.path.normcase(str(path)) not in output_keys
            and self._maven._jdt_dependency_facts(path)[0]
        )
        main_keys = {os.path.normcase(str(path)) for path in main_dependencies}
        test_dependencies = tuple(
            path
            for path in test_classpath
            if os.path.normcase(str(path)) not in output_keys
            and os.path.normcase(str(path)) not in main_keys
            and self._maven._jdt_dependency_facts(path)[0]
        )
        runtime_classpath = tuple(
            path
            for path in test_classpath
            if os.path.normcase(str(path)) not in output_keys
        )
        main_processing = snapshot.get("annotationProcessing")
        test_processing = snapshot.get("testAnnotationProcessing")
        test_runtime = snapshot.get("testRuntime")
        if not isinstance(main_processing, dict) or not isinstance(
            test_processing, dict
        ) or not isinstance(test_runtime, dict):
            raise FastTestManagerError(
                "FAST_TEST_PROCESSOR_MODEL_UNAVAILABLE",
                "The Probe omitted main/test Processor facts.",
            )
        runner_support = tuple(
            Path(str(value)).resolve(strict=True)
            for value in test_runtime.get(
                "runnerSupportClasspathElements", []
            )
        )
        runtime_classpath = tuple(
            dict.fromkeys((*runtime_classpath, *runner_support))
        )
        runner_support_provenance = {
            "selection_source": test_runtime.get(
                "runnerSupportSelectionSource"
            ),
            "selected_version": test_runtime.get(
                "runnerSupportSelectedVersion"
            ),
            "fallback_used": bool(
                test_runtime.get("runnerSupportFallbackUsed", False)
            ),
            "fallback_reason": test_runtime.get(
                "runnerSupportFallbackReason"
            ),
        }
        runner_support_provenance = {
            key: value
            for key, value in runner_support_provenance.items()
            if value is not None
        }
        effective_root = self._maven._read_effective_pom(effective_pom)
        effective_project = self._maven._select_effective_project(
            effective_root, module
        )
        surefire_configuration = self._surefire_configuration(effective_project)
        additional_classpath: tuple[Path, ...] = ()
        if surefire_configuration is not None:
            for entry in surefire_configuration.findall(
                "./{*}additionalClasspathElements/{*}additionalClasspathElement"
            ):
                value = (entry.text or "").strip()
                if value:
                    path = Path(value)
                    additional_classpath += ((module.directory / path).resolve(strict=False),)
        runtime_classpath = (*runtime_classpath, *additional_classpath)
        compiler_model = self._maven._compiler_model(
            compiler_scope(effective_project),
            build_jdk=build_jdk,
            runtime_jdk=build_jdk,
        )
        if (
            compiler_model["source_level"]
            != compiler_model["target_level"]
            or compiler_model["target_level"] not in SUPPORTED_JAVA_LEVELS
        ):
            raise FastTestManagerError(
                "FAST_TEST_JAVA_LEVEL_UNSUPPORTED",
                "Fast Test supports equal Java 8 through 26 source/target levels.",
                context={
                    "source_level": compiler_model["source_level"],
                    "target_level": compiler_model["target_level"],
                },
            )
        test_scope = compiler_scope(effective_project, test=True)
        test_model = self._maven._compiler_model(test_scope, build_jdk=build_jdk, runtime_jdk=build_jdk)
        if test_model["source_level"] != test_model["target_level"] or test_model["target_level"] not in SUPPORTED_JAVA_LEVELS:
            raise FastTestManagerError("FAST_TEST_JAVA_LEVEL_UNSUPPORTED",
                "The bundled JDT supports equal Java 8 through 26 test source/target levels.",
                context={"scope": "test", **test_model})
        for label, processing in (
            ("main", main_processing),
            ("test", test_processing),
        ):
            if processing.get("discoveryMode") not in {
                "DISABLED",
                "IMPLICIT_COMPILE_CLASSPATH",
                "EXPLICIT_PROCESSOR_PATH",
            }:
                raise FastTestManagerError(
                    "FAST_TEST_PROCESSOR_MODEL_UNSUPPORTED",
                    f"Fast Test cannot reproduce the {label} Processor discovery mode.",
                    context={"scope": label, "discovery_mode": processing.get("discoveryMode")},
                )
        unsupported_surefire = test_runtime.get(
            "unsupportedSurefireConfigurationNames", []
        )
        unsupported_surefire = [
            value
            for value in unsupported_surefire
            if value not in {"skip", "skipTests"}
        ]
        test_java = build_jdk.java_executable
        surefire = self._maven._find_build_plugin(effective_project, "maven-surefire-plugin")
        if surefire is not None:
            configured_java = surefire.findtext("./{*}configuration/{*}jvm")
            if configured_java:
                # Maven's optional <jvm>${property}</jvm> defaults to its
                # current Java when that property is not set.
                property_match = re.fullmatch(r"\$\{([^}]+)\}", configured_java.strip())
                if property_match:
                    configured_java = effective_project.findtext("./{*}properties/{*}" + property_match[1]) or ""
            if configured_java is not None and "${" not in configured_java:
                if configured_java.strip(): test_java = Path(configured_java.strip())
                unsupported_surefire = [name for name in unsupported_surefire if name != "jvm"]
        (
            unsupported_surefire,
            test_jvm_arguments,
        ) = self._surefire_runtime_compatibility(
            effective_project,
            unsupported_surefire,
            attempt=attempt,
        )
        if unsupported_surefire:
            raise FastTestManagerError(
                "FAST_TEST_SUREFIRE_CONFIGURATION_UNSUPPORTED",
                "Fast Test cannot reproduce this Surefire runtime configuration.",
                context={
                    "unsupported_configuration_names": list(
                        unsupported_surefire
                    )[:16]
                },
            )
        unsupported_failsafe = test_runtime.get(
            "unsupportedFailsafeConfigurationNames", []
        )
        unsupported_failsafe = [
            value
            for value in unsupported_failsafe
            # Fast Test selects explicit tests, not Failsafe's IT discovery.
            if value not in {"skip", "skipTests"} | _TEST_DISCOVERY_FILTERS
        ]
        if unsupported_failsafe:
            raise FastTestManagerError(
                "FAST_TEST_FAILSAFE_CONFIGURATION_UNSUPPORTED",
                "Fast Test cannot reproduce this Failsafe runtime configuration.",
                context={
                    "unsupported_configuration_names": list(
                        unsupported_failsafe
                    )[:16]
                },
            )
        unsupported_test_compiler = test_runtime.get(
            "unsupportedTestCompilerConfigurationNames", []
        )
        unsupported_test_compiler = [
            value
            for value in unsupported_test_compiler
            if value not in {
                "testCompile.parameters",
                "testCompile.annotationProcessorPaths",
                "testCompile.annotationProcessorPathsUseDepMgmt",
                "testCompile.annotationProcessors",
                # Maven's stale-source selection does not configure JDT's builder.
                "testCompile.useIncrementalCompilation",
                "testRelease", "testSource", "testTarget", "testEncoding",
                "testCompile.release", "testCompile.source", "testCompile.target", "testCompile.encoding",
            }
        ]
        unsupported_test_compiler = self._unshared_test_compiler_configuration(
            effective_project,
            unsupported_test_compiler,
            compiler_model=compiler_model,
        )
        (
            unsupported_test_compiler,
            worker_min_heap_mb,
            worker_max_heap_mb,
        ) = self._compiler_argument_compatibility(
            effective_project,
            unsupported_test_compiler,
        )
        if unsupported_test_compiler:
            raise FastTestManagerError(
                "FAST_TEST_COMPILER_CONFIGURATION_UNSUPPORTED",
                "Fast Test cannot reproduce this test compiler configuration.",
                context={
                    "unsupported_configuration_names": list(
                        unsupported_test_compiler
                    )[:16]
                },
            )
        main_processor_paths = processor_path(main_processing)
        attempt.require_not_cancelled()
        processor_entries, lombok = jdt_processor_paths(main_processing, main_processor_paths)
        method_parameters = compiler_parameters(compiler_scope(effective_project))
        target_level = int(compiler_model["target_level"])
        target_java_home = self._select_target_java(
            preferences,
            build_jdk,
            target_level=target_level,
        )
        dependency_keys = {
            os.path.normcase(str(path))
            for path in (*main_dependencies, *test_dependencies)
        }
        upstream_source_roots = tuple(
            root
            for item in workspace.modules
            if item.directory != module.directory
            and os.path.normcase(str(item.output_directory)) in dependency_keys
            for root in (
                item.directory / "src/main/java",
                item.directory / "src/test/java",
            )
            if root.is_dir()
        )
        configuration_inputs = tuple(
            [item.pom_file for item in workspace.modules]
            + [effective_pom]
            + ([source_settings] if source_settings is not None else [])
            + list(self._resource_inputs(resource_roots))
            + [
                path
                for path in (
                    workspace.build_root / ".mvn/maven.config",
                    workspace.build_root / ".mvn/jvm.config",
                    workspace.build_root / ".mvn/extensions.xml",
                )
                if path.is_file()
            ]
        )
        world = JavaTestBuildWorld(
            build_system="maven",
            project_root=workspace.project_root,
            module_root=module.directory,
            main_source_roots=main_roots,
            test_source_roots=test_roots,
            main_output=main_output,
            test_output=test_output,
            main_dependencies=main_dependencies,
            test_dependencies=test_dependencies,
            test_runtime_classpath=runtime_classpath,
            resource_roots=tuple(resource_roots),
            target_java_home=target_java_home,
            source_encoding=self._maven._source_encoding(effective_project),
            source_level=target_level,
            method_parameters=method_parameters,
            processor_entries=processor_entries,
            **processor_settings(main_processing),
            java_agents=tuple(f"{path}=ECJ" for path in lombok),
            extra_worker_jvm_arguments=(),
            test_java_executable=test_java,
            test_framework=None,
            test_working_directory=module.directory,
            test_classes_directories=(test_output,),
            runner_environment={},
            test_jvm_arguments=test_jvm_arguments,
            test_run_order=self._maven._config_text(surefire_configuration, "runOrder") if surefire_configuration is not None else "",
            javac_executable=build_jdk.javac_executable,
            configuration_inputs=configuration_inputs,
            configuration_environment_names=(),
            upstream_source_roots=upstream_source_roots,
            worker_min_heap_mb=worker_min_heap_mb,
            worker_max_heap_mb=worker_max_heap_mb,
            runner_support_provenance=runner_support_provenance,
            modules=tuple(snapshot.get("moduleWorlds", ())),
        )
        if world.modules:
            own_outputs = {str(main_output), str(test_output)}
            world = replace(world,
                main_source_roots=tuple(Path(p) for m in world.modules for p in m["source_roots"]),
                java_agents=tuple(dict.fromkeys(f"{p}=ECJ" for m in world.modules
                    for p in (*m["lombok_entries"], *m.get("test_compiler", {}).get("lombok_entries", ())))),
                test_runtime_classpath=tuple(Path(p) for p in snapshot["testClasspathElements"] if p not in own_outputs) + runner_support + additional_classpath,
                resource_roots=tuple(self._paths(snapshot,"testResourceDirectories",module.directory)) + tuple(self._paths(snapshot,"resourceDirectories",module.directory)),
            )
        result = self._start_build_world(
            attempt=attempt,
            world=world,
            build_jdk=build_jdk,
        )
        shutil.rmtree(session_root, ignore_errors=True)
        with self._lock:
            self._pending_roots.discard(session_root)
        return result

    def _start_build_world(
        self,
        *,
        attempt: TestAttempt,
        world: JavaTestBuildWorld,
        build_jdk: JavaToolchainCandidate,
    ) -> _FastTestProject:
        attempt.require_not_cancelled()
        from .runtime_preparation import prepared_runtime

        candidate, worker_java = prepared_runtime(
            (build_jdk.home, world.target_java_home),
            check_request=attempt.require_not_cancelled,
        )
        target_home = select_target_system_home(
            (world.target_java_home, build_jdk.home), world.source_level
        )
        identity = hashlib.sha256(json.dumps({
            "candidate": candidate.root.name,
            "source_level": world.source_level,
            "main_sources": [str(path) for path in world.main_source_roots],
            "test_sources": [str(path) for path in world.test_source_roots],
            "main_dependencies": [str(path) for path in world.main_dependencies],
            "test_dependencies": [str(path) for path in world.test_dependencies],
            "processors": [str(path) for path in world.processor_entries],
            "processor_names": world.processor_names,
            "processor_options": world.processor_options,
            "modules": compilation_modules(world.modules),
        }, sort_keys=True).encode()).hexdigest()
        workspace = self._cache.workspace_store(
            world.project_root, world.build_system
        ).claim(
            project_root=world.project_root,
            module_root=world.module_root,
            identity={"fast_test_world": identity},
        )
        lombok_enabled = bool(world.java_agents)
        factory = ModuleCompileSession if world.modules else PersistentJdtCompileSession
        compiler = factory(
            **({"modules": world.modules, "target_module": world.module_root, "split_tests": True} if world.modules else {}),
            root=workspace.root,
            candidate=candidate,
            worker_java_home=worker_java.home,
            source_roots=world.main_source_roots,
            classpath_entries=(
                *discover_target_system_entries(
                    target_home, world.source_level
                ),
                *world.main_dependencies,
            ),
            source_encoding=world.source_encoding,
            source_level=world.source_level,
            method_parameters=world.method_parameters,
            test_source_roots=world.test_source_roots,
            test_classpath_entries=world.test_dependencies,
            processor_entries=world.processor_entries,
            processor_names=world.processor_names,
            processor_options=world.processor_options,
            java_agents=world.java_agents,
            extra_jvm_arguments=(
                *world.extra_worker_jvm_arguments,
                *lombok_worker_jvm_arguments(
                    worker_java.major,
                    lombok_enabled=lombok_enabled,
                ),
            ),
            min_heap_mb=world.worker_min_heap_mb,
            max_heap_mb=world.worker_max_heap_mb,
            preserve_root_on_close=True,
        )
        return self._run_compiler_initialization_transaction(
            attempt=attempt,
            compiler=compiler,
            finish=lambda full: self._finish_build_world(
                attempt=attempt,
                full=full,
                compiler=compiler,
                world=world,
                build_jdk=build_jdk,
                workspace=workspace,
            ),
            reuse_workspace=workspace.reusable,
        )

    def _run_compiler_initialization_transaction(
        self,
        *,
        attempt: TestAttempt,
        compiler: PersistentJdtCompileSession,
        finish: Any,
        reuse_workspace: bool = False,
    ) -> _FastTestProject:
        published = False
        with self._lock:
            self._initializing_compiler = compiler
        try:
            full = (
                compiler.start(reuse_workspace=True, build_on_reuse=False)
                if reuse_workspace
                else compiler.start()
            )
            if reuse_workspace:
                changed = compiler.workspace_source_changes()
                if changed:
                    full = compiler.compile(changed)
            project = finish(full)
            with self._lock:
                attempt.require_not_cancelled()
                if self._initializing_compiler is not compiler:
                    raise FastTestManagerError(
                        "JDT_TEST_COMPILER_OWNERSHIP_LOST",
                        "The initializing JDT compiler ownership changed.",
                    )
                self._initializing_compiler = None
                published = True
            return project
        finally:
            with self._lock:
                if self._initializing_compiler is compiler:
                    self._initializing_compiler = None
            if not published:
                compiler.close()

    def _finish_build_world(
        self,
        *,
        attempt: TestAttempt,
        full: Any,
        compiler: PersistentJdtCompileSession,
        world: JavaTestBuildWorld,
        build_jdk: JavaToolchainCandidate,
        workspace: Any,
    ) -> _FastTestProject:
        attempt.require_not_cancelled()
        if not full.compile_ok:
            raise FastTestManagerError(
                "JDT_TEST_FULL_COMPILE_FAILED",
                "The initial JDT main/test FULL build failed.",
                context={"diagnostics": list(full.diagnostics)},
            )
        compiler.accept_baseline()
        compiler.save_source_index()
        workspace.mark_initialized()
        self._cache.save(world, build_jdk)
        attempt.require_not_cancelled()
        return _FastTestProject(
            build_system=world.build_system,
            project_root=world.project_root,
            module_root=world.module_root,
            build_jdk=build_jdk,
            test_java_executable=world.test_java_executable,
            test_framework=world.test_framework,
            test_working_directory=world.test_working_directory,
            runner_environment=dict(world.runner_environment),
            runner_jvm_arguments=world.test_jvm_arguments,
            test_run_order=world.test_run_order,
            compiler=compiler,
            runtime_classpath=(
                *(path for path in world.resource_roots if path.is_dir()),
                *(compiler.runtime_classpath(world.test_runtime_classpath) if world.modules else world.test_runtime_classpath),
            ),
            upstream_source_roots=world.upstream_source_roots,
            runner_support_provenance=dict(
                world.runner_support_provenance
            ),
            session_root=workspace.root,
            workspace_lease=workspace,
        )

    @staticmethod
    def _formal_resource_manifest(
        world: JavaTestBuildWorld,
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for prefix, root in (
            ("main", world.main_output),
            ("test", world.test_output),
        ):
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.suffix != ".class":
                    result[
                        f"{prefix}/{path.relative_to(root).as_posix()}"
                    ] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result


    def _select_build_jdk(
        self,
        preferences: Any,
        cwd: Path,
        log: Path,
        attempt: TestAttempt,
    ) -> JavaToolchainCandidate:
        for candidate in self._java.candidates(
            preferences=preferences,
            explicit_reference=None,
            for_build=True,
        ):
            if not candidate.has_runtime or not candidate.has_compiler:
                continue
            offset = log.stat().st_size if log.is_file() else 0
            result = self._supervisor.run(
                self._java.probe_spec(
                    candidate,
                    cwd=cwd,
                    output_capture=log,
                    operation_name="fast_test_java_probe",
                ),
                owner=attempt.owner,
            )
            if not result.succeeded:
                continue
            compiler_offset = log.stat().st_size if log.is_file() else 0
            compiler_result = self._supervisor.run(
                self._java.compiler_probe_spec(
                    candidate,
                    cwd=cwd,
                    output_capture=log,
                    operation_name="fast_test_javac_probe",
                ),
                owner=attempt.owner,
            )
            if not compiler_result.succeeded:
                continue
            java_output = self._read_log_segment(log, offset, compiler_offset)
            compiler_output = self._read_log_segment(
                log,
                compiler_offset,
                log.stat().st_size if log.is_file() else compiler_offset,
            )
            java_major = (
                JavaToolchainCandidate.parse_major_version_output(java_output)
                or candidate.major_version
            )
            compiler_major = (
                JavaToolchainCandidate.parse_compiler_major_version_output(
                    compiler_output
                )
                or candidate.compiler_major_version
            )
            if (
                java_major is not None
                and compiler_major is not None
                and java_major >= 8
                and compiler_major >= 8
            ):
                return replace(
                    candidate,
                    detected_major_version=java_major,
                    detected_compiler_major_version=compiler_major,
                )
        raise FastTestManagerError(
            "FAST_TEST_BUILD_JDK_UNAVAILABLE",
            "Fast Test requires a usable project build JDK.",
        )

    def _unshared_test_compiler_configuration(
        self,
        project: ET.Element,
        names: Sequence[str],
        *,
        compiler_model: dict[str, Any],
    ) -> list[str]:
        if not names:
            return []
        plugin = self._maven._find_build_plugin(
            project, "maven-compiler-plugin"
        )
        if plugin is None:
            return list(names)
        values: dict[str, list[str]] = {}
        direct = plugin.find("./{*}configuration")
        if direct is not None:
            for field in ("testSource", "testTarget", "testEncoding"):
                value = self._maven._config_text(direct, field)
                if value:
                    values.setdefault(field, []).append(value)
        for execution in plugin.findall("./{*}executions/{*}execution"):
            goals = {
                (goal.text or "").strip()
                for goal in execution.findall("./{*}goals/{*}goal")
            }
            if "testCompile" not in goals:
                continue
            configuration = execution.find("./{*}configuration")
            if configuration is None:
                continue
            for field in (
                "source",
                "target",
                "encoding",
                "optimize",
                "fork",
                "maxmem",
                "meminitial",
                "showWarnings",
            ):
                value = self._maven._config_text(configuration, field)
                if value:
                    values.setdefault(f"testCompile.{field}", []).append(
                        value
                    )

        source_level = int(compiler_model["source_level"])
        target_level = int(compiler_model["target_level"])
        expected_levels = {
            "testSource": source_level,
            "testTarget": target_level,
            "testCompile.source": source_level,
            "testCompile.target": target_level,
        }
        expected_encoding = self._maven._source_encoding(
            project, require_host_codec=False
        ).casefold()

        def shared(name: str) -> bool:
            observed = values.get(name, [])
            if not observed:
                return False
            if name in expected_levels:
                expected = expected_levels[name]
                normalized: set[int] = set()
                for value in observed:
                    try:
                        normalized.add(
                            int(
                                value.split(".", 2)[1]
                                if value.startswith("1.")
                                else value.split(".", 1)[0]
                            )
                        )
                    except (ValueError, IndexError):
                        return False
                return normalized == {expected}
            if name in {"testEncoding", "testCompile.encoding"}:
                return {value.casefold() for value in observed} == {
                    expected_encoding
                }
            if name in {"testOptimize", "testCompile.optimize"}:
                return {
                    value.casefold() for value in observed
                } <= {"true", "false"}
            if name in {"testFork", "testCompile.fork"}:
                return {
                    value.casefold() for value in observed
                } <= {"true", "false"}
            if name in {"testShowWarnings", "testCompile.showWarnings"}:
                return {value.casefold() for value in observed} == {"true"}
            if name in {
                "testMaxmem",
                "testCompile.maxmem",
                "testMeminitial",
                "testCompile.meminitial",
            }:
                return all(
                    parse_maven_memory_megabytes(value) is not None
                    for value in observed
                )
            return False

        return [name for name in names if not shared(str(name))]

    def _surefire_configuration(self, project: ET.Element) -> ET.Element | None:
        plugin = self._maven._find_build_plugin(project, "maven-surefire-plugin")
        if plugin is None:
            return None
        configuration = plugin.find("./{*}configuration")
        for execution in plugin.findall("./{*}executions/{*}execution"):
            goals = {
                (goal.text or "").strip()
                for goal in execution.findall("./{*}goals/{*}goal")
            }
            candidate = execution.find("./{*}configuration")
            if "test" in goals and candidate is not None:
                # The effective execution overrides matching plugin fields;
                # absent execution fields still inherit the plugin defaults.
                merged = ET.Element("configuration")
                overrides = {self._maven._local_name(item.tag) for item in candidate}
                if configuration is not None:
                    merged.extend(item for item in configuration
                                  if self._maven._local_name(item.tag) not in overrides)
                merged.extend(candidate)
                configuration = merged
        return configuration

    def _surefire_runtime_compatibility(
        self,
        project: ET.Element,
        unsupported: Sequence[str],
        *,
        attempt: TestAttempt,
    ) -> tuple[list[str], tuple[str, ...]]:
        if not unsupported:
            return [], ()
        configuration = self._surefire_configuration(project)
        if configuration is None:
            return list(unsupported), ()

        # Fast Test always receives explicit selectors, like -Dtest: these
        # override Surefire's discovery includes/excludes for classes too.
        modeled = set(_TEST_DISCOVERY_FILTERS) | {"additionalClasspathElements"}
        if self._maven._config_text(configuration, "runOrder") in {"alphabetical", "reversealphabetical"}:
            modeled.add("runOrder")
        arguments: list[str] = []
        single_method = (
            len(attempt.tests) == 1 and "#" in attempt.tests[0]
        )
        if single_method:
            modeled.update(
                {
                    "includes",
                    "excludes",
                    "forkCount",
                    "runOrder",
                    "parallel",
                    "reuseForks",
                    "skipAfterFailureCount",
                    "threadCount",
                }
            )

        fork_mode = self._maven._config_text(configuration, "forkMode")
        if single_method and fork_mode and fork_mode.casefold() == "once":
            modeled.add("forkMode")
        use_system_loader = self._maven._config_text(
            configuration, "useSystemClassLoader"
        )
        if (
            use_system_loader
            and use_system_loader.casefold() == "true"
        ):
            modeled.add("useSystemClassLoader")

        arg_line = self._maven._config_text(configuration, "argLine")
        if arg_line:
            arg_line = arg_line.replace("${jacocoArgLine}", "").replace(
                "@{jacocoArgLine}", ""
            )
            try:
                parsed = tuple(shlex.split(arg_line, posix=True))
            except ValueError:
                parsed = None
            if parsed is not None and not any(
                token.startswith("@{") or (
                    "${" in token and token != "${argLine}"
                )
                for token in parsed
            ):
                arguments.extend(
                    token for token in parsed if token != "${argLine}"
                )
                modeled.add("argLine")

        for field in ("systemProperties", "systemPropertyVariables"):
            container = configuration.find(f"./{{*}}{field}")
            if container is None:
                continue
            properties: list[tuple[str, str]] = []
            valid = True
            for item in container:
                name = self._maven._local_name(item.tag)
                if name == "property":
                    key = self._maven._config_text(item, "name")
                    value = self._maven._config_text(item, "value")
                else:
                    key = name
                    value = (item.text or "").strip()
                if (
                    not re.fullmatch(r"[A-Za-z0-9_.-]+", key)
                    or "\n" in value
                    or "\r" in value
                    or "\x00" in value
                ):
                    valid = False
                    break
                properties.append((key, value))
            if valid:
                arguments.extend(
                    f"-D{key}={value}" for key, value in properties
                )
                modeled.add(field)

        return (
            [value for value in unsupported if value not in modeled],
            tuple(dict.fromkeys(arguments)),
        )

    def _compiler_argument_compatibility(
        self,
        project: ET.Element,
        unsupported_test_configuration: Sequence[str],
    ) -> tuple[list[str], int, int]:
        compiler = self._maven._find_build_plugin(
            project, "maven-compiler-plugin"
        )
        main_profile = self._maven._compiler_argument_profile(
            self._maven._compiler_configurations(compiler)
        )
        test_profile = self._maven._compiler_argument_profile(
            self._maven._test_compiler_configurations(compiler)
        )
        remaining = list(unsupported_test_configuration)
        argument_configuration_names = {
            "testCompile.compilerArgs",
            "testCompile.compilerArgument",
            "testCompile.compilerArguments",
        }
        if test_profile.decisions and not test_profile.unresolved_arguments:
            remaining = [
                value
                for value in remaining
                if value not in argument_configuration_names
            ]
        unresolved = (
            *main_profile.unresolved_arguments,
            *test_profile.unresolved_arguments,
        )
        if unresolved:
            raise FastTestManagerError(
                "FAST_TEST_COMPILER_ARGUMENT_UNSUPPORTED",
                "Fast Test cannot reproduce one or more compiler arguments.",
                context={
                    "unresolved_argument_count": len(unresolved),
                    "argument_categories": ["compiler_extension"],
                },
            )
        worker_min_heap_mb = max(
            main_profile.worker_min_heap_mb,
            test_profile.worker_min_heap_mb,
        )
        worker_max_heap_mb = max(
            main_profile.worker_max_heap_mb,
            test_profile.worker_max_heap_mb,
        )
        compiler = self._maven._find_build_plugin(
            project, "maven-compiler-plugin"
        )
        main_structured = self._maven._structured_compiler_heap(
            self._maven._compiler_configurations(compiler)
        )
        test_structured = self._maven._structured_compiler_heap(
            self._maven._test_compiler_configurations(compiler)
        )
        worker_min_heap_mb = max(
            worker_min_heap_mb,
            main_structured[0],
            test_structured[0],
        )
        worker_max_heap_mb = max(
            worker_max_heap_mb,
            main_structured[1],
            test_structured[1],
        )
        if worker_min_heap_mb > worker_max_heap_mb:
            raise FastTestManagerError(
                "FAST_TEST_COMPILER_ARGUMENT_UNSUPPORTED",
                "Compiler JVM Xms exceeds Xmx.",
                context={
                    "unresolved_argument_count": 0,
                    "argument_categories": ["compiler_process_memory"],
                },
            )
        return remaining, worker_min_heap_mb, worker_max_heap_mb

    @staticmethod
    def _read_log_segment(path: Path, start: int, end: int) -> str:
        try:
            with path.open("rb") as stream:
                stream.seek(max(0, start))
                return stream.read(max(0, end - start)).decode(
                    "utf-8", errors="replace"
                )
        except OSError:
            return ""

    @staticmethod
    def _select_target_java(
        preferences: Any,
        build_jdk: JavaToolchainCandidate,
        *,
        target_level: int,
    ) -> Path:
        candidates: list[Path] = [build_jdk.home]
        for homes in preferences.jdk_homes_by_name.values():
            candidates.extend(homes)
        java_home = os.environ.get("JAVA_HOME")
        if java_home:
            candidates.append(Path(java_home))
        seen: set[str] = set()
        for raw in candidates:
            home = raw.expanduser().resolve(strict=False)
            key = os.path.normcase(str(home))
            if key in seen:
                continue
            seen.add(key)
            release = home / "release"
            try:
                text = release.read_text(encoding="utf-8")
            except OSError:
                continue
            major = JavaToolchainCandidate(
                home=home,
                java_executable=home / "bin/java",
                javac_executable=home / "bin/javac",
                source="fast_test_target",
            ).major_version
            platform_available = (
                (
                    (home / "jre/lib/rt.jar").is_file()
                    or (home / "lib/rt.jar").is_file()
                )
                if target_level == 8
                else (home / "lib/jrt-fs.jar").is_file()
            )
            if major == target_level and platform_available:
                return home
        raise FastTestManagerError(
            "FAST_TEST_TARGET_JDK_UNAVAILABLE",
            f"Fast Test requires a local Java {target_level} target JDK.",
        )

    def _select_maven(
        self,
        preferences: Any,
        root: Path,
        build_jdk: JavaToolchainCandidate,
        log: Path,
        attempt: TestAttempt,
    ) -> Any:
        environment = JavaToolchainResolver.maven_environment(build_jdk)
        for candidate in self._maven_tools.candidates(
            project_root=root, preferences=preferences
        ):
            result = self._supervisor.run(
                self._maven_tools.probe_spec(
                    candidate,
                    cwd=root,
                    environment=environment,
                    output_capture=log,
                ),
                owner=attempt.owner,
            )
            if result.succeeded:
                return candidate
        raise FastTestManagerError(
            "MAVEN_NOT_FOUND",
            "No usable Maven installation was found for Fast Test.",
        )

    @staticmethod
    def _paths(
        snapshot: dict[str, Any],
        field: str,
        module_root: Path,
    ) -> tuple[Path, ...]:
        values = snapshot.get(field)
        if not isinstance(values, list) or not all(
            isinstance(value, str) and value for value in values
        ):
            raise FastTestManagerError(
                "MAVEN_PROBE_OUTPUT_INVALID",
                f"The Probe field {field} is invalid.",
            )
        paths: list[Path] = []
        for value in values:
            path = Path(value)
            if not path.is_absolute():
                path = module_root / path
            resolved = path.expanduser().resolve(strict=False)
            if resolved.exists():
                paths.append(resolved)
        return tuple(dict.fromkeys(paths))

    @staticmethod
    def _freeze_sources(destination: Path, roots: Sequence[Path]) -> tuple[Path, ...]:
        frozen: list[Path] = []
        for index, root in enumerate(roots):
            target = destination / str(index)
            target.mkdir(parents=True, exist_ok=False)
            for source in sorted(root.rglob("*.java")):
                relative = source.relative_to(root)
                output = target / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, output)
            frozen.append(target)
        return tuple(frozen)

    @staticmethod
    def _freeze_trees(
        destination: Path, roots: Sequence[Path]
    ) -> tuple[Path, ...]:
        frozen: list[Path] = []
        for index, root in enumerate(roots):
            target = destination / str(index)
            target.mkdir(parents=True, exist_ok=False)
            if root.is_dir():
                for source in sorted(root.rglob("*")):
                    if not source.is_file():
                        continue
                    output = target / source.relative_to(root)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, output)
            frozen.append(target)
        return tuple(frozen)

    @staticmethod
    def _resource_inputs(roots: Sequence[Path]) -> tuple[Path, ...]:
        inputs: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            for source in sorted(root.rglob("*")):
                if source.is_symlink():
                    raise FastTestManagerError(
                        "FAST_TEST_RESOURCE_LINK_UNSUPPORTED",
                        "Fast Test resource roots may not contain links.",
                    )
                if source.is_file():
                    inputs.append(source)
                    if len(inputs) > 20_000:
                        raise FastTestManagerError(
                            "FAST_TEST_RESOURCE_LIMIT_EXCEEDED",
                            "Fast Test resource inputs exceed the v1 limit.",
                        )
        return tuple(inputs)

    @staticmethod
    def _resolve_sources(
        project: _FastTestProject,
        requested: Sequence[str],
    ) -> tuple[Path, ...]:
        if len(requested) > 16:
            raise FastTestManagerError(
                "INVALID_SOURCE_FILES",
                "One Fast Test accepts at most 16 explicit source files.",
            )
        result: list[Path] = []
        allowed = (
            *project.compiler.source_roots,
            *project.compiler.test_source_roots,
        )
        if isinstance(project.compiler, ModuleCompileSession):
            allowed = project.compiler.workspace_source_roots()
        upstream_roots = tuple(
            getattr(project, "upstream_source_roots", ())
        )
        for value in requested:
            source = (project.project_root / value).resolve(strict=False)
            is_target_source = any(
                source.is_relative_to(root) for root in allowed
            )
            is_upstream_source = any(
                source.is_relative_to(root)
                for root in upstream_roots
            )
            if (
                source.suffix != ".java"
                or not (is_target_source or is_upstream_source)
            ):
                raise FastTestManagerError(
                    "SOURCE_OUTSIDE_TEST_BUILD_WORLD",
                    "A Fast Test source is outside the target/upstream Test Build World.",
                )
            if source.is_symlink():
                raise FastTestManagerError(
                    "SOURCE_LINK_UNSUPPORTED",
                    "A Fast Test source may not be a symbolic link.",
                )
            if is_target_source:
                result.append(source)
        return tuple(dict.fromkeys(result))

    def _drop_project(self) -> None:
        project = self._project
        self._project = None
        if project is not None:
            project.close()

    @staticmethod
    def _retain_test_attempt(
        project: _FastTestProject,
        directory: Path,
        *,
        failed: bool,
    ) -> None:
        project.test_attempts.append((directory, failed))
        failures = [item for item in project.test_attempts if item[1]]
        successes = [item for item in project.test_attempts if not item[1]]
        retained = set(failures[-8:] + successes[-1:])
        next_items: list[tuple[Path, bool]] = []
        for item in project.test_attempts:
            if item in retained:
                next_items.append(item)
            else:
                shutil.rmtree(item[0], ignore_errors=True)
        project.test_attempts[:] = next_items

    @staticmethod
    def _error_next_step(error: BaseException) -> str:
        code = str(getattr(error, "error_code", ""))
        if code == "UNDECLARED_SOURCE_CHANGES":
            return "Retry test and include every edited main/test Java source_file."
        if code in {"TEST_TIMEOUT", "TEST_OUTPUT_LIMIT_EXCEEDED"}:
            return "Narrow the explicit test selection or adjust the bounded timeout/output."
        if code == "FAST_TEST_BOOTSTRAP_TIMEOUT":
            return (
                "Retry the Fast Test Bootstrap; dependency caches may now be "
                "partially warmed. If it repeats, inspect bootstrap_log_tail."
            )
        if code in {
            "MAVEN_PROBE_INTEGRITY_MISMATCH",
            "JDT_CANDIDATE_INTEGRITY_MISMATCH",
            "JDT_CANDIDATE_INSTALL_FAILED",
        }:
            return (
                "Repair or reinstall the bundled joLink build assets, then "
                "retry without changing the project or test input."
            )
        if code == "TEST_FRAMEWORK_UNAVAILABLE":
            return "Use the project's formal test command or add its matching JUnit runtime launcher."
        if "UNSUPPORTED" in code or "UNAVAILABLE" in code:
            return "Use the project's formal test workflow for this unsupported Build World."
        return "Inspect the structured error, correct the project/test input, and retry."

    @staticmethod
    def _bootstrap_timeout_error(
        operation: Any,
        *,
        attempt: TestAttempt,
        stage: str,
        log: Path,
    ) -> FastTestManagerError:
        termination = getattr(operation, "termination", None)
        return FastTestManagerError(
            "FAST_TEST_BOOTSTRAP_TIMEOUT",
            "The one-time Fast Test Build World Bootstrap timed out.",
            context={
                "stage": stage,
                "timed_out": True,
                "timeout_ms": round(
                    float(
                        getattr(
                            attempt,
                            "bootstrap_timeout_seconds",
                            _DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS,
                        )
                    )
                    * 1000
                ),
                "termination_forced": bool(
                    getattr(termination, "forced", False)
                ),
                "remaining_process_count": len(
                    getattr(termination, "remaining_pids", ())
                ),
                "bootstrap_log_tail": _redacted_build_log_tail(log),
            },
        )

    def _cleanup_pending_roots(self) -> None:
        with self._lock:
            roots = tuple(self._pending_roots)
            self._pending_roots.clear()
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)

    def close(self, *, deadline: float | None = None) -> bool:
        effective_deadline = (
            deadline if deadline is not None else time.monotonic() + 5.0
        )
        with self._lock:
            self._closed = True
            active = self._active
            compiler = (
                self._project.compiler
                if self._project is not None
                else self._initializing_compiler
            )
            if active is not None and not active.done.is_set():
                active.cancel_requested = True
        if compiler is not None and active is not None and not active.done.is_set():
            compiler.interrupt("FAST_TEST_SHUTDOWN")
        if active is not None and not active.done.is_set():
            self._supervisor.cancel(
                active.owner, deadline=effective_deadline
            )
            active.done.wait(max(0.0, effective_deadline - time.monotonic()))
        if active is not None and not active.done.is_set():
            return False
        self._drop_project()
        self._cleanup_pending_roots()
        return self._runner.close(deadline=effective_deadline)


__all__ = [
    "FastTestManager",
    "FastTestManagerError",
    "TestAttempt",
]
