#!/usr/bin/env python3
"""Validate explicit TestNG class/method Fast Test and incrementality."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from jolink_runtime.launch.fast_test_manager import FastTestManager


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
    selector: str,
    sources: tuple[str, ...] = (),
) -> dict:
    manager.start(
        project_path=project,
        source_files=sources,
        tests=(selector,),
        timeout_seconds=120.0,
        short_wait_seconds=0.0,
    )
    return wait(manager)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java-home", type=Path, required=True)
    args = parser.parse_args()
    java_home = args.java_home.expanduser().resolve(strict=True)
    os.environ["JAVA_HOME"] = str(java_home)
    os.environ["PATH"] = str(java_home / "bin") + os.pathsep + os.environ.get(
        "PATH", ""
    )
    with tempfile.TemporaryDirectory(prefix="jolink-fast-test-testng-") as raw:
        project = Path(raw) / "project"
        source = project / "src/main/java/example/Calculator.java"
        test = project / "src/test/java/example/CalculatorTest.java"
        source.parent.mkdir(parents=True)
        test.parent.mkdir(parents=True)
        pom = project / "pom.xml"
        pom_text = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion><groupId>example</groupId>
  <artifactId>testng-fast-test</artifactId><version>1</version>
  <properties><maven.compiler.source>11</maven.compiler.source>
    <maven.compiler.target>11</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
  <dependencies><dependency><groupId>org.testng</groupId>
    <artifactId>testng</artifactId><version>7.11.0</version>
    <scope>test</scope><exclusions>
      <exclusion><groupId>org.slf4j</groupId><artifactId>slf4j-api</artifactId></exclusion>
      <exclusion><groupId>org.jcommander</groupId><artifactId>jcommander</artifactId></exclusion>
      <exclusion><groupId>org.webjars</groupId><artifactId>jquery</artifactId></exclusion>
      <exclusion><groupId>com.google.inject</groupId><artifactId>guice</artifactId></exclusion>
      <exclusion><groupId>org.yaml</groupId><artifactId>snakeyaml</artifactId></exclusion>
    </exclusions></dependency>
    <dependency><groupId>org.slf4j</groupId><artifactId>slf4j-api</artifactId>
      <version>2.0.17</version><scope>test</scope></dependency>
    <dependency><groupId>org.jcommander</groupId><artifactId>jcommander</artifactId>
      <version>1.83</version><scope>test</scope></dependency>
  </dependencies>
  <build><plugins><plugin><groupId>org.apache.maven.plugins</groupId>
    <artifactId>maven-failsafe-plugin</artifactId><version>3.2.5</version>
  </plugin></plugins></build>
</project>
"""
        pom.write_text(pom_text, encoding="utf-8")
        good = (
            "package example; public class Calculator { "
            "public int add(int a, int b) { return a + b; } }\n"
        )
        source.write_text(good, encoding="utf-8")
        test.write_text(
            "package example; import org.testng.annotations.Test; "
            "import static org.testng.Assert.assertEquals; "
            "public class CalculatorTest { "
            "@Test public void works() { "
            "assertEquals(new Calculator().add(1, 2), 3); } "
            "@Test public void unselectedFailure() { assertEquals(1, 2); } }\n",
            encoding="utf-8",
        )
        manager = FastTestManager()
        try:
            method = "example.CalculatorTest#works"
            baseline = run(manager, project, method)
            if not baseline.get("passed") or baseline.get("framework") != "testng":
                raise AssertionError(baseline)
            class_result = run(manager, project, "example.CalculatorTest")
            if (
                class_result.get("passed") is not False
                or class_result.get("failed_count") != 1
            ):
                raise AssertionError(class_result)

            source.write_text(
                "package example; public class Calculator { "
                "public int add(int a, int b) { return a - b; } }\n",
                encoding="utf-8",
            )
            failed = run(
                manager,
                project,
                method,
                ("src/main/java/example/Calculator.java",),
            )
            if failed.get("passed") is not False or not failed.get("compile_ms"):
                raise AssertionError(failed)
            source.write_text(good, encoding="utf-8")
            recovered = run(
                manager,
                project,
                method,
                ("src/main/java/example/Calculator.java",),
            )
            if not recovered.get("passed") or not recovered.get("compile_ms"):
                raise AssertionError(recovered)
            pom.write_text(
                pom_text.replace(
                    "<artifactId>maven-failsafe-plugin</artifactId><version>3.2.5</version>",
                    "<artifactId>maven-failsafe-plugin</artifactId><version>3.2.5</version>"
                    "<configuration><systemPropertyVariables><secret>value</secret>"
                    "</systemPropertyVariables></configuration>",
                ),
                encoding="utf-8",
            )
            rejected = run(manager, project, method)
            if rejected.get("error_code") != (
                "FAST_TEST_FAILSAFE_CONFIGURATION_UNSUPPORTED"
            ):
                raise AssertionError(rejected)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "framework": recovered["framework"],
                        "method_selector_passed": True,
                        "class_selector_failure_observed": True,
                        "failsafe_unmodeled_configuration_rejected": True,
                        "incremental_failure_compile_ms": failed["compile_ms"],
                        "incremental_recovery_compile_ms": recovered[
                            "compile_ms"
                        ],
                    },
                    separators=(",", ":"),
                )
            )
        finally:
            if not manager.close():
                raise RuntimeError("Fast Test resources did not settle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
