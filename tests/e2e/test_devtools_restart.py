"""Managed JDT output must not trigger a second, automatic Spring restart."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import time
from pathlib import Path

import anyio
import pytest

from java_support import open_mcp_session, require_real_mcp_java_e2e, reserve_local_port


APP = """package example;
import java.util.UUID;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.web.bind.annotation.*;

@SpringBootApplication
@RestController
public class App {
    private static final String INITIALIZATION = UUID.randomUUID().toString();
    public static void main(String[] args) { SpringApplication.run(App.class, args); }
    @GetMapping("/")
    public String state() {
        return Reply.value() + "|" + INITIALIZATION + "|" +
            System.getProperty("spring.devtools.restart.enabled") + "|" +
            System.getProperty("fixture.flag", "missing") + "|" +
            App.class.getClassLoader().getClass().getName();
    }
}
"""


def _reply(value: str) -> str:
    return 'package example; public class Reply { public static String value(){return "' + value + '";} }'


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("boot_version,java_major", [("1.5.4.RELEASE", 8), ("2.5.8", 17)])
def test_devtools_default_failed_compile_recovery_and_restart_overrides(tmp_path, boot_version, java_major):
    require_real_mcp_java_e2e()
    java = os.environ.get(f"JOLINK_TEST_JAVA{java_major}_HOME")
    if not java or not shutil.which("mvn"):
        pytest.skip(f"Maven and JOLINK_TEST_JAVA{java_major}_HOME are required")
    project = tmp_path / "project"
    source = project / "src/main/java/example"
    source.mkdir(parents=True)
    (project / "pom.xml").write_text(f'''<project xmlns="http://maven.apache.org/POM/4.0.0">
<modelVersion>4.0.0</modelVersion>
<parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId>
<version>{boot_version}</version><relativePath/></parent>
<groupId>example</groupId><artifactId>devtools-restart</artifactId><version>1</version>
<properties><java.version>1.8</java.version></properties>
<dependencies>
<dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency>
<dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-devtools</artifactId></dependency>
<dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency>
</dependencies></project>''', encoding="utf-8")
    (source / "App.java").write_text(APP, encoding="utf-8")
    reply_source = source / "Reply.java"
    reply_source.write_text(_reply("v1"), encoding="utf-8")
    test = project / "src/test/java/example/ReplyTest.java"
    test.parent.mkdir(parents=True)
    test.write_text('''package example;
public class ReplyTest {
 @org.junit.Test public void current() {
  org.junit.Assert.assertTrue(Reply.value().startsWith("v"));
 }
}''', encoding="utf-8")
    env = {**os.environ, "JAVA_HOME": java,
           "PATH": str(Path(java) / "bin") + os.pathsep + os.environ.get("PATH", ""),
           "XDG_CACHE_HOME": str(tmp_path / "cache"), "JOLINK_LOG_LEVEL": "INFO"}
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")
    port = reserve_local_port()
    launch = dict(action="launch", project_path=str(project), main_class="example.App",
                  java_home=java, build_java_home=java, ready_port=port,
                  jdwp_port=reserve_local_port(), app_args=[f"--server.port={port}"], timeout=30)
    transcript = tmp_path / "calls.jsonl"

    def http_state():
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            assert response.status == 200, (response.status, body)
            return body.split("|")
        finally:
            connection.close()

    async def call(session, tool, **args):
        result = dict((await session.call_tool(tool, args)).structuredContent)
        with transcript.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"tool": tool, "arguments": args, "result": result}, ensure_ascii=False) + "\n")
        return result

    async def application(session, **args):
        result = await call(session, "java_application", **args)
        with anyio.fail_after(180):
            while result.get("ok") and (
                result.get("status") == "project_launch_started"
                or result.get("status") == "restart_started" and result.get("applied") is None
            ):
                await anyio.sleep(.2)
                status = await call(session, "java_status", action="status", details=True)
                if result.get("reload_id"):
                    latest = status.get("last_reload") or {}
                    if latest.get("reload_id") == result["reload_id"]:
                        result = latest
                elif status.get("launch_phase") in {"runtime_active", "failed"}:
                    result = status
        return result

    async def fast_test(session):
        result = await call(session, "java_fast_test", project_path=str(project),
                            tests=["example.ReplyTest"], java_home=java, build_java_home=java)
        with anyio.fail_after(180):
            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                await anyio.sleep(.2)
                result = await call(session, "java_fast_test", action="result", test_run_id=result["test_run_id"])
        return result

    async def assert_unchanged(session, pid, state):
        # A DevTools restart normally happens after its poll/quiet interval.
        # Observe beyond that window; PID alone misses an in-process restart.
        deadline = time.monotonic() + 3.0
        while True:
            assert await anyio.to_thread.run_sync(http_state) == state
            if time.monotonic() >= deadline:
                break
            await anyio.sleep(.25)
        status = await call(session, "java_status", action="status")
        assert status["pid"] == pid and status["startup_state"] == "ready", status

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w+", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                try:
                    started = await application(session, **launch)
                    assert started.get("ok") and started.get("startup_state") == "ready", started
                    pid = started["pid"]
                    original = await anyio.to_thread.run_sync(http_state)
                    assert original[0] == "v1" and original[2:4] == ["false", "missing"], original
                    assert "RestartClassLoader" not in original[4], original
                    assert (await fast_test(session)).get("passed") is True

                    # Fast Test has a separate bin. Its failed compilation must
                    # not touch the running app; nor should failed app restart
                    # invite DevTools to load JDT's generated problem class.
                    reply_source.write_text(_reply("v1").replace('"v1"', "missingSymbol"), encoding="utf-8")
                    failed_test = await fast_test(session)
                    assert failed_test.get("error_code") == "JDT_TEST_COMPILE_FAILED", failed_test
                    assert failed_test.get("runtime_unchanged") is True, failed_test
                    await assert_unchanged(session, pid, original)
                    failed_restart = await application(session, action="restart", timeout=30)
                    assert failed_restart.get("error_code") == "JDT_COMPILE_FAILED", failed_restart
                    assert failed_restart.get("applied") is False, failed_restart
                    await assert_unchanged(session, pid, original)

                    reply_source.write_text(_reply("v2"), encoding="utf-8")
                    recovered_test = await fast_test(session)
                    assert recovered_test.get("passed") is True, recovered_test
                    recovered = await application(session, action="restart", timeout=30)
                    assert recovered.get("ok") and recovered.get("apply_method") == "hotswap", recovered
                    assert recovered.get("build_kind") == "INCREMENTAL", recovered
                    assert recovered.get("compiled_source_count") == 1, recovered
                    changed = ["v2", *original[1:]]
                    await assert_unchanged(session, pid, changed)

                    # Replacing the entire argument list must reapply defaults.
                    # Explicit caller choice remains available for future use.
                    for arguments, enabled, flag in (
                        (["-Dfixture.flag=replaced"], "false", "replaced"),
                        ([], "false", "missing"),
                        (["-Dspring.devtools.restart.enabled=true"], "true", "missing"),
                        ([], "false", "missing"),
                    ):
                        restarted = await application(session, action="restart", hotswap=False,
                                                      vm_args=arguments, timeout=30)
                        assert restarted.get("ok") and restarted.get("apply_method") == "restart", restarted
                        assert restarted["pid"] != pid, restarted
                        assert restarted["compiled_source_count"] == 0, restarted
                        state = await anyio.to_thread.run_sync(http_state)
                        assert state[0] == "v2" and state[2:4] == [enabled, flag], state
                        assert state[1] != changed[1], state
                        assert ("RestartClassLoader" in state[4]) is (enabled == "true"), state
                        pid, changed = restarted["pid"], state
                finally:
                    await application(session, action="stop")

        with (tmp_path / "reopen-stderr.log").open("w+", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                try:
                    reopened = await application(session, **launch)
                    assert reopened.get("ok") and reopened.get("startup_state") == "ready", reopened
                    assert reopened.get("jdt_bootstrap_reused") is True, reopened
                    assert reopened.get("jdt_bootstrap_build_kind") is None, reopened
                    state = await anyio.to_thread.run_sync(http_state)
                    assert state[0] == "v2" and state[2:4] == ["false", "missing"], state
                    assert "RestartClassLoader" not in state[4], state
                    reused = await fast_test(session)
                    assert reused.get("passed") is True and reused["compiled_source_count"] == 0, reused
                finally:
                    await application(session, action="stop")

    anyio.run(scenario)
