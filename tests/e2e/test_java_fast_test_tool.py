"""Discover and exercise the standalone Test tool through real stdio MCP."""

import json
import os
import shutil
import sys
from pathlib import Path

import anyio
import pytest
from java_support import REPOSITORY_ROOT, require_real_mcp_java_e2e, reserve_local_port
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
                    if not reopened:
                        source.write_text("this is not valid Java")
                        cold_error, cold_failed = await run()
                        assert cold_error.isError, cold_failed
                        assert cold_failed["error_code"] == "JDT_TEST_FULL_COMPILE_FAILED", cold_failed
                        assert cold_failed["diagnostics"] and cold_failed["error_count"] > 0
                        assert "next_action" not in cold_failed
                        assert "runner_ms" not in cold_failed
                        assert "diagnostics" not in (await status())["fast_test"]
                        source.write_text(original)
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
                    assert failed["diagnostics"] and failed["error_count"] > 0
                    assert any(d["line"] > 0 and d["resource"] for d in failed["diagnostics"])
                    assert "next_action" not in failed
                    assert "compiled_source_units" not in failed
                    assert "diagnostics" not in (await status())["fast_test"]
                    detail = await session.call_tool("java_fast_test", {
                        "action": "result", "test_run_id": failed["test_run_id"],
                    })
                    assert detail.isError and detail.structuredContent["diagnostics"] == failed["diagnostics"]
                    repeated_error, repeated = await run()
                    assert repeated_error.isError and repeated["diagnostics"], repeated
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
                    assert "failed_tests" not in failed
                    detail = await session.call_tool("java_fast_test", {
                        "action": "result", "test_run_id": failed["test_run_id"],
                    })
                    assert not detail.isError
                    assert detail.structuredContent["failed_tests"]
                    source.write_text(original)
                    _, recovered = await run()
                    assert recovered["passed"], recovered
                    assert (await status())["process_state"] == "absent"

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_status_stays_small_with_281_sources_and_26_compile_errors(tmp_path):
    require_real_mcp_java_e2e()
    java = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not java:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    port = reserve_local_port()
    project = make_test_project(tmp_path, http_port=port)
    cases = project / "src/test/java/example/compatibility/workflow/validation"
    cases.mkdir(parents=True)

    def write_cases(changed=False):
        for i in range(280):
            value = "missingValue" if changed and i < 26 else str(2 if changed else 1)
            (cases / f"Case{i:03}.java").write_text(
                f"package example.compatibility.workflow.validation; public class Case{i:03} "
                f"{{ public int value() {{ return {value}; }} }}", encoding="utf-8",
            )

    write_cases()
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "jolink_runtime.transport.stdio"],
        cwd=REPOSITORY_ROOT, env={
            **os.environ, "JAVA_HOME": java,
            "PATH": str(Path(java) / "bin") + os.pathsep + os.environ["PATH"],
            "MAVEN_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache"),
        },
    )

    async def scenario():
        with (tmp_path / "mcp-status-size.log").open("w+") as log:
            async with (
                stdio_client(parameters, errlog=log) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()

                async def status():
                    return await session.call_tool("java_status", {"action": "status"})

                async def run():
                    reply = await session.call_tool("java_fast_test", {
                        "project_path": str(project), "tests": ["example.ReadyTest"], "timeout": 0,
                    })
                    original_id = reply.structuredContent["test_run_id"]
                    # result is also an immediate observation of an in-flight run.
                    observation = await session.call_tool("java_fast_test", {
                        "action": "result", "test_run_id": original_id,
                    })
                    assert observation.structuredContent["test_run_id"] == original_id
                    payload = observation.structuredContent
                    with anyio.fail_after(180):
                        while payload["status"] in {"starting", "bootstrapping", "compiling", "running"}:
                            await anyio.sleep(0.2)
                            payload = (await status()).structuredContent["fast_test"]
                    assert payload["test_run_id"] == original_id
                    return payload

                launched = await session.call_tool("java_application", {
                    "action": "launch", "project_path": str(project), "launch_name": "Preparation",
                    "ready_port": port, "jdwp_port": reserve_local_port(),
                })
                assert not launched.isError, launched
                try:
                    with anyio.fail_after(180):
                        while True:
                            state = (await status()).structuredContent
                            assert state.get("launch_phase") != "failed", state
                            if state.get("startup_state") == "ready":
                                break
                            await anyio.sleep(0.2)
                    pid = state["pid"]
                    baseline = await run()
                    assert baseline["passed"] is True, baseline
                    write_cases(True)
                    ready_test = project / "src/test/java/example/ReadyTest.java"
                    ready_test.write_text(ready_test.read_text() + "\n// recompile this test too\n")
                    failed = await run()
                    assert failed["status"] == "compile_failed", failed
                    assert failed["error_count"] == 26
                    assert failed["compiled_source_count"] == 281
                    for _ in range(2):
                        response = await status()
                        state = response.structuredContent
                        summary = state["fast_test"]
                        assert state["startup_state"] == "ready" and state["pid"] == pid
                        assert not {"diagnostics", "failed_tests", "compiled_source_units"} & summary.keys()
                        assert len(json.dumps(summary)) < 2000
                        hint = summary["next_action"]
                        detail = await session.call_tool(hint["tool"], hint["arguments"])
                        result = detail.structuredContent
                        assert detail.isError and result["error_count"] == 26
                        assert len(result["diagnostics"]) == 26
                        assert all(d["line"] > 0 and "missingValue" in d["message"] for d in result["diagnostics"])
                        assert "compiled_source_units" not in result
                        assert result["total_ms"] == summary["total_ms"]  # read did not rerun
                        assert json.loads(detail.content[0].text) == result
                    print(json.dumps({
                        "status_chars": len(response.content[0].text),
                        "fast_test_summary_chars": len(json.dumps(summary, separators=(",", ":"))),
                        "detail_chars": len(detail.content[0].text),
                        "errors": result["error_count"], "compiled_sources": result["compiled_source_count"],
                    }))
                    write_cases(False)
                    recovered = await run()
                    assert recovered["passed"] is True, recovered
                    missing = await session.call_tool("java_fast_test", {
                        "action": "result", "test_run_id": failed["test_run_id"],
                    })
                    assert missing.structuredContent["error_code"] == "TEST_RUN_NOT_FOUND"
                finally:
                    stopped = await session.call_tool("java_application", {"action": "stop"})
                    assert not stopped.isError

    anyio.run(scenario)
