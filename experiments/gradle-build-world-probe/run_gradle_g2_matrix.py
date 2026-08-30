#!/usr/bin/env python3
"""Run the canonical Gradle G2 version/DSL/daemon-JDK matrix."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
G2 = ROOT / "run_gradle_jdt_g2.py"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-jar", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--java8-home", type=Path, required=True)
    parser.add_argument("--java11-home", type=Path, required=True)
    parser.add_argument("--java17-home", type=Path, required=True)
    parser.add_argument("--gradle", action="append", required=True)
    parser.add_argument("--junit-jar", type=Path, action="append", required=True)
    args = parser.parse_args()
    gradle: dict[str, str] = {}
    for raw in args.gradle:
        version, separator, executable = raw.partition("=")
        if not separator:
            raise SystemExit("--gradle must be VERSION=/path")
        gradle[version] = executable
    cases = (
        ("8.10", args.java8_home, False),
        ("8.10", args.java17_home, True),
        ("8.14", args.java17_home, False),
        ("8.14", args.java17_home, True),
    )
    reports: list[dict] = []
    for version, daemon_java, kotlin_dsl in cases:
        command = [
            sys.executable,
            str(G2),
            "--probe-jar",
            str(args.probe_jar),
            "--lock",
            str(args.lock),
            "--gradle",
            gradle[version],
            "--gradle-version",
            version,
            "--java-home",
            str(daemon_java),
            "--java8-home",
            str(args.java8_home),
            "--java11-home",
            str(args.java11_home),
        ]
        if kotlin_dsl:
            command.append("--kotlin-dsl")
        for jar in args.junit_jar:
            command.extend(("--junit-jar", str(jar)))
        completed = subprocess.run(
            command,
            cwd=ROOT.parent.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit(completed.stdout + completed.stderr)
        payload = json.loads(completed.stdout.splitlines()[-1])
        if payload.get("ok") is not True:
            raise AssertionError(payload)
        reports.append(payload)
    print(
        json.dumps(
            {
                "ok": True,
                "case_count": len(reports),
                "cases": reports,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
