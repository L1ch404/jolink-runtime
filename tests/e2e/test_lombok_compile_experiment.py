from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.lombok_compile_e2e


@pytest.mark.parametrize(
    "processor_mode",
    ("explicit_path", "implicit_classpath", "explicit_name"),
)
def test_fresh_maven_and_repeated_direct_lombok_outputs_are_exact(
    tmp_path: Path,
    processor_mode: str,
) -> None:
    if os.environ.get("JOLINK_RUN_LOMBOK_COMPILE_E2E") != "1":
        pytest.skip("set JOLINK_RUN_LOMBOK_COMPILE_E2E=1")
    maven = os.environ.get("JOLINK_LOMBOK_TEST_MAVEN") or shutil.which(
        "mvn.cmd" if os.name == "nt" else "mvn"
    )
    java_home = os.environ.get("JOLINK_LOMBOK_TEST_JAVA_HOME")
    if not maven or not java_home:
        pytest.fail(
            "canonical Lombok E2E requires JOLINK_LOMBOK_TEST_JAVA_HOME "
            "and Maven"
        )
    fixture_source = (
        Path(__file__).parents[1]
        / "fixtures"
        / "java"
        / "lombok-processor-model"
    )
    fixture = tmp_path / "项目 with spaces"
    shutil.copytree(fixture_source, fixture)
    pom = fixture / "pom.xml"
    pom_text = pom.read_text(encoding="utf-8")
    java_release = (Path(java_home) / "release").read_text(
        encoding="utf-8",
        errors="replace",
    )
    jdk8_release_compatibility = (
        processor_mode == "explicit_path"
        and re.search(r'JAVA_VERSION="1\.8\.', java_release) is not None
    )
    if jdk8_release_compatibility:
        pom_text = re.sub(
            r"\s*<maven\.compiler\.source>8</maven\.compiler\.source>\s*"
            r"<maven\.compiler\.target>8</maven\.compiler\.target>",
            "\n    <maven.compiler.release>8</maven.compiler.release>",
            pom_text,
        )
    if processor_mode != "explicit_path":
        pom_text = re.sub(
            r"\s*<annotationProcessorPaths>.*?</annotationProcessorPaths>",
            "",
            pom_text,
            flags=re.DOTALL,
        )
    if processor_mode == "explicit_name":
        pom_text = pom_text.replace(
            "<configuration>",
            (
                "<configuration>\n"
                "          <annotationProcessors>\n"
                "            <annotationProcessor>"
                "lombok.launch.AnnotationProcessorHider$AnnotationProcessor"
                "</annotationProcessor>\n"
                "          </annotationProcessors>"
            ),
        )
    pom.write_text(pom_text, encoding="utf-8")
    generated = fixture / "src/main/java/example/generated"
    generated.mkdir(parents=True)
    generated_count = 180 if processor_mode == "explicit_path" else 8
    for index in range(generated_count):
        class_name = f"LongArgfileType{index:03d}{'X' * 48}"
        (generated / f"{class_name}.java").write_text(
            (
                "package example.generated; "
                f"final class {class_name} {{ "
                f'private final String value = "中文-{index}"; }}\n'
            ),
            encoding="utf-8",
        )
    stale = fixture / "target/classes/example/Stale.class"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"must-not-change")
    stale_before = stale.read_bytes()
    environment = os.environ.copy()
    source_root = str(Path(__file__).parents[2] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        value
        for value in (source_root, environment.get("PYTHONPATH", ""))
        if value
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "jolink_runtime.experiments.compile",
            "--project-path",
            str(fixture),
            "--java-home",
            java_home,
            "--maven",
            maven,
            "--attempt-root",
            str(tmp_path / f"尝试 {processor_mode} directory with spaces"),
            "--repeat",
            "2",
            "--timeout-seconds",
            "120",
            "--maven-baseline-timeout-seconds",
            "240",
            "--metadata-timeout-seconds",
            "180",
        ],
        cwd=Path(__file__).parents[2],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=360,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    output_lines = completed.stdout.strip().splitlines()
    assert len(output_lines) == 1
    payload = json.loads(output_lines[0])

    assert payload["ok"] is True
    assert payload["verification_state"] == "verified_exact"
    assert payload["trusted_for_product_decision"] is True
    assert payload["determinism"]["exact_match"] is True
    assert payload["maven_baseline"]["comparison"]["exact_match"] is True
    assert payload["target_outputs_modified"] is False
    assert payload["runtime_jdwp_touched"] is False
    assert payload["plan"]["self_output_on_compile_classpath"] is False
    if jdk8_release_compatibility:
        compiler = payload["plan"]["compiler"]
        assert compiler["release_level"] == 8
        assert compiler["platform_mode"] == "source_target"
    durations = payload["durations_ms"]
    phase_total = sum(
        value for name, value in durations.items() if name != "total"
    )
    assert phase_total <= durations["total"] + 5.0
    assert stale.read_bytes() == stale_before


def test_probe_only_resolves_model_without_running_maven_compile(
    tmp_path: Path,
) -> None:
    if os.environ.get("JOLINK_RUN_LOMBOK_COMPILE_E2E") != "1":
        pytest.skip("set JOLINK_RUN_LOMBOK_COMPILE_E2E=1")
    maven = os.environ.get("JOLINK_LOMBOK_TEST_MAVEN") or shutil.which(
        "mvn.cmd" if os.name == "nt" else "mvn"
    )
    java_home = os.environ.get("JOLINK_LOMBOK_TEST_JAVA_HOME")
    if not maven or not java_home:
        pytest.fail(
            "canonical Lombok E2E requires JOLINK_LOMBOK_TEST_JAVA_HOME "
            "and Maven"
        )
    fixture_source = (
        Path(__file__).parents[1]
        / "fixtures"
        / "java"
        / "lombok-processor-model"
    )
    fixture = tmp_path / "probe project"
    shutil.copytree(fixture_source, fixture)
    # Metadata and Processor-model resolution do not parse Java. A Maven
    # compile would fail, proving probe_ready did not execute the baseline.
    broken_source = fixture / "src/main/java/fixture/LombokFeatures.java"
    broken_source.write_text(
        "package fixture; this is deliberately invalid Java;\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    source_root = str(Path(__file__).parents[2] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        value
        for value in (source_root, environment.get("PYTHONPATH", ""))
        if value
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "jolink_runtime.experiments.compile",
            "--project-path",
            str(fixture),
            "--java-home",
            java_home,
            "--maven",
            maven,
            "--attempt-root",
            str(tmp_path / "probe attempt"),
            "--probe-only",
            "--metadata-timeout-seconds",
            "180",
        ],
        cwd=Path(__file__).parents[2],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=240,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    output_lines = completed.stdout.strip().splitlines()
    assert len(output_lines) == 1
    payload = json.loads(output_lines[0])
    assert payload["status"] == "probe_ready"
    assert payload["verification_state"] == "model_resolved"
    assert payload["maven_baseline_executed"] is False
    assert payload["direct_javac_executed"] is False
    assert payload["trusted_for_product_decision"] is False
    assert "maven_baseline" not in payload
    assert not (fixture / "target/classes").exists()
