#!/usr/bin/env python3
"""Validate a selected Fast Test module inside a Maven reactor."""

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
    source_files: tuple[str, ...] = (),
) -> dict:
    manager.start(
        project_path=project,
        source_files=source_files,
        tests=("example.AppTest#usesReactorOutput",),
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
    with tempfile.TemporaryDirectory(prefix="jolink-fast-test-reactor-") as raw:
        root = Path(raw) / "reactor"
        lib_source = root / "lib/src/main/java/example/Value.java"
        app_source = root / "app/src/main/java/example/App.java"
        test_source = root / "app/src/test/java/example/AppTest.java"
        lib_source.parent.mkdir(parents=True)
        app_source.parent.mkdir(parents=True)
        test_source.parent.mkdir(parents=True)
        (root / "pom.xml").write_text(
            """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion><groupId>example</groupId>
  <artifactId>reactor</artifactId><version>1</version><packaging>pom</packaging>
  <modules><module>lib</module><module>app</module></modules>
  <properties><maven.compiler.source>1.8</maven.compiler.source>
    <maven.compiler.target>1.8</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
</project>
""",
            encoding="utf-8",
        )
        (root / "lib/pom.xml").write_text(
            """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion><parent><groupId>example</groupId>
    <artifactId>reactor</artifactId><version>1</version></parent>
  <artifactId>lib</artifactId>
</project>
""",
            encoding="utf-8",
        )
        (root / "app/pom.xml").write_text(
            """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion><parent><groupId>example</groupId>
    <artifactId>reactor</artifactId><version>1</version></parent>
  <artifactId>app</artifactId><dependencies>
    <dependency><groupId>example</groupId><artifactId>lib</artifactId>
      <version>1</version></dependency>
    <dependency><groupId>junit</groupId><artifactId>junit</artifactId>
      <version>4.13.2</version><scope>test</scope></dependency>
  </dependencies>
</project>
""",
            encoding="utf-8",
        )
        good_lib = (
            "package example; public class Value { "
            "public int number() { return 40; } }\n"
        )
        good_app = (
            "package example; public class App { public int result() { "
            "return new Value().number() + 2; } }\n"
        )
        lib_source.write_text(good_lib, encoding="utf-8")
        app_source.write_text(good_app, encoding="utf-8")
        test_source.write_text(
            "package example; import org.junit.Test; "
            "import static org.junit.Assert.assertEquals; "
            "public class AppTest { @Test public void usesReactorOutput() { "
            "assertEquals(42, new App().result()); } }\n",
            encoding="utf-8",
        )
        manager = FastTestManager()
        try:
            baseline = run(manager, root)
            if not baseline.get("passed"):
                raise AssertionError(baseline)
            assert manager._project is not None
            reactor_output = (root / "lib/target/classes").resolve()
            if reactor_output not in manager._project.runtime_classpath:
                raise AssertionError("Reactor output is absent from runtime classpath")

            app_source.write_text(
                "package example; public class App { public int result() { "
                "return new Value().number() + 1; } }\n",
                encoding="utf-8",
            )
            app_failed = run(
                manager,
                root,
                ("app/src/main/java/example/App.java",),
            )
            if (
                app_failed.get("ok") is not True
                or app_failed.get("passed") is not False
            ):
                raise AssertionError(app_failed)
            app_source.write_text(good_app, encoding="utf-8")
            app_recovered = run(
                manager,
                root,
                ("app/src/main/java/example/App.java",),
            )
            if not app_recovered.get("passed"):
                raise AssertionError(app_recovered)

            lib_source.write_text(
                "package example; public class Value { "
                "public int number() { return 39; } }\n",
                encoding="utf-8",
            )
            upstream_failed = run(
                manager,
                root,
                ("lib/src/main/java/example/Value.java",),
            )
            if (
                upstream_failed.get("passed") is not False
                or not upstream_failed.get("bootstrap_ms")
            ):
                raise AssertionError(upstream_failed)
            lib_source.write_text(good_lib, encoding="utf-8")
            upstream_recovered = run(
                manager,
                root,
                ("lib/src/main/java/example/Value.java",),
            )
            if (
                not upstream_recovered.get("passed")
                or not upstream_recovered.get("bootstrap_ms")
            ):
                raise AssertionError(upstream_recovered)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "reactor_module_selected": "app",
                        "reactor_output_used": True,
                        "selected_module_incremental_ms": app_recovered[
                            "compile_ms"
                        ],
                        "upstream_change_rebootstrapped": True,
                        "upstream_source_files_classified": True,
                        "upstream_recovery_passed": True,
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
