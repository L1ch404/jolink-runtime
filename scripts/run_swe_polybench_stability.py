#!/usr/bin/env python3
"""Run resumable joLink stability checks on SWE-PolyBench Verified Java."""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence


PILOT = (
    "google__gson-2337",
    "apache__rocketmq-7563",
    "apache__dubbo-3317",
    "apolloconfig__apollo-4568",
    "google__guava-3971",
    "trinodb__trino-2707",
)
GOLD_FIELDS = {
    "patch",
    "modified_nodes",
    "problem_statement",
    "hints_text",
}
EXPECTED_BOUNDARY_MARKERS = (
    "UNSUPPORTED",
    "UNMODELED",
    "UNAVAILABLE",
    "UNVERIFIED",
    "AMBIGUOUS",
    "REQUIRES",
    "NOT_FOUND",
    "PACKAGING",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(
    command: Sequence[str],
    *,
    timeout: float,
    stdout=None,
    stderr=None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        tuple(command),
        stdout=stdout,
        stderr=stderr,
        text=stdout is subprocess.PIPE or stderr is subprocess.PIPE,
        timeout=timeout,
        check=check,
    )


def _docker(*arguments: str, timeout: float = 300, **kwargs):
    return _run(("docker", *arguments), timeout=timeout, **kwargs)


def _rows(dataset: Path) -> list[dict[str, str]]:
    csv.field_size_limit(sys.maxsize)
    with dataset.open(newline="", encoding="utf-8") as stream:
        values = [
            dict(row)
            for row in csv.DictReader(stream)
            if str(row.get("language", "")).casefold() == "java"
        ]
    if len(values) != 69:
        raise RuntimeError(f"expected 69 Java instances, found {len(values)}")
    identifiers = [row["instance_id"] for row in values]
    if len(set(identifiers)) != len(identifiers):
        raise RuntimeError("duplicate Java instance_id")
    return values


def _tests(raw: str) -> list[str]:
    try:
        values = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return []
    return [str(value) for value in values] if isinstance(values, list) else []


def _selector(row: dict[str, str]) -> tuple[str, str]:
    p2p = _tests(row.get("P2P", "[]"))
    f2p = _tests(row.get("F2P", "[]"))
    selected = (p2p or f2p)
    if not selected:
        raise RuntimeError("NO_P2P_OR_F2P_TEST")
    raw = selected[0]
    class_name, separator, method_name = raw.rpartition(".")
    if not separator or not class_name or not method_name:
        raise RuntimeError(f"INVALID_TEST_ID:{raw}")
    return f"{class_name}#{method_name}", "P2P" if p2p else "F2P"


def _java_home(test_command: str) -> str | None:
    match = re.search(
        r"(?:^|[;&]\s*)export\s+JAVA_HOME=(?:['\"])?([^\s;&'\"]+)",
        test_command,
    )
    return match.group(1).rstrip("/") if match else None


def _source_hint(test_patch: str, selector: str) -> tuple[str | None, int]:
    class_name = selector.partition("#")[0].split("$", 1)[0]
    suffix = "/".join(class_name.split(".")) + ".java"
    candidates = []
    for line in test_patch.splitlines():
        if not line.startswith("+++ b/"):
            continue
        path = line[6:].strip()
        if path.endswith(suffix) and path not in candidates:
            candidates.append(path)
    if not candidates:
        return None, 0
    return min(candidates, key=lambda value: (value.count("/"), value)), len(
        candidates
    )


def _build_environment_prefix(test_command: str) -> str:
    java_home = _java_home(test_command)
    if not java_home:
        return ""
    values = [
        f"export JAVA_HOME={java_home}",
        f"export PATH={java_home}/bin:$PATH",
    ]
    values.append(
        "export MAVEN_OPTS=\"${MAVEN_OPTS:-} "
        "-Djdk.tls.client.protocols=TLSv1.2 "
        "-Dhttps.protocols=TLSv1.2\""
    )
    return "; ".join(values) + "; "


def _safe_name(instance_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", instance_id)[:100]


def _image(instance_id: str) -> str:
    return (
        "ghcr.io/timesler/swe-polybench.eval.x86_64."
        f"{instance_id.lower()}:v1.1"
    )


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _apply_test_patch(
    container: str,
    patch: str,
    *,
    scratch: Path,
) -> tuple[bool, str]:
    source = scratch / "test.patch"
    source.write_text(patch, encoding="utf-8")
    copied = _docker("cp", str(source), f"{container}:/tmp/jolink-test.patch")
    if copied.returncode != 0:
        return False, "docker_cp_failed"
    applied = _docker(
        "exec",
        container,
        "bash",
        "-lc",
        "cd /testbed && git apply --ignore-whitespace --reject /tmp/jolink-test.patch",
        timeout=120,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if applied.returncode == 0:
        return True, str(applied.stdout or "")[-4000:]
    fallback = _docker(
        "exec",
        container,
        "bash",
        "-lc",
        "cd /testbed && patch --batch --fuzz=5 -p1 -f -i /tmp/jolink-test.patch",
        timeout=120,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return fallback.returncode == 0, str(fallback.stdout or "")[-4000:]


def _pull(image: str, attempts: int, log: Path) -> bool:
    local = _docker(
        "image",
        "inspect",
        image,
        timeout=60,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if local.returncode == 0:
        return True
    for attempt in range(1, attempts + 1):
        with log.open("a", encoding="utf-8") as stream:
            stream.write(f"pull attempt {attempt}: {image}\n")
            completed = _docker(
                "pull",
                "--platform",
                "linux/amd64",
                image,
                timeout=1800,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
        if completed.returncode == 0:
            return True
        time.sleep(min(30, attempt * 5))
    return False


def _classify(report: dict[str, Any]) -> str:
    if report.get("ok") is True and report.get("cleanup_ok") is True:
        return "PASS"
    code = str(report.get("error_code", ""))
    if report.get("stage") in {"image", "test_patch", "official_baseline"}:
        return "DATASET_OR_ENVIRONMENT_FAILURE"
    if report.get("stage") in {"host_runner", "driver"}:
        return "HARNESS_FAILURE"
    if any(marker in code for marker in EXPECTED_BOUNDARY_MARKERS):
        return "UNSUPPORTED_EXPECTED"
    return "PRODUCT_BUG"


def _instance(
    row: dict[str, str],
    *,
    output: Path,
    assets: Path,
    uv_cache: Path,
    wheel: Path,
    driver: Path,
    official_timeout: float,
    fast_timeout: float,
    pull_attempts: int,
    delete_image: bool,
) -> dict[str, Any]:
    instance_id = row["instance_id"]
    started = time.monotonic()
    instance_dir = output / "instances" / _safe_name(instance_id)
    instance_dir.mkdir(parents=True, exist_ok=True)
    image = _image(instance_id)
    container = f"jolink-spb-{_safe_name(instance_id).lower()}"
    report: dict[str, Any] = {
        "instance_id": instance_id,
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "image": image,
        "classification": "HARNESS_FAILURE",
        "timing_seconds": {},
    }
    try:
        selector, selector_source = _selector(row)
        report["selected_test"] = selector
        report["selector_source"] = selector_source
    except Exception as error:
        report.update(
            stage="host_runner",
            error_code=type(error).__name__,
            error=str(error),
        )
        return report

    pull_started = time.monotonic()
    if not _pull(image, pull_attempts, instance_dir / "pull.log"):
        report.update(stage="image", error_code="IMAGE_PULL_FAILED")
        report["timing_seconds"]["pull"] = time.monotonic() - pull_started
        report["classification"] = _classify(report)
        return report
    report["timing_seconds"]["pull"] = time.monotonic() - pull_started
    _docker("rm", "-f", container, timeout=60, stdout=subprocess.DEVNULL)
    created = _docker(
        "create",
        "--platform",
        "linux/amd64",
        "--name",
        container,
        "--workdir",
        "/testbed",
        "--volume",
        f"{assets}:/jolink/assets:ro",
        "--volume",
        f"{uv_cache}:/root/.cache/uv",
        "--env",
        "UV_CACHE_DIR=/root/.cache/uv/cache",
        "--env",
        "UV_PYTHON_INSTALL_DIR=/root/.cache/uv/python",
        image,
        "tail",
        "-f",
        "/dev/null",
        timeout=120,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if created.returncode != 0:
        report.update(
            stage="image",
            error_code="CONTAINER_CREATE_FAILED",
            error=str(created.stdout or "")[-4000:],
        )
        report["classification"] = _classify(report)
        return report
    try:
        _docker("start", container, timeout=60, check=True)
        with tempfile.TemporaryDirectory(prefix="jolink-spb-patch-") as raw:
            patch_ok, patch_log = _apply_test_patch(
                container,
                row["test_patch"],
                scratch=Path(raw),
            )
        (instance_dir / "test-patch.log").write_text(
            patch_log, encoding="utf-8"
        )
        if not patch_ok:
            report.update(stage="test_patch", error_code="TEST_PATCH_FAILED")
            report["classification"] = _classify(report)
            return report

        baseline_started = time.monotonic()
        build_environment = _build_environment_prefix(row["test_command"])
        with (instance_dir / "official-baseline.log").open(
            "w", encoding="utf-8"
        ) as stream:
            try:
                baseline = _docker(
                    "exec",
                    container,
                    "bash",
                    "-lc",
                    f"cd /testbed && {build_environment}{row['test_command']}",
                    timeout=official_timeout,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                )
                report["official_command_return_code"] = baseline.returncode
            except subprocess.TimeoutExpired:
                report.update(
                    stage="official_baseline",
                    error_code="OFFICIAL_BASELINE_TIMEOUT",
                )
                report["classification"] = _classify(report)
                return report
        report["timing_seconds"]["official_baseline"] = (
            time.monotonic() - baseline_started
        )
        baseline_text = (instance_dir / "official-baseline.log").read_text(
            encoding="utf-8", errors="replace"
        )
        report_resolution_failure = (
            "PluginResolutionException" in baseline_text
            or (
                "maven-surefire-report-plugin" in baseline_text
                and "could not be resolved" in baseline_text.casefold()
            )
        )
        compile_failure = (
            "COMPILATION ERROR" in baseline_text
            or "Compilation failure" in baseline_text
            or "CompilationFailureException" in baseline_text
        )
        report["official_failure_kind"] = (
            "compile_failed"
            if compile_failure
            else "report_plugin_resolution"
            if report_resolution_failure
            else None
        )
        reports = _docker(
            "exec",
            container,
            "bash",
            "-lc",
            "find /testbed -path '*/target/surefire-reports/TEST-*.xml' "
            "-print -quit | grep -q .",
            timeout=60,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        report["official_report_fallback_used"] = False
        if (
            reports.returncode != 0
            and report_resolution_failure
            and "surefire-report:report" in row["test_command"]
        ):
            fallback_command = row["test_command"].replace(
                "surefire-report:report", ""
            )
            fallback_started = time.monotonic()
            with (instance_dir / "official-baseline-fallback.log").open(
                "w", encoding="utf-8"
            ) as stream:
                try:
                    fallback = _docker(
                        "exec",
                        container,
                        "bash",
                        "-lc",
                        f"cd /testbed && {build_environment}{fallback_command}",
                        timeout=official_timeout,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                    )
                    report["official_fallback_return_code"] = fallback.returncode
                    report["official_report_fallback_used"] = True
                except subprocess.TimeoutExpired:
                    report["official_fallback_return_code"] = None
            report["timing_seconds"]["official_fallback"] = (
                time.monotonic() - fallback_started
            )

        source_hint, source_hint_count = _source_hint(
            row["test_patch"], report["selected_test"]
        )
        sanitized = {
            "instance_id": instance_id,
            "project_path": "/testbed",
            "selector": report["selected_test"],
            "fast_test_timeout": fast_timeout,
            "java_home": _java_home(row["test_command"]),
            "official_failure_kind": report["official_failure_kind"],
            "source_hint": source_hint,
            "source_hint_candidate_count": source_hint_count,
        }
        input_path = instance_dir / "driver-input.json"
        _atomic_json(input_path, sanitized)
        _docker("cp", str(input_path), f"{container}:/tmp/jolink-input.json")
        driver_started = time.monotonic()
        with (instance_dir / "jolink-driver.log").open(
            "w", encoding="utf-8"
        ) as stream:
            try:
                completed = _docker(
                    "exec",
                    container,
                    "/jolink/assets/uv",
                    "run",
                    "--python",
                    "3.11",
                    "--no-project",
                    "--with",
                    f"/jolink/assets/{wheel.name}",
                    "python",
                    f"/jolink/assets/{driver.name}",
                    "--input",
                    "/tmp/jolink-input.json",
                    timeout=fast_timeout * 3 + 300,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                )
            except subprocess.TimeoutExpired:
                report.update(stage="driver", error_code="DRIVER_TIMEOUT")
                report["classification"] = _classify(report)
                return report
        report["timing_seconds"]["jolink_driver"] = time.monotonic() - driver_started
        lines = (instance_dir / "jolink-driver.log").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        payload = next(
            (
                json.loads(line)
                for line in reversed(lines)
                if line.startswith("{") and line.endswith("}")
            ),
            None,
        )
        if not isinstance(payload, dict):
            report.update(
                stage="driver",
                error_code="DRIVER_RESULT_UNAVAILABLE",
                driver_return_code=completed.returncode,
            )
        else:
            report["jolink"] = payload
            report.update(
                ok=payload.get("ok"),
                stage=payload.get("stage"),
                error_code=payload.get("error_code"),
                error=payload.get("error"),
                cleanup_ok=payload.get("cleanup_ok"),
            )
        report["classification"] = _classify(report)
        return report
    except Exception as error:
        report.update(
            stage="host_runner",
            error_code=type(error).__name__,
            error=str(error),
        )
        report["classification"] = _classify(report)
        return report
    finally:
        cleanup = _docker(
            "rm",
            "-f",
            container,
            timeout=120,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        report["container_cleanup_ok"] = cleanup.returncode == 0
        report["timing_seconds"]["total"] = time.monotonic() - started
        if delete_image:
            _docker(
                "image",
                "rm",
                image,
                timeout=300,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def _percentile(values: list[float], percentage: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentage
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    classifications = Counter(item["classification"] for item in results)
    reasons = Counter(
        str(item.get("error_code"))
        for item in results
        if item.get("error_code")
    )
    durations = [
        float(item.get("timing_seconds", {}).get("total", 0))
        for item in results
        if item.get("timing_seconds", {}).get("total") is not None
    ]
    fast_test_ready = sum(
        1
        for item in results
        if item.get("jolink", {}).get("stages", {}).get("baseline", {}).get("ok")
        is True
    )
    incremental_roundtrip = sum(
        1
        for item in results
        if item.get("jolink", {}).get("stability_ok") is True
    )
    cleanup_passed = sum(
        1
        for item in results
        if item.get("jolink", {}).get("cleanup_ok") is True
    )
    return {
        "completed": len(results),
        "classification_counts": dict(classifications),
        "reason_counts": dict(reasons),
        "coverage": (
            classifications.get("PASS", 0) / len(results) if results else 0
        ),
        "fast_test_ready": fast_test_ready,
        "incremental_roundtrip_passed": incremental_roundtrip,
        "cleanup_passed": cleanup_passed,
        "timing_seconds": {
            "median": statistics.median(durations) if durations else None,
            "p95": _percentile(durations, 0.95),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--uv-cache", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--driver", type=Path, required=True)
    parser.add_argument("--instance", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--retry-existing", action="store_true")
    parser.add_argument("--delete-images", action="store_true")
    parser.add_argument("--official-timeout", type=float, default=1800)
    parser.add_argument("--fast-timeout", type=float, default=600)
    parser.add_argument("--pull-attempts", type=int, default=3)
    parser.add_argument("--jolink-commit", required=True)
    parser.add_argument("--dataset-revision", required=True)
    parser.add_argument("--harness-revision", required=True)
    parser.add_argument("--runner-commit", required=True)
    args = parser.parse_args()
    dataset = args.dataset.resolve(strict=True)
    output = args.output.resolve(strict=False)
    assets = args.assets.resolve(strict=True)
    uv_cache = args.uv_cache.resolve(strict=True)
    wheel = args.wheel.resolve(strict=True)
    driver = args.driver.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=True)
    (output / "instances").mkdir(exist_ok=True)
    shutil.copy2(wheel, assets / wheel.name)
    shutil.copy2(driver, assets / driver.name)
    rows = _rows(dataset)
    selected = set(args.instance)
    if selected:
        rows = [row for row in rows if row["instance_id"] in selected]
        missing = selected - {row["instance_id"] for row in rows}
        if missing:
            raise SystemExit(f"unknown instances: {sorted(missing)}")
    else:
        order = {name: index for index, name in enumerate(PILOT)}
        rows.sort(key=lambda row: (order.get(row["instance_id"], 999), row["instance_id"]))
    if args.limit is not None:
        rows = rows[: max(0, args.limit)]
    manifest = {
        "schema": "jolink.swe-polybench-stability.v1",
        "created_at": time.time(),
        "dataset_revision": args.dataset_revision,
        "dataset_sha256": _sha256(dataset),
        "harness_revision": args.harness_revision,
        "runner_commit": args.runner_commit,
        "jolink_commit": args.jolink_commit,
        "wheel": wheel.name,
        "wheel_sha256": _sha256(wheel),
        "driver_sha256": _sha256(driver),
        "instance_count": len(rows),
        "instance_ids": [row["instance_id"] for row in rows],
        "gold_fields_excluded": sorted(GOLD_FIELDS),
        "image_tag": "v1.1",
        "max_workers": 1,
    }
    _atomic_json(output / "run-manifest.json", manifest)
    results: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        destination = output / "instances" / _safe_name(row["instance_id"]) / "result.json"
        if destination.is_file() and not args.retry_existing:
            result = json.loads(destination.read_text(encoding="utf-8"))
            normalized = _classify(result)
            if result.get("classification") != normalized:
                result["classification"] = normalized
                _atomic_json(destination, result)
            results.append(result)
            print(f"[{index}/{len(rows)}] resume {row['instance_id']} {result['classification']}")
            continue
        print(f"[{index}/{len(rows)}] start {row['instance_id']}", flush=True)
        sanitized = {key: value for key, value in row.items() if key not in GOLD_FIELDS}
        result = _instance(
            sanitized,
            output=output,
            assets=assets,
            uv_cache=uv_cache,
            wheel=wheel,
            driver=driver,
            official_timeout=args.official_timeout,
            fast_timeout=args.fast_timeout,
            pull_attempts=args.pull_attempts,
            delete_image=args.delete_images,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(destination, result)
        results.append(result)
        _atomic_json(output / "summary.json", _summarize(results))
        print(
            f"[{index}/{len(rows)}] finish {row['instance_id']} "
            f"{result['classification']} {result.get('error_code') or ''}",
            flush=True,
        )
    summary = _summarize(results)
    summary["manifest"] = manifest
    _atomic_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
