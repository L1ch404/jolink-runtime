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
    assert stale.read_bytes() == stale_before
