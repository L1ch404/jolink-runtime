"""Shared-workspace incremental cases for the real Maven/JDT experiment.

The mutation plan is private input.  Public results contain only case labels,
counts, timings, and fingerprints; source paths and source text never leave the
attempt directory.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

import run_bootstrap_smoke as common


PLAN_SCHEMA = "jolink.jdt-incremental-suite-plan.v1"
CASE_CATEGORIES = frozenset(
    {
        "noop",
        "method_body",
        "compile_time_constant",
        "schema_change",
        "lombok_field",
        "apt_sensitive",
        "source_lifecycle",
        "compile_error_recovery",
    }
)
OPERATIONS = frozenset({"noop", "replace", "add", "delete"})
ORACLE_MODES = frozenset({"none", "forward", "recovery"})
_CASE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class IncrementalSuiteError(RuntimeError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(encoded)


@dataclasses.dataclass(frozen=True)
class IncrementalCase:
    case_id: str
    category: str
    operation: str
    source: str | None
    before: str | None
    after: str | None
    content: str | None
    expect_compile_ok: bool
    oracle: str


@dataclasses.dataclass(frozen=True)
class SuitePlan:
    cases: tuple[IncrementalCase, ...]
    fingerprint: str


@dataclasses.dataclass(frozen=True)
class OutputState:
    classes: dict[str, str]
    resources: dict[str, str]

    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                "classes": sorted(self.classes.items()),
                "resources": sorted(self.resources.items()),
            }
        )


@dataclasses.dataclass
class AppliedMutation:
    path: Path | None
    original: bytes | None
    created_directories: tuple[Path, ...] = ()

    def restore(self) -> None:
        if self.path is None:
            return
        if self.original is None:
            self.path.unlink(missing_ok=True)
            for directory in self.created_directories:
                try:
                    directory.rmdir()
                except OSError:
                    break
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(self.original)


OracleRunner = Callable[[str, Path], tuple[dict[str, Any], OutputState]]


def _require_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise IncrementalSuiteError(f"{field} must be a non-empty string.")
    return value


def _relative_java_source(value: object) -> str:
    raw = _require_text(value, field="source")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or path.suffix != ".java"
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise IncrementalSuiteError(
            "source must be a normalized relative Java path."
        )
    return path.as_posix()


def load_plan(path: Path) -> SuitePlan:
    payload = json.loads(path.expanduser().resolve(strict=True).read_text("utf-8"))
    if payload.get("schema") != PLAN_SCHEMA:
        raise IncrementalSuiteError("Unexpected incremental suite plan schema.")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases or len(raw_cases) > 16:
        raise IncrementalSuiteError("The suite plan must contain 1 to 16 cases.")

    cases: list[IncrementalCase] = []
    seen_ids: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise IncrementalSuiteError("Each suite case must be an object.")
        case_id = _require_text(raw.get("id"), field="id")
        if not _CASE_ID.fullmatch(case_id) or case_id in seen_ids:
            raise IncrementalSuiteError("Case ids must be unique safe labels.")
        seen_ids.add(case_id)
        category = _require_text(raw.get("category"), field="category")
        operation = _require_text(raw.get("operation"), field="operation")
        oracle = str(raw.get("oracle", "none"))
        if category not in CASE_CATEGORIES:
            raise IncrementalSuiteError("Unsupported incremental case category.")
        if operation not in OPERATIONS:
            raise IncrementalSuiteError("Unsupported incremental mutation operation.")
        if oracle not in ORACLE_MODES:
            raise IncrementalSuiteError("Unsupported incremental oracle mode.")

        source: str | None = None
        before: str | None = None
        after: str | None = None
        content: str | None = None
        if operation != "noop":
            source = _relative_java_source(raw.get("source"))
        if operation == "replace":
            before = _require_text(raw.get("before"), field="before")
            after = _require_text(raw.get("after"), field="after")
            if before == after:
                raise IncrementalSuiteError("Replacement must change source bytes.")
        elif operation == "add":
            content = _require_text(raw.get("content"), field="content")
        elif operation == "noop" and category != "noop":
            raise IncrementalSuiteError("Only the noop category may use noop.")

        expected = raw.get(
            "expect_compile_ok", category != "compile_error_recovery"
        )
        if not isinstance(expected, bool):
            raise IncrementalSuiteError("expect_compile_ok must be boolean.")
        if oracle == "forward" and not expected:
            raise IncrementalSuiteError(
                "A failing mutation cannot request a forward clean-full oracle."
            )
        cases.append(
            IncrementalCase(
                case_id=case_id,
                category=category,
                operation=operation,
                source=source,
                before=before,
                after=after,
                content=content,
                expect_compile_ok=expected,
                oracle=oracle,
            )
        )
    return SuitePlan(cases=tuple(cases), fingerprint=_fingerprint(payload))


def private_values(plan: SuitePlan) -> tuple[str, ...]:
    """Return plan values that must never appear in the shareable report."""

    values: list[str] = []
    for case in plan.cases:
        for value in (case.source, case.before, case.after, case.content):
            # Very short Java tokens commonly appear in JSON field names and
            # would make the redaction assertion noisy.  Reports are built
            # from a fixed projection that never includes any of these fields;
            # longer values add an independent leak check.
            if isinstance(value, str) and len(value) >= 8:
                values.append(value)
    return tuple(values)


def output_state(output: Path) -> OutputState:
    classes: dict[str, str] = {}
    resources: dict[str, str] = {}
    if output.is_dir():
        for child in sorted(item for item in output.rglob("*") if item.is_file()):
            relative = child.relative_to(output).as_posix()
            target = classes if child.suffix == ".class" else resources
            target[relative] = _sha256_file(child)
    return OutputState(classes=classes, resources=resources)


def _delta(before: dict[str, str], after: dict[str, str]) -> dict[str, object]:
    changed = sorted(
        path for path, digest in after.items() if before.get(path) != digest
    )
    deleted = sorted(path for path in before if path not in after)
    return {
        "changed_count": len(changed),
        "deleted_count": len(deleted),
        "identity_sha256": _fingerprint(
            {"changed": changed, "deleted": deleted}
        ),
    }


def output_delta(before: OutputState, after: OutputState) -> dict[str, object]:
    return {
        "classes": _delta(before.classes, after.classes),
        "resources": _delta(before.resources, after.resources),
    }


def effect_observed(summary: dict[str, object]) -> bool:
    delta = summary.get("output_delta")
    if not isinstance(delta, dict):
        return False
    for kind in ("classes", "resources"):
        values = delta.get(kind)
        if not isinstance(values, dict):
            continue
        if any(
            isinstance(values.get(field), int) and values[field] > 0
            for field in ("changed_count", "deleted_count")
        ):
            return True
    return False


def _safe_source_path(source_root: Path, relative: str) -> Path:
    root = source_root.resolve(strict=True)
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    resolved_parent = candidate.parent.resolve(strict=False)
    if resolved_parent != root and root not in resolved_parent.parents:
        raise IncrementalSuiteError("Mutation source escapes the private root.")
    current = candidate
    while current != root:
        if current.is_symlink():
            raise IncrementalSuiteError("Mutation source may not traverse links.")
        current = current.parent
    return candidate


def apply_case(
    source_root: Path, case: IncrementalCase, *, encoding: str
) -> AppliedMutation:
    if case.operation == "noop":
        return AppliedMutation(path=None, original=None)
    assert case.source is not None
    path = _safe_source_path(source_root, case.source)
    if case.operation == "add":
        if path.exists():
            raise IncrementalSuiteError("Added source already exists.")
        assert case.content is not None
        created_directories: list[Path] = []
        current = path.parent
        root = source_root.resolve(strict=True)
        while current != root and not current.exists():
            created_directories.append(current)
            current = current.parent
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(case.content.encode(encoding))
        return AppliedMutation(
            path=path,
            original=None,
            created_directories=tuple(created_directories),
        )
    if not path.is_file():
        raise IncrementalSuiteError("Mutation source is unavailable.")
    original = path.read_bytes()
    if case.operation == "delete":
        path.unlink()
        return AppliedMutation(path=path, original=original)
    assert case.before is not None and case.after is not None
    before = case.before.encode(encoding)
    if original.count(before) != 1:
        raise IncrementalSuiteError(
            "Replacement anchor must occur exactly once in private source."
        )
    path.write_bytes(original.replace(before, case.after.encode(encoding), 1))
    return AppliedMutation(path=path, original=original)


def _build_summary(
    frame: dict[str, Any], *, wall_duration_ms: float, before: OutputState,
    after: OutputState
) -> dict[str, object]:
    units = frame.get("compiled_source_units")
    if not isinstance(units, list):
        raise IncrementalSuiteError("Worker omitted compiled source units.")
    return {
        "compile_ok": frame.get("compile_ok"),
        "actual_build_kind": frame.get("actual_build_kind"),
        "build_duration_ms": frame.get("elapsed_ms"),
        "wall_duration_ms": wall_duration_ms,
        "compiled_source_count": len(units),
        "error_count": frame.get("error_count"),
        "warning_count": frame.get("warning_count"),
        "output_delta": output_delta(before, after),
    }


def _incremental_build(
    client: common.WorkerClient, before: OutputState, output: Path
) -> tuple[dict[str, Any], OutputState, dict[str, object]]:
    started = time.monotonic()
    frame = client.command("BUILD\tINCREMENTAL")
    wall = round((time.monotonic() - started) * 1000, 1)
    common.require_build_operation_contract(frame, operation_kind="INCREMENTAL")
    after = output_state(output)
    return frame, after, _build_summary(
        frame, wall_duration_ms=wall, before=before, after=after
    )


def _recover_baseline(
    client: common.WorkerClient,
    *,
    baseline: OutputState,
    output: Path,
    force: bool = False,
) -> tuple[bool, dict[str, object] | None]:
    current = output_state(output)
    if current == baseline and not force:
        return False, None
    clean = client.command("BUILD\tCLEAN")
    common.require_build_operation_contract(clean, operation_kind="CLEAN")
    started = time.monotonic()
    full = client.command("BUILD\tFULL")
    wall = round((time.monotonic() - started) * 1000, 1)
    common.require_build_operation_contract(full, operation_kind="FULL")
    recovered = output_state(output)
    summary = _build_summary(
        full,
        wall_duration_ms=wall,
        before=OutputState(classes={}, resources={}),
        after=recovered,
    )
    summary["baseline_restored"] = recovered == baseline
    if recovered != baseline:
        raise IncrementalSuiteError(
            "CLEAN + FULL did not restore the shared baseline output."
        )
    return True, summary


def run_suite(
    *,
    client: common.WorkerClient,
    plan: SuitePlan,
    source_root: Path,
    output: Path,
    source_encoding: str,
    oracle_runner: OracleRunner | None = None,
) -> dict[str, object]:
    baseline = output_state(output)
    if not baseline.classes:
        raise IncrementalSuiteError("Incremental suite baseline has no classes.")
    results: list[dict[str, object]] = []

    for case in plan.cases:
        if output_state(output) != baseline:
            raise IncrementalSuiteError("Shared workspace was not at baseline.")
        mutation = apply_case(source_root, case, encoding=source_encoding)
        before = baseline
        try:
            forward_frame, forward_state, forward = _incremental_build(
                client, before, output
            )
            expected_compile = (
                forward_frame.get("compile_ok") is case.expect_compile_ok
            )
            oracle: dict[str, object] | None = None
            oracle_equal = True
            if case.oracle == "forward":
                if oracle_runner is None:
                    raise IncrementalSuiteError("Clean-full oracle is unavailable.")
                oracle_summary, oracle_state = oracle_runner(
                    case.case_id, source_root
                )
                oracle_equal = (
                    oracle_summary.get("compile_ok") is True
                    and oracle_state == forward_state
                )
                oracle = {
                    **oracle_summary,
                    "output_equal": oracle_equal,
                }
        finally:
            mutation.restore()

        reverse_frame: dict[str, Any] | None = None
        reverse: dict[str, object] | None = None
        if case.operation != "noop":
            current = output_state(output)
            reverse_frame, reverse_state, reverse = _incremental_build(
                client, current, output
            )
            reverse["baseline_restored"] = reverse_state == baseline
        restored_by_incremental = output_state(output) == baseline
        fallback_used, fallback = _recover_baseline(
            client,
            baseline=baseline,
            output=output,
            force=(
                reverse_frame is not None
                and reverse_frame.get("compile_ok") is not True
            ),
        )

        recovery_oracle: dict[str, object] | None = None
        recovery_oracle_equal = True
        if case.oracle == "recovery":
            if oracle_runner is None:
                raise IncrementalSuiteError("Clean-full oracle is unavailable.")
            oracle_summary, oracle_state = oracle_runner(
                f"{case.case_id}-recovery", source_root
            )
            recovery_oracle_equal = (
                oracle_summary.get("compile_ok") is True
                and oracle_state == baseline
            )
            recovery_oracle = {
                **oracle_summary,
                "output_equal": recovery_oracle_equal,
            }

        reverse_ok = (
            True
            if case.operation == "noop"
            else reverse_frame is not None
            and reverse_frame.get("compile_ok") is True
        )
        actual_kind_ok = forward_frame.get("actual_build_kind") == "INCREMENTAL"
        if case.operation == "noop":
            actual_kind_ok = (
                forward_frame.get("actual_build_kind") is None
                and forward.get("compiled_source_count") == 0
                and forward_state == baseline
            )
        forward_effect_observed = effect_observed(forward)
        effect_gate_ok = (
            not case.expect_compile_ok
            or case.operation == "noop"
            or forward_effect_observed
        )
        forward_protocol_ok = expected_compile and actual_kind_ok
        if not forward_protocol_ok or not recovery_oracle_equal:
            capability_status = "failed"
        elif case.oracle == "forward" and not oracle_equal:
            capability_status = (
                "clean_full_fallback_candidate"
                if oracle is not None and oracle.get("compile_ok") is True
                else "failed"
            )
        elif not effect_gate_ok:
            capability_status = "failed"
        else:
            capability_status = "native_incremental_passed"

        if case.operation == "noop":
            baseline_recovery_status = "not_required"
        elif reverse_ok and restored_by_incremental:
            baseline_recovery_status = "restored_by_incremental"
        elif fallback_used:
            baseline_recovery_status = "restored_by_clean_full"
        else:
            baseline_recovery_status = "failed"
        results.append(
            {
                "case_id": case.case_id,
                "category": case.category,
                "operation": case.operation,
                "expected_compile_ok": case.expect_compile_ok,
                "capability_status": capability_status,
                "forward": forward,
                "forward_effect_observed": forward_effect_observed,
                "reverse": reverse,
                "baseline_recovery_status": baseline_recovery_status,
                "baseline_recovery_fallback_used": fallback_used,
                "baseline_recovery_fallback": fallback,
                "oracle": oracle,
                "recovery_oracle": recovery_oracle,
            }
        )

    capability_counts = {
        status: sum(
            1 for item in results if item["capability_status"] == status
        )
        for status in (
            "native_incremental_passed",
            "clean_full_fallback_candidate",
            "failed",
        )
    }
    baseline_recovery_counts = {
        status: sum(
            1 for item in results if item["baseline_recovery_status"] == status
        )
        for status in (
            "not_required",
            "restored_by_incremental",
            "restored_by_clean_full",
            "failed",
        )
    }
    all_native = (
        capability_counts["clean_full_fallback_candidate"] == 0
        and capability_counts["failed"] == 0
        and baseline_recovery_counts["restored_by_clean_full"] == 0
        and baseline_recovery_counts["failed"] == 0
    )
    return {
        "schema": "jolink.jdt-incremental-suite-report.v1",
        "suite_completed": True,
        "all_native_incremental_passed": all_native,
        "shared_bootstrap": True,
        "shared_full_build": True,
        "plan_fingerprint": plan.fingerprint,
        "baseline_class_count": len(baseline.classes),
        "baseline_resource_count": len(baseline.resources),
        "baseline_output_fingerprint": baseline.fingerprint,
        "case_count": len(results),
        "capability_counts": capability_counts,
        "baseline_recovery_counts": baseline_recovery_counts,
        "cases": results,
    }
