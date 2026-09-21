"""Real MCP: small polls, explicit details/logs, unchanged application behavior."""

import http.client
import json
import os
import sys
import time
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from java_support import REPOSITORY_ROOT, compile_java, require_real_mcp_java_e2e, reserve_local_port
from test_mcp_runtime_preparation import make_test_project
from jolink_runtime.launch.startup_timing import StartupTimings


@pytest.mark.mcp_java_e2e
def test_status_details_build_logs_and_reload_failures(tmp_path):
    require_real_mcp_java_e2e()
    java = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not java:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    port = reserve_local_port()
    project = make_test_project(tmp_path, http_port=port)
    app = project / "src/main/java/example/App.java"
    app.write_text(app.read_text().replace('"42".getBytes', 'Reply.value().getBytes'))
    source = app.with_name("Reply.java")
    original = 'package example; public class Reply { public static String value(){return "42";} }'
    source.write_text(original)
    cache = tmp_path / "cache"
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "jolink_runtime.transport.stdio"], cwd=REPOSITORY_ROOT,
        env={**os.environ, "JAVA_HOME": java,
             "PATH": str(Path(java) / "bin") + os.pathsep + os.environ["PATH"],
             "MAVEN_ARGS": "--offline", "XDG_CACHE_HOME": str(cache),
             "LOCALAPPDATA": str(cache), "JOLINK_LOG_LEVEL": "INFO"},
    )

    def response():
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("GET", "/")
            return connection.getresponse().read().decode()
        finally:
            connection.close()

    async def scenario():
        with (tmp_path / "stderr.log").open("w+") as stderr:
            async with (
                stdio_client(parameters, errlog=stderr) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()

                async def observe(action="status", **args):
                    reply = await session.call_tool("java_status", {"action": action, **args})
                    assert not reply.isError, reply
                    assert "server_diagnostics" not in reply.structuredContent
                    return reply.structuredContent

                async def poll(predicate):
                    with anyio.fail_after(120):
                        while True:
                            state = await observe()
                            if predicate(state):
                                return state
                            await anyio.sleep(.1)

                launch = {"action": "launch", "project_path": str(project), "launch_name": "Preparation",
                          "ready_port": port, "jdwp_port": reserve_local_port()}
                started = await session.call_tool("java_application", launch)
                assert not started.isError, started
                assert started.structuredContent["previous_startup_ms"] is None
                try:
                    state = await poll(lambda s: s.get("startup_state") == "ready" and s.get("launch_phase") == "runtime_active")
                    pid = state["pid"]
                    assert await anyio.to_thread.run_sync(response) == "42"
                    build = await observe("logs", source="build", tail=5)
                    log_path = Path(build["log_file"])
                    with log_path.open("a", encoding="utf-8") as stream:
                        stream.write(("BUILD_SENTINEL " * 70 + "\n") * 30)
                        stream.write("Authorization=Bearer secret-value\nBUILD_END\n")
                    for _ in range(2):
                        state = await observe()
                        assert state["startup_state"] == "ready" and state["pid"] == pid
                        assert "log_tail" not in state.get("build", {})
                        assert "BUILD_SENTINEL" not in json.dumps(state)
                        for key in ("build_log_tail", "product_timing_ms", "jdt_worker", "overlay_sources", "probe_cache_reused"):
                            assert key not in state
                        assert len(json.dumps(state)) < 2500
                    build = await observe("logs", source="build", tail=3)
                    assert "BUILD_SENTINEL" in json.dumps(build)
                    assert "secret-value" not in json.dumps(build)
                    assert "<redacted>" in json.dumps(build)
                    detail = await observe(details=True)
                    assert "jdt_worker" in detail and "product_timing_ms" in detail
                    assert "log_tail" not in detail.get("build", {})
                    first_startup_ms = detail["startup_elapsed_ms"]
                    assert first_startup_ms > 0

                    for replacement, expected, applied in ((original.replace('"42"', '"43"'), "43", True),
                                                          (original.replace('"42"', 'missingValue'), "43", False),
                                                          (original, "42", True)):
                        source.write_text(replacement)
                        submitted = await session.call_tool("java_application", {"action": "restart", "timeout": 0})
                        assert not submitted.isError, submitted
                        assert submitted.structuredContent["previous_startup_ms"] == first_startup_ms
                        rid = submitted.structuredContent["reload_id"]
                        state = await poll(lambda s: (s.get("last_reload") or {}).get("reload_id") == rid)
                        summary = state["last_reload"]
                        assert summary["applied"] is applied
                        assert state["pid"] == pid
                        assert "diagnostics" not in summary and "redefined_classes" not in summary
                        hint = summary["next_action"]
                        detailed = (await session.call_tool(hint["tool"], hint["arguments"])).structuredContent
                        full = detailed["last_reload"]
                        assert full["reload_id"] == rid and full["applied"] is applied
                        if applied:
                            assert full["redefined_classes"] == ["example.Reply"]
                        else:
                            assert summary["error_code"] == "JDT_COMPILE_FAILED"
                            assert full["diagnostics"]
                        assert await anyio.to_thread.run_sync(response) == expected

                    restarted = await session.call_tool("java_application", {"action": "restart", "hotswap": False})
                    assert not restarted.isError, restarted
                    assert restarted.structuredContent["previous_startup_ms"] == first_startup_ms
                    state = await poll(lambda s: s.get("startup_state") == "ready" and s["pid"] != pid)
                    previous_ms = (await observe(details=True))["startup_elapsed_ms"]
                    assert previous_ms > 0
                    await session.call_tool("java_application", {"action": "stop"})
                    relaunched = await session.call_tool("java_application", {**launch, "timeout": 0})
                    assert not relaunched.isError, relaunched
                    assert relaunched.structuredContent["previous_startup_ms"] == previous_ms
                    state = await poll(lambda s: s.get("startup_state") == "ready" and s.get("launch_phase") == "runtime_active")
                    assert await anyio.to_thread.run_sync(response) == "42"
                    previous_ms = (await observe(details=True))["startup_elapsed_ms"]
                    assert (cache / "jolink-runtime/logs/mcp.log").stat().st_size > 0
                    print(json.dumps({"status_chars": len(json.dumps(state)),
                                      "reload_summary_chars": len(json.dumps(summary)),
                                      "build_logs_on_demand": True, "mcp_log_still_written": True,
                                      "previous_startup_ms": previous_ms}))
                finally:
                    await session.call_tool("java_application", {"action": "stop"})

                # Failed initial compilation still exposes its error and logs on demand.
                source.write_text(original.replace('"42"', 'missingValue'))
                rejected = await session.call_tool("java_application", launch)
                assert rejected.structuredContent["previous_startup_ms"] == previous_ms
                failed = await poll(lambda s: s.get("launch_phase") == "failed")
                assert "diagnostics" not in failed["launch_error"]
                hint = failed["launch_error"]["next_action"]
                details = (await session.call_tool(hint["tool"], hint["arguments"])).structuredContent
                assert details["launch_error"]["error_code"] == failed["launch_error"]["error_code"]
                assert "log_tail" not in failed.get("build", {})
                # This warm launch reuses the model, so no new Maven build.log
                # exists. Missing logs must not masquerade as a successful read.
                missing_log = await session.call_tool("java_status", {"action": "logs", "source": "build"})
                assert missing_log.isError
                assert missing_log.structuredContent["error_code"] == "BUILD_LOG_UNAVAILABLE"

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_direct_launch_previous_duration_is_not_the_process_uptime(tmp_path):
    require_real_mcp_java_e2e()
    compile_java(tmp_path, "StartupTiming", """public class StartupTiming {
 public static void main(String[] args) throws Exception { while (true) Thread.sleep(1000); }
}""")
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "jolink_runtime.transport.stdio"],
        cwd=REPOSITORY_ROOT, env=os.environ.copy(),
    )
    async def scenario():
        with (tmp_path / "timing.log").open("w+") as log:
            async with (
                stdio_client(parameters, errlog=log) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                launch = {"action": "launch", "classpath": str(tmp_path), "main_class": "StartupTiming",
                          "jdwp_port": reserve_local_port(), "timeout": 0}
                started = time.monotonic()
                first = await session.call_tool("java_application", launch)
                roundtrip_ms = (time.monotonic() - started) * 1000
                assert not first.isError, first
                assert first.structuredContent["previous_startup_ms"] is None
                try:
                    await anyio.sleep(1)
                    # stop removes the process; the observed duration survives.
                    await session.call_tool("java_application", {"action": "stop"})
                    second = await session.call_tool("java_application", launch)
                    assert not second.isError, second
                    assert 0 < second.structuredContent["previous_startup_ms"] <= roundtrip_ms
                    restarted = await session.call_tool("java_application", {"action": "restart"})
                    assert not restarted.isError, restarted
                    assert restarted.structuredContent["previous_startup_ms"] > 0
                finally:
                    await session.call_tool("java_application", {"action": "stop"})
    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("mode", ["project", "direct"])
def test_previous_startup_survives_new_mcp_process_without_a_final_status_call(tmp_path, mode):
    require_real_mcp_java_e2e()
    java = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not java:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    port = reserve_local_port()
    if mode == "project":
        project = make_test_project(tmp_path, http_port=port)
        launch = {"action": "launch", "project_path": str(project), "launch_name": "Preparation"}
    else:
        compile_java(tmp_path, "PersistentTiming", """public class PersistentTiming {
 public static void main(String[] args) throws Exception {
  com.sun.net.httpserver.HttpServer s = com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1", PORT),0);
  s.start();
 }
}""".replace("PORT", str(port)))
        launch = {"action": "launch", "classpath": str(tmp_path), "main_class": "PersistentTiming"}
    launch.update(ready_port=port, jdwp_port=reserve_local_port(), timeout=0)
    cache = tmp_path / "cache"
    store = StartupTimings(root=cache / "jolink-runtime/startup-timings")
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "jolink_runtime.transport.stdio"], cwd=REPOSITORY_ROOT,
        env={**os.environ, "JAVA_HOME": java,
             "PATH": str(Path(java) / "bin") + os.pathsep + os.environ["PATH"],
             "XDG_CACHE_HOME": str(cache), "LOCALAPPDATA": str(cache), "MAVEN_ARGS": "--offline"},
    )

    async def scenario():
        previous = None
        for generation in range(3):
            if generation == 2:
                if mode == "project":
                    (project / "src/main/java/example/App.java").write_text("not valid Java")
                else:
                    compile_java(tmp_path, "PersistentTiming", """public class PersistentTiming {
 public static void main(String[] args) { throw new RuntimeException("expected startup failure"); }
}""")
            with (tmp_path / f"mcp-{generation}.log").open("w+") as log:
                async with (
                    stdio_client(parameters, errlog=log) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    result = await session.call_tool("java_application", {
                        **launch, "timeout": 30 if generation == 2 else 0,
                    })
                    assert result.structuredContent["previous_startup_ms"] == previous
                    if generation == 2:
                        assert result.isError, result
                        assert store.previous(None, launch) == previous
                        continue
                    assert not result.isError, result
                    if generation == 0:
                        # Only read the local file: no status or stop call may
                        # be necessary for a successful startup to be saved.
                        with anyio.fail_after(120):
                            while store.previous(None, launch) is None:
                                await anyio.sleep(.1)
                        previous = store.previous(None, launch)
                        assert previous > 0
                    else:
                        with anyio.fail_after(120):
                            while True:
                                state = (await session.call_tool("java_status", {"action": "status"})).structuredContent
                                if state.get("startup_state") == "ready":
                                    previous = state["startup_elapsed_ms"]
                                    break
                                await anyio.sleep(.1)
                        assert store.previous(None, launch) == previous
                    paths = list(store.root.glob("*.json"))
                    assert len(paths) == 1
                    saved = paths[0].read_bytes()
            # Shutdown must neither be required for persistence nor erase it.
            assert paths[0].read_bytes() == saved
        print(json.dumps({"mode": mode, "mcp_processes": 3, "persisted_previous_startup_ms": previous}))

    anyio.run(scenario)
