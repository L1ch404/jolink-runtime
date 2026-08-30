#!/usr/bin/env python3
"""Build the deterministic G1 Probe JAR and verify its content lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT / "probe"
LOCK = ROOT / "probe-lock.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gradle", type=Path, required=True)
    parser.add_argument("--java-home", type=Path, required=True)
    args = parser.parse_args()
    gradle = args.gradle.expanduser().resolve(strict=True)
    java_home = args.java_home.expanduser().resolve(strict=True)
    environment = {
        **os.environ,
        "JAVA_HOME": str(java_home),
        "PATH": str(java_home / "bin") + os.pathsep + os.environ.get("PATH", ""),
    }
    completed = subprocess.run(
        (
            str(gradle),
            "--offline",
            "--no-daemon",
            "-p",
            str(PROJECT),
            "clean",
            "jar",
        ),
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=180,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stdout + completed.stderr)
    jar = PROJECT / "build/libs/jolink-gradle-probe-0.1.0-spike1.jar"
    digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    with zipfile.ZipFile(jar) as archive:
        majors = {
            int.from_bytes(archive.read(name)[6:8], "big")
            for name in archive.namelist()
            if name.endswith(".class")
        }
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    if digest != lock["sha256"] or majors != {lock["class_major"]}:
        raise SystemExit(
            json.dumps(
                {
                    "ok": False,
                    "error": "probe_lock_mismatch",
                    "actual_sha256": digest,
                    "actual_majors": sorted(majors),
                },
                separators=(",", ":"),
            )
        )
    print(
        json.dumps(
            {
                "ok": True,
                "probe_jar": str(jar),
                "sha256": digest,
                "class_major": lock["class_major"],
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
