from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.fast_test_e2e
def test_real_maven_jdt_junit_fast_test_product_loop() -> None:
    if os.environ.get("JOLINK_RUN_FAST_TEST_E2E") != "1":
        pytest.skip("set JOLINK_RUN_FAST_TEST_E2E=1")
    java_home = os.environ.get("JOLINK_FAST_TEST_JAVA8_HOME")
    if not java_home:
        pytest.skip("set JOLINK_FAST_TEST_JAVA8_HOME to a JDK 8 home")
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/validate_fast_test_product.py"),
            "--java-home",
            java_home,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.splitlines()[-1])
    assert payload == {
        "ok": True,
        "bootstrap_passed": True,
        "maven_config_offline_passed": True,
        "temporary_settings_deleted": True,
        "junit4_counts_correct": True,
        "stress_cycles": 20,
        "stress_max_compile_ms": payload["stress_max_compile_ms"],
        "stress_max_total_ms": payload["stress_max_total_ms"],
        "stress_max_freshness_ms": payload["stress_max_freshness_ms"],
        "stress_max_source_scan_ms": payload["stress_max_source_scan_ms"],
        "stress_max_runner_ms": payload["stress_max_runner_ms"],
        "test_resources_passed": True,
        "system_loader_passed": True,
        "resource_change_rebootstrapped": True,
        "resource_drift_reported": True,
        "failure_observed": True,
        "test_compile_failure_observed": True,
        "stale_output_rejected": True,
        "test_source_edit_observed": True,
        "incremental_compile_ms": payload["incremental_compile_ms"],
        "recovery_passed": True,
        "runtime_unchanged": True,
        "mcp_passed": True,
        "mcp_cancelled": True,
        "mcp_assertion_failure_is_error": False,
        "junit5_passed": True,
        "junit5_container_failure_observed": True,
        "mixed_junit_passed": True,
        "surefire_config_rejected": True,
        "system_exit_isolated": True,
        "non_daemon_thread_terminated": True,
        "cancelled": True,
        "post_isolation_passed": True,
        "log_retention_bounded": True,
        "stdout_protocol_isolated": True,
        "timeout_observed": True,
    }


@pytest.mark.fast_test_e2e
def test_real_lombok_processor_fast_test_loop() -> None:
    if os.environ.get("JOLINK_RUN_FAST_TEST_E2E") != "1":
        pytest.skip("set JOLINK_RUN_FAST_TEST_E2E=1")
    java_home = os.environ.get("JOLINK_FAST_TEST_JAVA8_HOME")
    if not java_home:
        pytest.skip("set JOLINK_FAST_TEST_JAVA8_HOME to a JDK 8 home")
    root = Path(__file__).resolve().parents[2]
    environment = {**os.environ, "MAVEN_ARGS": "--offline"}
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/validate_fast_test_lombok.py"),
            "--java-home",
            java_home,
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.splitlines()[-1])
    assert payload["ok"] is True
    assert payload["baseline_passed"] is True
    assert payload["incremental_passed"] is True


@pytest.mark.fast_test_e2e
def test_real_java11_junit5_fast_test_loop() -> None:
    if os.environ.get("JOLINK_RUN_FAST_TEST_E2E") != "1":
        pytest.skip("set JOLINK_RUN_FAST_TEST_E2E=1")
    java_home = os.environ.get("JOLINK_FAST_TEST_JAVA11_HOME")
    if not java_home:
        pytest.skip("set JOLINK_FAST_TEST_JAVA11_HOME to a JDK 11 home")
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/validate_fast_test_java11.py"),
            "--java-home",
            java_home,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.splitlines()[-1])
    assert payload["ok"] is True
    assert payload["java_level"] == 11
    assert payload["class_major"] == 55
    assert payload["framework"] == "junit5"
    assert payload["launcher_companion_supported"] is True
    assert payload["launcher_selection_source"] in {
        "project_test_classpath",
        "local_exact",
        "maven_resolved_exact",
        "local_compatible_fallback",
    }
    assert payload["composed_junit5_annotation_supported"] is True
    assert payload["junit5_parameterized_method_supported"] is True
    assert payload["junit5_nested_method_supported"] is True
    assert payload["junit5_inherited_method_supported"] is True
    assert payload["junit5_interface_default_method_supported"] is True
    assert payload["source_addition_supported"] is True
    assert payload["source_deletion_supported"] is True
    assert payload["source_deletion_worker_observed"] is True


@pytest.mark.fast_test_e2e
def test_real_maven_reactor_fast_test_loop() -> None:
    if os.environ.get("JOLINK_RUN_FAST_TEST_E2E") != "1":
        pytest.skip("set JOLINK_RUN_FAST_TEST_E2E=1")
    java_home = os.environ.get("JOLINK_FAST_TEST_JAVA8_HOME")
    if not java_home:
        pytest.skip("set JOLINK_FAST_TEST_JAVA8_HOME to a JDK 8 home")
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/validate_fast_test_reactor.py"),
            "--java-home",
            java_home,
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
    assert payload["ok"] is True
    assert payload["reactor_module_selected"] == "app"
    assert payload["reactor_output_used"] is True
    assert payload["upstream_change_rebootstrapped"] is True
    assert payload["upstream_source_files_classified"] is True
    assert payload["upstream_recovery_passed"] is True


@pytest.mark.fast_test_e2e
def test_real_testng_fast_test_loop() -> None:
    if os.environ.get("JOLINK_RUN_FAST_TEST_E2E") != "1":
        pytest.skip("set JOLINK_RUN_FAST_TEST_E2E=1")
    java_home = os.environ.get("JOLINK_FAST_TEST_JAVA11_HOME")
    if not java_home:
        pytest.skip("set JOLINK_FAST_TEST_JAVA11_HOME to a JDK 11 home")
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/validate_fast_test_testng.py"),
            "--java-home",
            java_home,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.splitlines()[-1])
    assert payload["ok"] is True
    assert payload["framework"] == "testng"
    assert payload["method_selector_passed"] is True
    assert payload["class_selector_failure_observed"] is True
    assert payload["failsafe_unmodeled_configuration_rejected"] is True
