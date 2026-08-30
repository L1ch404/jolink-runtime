"""Persistent JDT Fast Test session for one headless Maven project."""

from __future__ import annotations

import hashlib
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

from .contracts import BuildOperationSpec, LaunchIntent
from .fast_compile import fast_compile_fingerprint
from .fast_test import FastTestError, FastTestRunner
from .idea_environment import IdeaEnvironmentImporter
from .jdt_compile_session import (
    JdtCandidate,
    JdtCompileError,
    PersistentJdtCompileSession,
    discover_target_system_entries,
    lombok_worker_jvm_arguments,
)
from .maven import MavenBuildSystemAdapter, MavenResolutionError
from .maven_probe import MavenProbeError, ProductMavenProbe
from .gradle_probe import (
    GradleProbeError,
    ProductGradleProbe,
    gradle_configuration_environment_names,
    gradle_configuration_inputs,
    wrapper_version,
)
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
    build_input_manifest,
)


_HELP_PLUGIN_GOAL = (
    "org.apache.maven.plugins:maven-help-plugin:3.2.0:effective-pom"
)
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
    text = raw.decode("utf-8", errors="replace")
    result: list[str] = []
    for line in text.splitlines()[-80:]:
        clean = _BUILD_LOG_ANSI.sub("", line)
        if _BUILD_LOG_SECRET.search(clean):
            result.append("<redacted sensitive build log line>")
        else:
            result.append(_BUILD_LOG_USERINFO.sub(r"\1<redacted>@", clean))
    return result


def _resource_tree_fingerprint(roots: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for root in roots:
        digest.update(str(root).encode("utf-8", errors="surrogateescape"))
        if not root.is_dir():
            digest.update(b"<absent>")
            continue
        for source in sorted(root.rglob("*")):
            if source.is_symlink():
                digest.update(b"<link>")
                digest.update(source.relative_to(root).as_posix().encode("utf-8"))
                continue
            if not source.is_file():
                continue
            digest.update(source.relative_to(root).as_posix().encode("utf-8"))
            digest.update(hashlib.sha256(source.read_bytes()).digest())
    return digest.hexdigest()


def _java_source_roots_fingerprint(roots: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for root in sorted(
        (path.resolve(strict=False) for path in roots),
        key=lambda path: os.path.normcase(str(path)),
    ):
        digest.update(str(root).encode("utf-8", errors="surrogateescape"))
        if not root.is_dir():
            digest.update(b"<absent>")
            continue
        for source in sorted(root.rglob("*.java")):
            digest.update(source.relative_to(root).as_posix().encode("utf-8"))
            if source.is_symlink():
                digest.update(b"<link>")
            elif source.is_file():
                digest.update(hashlib.sha256(source.read_bytes()).digest())
            else:
                digest.update(b"<unreadable>")
    return digest.hexdigest()


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
    javac_executable: Path
    compiler: PersistentJdtCompileSession
    runtime_classpath: tuple[Path, ...]
    configuration_inputs: tuple[Path, ...]
    configuration_environment_names: tuple[str, ...]
    dependency_entries: tuple[Path, ...]
    configuration_fingerprint: str
    resource_roots: tuple[Path, ...]
    resource_fingerprint: str
    upstream_source_roots: tuple[Path, ...]
    upstream_source_fingerprint: str
    runner_support_provenance: dict[str, Any]
    session_root: Path
    test_attempts: list[tuple[Path, bool]] = field(default_factory=list)

    def is_fresh(self) -> bool:
        try:
            current = fast_compile_fingerprint(
                configuration_inputs=self.configuration_inputs,
                configuration_environment_names=(
                    self.configuration_environment_names
                ),
                javac_executable=self.javac_executable,
                compile_classpath=self.dependency_entries,
            )
            resources_match = (
                self.resource_fingerprint
                == _resource_tree_fingerprint(self.resource_roots)
            )
            upstream_sources_match = (
                self.upstream_source_fingerprint
                == _java_source_roots_fingerprint(
                    self.upstream_source_roots
                )
            )
        except OSError:
            return False
        return (
            self.configuration_fingerprint == current
            and resources_match
            and upstream_sources_match
        )

    def close(self) -> bool:
        settled = self.compiler.close()
        shutil.rmtree(self.session_root, ignore_errors=True)
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
        short_wait_seconds: float = 2.0,
    ) -> dict[str, Any]:
        root = project_path.expanduser().resolve(strict=True)
        timeout = min(max(float(timeout_seconds), 0.1), 600.0)
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
                undeclared = set(
                    project.compiler.workspace_source_changes()
                ) - set(selected_sources)
                if undeclared:
                    raise FastTestManagerError(
                        "UNDECLARED_SOURCE_CHANGES",
                        "Fast Test source_files omitted edited Java sources.",
                        context={"undeclared_source_count": len(undeclared)},
                    )
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
                        working_directory=project.test_working_directory,
                        environment=project.runner_environment,
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
                source_changes_pending = bool(
                    project.compiler.workspace_source_changes()
                )
                post_freshness_started = time.monotonic()
                build_world_changes_pending = not project.is_fresh()
                attempt.post_test_freshness_ms = round(
                    (time.monotonic() - post_freshness_started) * 1000, 1
                )
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
                    "source_changes_pending": source_changes_pending,
                    "build_world_changes_pending": (
                        build_world_changes_pending
                    ),
                    "suggested_next_step": (
                        "Source or Build World changed while the TestRunLease "
                        "was active; retry with every edited source_file "
                        "before reload."
                        if source_changes_pending or build_world_changes_pending
                        else "Inspect failed_tests, edit the relevant source, and "
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
            and project.is_fresh()
        ):
            return project
        self._drop_project()
        attempt.state = "bootstrapping"
        project = provider.bootstrap(self, attempt)
        self._project = project
        return project

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
        workspace = self._maven.resolve_workspace(attempt.project_path)
        attempt.require_not_cancelled()
        module = self._select_test_module(workspace, attempt)
        if module.packaging != "jar":
            raise FastTestManagerError(
                "FAST_TEST_PACKAGING_UNSUPPORTED",
                "Fast Test v1 requires Maven jar packaging.",
            )
        build_jdk = self._select_build_jdk(
            preferences, workspace.build_root, log, attempt
        )
        maven = self._select_maven(
            preferences, workspace.project_root, build_jdk, log, attempt
        )
        attempt.require_not_cancelled()
        intent = LaunchIntent(
            source="maven_fast_test",
            launch_name="fast-test",
            launch_type="test",
            main_class="",
            working_directory=module.directory,
            ide_module_name=module.relative_path,
            build_before_run=True,
        )
        self._maven.create_execution_plan(
            workspace=workspace,
            module=module,
            intent=intent,
            maven=maven,
            build_jdk=build_jdk,
            preferences=preferences,
            attempt_directory=bootstrap,
        )
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
                module=module,
                build_jdk=build_jdk,
                local_repository=local_repository,
                offline=offline,
                effective_pom=effective_pom,
                log=log,
            )
        finally:
            prepared_probe.settings_file.unlink(missing_ok=True)
        attempt.require_not_cancelled()
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
        attempt.require_not_cancelled()
        session_root = Path(
            tempfile.mkdtemp(prefix="jolink-gradle-fast-test-session-")
        )
        with self._lock:
            self._pending_roots.add(session_root)
        bootstrap = session_root / "bootstrap"
        bootstrap.mkdir(mode=0o700)
        log = bootstrap / "gradle-test-bootstrap.log"
        project = attempt.project_path
        if (project / "buildSrc").exists() or (
            project / "build-logic"
        ).exists():
            raise FastTestManagerError(
                "GRADLE_BUILD_LOGIC_UNSUPPORTED",
                "Gradle buildSrc/build-logic is not supported in v0.1.",
            )
        preferences = self._idea.import_preferences(project)
        build_jdk = self._select_build_jdk(
            preferences, project, log, attempt
        )
        probe = ProductGradleProbe.load()
        version = wrapper_version(project)
        if version not in probe.supported_versions:
            raise FastTestManagerError(
                "GRADLE_VERSION_UNSUPPORTED",
                "This Gradle Wrapper version has no product evidence.",
            )
        wrapper = project / ("gradlew.bat" if os.name == "nt" else "gradlew")
        if not wrapper.is_file():
            raise FastTestManagerError(
                "GRADLE_WRAPPER_UNAVAILABLE",
                "The Gradle Wrapper executable is unavailable.",
            )
        main_root = project / "src/main/java"
        test_root = project / "src/test/java"
        resource_roots = (
            project / "src/main/resources",
            project / "src/test/resources",
        )
        before = build_input_manifest(
            (main_root, test_root), resource_roots
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
                    ),
                    cwd=project,
                    environment=environment,
                    timeout_seconds=max(120.0, attempt.timeout_seconds),
                    output_capture=log,
                    max_output_bytes=16 * 1024 * 1024,
                    operation_name="gradle_fast_test_bootstrap",
                ),
                owner=attempt.owner,
            )
            attempt.require_not_cancelled()
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
                    "The one-time Gradle classes/testClasses Bootstrap failed.",
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
            project_root=project,
            configuration_inputs=configuration_inputs,
            runner_environment=environment,
            configuration_environment_names=(
                configuration_environment_names
            ),
        )
        if before != world.expected_input_manifest:
            raise FastTestManagerError(
                "SOURCE_CHANGED_DURING_TEST_BOOTSTRAP",
                "Gradle Build World inputs changed during Bootstrap.",
            )
        result = self._start_build_world(
            attempt=attempt,
            world=world,
            build_jdk=build_jdk,
            session_root=session_root,
        )
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
        module: Any,
        build_jdk: JavaToolchainCandidate,
        local_repository: Path,
        offline: bool,
        effective_pom: Path,
        log: Path,
    ) -> dict[str, Any]:
        attempt.require_not_cancelled()
        workspace_modules = tuple(
            getattr(workspace, "modules", (module,))
        )
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
        if len(workspace_modules) > 1 and module.relative_path != ".":
            command.extend(["-pl", module.relative_path, "-am"])
        command.extend(
            [
                "-DskipTests=false",
                "-Dmaven.test.skip=false",
                "-Dtest.skip=false",
                "test-compile",
                prepared_probe.goal,
                (
                    "-Djolink.probe.outputDirectory="
                    f"{prepared_probe.output_directory}"
                ),
                _HELP_PLUGIN_GOAL,
                f"-Doutput={effective_pom}",
            ]
        )
        before = self._workspace_source_fingerprint(workspace_modules)
        try:
            operation = self._supervisor.run(
                BuildOperationSpec(
                    argv=tuple(command),
                    cwd=workspace.build_root,
                    environment=JavaToolchainResolver.maven_environment(build_jdk),
                    timeout_seconds=max(120.0, attempt.timeout_seconds),
                    output_capture=log,
                    max_output_bytes=16 * 1024 * 1024,
                    operation_name="maven_fast_test_bootstrap",
                ),
                owner=attempt.owner,
            )
            attempt.require_not_cancelled()
            if operation.output_limit_exceeded:
                raise FastTestManagerError(
                    "FAST_TEST_BOOTSTRAP_OUTPUT_LIMIT_EXCEEDED",
                    "The Maven Fast Test Bootstrap exceeded the 16 MiB log limit.",
                )
            if not operation.succeeded:
                raise FastTestManagerError(
                    "FAST_TEST_BOOTSTRAP_FAILED",
                    "The one-time Maven test-compile Bootstrap failed.",
                    context={
                        "return_code": operation.return_code,
                        "bootstrap_log_tail": _redacted_build_log_tail(log),
                    },
                )
            if before != self._workspace_source_fingerprint(
                workspace_modules
            ):
                raise FastTestManagerError(
                    "SOURCE_CHANGED_DURING_TEST_BOOTSTRAP",
                    "Java sources changed during the Fast Test Bootstrap.",
                )
            snapshot = probe.load_snapshot(
                prepared_probe, module_root=module.directory
            )
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
        if not main_roots or not test_roots:
            raise FastTestManagerError(
                "FAST_TEST_SOURCE_ROOTS_UNAVAILABLE",
                "The Maven Probe found no main or test Java source roots.",
            )
        attempt.require_not_cancelled()
        main_output = Path(str(snapshot["outputDirectory"])).resolve(strict=True)
        test_output = Path(str(snapshot["testOutputDirectory"])).resolve(strict=True)
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
        compiler_model = self._maven._compiler_model(
            effective_project,
            build_jdk=build_jdk,
            runtime_jdk=build_jdk,
        )
        for label, processing in (
            ("main", main_processing),
            ("test", test_processing),
        ):
            if processing.get("discoveryMode") not in {
                "DISABLED",
                "IMPLICIT_COMPILE_CLASSPATH",
            }:
                raise FastTestManagerError(
                    "FAST_TEST_PROCESSOR_MODEL_UNSUPPORTED",
                    f"Fast Test cannot reproduce the {label} Processor discovery mode.",
                )
            if processing.get("options"):
                raise FastTestManagerError(
                    "FAST_TEST_PROCESSOR_OPTIONS_UNSUPPORTED",
                    "Fast Test v1 does not reproduce Maven Processor -A options.",
                )
        unsupported_surefire = test_runtime.get(
            "unsupportedSurefireConfigurationNames", []
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
            if value != "testCompile.parameters"
        ]
        unsupported_test_compiler = self._unshared_test_compiler_configuration(
            effective_project,
            unsupported_test_compiler,
            compiler_model=compiler_model,
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
        main_processor_paths = tuple(
            Path(str(value)).resolve(strict=True)
            for value in main_processing.get(
                "processorProviderArtifactPaths", []
            )
        )
        test_processor_paths = tuple(
            Path(str(value)).resolve(strict=True)
            for value in test_processing.get(
                "processorProviderArtifactPaths", []
            )
        )
        if set(test_processor_paths) != set(main_processor_paths):
            raise FastTestManagerError(
                "FAST_TEST_PROCESSOR_MODEL_UNSUPPORTED",
                "Fast Test v1 requires identical main/test Processor paths.",
            )
        attempt.require_not_cancelled()
        lombok = tuple(
            path
            for path in main_processor_paths
            if self._maven._jdt_dependency_facts(path)[2]
        )
        processor_entries = tuple(
            path
            for path in main_processor_paths
            if path not in lombok
        )
        method_parameters = self._compiler_method_parameters(
            effective_project
        )
        if (
            compiler_model["source_level"]
            != compiler_model["target_level"]
            or compiler_model["target_level"] not in {8, 11}
        ):
            raise FastTestManagerError(
                "FAST_TEST_JAVA_LEVEL_UNSUPPORTED",
                "Fast Test supports equal Java 8 or 11 source/target levels.",
            )
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
            java_agents=tuple(f"{path}=ECJ" for path in lombok),
            extra_worker_jvm_arguments=(),
            test_java_executable=build_jdk.java_executable,
            test_framework=None,
            test_working_directory=module.directory,
            test_classes_directories=(test_output,),
            runner_environment={},
            javac_executable=build_jdk.javac_executable,
            configuration_inputs=configuration_inputs,
            configuration_environment_names=(),
            upstream_source_roots=upstream_source_roots,
            runner_support_provenance=runner_support_provenance,
        )
        return self._start_build_world(
            attempt=attempt,
            world=world,
            build_jdk=build_jdk,
            session_root=session_root,
        )

    def _start_build_world(
        self,
        *,
        attempt: TestAttempt,
        world: JavaTestBuildWorld,
        build_jdk: JavaToolchainCandidate,
        session_root: Path,
    ) -> _FastTestProject:
        if world.expected_input_manifest and build_input_manifest(
            (*world.main_source_roots, *world.test_source_roots),
            world.resource_roots,
        ) != world.expected_input_manifest:
            raise FastTestManagerError(
                "SOURCE_CHANGED_DURING_TEST_BOOTSTRAP",
                "Build World inputs changed before private snapshot.",
            )
        main_snapshots = self._freeze_sources(
            session_root / "main-source-snapshot",
            world.main_source_roots,
        )
        test_snapshots = self._freeze_sources(
            session_root / "test-source-snapshot",
            world.test_source_roots,
        )
        resource_snapshots = self._freeze_trees(
            session_root / "resource-snapshot", world.resource_roots
        )
        if world.expected_input_manifest:
            frozen_manifest = build_input_manifest(
                (*main_snapshots, *test_snapshots), resource_snapshots
            )
            current_manifest = build_input_manifest(
                (*world.main_source_roots, *world.test_source_roots),
                world.resource_roots,
            )
            if (
                frozen_manifest != world.expected_input_manifest
                or current_manifest != world.expected_input_manifest
            ):
                raise FastTestManagerError(
                    "SOURCE_CHANGED_DURING_TEST_BOOTSTRAP",
                    "Build World inputs changed during private snapshot.",
                )
        attempt.require_not_cancelled()
        candidate = JdtCandidate.load_product()
        worker_java = candidate.select_worker_java(
            (build_jdk.home, world.target_java_home)
        )
        lombok_enabled = bool(world.java_agents)
        compiler = PersistentJdtCompileSession(
            root=session_root / "compile-session",
            candidate=candidate,
            worker_java_home=worker_java.home,
            source_roots=world.main_source_roots,
            baseline_source_roots=main_snapshots,
            classpath_entries=(
                *discover_target_system_entries(
                    world.target_java_home, world.source_level
                ),
                *world.main_dependencies,
            ),
            source_encoding=world.source_encoding,
            source_level=world.source_level,
            method_parameters=world.method_parameters,
            test_source_roots=world.test_source_roots,
            baseline_test_source_roots=test_snapshots,
            test_classpath_entries=world.test_dependencies,
            baseline_main_output=world.main_output,
            baseline_test_output=world.test_output,
            processor_entries=world.processor_entries,
            java_agents=world.java_agents,
            extra_jvm_arguments=(
                *world.extra_worker_jvm_arguments,
                *lombok_worker_jvm_arguments(
                    worker_java.major,
                    lombok_enabled=lombok_enabled,
                ),
            ),
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
                session_root=session_root,
            ),
        )

    def _run_compiler_initialization_transaction(
        self,
        *,
        attempt: TestAttempt,
        compiler: PersistentJdtCompileSession,
        finish: Any,
    ) -> _FastTestProject:
        published = False
        with self._lock:
            self._initializing_compiler = compiler
        try:
            full = compiler.start()
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
        session_root: Path,
    ) -> _FastTestProject:
        attempt.require_not_cancelled()
        if not full.compile_ok:
            raise FastTestManagerError(
                "JDT_TEST_FULL_COMPILE_FAILED",
                "The initial JDT main/test FULL build failed.",
                context={"diagnostics": list(full.diagnostics)},
            )
        # Lazy import avoids the adapters.java package importing JavaRuntime
        # while JavaRuntime itself is wiring the Fast Test manager.
        from ..adapters.java.classfile import compare_class_output_tier1

        main_compatibility = compare_class_output_tier1(
            world.main_output, compiler.output_directory
        )
        test_compatibility = compare_class_output_tier1(
            world.test_output, compiler.test_output_directory
        )
        attempt.require_not_cancelled()
        if not main_compatibility["compatible"] or not test_compatibility[
            "compatible"
        ]:
            raise FastTestManagerError(
                "JDT_TEST_BASELINE_INCOMPATIBLE",
                "The JDT main/test baseline is not compatible with the build authority.",
                context={
                    "main": main_compatibility,
                    "test": test_compatibility,
                },
            )
        compiler.accept_baseline()
        attempt.require_not_cancelled()
        if world.native_resource_oracle_required:
            formal_resources = self._formal_resource_manifest(world)
            native_resources = compiler.native_full_resource_manifest
            if not native_resources or native_resources != formal_resources:
                raise FastTestManagerError(
                    "JDT_TEST_RESOURCE_BASELINE_INCOMPATIBLE",
                    "JDT Processor resources differ from the build authority.",
                )
        all_dependencies = tuple(
            dict.fromkeys(
                (
                    *world.main_dependencies,
                    *world.test_dependencies,
                    *world.test_runtime_classpath,
                )
            )
        )
        fingerprint = fast_compile_fingerprint(
            configuration_inputs=world.configuration_inputs,
            configuration_environment_names=(
                world.configuration_environment_names
            ),
            javac_executable=world.javac_executable,
            compile_classpath=all_dependencies,
        )
        resource_fingerprint = _resource_tree_fingerprint(
            world.resource_roots
        )
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
            javac_executable=world.javac_executable,
            compiler=compiler,
            runtime_classpath=world.test_runtime_classpath,
            configuration_inputs=world.configuration_inputs,
            configuration_environment_names=(
                world.configuration_environment_names
            ),
            dependency_entries=all_dependencies,
            configuration_fingerprint=fingerprint,
            resource_roots=world.resource_roots,
            resource_fingerprint=resource_fingerprint,
            upstream_source_roots=world.upstream_source_roots,
            upstream_source_fingerprint=_java_source_roots_fingerprint(
                world.upstream_source_roots
            ),
            runner_support_provenance=dict(
                world.runner_support_provenance
            ),
            session_root=session_root,
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

    @staticmethod
    def _select_test_module(workspace: Any, attempt: TestAttempt) -> Any:
        modules = [
            module for module in workspace.modules if module.packaging == "jar"
        ]
        if len(modules) == 1:
            return modules[0]
        if not modules:
            raise FastTestManagerError(
                "FAST_TEST_PACKAGING_UNSUPPORTED",
                "Fast Test requires a Maven jar module.",
            )

        test_classes = {
            selector.partition("#")[0] for selector in attempt.tests
        }
        matches = []
        for module in modules:
            if all(
                (
                    module.directory
                    / "src/test/java"
                    / Path(*class_name.split("."))
                ).with_suffix(".java").is_file()
                for class_name in test_classes
            ):
                matches.append(module)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise FastTestManagerError(
                "FAST_TEST_MODULE_AMBIGUOUS",
                "Fast Test selectors match multiple Maven jar modules.",
                context={
                    "candidate_modules": [
                        module.relative_path for module in matches[:16]
                    ]
                },
            )

        requested_paths = tuple(
            (attempt.project_path / raw).resolve(strict=False)
            for raw in attempt.source_files
        )
        if requested_paths:
            source_matches = [
                module
                for module in modules
                if all(
                    path.is_relative_to(module.directory)
                    for path in requested_paths
                )
            ]
            if len(source_matches) == 1:
                return source_matches[0]
        raise FastTestManagerError(
            "FAST_TEST_MODULE_AMBIGUOUS",
            "Fast Test selectors do not identify one Maven jar module.",
            context={
                "candidate_modules": [
                    module.relative_path for module in modules[:16]
                ]
            },
        )

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
            for field in ("source", "target", "encoding"):
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
            return False

        return [name for name in names if not shared(str(name))]

    def _compiler_method_parameters(self, project: Any) -> bool:
        plugin = self._maven._find_build_plugin(
            project, "maven-compiler-plugin"
        )
        direct: list[str] = []
        main: list[str] = []
        test: list[str] = []
        if plugin is not None:
            configuration = plugin.find("./{*}configuration")
            if configuration is not None:
                value = self._maven._config_text(
                    configuration, "parameters"
                )
                if value:
                    direct.append(value)
            for execution in plugin.findall(
                "./{*}executions/{*}execution"
            ):
                goals = {
                    (goal.text or "").strip()
                    for goal in execution.findall("./{*}goals/{*}goal")
                }
                configuration = execution.find("./{*}configuration")
                if configuration is None:
                    continue
                value = self._maven._config_text(
                    configuration, "parameters"
                )
                if not value:
                    continue
                if "compile" in goals:
                    main.append(value)
                if "testCompile" in goals:
                    test.append(value)
        property_element = project.find(
            "./{*}properties/{*}maven.compiler.parameters"
        )
        if property_element is not None and (property_element.text or "").strip():
            direct.append((property_element.text or "").strip())

        def resolve(values: Sequence[str]) -> bool:
            normalized = {value.casefold() for value in values}
            if not normalized:
                return False
            if not normalized <= {"true", "false"} or len(normalized) != 1:
                raise FastTestManagerError(
                    "FAST_TEST_COMPILER_CONFIGURATION_UNSUPPORTED",
                    "Maven method-parameter metadata configuration is ambiguous.",
                )
            return normalized == {"true"}

        main_value = resolve((*direct, *main))
        test_value = resolve((*direct, *test))
        if main_value != test_value:
            raise FastTestManagerError(
                "FAST_TEST_COMPILER_CONFIGURATION_UNSUPPORTED",
                "Fast Test requires equal main/test method-parameter metadata.",
            )
        return main_value

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
    def _source_fingerprint(module_root: Path) -> str:
        digest = hashlib.sha256()
        for root in (
            module_root / "src/main/java",
            module_root / "src/test/java",
        ):
            if not root.is_dir():
                continue
            for source in sorted(root.rglob("*.java")):
                digest.update(source.relative_to(module_root).as_posix().encode())
                digest.update(hashlib.sha256(source.read_bytes()).digest())
        return digest.hexdigest()

    @staticmethod
    def _workspace_source_fingerprint(modules: Sequence[Any]) -> str:
        roots = tuple(
            root
            for module in modules
            for root in (
                module.directory / "src/main/java",
                module.directory / "src/test/java",
            )
            if root.is_dir()
        )
        return _java_source_roots_fingerprint(roots)

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
        expected_upstream_fingerprint = getattr(
            project, "upstream_source_fingerprint", None
        )
        if (
            expected_upstream_fingerprint is not None
            and _java_source_roots_fingerprint(upstream_roots)
            != expected_upstream_fingerprint
        ):
            raise FastTestManagerError(
                "SOURCE_CHANGED_DURING_TEST_BOOTSTRAP",
                "An upstream Reactor source changed after Maven Bootstrap.",
            )
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
        if code == "TEST_FRAMEWORK_UNAVAILABLE":
            return "Use the project's formal test command or add its matching JUnit runtime launcher."
        if "UNSUPPORTED" in code or "UNAVAILABLE" in code:
            return "Use the project's formal test workflow for this unsupported Build World."
        return "Inspect the structured error, correct the project/test input, and retry."

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
