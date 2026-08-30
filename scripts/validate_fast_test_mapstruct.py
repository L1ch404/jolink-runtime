#!/usr/bin/env python3
"""Explore source-generating MapStruct Processor support in Fast Test."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from jolink_runtime.launch.fast_test_manager import FastTestManager


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java-home", type=Path, required=True)
    args = parser.parse_args()
    java_home = args.java_home.expanduser().resolve(strict=True)
    os.environ["JAVA_HOME"] = str(java_home)
    os.environ["PATH"] = str(java_home / "bin") + os.pathsep + os.environ.get(
        "PATH", ""
    )
    with tempfile.TemporaryDirectory(prefix="jolink-fast-test-mapstruct-") as raw:
        project = Path(raw) / "project"
        main = project / "src/main/java/example"
        test = project / "src/test/java/example/NameMapperTest.java"
        main.mkdir(parents=True)
        test.parent.mkdir(parents=True)
        (project / "pom.xml").write_text(
            """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId><artifactId>mapstruct-fast-test</artifactId><version>1</version>
  <properties><maven.compiler.source>1.8</maven.compiler.source>
    <maven.compiler.target>1.8</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
  <dependencies>
    <dependency><groupId>org.mapstruct</groupId><artifactId>mapstruct</artifactId>
      <version>1.3.0.Final</version></dependency>
    <dependency><groupId>org.mapstruct</groupId><artifactId>mapstruct-processor</artifactId>
      <version>1.3.0.Final</version><scope>provided</scope></dependency>
    <dependency><groupId>junit</groupId><artifactId>junit</artifactId>
      <version>4.13.2</version><scope>test</scope></dependency>
  </dependencies>
</project>\n""",
            encoding="utf-8",
        )
        (main / "Source.java").write_text(
            "package example; public class Source { private String name; "
            "public String getName(){return name;} "
            "public void setName(String value){name=value;} }\n",
            encoding="utf-8",
        )
        (main / "Target.java").write_text(
            "package example; public class Target { private String name; "
            "public String getName(){return name;} "
            "public void setName(String value){name=value;} }\n",
            encoding="utf-8",
        )
        (main / "NameMapper.java").write_text(
            "package example; import org.mapstruct.Mapper; "
            "import org.mapstruct.factory.Mappers; @Mapper public interface NameMapper { "
            "NameMapper INSTANCE=Mappers.getMapper(NameMapper.class); "
            "Target map(Source value); }\n",
            encoding="utf-8",
        )
        test.write_text(
            "package example; import org.junit.Test; "
            "import static org.junit.Assert.assertEquals; "
            "public class NameMapperTest { @Test public void maps(){ "
            "Source source=new Source(); source.setName(\"joLink\"); "
            "assertEquals(\"joLink\",NameMapper.INSTANCE.map(source).getName()); } }\n",
            encoding="utf-8",
        )
        manager = FastTestManager()
        try:
            manager.start(
                project_path=project,
                source_files=(),
                tests=("example.NameMapperTest#maps",),
                timeout_seconds=180,
                short_wait_seconds=0,
            )
            deadline = time.monotonic() + 180
            while manager.status()["status"] in {
                "starting",
                "bootstrapping",
                "compiling",
                "running",
            }:
                if time.monotonic() >= deadline:
                    raise TimeoutError(manager.status())
                time.sleep(0.05)
            result = manager.status()
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return 0 if result.get("passed") else 2
        finally:
            manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
