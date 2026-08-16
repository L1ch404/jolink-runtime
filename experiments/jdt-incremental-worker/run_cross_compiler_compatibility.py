#!/usr/bin/env python3
"""Run the frozen javac-8 versus ECJ-3.25 source-portability probe."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import run_bootstrap_smoke as common

from jolink_runtime.launch.toolchain import JavaToolchainCandidate


class CompatibilityProbeError(RuntimeError):
    """The expected cross-compiler result was not observed."""


def _combined_output(completed: subprocess.CompletedProcess[str]) -> str:
    return f"{completed.stdout}\n{completed.stderr}".lower()


def require_expected_divergence(
    javac: subprocess.CompletedProcess[str],
    ecj: subprocess.CompletedProcess[str],
) -> dict[str, bool]:
    """Gate the known javac-accepts/ECJ-rejects compatibility family."""

    javac_output = _combined_output(javac)
    ecj_output = _combined_output(ecj)
    if javac.returncode != 0:
        raise CompatibilityProbeError("Target JDK 8 javac rejected the fixture.")
    if "unchecked" not in javac_output:
        raise CompatibilityProbeError(
            "Target JDK 8 javac did not report the expected unchecked warning."
        )
    if ecj.returncode == 0:
        raise CompatibilityProbeError("Locked ECJ unexpectedly accepted the fixture.")
    if "type mismatch" not in ecj_output:
        raise CompatibilityProbeError(
            "Locked ECJ did not report the expected type-mismatch family."
        )
    return {
        "javac_8_accepted": True,
        "javac_unchecked_warning_observed": True,
        "ecj_3_25_rejected": True,
        "ecj_type_mismatch_observed": True,
    }


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
        env={
            **os.environ,
            "LC_ALL": "C",
            "LANG": "C",
        },
    )


def _jdt_core_path(lock: dict[str, Any], candidate_root: Path) -> Path:
    artifacts = lock.get("artifacts")
    if not isinstance(artifacts, list):
        raise CompatibilityProbeError("Candidate artifact lock is malformed.")
    for artifact in artifacts:
        if (
            isinstance(artifact, dict)
            and artifact.get("symbolic_name") == "org.eclipse.jdt.core"
        ):
            path = candidate_root / "plugins" / str(artifact.get("filename", ""))
            if not path.is_file() or common.sha256_file(path) != artifact.get(
                "sha256"
            ):
                raise CompatibilityProbeError(
                    "Locked JDT Core artifact is unavailable or changed."
                )
            return path
    raise CompatibilityProbeError("Candidate lock has no JDT Core artifact.")


def _source_fingerprint(sources: list[Path]) -> str:
    digest = hashlib.sha256()
    for source in sources:
        digest.update(source.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def target_jdk8_identity(java_home: Path) -> dict[str, Any]:
    """Capture evidence that the compatibility authority is an actual JDK 8."""

    identity = common.worker_java_identity(java_home)
    javac_path = common.java_tool(java_home, "javac")
    completed = _run([str(javac_path), "-version"], cwd=java_home)
    output = f"{completed.stdout}\n{completed.stderr}"
    compiler_major = JavaToolchainCandidate.parse_compiler_major_version_output(
        output
    )
    runtime_major = JavaToolchainCandidate.parse_major_version_output(
        f'java version "{identity.get("version", "")}"'
    )
    if (
        completed.returncode != 0
        or compiler_major != 8
        or runtime_major != 8
    ):
        raise CompatibilityProbeError(
            "--target-java-home must identify a complete JDK 8, not a newer "
            "javac using -source/-target 8."
        )
    return {
        **identity,
        "runtime_major": runtime_major,
        "compiler_major": compiler_major,
        "javac_binary_sha256": common.sha256_file(javac_path),
    }


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=(
            root
            / "locks"
            / "eclipse-2021-03-lombok-anchor-diagnostics-v2.json"
        ),
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path.home() / ".cache" / "jolink-runtime" / "jdt-poc",
    )
    parser.add_argument("--worker-java-home", type=Path, required=True)
    parser.add_argument("--target-java-home", type=Path, required=True)
    parser.add_argument("--keep-attempt", action="store_true")
    args = parser.parse_args(argv)

    attempt_id = f"cross-compiler-{uuid.uuid4().hex[:12]}"
    attempts = args.cache_root / "attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix=f"{attempt_id}-", dir=attempts))
    started = time.monotonic()
    try:
        lock = common.load_lock(args.lock)
        candidate_root = args.cache_root / "candidates" / str(lock["candidate_id"])
        common.verify_candidate(lock, candidate_root)
        expected_worker_java = (
            lock.get("worker_build", {})
            .get("java_home_identity", {})
            .get("java_binary_sha256")
        )
        worker_java = common.java_tool(args.worker_java_home, "java")
        if (
            not isinstance(expected_worker_java, str)
            or common.sha256_file(worker_java) != expected_worker_java
        ):
            raise CompatibilityProbeError(
                "Worker Java does not match the locked Worker build identity."
            )
        target_jdk = target_jdk8_identity(args.target_java_home)
        ecj_host_jdk = common.worker_java_identity(args.worker_java_home)

        fixture = root / "fixtures" / "cross-compiler-compatibility" / "src"
        sources = sorted(fixture.rglob("*.java"))
        if len(sources) != 2:
            raise CompatibilityProbeError("Compatibility fixture is incomplete.")

        javac_output = attempt / "javac-bin"
        ecj_output = attempt / "ecj-bin"
        javac_output.mkdir()
        ecj_output.mkdir()
        javac = _run(
            [
                str(common.java_tool(args.target_java_home, "javac")),
                "-J-Duser.language=en",
                "-J-Duser.country=US",
                "-encoding",
                "UTF-8",
                "-source",
                "8",
                "-target",
                "8",
                "-Xlint:unchecked",
                "-d",
                str(javac_output),
                *(str(source) for source in sources),
            ],
            cwd=attempt,
        )

        helper = (
            root
            / "target-system-helper"
            / "src"
            / "net"
            / "jolink"
            / "runtime"
            / "jdt"
            / "helper"
            / "TargetSystemLibraries.java"
        )
        target_system_attempt = attempt / "target-system"
        target_system_attempt.mkdir()
        target_snapshot = common.snapshot_target_system_libraries(
            target_java_home=args.target_java_home,
            attempt=target_system_attempt,
            helper_source=helper,
        )
        system_libraries = [
            line.strip()
            for line in target_snapshot["worker_input"].read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        jdt_core_path = _jdt_core_path(lock, candidate_root)
        ecj = _run(
            [
                str(worker_java),
                "-Duser.language=en",
                "-Duser.country=US",
                "-cp",
                str(jdt_core_path),
                "org.eclipse.jdt.internal.compiler.batch.Main",
                "-encoding",
                "UTF-8",
                "-source",
                "1.8",
                "-target",
                "1.8",
                "-proc:none",
                "-bootclasspath",
                os.pathsep.join(system_libraries),
                "-d",
                str(ecj_output),
                *(str(source) for source in sources),
            ],
            cwd=attempt,
        )
        observations = require_expected_divergence(javac, ecj)
        report = {
            "schema": "jolink.cross-compiler-compatibility.v1",
            "status": "expected_cross_compiler_divergence_observed",
            "candidate_id": lock["candidate_id"],
            "candidate_lock_sha256": common.sha256_file(args.lock),
            "worker_artifact_sha256": lock["worker_artifact"]["sha256"],
            "fixture_source_sha256": _source_fingerprint(sources),
            "target_jdk": target_jdk,
            "ecj_host_jdk": ecj_host_jdk,
            "jdt_core": {
                "version": next(
                    str(item["version"])
                    for item in lock["artifacts"]
                    if item["symbolic_name"] == "org.eclipse.jdt.core"
                ),
                "sha256": common.sha256_file(jdt_core_path),
            },
            **observations,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        }
        reports = args.cache_root / "reports" / "cross-compiler"
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
                    "status": report["status"],
                    "report_path": str(report_path),
                }
            )
        )
        return 0
    except (
        CompatibilityProbeError,
        common.SmokeError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "CROSS_COMPILER_COMPATIBILITY_PROBE_FAILED",
                    "message": str(error),
                }
            ),
            file=os.sys.stderr,
        )
        return 1
    finally:
        if not args.keep_attempt:
            shutil.rmtree(attempt, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
