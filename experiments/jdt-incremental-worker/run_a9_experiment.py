#!/usr/bin/env python3
"""Run the frozen A9-S/M/L Headless JDT experiment on the tiny fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import run_bootstrap_smoke as common


def atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class WorkspaceLineage:
    """Runner-owned manifest and clean-shutdown marker for A9-L."""

    def __init__(
        self,
        *,
        root: Path,
        workspace_lineage_id: str,
        candidate_id: str,
        candidate_lock_fingerprint: str,
        bundle_set_fingerprint: str,
        worker_sha256: str,
        target_system_library_fingerprint: str,
        project_model_fingerprint: str,
    ) -> None:
        self.root = root
        self.manifest_path = root / "workspace-lineage-manifest.json"
        self.marker_path = root / "clean-shutdown.marker.json"
        self.claimed_marker_path = root / "clean-shutdown.marker.claimed.json"
        self.identity = {
            "workspace_lineage_id": workspace_lineage_id,
            "candidate_id": candidate_id,
            "candidate_lock_fingerprint": candidate_lock_fingerprint,
            "bundle_set_fingerprint": bundle_set_fingerprint,
            "worker_sha256": worker_sha256,
            "target_system_library_fingerprint": (
                target_system_library_fingerprint
            ),
            "project_model_fingerprint": project_model_fingerprint,
        }

    def write_manifest(
        self, *, last_completed_build_generation_id: str, source: Path
    ) -> dict[str, Any]:
        manifest = {
            **self.identity,
            "last_completed_build_generation_id": (
                last_completed_build_generation_id
            ),
            "saved_source_tree_fingerprint": common.tree_fingerprint(source),
        }
        atomic_json_write(self.manifest_path, manifest)
        return manifest

    def publish_clean_marker(self, *, manifest: dict[str, Any]) -> dict[str, Any]:
        marker = {
            "workspace_lineage_id": self.identity["workspace_lineage_id"],
            "manifest_sha256": hashlib.sha256(
                self.manifest_path.read_bytes()
            ).hexdigest(),
            "last_completed_build_generation_id": manifest[
                "last_completed_build_generation_id"
            ],
        }
        atomic_json_write(self.marker_path, marker)
        return marker

    def consume_for_reopen(self, *, source: Path) -> dict[str, Any]:
        if not self.manifest_path.is_file() or not self.marker_path.is_file():
            return {
                "reusable": False,
                "reason": "missing_manifest_or_clean_shutdown_marker",
            }
        if self.claimed_marker_path.exists():
            self.claimed_marker_path.unlink()
        os.replace(self.marker_path, self.claimed_marker_path)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        marker = json.loads(
            self.claimed_marker_path.read_text(encoding="utf-8")
        )
        if any(manifest.get(key) != value for key, value in self.identity.items()):
            return {"reusable": False, "reason": "lineage_identity_changed"}
        if marker.get("manifest_sha256") != hashlib.sha256(
            self.manifest_path.read_bytes()
        ).hexdigest():
            return {"reusable": False, "reason": "manifest_marker_mismatch"}
        current_source = common.tree_fingerprint(source)
        return {
            "reusable": True,
            "offline_source_delta": current_source
            != manifest["saved_source_tree_fingerprint"],
            "saved_source_tree_fingerprint": manifest[
                "saved_source_tree_fingerprint"
            ],
            "current_source_tree_fingerprint": current_source,
            "manifest": manifest,
        }


def replace_once(path: Path, before: str, after: str) -> None:
    value = path.read_text(encoding="utf-8")
    changed = value.replace(before, after, 1)
    if changed == value:
        raise common.SmokeError(f"A9 mutation anchor is unavailable: {path.name}")
    path.write_text(changed, encoding="utf-8")


def prepare_states(root: Path, fixture: Path) -> dict[str, Path]:
    states: dict[str, Path] = {}

    def create(name: str) -> Path:
        destination = root / name
        shutil.copytree(fixture, destination)
        states[name] = destination
        return destination

    create("baseline")
    leaf = create("leaf")
    replace_once(leaf / "example/Application.java", "return service.calculate(20);", "return service.calculate(21);")
    upstream = create("upstream")
    replace_once(upstream / "example/Api.java", "return value * MULTIPLIER;", "return value * MULTIPLIER + 1;")
    constant = create("constant")
    replace_once(constant / "example/Api.java", "MULTIPLIER = 2", "MULTIPLIER = 3")
    broken = create("broken")
    replace_once(broken / "example/Service.java", "return api.transform(value)", "return missingSymbol(value)")
    deleted = create("deleted")
    (deleted / "example/Legacy.java").unlink()
    return states


def sync_source(source: Path, desired: Path) -> None:
    if source.exists():
        shutil.rmtree(source)
    shutil.copytree(desired, source)


def require_oracle(
    *,
    catalog: common.OracleCatalog,
    source: Path,
    classes: Path,
    frame: dict[str, Any],
    expected_compile_ok: bool,
) -> tuple[str, dict[str, Any]]:
    common.require_build_operation_contract(
        frame, operation_kind=str(frame.get("operation_kind"))
    )
    oracle = catalog.require(source)
    if frame.get("compile_ok") is not expected_compile_ok:
        raise common.SmokeError("A9 compiler outcome differs from expectation.")
    if oracle["compile_ok"] is not expected_compile_ok:
        raise common.SmokeError("A9 oracle compiler outcome differs from expectation.")
    if common.diagnostics_identity(frame) != oracle["diagnostics"]:
        raise common.SmokeError(
            "A9 diagnostics differ from the clean-full oracle: "
            f"actual={common.diagnostics_identity(frame)!r}, "
            f"oracle={oracle['diagnostics']!r}."
        )
    if expected_compile_ok and common.output_hashes(classes) != oracle["output_hashes"]:
        raise common.SmokeError("A9 output differs from the clean-full oracle.")
    if frame.get("generation_publishable") is not False:
        raise common.SmokeError("A9 Worker bypassed the Runner publication gate.")
    if frame.get("publishable_changed_classes") != []:
        raise common.SmokeError("A9 Worker exposed classes before oracle commit.")
    if frame.get("compiler_output_eligible") is not expected_compile_ok:
        raise common.SmokeError("A9 compiler eligibility differs from expectation.")
    return catalog.key(source), oracle


def commit_generation_after_oracle(frame: dict[str, Any]) -> dict[str, Any]:
    """Create Runner-owned publication evidence after oracle validation."""
    if frame.get("compiler_output_eligible") is not True:
        raise common.SmokeError("Cannot commit compiler-ineligible output.")
    return {
        "generation_publishable": True,
        "publication_gate_source": "runner_oracle_commit",
        "publishable_changed_classes": list(frame.get("changed_classes", [])),
    }


def execute_build(
    client: common.WorkerClient, kind: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    request_id = f"request-{uuid.uuid4().hex[:12]}"
    build_generation_id = f"build-{uuid.uuid4().hex[:12]}"
    accepted, terminal = client.async_build(
        request_id=request_id,
        build_generation_id=build_generation_id,
        kind=kind,
    )
    if terminal.get("status") != "BUILD_COMPLETED":
        raise common.SmokeError("A9 build did not produce BUILD_COMPLETED.")
    return accepted, terminal


def run_workload(
    *,
    client: common.WorkerClient,
    source: Path,
    classes: Path,
    states: dict[str, Path],
    catalog: common.OracleCatalog,
    sampler: common.ProcessTreeSampler,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    baseline = states["baseline"]
    sync_source(source, baseline)
    baseline_started = time.monotonic()
    sampler.capture("baseline_full_start")
    baseline_accepted, baseline_frame = execute_build(client, "FULL")
    sampler.capture("baseline_full_end")
    baseline_ended = time.monotonic()
    baseline_oracle_key, _ = require_oracle(
        catalog=catalog,
        source=baseline,
        classes=classes,
        frame=baseline_frame,
        expected_compile_ok=True,
    )
    baseline_fingerprint = common.tree_fingerprint(baseline)
    checkpoints: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []

    sequence = [
        ("leaf_edit", "leaf", True, "INCREMENTAL"),
        ("noop", "leaf", True, "INCREMENTAL"),
        ("leaf_restore", "baseline", True, "INCREMENTAL"),
        ("upstream_edit", "upstream", True, "INCREMENTAL"),
        ("upstream_restore", "baseline", True, "INCREMENTAL"),
        ("constant_edit", "constant", True, "INCREMENTAL"),
        ("constant_restore", "baseline", True, "INCREMENTAL"),
        ("compile_error", "broken", False, "INCREMENTAL"),
        ("error_recovery", "baseline", True, "INCREMENTAL"),
        ("delete_source", "deleted", True, "INCREMENTAL"),
        ("restore_source", "baseline", True, "INCREMENTAL"),
    ]

    for epoch in range(11):
        measured = epoch > 0
        epoch_label = "warmup" if not measured else f"measured_{epoch:02d}"
        for index, (name, state_name, compile_ok, operation_kind) in enumerate(sequence):
            desired = states[state_name]
            before_hashes = common.output_hashes(classes)
            if name != "noop":
                sync_source(source, desired)
            started = time.monotonic()
            sampler.capture("build_boundary_start")
            accepted, frame = execute_build(client, operation_kind)
            sampler.capture("build_boundary_end")
            ended = time.monotonic()
            oracle_key, oracle = require_oracle(
                catalog=catalog,
                source=desired,
                classes=classes,
                frame=frame,
                expected_compile_ok=compile_ok,
            )
            if name == "noop" and frame.get("build_outcome") != "NO_COMPILE":
                raise common.SmokeError("A9 no-op unexpectedly compiled a source.")
            if name == "noop" and common.output_hashes(classes) != before_hashes:
                raise common.SmokeError("A9 no-op changed its pre-request output.")
            if name not in {"noop", "delete_source"} and frame.get(
                "actual_build_kind"
            ) != "INCREMENTAL":
                raise common.SmokeError(
                    "A9 state-changing build was not incremental: "
                    f"operation={name}, actual={frame.get('actual_build_kind')!r}, "
                    f"outcome={frame.get('build_outcome')!r}."
                )
            if name == "delete_source" and not (
                frame.get("actual_build_kind") is None
                and frame.get("build_outcome") == "NO_COMPILE"
                and frame.get("compiled_source_units") == []
                and set(frame.get("deleted_classes", []))
                == set(before_hashes) - set(oracle["output_hashes"])
            ):
                raise common.SmokeError(
                    "A9 source deletion did not remove its class family "
                    "without a compilation callback."
                )
            publication = (
                commit_generation_after_oracle(frame)
                if compile_ok
                else {
                    "generation_publishable": False,
                    "publication_gate_source": "runner_compile_rejection",
                    "publishable_changed_classes": [],
                }
            )
            if common.tree_fingerprint(source) != common.tree_fingerprint(desired):
                raise common.SmokeError("A9 private source state changed unexpectedly.")
            operations.append(
                {
                    "epoch": epoch_label,
                    "measured": measured,
                    "operation_index": index + 1,
                    "operation": name,
                    "source_tree_fingerprint": common.tree_fingerprint(source),
                    "oracle_key": oracle_key,
                    "oracle_cache_result": "hit",
                    "oracle_output_equal": compile_ok,
                    "actual_build_kind": frame.get("actual_build_kind"),
                    "build_outcome": frame.get("build_outcome"),
                    "terminal_status": frame.get("terminal_status"),
                    "compile_ok": frame.get("compile_ok"),
                    "request_id": accepted.get("request_id"),
                    "build_generation_id": accepted.get("build_generation_id"),
                    "protocol_sequences": [
                        accepted.get("protocol_sequence"),
                        frame.get("protocol_sequence"),
                    ],
                    "worker_generation_publishable": frame.get(
                        "generation_publishable"
                    ),
                    "compiler_output_eligible": frame.get(
                        "compiler_output_eligible"
                    ),
                    **publication,
                    "diagnostics": common.diagnostics_identity(frame),
                    "worker_metrics_after_build": frame.get("metrics"),
                    "started_monotonic": started,
                    "ended_monotonic": ended,
                    "elapsed_ms": round((ended - started) * 1000, 3),
                }
            )
        if common.tree_fingerprint(source) != baseline_fingerprint:
            raise common.SmokeError("A9 epoch did not return to the baseline source.")
        checkpoints.append(
            common.metrics_checkpoint(
                client, request_gc=True, sampler=sampler
            )
        )
    baseline_publication = commit_generation_after_oracle(baseline_frame)
    baseline_evidence = {
        "request_id": baseline_accepted.get("request_id"),
        "build_generation_id": baseline_accepted.get("build_generation_id"),
        "protocol_sequences": [
            baseline_accepted.get("protocol_sequence"),
            baseline_frame.get("protocol_sequence"),
        ],
        "actual_build_kind": baseline_frame.get("actual_build_kind"),
        "compile_ok": baseline_frame.get("compile_ok"),
        "worker_generation_publishable": baseline_frame.get(
            "generation_publishable"
        ),
        "compiler_output_eligible": baseline_frame.get(
            "compiler_output_eligible"
        ),
        **baseline_publication,
        "oracle_key": baseline_oracle_key,
        "oracle_cache_result": "hit",
        "oracle_output_equal": True,
        "worker_metrics_after_build": baseline_frame.get("metrics"),
        "started_monotonic": baseline_started,
        "ended_monotonic": baseline_ended,
        "elapsed_ms": round((baseline_ended - baseline_started) * 1000, 3),
    }
    return baseline_evidence, operations, checkpoints


def wait_for_barrier(client: common.WorkerClient, build_id: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.command(f"STATUS\t{build_id}")
        if status.get("barrier_reached") is True:
            return
        time.sleep(0.02)
    raise common.SmokeError("A9 deterministic build barrier was not reached.")


def run_lifecycle(
    *,
    client: common.WorkerClient,
    source: Path,
    classes: Path,
    states: dict[str, Path],
    catalog: common.OracleCatalog,
    cancellation_reason: str,
    deadline_seconds: float | None = None,
) -> dict[str, Any]:
    timeline: list[dict[str, Any]] = []
    sync_source(source, states["baseline"])
    baseline_accepted, baseline = execute_build(client, "FULL")
    require_oracle(
        catalog=catalog,
        source=states["baseline"],
        classes=classes,
        frame=baseline,
        expected_compile_ok=True,
    )
    sync_source(source, states["leaf"])
    request_id = f"request-{uuid.uuid4().hex[:12]}"
    build_id = f"build-{uuid.uuid4().hex[:12]}"
    armed = client.command(f"BARRIER\tARM\t{request_id}\t{build_id}")
    if armed.get("status") != "BARRIER_ARMED":
        raise common.SmokeError("A9 lifecycle barrier was not armed.")
    accepted = client.command(
        f"BUILD_ASYNC\t{request_id}\t{build_id}\tINCREMENTAL"
    )
    timeline.append({"event": "BUILD_ACCEPTED", "at": time.monotonic()})
    if accepted.get("status") != "BUILD_ACCEPTED":
        raise common.SmokeError("A9 asynchronous build was not accepted.")
    client.build_ledger.accept(accepted)
    wait_for_barrier(client, build_id, 5.0)
    timeline.append({"event": "BARRIER_REACHED", "at": time.monotonic()})
    if deadline_seconds is not None:
        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            time.sleep(min(0.01, deadline - time.monotonic()))
        timeline.append({"event": "BUILD_DEADLINE_EXPIRED", "at": time.monotonic()})
    cancelled = client.command(f"CANCEL\t{build_id}")
    timeline.append({"event": "CANCEL_REQUESTED", "at": time.monotonic()})
    if cancelled.get("status") != "CANCEL_REQUESTED":
        raise common.SmokeError("A9 cancellation was not accepted.")
    released = client.command(f"BARRIER\tRELEASE\t{request_id}\t{build_id}")
    timeline.append({"event": "BARRIER_RELEASED", "at": time.monotonic()})
    if released.get("status") != "BARRIER_RELEASED":
        raise common.SmokeError("A9 lifecycle barrier was not released.")
    terminal = client.receive_terminal(build_generation_id=build_id)
    timeline.append({"event": str(terminal.get("status")), "at": time.monotonic()})
    if terminal.get("status") != "BUILD_CANCELLED":
        raise common.SmokeError("A9 cancelled build has the wrong terminal event.")
    if terminal.get("generation_publishable") is not False:
        raise common.SmokeError("A9 cancelled generation was publishable.")
    late_cancel = client.command(f"CANCEL\t{build_id}")
    if late_cancel.get("status") != "ALREADY_FINISHED":
        raise common.SmokeError("A9 cancelled generation did not become terminal.")
    stale_cancel = client.command(f"CANCEL\tstale-{uuid.uuid4().hex[:8]}")
    if stale_cancel.get("error_code") != "STALE_BUILD_ID":
        raise common.SmokeError("A9 stale build id was not rejected.")

    recovery_id = f"recovery-{uuid.uuid4().hex[:12]}"
    clean_accepted, clean = execute_build(client, "CLEAN")
    common.require_build_operation_contract(clean, operation_kind="CLEAN")
    if clean.get("generation_publishable") is not False:
        raise common.SmokeError("A9 CLEAN output was incorrectly publishable.")
    sync_source(source, states["baseline"])
    full_accepted, recovered = execute_build(client, "FULL")
    require_oracle(
        catalog=catalog,
        source=states["baseline"],
        classes=classes,
        frame=recovered,
        expected_compile_ok=True,
    )
    recovery_publication = commit_generation_after_oracle(recovered)
    return {
        "request_id": request_id,
        "build_generation_id": build_id,
        "cancellation_reason": cancellation_reason,
        "deadline_seconds": deadline_seconds,
        "timeline": timeline,
        "baseline": {
            "accepted": baseline_accepted,
            "terminal": baseline,
        },
        "accepted": accepted,
        "cancel": cancelled,
        "terminal": terminal,
        "late_cancel": late_cancel,
        "stale_cancel": stale_cancel,
        "recovery": {
            "recovery_id": recovery_id,
            "state_transitions": [
                "RECOVERY_REQUIRED",
                "RECOVERING",
                "READY",
            ],
            "clean_accepted": clean_accepted,
            "clean": clean,
            "full_accepted": full_accepted,
            "full": recovered,
            "oracle_equal": True,
            **recovery_publication,
            "workspace_state": "READY",
            "runtime_publication_performed": False,
        },
    }


def start_case_worker(
    *,
    case_root: Path,
    lock: dict[str, Any],
    candidate_root: Path,
    worker_java_home: Path,
    system_libraries_file: Path,
    timeout: float,
    reuse_existing: bool = False,
) -> common.WorkerClient:
    case_root.mkdir(parents=True, exist_ok=reuse_existing)
    return common.start_worker(
        lock=lock,
        candidate_root=candidate_root,
        worker_java_home=worker_java_home,
        attempt=case_root,
        system_libraries_file=system_libraries_file,
        instrumentation="enabled",
        timeout=timeout,
        reuse_existing=reuse_existing,
    )


def require_shutdown(report: dict[str, Any]) -> None:
    if (
        report.get("status") != "settled"
        or report.get("forced") is not False
        or report.get("exit_code") != 0
        or report.get("owned_root_identity_absent") is not True
        or report.get("owned_process_tree_absent") is not True
    ):
        raise common.SmokeError("A9 Worker did not settle cooperatively and cleanly.")


def run_stop_after_build_wins(
    *, client: common.WorkerClient, source: Path, states: dict[str, Path]
) -> dict[str, Any]:
    sync_source(source, states["baseline"])
    request_id = f"request-{uuid.uuid4().hex[:12]}"
    build_id = f"build-{uuid.uuid4().hex[:12]}"
    accepted, terminal = client.async_build(
        request_id=request_id,
        build_generation_id=build_id,
        kind="FULL",
    )
    if terminal.get("status") != "BUILD_COMPLETED":
        raise common.SmokeError("A9 build-win case did not complete first.")
    late_cancel = client.command(f"CANCEL\t{build_id}")
    if late_cancel.get("status") != "ALREADY_FINISHED":
        raise common.SmokeError("A9 completed generation accepted a late cancel.")
    shutdown = client.close()
    require_shutdown(shutdown)
    terminal_count = sum(
        frame.get("build_generation_id") == build_id
        and frame.get("status")
        in {"BUILD_COMPLETED", "BUILD_CANCELLED", "BUILD_ABORTED"}
        for frame in client.received_frames
    )
    if terminal_count != 1:
        raise common.SmokeError("A9 build-win case emitted multiple terminal frames.")
    return {
        "winner": "BUILD_COMPLETED",
        "accepted": accepted,
        "terminal": terminal,
        "late_cancel": late_cancel,
        "terminal_frame_count": terminal_count,
        "shutdown": shutdown,
    }


def run_stop_after_cancel_wins(
    *, client: common.WorkerClient, source: Path, states: dict[str, Path]
) -> dict[str, Any]:
    sync_source(source, states["baseline"])
    _, baseline = execute_build(client, "FULL")
    if baseline.get("compile_ok") is not True:
        raise common.SmokeError("A9 stop-race baseline failed.")
    sync_source(source, states["leaf"])
    request_id = f"request-{uuid.uuid4().hex[:12]}"
    build_id = f"build-{uuid.uuid4().hex[:12]}"
    client.command(f"BARRIER\tARM\t{request_id}\t{build_id}")
    accepted = client.command(
        f"BUILD_ASYNC\t{request_id}\t{build_id}\tINCREMENTAL"
    )
    if accepted.get("status") != "BUILD_ACCEPTED":
        raise common.SmokeError("A9 STOP cancel race build was not accepted.")
    client.build_ledger.accept(accepted)
    wait_for_barrier(client, build_id, 5.0)
    cancelled = client.command(f"CANCEL\t{build_id}")
    if cancelled.get("status") != "CANCEL_REQUESTED":
        raise common.SmokeError("A9 STOP race cancellation was not accepted first.")
    client.send("STOP")
    terminal = client.receive_terminal(build_generation_id=build_id)
    stopped = client.receive()
    if terminal.get("status") != "BUILD_CANCELLED":
        raise common.SmokeError("A9 STOP did not produce BUILD_CANCELLED.")
    if stopped.get("status") != "stopped":
        raise common.SmokeError("A9 STOP did not acknowledge bounded close.")
    client.process.wait(timeout=5)
    shutdown = client.close()
    require_shutdown(shutdown)
    terminal_count = sum(
        frame.get("build_generation_id") == build_id
        and frame.get("status")
        in {"BUILD_COMPLETED", "BUILD_CANCELLED", "BUILD_ABORTED"}
        for frame in client.received_frames
    )
    if terminal_count != 1:
        raise common.SmokeError("A9 STOP cancel race emitted multiple terminals.")
    return {
        "winner": "BUILD_CANCELLED",
        "accepted": accepted,
        "cancel": cancelled,
        "terminal": terminal,
        "stop_ack": stopped,
        "terminal_frame_count": terminal_count,
        "shutdown": shutdown,
    }


def run_clean_reopen_case(
    *,
    case_root: Path,
    lock: dict[str, Any],
    candidate_root: Path,
    worker_java_home: Path,
    system_libraries_file: Path,
    timeout: float,
    states: dict[str, Path],
    catalog: common.OracleCatalog,
    lineage: WorkspaceLineage,
) -> dict[str, Any]:
    client = start_case_worker(
        case_root=case_root,
        lock=lock,
        candidate_root=candidate_root,
        worker_java_home=worker_java_home,
        system_libraries_file=system_libraries_file,
        timeout=timeout,
    )
    source = case_root / "workspace/plain-fixture/src"
    classes = case_root / "workspace/plain-fixture/bin"
    sync_source(source, states["baseline"])
    _, baseline = execute_build(client, "FULL")
    require_oracle(
        catalog=catalog,
        source=states["baseline"],
        classes=classes,
        frame=baseline,
        expected_compile_ok=True,
    )
    saved = client.command("SAVE")
    if saved.get("status") != "saved":
        raise common.SmokeError("A9 Worker did not acknowledge SAVE.")
    manifest = lineage.write_manifest(
        last_completed_build_generation_id=str(
            baseline["build_generation_id"]
        ),
        source=source,
    )
    first_shutdown = client.close()
    require_shutdown(first_shutdown)
    marker = lineage.publish_clean_marker(manifest=manifest)

    sync_source(source, states["leaf"])
    reuse = lineage.consume_for_reopen(source=source)
    marker_consumed = not lineage.marker_path.exists()
    if reuse.get("reusable") is not True or reuse.get("offline_source_delta") is not True:
        raise common.SmokeError("A9 clean lineage did not identify offline source delta.")
    reopened = start_case_worker(
        case_root=case_root,
        lock=lock,
        candidate_root=candidate_root,
        worker_java_home=worker_java_home,
        system_libraries_file=system_libraries_file,
        timeout=timeout,
        reuse_existing=True,
    )
    try:
        _, incremental = execute_build(reopened, "INCREMENTAL")
        if incremental.get("actual_build_kind") != "INCREMENTAL":
            raise common.SmokeError("A9 clean reopen silently fell back to full.")
        require_oracle(
            catalog=catalog,
            source=states["leaf"],
            classes=classes,
            frame=incremental,
            expected_compile_ok=True,
        )
        second_save = reopened.command("SAVE")
        if second_save.get("status") != "saved":
            raise common.SmokeError("A9 reopened Worker did not acknowledge SAVE.")
        second_manifest = lineage.write_manifest(
            last_completed_build_generation_id=str(
                incremental["build_generation_id"]
            ),
            source=source,
        )
    finally:
        second_shutdown = reopened.close()
    require_shutdown(second_shutdown)
    second_marker = lineage.publish_clean_marker(manifest=second_manifest)
    return {
        "first_save_ack": saved,
        "first_shutdown": first_shutdown,
        "manifest": manifest,
        "published_clean_marker": marker,
        "marker_consumed_before_reopen": marker_consumed,
        "reuse_decision": reuse,
        "reopen_project_state": reopened.ready.get("workspace_project_state"),
        "incremental": incremental,
        "second_save_ack": second_save,
        "second_manifest": second_manifest,
        "second_published_clean_marker": second_marker,
        "second_shutdown": second_shutdown,
    }


def run_abnormal_exit_case(
    *,
    case_root: Path,
    lock: dict[str, Any],
    candidate_root: Path,
    worker_java_home: Path,
    system_libraries_file: Path,
    timeout: float,
    states: dict[str, Path],
    catalog: common.OracleCatalog,
    lineage: WorkspaceLineage,
) -> dict[str, Any]:
    client = start_case_worker(
        case_root=case_root,
        lock=lock,
        candidate_root=candidate_root,
        worker_java_home=worker_java_home,
        system_libraries_file=system_libraries_file,
        timeout=timeout,
    )
    source = case_root / "workspace/plain-fixture/src"
    sync_source(source, states["baseline"])
    _, baseline = execute_build(client, "FULL")
    require_oracle(
        catalog=catalog,
        source=states["baseline"],
        classes=case_root / "workspace/plain-fixture/bin",
        frame=baseline,
        expected_compile_ok=True,
    )
    lineage.write_manifest(
        last_completed_build_generation_id=str(
            baseline["build_generation_id"]
        ),
        source=source,
    )
    sync_source(source, states["leaf"])
    request_id = f"request-{uuid.uuid4().hex[:12]}"
    build_id = f"build-{uuid.uuid4().hex[:12]}"
    client.command(f"BARRIER\tARM\t{request_id}\t{build_id}")
    accepted = client.command(
        f"BUILD_ASYNC\t{request_id}\t{build_id}\tINCREMENTAL"
    )
    if accepted.get("status") != "BUILD_ACCEPTED":
        raise common.SmokeError("A9 abnormal-exit build was not accepted.")
    client.build_ledger.accept(accepted)
    wait_for_barrier(client, build_id, 5.0)
    crash_source_fingerprint = common.tree_fingerprint(source)
    create_time = client.process_create_time
    client.process.kill()
    client.process.wait(timeout=5)
    terminal = client.receive_terminal(
        build_generation_id=build_id,
        timeout=5.0,
    )
    if (
        terminal.get("status") != "BUILD_ABORTED"
        or terminal.get("terminal_record_source") != "runner"
        or terminal.get("generation_publishable") is not False
    ):
        raise common.SmokeError(
            "A9 Runner did not synthesize the abnormal terminal record."
        )
    terminal_count = sum(
        frame.get("build_generation_id") == build_id
        and frame.get("status") in common.BUILD_TERMINAL_STATUSES
        for frame in [*client.received_frames, terminal]
    )
    if terminal_count != 1:
        raise common.SmokeError(
            "A9 abnormal exit produced more than one terminal record."
        )
    client.stderr_stream.close()
    invalid = lineage.consume_for_reopen(source=source)
    if invalid.get("reusable") is not False:
        raise common.SmokeError("A9 abnormal exit incorrectly reused saved state.")
    identity_absent = True
    if create_time is not None:
        try:
            identity_absent = abs(
                common.psutil.Process(client.process.pid).create_time() - create_time
            ) > 0.01
        except common.psutil.NoSuchProcess:
            identity_absent = True
    fresh_root = case_root.parent / "abnormal-exit-fallback"
    fresh_identity = {
        key: value
        for key, value in lineage.identity.items()
        if key != "workspace_lineage_id"
    }
    fresh_lineage = WorkspaceLineage(
        root=fresh_root,
        workspace_lineage_id=f"workspace-{uuid.uuid4().hex[:12]}",
        **fresh_identity,
    )
    fresh_client = start_case_worker(
        case_root=fresh_root,
        lock=lock,
        candidate_root=candidate_root,
        worker_java_home=worker_java_home,
        system_libraries_file=system_libraries_file,
        timeout=timeout,
    )
    fresh_source = fresh_root / "workspace/plain-fixture/src"
    fresh_classes = fresh_root / "workspace/plain-fixture/bin"
    try:
        # Preserve the exact source world that existed when the old Worker
        # crashed. Discard compiler state, never the user's current sources.
        sync_source(fresh_source, source)
        fresh_source_fingerprint = common.tree_fingerprint(fresh_source)
        if fresh_source_fingerprint != crash_source_fingerprint:
            raise common.SmokeError(
                "A9 abnormal fallback did not preserve crash-time source state."
            )
        fresh_accepted, fresh_full = execute_build(fresh_client, "FULL")
        require_oracle(
            catalog=catalog,
            source=source,
            classes=fresh_classes,
            frame=fresh_full,
            expected_compile_ok=True,
        )
        fresh_publication = commit_generation_after_oracle(fresh_full)
    finally:
        fresh_shutdown = fresh_client.close()
    require_shutdown(fresh_shutdown)
    if fresh_lineage.identity["workspace_lineage_id"] == lineage.identity[
        "workspace_lineage_id"
    ]:
        raise common.SmokeError("A9 abnormal fallback reused the old lineage id.")
    return {
        "exit_code": client.process.returncode,
        "accepted": accepted,
        "terminal": terminal,
        "terminal_record_count": terminal_count,
        "clean_marker_present": lineage.marker_path.exists(),
        "reuse_decision": invalid,
        "lineage_discarded": True,
        "owned_root_identity_absent": identity_absent,
        "fresh_fallback": {
            "old_workspace_lineage_id": lineage.identity[
                "workspace_lineage_id"
            ],
            "new_workspace_lineage_id": fresh_lineage.identity[
                "workspace_lineage_id"
            ],
            "workspace_project_state": fresh_client.ready.get(
                "workspace_project_state"
            ),
            "invalidated_source_tree_fingerprint": (
                crash_source_fingerprint
            ),
            "fresh_fallback_source_tree_fingerprint": (
                fresh_source_fingerprint
            ),
            "source_continuity_preserved": True,
            "accepted": fresh_accepted,
            "full": fresh_full,
            "oracle_equal": True,
            **fresh_publication,
            "shutdown": fresh_shutdown,
        },
    }


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=root / "locks/eclipse-4.40-current-diagnostics-v2.json",
    )
    parser.add_argument("--cache-root", type=Path, default=Path.home() / ".cache/jolink-runtime/jdt-poc")
    parser.add_argument("--worker-java-home", type=Path, required=True)
    parser.add_argument("--target-java-home", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--keep-attempt", action="store_true")
    args = parser.parse_args(argv)

    attempt_id = f"a9-{uuid.uuid4().hex[:12]}"
    attempt = Path(tempfile.mkdtemp(prefix=f"{attempt_id}-", dir=args.cache_root / "attempts"))
    shutdown_reports: dict[str, dict[str, Any]] = {}
    owned_worker_pids: list[int] = []
    started = time.monotonic()
    repository_root = root.parents[1]
    starting_git_identity = common.git_identity(repository_root)
    workload_lineage_id = f"workspace-{uuid.uuid4().hex[:12]}"
    try:
        lock = common.load_lock(args.lock)
        starting_lock_fingerprint = common.canonical_json_fingerprint(lock)
        candidate_root = args.cache_root / "candidates" / lock["candidate_id"]
        common.verify_candidate(lock, candidate_root)
        snapshot = common.snapshot_target_system_libraries(
            target_java_home=args.target_java_home,
            attempt=attempt,
            helper_source=root / "target-system-helper/src/net/jolink/runtime/jdt/helper/TargetSystemLibraries.java",
        )
        state_root = attempt / "source-states"
        state_root.mkdir()
        states = prepare_states(state_root, root / "fixtures/a9-mixed-java/src")
        fixture_roots = {"a9_mixed_java": root / "fixtures/a9-mixed-java/src"}
        fixture_fingerprints = {
            name: common.tree_fingerprint(path)
            for name, path in fixture_roots.items()
        }
        catalog = common.OracleCatalog(
            lock=lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            system_libraries_file=snapshot["worker_input"],
            timeout=args.timeout,
            attempt_root=attempt / "oracle-catalog",
            candidate_id=lock["candidate_id"],
            system_library_fingerprint=snapshot["system_library_fingerprint"],
            shutdown_reports=shutdown_reports,
            owned_worker_pids=owned_worker_pids,
        )
        catalog.attempt_root.mkdir()
        catalog.precompute(states)

        workload_attempt = attempt / "a9-s-m"
        workload_attempt.mkdir()
        client = common.start_worker(
            lock=lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            attempt=workload_attempt,
            system_libraries_file=snapshot["worker_input"],
            instrumentation="enabled",
            timeout=args.timeout,
        )
        owned_worker_pids.append(client.process.pid)
        sampler = common.ProcessTreeSampler(client.process.pid)
        sampler.start()
        try:
            source = workload_attempt / "workspace/plain-fixture/src"
            classes = workload_attempt / "workspace/plain-fixture/bin"
            baseline_evidence, operations, checkpoints = run_workload(
                client=client,
                source=source,
                classes=classes,
                states=states,
                catalog=catalog,
                sampler=sampler,
            )
            time.sleep(30.0)
            idle_sample = sampler.capture("final_30_second_idle")
        finally:
            sampler_report = sampler.stop()
            shutdown_reports["a9_workload"] = client.close()
        common.annotate_sampled_build_peaks(operations, sampler_report)
        baseline_peak = {
            "sample_count": len(
                [
                    sample
                    for sample in sampler_report["samples"]
                    if baseline_evidence["started_monotonic"]
                    <= sample["monotonic_seconds"]
                    <= baseline_evidence["ended_monotonic"]
                ]
            ),
            "process_tree_rss_sum_bytes": max(
                (
                    sample["process_tree_rss_sum_bytes"]
                    for sample in sampler_report["samples"]
                    if baseline_evidence["started_monotonic"]
                    <= sample["monotonic_seconds"]
                    <= baseline_evidence["ended_monotonic"]
                ),
                default=None,
            ),
        }
        baseline_evidence.pop("started_monotonic")
        baseline_evidence.pop("ended_monotonic")
        baseline_evidence["sampled_process_tree_peak"] = baseline_peak

        lifecycle_root = attempt / "a9-l"
        lifecycle_attempt = lifecycle_root / "explicit-cancel"
        lifecycle_client = start_case_worker(
            case_root=lifecycle_attempt,
            lock=lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            system_libraries_file=snapshot["worker_input"],
            timeout=args.timeout,
        )
        owned_worker_pids.append(lifecycle_client.process.pid)
        try:
            explicit_cancel = run_lifecycle(
                client=lifecycle_client,
                source=lifecycle_attempt / "workspace/plain-fixture/src",
                classes=lifecycle_attempt / "workspace/plain-fixture/bin",
                states=states,
                catalog=catalog,
                cancellation_reason="explicit_cancel",
            )
        finally:
            shutdown_reports["a9_lifecycle_explicit_cancel"] = lifecycle_client.close()

        timeout_attempt = lifecycle_root / "deadline-cancel"
        timeout_client = start_case_worker(
            case_root=timeout_attempt,
            lock=lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            system_libraries_file=snapshot["worker_input"],
            timeout=args.timeout,
        )
        owned_worker_pids.append(timeout_client.process.pid)
        try:
            deadline_cancel = run_lifecycle(
                client=timeout_client,
                source=timeout_attempt / "workspace/plain-fixture/src",
                classes=timeout_attempt / "workspace/plain-fixture/bin",
                states=states,
                catalog=catalog,
                cancellation_reason="build_deadline_expired",
                deadline_seconds=0.1,
            )
        finally:
            shutdown_reports["a9_lifecycle_deadline_cancel"] = timeout_client.close()

        build_wins_attempt = lifecycle_root / "stop-build-wins"
        build_wins_client = start_case_worker(
            case_root=build_wins_attempt,
            lock=lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            system_libraries_file=snapshot["worker_input"],
            timeout=args.timeout,
        )
        owned_worker_pids.append(build_wins_client.process.pid)
        stop_after_build_wins = run_stop_after_build_wins(
            client=build_wins_client,
            source=build_wins_attempt / "workspace/plain-fixture/src",
            states=states,
        )
        shutdown_reports["a9_stop_after_build_wins"] = stop_after_build_wins[
            "shutdown"
        ]

        cancel_wins_attempt = lifecycle_root / "stop-cancel-wins"
        cancel_wins_client = start_case_worker(
            case_root=cancel_wins_attempt,
            lock=lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            system_libraries_file=snapshot["worker_input"],
            timeout=args.timeout,
        )
        owned_worker_pids.append(cancel_wins_client.process.pid)
        stop_after_cancel_wins = run_stop_after_cancel_wins(
            client=cancel_wins_client,
            source=cancel_wins_attempt / "workspace/plain-fixture/src",
            states=states,
        )
        shutdown_reports["a9_stop_after_cancel_wins"] = stop_after_cancel_wins[
            "shutdown"
        ]

        lineage_identity = {
            "candidate_id": lock["candidate_id"],
            "candidate_lock_fingerprint": starting_lock_fingerprint,
            "bundle_set_fingerprint": common.canonical_json_fingerprint(
                lock["artifacts"]
            ),
            "worker_sha256": lock["worker_artifact"]["sha256"],
            "target_system_library_fingerprint": snapshot[
                "system_library_fingerprint"
            ],
            "project_model_fingerprint": catalog.project_model_fingerprint,
        }
        clean_reopen_root = lifecycle_root / "clean-reopen"
        clean_lineage = WorkspaceLineage(
            root=clean_reopen_root,
            workspace_lineage_id=f"workspace-{uuid.uuid4().hex[:12]}",
            **lineage_identity,
        )
        clean_reopen = run_clean_reopen_case(
            case_root=clean_reopen_root,
            lock=lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            system_libraries_file=snapshot["worker_input"],
            timeout=args.timeout,
            states=states,
            catalog=catalog,
            lineage=clean_lineage,
        )
        shutdown_reports["a9_clean_reopen_initial"] = clean_reopen[
            "first_shutdown"
        ]
        shutdown_reports["a9_clean_reopen_second"] = clean_reopen[
            "second_shutdown"
        ]

        abnormal_root = lifecycle_root / "abnormal-exit"
        abnormal_lineage = WorkspaceLineage(
            root=abnormal_root,
            workspace_lineage_id=f"workspace-{uuid.uuid4().hex[:12]}",
            **lineage_identity,
        )
        abnormal_exit = run_abnormal_exit_case(
            case_root=abnormal_root,
            lock=lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            system_libraries_file=snapshot["worker_input"],
            timeout=args.timeout,
            states=states,
            catalog=catalog,
            lineage=abnormal_lineage,
        )

        lifecycle = {
            "explicit_cancel_recovery": explicit_cancel,
            "deadline_cancel_recovery": deadline_cancel,
            "stop_after_build_wins": stop_after_build_wins,
            "stop_after_cancel_wins": stop_after_cancel_wins,
            "clean_reopen_offline_delta": clean_reopen,
            "abnormal_exit_invalidation": abnormal_exit,
        }

        resource_decision = common.a9_resource_decision(
            checkpoints=checkpoints,
            sampler=sampler_report,
            build_evidence=[baseline_evidence, *operations],
        )
        measured = [item for item in operations if item["measured"]]
        warmup = [item for item in operations if not item["measured"]]
        if len(warmup) != 11 or len(measured) != 110:
            raise common.SmokeError("A9 workload request count is incorrect.")
        if resource_decision["status"] != "PASS":
            raise common.SmokeError(
                "A9 overall PASS requires an A9-M PASS decision: "
                f"decision={resource_decision}, "
                "sampling="
                f"{ {key: sampler_report.get(key) for key in ('sample_count', 'scheduled_sample_count', 'expected_interval_count', 'observed_interval_ratio', 'max_gap_ms', 'coverage_status')} }"
            )
        if any(
            report.get("status") != "settled"
            or report.get("forced") is not False
            or report.get("owned_root_identity_absent") is not True
            or report.get("owned_process_tree_absent") is not True
            for report in shutdown_reports.values()
        ):
            raise common.SmokeError("An A9 Worker did not settle cooperatively.")
        input_revalidation = common.revalidate_frozen_inputs(
            lock_path=args.lock,
            starting_lock=lock,
            candidate_root=candidate_root,
            target_java_home=args.target_java_home,
            attempt=attempt,
            helper_source=root
            / "target-system-helper/src/net/jolink/runtime/jdt/helper/TargetSystemLibraries.java",
            starting_snapshot=snapshot,
            fixture_roots=fixture_roots,
            starting_fixture_fingerprints=fixture_fingerprints,
            repository_root=repository_root,
            starting_git_identity=starting_git_identity,
        )
        report = {
            "schema_version": "jolink-jdt-a9-v1",
            "ok": True,
            "status": "a9_evidence_passed",
            "attempt_id": attempt_id,
            "candidate_id": lock["candidate_id"],
            "candidate_lock_fingerprint": starting_lock_fingerprint,
            "git_identity": starting_git_identity,
            "target_system_library_fingerprint": snapshot[
                "system_library_fingerprint"
            ],
            "workspace_lineage_ids": {
                "workload": workload_lineage_id,
                "clean_reopen": clean_lineage.identity[
                    "workspace_lineage_id"
                ],
                "abnormal_exit": abnormal_lineage.identity[
                    "workspace_lineage_id"
                ],
                "abnormal_exit_fallback": abnormal_exit["fresh_fallback"][
                    "new_workspace_lineage_id"
                ],
            },
            "a9_s": {
                "baseline_full_passed": True,
                "baseline_full": baseline_evidence,
                "warmup_passed": len(warmup) == 11,
                "measured_passed": len(measured) == 110,
                "operations": operations,
                "oracle_catalog": catalog.report(),
            },
            "a9_m": {
                "checkpoints": checkpoints,
                "process_tree_sampler": sampler_report,
                "final_idle_process_tree": idle_sample,
                "decision": resource_decision,
            },
            "a9_l": lifecycle,
            "shutdown": shutdown_reports,
            "input_revalidation": input_revalidation,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "limitations": [
                "A10 is not run",
                "resource delta observation remains unavailable",
                "A9 evidence uses only the tiny plain-Java fixture",
            ],
        }
        reports = args.cache_root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        report_path = reports / f"{attempt_id}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"ok": True, "status": report["status"], "attempt_id": attempt_id, "report_path": str(report_path), "a9_m_status": resource_decision["status"]}))
        if not args.keep_attempt:
            shutil.rmtree(attempt)
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "message": str(exc), "attempt_path": str(attempt)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
