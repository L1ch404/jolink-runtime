from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.gradle_runtime_e2e
def test_real_gradle_runtime_launch_reload_and_rollback() -> None:
    if os.environ.get("JOLINK_RUN_GRADLE_RUNTIME_E2E") != "1":
        pytest.skip("set JOLINK_RUN_GRADLE_RUNTIME_E2E=1")
    names = {
        "gradle": "JOLINK_GRADLE_RUNTIME_GRADLE",
        "java8": "JOLINK_GRADLE_RUNTIME_JAVA8_HOME",
        "java11": "JOLINK_GRADLE_RUNTIME_JAVA11_HOME",
        "java17": "JOLINK_GRADLE_RUNTIME_JAVA17_HOME",
    }
    values = {key: os.environ.get(name) for key, name in names.items()}
    missing = [names[key] for key, value in values.items() if not value]
    if missing:
        pytest.skip(f"set Gradle Runtime E2E inputs: {', '.join(missing)}")
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/validate_gradle_runtime_product.py"),
            "--gradle",
            str(values["gradle"]),
            "--gradle-version",
            "8.14",
            "--java8-home",
            str(values["java8"]),
            "--java11-home",
            str(values["java11"]),
            "--java17-home",
            str(values["java17"]),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.splitlines()[-1])
    assert payload == {
        "ok": True,
        "build_system": "gradle",
        "target_java": 11,
        "baseline_ready": True,
        "hotswap_passed": True,
        "candidate_restart_passed": True,
        "rollback_passed": True,
        "final_process_state": "absent",
    }
