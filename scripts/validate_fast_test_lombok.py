#!/usr/bin/env python3
"""Validate Fast Test with Lombok and a metadata Annotation Processor."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from jolink_runtime.launch.fast_test_manager import FastTestManager


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java-home", type=Path, required=True)
    args = parser.parse_args()
    java_home = args.java_home.expanduser().resolve(strict=True)
    os.environ["JAVA_HOME"] = str(java_home)
    os.environ["PATH"] = str(java_home / "bin") + os.pathsep + os.environ.get(
        "PATH", ""
    )
    with tempfile.TemporaryDirectory(prefix="jolink-fast-test-apt-") as raw:
        project = Path(raw) / "project"
        main_source = project / "src/main/java/example/Settings.java"
        test_source = project / "src/test/java/example/SettingsTest.java"
        main_source.parent.mkdir(parents=True)
        test_source.parent.mkdir(parents=True)
        (project / "pom.xml").write_text(
            """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId><artifactId>fast-test-apt</artifactId><version>1</version>
  <properties><maven.compiler.source>1.8</maven.compiler.source>
    <maven.compiler.target>1.8</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
  <dependencies>
    <dependency><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId>
      <version>1.18.20</version><scope>provided</scope></dependency>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot</artifactId>
      <version>2.2.4.RELEASE</version></dependency>
    <dependency><groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-configuration-processor</artifactId>
      <version>2.2.4.RELEASE</version><optional>true</optional></dependency>
    <dependency><groupId>junit</groupId><artifactId>junit</artifactId>
      <version>4.13.2</version><scope>test</scope></dependency>
  </dependencies>
</project>\n""",
            encoding="utf-8",
        )
        main_source.write_text(
            "package example; import lombok.Data; "
            "import org.springframework.boot.context.properties.ConfigurationProperties; "
            "@Data @ConfigurationProperties(prefix=\"sample\") "
            "public class Settings { private String name; }\n",
            encoding="utf-8",
        )
        test_source.write_text(
            "package example; import org.junit.Test; "
            "import static org.junit.Assert.assertEquals; "
            "public class SettingsTest { @Test public void lombokAccessors(){ "
            "Settings value=new Settings(); value.setName(\"joLink\"); "
            "assertEquals(\"joLink\",value.getName()); } }\n",
            encoding="utf-8",
        )
        manager = FastTestManager()
        try:
            manager.start(
                project_path=project,
                source_files=(),
                tests=("example.SettingsTest#lombokAccessors",),
                timeout_seconds=180,
                short_wait_seconds=0,
            )
            baseline = wait(manager)
            if not baseline.get("passed"):
                raise AssertionError(baseline)
            main_source.write_text(
                "package example; import lombok.Data; "
                "import org.springframework.boot.context.properties.ConfigurationProperties; "
                "@Data @ConfigurationProperties(prefix=\"sample\") "
                "public class Settings { private String name; "
                "public String label(){ return \"value:\"+name; } }\n",
                encoding="utf-8",
            )
            test_source.write_text(
                "package example; import org.junit.Test; "
                "import static org.junit.Assert.assertEquals; "
                "public class SettingsTest { @Test public void lombokAccessors(){ "
                "Settings value=new Settings(); value.setName(\"joLink\"); "
                "assertEquals(\"value:joLink\",value.label()); } }\n",
                encoding="utf-8",
            )
            manager.start(
                project_path=project,
                source_files=(
                    "src/main/java/example/Settings.java",
                    "src/test/java/example/SettingsTest.java",
                ),
                tests=("example.SettingsTest#lombokAccessors",),
                timeout_seconds=60,
                short_wait_seconds=0,
            )
            incremental = wait(manager)
            if not incremental.get("passed"):
                raise AssertionError(incremental)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "baseline_passed": baseline["passed"],
                        "incremental_passed": incremental["passed"],
                        "compile_ms": incremental["compile_ms"],
                    },
                    separators=(",", ":"),
                )
            )
        finally:
            manager.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
