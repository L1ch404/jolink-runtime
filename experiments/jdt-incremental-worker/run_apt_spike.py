#!/usr/bin/env python3
"""Run the bounded Spring configuration-processor APT spike."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import psutil

import run_bootstrap_smoke as common


EXPERIMENT = Path(__file__).resolve().parent
FIXTURE = EXPERIMENT / "fixtures" / "apt-spring-config-java8"
METADATA_RELATIVE = Path("META-INF/spring-configuration-metadata.json")


class AptSpikeError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_fixture_artifacts(
    lock_path: Path, repository: Path
) -> tuple[list[Path], list[Path]]:
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    if payload.get("schema") != "jolink.apt-spring-config-fixture-lock.v1":
        raise AptSpikeError("Unexpected APT fixture lock schema.")
    artifacts: list[Path] = []
    processor_path: list[Path] = []
    for item in payload.get("artifacts", []):
        if not isinstance(item, dict):
            raise AptSpikeError("Invalid APT fixture artifact entry.")
        path = repository / str(item.get("relative_path", ""))
        if not path.is_file():
            raise AptSpikeError("A locked APT fixture artifact is unavailable.")
        if path.stat().st_size != item.get("bytes") or sha256_file(path) != item.get(
            "sha256"
        ):
            raise AptSpikeError("A locked APT fixture artifact changed.")
        artifacts.append(path)
        if item.get("processor_path") is True:
            processor_path.append(path)
    if not artifacts:
        raise AptSpikeError("APT fixture artifact lock is empty.")
    if not processor_path:
        raise AptSpikeError("APT fixture processor path is empty.")
    return artifacts, processor_path


def load_probe_processor_provider_path(path: Path) -> list[Path]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "jolink.maven-build-world-probe.v1":
        raise AptSpikeError("Unexpected Maven Probe snapshot schema.")
    facts = payload.get("annotationProcessing")
    if not isinstance(facts, dict):
        raise AptSpikeError("Maven Probe has no annotation-processing facts.")
    if (
        facts.get("processingMode") != "DEFAULT"
        or facts.get("executionProcessorConfigurationDetected") is not False
        or facts.get("legacyProcessorOptionsDetected") is not False
        or facts.get("legacyProcessorOptionCount") != 0
        or facts.get("procPropertyDetected") is not False
        or facts.get("procPropertySourceCount") != 0
        or facts.get("unmodeledProcessorCompilerArgsDetected") is not False
        or facts.get("unmodeledProcessorCompilerArgCount") != 0
        or facts.get("discoveryMode") != "IMPLICIT_COMPILE_CLASSPATH"
        or facts.get("compileClasspathDiscovery") is not True
    ):
        raise AptSpikeError("Maven Probe processor path mode is unsupported.")
    providers = facts.get("providers")
    artifacts = facts.get("processorProviderArtifactPaths")
    options = facts.get("options")
    explicit_names = facts.get("explicitProcessorNames")
    if (
        not isinstance(providers, list)
        or not isinstance(artifacts, list)
        or not isinstance(options, list)
        or not isinstance(explicit_names, list)
    ):
        raise AptSpikeError("Maven Probe processor facts have an invalid shape.")
    if options:
        raise AptSpikeError(
            "Maven Probe Processor options are not materialized by the Worker."
        )
    if explicit_names:
        raise AptSpikeError(
            "Explicit Processor selection is not supported by the Worker."
        )
    expected = (
        "org.springframework.boot.configurationprocessor."
        "ConfigurationMetadataAnnotationProcessor"
    )
    if expected not in providers:
        raise AptSpikeError("Spring configuration Processor was not discovered.")
    paths: list[Path] = []
    for item in artifacts:
        candidate = Path(str(item)).resolve(strict=True)
        if not candidate.is_file():
            raise AptSpikeError("APT Processor directory is not supported.")
        paths.append(candidate)
    if not paths:
        raise AptSpikeError("Maven Probe processor artifact path is empty.")
    return paths


def candidate_bytes(lock: dict[str, Any]) -> int:
    total = sum(int(item["compressed_bytes"]) for item in lock["artifacts"])
    worker = lock.get("worker_artifact")
    if isinstance(worker, dict):
        total += int(worker["compressed_bytes"])
    return total


def metadata_properties(project: Path) -> list[tuple[str, str | None]]:
    path = project / "bin" / METADATA_RELATIVE
    if not path.is_file():
        raise AptSpikeError("Spring configuration metadata was not generated.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return sorted(
        (str(item.get("name")), item.get("type"))
        for item in payload.get("properties", [])
        if isinstance(item, dict)
    )


def metadata_state(project: Path) -> dict[str, Any]:
    path = project / "bin" / METADATA_RELATIVE
    if not path.is_file():
        return {"exists": False, "properties": []}
    return {"exists": True, "properties": metadata_properties(project)}


def resource_hashes(output: Path) -> dict[str, str]:
    if not output.is_dir():
        return {}
    return {
        path.relative_to(output).as_posix(): sha256_file(path)
        for path in sorted(output.rglob("*"))
        if path.is_file() and path.suffix != ".class"
    }


def output_changes(
    before: dict[str, str], after: dict[str, str]
) -> dict[str, list[str]]:
    return {
        "changed": sorted(
            path for path, digest in after.items() if before.get(path) != digest
        ),
        "deleted": sorted(path for path in before if path not in after),
    }


def worker_rss(pid: int) -> int:
    root = psutil.Process(pid)
    processes = [root, *root.children(recursive=True)]
    total = 0
    for process in processes:
        try:
            total += process.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total


def start_worker(
    *,
    lock: dict[str, Any],
    cache_root: Path,
    worker_java_home: Path,
    attempt: Path,
    classpath_file: Path,
    processor_file: Path | None,
    reuse_existing: bool = False,
) -> tuple[common.WorkerClient, float, int]:
    candidate = cache_root / "candidates" / str(lock["candidate_id"])
    started = time.monotonic()
    client = common.start_worker(
        lock=lock,
        candidate_root=candidate,
        worker_java_home=worker_java_home,
        attempt=attempt,
        system_libraries_file=classpath_file,
        instrumentation="enabled",
        timeout=120,
        source_encoding="UTF-8",
        apt_processors_file=processor_file,
        reuse_existing=reuse_existing,
    )
    return client, round((time.monotonic() - started) * 1000, 1), worker_rss(
        client.process.pid
    )


def build(
    client: common.WorkerClient, kind: str
) -> tuple[dict[str, Any], float, int]:
    sampler = common.ProcessTreeSampler(client.process.pid)
    sampler.start()
    started = time.monotonic()
    frame = client.command(f"BUILD\t{kind}")
    duration = round((time.monotonic() - started) * 1000, 1)
    common.require_build_operation_contract(frame, operation_kind=kind)
    sampled = sampler.stop()
    peak = max(
        (
            int(item["process_tree_rss_sum_bytes"])
            for item in sampled.get("samples", [])
        ),
        default=worker_rss(client.process.pid),
    )
    return frame, duration, peak


def clean_full_oracle(
    *,
    label: str,
    source: Path,
    lock: dict[str, Any],
    cache_root: Path,
    worker_java_home: Path,
    classpath_file: Path,
    processor_file: Path,
    attempt_root: Path,
) -> dict[str, Any]:
    attempt = attempt_root / label
    client, _, _ = start_worker(
        lock=lock,
        cache_root=cache_root,
        worker_java_home=worker_java_home,
        attempt=attempt,
        classpath_file=classpath_file,
        processor_file=processor_file,
    )
    shutil.copytree(
        source,
        attempt / "workspace" / "plain-fixture" / "src",
        dirs_exist_ok=True,
    )
    frame, duration, peak = build(client, "FULL")
    project = attempt / "workspace" / "plain-fixture"
    result = {
        "compile_ok": frame.get("compile_ok"),
        "duration_ms": duration,
        "peak_rss_bytes": peak,
        "class_hashes": common.output_hashes(project / "bin"),
        "resource_hashes": resource_hashes(project / "bin"),
        "metadata": metadata_state(project),
        "shutdown": client.close(),
    }
    return result


def clean_full_fallback(
    client: common.WorkerClient, project: Path
) -> dict[str, Any]:
    clean_frame, clean_ms, clean_peak = build(client, "CLEAN")
    full_frame, full_ms, full_peak = build(client, "FULL")
    return {
        "clean_operation_ok": clean_frame.get("operation_ok"),
        "full_compile_ok": full_frame.get("compile_ok"),
        "clean_duration_ms": clean_ms,
        "full_duration_ms": full_ms,
        "peak_rss_bytes": max(clean_peak, full_peak),
        "class_hashes": common.output_hashes(project / "bin"),
        "resource_hashes": resource_hashes(project / "bin"),
        "metadata": metadata_state(project),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-java-home", type=Path, required=True)
    parser.add_argument("--target-java-home", type=Path, required=True)
    parser.add_argument(
        "--local-repository",
        type=Path,
        default=Path.home() / ".m2" / "repository",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=EXPERIMENT / "locks" / "eclipse-2021-03-apt-spike.json",
    )
    parser.add_argument(
        "--baseline-lock",
        type=Path,
        default=(
            EXPERIMENT
            / "locks"
            / "eclipse-2021-03-no-apt-spike.json"
        ),
    )
    parser.add_argument(
        "--fixture-lock",
        type=Path,
        default=EXPERIMENT / "apt-spring-config-java8-lock.json",
    )
    parser.add_argument(
        "--probe-snapshot",
        type=Path,
        help="Optional Maven Probe snapshot authoritative for Processor artifacts.",
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path.home() / ".cache" / "jolink-runtime" / "jdt-poc",
    )
    parser.add_argument("--keep-attempt", action="store_true")
    args = parser.parse_args(argv)

    attempt = Path(tempfile.mkdtemp(prefix="jolink-apt-spike-"))
    try:
        lock = common.load_lock(args.lock)
        baseline_lock = common.load_lock(args.baseline_lock)
        artifacts, processor_path = load_fixture_artifacts(
            args.fixture_lock, args.local_repository.expanduser().resolve()
        )
        processor_path_provider = "fixture_lock"
        if args.probe_snapshot is not None:
            processor_path = load_probe_processor_provider_path(
                args.probe_snapshot.expanduser().resolve(strict=True)
            )
            processor_path_provider = "maven_probe"
        system = common.snapshot_target_system_libraries(
            target_java_home=args.target_java_home.expanduser().resolve(),
            attempt=attempt / "target-system",
            helper_source=(
                EXPERIMENT
                / "target-system-helper"
                / "src/net/jolink/runtime/jdt/helper/TargetSystemLibraries.java"
            ),
        )
        classpath_file = attempt / "worker-classpath.txt"
        classpath_file.write_text(
            Path(system["worker_input"]).read_text(encoding="utf-8")
            + "".join(f"{path}\n" for path in artifacts),
            encoding="utf-8",
        )
        processor_file = attempt / "apt-processors.txt"
        processor_file.write_text(
            "".join(f"{path}\n" for path in processor_path), encoding="utf-8"
        )

        footprint: dict[str, Any] = {
            "baseline_candidate_bytes": candidate_bytes(baseline_lock),
            "apt_candidate_bytes": candidate_bytes(lock),
            "baseline_candidate_bundle_count": len(baseline_lock["artifacts"]),
            "apt_candidate_bundle_count": len(lock["artifacts"]),
        }
        footprint["candidate_delta_bytes"] = (
            footprint["apt_candidate_bytes"]
            - footprint["baseline_candidate_bytes"]
        )
        footprint["candidate_bundle_delta"] = (
            footprint["apt_candidate_bundle_count"]
            - footprint["baseline_candidate_bundle_count"]
        )

        baseline, baseline_start, baseline_rss = start_worker(
            lock=baseline_lock,
            cache_root=args.cache_root,
            worker_java_home=args.worker_java_home,
            attempt=attempt / "baseline-worker",
            classpath_file=classpath_file,
            processor_file=None,
        )
        footprint["baseline_start_ms"] = baseline_start
        footprint["baseline_ready_rss_bytes"] = baseline_rss
        footprint["baseline_shutdown"] = baseline.close()["status"]

        passive, passive_start, passive_rss = start_worker(
            lock=lock,
            cache_root=args.cache_root,
            worker_java_home=args.worker_java_home,
            attempt=attempt / "apt-passive-worker",
            classpath_file=classpath_file,
            processor_file=None,
        )
        footprint["apt_passive_start_ms"] = passive_start
        footprint["apt_passive_ready_rss_bytes"] = passive_rss
        footprint["apt_passive_shutdown"] = passive.close()["status"]

        active_attempt = attempt / "apt-active-worker"
        active, active_start, active_rss = start_worker(
            lock=lock,
            cache_root=args.cache_root,
            worker_java_home=args.worker_java_home,
            attempt=active_attempt,
            classpath_file=classpath_file,
            processor_file=processor_file,
        )
        source_root = active_attempt / "workspace" / "plain-fixture" / "src"
        shutil.copytree(
            FIXTURE / "src" / "main" / "java",
            source_root,
            dirs_exist_ok=True,
        )
        full_frame, full_ms, full_peak = build(active, "FULL")
        active_project = active_attempt / "workspace" / "plain-fixture"
        full_properties = metadata_properties(active_project)
        full_hashes = common.output_hashes(active_project / "bin")
        full_resources = resource_hashes(active_project / "bin")
        full_ready = dict(active.ready)
        full_shutdown = active.close()

        source = source_root / "example" / "DemoProperties.java"
        original = source.read_text(encoding="utf-8")
        field_anchor = "    private String name;\n"
        methods_anchor = "    public String helper() {\n"
        timeout_field = "    private Integer timeout;\n"
        timeout_methods = (
            "    public Integer getTimeout() {\n"
            "        return timeout;\n"
            "    }\n\n"
            "    public void setTimeout(Integer timeout) {\n"
            "        this.timeout = timeout;\n"
            "    }\n\n"
        )
        if field_anchor not in original or methods_anchor not in original:
            raise AptSpikeError("APT fixture mutation anchors changed.")
        added = original.replace(
            field_anchor, field_anchor + timeout_field
        ).replace(methods_anchor, timeout_methods + methods_anchor)

        active, _, _ = start_worker(
            lock=lock,
            cache_root=args.cache_root,
            worker_java_home=args.worker_java_home,
            attempt=active_attempt,
            classpath_file=classpath_file,
            processor_file=processor_file,
            reuse_existing=True,
        )
        source.write_text(added, encoding="utf-8")
        add_frame, add_ms, add_peak = build(active, "INCREMENTAL")
        add_properties = metadata_properties(active_project)
        add_hashes = common.output_hashes(active_project / "bin")
        add_resources = resource_hashes(active_project / "bin")
        add_shutdown = active.close()
        add_oracle = clean_full_oracle(
            label="add-oracle",
            source=source_root,
            lock=lock,
            cache_root=args.cache_root,
            worker_java_home=args.worker_java_home,
            classpath_file=classpath_file,
            processor_file=processor_file,
            attempt_root=attempt,
        )

        active, _, _ = start_worker(
            lock=lock,
            cache_root=args.cache_root,
            worker_java_home=args.worker_java_home,
            attempt=active_attempt,
            classpath_file=classpath_file,
            processor_file=processor_file,
            reuse_existing=True,
        )
        source.write_text(original, encoding="utf-8")
        delete_frame, delete_ms, delete_peak = build(active, "INCREMENTAL")
        delete_properties = metadata_properties(active_project)
        delete_hashes = common.output_hashes(active_project / "bin")
        delete_resources = resource_hashes(active_project / "bin")
        delete_shutdown = active.close()
        delete_oracle = clean_full_oracle(
            label="delete-oracle",
            source=source_root,
            lock=lock,
            cache_root=args.cache_root,
            worker_java_home=args.worker_java_home,
            classpath_file=classpath_file,
            processor_file=processor_file,
            attempt_root=attempt,
        )

        annotation = '@ConfigurationProperties(prefix = "demo")\n'
        if annotation not in original:
            raise AptSpikeError("APT fixture annotation anchor changed.")
        active, _, _ = start_worker(
            lock=lock,
            cache_root=args.cache_root,
            worker_java_home=args.worker_java_home,
            attempt=active_attempt,
            classpath_file=classpath_file,
            processor_file=processor_file,
            reuse_existing=True,
        )
        source.write_text(original.replace(annotation, ""), encoding="utf-8")
        annotation_frame, annotation_ms, annotation_peak = build(
            active, "INCREMENTAL"
        )
        annotation_metadata = metadata_state(active_project)
        annotation_resources = resource_hashes(active_project / "bin")
        annotation_hashes = common.output_hashes(active_project / "bin")
        annotation_oracle = clean_full_oracle(
            label="annotation-delete-oracle",
            source=source_root,
            lock=lock,
            cache_root=args.cache_root,
            worker_java_home=args.worker_java_home,
            classpath_file=classpath_file,
            processor_file=processor_file,
            attempt_root=attempt,
        )
        annotation_fallback = clean_full_fallback(active, active_project)
        annotation_shutdown = active.close()

        source_attempt = attempt / "source-delete-active"
        source_client, _, _ = start_worker(
            lock=lock,
            cache_root=args.cache_root,
            worker_java_home=args.worker_java_home,
            attempt=source_attempt,
            classpath_file=classpath_file,
            processor_file=processor_file,
        )
        source_case_root = source_attempt / "workspace" / "plain-fixture" / "src"
        shutil.copytree(
            FIXTURE / "src" / "main" / "java",
            source_case_root,
            dirs_exist_ok=True,
        )
        source_project = source_attempt / "workspace" / "plain-fixture"
        source_full_frame, _, _ = build(source_client, "FULL")
        source_resources_before = resource_hashes(source_project / "bin")
        source_file = source_case_root / "example" / "DemoProperties.java"
        source_file.unlink()
        source_frame, source_ms, source_peak = build(
            source_client, "INCREMENTAL"
        )
        source_metadata = metadata_state(source_project)
        source_resources = resource_hashes(source_project / "bin")
        source_hashes = common.output_hashes(source_project / "bin")
        source_oracle = clean_full_oracle(
            label="source-delete-oracle",
            source=source_case_root,
            lock=lock,
            cache_root=args.cache_root,
            worker_java_home=args.worker_java_home,
            classpath_file=classpath_file,
            processor_file=processor_file,
            attempt_root=attempt,
        )
        source_fallback = clean_full_fallback(source_client, source_project)
        source_shutdown = source_client.close()

        result = {
            "schema": "jolink.jdt-apt-spike.v1",
            "ok": True,
            "candidate_id": lock["candidate_id"],
            "apt_root_bundle_count": 4,
            "processor_path_provider": processor_path_provider,
            "forbidden_ui_bundle_count": sum(
                any(
                    token in str(item["symbolic_name"]).casefold()
                    for token in (
                        "apt.ui",
                        "jdt.ui",
                        "eclipse.ui",
                        "swt",
                        "m2e",
                    )
                )
                for item in lock["artifacts"]
            ),
            "footprint": {
                **footprint,
                "apt_active_start_ms": active_start,
                "apt_active_ready_rss_bytes": active_rss,
                "full_peak_rss_bytes": full_peak,
                "add_incremental_peak_rss_bytes": add_peak,
                "delete_incremental_peak_rss_bytes": delete_peak,
                "annotation_delete_peak_rss_bytes": annotation_peak,
                "source_delete_peak_rss_bytes": source_peak,
                "annotation_fallback_peak_rss_bytes": annotation_fallback[
                    "peak_rss_bytes"
                ],
                "source_fallback_peak_rss_bytes": source_fallback[
                    "peak_rss_bytes"
                ],
            },
            "full": {
                "compile_ok": full_frame.get("compile_ok"),
                "error_count": full_frame.get("error_count"),
                "duration_ms": full_ms,
                "properties": full_properties,
                "class_count": len(full_hashes),
                "resource_count": len(full_resources),
                "apt_enabled": full_ready.get("apt_enabled"),
                "factory_path_requested_count": full_ready.get(
                    "apt_factory_path_requested_count"
                ),
                "factory_path_effective_count": full_ready.get(
                    "apt_factory_path_effective_count"
                ),
                "factory_path_verified": full_ready.get(
                    "apt_factory_path_verified"
                ),
                "generated_source_requested": full_ready.get(
                    "apt_generated_source_requested"
                ),
                "generated_source_effective": full_ready.get(
                    "apt_generated_source_effective"
                ),
                "generated_source_verified": full_ready.get(
                    "apt_generated_source_verified"
                ),
                "shutdown": full_shutdown["status"],
            },
            "add_property_incremental": {
                "actual_build_kind": add_frame.get("actual_build_kind"),
                "compiled_source_units": add_frame.get("compiled_source_units"),
                "error_count": add_frame.get("error_count"),
                "duration_ms": add_ms,
                "properties": add_properties,
                "timeout_present": any(
                    name == "demo.timeout" for name, _ in add_properties
                ),
                "class_oracle_equal": add_hashes
                == add_oracle["class_hashes"],
                "metadata_oracle_equal": add_properties
                == add_oracle["metadata"]["properties"],
                "resource_oracle_equal": add_resources
                == add_oracle["resource_hashes"],
                "resource_changes": output_changes(
                    full_resources, add_resources
                ),
                "shutdown": add_shutdown["status"],
            },
            "delete_property_incremental": {
                "actual_build_kind": delete_frame.get("actual_build_kind"),
                "compiled_source_units": delete_frame.get(
                    "compiled_source_units"
                ),
                "error_count": delete_frame.get("error_count"),
                "duration_ms": delete_ms,
                "properties": delete_properties,
                "timeout_absent": all(
                    name != "demo.timeout" for name, _ in delete_properties
                ),
                "class_oracle_equal": delete_hashes
                == delete_oracle["class_hashes"],
                "metadata_oracle_equal": delete_properties
                == delete_oracle["metadata"]["properties"],
                "resource_oracle_equal": delete_resources
                == delete_oracle["resource_hashes"],
                "resource_changes": output_changes(
                    add_resources, delete_resources
                ),
                "shutdown": delete_shutdown["status"],
            },
            "delete_annotation_incremental": {
                "actual_build_kind": annotation_frame.get("actual_build_kind"),
                "compiled_source_units": annotation_frame.get(
                    "compiled_source_units"
                ),
                "error_count": annotation_frame.get("error_count"),
                "duration_ms": annotation_ms,
                "metadata": annotation_metadata,
                "configuration_properties_absent": all(
                    not name.startswith("demo.")
                    for name, _ in annotation_metadata["properties"]
                ),
                "class_oracle_equal": annotation_hashes
                == annotation_oracle["class_hashes"],
                "metadata_oracle_equal": annotation_metadata
                == annotation_oracle["metadata"],
                "resource_oracle_equal": annotation_resources
                == annotation_oracle["resource_hashes"],
                "resource_changes": output_changes(
                    delete_resources, annotation_resources
                ),
                "native_incremental_stale": annotation_resources
                != annotation_oracle["resource_hashes"],
                "clean_full_fallback_oracle_equal": (
                    annotation_fallback["class_hashes"]
                    == annotation_oracle["class_hashes"]
                    and annotation_fallback["resource_hashes"]
                    == annotation_oracle["resource_hashes"]
                    and annotation_fallback["metadata"]
                    == annotation_oracle["metadata"]
                ),
                "fallback_clean_operation_ok": annotation_fallback[
                    "clean_operation_ok"
                ],
                "fallback_full_compile_ok": annotation_fallback[
                    "full_compile_ok"
                ],
                "shutdown": annotation_shutdown["status"],
            },
            "delete_source_incremental": {
                "initial_full_compile_ok": source_full_frame.get("compile_ok"),
                "actual_build_kind": source_frame.get("actual_build_kind"),
                "compiled_source_units": source_frame.get(
                    "compiled_source_units"
                ),
                "error_count": source_frame.get("error_count"),
                "duration_ms": source_ms,
                "metadata": source_metadata,
                "class_count": len(source_hashes),
                "class_oracle_equal": source_hashes
                == source_oracle["class_hashes"],
                "metadata_oracle_equal": source_metadata
                == source_oracle["metadata"],
                "resource_oracle_equal": source_resources
                == source_oracle["resource_hashes"],
                "resource_changes": output_changes(
                    source_resources_before, source_resources
                ),
                "native_incremental_stale": source_resources
                != source_oracle["resource_hashes"],
                "clean_full_fallback_oracle_equal": (
                    source_fallback["class_hashes"]
                    == source_oracle["class_hashes"]
                    and source_fallback["resource_hashes"]
                    == source_oracle["resource_hashes"]
                    and source_fallback["metadata"]
                    == source_oracle["metadata"]
                ),
                "fallback_clean_operation_ok": source_fallback[
                    "clean_operation_ok"
                ],
                "fallback_full_compile_ok": source_fallback[
                    "full_compile_ok"
                ],
                "shutdown": source_shutdown["status"],
            },
        }
        gates = [
            result["full"]["compile_ok"] is True,
            result["full"]["apt_enabled"] is True,
            result["full"]["factory_path_verified"] is True,
            result["full"]["generated_source_verified"] is True,
            result["full"]["properties"]
            == [("demo.name", "java.lang.String")],
            result["add_property_incremental"]["actual_build_kind"]
            == "INCREMENTAL",
            add_oracle["compile_ok"] is True,
            result["add_property_incremental"]["timeout_present"] is True,
            result["add_property_incremental"]["class_oracle_equal"] is True,
            result["add_property_incremental"]["metadata_oracle_equal"] is True,
            result["add_property_incremental"]["resource_oracle_equal"] is True,
            result["delete_property_incremental"]["timeout_absent"] is True,
            delete_oracle["compile_ok"] is True,
            result["delete_property_incremental"]["class_oracle_equal"]
            is True,
            result["delete_property_incremental"]["metadata_oracle_equal"]
            is True,
            result["delete_property_incremental"]["resource_oracle_equal"]
            is True,
            result["delete_annotation_incremental"]["class_oracle_equal"]
            is True,
            annotation_oracle["compile_ok"] is True,
            result["delete_annotation_incremental"]["metadata_oracle_equal"]
            is False,
            result["delete_annotation_incremental"]["resource_oracle_equal"]
            is False,
            result["delete_annotation_incremental"]["native_incremental_stale"]
            is True,
            result["delete_annotation_incremental"]
            ["clean_full_fallback_oracle_equal"]
            is True,
            result["delete_annotation_incremental"]
            ["fallback_clean_operation_ok"]
            is True,
            result["delete_annotation_incremental"]["fallback_full_compile_ok"]
            is True,
            result["delete_source_incremental"]["class_count"] == 0,
            source_oracle["compile_ok"] is True,
            result["delete_source_incremental"]["class_oracle_equal"] is True,
            result["delete_source_incremental"]["metadata_oracle_equal"]
            is False,
            result["delete_source_incremental"]["resource_oracle_equal"]
            is False,
            result["delete_source_incremental"]["native_incremental_stale"]
            is True,
            result["delete_source_incremental"]
            ["clean_full_fallback_oracle_equal"]
            is True,
            result["delete_source_incremental"]["fallback_clean_operation_ok"]
            is True,
            result["delete_source_incremental"]["fallback_full_compile_ok"]
            is True,
        ]
        result["ok"] = all(gates)
        report = attempt / "report.json"
        report.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=False))
        if not result["ok"]:
            return 1
        return 0
    except (AptSpikeError, OSError, ValueError, common.SmokeError) as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "JDT_APT_SPIKE_FAILED",
                    "error_type": type(error).__name__,
                    "message": str(error),
                }
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if not args.keep_attempt:
            shutil.rmtree(attempt, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
