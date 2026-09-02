from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.gradle_runtime_e2e
@pytest.mark.parametrize(
    ("gradle_environment", "gradle_version", "target_java", "offline"),
    (
        ("JOLINK_GRADLE_RUNTIME_GRADLE_810", "8.10", 8, False),
        ("JOLINK_GRADLE_RUNTIME_GRADLE_814", "8.14", 11, True),
    ),
)
def test_real_gradle_runtime_launch_hotswap_and_relaunch_boundary(
    gradle_environment: str,
    gradle_version: str,
    target_java: int,
    offline: bool,
) -> None:
    if os.environ.get("JOLINK_RUN_GRADLE_RUNTIME_E2E") != "1":
        pytest.skip("set JOLINK_RUN_GRADLE_RUNTIME_E2E=1")
    names = {
        "gradle": gradle_environment,
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
            gradle_version,
            "--java8-home",
            str(values["java8"]),
            "--java11-home",
            str(values["java11"]),
            "--java17-home",
            str(values["java17"]),
            "--target-java",
            str(target_java),
            *(["--offline"] if offline else []),
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
        "target_java": target_java,
        "baseline_ready": True,
        "warm_probe_cache_reused": True,
        "warm_incremental_startup": True,
        "unchanged_startup_no_compile": True,
        "hotswap_passed": True,
        "structural_relaunch_required": True,
        "resources_sealed": True,
        "resource_drift_rejected": True,
        "runtime_probe_ignored_test_world": True,
        "private_model_deleted": True,
        "bytecode_transform_rejected": True,
        "final_process_state": "absent",
    }
