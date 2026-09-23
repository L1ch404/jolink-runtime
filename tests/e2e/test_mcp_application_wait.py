"""Real MCP launch/Test: wait, background continuation, errors and controls."""

import http.client
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest
from java_support import REPOSITORY_ROOT, require_real_mcp_java_e2e, reserve_local_port
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_mcp_runtime_preparation import make_test_project


@pytest.mark.mcp_java_e2e
def test_launch_and_test_wait_and_continue_on_real_mcp(tmp_path):
    require_real_mcp_java_e2e()
    java = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not java:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    port = reserve_local_port()
    project = make_test_project(tmp_path, port)
    source = project / "src/test/java/example/ReadyTest.java"
    source.write_text("""package example;
public class ReadyTest {
 @org.junit.Test public void ready(){org.junit.Assert.assertEquals(42,40+2);}
 @org.junit.Test public void slow() throws Exception {Thread.sleep(32000);}
 @org.junit.Test public void broken(){org.junit.Assert.fail("intentional assertion");}
}""")
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
        with (tmp_path / "mcp.log").open("w+") as log:
            async with (
                stdio_client(parameters, errlog=log) as (r, w),
                ClientSession(r, w) as session,
            ):
                await session.initialize()

                async def call(args):
                    with anyio.fail_after(40):
                        return dict(
                            (
                                await session.call_tool(
                                    "java_fast_test" if "tests" in args or args.get("action") == "cancel" else "java_application", args
                                )
                            ).structuredContent
                        )

                async def status():
                    return dict(
                        (
                            await session.call_tool("java_status", {"action": "status"})
                        ).structuredContent
                    )

                launch = {
                    "action": "launch",
                    "project_path": str(project),
                    "launch_name": "Preparation",
                    "ready_port": port,
                    "jdwp_port": reserve_local_port(),
                }
                ready = await call(launch)
                assert ready["ok"] and ready["launch_phase"] == "runtime_active", ready
                assert ready["startup_state"] == "ready", ready
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    connection.request("GET", "/")
                    response = connection.getresponse()
                    assert response.status == 200 and response.read() == b"42"
                finally:
                    connection.close()
                assert "still running" not in ready.get("suggested_next_step", "")

                test = {
                    "project_path": str(project),
                    "tests": ["example.ReadyTest#ready"],
                }
                passed = await call(test)
                assert (
                    passed["status"] == "completed" and passed["passed_count"] == 1
                ), passed
                assert (await status())["pid"] == ready["pid"]
                failed = await call({**test, "tests": ["example.ReadyTest#broken"]})
                assert failed["status"] == "completed" and not failed["passed"], failed

                # Larger timeout is accepted, but the same Runner survives the
                # 30-second reply budget and finishes its 32-second test.
                began = time.monotonic()
                pending = await call(
                    {**test, "tests": ["example.ReadyTest#slow"], "timeout": 600}
                )
                elapsed = time.monotonic() - began
                assert 29 <= elapsed < 35, (elapsed, pending)
                assert pending["status"] == "running", pending
                assert "sleep" in pending["suggested_next_step"]
                with anyio.fail_after(15):
                    while True:
                        final = (await status())["fast_test"]
                        if final["status"] == "completed":
                            break
                        await anyio.sleep(0.2)
                assert (
                    final["test_run_id"] == pending["test_run_id"] and final["passed"]
                ), final

                # Explicit cancellation can enter while the original Test call
                # is synchronously awaiting the very same run.
                responses = []

                async def slow_call():
                    responses.append(
                        await call({**test, "tests": ["example.ReadyTest#slow"]})
                    )

                async with anyio.create_task_group() as group:
                    group.start_soon(slow_call)
                    with anyio.fail_after(5):
                        while True:
                            observed = (await status())["fast_test"]
                            if (
                                observed["test_run_id"] != final["test_run_id"]
                                and observed["status"] == "running"
                            ):
                                break
                            await anyio.sleep(0.1)
                        await call(
                            {
                                "action": "cancel",
                                "test_run_id": observed["test_run_id"],
                            }
                        )
                assert responses[0]["test_run_id"] == observed["test_run_id"]
                assert responses[0]["status"] == "cancelled", responses

                immediate = await call({**test, "timeout": 0})
                assert immediate["test_run_id"] != observed["test_run_id"]
                with anyio.fail_after(10):
                    while (await status())["fast_test"]["status"] != "completed":
                        await anyio.sleep(0.1)
                await call({"action": "stop"})

                # A project-worker error must be returned as failure, not as a
                # successful status observation that happened to contain it.
                error = await call({**launch, "launch_name": "MissingLaunch"})
                assert error["ok"] is False and error["launch_phase"] == "failed", error
                assert error["error_code"] and "still running" not in error.get(
                    "suggested_next_step", ""
                )

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_direct_launch_readiness_and_stop_while_waiting(tmp_path):
    require_real_mcp_java_e2e()
    java = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not java:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    source = tmp_path / "Delayed.java"
    source.write_text("""public class Delayed {
 public static void main(String[] args) throws Exception {
  Thread.sleep(Long.parseLong(args[1]));
  java.net.ServerSocket socket=new java.net.ServerSocket(Integer.parseInt(args[0]));
  while(true){socket.accept().close();}
 }
}""")
    subprocess.run(
        [str(Path(java) / "bin/javac"), str(source)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jolink_runtime.transport.stdio"],
        cwd=REPOSITORY_ROOT,
        env={
            **os.environ,
            "JAVA_HOME": java,
            "PATH": str(Path(java) / "bin") + os.pathsep + os.environ["PATH"],
        },
    )

    async def scenario():
        with (tmp_path / "mcp.log").open("w+") as log:
            async with (
                stdio_client(parameters, errlog=log) as (r, w),
                ClientSession(r, w) as session,
            ):
                await session.initialize()

                async def call(args):
                    return dict(
                        (
                            await session.call_tool("java_application", args)
                        ).structuredContent
                    )

                async def status():
                    return dict(
                        (
                            await session.call_tool("java_status", {"action": "status"})
                        ).structuredContent
                    )

                def launch(delay, timeout=30, verified=True):
                    port = reserve_local_port()
                    return {
                        "action": "launch",
                        "main_class": "Delayed",
                        "classpath": str(tmp_path),
                        "app_args": [str(port), str(delay)],
                        "jdwp_port": reserve_local_port(),
                        "timeout": timeout,
                        **({"ready_port": port} if verified else {}),
                    }

                ready = await call(launch(4500, 1000))
                assert ready["ok"] and ready["startup_state"] == "ready", ready
                assert "next_action" not in ready and "suggested_next_step" not in ready, ready
                assert "startup_wait_timed_out" not in ready, ready
                await call({"action": "stop"})
                pending = await call(launch(5000, 0.1))
                assert (
                    pending["startup_state"] == "starting"
                    and "sleep" in pending["suggested_next_step"]
                ), pending
                with anyio.fail_after(8):
                    while True:
                        observation = await status()
                        if observation["startup_state"] == "ready":
                            break
                        await anyio.sleep(0.1)
                assert observation["pid"] == pending["pid"]
                await call({"action": "stop"})

                results = []

                async def starting():
                    results.append(await call(launch(60000)))

                async with anyio.create_task_group() as group:
                    group.start_soon(starting)
                    with anyio.fail_after(6):
                        while True:
                            observation = await status()
                            if observation.get("process_state") == "running":
                                break
                            await anyio.sleep(0.1)
                        await call({"action": "stop"})
                assert results[0]["ok"] is False, results
                unverified = await call(launch(5000, verified=False))
                assert (
                    unverified["ok"] and unverified["startup_state"] == "unverified"
                ), unverified
                assert "ready_port" in unverified["suggested_next_step"], unverified
                restarted = await call({"action": "restart"})
                assert restarted["ok"] and restarted["startup_state"] == "unverified", restarted
                assert "ready_port" in restarted["suggested_next_step"], restarted
                await call({"action": "stop"})

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_project_launch_and_restart_preserve_unverified_guidance(tmp_path):
    require_real_mcp_java_e2e()
    java = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not java:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    port = reserve_local_port()
    project = make_test_project(tmp_path, port)
    gate = tmp_path / "allow-http-start"
    app = project / "src/main/java/example/App.java"
    app.write_text(app.read_text().replace(
        'com.sun.net.httpserver.HttpServer s=',
        f'while (!java.nio.file.Files.exists(java.nio.file.Paths.get({json.dumps(str(gate))}))) Thread.sleep(50);\n'
        'com.sun.net.httpserver.HttpServer s=',
    ).replace('"42".getBytes', 'Reply.value().getBytes'))
    reply = app.with_name("Reply.java")
    original = ('package example; public class Reply { '
                'public static String value(){return "old";} '
                'public static class Deferred { public static String value(){return "old";} } }')
    reply.write_text(original)
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "jolink_runtime.transport.stdio"],
        cwd=REPOSITORY_ROOT,
        env={**os.environ, "JAVA_HOME": java,
             "PATH": str(Path(java) / "bin") + os.pathsep + os.environ["PATH"],
             "MAVEN_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache")},
    )

    def response():
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/")
            reply = connection.getresponse()
            assert reply.status == 200
            return reply.read().decode()
        finally:
            connection.close()

    async def scenario():
        with (tmp_path / "mcp.log").open("w+") as log:
            async with (stdio_client(parameters, errlog=log) as (r, w), ClientSession(r, w) as session):
                await session.initialize()

                async def call(tool, **args):
                    return dict((await session.call_tool(tool, args)).structuredContent)

                async def check_unverified(result):
                    assert result["ok"] and result["startup_state"] == "unverified", result
                    assert result["process_state"] == "running", result
                    assert "ready_port" in result["suggested_next_step"], result
                    observed = await call("java_status", action="status")
                    assert result["suggested_next_step"] == observed["suggested_next_step"]
                    with pytest.raises(OSError):
                        await anyio.to_thread.run_sync(response)

                async def allow_http(expected):
                    gate.write_text("go")
                    with anyio.fail_after(10):
                        while True:
                            try:
                                value = await anyio.to_thread.run_sync(response)
                                break
                            except OSError:
                                await anyio.sleep(.05)
                    assert value == expected

                try:
                    launched = await call("java_application", action="launch", project_path=str(project),
                                          launch_name="Preparation", jdwp_port=reserve_local_port())
                    await check_unverified(launched)
                    await allow_http("old")
                    gate.unlink()
                    reply.write_text(original.replace('"old"', '"new"'))
                    restarted = await call("java_application", action="restart")
                    assert restarted["restart_reason"] == "CLASS_NOT_LOADED", restarted
                    assert restarted["pid"] != launched["pid"]
                    await check_unverified(restarted)
                    await allow_http("new")
                finally:
                    gate.write_text("go")
                    await call("java_application", action="stop")

    anyio.run(scenario)
