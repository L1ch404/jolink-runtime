#!/usr/bin/env python3
"""Validate the product FastTestManager against a real Gradle Wrapper project."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path

from jolink_runtime.launch.fast_test_manager import FastTestManager


ROOT = Path(__file__).resolve().parents[1]
G1 = ROOT / "experiments/gradle-build-world-probe/run_gradle_probe_spike.py"


def load_g1():
    spec = importlib.util.spec_from_file_location("jolink_gradle_fixture", G1)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load Gradle fixture helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait(manager: FastTestManager, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = manager.status()
        if result["status"] not in {
            "starting",
            "bootstrapping",
            "compiling",
            "running",
        }:
            return result
        time.sleep(0.05)
    raise TimeoutError(manager.status())


def run(
    manager: FastTestManager,
    project: Path,
    source_files: tuple[str, ...] = (),
) -> dict:
    manager.start(
        project_path=project,
        source_files=source_files,
        tests=("example.TextServiceTest#works",),
        timeout_seconds=180,
        short_wait_seconds=0,
    )
    return wait(manager)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gradle", type=Path, required=True)
    parser.add_argument("--gradle-version", required=True)
    parser.add_argument("--java8-home", type=Path, required=True)
    parser.add_argument("--java11-home", type=Path, required=True)
    parser.add_argument("--java17-home", type=Path, required=True)
    parser.add_argument("--junit-jar", type=Path, action="append", required=True)
    args = parser.parse_args()
    g1 = load_g1()
    gradle = args.gradle.expanduser().resolve(strict=True)
    java8 = args.java8_home.expanduser().resolve(strict=True)
    java11 = args.java11_home.expanduser().resolve(strict=True)
    java17 = args.java17_home.expanduser().resolve(strict=True)
    junit = tuple(path.expanduser().resolve(strict=True) for path in args.junit_jar)
    environment = {
        **os.environ,
        "JAVA_HOME": str(java17),
        "PATH": str(java17 / "bin") + os.pathsep + os.environ.get("PATH", ""),
    }
    old_environment = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="jolink-gradle-product-") as raw:
        root = Path(raw)
        environment["GRADLE_USER_HOME"] = str(root / "gradle-user-home")
        os.environ.update(environment)
        processor = g1.build_local_processor(root, java8)
        project = g1.create_fixture(
            root,
            kotlin_dsl=False,
            processor_jar=processor,
            junit_jars=junit,
            custom_test_runtime=False,
        )
        distribution = g1.private_distribution_zip(
            root, gradle=gradle, version=args.gradle_version
        )
        g1.create_wrapper(
            project,
            gradle=gradle,
            version=args.gradle_version,
            distribution_zip=distribution,
            environment=environment,
        )
        manager = FastTestManager()
        try:
            baseline = run(manager, project)
            if not baseline.get("passed"):
                raise AssertionError(baseline)
            support = baseline.get("test_runtime_support", {})
            if support.get("build_system") != "gradle":
                raise AssertionError(support)

            main = project / "src/main/java/example/TextService.java"
            good_main = main.read_text(encoding="utf-8")
            main.write_text(
                good_main.replace("return value.strip();", "return value;"),
                encoding="utf-8",
            )
            main_failed = run(
                manager,
                project,
                ("src/main/java/example/TextService.java",),
            )
            if main_failed.get("passed") is not False:
                raise AssertionError(main_failed)
            main.write_text(good_main, encoding="utf-8")
            main_recovered = run(
                manager,
                project,
                ("src/main/java/example/TextService.java",),
            )
            if not main_recovered.get("passed"):
                raise AssertionError(main_recovered)

            test = project / "src/test/java/example/TextServiceTest.java"
            good_test = test.read_text(encoding="utf-8")
            test.write_text(
                good_test.replace('assertEquals("x",', 'assertEquals("wrong",'),
                encoding="utf-8",
            )
            test_failed = run(
                manager,
                project,
                ("src/test/java/example/TextServiceTest.java",),
            )
            if test_failed.get("passed") is not False:
                raise AssertionError(test_failed)
            test.write_text(good_test, encoding="utf-8")
            test_recovered = run(
                manager,
                project,
                ("src/test/java/example/TextServiceTest.java",),
            )
            if not test_recovered.get("passed"):
                raise AssertionError(test_recovered)

            print(
                json.dumps(
                    {
                        "ok": True,
                        "build_system": "gradle",
                        "gradle_version": support["gradle_version"],
                        "baseline_passed": True,
                        "main_failure_observed": True,
                        "main_recovery_passed": True,
                        "test_failure_observed": True,
                        "test_recovery_passed": True,
                        "compile_java_home": str(java11),
                        "runtime_unchanged": True,
                    },
                    separators=(",", ":"),
                )
            )
        finally:
            if not manager.close():
                raise RuntimeError("Gradle Fast Test resources did not settle")
            os.environ.clear()
            os.environ.update(old_environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
