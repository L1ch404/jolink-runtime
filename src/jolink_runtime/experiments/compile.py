"""Internal command for Java compiler-model experiments.

This command is intentionally absent from the MCP schema and package scripts.
It freezes a private workspace copy, obtains a fresh Maven compile baseline,
and compares it with repeated direct ``javac`` output. joLink never publishes
the private classes into the user's Maven output and never opens JDWP; Maven
and javac still execute trusted project build code rather than an OS sandbox.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Sequence

from jolink_runtime.experiments.lombok_processor import (
    LombokExperimentError,
    LombokExperimentPlan,
    LombokExperimentPlanner,
    LombokExperimentRunner,
    compare_class_outputs,
    discover_lombok_configuration,
    freeze_plan_artifacts,
    monotonic_ms,
    scan_class_hashes,
    validate_compiler_environment,
)
from jolink_runtime.launch.contracts import BuildOperationSpec, LaunchIntent
from jolink_runtime.launch.idea_environment import (
    IdeaBuildPreferences,
    IdeaEnvironmentImporter,
)
from jolink_runtime.launch.maven import (
    MavenBuildSystemAdapter,
    MavenExecutionPlan,
    MavenModule,
    MavenResolutionError,
    MavenWorkspace,
)
from jolink_runtime.launch.process_supervisor import (
    AttemptToken,
    OperationResult,
    ProcessSupervisor,
)
from jolink_runtime.launch.toolchain import (
    JavaToolchainCandidate,
    JavaToolchainResolver,
    MavenToolCandidate,
    MavenToolResolver,
)


_SNAPSHOT_ROOT_METADATA = frozenset({".git", ".hg", ".jolink", ".svn"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare a fresh Maven Lombok compile with isolated repeated "
            "module-full javac output. No output is published."
        )
    )
    parser.add_argument("--project-path", required=True, type=Path)
    parser.add_argument(
        "--module",
        help="Maven relative path, artifactId, directory name, or module name.",
    )
    parser.add_argument(
        "--strategy",
        choices=("module_full_javac",),
        default="module_full_javac",
    )
    parser.add_argument("--java-home", type=Path)
    parser.add_argument("--maven", type=Path)
    parser.add_argument("--attempt-root", type=Path)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument(
        "--maven-baseline-timeout-seconds",
        type=float,
        default=900.0,
    )
    parser.add_argument(
        "--metadata-timeout-seconds",
        type=float,
        default=300.0,
    )
    parser.add_argument(
        "--external-baseline-output",
        type=Path,
        help=(
            "Optional diagnostic-only class output. It is always marked "
            "external_unverified and never used as a promotion gate."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    started = time.monotonic()
    supervisor = ProcessSupervisor()
    public_attempt_id = f"experiment_{uuid.uuid4().hex[:12]}"
    attempt_root: Path | None = None
    original_output_fingerprints: dict[Path, str] | None = None
    original_module: MavenModule | None = None
    try:
        _validate_arguments(args)
        validate_compiler_environment()
        original_project = args.project_path.expanduser().resolve(strict=True)
        _validate_attempt_root_location(args.attempt_root, original_project)
        attempt_root = _attempt_root(args.attempt_root)
        preferences = IdeaEnvironmentImporter().import_preferences(
            original_project
        )
        adapter = MavenBuildSystemAdapter()
        original_workspace = adapter.resolve_workspace(original_project)
        _validate_p0_workspace(original_workspace)
        _validate_standard_workspace_outputs(original_workspace)
        _static_maven_preflight(adapter, original_workspace)
        original_module = _select_module(original_workspace, args.module)
        original_output_fingerprints = _workspace_output_fingerprints(
            original_workspace
        )
        _validate_original_lombok_boundary(original_workspace, original_module)

        snapshot_started = time.monotonic()
        snapshot_project = attempt_root / "workspace-snapshot"
        _snapshot_workspace(
            original_project,
            snapshot_project,
            excluded_directories=_snapshot_build_directories(
                original_workspace
            ),
        )
        # Both Maven and direct javac see this private workspace-root guard.
        # External configuration was rejected on the original workspace, so
        # stopping an otherwise empty upward search changes no project setting
        # while preventing the temp-directory host from influencing evidence.
        _install_snapshot_lombok_guard(snapshot_project)
        snapshot_duration_ms = monotonic_ms(snapshot_started)

        workspace = adapter.resolve_workspace(snapshot_project)
        _validate_p0_workspace(workspace)
        _validate_standard_workspace_outputs(workspace)
        _static_maven_preflight(adapter, workspace)
        _remove_snapshot_build_outputs(workspace)
        module = _module_by_relative_path(
            workspace,
            original_module.relative_path,
        )
        private_log = attempt_root / "supervised-tools.log"
        build_jdk = _select_jdk(
            project=snapshot_project,
            preferences=preferences,
            explicit_home=args.java_home,
            supervisor=supervisor,
            output_capture=private_log,
        )
        maven = _select_maven(
            project=snapshot_project,
            preferences=preferences,
            explicit_executable=args.maven,
            build_jdk=build_jdk,
            supervisor=supervisor,
            output_capture=private_log,
        )
        intent = LaunchIntent(
            source="experiment",
            launch_name="lombok-processor-model",
            launch_type="java_application",
            main_class="experiment.NoRuntimeLaunch",
            working_directory=module.directory,
            ide_module_name=module.relative_path,
            build_before_run=False,
        )
        execution = adapter.create_execution_plan(
            workspace=workspace,
            module=module,
            intent=intent,
            maven=maven,
            build_jdk=build_jdk,
            preferences=preferences,
            attempt_directory=attempt_root / "maven-model",
        )

        metadata_operation = replace(
            adapter.create_compile_classpath_operation(execution),
            timeout_seconds=args.metadata_timeout_seconds,
        )
        metadata_result = _run_supervised(
            supervisor,
            metadata_operation,
            operation="maven_compile_model",
        )
        _require_success(
            metadata_result,
            error_code="COMPILE_MODEL_UNAVAILABLE",
            message="Maven could not resolve the experiment compile model.",
            suggested_next_step=(
                "Inspect the private Maven log and correct model resolution."
            ),
        )

        planner = LombokExperimentPlanner(adapter)
        planner.validate_before_baseline(
            execution,
            validate_processor=False,
        )
        coordinate = planner.explicit_processor_coordinate(execution)
        processor_directory: Path | None = None
        processor_resolution_ms = 0.0
        if coordinate is not None:
            processor_started = time.monotonic()
            processor_directory = attempt_root / "processor-path"
            processor_result = _run_supervised(
                supervisor,
                planner.create_processor_copy_operation(
                    execution,
                    coordinate=coordinate,
                    destination=processor_directory,
                    timeout_seconds=args.metadata_timeout_seconds,
                ),
                operation="lombok_processor_resolution",
            )
            _require_success(
                processor_result,
                error_code="PROCESSOR_PATH_UNRESOLVED",
                message="Maven could not copy the explicit Lombok artifact.",
                suggested_next_step=(
                    "Inspect the private Maven log and keep using Maven."
                ),
            )
            processor_resolution_ms = monotonic_ms(processor_started)

        model_started = time.monotonic()
        planner.validate_before_baseline(
            execution,
            explicit_processor_directory=processor_directory,
        )
        before_baseline_plan = planner.create_plan(
            execution,
            explicit_processor_directory=processor_directory,
        )
        snapshot_exclusions = frozenset(
            _snapshot_build_directories(workspace)
        )
        baseline_input_manifest = _workspace_manifest(
            snapshot_project,
            snapshot_exclusions,
        )

        baseline_result = _run_supervised(
            supervisor,
            _fresh_maven_baseline_operation(
                adapter,
                execution,
                timeout_seconds=args.maven_baseline_timeout_seconds,
            ),
            operation="fresh_maven_baseline",
        )
        _require_success(
            baseline_result,
            error_code="MAVEN_BASELINE_FAILED",
            message="The fresh private Maven compile did not succeed.",
            suggested_next_step=(
                "Inspect the private Maven log and make the formal build pass."
            ),
        )
        baseline_hashes = scan_class_hashes(module.output_directory)

        metadata_after_result = _run_supervised(
            supervisor,
            metadata_operation,
            operation="maven_compile_model_after_baseline",
        )
        _require_success(
            metadata_after_result,
            error_code="COMPILE_MODEL_UNAVAILABLE",
            message="Maven could not revalidate the compile model.",
            suggested_next_step=(
                "Discard this evidence and inspect the private Maven log."
            ),
        )
        plan = planner.create_plan(
            execution,
            explicit_processor_directory=processor_directory,
        )
        if (
            _workspace_manifest(snapshot_project, snapshot_exclusions)
            != baseline_input_manifest
            or not _plans_semantically_equal(before_baseline_plan, plan)
        ):
            raise LombokExperimentError(
                "COMPILE_MODEL_CHANGED_DURING_BASELINE",
                "The effective compiler model changed during Maven compile.",
                suggested_next_step=(
                    "Remove file-activated build drift or use Maven directly."
                ),
                retryable=True,
            )
        plan = freeze_plan_artifacts(
            plan,
            destination=attempt_root / "frozen-compiler-inputs",
        )
        model_duration_ms = monotonic_ms(model_started)
        report: dict[str, object] = {
            "ok": True,
            "status": "probe_ready" if args.probe_only else "completed",
            "experiment": "lombok_processor_model",
            "public_api_changed": False,
            "target_outputs_modified": False,
            "runtime_jdwp_touched": False,
            "subprocess_isolation": "supervised_not_security_sandbox",
            "plan": plan.redacted_summary(),
            "maven_baseline": {
                "provenance": "private_frozen_workspace_fresh_compile",
                "generated_class_count": len(baseline_hashes),
                "duration_ms": round(
                    (
                        baseline_result.finished_at
                        - baseline_result.started_at
                    )
                    * 1000,
                    3,
                ),
                "trusted_input": True,
            },
            "durations_ms": {
                "workspace_snapshot": snapshot_duration_ms,
                "metadata_resolution": round(
                    (
                        metadata_result.finished_at
                        - metadata_result.started_at
                        + metadata_after_result.finished_at
                        - metadata_after_result.started_at
                    )
                    * 1000,
                    3,
                ),
                "processor_resolution": processor_resolution_ms,
                "model_validation": model_duration_ms,
            },
            "private_artifacts": {
                "retained": True,
                "attempt_id": public_attempt_id,
                "paths_disclosed": False,
            },
            "warnings": list(preferences.warnings),
        }
        if args.probe_only:
            report["verification_state"] = "probe_only"
            durations = report["durations_ms"]
            assert isinstance(durations, dict)
            durations["total"] = monotonic_ms(started)
            _require_supervisor_settled(supervisor)
            _assert_original_outputs_unchanged(
                original_output_fingerprints,
            )
            _emit(report)
            return 0

        compiler = LombokExperimentRunner(supervisor)
        compile_root = attempt_root / "compile-attempts"
        compile_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        attempts = [
            compiler.compile(
                plan,
                root_directory=compile_root,
                timeout_seconds=args.timeout_seconds,
            )
            for _index in range(args.repeat)
        ]
        report["attempts"] = [
            attempt.redacted_summary() for attempt in attempts
        ]
        first = attempts[0]
        repeated = [
            compare_class_outputs(first.class_hashes, item.class_hashes)
            for item in attempts[1:]
        ]
        deterministic = bool(repeated) and all(
            item["exact_match"] for item in repeated
        )
        report["determinism"] = {
            "repeat_count": len(attempts),
            "exact_match": deterministic,
            "comparisons": repeated,
            "verification_state": (
                "verified_exact"
                if deterministic
                else (
                    "single_run_only" if not repeated else "requires_review"
                )
            ),
        }
        baseline_comparison = compare_class_outputs(
            baseline_hashes,
            first.class_hashes,
        )
        baseline_comparison["provenance"] = (
            "private_frozen_workspace_fresh_compile"
        )
        maven_baseline = report["maven_baseline"]
        assert isinstance(maven_baseline, dict)
        maven_baseline["comparison"] = baseline_comparison
        verified = deterministic and bool(baseline_comparison["exact_match"])
        report["verification_state"] = (
            "verified_exact" if verified else "requires_review"
        )
        report["trusted_for_product_decision"] = verified

        if args.external_baseline_output is not None:
            external = scan_class_hashes(
                args.external_baseline_output.expanduser().resolve(strict=True)
            )
            external_comparison = compare_class_outputs(
                external,
                first.class_hashes,
            )
            external_comparison.update(
                {
                    "provenance": "external_unverified",
                    "trusted_for_product_decision": False,
                }
            )
            report["external_baseline"] = external_comparison

        report["durations_ms"]["total"] = monotonic_ms(started)  # type: ignore[index]
        report["private_artifacts"]["compile_attempt_ids"] = [  # type: ignore[index]
            item.attempt_id for item in attempts
        ]
        _require_supervisor_settled(supervisor)
        _assert_original_outputs_unchanged(
            original_output_fingerprints,
        )
        _emit(report)
        return 0
    except LombokExperimentError as error:
        payload = error.as_dict()
        cleanup_settled = _settle_supervisor(supervisor)
        output_changed = _original_outputs_changed(
            original_output_fingerprints,
        )
        if cleanup_settled is False:
            payload.update(
                {
                    "error_code": "PROCESS_CLEANUP_UNSETTLED",
                    "error": "A supervised subprocess tree did not settle.",
                    "retryable": True,
                    "suggested_next_step": (
                        "Stop remaining compiler processes before retrying."
                    ),
                }
            )
        elif output_changed is True:
            payload.update(
                {
                    "error_code": "ORIGINAL_OUTPUT_CHANGED_DURING_EXPERIMENT",
                    "error": (
                        "The original Maven output changed during the "
                        "experiment."
                    ),
                    "retryable": True,
                    "suggested_next_step": (
                        "Stop external builds, discard this evidence, and "
                        "retry."
                    ),
                }
            )
        payload.update(
            {
                "experiment": "lombok_processor_model",
                "public_api_changed": False,
                "target_outputs_modified": (
                    output_changed
                    if output_changed is not None
                    else "not_verified"
                ),
                "runtime_jdwp_touched": False,
                "subprocess_isolation": "supervised_not_security_sandbox",
                "private_artifacts": (
                    {
                        "retained": True,
                        "attempt_id": public_attempt_id,
                        "paths_disclosed": False,
                    }
                    if attempt_root is not None
                    else {}
                ),
            }
        )
        _emit(payload)
        return 1
    except (MavenResolutionError, OSError, ValueError) as error:
        cleanup_settled = _settle_supervisor(supervisor)
        output_changed = _original_outputs_changed(
            original_output_fingerprints,
        )
        error_code = (
            "PROCESS_CLEANUP_UNSETTLED"
            if cleanup_settled is False
            else "EXPERIMENT_PREPARE_FAILED"
        )
        _emit(
            {
                "ok": False,
                "error_code": error_code,
                "error": type(error).__name__,
                "retryable": False,
                "suggested_next_step": (
                    "Verify project_path, IDEA Maven/JDK settings, and retry."
                ),
                "public_api_changed": False,
                "target_outputs_modified": (
                    output_changed
                    if output_changed is not None
                    else "not_verified"
                ),
                "runtime_jdwp_touched": False,
                "subprocess_isolation": "supervised_not_security_sandbox",
                "private_artifacts": (
                    {
                        "retained": True,
                        "attempt_id": public_attempt_id,
                        "paths_disclosed": False,
                    }
                    if attempt_root is not None
                    else {}
                ),
            }
        )
        return 1
    finally:
        _settle_supervisor(supervisor)


def _validate_arguments(args: argparse.Namespace) -> None:
    if args.repeat < 1 or args.repeat > 5:
        raise LombokExperimentError(
            "INVALID_ARGUMENT",
            "repeat must be between 1 and 5.",
            suggested_next_step="Use --repeat 2 for determinism evidence.",
        )
    timeouts = (
        args.timeout_seconds,
        args.maven_baseline_timeout_seconds,
        args.metadata_timeout_seconds,
    )
    if any(value <= 0 for value in timeouts):
        raise LombokExperimentError(
            "INVALID_ARGUMENT",
            "Experiment timeouts must be positive.",
            suggested_next_step="Provide positive timeout values.",
        )


def _validate_attempt_root_location(
    configured: Path | None,
    project: Path,
) -> None:
    if configured is None:
        return
    candidate = configured.expanduser().resolve(strict=False)
    project = project.resolve(strict=True)
    if _contains_path(project, candidate) or _contains_path(candidate, project):
        raise LombokExperimentError(
            "INVALID_ARGUMENT",
            "attempt_root and project_path must not contain one another.",
            suggested_next_step=(
                "Use a new user-private temporary directory outside the project."
            ),
        )


def _contains_path(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_p0_workspace(workspace: MavenWorkspace) -> None:
    modules = tuple(workspace.modules)
    if (
        len(modules) != 1
        or modules[0].packaging != "jar"
        or modules[0].relative_path != "."
    ):
        raise LombokExperimentError(
            "COMPILE_EXPERIMENT_UNSUPPORTED",
            "The Lombok P0 experiment accepts one standalone jar module.",
            suggested_next_step=(
                "Use Maven for Reactor projects until every -am module is modeled."
            ),
        )


def _validate_standard_workspace_outputs(workspace: MavenWorkspace) -> None:
    root = workspace.project_root.resolve(strict=True)
    for module in workspace.modules:
        output = module.output_directory.resolve(strict=False)
        expected = (module.directory / "target" / "classes").resolve(
            strict=False
        )
        if output != expected or not _contains_path(root, output):
            raise LombokExperimentError(
                "COMPILE_EXPERIMENT_UNSUPPORTED",
                "The experiment requires conventional Maven output paths.",
                suggested_next_step=(
                    "Use Maven until custom build/output paths are modeled."
                ),
            )


def _static_maven_preflight(
    adapter: MavenBuildSystemAdapter,
    workspace: MavenWorkspace,
) -> None:
    project_inputs = tuple(
        workspace.build_root / ".mvn" / name
        for name in ("maven.config", "jvm.config", "extensions.xml")
    )
    for path in project_inputs:
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise LombokExperimentError(
                "COMPILE_MODEL_UNAVAILABLE",
                "A Maven project configuration input is unsafe.",
                suggested_next_step="Use the formal Maven build.",
            )
        if path.name == "extensions.xml" or path.read_bytes().strip():
            raise LombokExperimentError(
                "COMPILE_MODEL_UNAVAILABLE",
                "Project Maven extensions/arguments are outside the P0 model.",
                suggested_next_step=(
                    "Use Maven until project-level Maven options are modeled."
                ),
            )
    for module in workspace.modules:
        project = adapter._read_pom(module.pom_file)  # noqa: SLF001
        candidates = (project, *project.findall("./{*}profiles/{*}profile"))
        for candidate in candidates:
            try:
                adapter._ensure_no_unverified_maven_extensions(  # noqa: SLF001
                    candidate,
                    maven_project_inputs=project_inputs,
                )
                adapter._ensure_no_unverified_build_transforms(  # noqa: SLF001
                    candidate,
                    (),
                    allow_lombok_processor=True,
                )
            except MavenResolutionError as error:
                raise LombokExperimentError(
                    "COMPILE_MODEL_UNAVAILABLE",
                    "A raw Maven model can execute an unmodeled build step.",
                    suggested_next_step="Use the formal Maven build.",
                ) from error


def _validate_original_lombok_boundary(
    workspace: MavenWorkspace,
    module: MavenModule,
) -> None:
    source_root = module.directory / "src" / "main" / "java"
    if not source_root.is_dir():
        return
    sources = tuple(sorted(source_root.rglob("*.java")))
    if sources:
        discover_lombok_configuration(workspace.project_root, sources)


def _fresh_maven_baseline_operation(
    adapter: MavenBuildSystemAdapter,
    execution: MavenExecutionPlan,
    *,
    timeout_seconds: float,
) -> BuildOperationSpec:
    arguments = adapter._base_maven_arguments(execution)  # noqa: SLF001
    if execution.module.relative_path != ".":
        arguments.extend(["-pl", execution.module.relative_path, "-am"])
    arguments.append("compile")
    environment = JavaToolchainResolver.maven_environment(execution.build_jdk)
    return BuildOperationSpec(
        argv=tuple(arguments),
        cwd=execution.workspace.build_root,
        environment=environment,
        timeout_seconds=timeout_seconds,
        output_capture=execution.build_log,
        operation_name="fresh_private_maven_compile",
    )


def _run_supervised(
    supervisor: ProcessSupervisor,
    spec: BuildOperationSpec,
    *,
    operation: str,
) -> OperationResult:
    token = AttemptToken(
        attempt_id=f"{operation}_{uuid.uuid4().hex[:12]}",
        generation=1,
    )
    result = None
    try:
        result = supervisor.run(spec, owner=token)
    finally:
        owner_settled = supervisor.release_owner(token)
    if not owner_settled:
        raise LombokExperimentError(
            "PROCESS_CLEANUP_UNSETTLED",
            "A supervised tool process tree did not settle.",
            suggested_next_step=(
                "Stop remaining build/compiler processes before retrying."
            ),
            retryable=True,
        )
    assert result is not None
    return result


def _settle_supervisor(supervisor: ProcessSupervisor) -> bool:
    graceful = supervisor.close(deadline=time.monotonic() + 3.0)
    if graceful.settled:
        return True
    forced = supervisor.force_close(deadline=time.monotonic() + 3.0)
    return forced.settled


def _require_supervisor_settled(supervisor: ProcessSupervisor) -> None:
    if not _settle_supervisor(supervisor):
        raise LombokExperimentError(
            "PROCESS_CLEANUP_UNSETTLED",
            "A supervised subprocess tree did not settle.",
            suggested_next_step=(
                "Stop remaining build/compiler processes before retrying."
            ),
            retryable=True,
        )


def _require_success(
    result: OperationResult,
    *,
    error_code: str,
    message: str,
    suggested_next_step: str,
) -> None:
    if result.succeeded:
        return
    raise LombokExperimentError(
        error_code,
        message,
        suggested_next_step=suggested_next_step,
        retryable=bool(result.timed_out or result.cancelled),
        context={
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
            "return_code": result.return_code,
        },
    )


def _plans_semantically_equal(
    before: LombokExperimentPlan,
    after: LombokExperimentPlan,
) -> bool:
    before_sources = {
        path.relative_to(before.project_root).as_posix(): digest
        for path, digest in before.source_hashes.items()
    }
    after_sources = {
        path.relative_to(after.project_root).as_posix(): digest
        for path, digest in after.source_hashes.items()
    }
    return bool(
        before.compiler_model == after.compiler_model
        and before.processor_model == after.processor_model
        and before.dependency_classpath == after.dependency_classpath
        and before.config_snapshot.fingerprint
        == after.config_snapshot.fingerprint
        and before_sources == after_sources
    )


def _select_module(
    workspace: MavenWorkspace,
    requested: str | None,
) -> MavenModule:
    candidates = [
        module for module in workspace.modules if module.packaging != "pom"
    ]
    if requested:
        matches = [
            module
            for module in candidates
            if requested
            in {
                module.relative_path,
                module.artifact_id,
                module.directory.name,
                module.name,
            }
        ]
        if len(matches) == 1:
            return matches[0]
        raise LombokExperimentError(
            "AMBIGUOUS_MODULE" if matches else "MODULE_NOT_FOUND",
            "The requested Maven module could not be selected uniquely.",
            suggested_next_step=(
                "Use the exact Maven relative module path from pom.xml."
            ),
            retryable=True,
            context={"matching_module_count": len(matches)},
        )
    if len(candidates) == 1:
        return candidates[0]
    raise LombokExperimentError(
        "AMBIGUOUS_MODULE",
        "The Maven workspace contains multiple Java modules.",
        suggested_next_step="Pass --module with the exact relative path.",
        retryable=True,
        context={"candidate_count": len(candidates)},
    )


def _module_by_relative_path(
    workspace: MavenWorkspace,
    relative_path: str,
) -> MavenModule:
    matches = [
        module
        for module in workspace.modules
        if module.relative_path == relative_path
    ]
    if len(matches) != 1:
        raise LombokExperimentError(
            "WORKSPACE_SNAPSHOT_UNVERIFIED",
            "The private workspace changed the Maven module layout.",
            suggested_next_step="Inspect the private snapshot and retry.",
        )
    return matches[0]


def _remove_snapshot_build_outputs(workspace: MavenWorkspace) -> None:
    """Make the copied workspace a fresh Maven baseline by construction."""

    root = workspace.project_root.resolve(strict=True)
    for module in workspace.modules:
        output = module.output_directory.resolve(strict=False)
        expected = (module.directory / "target" / "classes").resolve(
            strict=False
        )
        if output != expected:
            raise LombokExperimentError(
                "COMPILE_EXPERIMENT_UNSUPPORTED",
                "The fresh-baseline experiment requires standard Maven output.",
                suggested_next_step=(
                    "Use Maven until custom build/output directories are "
                    "modeled as a complete private generation."
                ),
            )
        try:
            output.relative_to(root)
        except ValueError as error:
            raise LombokExperimentError(
                "WORKSPACE_SNAPSHOT_UNVERIFIED",
                "A Maven output directory leaves the private workspace.",
                suggested_next_step=(
                    "Use a workspace-local Maven output directory."
                ),
            ) from error
        if output == root:
            raise LombokExperimentError(
                "WORKSPACE_SNAPSHOT_UNVERIFIED",
                "A Maven output directory aliases the workspace root.",
                suggested_next_step="Use a conventional Maven output path.",
            )
        if output.is_symlink():
            raise LombokExperimentError(
                "WORKSPACE_SNAPSHOT_UNVERIFIED",
                "A Maven output directory is a symbolic link.",
                suggested_next_step="Use a regular Maven output directory.",
            )
        if output.exists():
            if not output.is_dir():
                raise LombokExperimentError(
                    "WORKSPACE_SNAPSHOT_UNVERIFIED",
                    "A Maven output path is not a directory.",
                    suggested_next_step="Use a regular Maven output directory.",
                )
            shutil.rmtree(output)


def _snapshot_build_directories(
    workspace: MavenWorkspace,
) -> tuple[Path, ...]:
    return tuple(
        Path(os.path.abspath(module.directory / "target"))
        for module in workspace.modules
    )


def _workspace_output_fingerprints(
    workspace: MavenWorkspace,
) -> dict[Path, str]:
    return {
        module.output_directory.resolve(strict=False): _tree_fingerprint(
            module.output_directory
        )
        for module in workspace.modules
    }


def _select_jdk(
    *,
    project: Path,
    preferences: IdeaBuildPreferences,
    explicit_home: Path | None,
    supervisor: ProcessSupervisor,
    output_capture: Path,
) -> JavaToolchainCandidate:
    resolver = JavaToolchainResolver()
    if explicit_home is not None:
        suffix = ".exe" if os.name == "nt" else ""
        home = explicit_home.expanduser().resolve(strict=True)
        candidates = (
            JavaToolchainCandidate(
                home=home,
                java_executable=home / "bin" / f"java{suffix}",
                javac_executable=home / "bin" / f"javac{suffix}",
                source="explicit_experiment_argument",
            ),
        )
    else:
        candidates = resolver.candidates(
            preferences=preferences,
            explicit_reference=None,
            for_build=True,
        )
    for candidate in candidates:
        if not candidate.has_runtime or not candidate.has_compiler:
            continue
        offset = _file_size(output_capture)
        result = _run_supervised(
            supervisor,
            resolver.compiler_probe_spec(
                candidate,
                cwd=project,
                output_capture=output_capture,
                operation_name="experiment_javac_probe",
            ),
            operation="javac_probe",
        )
        if not result.succeeded:
            continue
        output = _read_from(output_capture, offset)
        major = JavaToolchainCandidate.parse_compiler_major_version_output(
            output
        ) or candidate.compiler_major_version
        if major is not None:
            return replace(
                candidate,
                detected_major_version=major,
                detected_compiler_major_version=major,
            )
    raise LombokExperimentError(
        "JAVA_TOOLCHAIN_NOT_FOUND",
        "No usable Build JDK with javac was found.",
        suggested_next_step=(
            "Pass --java-home or configure the IDEA Maven runner JDK."
        ),
        retryable=True,
    )


def _select_maven(
    *,
    project: Path,
    preferences: IdeaBuildPreferences,
    explicit_executable: Path | None,
    build_jdk: JavaToolchainCandidate,
    supervisor: ProcessSupervisor,
    output_capture: Path,
) -> MavenToolCandidate:
    resolver = MavenToolResolver()
    if explicit_executable is not None:
        executable = explicit_executable.expanduser().resolve(strict=True)
        candidates = (
            MavenToolCandidate(
                argv_prefix=(str(executable),),
                source="explicit_experiment_argument",
            ),
        )
    else:
        candidates = resolver.candidates(
            project_root=project,
            preferences=preferences,
        )
    candidates = tuple(
        candidate
        for candidate in candidates
        if candidate.source != "project_wrapper"
    )
    environment = JavaToolchainResolver.maven_environment(build_jdk)
    for candidate in candidates:
        result = _run_supervised(
            supervisor,
            resolver.probe_spec(
                candidate,
                cwd=project,
                environment=environment,
                output_capture=output_capture,
            ),
            operation="maven_probe",
        )
        if result.succeeded:
            return candidate
    raise LombokExperimentError(
        "MAVEN_NOT_FOUND",
        "No usable Maven command was found.",
        suggested_next_step=(
            "Pass --maven or configure the IDEA Maven home."
        ),
        retryable=True,
    )


def _snapshot_workspace(
    source: Path,
    destination: Path,
    *,
    excluded_directories: Iterable[Path] = (),
) -> None:
    source = source.resolve(strict=True)
    excluded = frozenset(
        Path(os.path.abspath(path)) for path in excluded_directories
    )
    if any(not _contains_path(source, path) for path in excluded):
        raise LombokExperimentError(
            "WORKSPACE_SNAPSHOT_UNVERIFIED",
            "A snapshot exclusion leaves the Maven workspace.",
            suggested_next_step="Use conventional workspace-local outputs.",
        )
    if destination.exists():
        raise LombokExperimentError(
            "WORKSPACE_SNAPSHOT_UNVERIFIED",
            "The private workspace destination already exists.",
            suggested_next_step="Use a new --attempt-root directory.",
        )
    before = _workspace_manifest(source, excluded)
    destination.mkdir(parents=True, mode=0o700)
    _chmod_private(destination)
    try:
        _copy_workspace_directory(
            source,
            destination,
            root=source,
            excluded=excluded,
        )
        after = _workspace_manifest(source, excluded)
        copied = _workspace_manifest(destination, frozenset())
        if before != after or copied != before:
            raise LombokExperimentError(
                "WORKSPACE_CHANGED_DURING_SNAPSHOT",
                "The Maven workspace changed while it was copied.",
                suggested_next_step=(
                    "Wait for edits/builds to settle, then retry."
                ),
                retryable=True,
            )
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _copy_workspace_directory(
    source: Path,
    destination: Path,
    *,
    root: Path,
    excluded: frozenset[Path],
) -> None:
    for entry in sorted(os.scandir(source), key=lambda item: item.name):
        source_path = Path(entry.path)
        if _snapshot_entry_excluded(source_path, root, excluded):
            continue
        target_path = destination / entry.name
        metadata = entry.stat(follow_symlinks=False)
        if _metadata_is_link_or_reparse(metadata):
            raise LombokExperimentError(
                "WORKSPACE_SNAPSHOT_UNVERIFIED",
                "The Maven workspace contains a link or reparse point.",
                suggested_next_step=(
                    "Use a regular-file workspace for this P0 experiment."
                ),
            )
        if entry.is_dir(follow_symlinks=False):
            target_path.mkdir()
            _copy_workspace_directory(
                source_path,
                target_path,
                root=root,
                excluded=excluded,
            )
        elif entry.is_file(follow_symlinks=False):
            shutil.copy2(source_path, target_path)
        else:
            raise LombokExperimentError(
                "WORKSPACE_SNAPSHOT_UNVERIFIED",
                "The Maven workspace contains a non-regular filesystem entry.",
                suggested_next_step="Use a regular-file workspace.",
            )


def _workspace_manifest(
    root: Path,
    excluded: frozenset[Path],
) -> str:
    digest = hashlib.sha256()

    def visit(directory: Path) -> None:
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            path = Path(entry.path)
            if _snapshot_entry_excluded(path, root, excluded):
                continue
            relative = path.relative_to(root).as_posix()
            metadata = entry.stat(follow_symlinks=False)
            if _metadata_is_link_or_reparse(metadata):
                raise LombokExperimentError(
                    "WORKSPACE_SNAPSHOT_UNVERIFIED",
                    "The Maven workspace contains a link or reparse point.",
                    suggested_next_step="Use regular workspace files.",
                )
            if entry.is_dir(follow_symlinks=False):
                digest.update(b"dir\0")
                digest.update(
                    relative.encode("utf-8", errors="surrogateescape")
                )
                digest.update(b"\0")
                visit(path)
            elif entry.is_file(follow_symlinks=False):
                digest.update(b"file\0")
                digest.update(
                    relative.encode("utf-8", errors="surrogateescape")
                )
                digest.update(b"\0")
                digest.update(str(metadata.st_size).encode("ascii"))
                digest.update(b"\0")
                with path.open("rb") as handle:
                    for block in iter(
                        lambda: handle.read(1024 * 1024),
                        b"",
                    ):
                        digest.update(block)
            else:
                raise LombokExperimentError(
                    "WORKSPACE_SNAPSHOT_UNVERIFIED",
                    "The Maven workspace has a non-regular entry.",
                    suggested_next_step="Use regular workspace files.",
                )

    visit(root)
    return digest.hexdigest()


def _snapshot_entry_excluded(
    path: Path,
    root: Path,
    excluded: frozenset[Path],
) -> bool:
    lexical = Path(os.path.abspath(path))
    if lexical in excluded:
        return True
    return path.parent == root and path.name in _SNAPSHOT_ROOT_METADATA


def _metadata_is_link_or_reparse(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(
        stat.S_ISLNK(metadata.st_mode)
        or (reparse_flag and attributes & reparse_flag)
    )


def _install_snapshot_lombok_guard(snapshot_project: Path) -> None:
    config = snapshot_project / "lombok.config"
    guard = "config.stopBubbling = true\n"
    if config.exists():
        if _path_is_link_or_reparse(config) or not config.is_file():
            raise LombokExperimentError(
                "LOMBOK_CONFIG_UNVERIFIED",
                "The workspace-root Lombok configuration is unsafe.",
                suggested_next_step="Use a regular lombok.config file.",
            )
        existing = config.read_text(encoding="utf-8-sig")
        separator = "" if not existing or existing.endswith("\n") else "\n"
        config.write_text(
            existing + separator + guard,
            encoding="utf-8",
        )
    else:
        config.write_text(guard, encoding="utf-8")


def _assert_original_outputs_unchanged(
    expected: dict[Path, str] | None,
) -> None:
    if expected is None:
        raise LombokExperimentError(
            "ORIGINAL_OUTPUT_STATE_UNVERIFIED",
            "Original Maven outputs were not frozen before the experiment.",
            suggested_next_step="Discard this evidence and retry.",
            retryable=True,
        )
    try:
        changed = any(
            _tree_fingerprint(path) != value
            for path, value in expected.items()
        )
    except OSError as error:
        raise LombokExperimentError(
            "ORIGINAL_OUTPUT_STATE_UNVERIFIED",
            "Original Maven outputs could not be revalidated.",
            suggested_next_step="Discard this evidence and retry.",
            retryable=True,
        ) from error
    if changed:
        raise LombokExperimentError(
            "ORIGINAL_OUTPUT_CHANGED_DURING_EXPERIMENT",
            "An original Maven output changed during the experiment.",
            suggested_next_step=(
                "Stop external builds, discard this evidence, and retry."
            ),
            retryable=True,
        )


def _original_outputs_changed(
    expected: dict[Path, str] | None,
) -> bool | None:
    if expected is None:
        return None
    try:
        return any(
            _tree_fingerprint(path) != value
            for path, value in expected.items()
        )
    except OSError:
        return None


def _tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        digest.update(b"<missing>")
        return digest.hexdigest()
    if _path_is_link_or_reparse(root) or not root.is_dir():
        digest.update(b"<unsafe>")
        return digest.hexdigest()
    for candidate in sorted(root.rglob("*")):
        relative = candidate.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        if _path_is_link_or_reparse(candidate):
            digest.update(b"<symlink>")
            continue
        if candidate.is_file():
            with candidate.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
    return digest.hexdigest()


def _path_is_link_or_reparse(path: Path) -> bool:
    try:
        return _metadata_is_link_or_reparse(path.lstat())
    except OSError:
        return False


def _attempt_root(configured: Path | None) -> Path:
    if configured is None:
        root = Path(tempfile.mkdtemp(prefix="jolink-lombok-experiment-"))
    else:
        root = configured.expanduser().resolve(strict=False)
        root.mkdir(parents=True, exist_ok=False, mode=0o700)
    _chmod_private(root)
    return root


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(stat.S_IRWXU)
    except OSError:
        pass


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_from(path: Path, offset: int) -> str:
    try:
        with path.open("rb") as source:
            source.seek(offset)
            data = source.read(64 * 1024)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _emit(payload: dict[str, object]) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
