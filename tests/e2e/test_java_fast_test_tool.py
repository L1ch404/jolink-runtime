"""Discover and exercise the standalone Test tool through real stdio MCP."""

import os
import shutil
import sys
from pathlib import Path

import anyio
import pytest
from java_support import REPOSITORY_ROOT, require_real_mcp_java_e2e
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_mcp_runtime_preparation import make_test_project


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("framework", ["junit4", "testng"])
def test_fast_test_without_idea_or_application_and_cached_reopen(tmp_path, framework):
    require_real_mcp_java_e2e()
    java = os.environ.get(
        "JOLINK_TEST_JAVA8_HOME"
        if framework == "junit4"
        else "JOLINK_FAST_TEST_JAVA11_HOME"
    )
    if not java:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME and JOLINK_FAST_TEST_JAVA11_HOME")
    project = make_test_project(tmp_path)
    # This is a disposable fixture, deliberately without any IDEA launch config.
    shutil.rmtree(project / ".idea")
    source = project / "src/test/java/example/ReadyTest.java"
    annotation, assertions = "org.junit.Test", "org.junit.Assert"
    if framework == "testng":
        pom = project / "pom.xml"
        pom.write_text(
            pom.read_text()
            .replace(
                "<groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version>",
                "<groupId>org.testng</groupId><artifactId>testng</artifactId><version>7.11.0</version>",
            )
            .replace("source>8<", "source>11<")
            .replace("target>8<", "target>11<")
        )
        annotation, assertions = "org.testng.annotations.Test", "org.testng.Assert"
        source.write_text(
            source.read_text()
            .replace("org.junit.Test", annotation)
            .replace("org.junit.Assert", assertions)
        )
    original = source.read_text()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jolink_runtime.transport.stdio"],
        cwd=REPOSITORY_ROOT,
        env={
            **os.environ,
            "JAVA_HOME": java,
            "PATH": str(Path(java) / "bin") + os.pathsep + os.environ["PATH"],
            "MAVEN_ARGS": "--offline",
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        },
    )

    async def scenario():
        for reopened in (False, True):
            with (tmp_path / f"mcp-{reopened}.log").open("w+") as log:
                async with (
                    stdio_client(parameters, errlog=log) as (r, w),
                    ClientSession(r, w) as session,
                ):
                    await session.initialize()
                    tools = {
                        tool.name: tool for tool in (await session.list_tools()).tools
                    }
                    assert "java_fast_test" in tools
                    assert (
                        "No application launch" in tools["java_fast_test"].description
                    )
                    assert (
                        "test"
                        not in tools["java_application"].inputSchema["properties"][
                            "action"
                        ]["enum"]
                    )
                    assert (
                        "tests" not in tools["java_debugger"].inputSchema["properties"]
                    )

                    async def status():
                        return dict(
                            (
                                await session.call_tool(
                                    "java_status", {"action": "status"}
                                )
                            ).structuredContent
                        )

                    async def run(**extra):
                        args = {
                            "project_path": str(project),
                            "tests": ["example.ReadyTest#ready"],
                            **extra,
                        }
                        result = await session.call_tool(
                            "java_fast_test", args
                        )  # no action
                        payload = dict(result.structuredContent)
                        with anyio.fail_after(120):
                            while payload.get("status") in {
                                "starting",
                                "bootstrapping",
                                "compiling",
                                "running",
                            }:
                                await anyio.sleep(0.1)
                                payload = (await status())["fast_test"]
                        return result, payload

                    assert (await status())["process_state"] == "absent"
                    _, passed = await run()
                    assert passed["passed"] and passed["tests"] == 1, passed
                    assert passed["framework"] == framework, passed
                    assert (await status())["process_state"] == "absent"
                    if reopened:
                        assert passed["compiled_source_count"] == 0, passed
                        continue
                    _, warm = await run(timeout=1000)
                    assert (
                        warm["status"] == "completed"
                        and warm["compiled_source_count"] == 0
                    ), warm
                    source.write_text("this is not valid Java")
                    error, failed = await run()
                    assert (
                        error.isError
                        and failed["error_code"] == "JDT_TEST_COMPILE_FAILED"
                    ), failed
                    source.write_text(
                        'package example; public class ReadyTest { @ANNOTATION public void ready(){ASSERTIONS.fail("expected failure");} }'.replace(
                            "ANNOTATION", annotation
                        ).replace("ASSERTIONS", assertions)
                    )
                    assertion, failed = await run()
                    assert (
                        not assertion.isError
                        and failed["status"] == "completed"
                        and not failed["passed"]
                    ), failed
                    source.write_text(original)
                    _, recovered = await run()
                    assert recovered["passed"], recovered
                    assert (await status())["process_state"] == "absent"

    anyio.run(scenario)
