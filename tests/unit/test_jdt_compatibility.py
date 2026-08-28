from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from jolink_runtime.adapters.java.classfile import compare_class_output_tier1


def _compile(root: Path, source: str) -> Path:
    javac = shutil.which("javac")
    if javac is None:
        pytest.skip("javac is required for class compatibility tests")
    source_file = root / "src/example/App.java"
    output = root / "classes"
    source_file.parent.mkdir(parents=True)
    output.mkdir()
    source_file.write_text(source, encoding="utf-8")
    completed = subprocess.run(
        [javac, "-g", "-d", str(output), str(source_file)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return output


def test_jdt_baseline_gate_accepts_method_body_bytecode_difference(
    tmp_path: Path,
) -> None:
    maven = _compile(
        tmp_path / "maven",
        "package example; public class App { public int value() { return 1; } }",
    )
    jdt = _compile(
        tmp_path / "jdt",
        "package example; public class App { public int value() { return 2; } }",
    )

    result = compare_class_output_tier1(maven, jdt)

    assert result["compatible"] is True
    assert result["api_mismatch_count"] == 0


def test_jdt_baseline_gate_rejects_public_api_difference(
    tmp_path: Path,
) -> None:
    maven = _compile(
        tmp_path / "maven",
        "package example; public class App { public int value() { return 1; } }",
    )
    jdt = _compile(
        tmp_path / "jdt",
        "package example; public class App { public long value() { return 1; } }",
    )

    result = compare_class_output_tier1(maven, jdt)

    assert result["compatible"] is False
    assert result["api_mismatch_count"] == 1
