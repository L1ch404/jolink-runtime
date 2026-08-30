#!/usr/bin/env python3
"""Build and package the locked Gradle Build World Probe product assets."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "experiments/gradle-build-world-probe"
PROJECT = EXPERIMENT / "probe"
PRODUCT = ROOT / "src/jolink_runtime/launch"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gradle", type=Path, required=True)
    parser.add_argument("--java8-home", type=Path, required=True)
    args = parser.parse_args()
    gradle = args.gradle.expanduser().resolve(strict=True)
    java8 = args.java8_home.expanduser().resolve(strict=True)
    environment = {
        **os.environ,
        "JAVA_HOME": str(java8),
        "PATH": str(java8 / "bin") + os.pathsep + os.environ.get("PATH", ""),
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
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stdout + completed.stderr)
    jar = PROJECT / "build/libs/jolink-gradle-probe-0.1.0-spike3.jar"
    raw = jar.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    with zipfile.ZipFile(jar) as archive:
        majors = {
            int.from_bytes(archive.read(name)[6:8], "big")
            for name in archive.namelist()
            if name.endswith(".class")
        }
    if majors != {52}:
        raise SystemExit(f"Unexpected Probe class majors: {sorted(majors)}")
    lock = {
        "schema": "jolink.gradle-build-world-probe.product-lock.v1",
        "probe_version": "0.1.0-spike3",
        "sha256": digest,
        "class_major": 52,
        "supported_gradle_versions": ["8.10", "8.14"],
        "task_prefix": "jolinkExportBuildWorld_",
    }
    (PRODUCT / "gradle-build-world-probe.jar.b64").write_text(
        base64.b64encode(raw).decode("ascii") + "\n",
        encoding="ascii",
    )
    (PRODUCT / "gradle-build-world-probe-lock.json").write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (PRODUCT / "gradle-init.gradle").write_bytes(
        (EXPERIMENT / "init.gradle.template").read_bytes()
    )
    print(json.dumps({"ok": True, **lock}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
