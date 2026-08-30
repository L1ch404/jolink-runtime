#!/usr/bin/env python3
"""Run Java 8-target Fast Test with JDK 8/11/17 build and Runner JVMs."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from jolink_runtime.launch.fast_test_manager import FastTestManager
from jolink_runtime.launch.idea_environment import IdeaBuildPreferences


def wait(manager: FastTestManager) -> dict:
    deadline = time.monotonic() + 180
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


def run_one(root: Path, target8: Path, build: Path, index: int) -> dict:
    project = root / f"project-{index}"
    main = project / "src/main/java/example/Value.java"
    test = project / "src/test/java/example/ValueTest.java"
    main.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    (project / "pom.xml").write_text(
        """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId><artifactId>jdk-matrix</artifactId><version>1</version>
  <properties><maven.compiler.source>1.8</maven.compiler.source>
    <maven.compiler.target>1.8</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
  <dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId>
    <version>4.13.2</version><scope>test</scope></dependency></dependencies>
</project>\n""",
        encoding="utf-8",
    )
    main.write_text(
        "package example; public class Value { public int get(){return 7;} }\n",
        encoding="utf-8",
    )
    test.write_text(
        "package example; import org.junit.Test; "
        "import static org.junit.Assert.assertEquals; "
        "public class ValueTest { @Test public void reads(){ "
        "assertEquals(7,new Value().get()); } }\n",
        encoding="utf-8",
    )
    preferences = IdeaBuildPreferences(
        project_jdk_name="target8",
        maven_runner_jdk_name="build",
        jdk_homes_by_name={
            "target8": (target8,),
            "build": (build,),
        },
    )
    manager = FastTestManager()
    manager._idea = SimpleNamespace(
        import_preferences=lambda _path: preferences
    )
    try:
        manager.start(
            project_path=project,
            source_files=(),
            tests=("example.ValueTest#reads",),
            timeout_seconds=180,
            short_wait_seconds=0,
        )
        result = wait(manager)
        if not result.get("passed"):
            raise AssertionError(result)
        return {
            "build_java_home": str(build),
            "passed": True,
            "bootstrap_ms": result["bootstrap_ms"],
            "total_ms": result["total_ms"],
        }
    finally:
        manager.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-java8-home", type=Path, required=True)
    parser.add_argument(
        "--build-java-home", type=Path, action="append", required=True
    )
    args = parser.parse_args()
    target = args.target_java8_home.expanduser().resolve(strict=True)
    builds = [path.expanduser().resolve(strict=True) for path in args.build_java_home]
    with tempfile.TemporaryDirectory(prefix="jolink-fast-test-jdk-matrix-") as raw:
        results = [
            run_one(Path(raw), target, build, index)
            for index, build in enumerate(builds)
        ]
    print(json.dumps({"ok": True, "results": results}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
