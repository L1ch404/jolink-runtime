#!/usr/bin/env python3
"""Validate Java 11 Maven -> JDT -> JUnit 5 Fast Test and incrementality."""

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
    source_files: tuple[str, ...],
    selector: str = "example.TextServiceTest#normalizes",
) -> dict:
    manager.start(
        project_path=project,
        source_files=source_files,
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
    with tempfile.TemporaryDirectory(prefix="jolink-fast-test-java11-") as raw:
        project = Path(raw) / "project"
        source = project / "src/main/java/example/TextService.java"
        added_source = project / "src/main/java/example/Decorator.java"
        test = project / "src/test/java/example/TextServiceTest.java"
        base_test = project / "src/test/java/example/BaseTextTest.java"
        child_test = project / "src/test/java/example/InheritedTextTest.java"
        interface_test = project / "src/test/java/example/TextTestContract.java"
        interface_impl = project / "src/test/java/example/InterfaceTextTest.java"
        source.parent.mkdir(parents=True)
        test.parent.mkdir(parents=True)
        (project / "pom.xml").write_text(
            """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId><artifactId>java11-fast-test</artifactId>
  <version>1</version>
  <properties>
    <maven.compiler.source>11</maven.compiler.source>
    <maven.compiler.target>11</maven.compiler.target>
    <maven.compiler.parameters>true</maven.compiler.parameters>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency><groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter-engine</artifactId><version>5.7.2</version>
      <scope>test</scope></dependency>
    <dependency><groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter-params</artifactId><version>5.7.2</version>
      <scope>test</scope></dependency>
    <dependency><groupId>junit</groupId><artifactId>junit</artifactId>
      <version>4.13.2</version><scope>test</scope></dependency>
  </dependencies>
</project>
""",
            encoding="utf-8",
        )
        good_source = (
            "package example; public class TextService { "
            "public String normalize(String value) { "
            "if (value.isBlank()) return \"\"; return value.strip(); } }\n"
        )
        source.write_text(good_source, encoding="utf-8")
        test.write_text(
            "package example; import java.lang.annotation.*; "
            "import org.junit.jupiter.api.Test; "
            "import org.junit.jupiter.api.Nested; "
            "import org.junit.jupiter.params.ParameterizedTest; "
            "import org.junit.jupiter.params.provider.ValueSource; "
            "import static org.junit.jupiter.api.Assertions.assertEquals; "
            "public class TextServiceTest { "
            "@Retention(RetentionPolicy.RUNTIME) @Target(ElementType.METHOD) "
            "@Test public @interface FastCheck {} "
            "@FastCheck public void normalizes() { "
            "assertEquals(\"hi\", new TextService().normalize(\" hi \")); } "
            "@ParameterizedTest @ValueSource(strings={\" hi \",\"hi\"}) "
            "public void parameterized(String value) { assertEquals(\"hi\", "
            "new TextService().normalize(value)); } "
            "@Nested public class NestedChecks { @Test public void nested() { "
            "assertEquals(\"hi\", new TextService().normalize(\" hi \")); } } }\n",
            encoding="utf-8",
        )
        base_test.write_text(
            "package example; import org.junit.jupiter.api.Test; "
            "import static org.junit.jupiter.api.Assertions.assertEquals; "
            "public class BaseTextTest { @Test public void inherited() { "
            "assertEquals(\"hi\", new TextService().normalize(\" hi \")); } }\n",
            encoding="utf-8",
        )
        child_test.write_text(
            "package example; public class InheritedTextTest "
            "extends BaseTextTest {}\n",
            encoding="utf-8",
        )
        interface_test.write_text(
            "package example; import org.junit.jupiter.api.Test; "
            "import static org.junit.jupiter.api.Assertions.assertEquals; "
            "public interface TextTestContract { @Test default void fromInterface() { "
            "assertEquals(\"hi\", new TextService().normalize(\" hi \")); } }\n",
            encoding="utf-8",
        )
        interface_impl.write_text(
            "package example; public class InterfaceTextTest "
            "implements TextTestContract {}\n",
            encoding="utf-8",
        )
        manager = FastTestManager()
        try:
            baseline = run(manager, project, ())
            if not baseline.get("passed"):
                raise AssertionError(baseline)
            runtime_support = baseline.get("test_runtime_support", {})
            if runtime_support.get("selection_source") not in {
                "project_test_classpath",
                "local_exact",
                "maven_resolved_exact",
                "local_compatible_fallback",
            }:
                raise AssertionError(runtime_support)
            parameterized = run(
                manager,
                project,
                (),
                "example.TextServiceTest#parameterized",
            )
            if (
                not parameterized.get("passed")
                or parameterized.get("tests") != 2
            ):
                raise AssertionError(parameterized)
            nested = run(
                manager,
                project,
                (),
                "example.TextServiceTest$NestedChecks#nested",
            )
            if not nested.get("passed") or nested.get("tests") != 1:
                raise AssertionError(nested)
            inherited = run(
                manager,
                project,
                (),
                "example.InheritedTextTest#inherited",
            )
            if not inherited.get("passed") or inherited.get("tests") != 1:
                raise AssertionError(inherited)
            interface_method = run(
                manager,
                project,
                (),
                "example.InterfaceTextTest#fromInterface",
            )
            if (
                not interface_method.get("passed")
                or interface_method.get("tests") != 1
            ):
                raise AssertionError(interface_method)
            assert manager._project is not None
            output = (
                manager._project.compiler.output_directory
                / "example/TextService.class"
            )
            class_major = int.from_bytes(output.read_bytes()[6:8], "big")
            if class_major != 55:
                raise AssertionError(class_major)

            source.write_text(
                "package example; public class TextService { "
                "public String normalize(String value) { return value; } }\n",
                encoding="utf-8",
            )
            failed = run(
                manager,
                project,
                ("src/main/java/example/TextService.java",),
            )
            if failed.get("passed") is not False or not failed.get("compile_ms"):
                raise AssertionError(failed)

            source.write_text(good_source, encoding="utf-8")
            recovered = run(
                manager,
                project,
                ("src/main/java/example/TextService.java",),
            )
            if not recovered.get("passed") or not recovered.get("compile_ms"):
                raise AssertionError(recovered)

            added_source.write_text(
                "package example; public class Decorator { "
                "public static String apply(String value) { return value; } }\n",
                encoding="utf-8",
            )
            source.write_text(
                "package example; public class TextService { "
                "public String normalize(String value) { "
                "if (value.isBlank()) return \"\"; "
                "return Decorator.apply(value.strip()); } }\n",
                encoding="utf-8",
            )
            added = run(
                manager,
                project,
                (
                    "src/main/java/example/TextService.java",
                    "src/main/java/example/Decorator.java",
                ),
            )
            added_class = (
                manager._project.compiler.output_directory
                / "example/Decorator.class"
            )
            if not added.get("passed") or not added_class.is_file():
                raise AssertionError(added)

            added_source.unlink()
            source.write_text(good_source, encoding="utf-8")
            deleted = run(
                manager,
                project,
                (
                    "src/main/java/example/TextService.java",
                    "src/main/java/example/Decorator.java",
                ),
            )
            if not deleted.get("passed") or added_class.exists():
                raise AssertionError(deleted)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "java_level": 11,
                        "class_major": class_major,
                        "framework": recovered["framework"],
                        "composed_junit5_annotation_supported": True,
                        "junit5_parameterized_method_supported": True,
                        "junit5_nested_method_supported": True,
                        "junit5_inherited_method_supported": True,
                        "junit5_interface_default_method_supported": True,
                        "baseline_ms": baseline["total_ms"],
                        "incremental_failure_compile_ms": failed["compile_ms"],
                        "incremental_recovery_compile_ms": recovered["compile_ms"],
                        "launcher_companion_supported": True,
                        "launcher_selection_source": runtime_support.get(
                            "selection_source"
                        ),
                        "launcher_selected_version": runtime_support.get(
                            "selected_version"
                        ),
                        "launcher_fallback_used": runtime_support.get(
                            "fallback_used", False
                        ),
                        "source_addition_supported": True,
                        "source_deletion_supported": True,
                        "source_deletion_worker_observed": True,
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
