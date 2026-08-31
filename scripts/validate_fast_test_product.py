#!/usr/bin/env python3
"""Run the headless Maven Bootstrap -> JDT incremental -> JUnit Fast Test loop."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

import anyio

from jolink_runtime.launch.fast_test_manager import FastTestManager
from jolink_runtime.server.mcp_server import RuntimeMCPBoundary


def wait(manager: FastTestManager, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = manager.status()
        if status["status"] not in {
            "starting",
            "bootstrapping",
            "compiling",
            "running",
        }:
            return status
        time.sleep(0.05)
    raise TimeoutError(manager.status())


async def validate_mcp(project: Path) -> dict:
    boundary = RuntimeMCPBoundary()
    try:
        listed = await boundary.list_tools()
        application = next(tool for tool in listed if tool.name == "java_application")
        assert "test" in application.inputSchema["properties"]["action"]["enum"]
        started = await boundary.call_tool(
            "java_application",
            {
                "action": "test",
                "project_path": str(project),
                "tests": ["example.CalculatorTest#adds"],
                "timeout": 120,
            },
        )
        payload = dict(started.structuredContent or {})
        if payload.get("status") not in {
            "starting",
            "bootstrapping",
            "compiling",
            "running",
            "completed",
        }:
            raise AssertionError(payload)
        deadline = time.monotonic() + 120
        while payload.get("status") in {
            "starting",
            "bootstrapping",
            "compiling",
            "running",
        }:
            if time.monotonic() >= deadline:
                raise TimeoutError(payload)
            await anyio.sleep(0.05)
            observed = await boundary.call_tool(
                "java_status", {"action": "status"}
            )
            payload = dict(observed.structuredContent or {})["fast_test"]
        if not payload.get("passed"):
            raise AssertionError(payload)
        main_source = project / "src/main/java/example/Calculator.java"
        main_source.write_text(
            "package example; public class Calculator { "
            "public int add(int a,int b){ return a-b; } }\n",
            encoding="utf-8",
        )
        failed_call = await boundary.call_tool(
            "java_application",
            {
                "action": "test",
                "project_path": str(project),
                "source_files": ["src/main/java/example/Calculator.java"],
                "tests": ["example.CalculatorTest#adds"],
                "timeout": 30,
            },
        )
        failed_payload = dict(failed_call.structuredContent or {})
        while failed_payload.get("status") in {
            "starting",
            "bootstrapping",
            "compiling",
            "running",
        }:
            await anyio.sleep(0.05)
            observed = await boundary.call_tool(
                "java_status", {"action": "status"}
            )
            failed_payload = dict(observed.structuredContent or {})["fast_test"]
        if (
            failed_call.isError is True
            or failed_payload.get("ok") is not True
            or failed_payload.get("passed") is not False
        ):
            raise AssertionError((failed_call.isError, failed_payload))
        main_source.write_text(
            "package example; public class Calculator { "
            "public int add(int a,int b){ return a+b; } }\n",
            encoding="utf-8",
        )
        recovered_call = await boundary.call_tool(
            "java_application",
            {
                "action": "test",
                "project_path": str(project),
                "source_files": ["src/main/java/example/Calculator.java"],
                "tests": ["example.CalculatorTest#adds"],
                "timeout": 30,
            },
        )
        recovered_payload = dict(recovered_call.structuredContent or {})
        while recovered_payload.get("status") in {
            "starting",
            "bootstrapping",
            "compiling",
            "running",
        }:
            await anyio.sleep(0.05)
            observed = await boundary.call_tool(
                "java_status", {"action": "status"}
            )
            recovered_payload = dict(observed.structuredContent or {})[
                "fast_test"
            ]
        if not recovered_payload.get("passed"):
            raise AssertionError(recovered_payload)
        hanging = await boundary.call_tool(
            "java_application",
            {
                "action": "test",
                "project_path": str(project),
                "tests": ["example.CalculatorTest#hangs"],
                "timeout": 60,
            },
        )
        hanging_payload = dict(hanging.structuredContent or {})
        if hanging_payload.get("status") != "running":
            raise AssertionError(hanging_payload)
        cancelled = await boundary.call_tool(
            "java_application",
            {
                "action": "cancel_test",
                "test_run_id": hanging_payload["test_run_id"],
            },
        )
        cancelled_payload = dict(cancelled.structuredContent or {})
        if cancelled_payload.get("status") != "cancel_requested":
            raise AssertionError(cancelled_payload)
        deadline = time.monotonic() + 10
        while True:
            observed = await boundary.call_tool(
                "java_status", {"action": "status"}
            )
            fast_test = dict(observed.structuredContent or {})["fast_test"]
            if fast_test.get("status") == "cancelled":
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(fast_test)
            await anyio.sleep(0.05)
        payload["cancelled"] = True
        payload["assertion_failure_is_error"] = failed_call.isError is True
        return payload
    finally:
        await boundary.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java-home", type=Path, required=True)
    args = parser.parse_args()
    java_home = args.java_home.expanduser().resolve(strict=True)
    os.environ["JAVA_HOME"] = str(java_home)
    os.environ["PATH"] = str(java_home / "bin") + os.pathsep + os.environ.get(
        "PATH", ""
    )
    with tempfile.TemporaryDirectory(prefix="jolink-fast-test-product-") as raw:
        project = Path(raw) / "project"
        main = project / "src/main/java/example/Calculator.java"
        test = project / "src/test/java/example/CalculatorTest.java"
        counting_test = project / "src/test/java/example/CountingTest.java"
        test_resource = project / "src/test/resources/fixture.txt"
        main_resource = project / "src/main/resources/main-fixture.txt"
        main.parent.mkdir(parents=True)
        test.parent.mkdir(parents=True)
        (project / ".mvn").mkdir()
        (project / ".mvn/maven.config").write_text(
            "--offline\n", encoding="utf-8"
        )
        test_resource.parent.mkdir(parents=True)
        main_resource.parent.mkdir(parents=True)
        test_resource.write_text("resource-ok\n", encoding="utf-8")
        main_resource.write_text("main-resource-ok\n", encoding="utf-8")
        (project / "pom.xml").write_text(
            """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId><artifactId>fast-test</artifactId><version>1</version>
  <properties><maven.compiler.source>1.8</maven.compiler.source>
    <maven.compiler.target>1.8</maven.compiler.target>
    <test.skip>true</test.skip>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
  <dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId>
    <version>4.13.2</version><scope>test</scope></dependency></dependencies>
  <build><plugins><plugin>
    <groupId>org.apache.maven.plugins</groupId>
    <artifactId>maven-compiler-plugin</artifactId><version>3.8.1</version>
    <executions><execution><id>test-memory</id>
      <goals><goal>testCompile</goal></goals>
      <configuration><compilerArgs><arg>-J-Xmx3g</arg></compilerArgs>
      </configuration>
    </execution></executions>
  </plugin><plugin>
    <groupId>org.apache.maven.plugins</groupId>
    <artifactId>maven-surefire-plugin</artifactId><version>2.22.2</version>
    <configuration><skipTests>${test.skip}</skipTests></configuration>
  </plugin></plugins></build>
</project>\n""",
            encoding="utf-8",
        )
        main.write_text(
            "package example; public class Calculator { "
            "public int add(int a,int b){ return a+b; } }\n",
            encoding="utf-8",
        )
        original_test = (
            "package example; import org.junit.Test; "
            "import static org.junit.Assert.*; "
            "public class CalculatorTest { @Test public void adds(){ "
            "assertEquals(3,new Calculator().add(1,2)); } "
            "@Test public void prints(){ System.out.println(\"{not-protocol}\"); "
            "assertEquals(3,new Calculator().add(1,2)); } "
            "@Test public void resources(){ "
            "assertNotNull(getClass().getResource(\"/fixture.txt\")); "
            "assertNotNull(getClass().getResource(\"/main-fixture.txt\")); } "
            "@Test public void systemLoader() throws Exception { "
            "assertNotNull(ClassLoader.getSystemClassLoader()"
            ".loadClass(\"example.Calculator\")); } "
            "@Test public void changesResource() throws Exception { "
            "java.nio.file.Files.write(java.nio.file.Paths.get("
            "\"src/test/resources/fixture.txt\"),"
            "\"changed-by-test\\n\".getBytes(\"UTF-8\")); } "
            "@Test public void exits(){ System.exit(0); } "
            "@Test public void leavesThread(){ Thread thread=new Thread(() -> { "
            "while(true){ try { Thread.sleep(1000); } catch(Exception ignored){} } "
            "}); thread.setDaemon(false); thread.start(); } "
            "@Test public void hangs() throws Exception { Thread.sleep(60000); } }\n"
        )
        test.write_text(original_test, encoding="utf-8")
        counting_test.write_text(
            "package example; import org.junit.*; "
            "public class CountingTest { "
            "@Test public void passes(){} "
            "@Ignore @Test public void ignored(){} "
            "@Test public void assumption(){ Assume.assumeTrue(false); } }\n",
            encoding="utf-8",
        )
        manager = FastTestManager()
        try:
            first = manager.start(
                project_path=project,
                source_files=(),
                tests=("example.CalculatorTest#adds",),
                timeout_seconds=120.0,
                short_wait_seconds=0.0,
            )
            if first["status"] not in {"starting", "bootstrapping"}:
                raise AssertionError(first)
            baseline = wait(manager)
            if not baseline.get("passed"):
                raise AssertionError(baseline)
            if manager._project.compiler.max_heap_mb != 3072:
                raise AssertionError(
                    manager._project.compiler.max_heap_mb
                )
            temporary_settings_deleted = not any(
                manager._project.session_root.rglob(
                    "maven-probe-settings.xml"
                )
            )
            if not temporary_settings_deleted:
                raise AssertionError("temporary Maven settings retained")
            manager.start(
                project_path=project,
                source_files=(),
                tests=("example.CountingTest",),
                timeout_seconds=20.0,
                short_wait_seconds=0.0,
            )
            junit4_counts = wait(manager)
            if not (
                junit4_counts.get("tests") == 3
                and junit4_counts.get("passed_count") == 1
                and junit4_counts.get("skipped_count") == 2
                and junit4_counts.get("failed_count") == 0
            ):
                raise AssertionError(junit4_counts)

            stress_project_identity = id(manager._project)
            stress_compile_ms: list[float] = []
            stress_total_ms: list[float] = []
            stress_freshness_ms: list[float] = []
            stress_source_scan_ms: list[float] = []
            stress_runner_ms: list[float] = []
            for cycle in range(20):
                offset = cycle + 1
                main.write_text(
                    "package example; public class Calculator { "
                    f"public int add(int a,int b){{ return a+b+{offset}; }} }}\n",
                    encoding="utf-8",
                )
                test.write_text(
                    original_test.replace(
                        "assertEquals(3,new Calculator().add(1,2)); } ",
                        (
                            f"assertEquals({3 + offset},"
                            "new Calculator().add(1,2)); } "
                        ),
                        1,
                    ),
                    encoding="utf-8",
                )
                manager.start(
                    project_path=project,
                    source_files=(
                        "src/main/java/example/Calculator.java",
                        "src/test/java/example/CalculatorTest.java",
                    ),
                    tests=("example.CalculatorTest#adds",),
                    timeout_seconds=30.0,
                    short_wait_seconds=0.0,
                )
                stressed = wait(manager)
                if (
                    not stressed.get("passed")
                    or id(manager._project) != stress_project_identity
                ):
                    raise AssertionError(stressed)
                stress_compile_ms.append(float(stressed["compile_ms"]))
                stress_total_ms.append(float(stressed["total_ms"]))
                stress_freshness_ms.append(float(stressed["freshness_ms"]))
                stress_source_scan_ms.append(
                    float(stressed["source_scan_ms"])
                )
                stress_runner_ms.append(float(stressed["runner_ms"]))
            main.write_text(
                "package example; public class Calculator { "
                "public int add(int a,int b){ return a+b; } }\n",
                encoding="utf-8",
            )
            test.write_text(original_test, encoding="utf-8")
            manager.start(
                project_path=project,
                source_files=(
                    "src/main/java/example/Calculator.java",
                    "src/test/java/example/CalculatorTest.java",
                ),
                tests=("example.CalculatorTest#adds",),
                timeout_seconds=30.0,
                short_wait_seconds=0.0,
            )
            stress_recovered = wait(manager)
            if not stress_recovered.get("passed"):
                raise AssertionError(stress_recovered)
            manager.start(
                project_path=project,
                source_files=(),
                tests=("example.CalculatorTest#resources",),
                timeout_seconds=20.0,
                short_wait_seconds=0.0,
            )
            resources_passed = wait(manager)
            if not resources_passed.get("passed"):
                raise AssertionError(resources_passed)
            manager.start(
                project_path=project,
                source_files=(),
                tests=("example.CalculatorTest#systemLoader",),
                timeout_seconds=20.0,
                short_wait_seconds=0.0,
            )
            system_loader_passed = wait(manager)
            if not system_loader_passed.get("passed"):
                raise AssertionError(system_loader_passed)
            before_resource_project = id(manager._project)
            manager.start(
                project_path=project,
                source_files=(),
                tests=("example.CalculatorTest#changesResource",),
                timeout_seconds=20.0,
                short_wait_seconds=0.0,
            )
            resource_drift = wait(manager)
            if not (
                resource_drift.get("passed")
                and resource_drift.get("build_world_changes_pending") is True
            ):
                raise AssertionError(resource_drift)
            manager.start(
                project_path=project,
                source_files=(),
                tests=("example.CalculatorTest#resources",),
                timeout_seconds=120.0,
                short_wait_seconds=0.0,
            )
            resource_rebootstrap = wait(manager)
            if (
                not resource_rebootstrap.get("passed")
                or id(manager._project) == before_resource_project
            ):
                raise AssertionError(resource_rebootstrap)

            main.write_text(
                "package example; public class Calculator { "
                "public String add(){ return \"changed\"; } }\n",
                encoding="utf-8",
            )
            manager.start(
                project_path=project,
                source_files=("src/main/java/example/Calculator.java",),
                tests=("example.CalculatorTest#adds",),
                timeout_seconds=30.0,
                short_wait_seconds=0.0,
            )
            test_compile_failed = wait(manager)
            if (
                test_compile_failed.get("status") != "compile_failed"
                or test_compile_failed.get("main_compile_ok") is not True
                or test_compile_failed.get("test_compile_ok") is not False
            ):
                raise AssertionError(test_compile_failed)
            manager.start(
                project_path=project,
                source_files=(),
                tests=("example.CalculatorTest#adds",),
                timeout_seconds=20.0,
                short_wait_seconds=0.0,
            )
            stale_output_rejected = wait(manager)
            if stale_output_rejected.get("error_code") != (
                "FAST_TEST_COMPILE_STATE_INVALID"
            ):
                raise AssertionError(stale_output_rejected)

            main.write_text(
                "package example; public class Calculator { "
                "public int add(int a,int b){ return a+b; } }\n",
                encoding="utf-8",
            )
            manager.start(
                project_path=project,
                source_files=("src/main/java/example/Calculator.java",),
                tests=("example.CalculatorTest#adds",),
                timeout_seconds=30.0,
                short_wait_seconds=0.0,
            )
            compile_recovered = wait(manager)
            if not compile_recovered.get("passed"):
                raise AssertionError(compile_recovered)

            main.write_text(
                "package example; public class Calculator { "
                "public int add(int a,int b){ return a-b; } }\n",
                encoding="utf-8",
            )
            manager.start(
                project_path=project,
                source_files=("src/main/java/example/Calculator.java",),
                tests=("example.CalculatorTest#adds",),
                timeout_seconds=30.0,
                short_wait_seconds=0.0,
            )
            failed = wait(manager)
            if failed.get("passed") is not False or failed.get("failed_count") != 1:
                raise AssertionError(failed)

            main.write_text(
                "package example; public class Calculator { "
                "public int add(int a,int b){ return a+b; } }\n",
                encoding="utf-8",
            )
            manager.start(
                project_path=project,
                source_files=("src/main/java/example/Calculator.java",),
                tests=("example.CalculatorTest#adds",),
                timeout_seconds=30.0,
                short_wait_seconds=0.0,
            )
            recovered = wait(manager)
            if not recovered.get("passed"):
                raise AssertionError(recovered)

            test.write_text(
                original_test.replace(
                    "assertEquals(3,new Calculator().add(1,2)); } ",
                    "assertEquals(4,new Calculator().add(1,2)); } ",
                    1,
                ),
                encoding="utf-8",
            )
            manager.start(
                project_path=project,
                source_files=("src/test/java/example/CalculatorTest.java",),
                tests=("example.CalculatorTest#adds",),
                timeout_seconds=30.0,
                short_wait_seconds=0.0,
            )
            test_edit_failed = wait(manager)
            if test_edit_failed.get("failed_count") != 1:
                raise AssertionError(test_edit_failed)
            test.write_text(original_test, encoding="utf-8")
            manager.start(
                project_path=project,
                source_files=("src/test/java/example/CalculatorTest.java",),
                tests=("example.CalculatorTest#prints",),
                timeout_seconds=30.0,
                short_wait_seconds=0.0,
            )
            protocol_isolated = wait(manager)
            if not protocol_isolated.get("passed"):
                raise AssertionError(protocol_isolated)

            manager.start(
                project_path=project,
                source_files=(),
                tests=("example.CalculatorTest#exits",),
                timeout_seconds=20.0,
                short_wait_seconds=0.0,
            )
            exited = wait(manager)
            if exited.get("error_code") != "TEST_RUNNER_FAILED":
                raise AssertionError(exited)

            manager.start(
                project_path=project,
                source_files=(),
                tests=("example.CalculatorTest#leavesThread",),
                timeout_seconds=5.0,
                short_wait_seconds=0.0,
            )
            non_daemon_thread = wait(manager)
            if not non_daemon_thread.get("passed"):
                raise AssertionError(non_daemon_thread)

            hanging = manager.start(
                project_path=project,
                source_files=(),
                tests=("example.CalculatorTest#hangs",),
                timeout_seconds=60.0,
                short_wait_seconds=0.0,
            )
            deadline = time.monotonic() + 10
            while manager.status()["status"] != "running":
                if time.monotonic() >= deadline:
                    raise TimeoutError(manager.status())
                time.sleep(0.02)
            cancelled = manager.cancel(hanging["test_run_id"])
            cancelled_result = wait(manager)
            if (
                cancelled["status"] != "cancel_requested"
                or cancelled_result["status"] != "cancelled"
            ):
                raise AssertionError((cancelled, cancelled_result))

            manager.start(
                project_path=project,
                source_files=(),
                tests=("example.CalculatorTest#hangs",),
                timeout_seconds=0.5,
                short_wait_seconds=0.0,
            )
            timed_out = wait(manager)
            if timed_out.get("error_code") != "TEST_TIMEOUT":
                raise AssertionError(timed_out)

            manager.start(
                project_path=project,
                source_files=(),
                tests=("example.CalculatorTest#adds",),
                timeout_seconds=20.0,
                short_wait_seconds=0.0,
            )
            post_isolation = wait(manager)
            if not post_isolation.get("passed"):
                raise AssertionError(post_isolation)
            retained_attempts = list(manager._project.test_attempts)
            log_retention_bounded = (
                len(retained_attempts) <= 9
                and sum(not failed for _path, failed in retained_attempts) <= 1
                and sum(failed for _path, failed in retained_attempts) <= 8
                and all(path.is_dir() for path, _failed in retained_attempts)
            )
            if not log_retention_bounded:
                raise AssertionError(retained_attempts)
            mcp_result = anyio.run(validate_mcp, project)

            junit5_project = Path(raw) / "junit5-project"
            junit5_main = (
                junit5_project / "src/main/java/example/Calculator.java"
            )
            junit5_test = (
                junit5_project / "src/test/java/example/CalculatorTest.java"
            )
            junit5_broken = (
                junit5_project / "src/test/java/example/BrokenContainerTest.java"
            )
            junit5_legacy = (
                junit5_project / "src/test/java/example/LegacyJUnit4Test.java"
            )
            junit5_main.parent.mkdir(parents=True)
            junit5_test.parent.mkdir(parents=True)
            (junit5_project / "pom.xml").write_text(
                """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId><artifactId>fast-test-junit5</artifactId><version>1</version>
  <properties><maven.compiler.source>1.8</maven.compiler.source>
    <maven.compiler.target>1.8</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
  <dependencies>
    <dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter-api</artifactId>
      <version>5.8.2</version><scope>test</scope></dependency>
    <dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter-engine</artifactId>
      <version>5.8.2</version><scope>test</scope></dependency>
    <dependency><groupId>org.junit.platform</groupId><artifactId>junit-platform-launcher</artifactId>
      <version>1.8.2</version><scope>test</scope></dependency>
    <dependency><groupId>junit</groupId><artifactId>junit</artifactId>
      <version>4.13.2</version><scope>test</scope></dependency>
  </dependencies>
</project>\n""",
                encoding="utf-8",
            )
            junit5_main.write_text(
                "package example; public class Calculator { "
                "public int add(int a,int b){ return a+b; } }\n",
                encoding="utf-8",
            )
            junit5_test.write_text(
                "package example; import org.junit.jupiter.api.Test; "
                "import static org.junit.jupiter.api.Assertions.assertEquals; "
                "public class CalculatorTest { @Test void adds(){ "
                "assertEquals(3,new Calculator().add(1,2)); } }\n",
                encoding="utf-8",
            )
            junit5_broken.write_text(
                "package example; import org.junit.jupiter.api.*; "
                "public class BrokenContainerTest { "
                "@BeforeAll static void init(){ throw new RuntimeException(\"boom\"); } "
                "@Test void neverRuns(){} }\n",
                encoding="utf-8",
            )
            junit5_legacy.write_text(
                "package example; import org.junit.Test; "
                "public class LegacyJUnit4Test { @Test public void legacy(){} }\n",
                encoding="utf-8",
            )
            manager5 = FastTestManager()
            try:
                manager5.start(
                    project_path=junit5_project,
                    source_files=(),
                    tests=("example.CalculatorTest#adds",),
                    timeout_seconds=120.0,
                    short_wait_seconds=0.0,
                )
                junit5_result = wait(manager5)
                if (
                    not junit5_result.get("passed")
                    or junit5_result.get("framework") != "junit5"
                ):
                    raise AssertionError(junit5_result)
                manager5.start(
                    project_path=junit5_project,
                    source_files=(),
                    tests=("example.BrokenContainerTest",),
                    timeout_seconds=30.0,
                    short_wait_seconds=0.0,
                )
                junit5_container_failed = wait(manager5)
                if not (
                    junit5_container_failed.get("passed") is False
                    and junit5_container_failed.get("failed_count", 0) >= 1
                    and junit5_container_failed.get(
                        "failed_container_count", 0
                    ) >= 1
                ):
                    raise AssertionError(junit5_container_failed)
                manager5.start(
                    project_path=junit5_project,
                    source_files=(),
                    tests=(
                        "example.CalculatorTest#adds",
                        "example.LegacyJUnit4Test#legacy",
                    ),
                    timeout_seconds=30.0,
                    short_wait_seconds=0.0,
                )
                mixed_junit = wait(manager5)
                if not (
                    mixed_junit.get("passed") is True
                    and mixed_junit.get("tests") == 2
                    and mixed_junit.get("framework") == "mixed"
                ):
                    raise AssertionError(mixed_junit)
            finally:
                manager5.close()

            surefire_project = Path(raw) / "surefire-config-project"
            surefire_main = (
                surefire_project / "src/main/java/example/Calculator.java"
            )
            surefire_test = (
                surefire_project / "src/test/java/example/CalculatorTest.java"
            )
            surefire_main.parent.mkdir(parents=True)
            surefire_test.parent.mkdir(parents=True)
            (surefire_project / "pom.xml").write_text(
                """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId><artifactId>surefire-config</artifactId><version>1</version>
  <properties><maven.compiler.source>1.8</maven.compiler.source>
    <maven.compiler.target>1.8</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
  <dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId>
    <version>4.13.2</version><scope>test</scope></dependency></dependencies>
  <build><plugins><plugin><groupId>org.apache.maven.plugins</groupId>
    <artifactId>maven-surefire-plugin</artifactId><version>2.22.2</version>
    <configuration><systemPropertyVariables><mode>special</mode>
    </systemPropertyVariables><jdkToolchain><version>8</version></jdkToolchain>
    <useSystemClassLoader>false</useSystemClassLoader></configuration>
    </plugin></plugins></build>
</project>\n""",
                encoding="utf-8",
            )
            surefire_main.write_text(
                "package example; public class Calculator {}\n",
                encoding="utf-8",
            )
            surefire_test.write_text(
                "package example; import org.junit.Test; "
                "public class CalculatorTest { @Test public void works(){} }\n",
                encoding="utf-8",
            )
            surefire_manager = FastTestManager()
            try:
                surefire_manager.start(
                    project_path=surefire_project,
                    source_files=(),
                    tests=("example.CalculatorTest#works",),
                    timeout_seconds=120,
                    short_wait_seconds=0,
                )
                surefire_rejected = wait(surefire_manager)
                if surefire_rejected.get("error_code") != (
                    "FAST_TEST_SUREFIRE_CONFIGURATION_UNSUPPORTED"
                ):
                    raise AssertionError(surefire_rejected)
                if not {
                    "systemPropertyVariables",
                    "jdkToolchain",
                    "useSystemClassLoader",
                }.issubset(
                    set(
                        surefire_rejected.get(
                            "unsupported_configuration_names", []
                        )
                    )
                ):
                    raise AssertionError(surefire_rejected)
            finally:
                surefire_manager.close()
            print(
                json.dumps(
                    {
                        "ok": True,
                        "bootstrap_passed": baseline["passed"],
                        "maven_config_offline_passed": baseline["passed"],
                        "temporary_settings_deleted": (
                            temporary_settings_deleted
                        ),
                        "junit4_counts_correct": (
                            junit4_counts["tests"] == 3
                        ),
                        "stress_cycles": len(stress_compile_ms),
                        "stress_max_compile_ms": max(stress_compile_ms),
                        "stress_max_total_ms": max(stress_total_ms),
                        "stress_max_freshness_ms": max(stress_freshness_ms),
                        "stress_max_source_scan_ms": max(
                            stress_source_scan_ms
                        ),
                        "stress_max_runner_ms": max(stress_runner_ms),
                        "test_resources_passed": resources_passed["passed"],
                        "system_loader_passed": system_loader_passed["passed"],
                        "resource_change_rebootstrapped": (
                            resource_rebootstrap["passed"]
                        ),
                        "resource_drift_reported": (
                            resource_drift["build_world_changes_pending"]
                        ),
                        "failure_observed": failed["failed_count"] == 1,
                        "test_compile_failure_observed": (
                            test_compile_failed["status"] == "compile_failed"
                        ),
                        "stale_output_rejected": (
                            stale_output_rejected["error_code"]
                            == "FAST_TEST_COMPILE_STATE_INVALID"
                        ),
                        "test_source_edit_observed": (
                            test_edit_failed["failed_count"] == 1
                        ),
                        "incremental_compile_ms": recovered.get("compile_ms"),
                        "recovery_passed": recovered["passed"],
                        "runtime_unchanged": recovered["runtime_unchanged"],
                        "mcp_passed": mcp_result["passed"],
                        "mcp_cancelled": mcp_result["cancelled"],
                        "mcp_assertion_failure_is_error": (
                            mcp_result["assertion_failure_is_error"]
                        ),
                        "junit5_passed": junit5_result["passed"],
                        "junit5_container_failure_observed": (
                            junit5_container_failed["passed"] is False
                        ),
                        "mixed_junit_passed": mixed_junit["passed"],
                        "surefire_config_rejected": (
                            surefire_rejected["error_code"]
                            == "FAST_TEST_SUREFIRE_CONFIGURATION_UNSUPPORTED"
                        ),
                        "system_exit_isolated": (
                            exited["error_code"] == "TEST_RUNNER_FAILED"
                        ),
                        "non_daemon_thread_terminated": (
                            non_daemon_thread["passed"]
                        ),
                        "cancelled": cancelled_result["status"] == "cancelled",
                        "post_isolation_passed": post_isolation["passed"],
                        "log_retention_bounded": log_retention_bounded,
                        "stdout_protocol_isolated": protocol_isolated["passed"],
                        "timeout_observed": timed_out["error_code"] == "TEST_TIMEOUT",
                    },
                    separators=(",", ":"),
                )
            )
        finally:
            manager.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
