#!/usr/bin/env python3
"""Run the private Phase 2A Maven Build World -> JDT FULL experiment."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Sequence

import run_bootstrap_smoke as common

from jolink_runtime.experiments.jdt_build_world import (
    BuildWorldError,
    BuildWorldSnapshot,
    canonical_fingerprint,
    classify_diagnostics,
    compare_class_outputs,
    create_snapshot,
    describe_source_root,
    materialize_private_sources,
    sha256_file,
    tree_fingerprint,
    write_worker_classpath,
)
from jolink_runtime.experiments.lombok_processor import (
    LombokExperimentError,
    discover_lombok_configuration,
)
from jolink_runtime.launch.contracts import BuildOperationSpec, LaunchIntent
from jolink_runtime.launch.idea_environment import IdeaBuildPreferences
from jolink_runtime.launch.maven import (
    MavenBuildSystemAdapter,
    MavenExecutionPlan,
    MavenModule,
    MavenResolutionError,
)
from jolink_runtime.launch.process_supervisor import AttemptToken, ProcessSupervisor
from jolink_runtime.launch.toolchain import (
    JavaToolchainCandidate,
    JavaToolchainResolver,
    MavenToolCandidate,
    MavenToolResolver,
)


def _java_tool(home: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    path = home.expanduser().resolve(strict=True) / "bin" / f"{name}{suffix}"
    if not path.is_file():
        raise BuildWorldError(
            "JAVA_TOOLCHAIN_UNAVAILABLE",
            f"The requested {name} tool is unavailable.",
            suggested_next_step="Provide a complete JDK home and retry.",
            retryable=True,
        )
    return path


def _probe_java_major(home: Path, tool: str) -> int:
    executable = _java_tool(home, tool)
    import subprocess

    completed = subprocess.run(
        [str(executable), "-version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    output = completed.stdout + "\n" + completed.stderr
    parser = (
        JavaToolchainCandidate.parse_compiler_major_version_output
        if tool == "javac"
        else JavaToolchainCandidate.parse_major_version_output
    )
    major = parser(output)
    if completed.returncode != 0 or major is None:
        raise BuildWorldError(
            "JAVA_TOOLCHAIN_UNAVAILABLE",
            f"The requested {tool} version could not be verified.",
            suggested_next_step="Provide a working JDK home and retry.",
            retryable=True,
        )
    return major


def _java_candidate(home: Path, source: str) -> JavaToolchainCandidate:
    resolved = home.expanduser().resolve(strict=True)
    return JavaToolchainCandidate(
        home=resolved,
        java_executable=_java_tool(resolved, "java"),
        javac_executable=_java_tool(resolved, "javac"),
        source=source,
        detected_major_version=_probe_java_major(resolved, "java"),
        detected_compiler_major_version=_probe_java_major(resolved, "javac"),
    )


def _maven_candidate(
    explicit: Path | None,
    *,
    project_root: Path,
    preferences: IdeaBuildPreferences,
) -> MavenToolCandidate:
    if explicit is not None:
        executable = explicit.expanduser().resolve(strict=True)
        if not executable.is_file():
            raise BuildWorldError(
                "MAVEN_UNAVAILABLE",
                "The explicit Maven executable is unavailable.",
                suggested_next_step="Provide the exact mvn or mvn.cmd path.",
                retryable=True,
            )
        return MavenToolCandidate(
            argv_prefix=(str(executable),), source="explicit_phase2a"
        )
    candidates = MavenToolResolver().candidates(
        project_root=project_root, preferences=preferences
    )
    if not candidates:
        raise BuildWorldError(
            "MAVEN_UNAVAILABLE",
            "No Maven executable could be discovered.",
            suggested_next_step="Pass --maven-executable and retry.",
            retryable=True,
        )
    return candidates[0]


def _select_module(
    modules: Sequence[MavenModule], selector: str | None
) -> MavenModule:
    runnable = [module for module in modules if module.packaging != "pom"]
    if selector is None and len(runnable) == 1:
        return runnable[0]
    if selector is not None:
        matches = [
            module
            for module in runnable
            if selector
            in {
                module.relative_path,
                module.directory.name,
                module.artifact_id,
                module.name,
            }
        ]
        if len(matches) == 1:
            return matches[0]
    else:
        matches = []
    raise BuildWorldError(
        "REPRESENTATIVE_MODULE_AMBIGUOUS",
        "The Maven workspace has no unique representative module.",
        suggested_next_step="Pass --module with one exact module selector.",
        retryable=True,
        context={
            "candidate_count": len(runnable),
            "matching_candidate_count": len(matches),
        },
    )


def _preferences(args: argparse.Namespace) -> IdeaBuildPreferences:
    settings = (
        args.settings_file.expanduser().resolve(strict=True)
        if args.settings_file is not None
        else None
    )
    repository = (
        args.local_repository.expanduser().resolve(strict=False)
        if args.local_repository is not None
        else None
    )
    return IdeaBuildPreferences(
        user_settings_file=settings,
        local_repository=repository,
        active_profiles=tuple(args.profile),
    )


def _maven_operation(
    *,
    adapter: MavenBuildSystemAdapter,
    execution: MavenExecutionPlan,
    goals: Sequence[str],
    name: str,
    timeout: float,
    output: Path,
) -> BuildOperationSpec:
    arguments = adapter._base_maven_arguments(execution)  # noqa: SLF001
    if execution.module.relative_path != ".":
        arguments.extend(["-pl", execution.module.relative_path, "-am"])
    arguments.extend(goals)
    return BuildOperationSpec(
        argv=tuple(arguments),
        cwd=execution.workspace.build_root,
        environment=JavaToolchainResolver.maven_environment(execution.build_jdk),
        timeout_seconds=timeout,
        output_capture=output,
        operation_name=name,
    )


def _run_maven(
    supervisor: ProcessSupervisor,
    spec: BuildOperationSpec,
    owner: AttemptToken,
) -> float:
    result = supervisor.run(spec, owner=owner)
    if not result.succeeded:
        raise BuildWorldError(
            "MAVEN_BOOTSTRAP_FAILED",
            "The private Maven Build World operation failed.",
            suggested_next_step="Inspect the local Maven log and project configuration.",
            retryable=bool(result.timed_out or result.cancelled),
            context={
                "operation": result.operation_name,
                "return_code": result.return_code,
                "timed_out": result.timed_out,
                "cancelled": result.cancelled,
            },
        )
    return result.finished_at - result.started_at


def _metadata_operation(
    *,
    adapter: MavenBuildSystemAdapter,
    execution: MavenExecutionPlan,
    timeout: float,
) -> BuildOperationSpec:
    spec = adapter.create_compile_classpath_operation(execution)
    return dataclasses.replace(spec, timeout_seconds=timeout)


def _child_text(element: ET.Element, name: str) -> str | None:
    child = element.find(f"./{{*}}{name}")
    if child is None:
        return None
    value = (child.text or "").strip()
    return value or None


def _effective_source_directory(project: ET.Element, module: MavenModule) -> Path:
    build = project.find("./{*}build")
    value = _child_text(
        build if build is not None else ET.Element("build"),
        "sourceDirectory",
    )
    if not value:
        return module.directory / "src" / "main" / "java"
    if "${" in value:
        raise BuildWorldError(
            "SOURCE_ROOT_UNRESOLVED",
            "The effective Maven source directory contains an unresolved expression.",
            suggested_next_step="Use the formal Maven build for this module.",
        )
    path = Path(value)
    if not path.is_absolute():
        path = module.directory / path
    return path.resolve(strict=False)


def _generated_source_roots(module: MavenModule) -> list[tuple[Path, str]]:
    generated = module.directory / "target" / "generated-sources"
    if not generated.is_dir():
        return []
    roots: list[tuple[Path, str]] = []
    for candidate in sorted(path for path in generated.iterdir() if path.is_dir()):
        if not any(candidate.rglob("*.java")):
            continue
        provenance = (
            "COMPILE_TIME_AP_GENERATED"
            if candidate.name.casefold() in {"annotations", "annotationprocessor"}
            else "BOOTSTRAP_GENERATED"
        )
        roots.append((candidate, provenance))
    return roots


def _annotation_processor_model(
    adapter: MavenBuildSystemAdapter,
    project: ET.Element,
) -> dict[str, object]:
    compiler = adapter._find_build_plugin(  # noqa: SLF001
        project, "maven-compiler-plugin"
    )
    configurations = adapter._compiler_configurations(compiler)  # noqa: SLF001
    coordinates: set[tuple[str, str, str]] = set()
    kinds: set[str] = set()
    option_names: set[str] = set()
    explicit_names: set[str] = set()
    for configuration in configurations:
        paths = configuration.find("./{*}annotationProcessorPaths")
        if paths is not None:
            for path in paths.findall("./{*}path"):
                coordinate = tuple(
                    (_child_text(path, name) or "")
                    for name in ("groupId", "artifactId", "version")
                )
                if not all(coordinate) or any("${" in value for value in coordinate):
                    kinds.add("unknown")
                    continue
                coordinates.add(coordinate)
                kinds.add(
                    "lombok"
                    if coordinate[:2] == ("org.projectlombok", "lombok")
                    else "unknown"
                )
        processors = configuration.find("./{*}annotationProcessors")
        if processors is not None:
            for item in processors.findall("./{*}annotationProcessor"):
                value = (item.text or "").strip()
                if value:
                    explicit_names.add(value)
                    kinds.add("lombok" if value.startswith("lombok.") else "unknown")
        compiler_args = configuration.find("./{*}compilerArgs")
        if compiler_args is not None:
            for item in compiler_args.findall("./{*}arg"):
                raw = (item.text or "").strip()
                if raw.startswith("-A") and len(raw) > 2:
                    option_names.add(raw[2:].partition("=")[0])
    identities = [canonical_fingerprint(value) for value in sorted(coordinates)]
    identities.extend(
        canonical_fingerprint(("explicit", value))
        for value in sorted(explicit_names)
    )
    return {
        "coordinates": tuple(sorted(coordinates)),
        "identities": tuple(sorted(identities)),
        "kinds": tuple(sorted(kinds)),
        "option_identities": tuple(
            canonical_fingerprint(value) for value in sorted(option_names)
        ),
    }


def _declared_lombok_artifacts(
    processor_model: dict[str, object],
    *,
    local_repository: Path | None,
) -> tuple[Path, ...]:
    repository = (
        local_repository.resolve(strict=True)
        if local_repository is not None
        else (Path.home() / ".m2" / "repository").resolve(strict=False)
    )
    artifacts: list[Path] = []
    for coordinate in processor_model["coordinates"]:
        group, artifact, version = coordinate
        if (group, artifact) != ("org.projectlombok", "lombok"):
            continue
        path = repository.joinpath(
            *group.split("."), artifact, version, f"{artifact}-{version}.jar"
        )
        if not path.is_file():
            raise BuildWorldError(
                "DECLARED_LOMBOK_ARTIFACT_UNAVAILABLE",
                "The effective Lombok Processor artifact is unavailable locally.",
                suggested_next_step=(
                    "Run the Maven baseline with the configured repository and retry."
                ),
                retryable=True,
            )
        artifacts.append(path.resolve())
    return tuple(artifacts)


def _configuration_fingerprint(
    execution: MavenExecutionPlan, project: ET.Element
) -> str:
    inputs: list[tuple[str, str | None]] = []
    for module in execution.workspace.modules:
        inputs.append((module.relative_path, sha256_file(module.pom_file)))
    for name in ("maven.config", "jvm.config", "extensions.xml"):
        path = execution.workspace.build_root / ".mvn" / name
        inputs.append((f".mvn/{name}", sha256_file(path) if path.is_file() else None))
    settings = execution.preferences.user_settings_file
    inputs.append(("settings.xml", sha256_file(settings) if settings else None))
    inputs.append(("effective-project", canonical_fingerprint(_xml_shape(project))))
    return canonical_fingerprint(inputs)


def _xml_shape(element: ET.Element) -> object:
    return (
        element.tag.rsplit("}", 1)[-1],
        (element.text or "").strip(),
        tuple(sorted(element.attrib.items())),
        tuple(_xml_shape(child) for child in element),
    )


def _project_inputs_fingerprint(
    workspace_root: Path, source_roots: Sequence[Path]
) -> str:
    values: list[tuple[str, str]] = []
    for root in source_roots:
        values.append(
            (root.relative_to(workspace_root).as_posix(), tree_fingerprint(root))
        )
    for path in sorted(workspace_root.rglob("pom.xml")):
        values.append((path.relative_to(workspace_root).as_posix(), sha256_file(path)))
    for name in ("workspace.xml", "misc.xml", "compiler.xml"):
        path = workspace_root / ".idea" / name
        if path.is_file():
            values.append((f".idea/{name}", sha256_file(path)))
    return canonical_fingerprint(values)


def _lombok_config_files(
    snapshot: BuildWorldSnapshot,
) -> tuple[list[tuple[Path, Path]], dict[str, object]]:
    if not snapshot.lombok_dependencies:
        return [], {"status": "not_applicable", "file_count": 0}
    source_files = [
        path for root in snapshot.source_roots for path in root.path.rglob("*.java")
    ]
    try:
        config = discover_lombok_configuration(snapshot.workspace_root, source_files)
    except LombokExperimentError as error:
        raise BuildWorldError(
            error.error_code,
            str(error),
            suggested_next_step=error.suggested_next_step,
            retryable=error.retryable,
        ) from error
    if any(item.imported for item in config.inputs):
        raise BuildWorldError(
            "LOMBOK_CONFIG_LAYOUT_UNREPRESENTABLE",
            "Imported Lombok configuration cannot be mapped into the frozen one-root Worker.",
            suggested_next_step=(
                "Use the formal Maven build or extend the versioned JDT project model."
            ),
        )
    files: list[tuple[Path, Path]] = []
    parent_config_count = 0
    for item in config.inputs:
        source = snapshot.workspace_root / item.relative_path
        relative: Path | None = None
        for root in snapshot.source_roots:
            try:
                relative = Path("src") / source.relative_to(root.path)
                break
            except ValueError:
                continue
        if relative is None:
            parent_config_count += 1
            relative = Path("lombok.config")
        if parent_config_count > 1:
            raise BuildWorldError(
                "LOMBOK_CONFIG_LAYOUT_UNREPRESENTABLE",
                "Multiple parent Lombok configurations cannot be mapped into "
                "the frozen one-root Worker.",
                suggested_next_step=(
                    "Use the formal Maven build or extend the versioned JDT project model."
                ),
            )
        files.append((source, relative))
    return files, {
        "status": "frozen",
        "file_count": len(files),
        "fingerprint": config.fingerprint,
    }


def _private_snapshot_payload(snapshot: BuildWorldSnapshot) -> dict[str, object]:
    return {
        "schema": "jolink.build-world-snapshot.private.v1",
        "workspace_root": str(snapshot.workspace_root),
        "module_root": str(snapshot.module_root),
        "maven_output": str(snapshot.maven_output),
        "source_roots": [
            {
                "path": str(item.path),
                "mount_relative": item.mount_relative.as_posix(),
                "provenance": item.provenance,
                "content_sha256": item.content_sha256,
            }
            for item in snapshot.source_roots
        ],
        "compile_classpath": [
            {"path": str(item.path), "content_sha256": item.content_sha256}
            for item in snapshot.dependencies
        ],
        "snapshot_fingerprint": snapshot.fingerprint,
    }


def _write_private_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _parse_classpath(adapter: MavenBuildSystemAdapter, source: Path) -> list[Path]:
    raw = adapter._read_classpath_file(source)  # noqa: SLF001
    return [Path(item) for item in raw.split(os.pathsep) if item.strip()]


def _lock_for_phase2a(root: Path, explicit: Path | None) -> Path:
    return explicit or root / "locks" / "eclipse-2021-03-lombok-anchor.json"


def _candidate_identity(lock: dict[str, Any], lock_path: Path) -> dict[str, object]:
    return {
        "candidate_id": lock["candidate_id"],
        "lock_sha256": sha256_file(lock_path),
        "worker_artifact_sha256": lock["worker_artifact"]["sha256"],
        "jdt_core_version": next(
            (
                artifact["version"]
                for artifact in lock["artifacts"]
                if artifact["symbolic_name"] == "org.eclipse.jdt.core"
            ),
            None,
        ),
    }


def _report_path(cache_root: Path, attempt_id: str) -> Path:
    reports = cache_root / "reports" / "phase2a"
    reports.mkdir(parents=True, exist_ok=True)
    return reports / f"{attempt_id}.json"


def _assert_shareable_report(
    report: dict[str, Any], *, private_values: Sequence[str]
) -> None:
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
    for value in private_values:
        if value and value in rendered:
            raise BuildWorldError(
                "REPORT_REDACTION_FAILED",
                "The shareable report contains a private Build World value.",
                suggested_next_step=(
                    "Keep the local attempt private and fix report redaction."
                ),
            )


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    repository_root = root.parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-path", type=Path, required=True)
    parser.add_argument("--module")
    parser.add_argument("--maven-executable", type=Path)
    parser.add_argument("--settings-file", type=Path)
    parser.add_argument("--local-repository", type=Path)
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--build-java-home", type=Path, required=True)
    parser.add_argument("--worker-java-home", type=Path, required=True)
    parser.add_argument("--target-java-home", type=Path, required=True)
    parser.add_argument("--lock", type=Path)
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path.home() / ".cache" / "jolink-runtime" / "jdt-poc",
    )
    parser.add_argument("--maven-timeout", type=float, default=900.0)
    parser.add_argument("--worker-timeout", type=float, default=600.0)
    parser.add_argument("--keep-attempt", action="store_true")
    args = parser.parse_args(argv)

    attempt_id = f"phase2a-{uuid.uuid4().hex[:12]}"
    attempts = args.cache_root / "attempts" / "phase2a"
    attempts.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix=f"{attempt_id}-", dir=attempts))
    client: common.WorkerClient | None = None
    supervisor = ProcessSupervisor()
    owner = AttemptToken(attempt_id, 1)
    started = time.monotonic()
    try:
        project = args.project_path.expanduser().resolve(strict=True)
        adapter = MavenBuildSystemAdapter()
        workspace = adapter.resolve_workspace(project)
        module = _select_module(workspace.modules, args.module)
        preferences = _preferences(args)
        build_jdk = _java_candidate(args.build_java_home, "phase2a_build_jdk")
        target_jdk = _java_candidate(args.target_java_home, "phase2a_target_jdk")
        maven = _maven_candidate(
            args.maven_executable,
            project_root=workspace.project_root,
            preferences=preferences,
        )
        intent = LaunchIntent(
            source="phase2a",
            launch_name="phase2a",
            launch_type="java_application",
            main_class="",
            working_directory=module.directory,
            ide_module_name=module.relative_path,
            build_before_run=True,
        )
        execution = adapter.create_execution_plan(
            workspace=workspace,
            module=module,
            intent=intent,
            maven=maven,
            build_jdk=build_jdk,
            preferences=preferences,
            attempt_directory=attempt / "maven-model",
        )

        baseline_log = attempt / "maven-baseline.log"
        baseline_seconds = _run_maven(
            supervisor,
            _maven_operation(
                adapter=adapter,
                execution=execution,
                goals=("clean", "compile"),
                name="phase2a_maven_clean_compile",
                timeout=args.maven_timeout,
                output=baseline_log,
            ),
            owner,
        )
        if not module.output_directory.is_dir():
            raise BuildWorldError(
                "MAVEN_BASELINE_OUTPUT_UNAVAILABLE",
                "Maven baseline produced no selected-module class output.",
                suggested_next_step="Inspect the local Maven baseline log.",
            )
        baseline_target_fingerprint = tree_fingerprint(module.directory / "target")

        metadata_seconds = _run_maven(
            supervisor,
            _metadata_operation(
                adapter=adapter,
                execution=execution,
                timeout=args.maven_timeout,
            ),
            owner,
        )
        effective_root = adapter._read_effective_pom(  # noqa: SLF001
            execution.effective_pom_file
        )
        effective_project = adapter._select_effective_project(  # noqa: SLF001
            effective_root, module
        )
        compiler = adapter._compiler_model(  # noqa: SLF001
            effective_project,
            build_jdk=build_jdk,
            runtime_jdk=target_jdk,
        )
        encoding = adapter._source_encoding(effective_project)  # noqa: SLF001
        source_directory = _effective_source_directory(effective_project, module)
        if not source_directory.is_dir():
            raise BuildWorldError(
                "SOURCE_ROOT_UNAVAILABLE",
                "The selected module has no effective main source root.",
                suggested_next_step="Select a representative Java module.",
            )
        source_roots = [
            describe_source_root(
                source_directory,
                "DECLARED_SOURCE",
                workspace_root=workspace.project_root,
            )
        ]
        for path, provenance in _generated_source_roots(module):
            source_roots.append(
                describe_source_root(
                    path,
                    provenance,
                    workspace_root=workspace.project_root,
                )
            )
        processor_model = _annotation_processor_model(adapter, effective_project)
        compile_classpath = _parse_classpath(
            adapter, execution.compile_classpath_file
        )
        for processor_artifact in _declared_lombok_artifacts(
            processor_model,
            local_repository=preferences.local_repository,
        ):
            if processor_artifact not in compile_classpath:
                compile_classpath.append(processor_artifact)
        project_inputs_before = _project_inputs_fingerprint(
            workspace.project_root, [item.path for item in source_roots]
        )
        private_output = attempt / "worker" / "workspace" / "plain-fixture" / "bin"
        snapshot = create_snapshot(
            workspace_root=workspace.project_root,
            module_root=module.directory,
            maven_output=module.output_directory,
            source_roots=source_roots,
            compile_classpath=compile_classpath,
            private_candidate_output=private_output,
            source_level=int(compiler["source_level"]),
            target_level=int(compiler["target_level"]),
            encoding=encoding,
            configuration_fingerprint=_configuration_fingerprint(
                execution, effective_project
            ),
            current_module_coordinate=adapter._effective_coordinate(  # noqa: SLF001
                effective_project
            ),
            declared_processor_identities=processor_model["identities"],
            declared_processor_kinds=processor_model["kinds"],
            processor_option_identities=processor_model["option_identities"],
        )
        if snapshot.self_output_on_compile_classpath:
            raise BuildWorldError(
                "SELF_OUTPUT_ON_COMPILE_CLASSPATH",
                "The current module Maven output is present on the JDT classpath.",
                suggested_next_step="Discard the attempt and inspect discovery.",
            )
        private_snapshot_path = attempt / "build-world.private.json"
        _write_private_json(private_snapshot_path, _private_snapshot_payload(snapshot))

        worker_attempt = attempt / "worker"
        private_source = worker_attempt / "workspace" / "plain-fixture" / "src"
        config_files, lombok_config = _lombok_config_files(snapshot)
        system_snapshot = common.snapshot_target_system_libraries(
            target_java_home=target_jdk.home,
            attempt=attempt / "target-system",
            helper_source=(
                root
                / "target-system-helper"
                / "src/net/jolink/runtime/jdt/helper/TargetSystemLibraries.java"
            ),
        )
        worker_classpath = attempt / "worker-classpath.private.txt"
        write_worker_classpath(
            system_library_file=system_snapshot["worker_input"],
            snapshot=snapshot,
            output=worker_classpath,
        )

        lock_path = _lock_for_phase2a(root, args.lock)
        lock = common.load_lock(lock_path)
        candidate_root = args.cache_root / "candidates" / lock["candidate_id"]
        common.verify_candidate(lock, candidate_root)
        worker_identity = common.worker_java_identity(args.worker_java_home)
        expected_worker_java = lock.get("worker_build", {}).get(
            "java_home_identity", {}
        ).get("java_binary_sha256")
        if worker_identity["java_binary_sha256"] != expected_worker_java:
            raise BuildWorldError(
                "WORKER_JDK_IDENTITY_MISMATCH",
                "The Worker JDK does not match the locked candidate identity.",
                suggested_next_step="Use the JDK 17 installation used by build_worker.py.",
            )
        lombok = snapshot.lombok_dependencies
        if len(lombok) > 1:
            raise BuildWorldError(
                "LOMBOK_RUNTIME_AMBIGUOUS",
                "Multiple Lombok artifacts exist on the compile classpath.",
                suggested_next_step="Align the project to one Lombok artifact.",
            )
        java_agents = (
            (f"{lombok[0].path}=ECJ",) if lombok else ()
        )
        extra_jvm = (
            ("--add-opens=java.base/java.lang=ALL-UNNAMED",) if lombok else ()
        )
        worker_started = time.monotonic()
        client = common.start_worker(
            lock=lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            attempt=worker_attempt,
            system_libraries_file=worker_classpath,
            instrumentation="enabled",
            timeout=args.worker_timeout,
            java_agents=java_agents,
            extra_jvm_arguments=extra_jvm,
        )
        worker_start_seconds = time.monotonic() - worker_started
        materialization = materialize_private_sources(
            snapshot,
            destination=private_source,
            config_files=config_files,
        )
        build_started = time.monotonic()
        full = client.command("BUILD\tFULL")
        common.require_build_operation_contract(full, operation_kind="FULL")
        jdt_seconds = time.monotonic() - build_started
        diagnostics = common.diagnostics_identity(full)
        diagnostic_summary = classify_diagnostics(diagnostics)
        jdt_compile_ok = full.get("compile_ok") is True and int(
            full.get("error_count", -1)
        ) == 0
        comparison = (
            compare_class_outputs(
                maven_output=module.output_directory,
                jdt_output=private_output,
            )
            if jdt_compile_ok
            else {
                "comparison": "not_run",
                "reason": "jdt_full_compile_failed",
                "class_loading_or_initialization_used": False,
            }
        )
        shutdown = client.close()
        client = None
        target_after = tree_fingerprint(module.directory / "target")
        project_inputs_after = _project_inputs_fingerprint(
            workspace.project_root, [item.path for item in source_roots]
        )
        isolation = {
            "private_workspace": True,
            "jdt_output_outside_project": not private_output.is_relative_to(
                workspace.project_root
            ),
            "maven_target_fingerprint_unchanged_after_jdt": (
                target_after == baseline_target_fingerprint
            ),
            "project_inputs_unchanged_after_jdt": (
                project_inputs_after == project_inputs_before
            ),
            "self_output_on_compile_classpath": False,
            "stale_candidate_output_on_classpath": False,
            "runtime_jdwp_touched": False,
            "public_mcp_api_changed": False,
        }
        tier1_ok = bool(
            comparison.get("tier1", {}).get("status") == "compatible"
        )
        isolation_ok = all(
            value is True
            for key, value in isolation.items()
            if key
            in {
                "private_workspace",
                "jdt_output_outside_project",
                "maven_target_fingerprint_unchanged_after_jdt",
                "project_inputs_unchanged_after_jdt",
            }
        )
        if (
            jdt_compile_ok
            and tier1_ok
            and isolation_ok
            and snapshot.phase2b_incremental_eligible
        ):
            status = "phase2a_passed"
            decision = "GO_FOR_PHASE2B_DESIGN"
        elif jdt_compile_ok and tier1_ok and isolation_ok:
            status = "phase2a_passed_with_incremental_blockers"
            decision = "PHASE2B_BLOCKED_BY_BUILD_WORLD"
        elif not jdt_compile_ok:
            status = "phase2a_jdt_full_failed"
            decision = "BUILD_WORLD_GAP_RECORDED"
        else:
            status = "phase2a_structural_or_isolation_gap"
            decision = "REVIEW_REQUIRED"
        report = {
            "schema": "jolink.jdt-phase2a-report.v1",
            "ok": True,
            "status": status,
            "decision": decision,
            "attempt_id": attempt_id,
            "evidence_scope": "private_real_maven_build_world_phase2a",
            "candidate": _candidate_identity(lock, lock_path),
            "build_world": snapshot.redacted_summary(),
            "maven_baseline": {
                "status": "passed",
                "duration_ms": round(baseline_seconds * 1000, 1),
                "metadata_duration_ms": round(metadata_seconds * 1000, 1),
                "selected_module_identity_sha256": canonical_fingerprint(
                    (
                        module.relative_path,
                        module.group_id,
                        module.artifact_id,
                    )
                ),
                "target_tree_fingerprint": baseline_target_fingerprint,
            },
            "private_materialization": materialization,
            "lombok_configuration": lombok_config,
            "target_system_libraries": {
                key: value
                for key, value in system_snapshot.items()
                if key not in {"worker_input"}
            },
            "jdt_full": {
                "compile_ok": full.get("compile_ok"),
                "operation_ok": full.get("operation_ok"),
                "actual_build_kind": full.get("actual_build_kind"),
                "error_count": full.get("error_count"),
                "warning_count": full.get("warning_count"),
                "worker_start_duration_ms": round(worker_start_seconds * 1000, 1),
                "build_duration_ms": round(jdt_seconds * 1000, 1),
                "diagnostics": diagnostic_summary,
                "raw_diagnostics_in_report": False,
            },
            "cross_compiler_comparison": comparison,
            "isolation": isolation,
            "worker_shutdown": shutdown,
            "sensitive_values": {
                "absolute_project_paths_in_report": False,
                "dependency_coordinates_in_report": False,
                "dependency_paths_in_report": False,
                "source_contents_in_report": False,
                "maven_log_contents_in_report": False,
                "diagnostic_messages_in_report": False,
            },
            "limitations": [
                "Phase 2A proves only Maven baseline to private JDT FULL for one Java 8 module.",
                "Maven/javac and ECJ/JDT byte-for-byte equality is not required or claimed.",
                "Tier 2 compiler-generated class differences are recorded but "
                "are not a Phase 2A gate.",
                "Unknown compile-time annotation processors block Phase 2B "
                "even when this full build succeeds.",
                "No incremental mutation, HotSwap, target JVM, HTTP, or "
                "product integration is exercised.",
            ],
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        }
        _assert_shareable_report(
            report,
            private_values=(
                str(workspace.project_root),
                str(module.directory),
                str(module.output_directory),
                str(preferences.user_settings_file or ""),
                str(preferences.local_repository or ""),
                *(str(item.path) for item in snapshot.dependencies),
                *diagnostics,
            ),
        )
        report_path = _report_path(args.cache_root, attempt_id)
        _write_private_json(report_path, report)
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": status,
                    "decision": decision,
                    "attempt_id": attempt_id,
                    "report_path": str(report_path),
                    "phase2b_incremental_eligible": (
                        snapshot.phase2b_incremental_eligible
                    ),
                    "keep_attempt": args.keep_attempt,
                },
                ensure_ascii=False,
            )
        )
        if not args.keep_attempt:
            shutil.rmtree(attempt)
        return 0
    except (BuildWorldError, MavenResolutionError, common.SmokeError, OSError) as error:
        if isinstance(error, BuildWorldError):
            payload: dict[str, object] = error.as_dict()
        elif isinstance(error, MavenResolutionError):
            payload = {
                "ok": False,
                "error_code": str(error.error_code),
                "error": str(error),
                "retryable": error.retryable,
                "suggested_next_step": error.suggested_next_step,
                **error.context,
            }
        else:
            private_failure = attempt / "failure.private.txt"
            private_failure.write_text(
                f"{type(error).__name__}: {error}\n", encoding="utf-8"
            )
            try:
                private_failure.chmod(0o600)
            except OSError:
                pass
            payload = {
                "ok": False,
                "error_code": "PHASE2A_INFRASTRUCTURE_FAILED",
                "error": "The private Phase 2A infrastructure failed.",
                "retryable": False,
                "suggested_next_step": "Inspect the retained local attempt and retry.",
            }
        payload.update(
            {
                "attempt_id": attempt_id,
                "attempt_retained": True,
                "runtime_jdwp_touched": False,
                "jdt_output_published_to_project": False,
                "formal_maven_baseline_may_have_updated_target": True,
            }
        )
        report_path = _report_path(args.cache_root, attempt_id)
        _write_private_json(report_path, payload)
        print(json.dumps({**payload, "report_path": str(report_path)}), file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()
        supervisor.close(deadline=time.monotonic() + 5.0)
        # Maven clean compile intentionally writes the formal baseline into
        # target/.  Every later step is read-only with respect to that tree;
        # source/POM/IDE inputs and the baseline target fingerprint are the
        # isolation gates.  Failed attempts are retained for local diagnosis.


if __name__ == "__main__":
    raise SystemExit(main())
