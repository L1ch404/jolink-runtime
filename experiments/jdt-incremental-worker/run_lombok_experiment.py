#!/usr/bin/env python3
"""Run the exploratory Phase 1B Lombok 1.18.20 JDT compatibility slice."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import run_bootstrap_smoke as common


def load_lombok_lock(path: Path, *, maven_repository: Path) -> dict[str, Any]:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise common.SmokeError("Unable to read the Phase 1B artifact lock.") from exc
    for name in ("lombok", "slf4j_api"):
        artifact = lock.get(name)
        if not isinstance(artifact, dict):
            raise common.SmokeError("Phase 1B artifact lock is incomplete.")
        path_value = maven_repository / str(artifact.get("relative_maven_path", ""))
        if (
            not path_value.is_file()
            or common.sha256_file(path_value) != artifact.get("sha256")
            or path_value.stat().st_size != artifact.get("bytes")
        ):
            raise common.SmokeError(
                f"Locked Phase 1B artifact is unavailable or changed: {name}"
            )
        artifact["resolved_path"] = path_value.resolve()
    return lock


def class_shape(output: Path) -> dict[str, Any]:
    from jolink_runtime.adapters.java.classfile import parse_class_file

    model_bytes = (output / "example/LombokModel.class").read_bytes()
    model = parse_class_file(model_bytes)
    logging = parse_class_file((output / "example/LoggingFeature.class").read_bytes())
    return {
        "model_fields": sorted(
            (field.name, field.descriptor) for field in model.fields
        ),
        "model_methods": sorted(
            (method.name, method.descriptor) for method in model.methods
        ),
        "logging_fields": sorted(
            (field.name, field.descriptor) for field in logging.fields
        ),
        "activation": {
            "builder_generated": any(
                method.name == "builder" for method in model.methods
            ),
            "getter_generated": any(
                method.name == "getName" for method in model.methods
            ),
            "setter_generated": any(
                method.name == "setCount" for method in model.methods
            ),
            "nonnull_guard_literal_present": (
                b"is marked non-null but is null" in model_bytes
            ),
            "slf4j_field_generated": any(
                field.name == "log"
                and field.descriptor == "Lorg/slf4j/Logger;"
                for field in logging.fields
            ),
        },
    }


def assert_activation_shape(shape: dict[str, Any]) -> None:
    activation = shape.get("activation")
    if not isinstance(activation, dict) or not all(activation.values()):
        raise common.SmokeError("Lombok transform activation evidence is incomplete.")


def require_success(
    frame: dict[str, Any], *, label: str, operation_kind: str = "FULL"
) -> None:
    common.require_build_operation_contract(frame, operation_kind=operation_kind)
    if frame.get("ok") is not True or frame.get("compile_ok") is not True:
        raise common.SmokeError(f"{label} did not compile successfully.")
    if int(frame.get("error_count", -1)) != 0:
        raise common.SmokeError(f"{label} produced compiler errors.")
    if frame.get("generation_publishable") is not False:
        raise common.SmokeError(f"{label} bypassed the Runner publication gate.")


def require_compile_failure(
    frame: dict[str, Any], *, label: str, operation_kind: str = "INCREMENTAL"
) -> None:
    common.require_build_operation_contract(frame, operation_kind=operation_kind)
    if frame.get("compile_ok") is not False:
        raise common.SmokeError(
            f"{label} did not report a compile failure "
            f"(compile_ok={frame.get('compile_ok')!r}, "
            f"outcome={frame.get('build_outcome')!r}, "
            f"units={frame.get('compiled_source_units')!r}, "
            f"errors={frame.get('error_count')!r})."
        )
    if int(frame.get("error_count", 0)) < 1:
        raise common.SmokeError(f"{label} did not report compiler errors.")
    if frame.get("generation_publishable") is not False:
        raise common.SmokeError(f"{label} exposed a failed generation.")


def compiled_source_units(frame: dict[str, Any]) -> set[str]:
    values = frame.get("compiled_source_units")
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        raise common.SmokeError("Worker compiled-source evidence is unavailable.")
    return set(values)


def require_incremental_sources(
    frame: dict[str, Any], *, label: str, required: set[str]
) -> None:
    require_success(frame, label=label, operation_kind="INCREMENTAL")
    if frame.get("actual_build_kind") != "INCREMENTAL":
        raise common.SmokeError(f"{label} was not an incremental build.")
    observed = compiled_source_units(frame)
    if not required.issubset(observed):
        raise common.SmokeError(f"{label} omitted an affected source unit.")


def require_oracle_equal(
    *,
    label: str,
    output: Path,
    frame: dict[str, Any],
    oracle_hashes: dict[str, str],
    oracle_diagnostics: list[str],
) -> dict[str, Any]:
    hashes = common.output_hashes(output)
    diagnostics = common.diagnostics_identity(frame)
    if hashes != oracle_hashes or diagnostics != oracle_diagnostics:
        raise common.SmokeError(f"{label} differs from its clean-full oracle.")
    return {
        "class_sha256": hashes,
        "clean_full_oracle_sha256": oracle_hashes,
        "clean_full_oracle_equal": True,
        "diagnostics": diagnostics,
        "clean_full_oracle_diagnostics": oracle_diagnostics,
        "diagnostics_equal": True,
    }


def require_to_builder_contract(output: Path) -> dict[str, Any]:
    """Prove the generated API used by a downstream source, not just its name."""
    from jolink_runtime.adapters.java.classfile import parse_class_file

    expected_descriptor = "()Lexample/LombokModel$LombokModelBuilder;"
    model = parse_class_file((output / "example/LombokModel.class").read_bytes())
    observed = sorted(
        method.descriptor for method in model.methods if method.name == "toBuilder"
    )
    if observed != [expected_descriptor]:
        raise common.SmokeError(
            "The @Builder(toBuilder=true) generated method descriptor is incorrect."
        )
    if not (output / "example/LombokConsumer.class").is_file():
        raise common.SmokeError(
            "The @Builder(toBuilder=true) downstream consumer did not compile."
        )
    return {
        "generated_method": "toBuilder",
        "expected_descriptor": expected_descriptor,
        "observed_descriptors": observed,
        "downstream_consumer_compiled": True,
    }


def add_to_builder_consumer(source_root: Path) -> None:
    consumer = source_root / "example/LombokConsumer.java"
    original = consumer.read_text(encoding="utf-8")
    updated = original.replace(
        "        model.setCount(model.getCount() + 1);\n",
        (
            "        model.setCount(model.getCount() + 1);\n"
            "        LombokModel copy = model.toBuilder()\n"
            "                .count(3)\n"
            "                .build();\n"
        ),
    ).replace(
        "return model.getName() + \":\" + model.getCount()",
        "return copy.getName() + \":\" + copy.getCount()",
    )
    if updated == original or "model.toBuilder()" not in updated:
        raise common.SmokeError(
            "The @Builder(toBuilder=true) downstream consumer edit was not applied."
        )
    consumer.write_text(updated, encoding="utf-8")


def sampled_process_tree_peak(
    sampler: dict[str, Any], *, started: float, ended: float
) -> dict[str, Any]:
    samples = [
        sample
        for sample in sampler.get("samples", [])
        if isinstance(sample, dict)
        and isinstance(sample.get("monotonic_seconds"), (int, float))
        and started <= sample["monotonic_seconds"] <= ended
    ]
    return {
        "sample_count": len(samples),
        "process_tree_rss_sum_bytes": max(
            (sample["process_tree_rss_sum_bytes"] for sample in samples),
            default=None,
        ),
    }


def start_lombok_worker(
    *,
    common_lock: dict[str, Any],
    candidate_root: Path,
    worker_java_home: Path,
    attempt: Path,
    compile_path_file: Path,
    timeout: float,
    lombok_jar: Path,
) -> common.WorkerClient:
    return common.start_worker(
        lock=common_lock,
        candidate_root=candidate_root,
        worker_java_home=worker_java_home,
        attempt=attempt,
        system_libraries_file=compile_path_file,
        instrumentation="enabled",
        timeout=timeout,
        java_agents=(f"{lombok_jar}=ECJ",),
        extra_jvm_arguments=(
            "--add-opens=java.base/java.lang=ALL-UNNAMED",
        ),
    )


def worker_source_path_evidence(
    worker: common.WorkerClient, *, expected_source: Path
) -> dict[str, Any]:
    location_uri = worker.ready.get("source_location_uri")
    observed_path: Path | None = None
    if isinstance(location_uri, str):
        parsed = urllib.parse.urlparse(location_uri)
        if parsed.scheme == "file":
            path_text = urllib.request.url2pathname(parsed.path)
            if os.name == "nt" and len(path_text) >= 3:
                if path_text[0] in ("/", "\\") and path_text[2] == ":":
                    path_text = path_text[1:]
            observed_path = Path(path_text).resolve()
    expected = expected_source.resolve()
    expected_identity = hashlib.sha256(
        str(expected).encode("utf-8")
    ).hexdigest()
    observed_identity = (
        hashlib.sha256(str(observed_path).encode("utf-8")).hexdigest()
        if observed_path is not None
        else None
    )
    return {
        "resource_full_path": worker.ready.get("source_resource_full_path"),
        "location_uri_scheme": (
            urllib.parse.urlparse(location_uri).scheme
            if isinstance(location_uri, str)
            else None
        ),
        "expected_path_identity_sha256": expected_identity,
        "observed_path_identity_sha256": observed_identity,
        "physical_path_matches_expected": observed_path == expected,
    }


def run_compatibility_probes(
    *,
    args: argparse.Namespace,
    root: Path,
    repository_root: Path,
    attempt_id: str,
    attempt: Path,
    started: float,
    common_lock: dict[str, Any],
    candidate_lock_fingerprint: str,
    candidate_identity: dict[str, Any],
    worker_jdk: dict[str, Any],
    lombok_lock_file_sha256: str,
    lombok_lock: dict[str, Any],
    lombok_jar: Path,
    snapshot: dict[str, Any],
    compile_path: Path,
    fixture: Path,
    fixture_fingerprint: str,
    git: dict[str, Any],
    candidate_root: Path,
    clients: list[common.WorkerClient],
    shutdown: list[dict[str, Any]],
) -> int:
    builder_attempt = attempt / "builder-to-builder-probe"
    builder_attempt.mkdir()
    builder_worker = start_lombok_worker(
        common_lock=common_lock,
        candidate_root=candidate_root,
        worker_java_home=args.worker_java_home,
        attempt=builder_attempt,
        compile_path_file=compile_path,
        timeout=args.timeout,
        lombok_jar=lombok_jar,
    )
    clients.append(builder_worker)
    builder_source = builder_attempt / "workspace/plain-fixture/src"
    builder_path_evidence = worker_source_path_evidence(
        builder_worker, expected_source=builder_source
    )
    try:
        shutil.copytree(fixture, builder_source, dirs_exist_ok=True)
        model = builder_source / "example/LombokModel.java"
        model.write_text(
            model.read_text(encoding="utf-8").replace(
                "@Builder", "@Builder(toBuilder = true)"
            ),
            encoding="utf-8",
        )
        add_to_builder_consumer(builder_source)
        builder_frame = builder_worker.command("BUILD\tFULL")
    finally:
        shutdown.append(builder_worker.close())
        clients.remove(builder_worker)
    builder_stderr = (builder_attempt / "worker.stderr.log").read_text(
        encoding="utf-8", errors="replace"
    )
    builder_supported = builder_frame.get("compile_ok") is True
    builder_contract: dict[str, Any] | None = None
    if builder_supported:
        require_success(
            builder_frame,
            label="Phase 1B @Builder(toBuilder=true) compatibility probe",
        )
        from jolink_runtime.adapters.java.classfile import parse_class_file

        model_class = parse_class_file(
            (
                builder_attempt
                / "workspace/plain-fixture/bin/example/LombokModel.class"
            ).read_bytes()
        )
        builder_supported = any(
            method.name == "toBuilder" for method in model_class.methods
        )
        if builder_supported:
            builder_contract = require_to_builder_contract(
                builder_attempt / "workspace/plain-fixture/bin"
            )
    builder_internal_api_failure = (
        "NoSuchMethodError" in builder_stderr
        and "Expression.print" in builder_stderr
    )
    if not builder_supported and not builder_internal_api_failure:
        raise common.SmokeError(
            "The @Builder(toBuilder=true) probe failed with an unknown signature."
        )

    config_attempt = attempt / "lombok-config-probe"
    config_attempt.mkdir()
    config_worker = start_lombok_worker(
        common_lock=common_lock,
        candidate_root=candidate_root,
        worker_java_home=args.worker_java_home,
        attempt=config_attempt,
        compile_path_file=compile_path,
        timeout=args.timeout,
        lombok_jar=lombok_jar,
    )
    clients.append(config_worker)
    config_source = config_attempt / "workspace/plain-fixture/src"
    config_path_evidence = worker_source_path_evidence(
        config_worker, expected_source=config_source
    )
    try:
        logging = config_source / "example/LoggingFeature.java"
        logging.parent.mkdir(parents=True, exist_ok=True)
        logging.write_text(
            (fixture / "example/LoggingFeature.java")
            .read_text(encoding="utf-8")
            .replace("log.info", "audit.info"),
            encoding="utf-8",
        )
        config_file = config_source / "example/lombok.config"
        config_file.write_text(
            "config.stopBubbling = true\nlombok.log.fieldName = audit\n",
            encoding="utf-8",
        )
        config_frame = config_worker.command("BUILD\tFULL")
    finally:
        shutdown.append(config_worker.close())
        clients.remove(config_worker)
    config_output = config_attempt / "workspace/plain-fixture/bin"
    config_supported = (
        config_frame.get("compile_ok") is True
        and (config_output / "example/LoggingFeature.class").is_file()
    )
    if config_supported:
        require_success(
            config_frame,
            label="Phase 1B lombok.config compatibility probe",
        )
        from jolink_runtime.adapters.java.classfile import parse_class_file

        parsed = parse_class_file(
            (config_output / "example/LoggingFeature.class").read_bytes()
        )
        config_supported = any(
            field.name == "audit"
            and field.descriptor == "Lorg/slf4j/Logger;"
            for field in parsed.fields
        )
    config_diagnostics = common.diagnostics_identity(config_frame)
    config_known_mapping_failure = (
        config_frame.get("compile_ok") is False
        and any("audit cannot be resolved" in item for item in config_diagnostics)
        and any("LoggingFeature.log" in item for item in config_diagnostics)
    )
    if not config_supported and not config_known_mapping_failure:
        raise common.SmokeError(
            "The lombok.config compatibility probe failed with an unknown signature."
        )

    if any(item.get("status") != "settled" for item in shutdown):
        raise common.SmokeError("A compatibility-probe Worker did not settle.")
    post_run_revalidation = common.revalidate_frozen_inputs(
        lock_path=args.lock,
        starting_lock=common_lock,
        candidate_root=candidate_root,
        target_java_home=args.target_java_home,
        attempt=attempt,
        helper_source=(
            root
            / "target-system-helper/src/net/jolink/runtime/jdt/helper/TargetSystemLibraries.java"
        ),
        starting_snapshot=snapshot,
        fixture_roots={"phase1b_lombok": fixture},
        starting_fixture_fingerprints={"phase1b_lombok": fixture_fingerprint},
        repository_root=repository_root,
        starting_git_identity=git,
    )
    load_lombok_lock(
        args.lombok_lock, maven_repository=args.maven_repository
    )
    if common.sha256_file(args.lombok_lock) != lombok_lock_file_sha256:
        raise common.SmokeError(
            "Phase 1B artifact lock changed during the compatibility probes."
        )

    blockers = []
    if not config_supported:
        blockers.append("lombok_config_unresolved")
    if not builder_supported:
        blockers.append("builder_to_builder_incompatible")
    status = (
        "phase_1b_compatibility_dual_probe_passed"
        if not blockers
        else "phase_1b_compatibility_dual_probe_partial"
    )
    report = {
        "schema_version": "jolink-jdt-phase1b-compatibility-v1",
        "ok": True,
        "status": status,
        "evidence_class": "exploratory_non_canonical",
        "attempt_id": attempt_id,
        "candidate_id": common_lock["candidate_id"],
        "candidate_lock_fingerprint": candidate_lock_fingerprint,
        "candidate_identity": candidate_identity,
        "worker_jdk": worker_jdk,
        "phase1b_artifact_lock_fingerprint": lombok_lock_file_sha256,
        "target_system_library_fingerprint": snapshot[
            "system_library_fingerprint"
        ],
        "git_identity": git,
        "lombok_identity": {
            key: value
            for key, value in lombok_lock["lombok"].items()
            if key != "resolved_path"
        },
        "support_identity": {
            key: value
            for key, value in lombok_lock["slf4j_api"].items()
            if key != "resolved_path"
        },
        "integration": lombok_lock["integration"],
        "fixture_fingerprint": fixture_fingerprint,
        "builder_to_builder_probe": {
            "supported": builder_supported,
            "compile_ok": builder_frame.get("compile_ok"),
            "diagnostics": common.diagnostics_identity(builder_frame),
            "ecj_internal_api_failure_matched": builder_internal_api_failure,
            "generated_api_contract": builder_contract,
            "source_path_evidence": builder_path_evidence,
        },
        "lombok_config_probe": {
            "supported": config_supported,
            "compile_ok": config_frame.get("compile_ok"),
            "diagnostics": config_diagnostics,
            "expected_generated_field": "audit",
            "known_default_field_failure_matched": config_known_mapping_failure,
            "source_path_evidence": config_path_evidence,
            "config_relative_path": "example/lombok.config",
        },
        "blockers": blockers,
        "shutdown": shutdown,
        "post_run_input_revalidation": post_run_revalidation,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
    }
    reports = args.cache_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f"{attempt_id}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "status": status,
                "attempt_id": attempt_id,
                "candidate_id": common_lock["candidate_id"],
                "report_path": str(report_path),
                "blockers": blockers,
            },
            ensure_ascii=False,
        )
    )
    if not args.keep_attempt:
        shutil.rmtree(attempt)
    return 0


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    repository_root = root.parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=root / "locks" / "eclipse-4.40-current.json",
    )
    parser.add_argument(
        "--lombok-lock",
        type=Path,
        default=root / "lombok-1.18.20-lock.json",
    )
    parser.add_argument(
        "--maven-repository",
        type=Path,
        default=Path.home() / ".m2" / "repository",
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path.home() / ".cache" / "jolink-runtime" / "jdt-poc",
    )
    parser.add_argument("--worker-java-home", type=Path, required=True)
    parser.add_argument("--target-java-home", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--keep-attempt", action="store_true")
    parser.add_argument(
        "--compatibility-probes-only",
        action="store_true",
        help="Run only lombok.config and @Builder(toBuilder=true) probes.",
    )
    args = parser.parse_args(argv)

    attempt_id = f"phase1b-lombok-{uuid.uuid4().hex[:12]}"
    attempts_root = args.cache_root / "attempts"
    attempts_root.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix=f"{attempt_id}-", dir=attempts_root))
    started = time.monotonic()
    clients: list[common.WorkerClient] = []
    shutdown: list[dict[str, Any]] = []
    primary_sampler: common.ProcessTreeSampler | None = None
    try:
        git = common.git_identity(repository_root)
        lombok_lock_file_sha256 = common.sha256_file(args.lombok_lock)
        common_lock = common.load_lock(args.lock)
        candidate_lock_fingerprint = common.canonical_json_fingerprint(
            common_lock
        )
        candidate_root = args.cache_root / "candidates" / common_lock["candidate_id"]
        common.verify_candidate(common_lock, candidate_root)
        candidate_identity = common.candidate_report_identity(
            common_lock, args.lock
        )
        worker_jdk = common.worker_java_identity(args.worker_java_home)
        expected_worker_java = common_lock.get("worker_build", {}).get(
            "java_home_identity", {}
        ).get("java_binary_sha256")
        if (
            not expected_worker_java
            or worker_jdk["java_binary_sha256"] != expected_worker_java
        ):
            raise common.SmokeError(
                "Worker Java does not match the locked Worker build identity."
            )
        lombok_lock = load_lombok_lock(
            args.lombok_lock,
            maven_repository=args.maven_repository,
        )
        lombok_jar = lombok_lock["lombok"]["resolved_path"]
        slf4j_jar = lombok_lock["slf4j_api"]["resolved_path"]
        snapshot = common.snapshot_target_system_libraries(
            target_java_home=args.target_java_home,
            attempt=attempt,
            helper_source=(
                root
                / "target-system-helper/src/net/jolink/runtime/jdt/helper/TargetSystemLibraries.java"
            ),
        )
        compile_path = attempt / "phase1b-compile-paths.txt"
        compile_path.write_text(
            snapshot["worker_input"].read_text(encoding="utf-8")
            + f"{lombok_jar}\n{slf4j_jar}\n",
            encoding="utf-8",
        )
        fixture = root / "fixtures" / "lombok-java" / "src"
        fixture_fingerprint = common.tree_fingerprint(fixture)

        if args.compatibility_probes_only:
            return run_compatibility_probes(
                args=args,
                root=root,
                repository_root=repository_root,
                attempt_id=attempt_id,
                attempt=attempt,
                started=started,
                common_lock=common_lock,
                candidate_lock_fingerprint=candidate_lock_fingerprint,
                candidate_identity=candidate_identity,
                worker_jdk=worker_jdk,
                lombok_lock_file_sha256=lombok_lock_file_sha256,
                lombok_lock=lombok_lock,
                lombok_jar=lombok_jar,
                snapshot=snapshot,
                compile_path=compile_path,
                fixture=fixture,
                fixture_fingerprint=fixture_fingerprint,
                git=git,
                candidate_root=candidate_root,
                clients=clients,
                shutdown=shutdown,
            )

        primary_attempt = attempt / "primary"
        primary_attempt.mkdir()
        primary = start_lombok_worker(
            common_lock=common_lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            attempt=primary_attempt,
            compile_path_file=compile_path,
            timeout=args.timeout,
            lombok_jar=lombok_jar,
        )
        clients.append(primary)
        primary_sampler = common.ProcessTreeSampler(primary.process.pid)
        primary_sampler.start()
        primary_source = primary_attempt / "workspace/plain-fixture/src"
        shutil.copytree(fixture, primary_source, dirs_exist_ok=True)
        baseline_started = time.monotonic()
        primary_sampler.capture("phase1b_baseline_full_start")
        full = primary.command("BUILD\tFULL")
        primary_sampler.capture("phase1b_baseline_full_end")
        baseline_ended = time.monotonic()
        require_success(full, label="Phase 1B baseline full build")
        output = primary_attempt / "workspace/plain-fixture/bin"
        baseline_hashes = common.output_hashes(output)
        if any(
            common.class_major(output / relative) != 52
            for relative in baseline_hashes
        ):
            raise common.SmokeError(
                "Phase 1B baseline did not produce Java 8 class files."
            )
        shape = class_shape(output)
        assert_activation_shape(shape)

        model_source = primary_source / "example/LombokModel.java"
        original_model = model_source.read_text(encoding="utf-8")
        edited_model = original_model.replace(
            "return value.trim();", 'return "[" + value.trim() + "]";'
        )
        if edited_model == original_model:
            raise common.SmokeError("Phase 1B method-body edit was not applied.")
        model_source.write_text(edited_model, encoding="utf-8")
        incremental = primary.command("BUILD\tINCREMENTAL")
        if incremental.get("ok") is not True or incremental.get("compile_ok") is not True:
            raise common.SmokeError("Phase 1B method-body incremental build failed.")
        if incremental.get("actual_build_kind") != "INCREMENTAL":
            raise common.SmokeError("Phase 1B method-body edit was not incremental.")
        incremental_hashes = common.output_hashes(output)

        oracle_attempt = attempt / "method-body-clean-full-oracle"
        oracle_attempt.mkdir()
        oracle = start_lombok_worker(
            common_lock=common_lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            attempt=oracle_attempt,
            compile_path_file=compile_path,
            timeout=args.timeout,
            lombok_jar=lombok_jar,
        )
        clients.append(oracle)
        oracle_source = oracle_attempt / "workspace/plain-fixture/src"
        shutil.copytree(primary_source, oracle_source, dirs_exist_ok=True)
        oracle_full = oracle.command("BUILD\tFULL")
        require_success(oracle_full, label="Phase 1B method-body oracle")
        oracle_hashes = common.output_hashes(
            oracle_attempt / "workspace/plain-fixture/bin"
        )
        method_body_evidence = require_oracle_equal(
            label="Phase 1B method-body incremental",
            output=output,
            frame=incremental,
            oracle_hashes=oracle_hashes,
            oracle_diagnostics=common.diagnostics_identity(oracle_full),
        )

        oracle_counter = 0

        def clean_full_oracle(
            label: str, *, source_root: Path | None = None
        ) -> tuple[dict[str, str], list[str]]:
            nonlocal oracle_counter
            oracle_counter += 1
            oracle_case_attempt = attempt / (
                f"oracle-{oracle_counter:02d}-{label.replace('_', '-')}"
            )
            oracle_case_attempt.mkdir()
            oracle_client = start_lombok_worker(
                common_lock=common_lock,
                candidate_root=candidate_root,
                worker_java_home=args.worker_java_home,
                attempt=oracle_case_attempt,
                compile_path_file=compile_path,
                timeout=args.timeout,
                lombok_jar=lombok_jar,
            )
            try:
                oracle_case_source = (
                    oracle_case_attempt / "workspace/plain-fixture/src"
                )
                shutil.copytree(
                    primary_source if source_root is None else source_root,
                    oracle_case_source,
                    dirs_exist_ok=True,
                )
                oracle_case_frame = oracle_client.command("BUILD\tFULL")
                require_success(
                    oracle_case_frame,
                    label=f"Phase 1B {label} clean-full oracle",
                )
                return (
                    common.output_hashes(
                        oracle_case_attempt / "workspace/plain-fixture/bin"
                    ),
                    common.diagnostics_identity(oracle_case_frame),
                )
            finally:
                shutdown.append(oracle_client.close())

        model_source.write_text(
            edited_model.replace(
                "private int count;",
                "private int count;\n    private long revision;",
            ),
            encoding="utf-8",
        )
        field_incremental = primary.command("BUILD\tINCREMENTAL")
        require_incremental_sources(
            field_incremental,
            label="Phase 1B generated-accessor field edit",
            required={
                "src/example/LombokModel.java",
                "src/example/LombokConsumer.java",
            },
        )
        field_shape = class_shape(output)
        field_methods = set(map(tuple, field_shape["model_methods"]))
        if not {
            ("getRevision", "()J"),
            ("setRevision", "(J)V"),
        }.issubset(field_methods):
            raise common.SmokeError(
                "Phase 1B field edit did not generate the new accessors."
            )
        field_oracle_hashes, field_oracle_diagnostics = clean_full_oracle(
            "field_edit"
        )
        field_evidence = {
            "actual_build_kind": field_incremental.get("actual_build_kind"),
            "compiled_source_units": field_incremental.get(
                "compiled_source_units"
            ),
            "generated_methods": ["getRevision()J", "setRevision(J)V"],
            **require_oracle_equal(
                label="Phase 1B generated-accessor field edit",
                output=output,
                frame=field_incremental,
                oracle_hashes=field_oracle_hashes,
                oracle_diagnostics=field_oracle_diagnostics,
            ),
        }

        model_with_field = model_source.read_text(encoding="utf-8")
        model_source.write_text(
            model_with_field.replace(
                "import lombok.Data;",
                "import lombok.Getter;\nimport lombok.Setter;",
            ).replace("@Data\n@Builder", "@Getter\n@Setter\n@Builder"),
            encoding="utf-8",
        )
        annotation_incremental = primary.command("BUILD\tINCREMENTAL")
        require_incremental_sources(
            annotation_incremental,
            label="Phase 1B generated-schema annotation edit",
            required={"src/example/LombokModel.java"},
        )
        annotation_shape = class_shape(output)
        annotation_methods = set(map(tuple, annotation_shape["model_methods"]))
        removed_data_methods = {"canEqual", "equals", "hashCode", "toString"}
        if any(name in removed_data_methods for name, _ in annotation_methods):
            raise common.SmokeError(
                "Phase 1B annotation edit left stale @Data-generated methods."
            )
        if not {"getName", "setName", "getRevision", "setRevision"}.issubset(
            {name for name, _ in annotation_methods}
        ):
            raise common.SmokeError(
                "Phase 1B annotation edit lost required accessor methods."
            )
        annotation_oracle_hashes, annotation_oracle_diagnostics = (
            clean_full_oracle("annotation_edit")
        )
        annotation_evidence = {
            "actual_build_kind": annotation_incremental.get(
                "actual_build_kind"
            ),
            "compiled_source_units": annotation_incremental.get(
                "compiled_source_units"
            ),
            "removed_generated_methods": sorted(removed_data_methods),
            "retained_generated_methods": [
                "getName",
                "setName",
                "getRevision",
                "setRevision",
            ],
            **require_oracle_equal(
                label="Phase 1B generated-schema annotation edit",
                output=output,
                frame=annotation_incremental,
                oracle_hashes=annotation_oracle_hashes,
                oracle_diagnostics=annotation_oracle_diagnostics,
            ),
        }

        consumer_source = primary_source / "example/LombokConsumer.java"
        original_consumer = consumer_source.read_text(encoding="utf-8")
        edited_consumer = original_consumer.replace(
            '.count(2)\n                .build();',
            '.count(2)\n                .revision(7L)\n                .build();',
        ).replace(
            'return model.getName() + ":" + model.getCount()',
            'return model.getName() + ":" + model.getCount() + ":" + model.getRevision()',
        )
        if not all(
            token in edited_consumer
            for token in (".revision(7L)", ".getRevision()")
        ):
            raise common.SmokeError(
                "Phase 1B generated-member consumer edit was not applied."
            )
        consumer_source.write_text(edited_consumer, encoding="utf-8")
        consumer_incremental = primary.command("BUILD\tINCREMENTAL")
        require_incremental_sources(
            consumer_incremental,
            label="Phase 1B generated-member consumer edit",
            required={"src/example/LombokConsumer.java"},
        )
        consumer_oracle_hashes, consumer_oracle_diagnostics = clean_full_oracle(
            "consumer_edit"
        )
        consumer_evidence = {
            "actual_build_kind": consumer_incremental.get("actual_build_kind"),
            "compiled_source_units": consumer_incremental.get(
                "compiled_source_units"
            ),
            "generated_members_consumed": [
                "builder.revision(long)",
                "LombokModel.getRevision()",
            ],
            **require_oracle_equal(
                label="Phase 1B generated-member consumer edit",
                output=output,
                frame=consumer_incremental,
                oracle_hashes=consumer_oracle_hashes,
                oracle_diagnostics=consumer_oracle_diagnostics,
            ),
        }

        valid_consumer = consumer_source.read_text(encoding="utf-8")
        consumer_source.write_text(
            valid_consumer.replace(
                "model.getRevision()", "model.getMissingRevision()"
            ),
            encoding="utf-8",
        )
        generated_member_failure = primary.command("BUILD\tINCREMENTAL")
        require_compile_failure(
            generated_member_failure,
            label="Phase 1B generated-member failure",
        )
        if not any(
            "getMissingRevision" in diagnostic
            for diagnostic in common.diagnostics_identity(
                generated_member_failure
            )
        ):
            raise common.SmokeError(
                "Phase 1B generated-member failure lacked diagnostic identity."
            )
        consumer_source.write_text(valid_consumer, encoding="utf-8")
        generated_member_recovery = primary.command("BUILD\tINCREMENTAL")
        require_incremental_sources(
            generated_member_recovery,
            label="Phase 1B generated-member recovery",
            required={"src/example/LombokConsumer.java"},
        )
        recovery_oracle_hashes, recovery_oracle_diagnostics = clean_full_oracle(
            "generated_member_recovery"
        )
        recovery_evidence = {
            "failure": {
                "compile_ok": False,
                "diagnostics": common.diagnostics_identity(
                    generated_member_failure
                ),
                "generation_publishable": generated_member_failure.get(
                    "generation_publishable"
                ),
            },
            "recovery": {
                "actual_build_kind": generated_member_recovery.get(
                    "actual_build_kind"
                ),
                "compiled_source_units": generated_member_recovery.get(
                    "compiled_source_units"
                ),
                **require_oracle_equal(
                    label="Phase 1B generated-member recovery",
                    output=output,
                    frame=generated_member_recovery,
                    oracle_hashes=recovery_oracle_hashes,
                    oracle_diagnostics=recovery_oracle_diagnostics,
                ),
            },
        }

        stable_model = model_source.read_text(encoding="utf-8")
        cycle_sources = {
            "square": stable_model,
            "curly": stable_model.replace(
                'return "[" + value.trim() + "]";',
                'return "{" + value.trim() + "}";',
            ),
        }
        if cycle_sources["square"] == cycle_sources["curly"]:
            raise common.SmokeError("Phase 1B cycle source variants are identical.")
        cycle_oracles: dict[str, dict[str, Any]] = {}
        for state, source_text in cycle_sources.items():
            model_source.write_text(source_text, encoding="utf-8")
            hashes, diagnostics = clean_full_oracle(f"cycle_{state}")
            cycle_oracles[state] = {
                "class_sha256": hashes,
                "diagnostics": diagnostics,
            }

        # Synchronize the primary Worker to the final source write before the
        # warm-up epoch; thereafter odd operations are true no-op requests.
        synchronized = primary.command("BUILD\tINCREMENTAL")
        require_incremental_sources(
            synchronized,
            label="Phase 1B repeated-build synchronization",
            required={"src/example/LombokModel.java"},
        )
        current_cycle_state = "curly"

        def run_cycle(index: int, *, measured: bool) -> dict[str, Any]:
            nonlocal current_cycle_state
            edit = index % 2 == 0
            if edit:
                current_cycle_state = (
                    "square" if current_cycle_state == "curly" else "curly"
                )
                model_source.write_text(
                    cycle_sources[current_cycle_state], encoding="utf-8"
                )
            operation_started = time.monotonic()
            primary_sampler.capture("phase1b_cycle_start")
            frame = primary.command("BUILD\tINCREMENTAL")
            primary_sampler.capture("phase1b_cycle_end")
            operation_ended = time.monotonic()
            if edit:
                require_incremental_sources(
                    frame,
                    label="Phase 1B repeated mixed edit",
                    required={"src/example/LombokModel.java"},
                )
            else:
                common.require_exact_observed_build(
                    frame,
                    label="Phase 1B repeated no-op",
                    actual_build_kind=None,
                    build_outcome="NO_COMPILE",
                    compiled_source_units=[],
                    changed_classes=[],
                    expected_callbacks_seen=False,
                    expected_observer_build_finished=False,
                )
            expected = cycle_oracles[current_cycle_state]
            observed_hashes = common.output_hashes(output)
            observed_diagnostics = common.diagnostics_identity(frame)
            if (
                observed_hashes != expected["class_sha256"]
                or observed_diagnostics != expected["diagnostics"]
            ):
                raise common.SmokeError(
                    "Phase 1B repeated-build output differs from its oracle."
                )
            return {
                "ordinal": index,
                "measured": measured,
                "operation": "edit" if edit else "noop",
                "source_state": current_cycle_state,
                "actual_build_kind": frame.get("actual_build_kind"),
                "build_outcome": frame.get("build_outcome"),
                "compiled_source_units": frame.get("compiled_source_units"),
                "worker_metrics_after_build": frame.get("metrics"),
                "started_monotonic": operation_started,
                "ended_monotonic": operation_ended,
                "elapsed_ms": round(
                    (operation_ended - operation_started) * 1000, 3
                ),
                "oracle_equal": True,
            }

        warmup_cycles: list[dict[str, Any]] = []
        measured_cycles: list[dict[str, Any]] = []
        resource_checkpoints: list[dict[str, Any]] = []
        for epoch in range(11):
            target = warmup_cycles if epoch == 0 else measured_cycles
            for offset in range(10):
                target.append(
                    run_cycle(epoch * 10 + offset, measured=epoch > 0)
                )
            resource_checkpoints.append(
                common.metrics_checkpoint(
                    primary, request_gc=True, sampler=primary_sampler
                )
            )
        time.sleep(30.0)
        final_idle_sample = primary_sampler.capture("phase1b_final_30_second_idle")
        primary_sampler_report = primary_sampler.stop()
        primary_sampler = None
        all_cycles = [*warmup_cycles, *measured_cycles]
        common.annotate_sampled_build_peaks(all_cycles, primary_sampler_report)
        baseline_resource_evidence = {
            "actual_build_kind": full.get("actual_build_kind"),
            "compile_ok": full.get("compile_ok"),
            "worker_metrics_after_build": full.get("metrics"),
            "elapsed_ms": round((baseline_ended - baseline_started) * 1000, 3),
            "sampled_process_tree_peak": sampled_process_tree_peak(
                primary_sampler_report,
                started=baseline_started,
                ended=baseline_ended,
            ),
        }
        resource_decision = common.a9_resource_decision(
            checkpoints=resource_checkpoints,
            sampler=primary_sampler_report,
            build_evidence=[baseline_resource_evidence, *all_cycles],
        )
        repeated_build_evidence = {
            "warmup_operation_count": len(warmup_cycles),
            "measured_operation_count": len(measured_cycles),
            "measured_edit_count": sum(
                item["operation"] == "edit" for item in measured_cycles
            ),
            "measured_noop_count": sum(
                item["operation"] == "noop" for item in measured_cycles
            ),
            "every_operation_oracle_equal": all(
                item["oracle_equal"]
                for item in all_cycles
            ),
            "operations": all_cycles,
        }

        optional_builder_attempt = attempt / "optional-builder-probe"
        optional_builder_attempt.mkdir()
        optional_builder_worker = start_lombok_worker(
            common_lock=common_lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            attempt=optional_builder_attempt,
            compile_path_file=compile_path,
            timeout=args.timeout,
            lombok_jar=lombok_jar,
        )
        clients.append(optional_builder_worker)
        try:
            optional_builder_source = (
                optional_builder_attempt / "workspace/plain-fixture/src"
            )
            shutil.copytree(fixture, optional_builder_source, dirs_exist_ok=True)
            optional_builder_model = (
                optional_builder_source / "example/LombokModel.java"
            )
            optional_builder_model.write_text(
                optional_builder_model.read_text(encoding="utf-8").replace(
                    "@Builder", "@Builder(toBuilder = true)"
                ),
                encoding="utf-8",
            )
            add_to_builder_consumer(optional_builder_source)
            optional_builder_frame = optional_builder_worker.command("BUILD\tFULL")
        finally:
            optional_builder_shutdown = optional_builder_worker.close()
            clients.remove(optional_builder_worker)
            shutdown.append(optional_builder_shutdown)
        optional_builder_stderr = (
            optional_builder_attempt / "worker.stderr.log"
        ).read_text(encoding="utf-8", errors="replace")
        optional_builder_supported = optional_builder_frame.get("compile_ok") is True
        optional_builder_contract: dict[str, Any] | None = None
        optional_builder_oracle: dict[str, Any] | None = None
        if optional_builder_supported:
            require_success(
                optional_builder_frame,
                label="Phase 1B optional @Builder compatibility probe",
            )
            optional_builder_output = (
                optional_builder_attempt / "workspace/plain-fixture/bin"
            )
            optional_builder_contract = require_to_builder_contract(
                optional_builder_output
            )
            (
                optional_builder_oracle_hashes,
                optional_builder_oracle_diagnostics,
            ) = clean_full_oracle(
                "builder_to_builder", source_root=optional_builder_source
            )
            optional_builder_oracle = require_oracle_equal(
                label="Phase 1B optional @Builder compatibility probe",
                output=optional_builder_output,
                frame=optional_builder_frame,
                oracle_hashes=optional_builder_oracle_hashes,
                oracle_diagnostics=optional_builder_oracle_diagnostics,
            )
        optional_builder_internal_api_failure = (
            "NoSuchMethodError" in optional_builder_stderr
            and "Expression.print" in optional_builder_stderr
        )
        if not optional_builder_supported and not optional_builder_internal_api_failure:
            raise common.SmokeError(
                "The optional @Builder compatibility probe failed unexpectedly."
            )
        optional_builder_finding = {
            "feature": "@Builder(toBuilder = true)",
            "status": (
                "passed"
                if optional_builder_supported
                else "unsupported_on_current_candidate"
            ),
            "compile_ok": optional_builder_frame.get("compile_ok"),
            "error_type": (
                None if optional_builder_supported else "NoSuchMethodError"
            ),
            "internal_api_boundary": (
                None
                if optional_builder_supported
                else "ECJ Expression#print signature"
            ),
            "generated_api_contract": optional_builder_contract,
            "clean_full_oracle": optional_builder_oracle,
            "owned_worker_settled": (
                optional_builder_shutdown.get("status") == "settled"
                and optional_builder_shutdown.get("owned_process_tree_absent") is True
            ),
        }

        config_attempt = attempt / "config-probe"
        config_attempt.mkdir()
        config_worker = start_lombok_worker(
            common_lock=common_lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            attempt=config_attempt,
            compile_path_file=compile_path,
            timeout=args.timeout,
            lombok_jar=lombok_jar,
        )
        clients.append(config_worker)
        config_source = config_attempt / "workspace/plain-fixture/src"
        config_path_evidence = worker_source_path_evidence(
            config_worker, expected_source=config_source
        )
        config_logging = config_source / "example/LoggingFeature.java"
        config_logging.parent.mkdir(parents=True, exist_ok=True)
        config_logging.write_text(
            (fixture / "example/LoggingFeature.java")
            .read_text(encoding="utf-8")
            .replace("log.info", "audit.info"),
            encoding="utf-8",
        )
        (config_source / "example/lombok.config").write_text(
            "config.stopBubbling = true\nlombok.log.fieldName = audit\n",
            encoding="utf-8",
        )
        config_frame = config_worker.command("BUILD\tFULL")
        config_output = config_attempt / "workspace/plain-fixture/bin"
        config_supported = (
            config_frame.get("compile_ok") is True
            and "example/LoggingFeature.class" in common.output_hashes(config_output)
        )
        if config_supported:
            require_success(config_frame, label="Phase 1B lombok.config probe")
            from jolink_runtime.adapters.java.classfile import parse_class_file

            config_logging_class = parse_class_file(
                (config_output / "example/LoggingFeature.class").read_bytes()
            )
            config_supported = any(
                field.name == "audit"
                and field.descriptor == "Lorg/slf4j/Logger;"
                for field in config_logging_class.fields
            )
        config_diagnostics = common.diagnostics_identity(config_frame)
        config_known_mapping_failure = (
            config_frame.get("compile_ok") is False
            and any("audit cannot be resolved" in item for item in config_diagnostics)
            and any("LoggingFeature.log" in item for item in config_diagnostics)
        )
        if not config_supported and not config_known_mapping_failure:
            raise common.SmokeError(
                "The lombok.config probe failed with an unknown signature."
            )
        config_oracle: dict[str, Any] | None = None
        if config_supported:
            config_oracle_hashes, config_oracle_diagnostics = clean_full_oracle(
                "lombok_config", source_root=config_source
            )
            config_oracle = require_oracle_equal(
                label="Phase 1B lombok.config probe",
                output=config_output,
                frame=config_frame,
                oracle_hashes=config_oracle_hashes,
                oracle_diagnostics=config_oracle_diagnostics,
            )

        while clients:
            shutdown.append(clients.pop().close())
        if any(item.get("status") != "settled" for item in shutdown):
            raise common.SmokeError("A Phase 1B Worker did not settle.")

        post_run_revalidation = common.revalidate_frozen_inputs(
            lock_path=args.lock,
            starting_lock=common_lock,
            candidate_root=candidate_root,
            target_java_home=args.target_java_home,
            attempt=attempt,
            helper_source=(
                root
                / "target-system-helper/src/net/jolink/runtime/jdt/helper/TargetSystemLibraries.java"
            ),
            starting_snapshot=snapshot,
            fixture_roots={"phase1b_lombok": fixture},
            starting_fixture_fingerprints={
                "phase1b_lombok": fixture_fingerprint
            },
            repository_root=repository_root,
            starting_git_identity=git,
        )
        load_lombok_lock(
            args.lombok_lock,
            maven_repository=args.maven_repository,
        )
        if common.sha256_file(args.lombok_lock) != lombok_lock_file_sha256:
            raise common.SmokeError(
                "Phase 1B artifact lock changed during the evidence run."
            )
        post_run_revalidation.update(
            {
                "phase1b_artifact_lock_unchanged": True,
                "phase1b_artifacts_unchanged": True,
            }
        )

        current_candidate_blockers = []
        if resource_decision["status"] not in {"PASS", "CONDITIONAL"}:
            current_candidate_blockers.append(
                "lombok_worker_resource_gate_not_acceptable"
            )
        if not config_supported:
            current_candidate_blockers.append("lombok_config_unresolved")
        if not optional_builder_supported:
            current_candidate_blockers.append(
                "builder_to_builder_ecj_internal_api_incompatible"
            )

        status = (
            "phase_1b_exploratory_candidate_passed"
            if not current_candidate_blockers
            else "phase_1b_exploratory_partial"
        )
        report = {
            "schema_version": "jolink-jdt-phase1b-v1",
            "ok": True,
            "status": status,
            "evidence_class": "exploratory_non_canonical",
            "attempt_id": attempt_id,
            "candidate_id": common_lock["candidate_id"],
            "candidate_lock_fingerprint": candidate_lock_fingerprint,
            "candidate_identity": candidate_identity,
            "worker_jdk": worker_jdk,
            "phase1b_artifact_lock_fingerprint": lombok_lock_file_sha256,
            "git_identity": git,
            "platform": {
                "operating_system": platform.system(),
                "architecture": platform.machine(),
                "python_version": platform.python_version(),
            },
            "target_system_library_fingerprint": snapshot[
                "system_library_fingerprint"
            ],
            "lombok_identity": {
                key: value
                for key, value in lombok_lock["lombok"].items()
                if key != "resolved_path"
            },
            "support_identity": {
                key: value
                for key, value in lombok_lock["slf4j_api"].items()
                if key != "resolved_path"
            },
            "integration": lombok_lock["integration"],
            "fixture_fingerprint": fixture_fingerprint,
            "baseline": {
                "compile_ok": True,
                "class_major": 52,
                "class_sha256": baseline_hashes,
                "class_shape": shape,
                "downstream_generated_member_calls_compiled": True,
            },
            "method_body_incremental": {
                "actual_build_kind": incremental.get("actual_build_kind"),
                "compiled_source_units": incremental.get("compiled_source_units"),
                **method_body_evidence,
            },
            "generated_accessor_field_edit": field_evidence,
            "generated_schema_annotation_edit": annotation_evidence,
            "generated_member_consumer_edit": consumer_evidence,
            "generated_member_error_recovery": recovery_evidence,
            "repeated_build_stability": repeated_build_evidence,
            "resource_measurement": {
                "decision": resource_decision,
                "baseline": baseline_resource_evidence,
                "checkpoints": resource_checkpoints,
                "process_tree_sampler": primary_sampler_report,
                "final_30_second_idle": final_idle_sample,
            },
            "lombok_config_probe": {
                "status": (
                    "passed"
                    if config_supported
                    else "known_candidate_config_resolution_incompatibility"
                ),
                "compile_ok": config_frame.get("compile_ok"),
                "diagnostics": config_diagnostics,
                "expected_generated_field": "audit",
                "known_failure_signature_matched": config_known_mapping_failure,
                "observed_default_field_in_diagnostics": any(
                    "LoggingFeature.log" in diagnostic
                    for diagnostic in config_diagnostics
                ),
                "source_path_evidence": config_path_evidence,
                "config_relative_path": "example/lombok.config",
                "clean_full_oracle": config_oracle,
            },
            "compatibility_findings": [optional_builder_finding],
            "current_candidate_blockers": current_candidate_blockers,
            "shutdown": shutdown,
            "post_run_input_revalidation": post_run_revalidation,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "limitations": [
                "Phase 1A final Go is not yet recorded, so this cannot be canonical Phase 1B evidence",
                (
                    "lombok.config resolution is incompatible with the current JDT candidate despite an exact source filesystem mapping"
                    if not config_supported
                    else "the bounded lombok.config probe passed"
                ),
                "the required Windows Phase 1B platform run remains pending",
            ],
        }
        reports = args.cache_root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        report_path = reports / f"{attempt_id}.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": status,
                    "attempt_id": attempt_id,
                    "report_path": str(report_path),
                    "config_status": report["lombok_config_probe"]["status"],
                },
                ensure_ascii=False,
            )
        )
        if not args.keep_attempt:
            shutil.rmtree(attempt)
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "attempt_path": str(attempt),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if primary_sampler is not None:
            primary_sampler.stop()
        while clients:
            shutdown.append(clients.pop().close())


if __name__ == "__main__":
    raise SystemExit(main())
