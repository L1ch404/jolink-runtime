"""Real MCP -> persistent JDT -> HotSwap or new JVM, verified by HTTP."""

import http.client
import os
import time
from pathlib import Path

import anyio
import pytest
import psutil
from java_support import open_mcp_session, require_real_mcp_java_e2e, reserve_local_port
from test_mcp_runtime_preparation import make_test_project


@pytest.mark.mcp_java_e2e
def test_restart_compiles_current_sources_and_chooses_apply_method(tmp_path):
    require_real_mcp_java_e2e()
    java = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not java:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    port = reserve_local_port()
    project = make_test_project(tmp_path, port)
    app = project / "src/main/java/example/App.java"
    app.write_text(app.read_text().replace('"42".getBytes', 'Reply.value().getBytes'))
    source = app.with_name("Reply.java")

    def edit(value, field=""):
        source.write_text('package example; public class Reply { ' + field
                          + ' public static String value(){return "' + value + '";} }')

    edit("42")
    env = {**os.environ, "JAVA_HOME": java,
           "PATH": str(Path(java) / "bin") + os.pathsep + os.environ["PATH"],
           "MAVEN_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache")}

    def http_value():
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            assert response.status == 200
            return response.read().decode()
        finally:
            connection.close()

    async def scenario():
        with (tmp_path / "mcp.log").open("w+") as log:
            async with open_mcp_session(log, environment=env) as session:
                async def call(action, **args):
                    tool = "java_status" if action == "status" else "java_application"
                    return dict((await session.call_tool(tool, {"action": action, **args})).structuredContent)

                async def finish(value):
                    if value.get("applied") is not None or not value.get("ok"):
                        return value
                    with anyio.fail_after(90):
                        while True:
                            state = await call("status")
                            last = state.get("last_reload") or {}
                            if last.get("reload_id") == value.get("reload_id") and last:
                                return last
                            await anyio.sleep(.05)

                async def restarted(expected, method, *, previous_pid, **args):
                    result = await finish(await call("restart", **args))
                    assert result["ok"] and result["applied"], result
                    assert result["apply_method"] == method, result
                    state = await call("status")
                    assert state["startup_state"] == "ready", state
                    assert (state["pid"] == previous_pid) is (method == "hotswap"), result
                    assert await anyio.to_thread.run_sync(http_value) == expected
                    return state["pid"], result

                launched = await call("launch", project_path=str(project), launch_name="Preparation",
                                      jdwp_port=reserve_local_port(), ready_port=port)
                assert launched["ok"] and launched["startup_state"] == "ready", launched
                pid = launched["pid"]
                assert await anyio.to_thread.run_sync(http_value) == "42"
                edit("43")
                pid, hot = await restarted("43", "hotswap", previous_pid=pid)
                assert hot["compiled_source_count"] == 1, hot

                source.write_text("package example; public class Reply { broken }")
                failed = await finish(await call("restart"))
                assert failed["error_code"] == "JDT_COMPILE_FAILED", failed
                assert (await call("status"))["pid"] == pid
                assert await anyio.to_thread.run_sync(http_value) == "43"
                # No edits after a failed compile must not turn into success.
                repeated = await finish(await call("restart", hotswap=False))
                assert repeated["error_code"] == "JDT_COMPILE_FAILED", repeated
                edit("44")
                pid, _ = await restarted("44", "hotswap", previous_pid=pid)

                edit("45", "public static int extra;")
                pid, structural = await restarted("45", "restart", previous_pid=pid)
                assert structural["restart_reason"] == "HOT_SWAP_REJECTED", structural
                assert structural["compiled_source_count"] >= 1, structural
                assert structural["build_kind"] == "INCREMENTAL", structural
                edit("46", "public static int extra;")
                pid, _ = await restarted("46", "restart", previous_pid=pid, hotswap=False)
                pid, unchanged = await restarted("46", "restart", previous_pid=pid)
                assert unchanged["compiled_source_count"] == 0, unchanged

                extra = source.with_name("Extra.java")
                extra.write_text('package example; public class Extra {static String value(){return "47";}}')
                source.write_text('package example; public class Reply {public static int extra; public static String value(){return Extra.value();}}')
                pid, added = await restarted("47", "restart", previous_pid=pid)
                assert added["restart_reason"] == "CLASS_NOT_LOADED", added
                extra.unlink()
                edit("48", "public static int extra;")
                pid, deleted = await restarted("48", "restart", previous_pid=pid)
                assert deleted["restart_reason"] == "NON_HOTSWAPPABLE_OUTPUT_DELTA", deleted

                edit("49", "public static int extra;")
                pid, _ = await restarted("49", "hotswap", previous_pid=pid, timeout=0)
                passed = dict((await session.call_tool("java_fast_test", {
                    "project_path": str(project), "tests": ["example.ReadyTest"],
                })).structuredContent)
                assert passed["passed"], passed
                assert (await call("status"))["pid"] == pid
                # Restart also compiles edits when the owned application exited.
                process = psutil.Process(pid)
                process.terminate()
                # The MCP owns/reaps this process; observe it through that owner.
                with anyio.fail_after(10):
                    while (await call("status"))["process_state"] == "running":
                        await anyio.sleep(.05)
                edit("50", "public static int extra;")
                recovered = await call("restart")
                if recovered.get("reload_id"):
                    recovered = await finish(recovered)
                assert recovered["ok"] and recovered["apply_method"] == "restart", recovered
                assert await anyio.to_thread.run_sync(http_value) == "50"
                assert (await call("stop"))["ok"]

        # A fresh MCP process uses the saved output, including prior HotSwap.
        with (tmp_path / "reopen.log").open("w+") as log:
            async with open_mcp_session(log, environment=env) as session:
                launched = dict((await session.call_tool("java_application", {
                    "action": "launch", "project_path": str(project), "launch_name": "Preparation",
                    "jdwp_port": reserve_local_port(), "ready_port": port,
                })).structuredContent)
                assert launched["ok"] and launched["startup_state"] == "ready", launched
                assert launched["jdt_bootstrap_reused"] is True, launched
                assert launched["jdt_bootstrap_build_kind"] is None, launched
                assert await anyio.to_thread.run_sync(http_value) == "50"
                await session.call_tool("java_application", {"action": "stop"})

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_restart_reply_budget_and_stop_during_real_readiness(tmp_path):
    require_real_mcp_java_e2e()
    java = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not java:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    port = reserve_local_port()
    project = make_test_project(tmp_path, port)
    source = project / "src/main/java/example/App.java"
    original = source.read_text()
    env = {**os.environ, "JAVA_HOME": java,
           "PATH": str(Path(java) / "bin") + os.pathsep + os.environ["PATH"],
           "MAVEN_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache")}

    async def scenario():
        with (tmp_path / "mcp.log").open("w+") as log:
            async with open_mcp_session(log, environment=env) as session:
                async def call(action, **args):
                    tool = "java_status" if action == "status" else "java_application"
                    return dict((await session.call_tool(tool, {"action": action, **args})).structuredContent)

                launched = await call("launch", project_path=str(project), launch_name="Preparation",
                                      jdwp_port=reserve_local_port(), ready_port=port)
                assert launched["startup_state"] == "ready", launched
                source.write_text(original.replace(" com.sun.net", " Thread.sleep(32000); com.sun.net", 1))
                before = time.monotonic()
                pending = await call("restart", hotswap=False, timeout=600)
                assert 29 <= time.monotonic() - before < 36, pending
                assert pending["ok"] and pending["applied"] is None, pending
                assert pending["stage"] == "restarting", pending
                assert "still running" in pending["suggested_next_step"]
                with anyio.fail_after(15):
                    while True:
                        state = await call("status")
                        terminal = state.get("last_reload") or {}
                        if terminal.get("reload_id") == pending["reload_id"]:
                            break
                        await anyio.sleep(.05)
                assert terminal["applied"] and terminal["apply_method"] == "restart", terminal
                assert state["startup_state"] == "ready" and state["pid"] != launched["pid"]

                # No source edit: real restart, no compile, remains stoppable while waiting.
                accepted = await call("restart", hotswap=False, timeout=0)
                assert accepted["status"] == "restart_started", accepted
                with anyio.fail_after(10):
                    while (await call("status")).get("launch_phase") != "waiting_readiness":
                        await anyio.sleep(.05)
                with anyio.fail_after(10):
                    stopped = await call("stop")
                assert stopped["ok"], stopped
                state = await call("status")
                assert state["process_state"] == "absent", state
                source.write_text(original)
                # Restart after stop takes the persistent launch path, not a dead session.
                active = await call("restart")
                assert active["ok"] and active["startup_state"] == "ready", active
                await call("stop")

    anyio.run(scenario)
