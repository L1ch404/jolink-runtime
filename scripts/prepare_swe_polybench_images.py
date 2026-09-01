#!/usr/bin/env python3
"""Materialize deterministic local SWE-PolyBench images for joLink A/B."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from run_swe_polybench_stability import (
    _atomic_json,
    _build_environment_prefix,
    _build_system,
    _docker,
    _image,
    _pull,
    _rows,
    _safe_name,
    _sha256,
)


_HELP_GOAL = "org.apache.maven.plugins:maven-help-plugin:3.2.0:effective-pom"


def _output(command: tuple[str, ...], *, timeout: float = 120) -> str:
    completed = _docker(
        *command,
        timeout=timeout,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        raise RuntimeError(str(completed.stdout or "")[-4000:])
    return str(completed.stdout or "").strip()


def _prepare_one(
    row: dict[str, str],
    *,
    output: Path,
    pull_attempts: int,
    resolve_attempts: int,
    force: bool,
    uv_binary: Path,
    uv_cache: Path,
    jdt_cache: Path | None,
    wheel: Path,
    seed_image: str | None,
) -> dict[str, object]:
    instance_id = row["instance_id"]
    base_image = _image(instance_id)
    wheel_sha256 = _sha256(wheel)
    prepared_image = (
        "jolink-swe-polybench-prepared:"
        f"{_safe_name(instance_id).lower()}-{wheel_sha256[:12]}"
    )
    instance_dir = output / "instances" / _safe_name(instance_id)
    instance_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {
        "instance_id": instance_id,
        "base_image": base_image,
        "prepared_image": prepared_image,
        "build_system": _build_system(row["test_command"]),
        "wheel_sha256": wheel_sha256,
        "uv_sha256": _sha256(uv_binary),
        "ok": False,
    }
    if result["build_system"] != "maven":
        result.update(
            error_code="PREPARE_BUILD_SYSTEM_UNSUPPORTED",
            error="Prepared image v2 currently materializes Maven Probe dependencies.",
        )
        return result
    if not _pull(base_image, pull_attempts, instance_dir / "pull.log"):
        result["error_code"] = "IMAGE_PULL_FAILED"
        return result
    base_image_id = _output(("image", "inspect", "--format", "{{.Id}}", base_image))
    result["base_image_id"] = base_image_id
    source_image = base_image
    if seed_image:
        seed = _docker(
            "image",
            "inspect",
            seed_image,
            timeout=60,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        seed_base = (
            _output(
                (
                    "image",
                    "inspect",
                    "--format",
                    '{{ index .Config.Labels "io.jolink.swe-polybench.base-image-id" }}',
                    seed_image,
                )
            )
            if seed.returncode == 0
            else ""
        )
        if seed.returncode == 0 and seed_base == base_image_id:
            source_image = seed_image
            result["seed_image"] = seed_image
    if not force:
        existing = _docker(
            "image",
            "inspect",
            prepared_image,
            timeout=60,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if existing.returncode == 0:
            prepared_base = _output(
                (
                    "image",
                    "inspect",
                    "--format",
                    '{{ index .Config.Labels "io.jolink.swe-polybench.base-image-id" }}',
                    prepared_image,
                )
            )
            if prepared_base == base_image_id:
                result.update(
                    ok=True,
                    reused=True,
                    prepared_image_id=_output(
                        ("image", "inspect", "--format", "{{.Id}}", prepared_image)
                    ),
                )
                return result
            _docker(
                "image",
                "rm",
                prepared_image,
                timeout=300,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    container = f"jolink-spb-prepare-{_safe_name(instance_id).lower()}"
    _docker("rm", "-f", container, timeout=60, stdout=subprocess.DEVNULL)
    volumes = [
        "--volume",
        f"{uv_binary}:/jolink/uv:ro",
        "--volume",
        f"{wheel}:/jolink/{wheel.name}:ro",
        "--volume",
        f"{uv_cache}:/root/.cache/uv",
    ]
    if jdt_cache is not None:
        volumes.extend(
            (
                "--volume",
                f"{jdt_cache}:/jolink/jdt-worker-cache:ro",
            )
        )
    created = _docker(
        "create",
        "--platform",
        "linux/amd64",
        "--name",
        container,
        "--workdir",
        "/testbed",
        *volumes,
        "--env",
        "UV_CACHE_DIR=/root/.cache/uv/cache",
        "--env",
        "UV_PYTHON_INSTALL_DIR=/root/.cache/uv/python",
        source_image,
        "tail",
        "-f",
        "/dev/null",
        timeout=120,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if created.returncode != 0:
        result.update(
            error_code="CONTAINER_CREATE_FAILED",
            error=str(created.stdout or "")[-4000:],
        )
        return result
    try:
        _docker("start", container, timeout=60, check=True)
        if jdt_cache is not None:
            seeded = _docker(
                "exec",
                container,
                "bash",
                "-lc",
                "mkdir -p /root/.cache/jolink-runtime/jdt-worker && "
                "cp -a /jolink/jdt-worker-cache/. "
                "/root/.cache/jolink-runtime/jdt-worker/",
                timeout=120,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            if seeded.returncode != 0:
                result.update(
                    error_code="JDT_CANDIDATE_CACHE_SEED_FAILED",
                    error=str(seeded.stdout or "")[-4000:],
                )
                return result
        status_before = _output(
            (
                "exec",
                container,
                "bash",
                "-lc",
                "cd /testbed && git status --porcelain=v1 --untracked-files=all",
            )
        )
        environment = _build_environment_prefix(row["test_command"])
        online = (
            "cd /testbed && "
            f"{environment}mvn -B -DskipTests -DskipITs {_HELP_GOAL} "
            "-Doutput=/tmp/jolink-prewarm-effective-pom.xml"
        )
        log = instance_dir / "maven-probe-prewarm.log"
        resolved = False
        for attempt in range(1, resolve_attempts + 1):
            with log.open("a", encoding="utf-8") as stream:
                stream.write(f"\n=== online attempt {attempt} ===\n")
                stream.flush()
                completed = _docker(
                    "exec",
                    container,
                    "bash",
                    "-lc",
                    online,
                    timeout=1800,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                )
            if completed.returncode == 0:
                resolved = True
                break
            time.sleep(min(30, attempt * 5))
        if not resolved:
            result["error_code"] = "MAVEN_PROBE_PREWARM_FAILED"
            return result
        offline = online.replace("mvn -B ", "mvn -B -o ", 1)
        with log.open("a", encoding="utf-8") as stream:
            stream.write("\n=== offline verification ===\n")
            stream.flush()
            verified = _docker(
                "exec",
                container,
                "bash",
                "-lc",
                offline,
                timeout=1800,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
        if verified.returncode != 0:
            result["error_code"] = "MAVEN_PROBE_OFFLINE_VERIFICATION_FAILED"
            return result
        jdt_command = (
            "/jolink/uv run --python 3.11 --no-project "
            f"--with /jolink/{wheel.name} python -c \"from "
            "jolink_runtime.launch.jdt_compile_session import JdtCandidate; "
            "candidate=JdtCandidate.load_product(); "
            "print(candidate.candidate_id)\""
        )
        jdt_log = instance_dir / "jdt-candidate-prewarm.log"
        jdt_ready = False
        for attempt in range(1, resolve_attempts + 1):
            with jdt_log.open("a", encoding="utf-8") as stream:
                stream.write(f"\n=== online attempt {attempt} ===\n")
                stream.flush()
                completed = _docker(
                    "exec",
                    container,
                    "bash",
                    "-lc",
                    jdt_command,
                    timeout=1800,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                )
            if completed.returncode == 0:
                jdt_ready = True
                break
            time.sleep(min(30, attempt * 5))
        if not jdt_ready:
            result["error_code"] = "JDT_CANDIDATE_PREWARM_FAILED"
            return result
        disconnected = _docker(
            "network",
            "disconnect",
            "bridge",
            container,
            timeout=60,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if disconnected.returncode != 0:
            result.update(
                error_code="PREWARM_NETWORK_ISOLATION_FAILED",
                error=str(disconnected.stdout or "")[-4000:],
            )
            return result
        with jdt_log.open("a", encoding="utf-8") as stream:
            stream.write("\n=== offline verification ===\n")
            stream.flush()
            jdt_offline_command = jdt_command.replace(
                "/jolink/uv run ",
                "/jolink/uv run --offline ",
                1,
            )
            jdt_verified = _docker(
                "exec",
                container,
                "bash",
                "-lc",
                jdt_offline_command,
                timeout=300,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
        if jdt_verified.returncode != 0:
            result["error_code"] = "JDT_CANDIDATE_OFFLINE_VERIFICATION_FAILED"
            return result
        _docker(
            "exec",
            container,
            "rm",
            "-f",
            "/tmp/jolink-prewarm-effective-pom.xml",
            timeout=60,
            check=True,
        )
        status_after = _output(
            (
                "exec",
                container,
                "bash",
                "-lc",
                "cd /testbed && git status --porcelain=v1 --untracked-files=all",
            )
        )
        if status_after != status_before:
            result.update(
                error_code="PREWARM_MODIFIED_PROJECT",
                error="Maven Probe prewarm changed the benchmark worktree.",
            )
            return result
        committed = _docker(
            "commit",
            "--change",
            f"LABEL io.jolink.swe-polybench.base-image-id={base_image_id}",
            container,
            prepared_image,
            timeout=1800,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if committed.returncode != 0:
            result.update(
                error_code="PREPARED_IMAGE_COMMIT_FAILED",
                error=str(committed.stdout or "")[-4000:],
            )
            return result
        result.update(
            ok=True,
            reused=False,
            prepared_image_id=_output(
                ("image", "inspect", "--format", "{{.Id}}", prepared_image)
            ),
            project_status_sha256=_sha256_bytes(status_before.encode("utf-8")),
        )
        return result
    finally:
        _docker(
            "rm",
            "-f",
            container,
            timeout=120,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _sha256_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--uv-cache", type=Path, required=True)
    parser.add_argument("--jdt-cache", type=Path)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--instance", action="append", default=[])
    parser.add_argument("--pull-attempts", type=int, default=5)
    parser.add_argument("--resolve-attempts", type=int, default=5)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--seed-images", type=Path, action="append", default=[])
    args = parser.parse_args()
    dataset = args.dataset.resolve(strict=True)
    output = args.output.resolve(strict=False)
    uv_binary = args.uv.resolve(strict=True)
    uv_cache = args.uv_cache.resolve(strict=True)
    jdt_cache = (
        args.jdt_cache.expanduser().resolve(strict=False)
        if args.jdt_cache is not None
        else None
    )
    if jdt_cache is not None:
        jdt_cache.mkdir(parents=True, exist_ok=True)
    wheel = args.wheel.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=True)
    rows = _rows(dataset)
    seed_entries: dict[str, dict[str, object]] = {}
    for source in args.seed_images:
        manifest = json.loads(
            source.expanduser().resolve(strict=True).read_text(encoding="utf-8")
        )
        if manifest.get("schema") != "jolink.swe-polybench-prepared-images.v2":
            raise SystemExit("unsupported seed image manifest")
        for instance_id, entry in dict(manifest.get("images", {})).items():
            seed_entries[str(instance_id)] = dict(entry)
    selected = set(args.instance)
    if selected:
        rows = [row for row in rows if row["instance_id"] in selected]
        missing = selected - {row["instance_id"] for row in rows}
        if missing:
            raise SystemExit(f"unknown instances: {sorted(missing)}")
    results = []
    for index, row in enumerate(rows, start=1):
        print(f"[{index}/{len(rows)}] prepare {row['instance_id']}", flush=True)
        result = _prepare_one(
            row,
            output=output,
            pull_attempts=max(1, args.pull_attempts),
            resolve_attempts=max(1, args.resolve_attempts),
            force=args.force,
            uv_binary=uv_binary,
            uv_cache=uv_cache,
            jdt_cache=jdt_cache,
            wheel=wheel,
            seed_image=(
                str(seed_entries[row["instance_id"]]["prepared_image"])
                if row["instance_id"] in seed_entries
                and seed_entries[row["instance_id"]].get("base_image")
                == _image(row["instance_id"])
                else None
            ),
        )
        results.append(result)
        _atomic_json(
            output / "instances" / _safe_name(row["instance_id"]) / "result.json",
            result,
        )
        print(
            f"[{index}/{len(rows)}] {row['instance_id']} "
            f"{'PASS' if result.get('ok') else result.get('error_code')}",
            flush=True,
        )
    manifest = {
        "schema": "jolink.swe-polybench-prepared-images.v2",
        "created_at": time.time(),
        "dataset_sha256": _sha256(dataset),
        "wheel_sha256": _sha256(wheel),
        "uv_sha256": _sha256(uv_binary),
        "images": {
            str(item["instance_id"]): item
            for item in results
            if item.get("ok") is True
        },
        "failures": [item for item in results if item.get("ok") is not True],
    }
    _atomic_json(output / "prepared-images.json", manifest)
    return 0 if not manifest["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
