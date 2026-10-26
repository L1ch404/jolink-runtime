"""Evaluate real Java in a suspended JVM through the public stdio MCP tool."""

from __future__ import annotations

import http.client
import hashlib
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

import anyio
import psutil
import pytest

from java_support import (
    assert_ok,
    call_payload,
    open_mcp_session,
    require_real_mcp_java_e2e,
    reserve_local_port,
    source_line,
    terminate_process,
    wait_until,
)


pytestmark = pytest.mark.mcp_java_e2e


SOURCE = """\
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.Executors;

public class EvaluationFixture {
    private int bias = 7;
    private int count = 99;
    private volatile int slowCalls = 0;

    public String choose(Object value) {
        return "object";
    }

    public String choose(String value) {
        return "string";
    }

    public String decorate(String name) {
        return name + ":" + bias; // nested evaluation breakpoint
    }

    private int twice(int value) {
        return value * 2;
    }

    private String hidden(String value) {
        return "hidden:" + value;
    }

    private String explode() {
        throw new IllegalStateException("evaluation-boom");
    }

    private int slow() throws InterruptedException {
        Thread.sleep(600);
        return ++slowCalls;
    }

    private String observe() {
        int count = 4;
        long wide = 3000000000L;
        double ratio = 2.5;
        String expected = "ALICE";
        String absent = null;
        Object candidate = "x";
        List<String> names = new ArrayList<String>(Arrays.asList("alice", "bob"));
        int answer = count + 1; // evaluation breakpoint
        return count + "|" + answer + "|" + names.get(0) + "|" + bias;
    }

    public static void main(String[] args) throws Exception {
        Class.forName("java.lang.IllegalStateException");
        final EvaluationFixture fixture = new EvaluationFixture();
        HttpServer server = HttpServer.create(
            new InetSocketAddress("127.0.0.1", Integer.parseInt(args[0])), 0);
        server.createContext("/", exchange -> {
            byte[] body = fixture.observe().getBytes("UTF-8");
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.createContext("/classpath", exchange -> {
            byte[] body = System.getProperty("java.class.path").getBytes("UTF-8");
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.createContext("/stats", exchange -> {
            byte[] body = Integer.toString(fixture.slowCalls).getBytes("UTF-8");
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.setExecutor(Executors.newCachedThreadPool());
        server.start();
    }
}
"""


LOADER_SOURCE = """\
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.file.Paths;
import java.util.Arrays;

public class EvaluationLoader {
    public static void main(String[] args) throws Exception {
        URLClassLoader loader = new URLClassLoader(
            new URL[] {Paths.get(args[0]).toUri().toURL()},
            ClassLoader.getSystemClassLoader().getParent());
        Thread.currentThread().setContextClassLoader(loader);
        Class<?> app = Class.forName("EvaluationFixture", true, loader);
        app.getMethod("main", String[].class).invoke(
            null, (Object) Arrays.copyOfRange(args, 1, args.length));
    }
}
"""


def _prepare(
    tmp_path: Path, java_major: int, mode: str = "direct",
) -> tuple[Path, dict[str, str], dict[str, str], Path]:
    require_real_mcp_java_e2e()
    home = os.environ.get(f"JOLINK_TEST_JAVA{java_major}_HOME")
    if not home:
        pytest.skip(f"set JOLINK_TEST_JAVA{java_major}_HOME")
    java_home = Path(home)
    suffix = ".exe" if os.name == "nt" else ""
    if mode == "project":
        if shutil.which("mvn") is None:
            pytest.skip("Maven is required for the project-path evaluation case")
        project = tmp_path / "project"
        source = project / "src/main/java/EvaluationFixture.java"
        source.parent.mkdir(parents=True)
        (project / "pom.xml").write_text("""<project xmlns="http://maven.apache.org/POM/4.0.0">
<modelVersion>4.0.0</modelVersion><groupId>example</groupId>
<artifactId>evaluation-fixture</artifactId><version>1</version>
<properties><maven.compiler.source>8</maven.compiler.source>
<maven.compiler.target>8</maven.compiler.target>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId>
<version>3.11.0</version></plugin></plugins></build></project>
""", encoding="utf-8")
        target = {"project_path": str(project)}
    else:
        source = tmp_path / "EvaluationFixture.java"
        target = {"classpath": str(tmp_path)}
    source.write_text(SOURCE, encoding="utf-8")
    if mode != "project":
        sources = [str(source)]
        if mode == "custom_loader":
            loader = tmp_path / "EvaluationLoader.java"
            loader.write_text(LOADER_SOURCE, encoding="utf-8")
            sources.append(str(loader))
        subprocess.run(
            [str(java_home / f"bin/javac{suffix}"), "-g", "-encoding", "UTF-8", *sources],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    # Preserve the shared, already downloaded JDT runtime. Only this test's
    # application and MCP processes are created; no user project is modified.
    environment = {
        **os.environ,
        "JAVA_HOME": str(java_home),
        "PATH": str(java_home / "bin") + os.pathsep + os.environ.get("PATH", ""),
    }
    return java_home, environment, target, source


def _http_get(port: int, path: str = "/") -> str:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        assert response.status == 200
        return response.read().decode("utf-8")
    finally:
        connection.close()


def _class_snapshot(classpath: str) -> dict[str, str]:
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for entry in classpath.split(os.pathsep)
        if Path(entry).is_dir()
        for path in Path(entry).rglob("*.class")
    }


def _compiler_pids(application_pid: int) -> set[int]:
    parent = psutil.Process(application_pid).parent()
    assert parent is not None
    return {
        process.pid
        for process in parent.children(recursive=True)
        if "net.jolink.runtime.jdt.worker" in process.cmdline()
    }


@pytest.mark.parametrize("java_major, mode", [
    (8, "direct"), (17, "direct"), (21, "direct"), (8, "project"),
    (17, "custom_loader"), (17, "attach"),
])
def test_evaluate_live_frame_and_keep_debugging(tmp_path: Path, java_major: int, mode: str) -> None:
    java_home, environment, target, source = _prepare(tmp_path, java_major, mode)
    port = reserve_local_port()
    jdwp_port = reserve_local_port()
    external: subprocess.Popen[bytes] | None = None

    async def scenario() -> None:
        with (tmp_path / "mcp-stderr.log").open("w+", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=environment) as session:
                try:
                    if mode == "attach":
                        assert external is not None
                        started = assert_ok(await call_payload(session, {
                            "action": "attach", "pid": external.pid,
                            "main_class": "EvaluationFixture", "jdwp_port": jdwp_port,
                        }))
                        assert started["status"] == "attached", started
                    else:
                        started = assert_ok(await call_payload(session, {
                            "action": "run",
                            **target,
                            "main_class": "EvaluationLoader" if mode == "custom_loader" else "EvaluationFixture",
                            "java_home": str(java_home),
                            "app_args": [str(tmp_path), str(port)] if mode == "custom_loader" else [str(port)],
                            "ready_port": port,
                            "jdwp_port": jdwp_port,
                        }))
                        assert started["startup_state"] == "ready", started
                    classpath = await anyio.to_thread.run_sync(_http_get, port, "/classpath")
                    before = _class_snapshot(classpath)
                    assert before
                    compiler_pids = _compiler_pids(started["pid"]) if mode == "project" else set()
                    if mode == "project":
                        assert compiler_pids, "Project launch must have initialized its JDT compiler"
                    assert_ok(await call_payload(session, {
                        "action": "breakpoint",
                        "bp_action": "set",
                        "class_pattern": "EvaluationFixture",
                        "line": source_line(SOURCE, "// evaluation breakpoint"),
                    }))
                    # These logical requests must not stop an invoked evaluator
                    # inside another method or while it catches an exception.
                    assert_ok(await call_payload(session, {
                        "action": "breakpoint",
                        "bp_action": "set",
                        "class_pattern": "EvaluationFixture",
                        "line": source_line(SOURCE, "// nested evaluation breakpoint"),
                    }))
                    assert_ok(await call_payload(session, {
                        "action": "exception",
                        "exception_class": "java.lang.IllegalStateException",
                        "caught": True,
                        "uncaught": True,
                    }))

                    for iteration in range(2):
                        response: dict[str, str] = {}
                        suspension: str | None = None
                        armed = assert_ok(await call_payload(session, {
                            "action": "wait_event", "wait_mode": "arm", "timeout": 20,
                        }))
                        assert armed["status"] == "armed", armed

                        async def request() -> None:
                            response["body"] = await anyio.to_thread.run_sync(_http_get, port)

                        async with anyio.create_task_group() as task_group:
                            task_group.start_soon(request)
                            hit = assert_ok(await call_payload(session, {
                                "action": "wait_event", "wait_mode": "await",
                                "wait_handle": armed["wait_handle"], "timeout": 20,
                            }))
                            assert hit["status"] == "breakpoint_hit", hit
                            suspension = hit["suspension_id"]

                            async def evaluate(expression: str) -> dict[str, Any]:
                                return await call_payload(session, {
                                    "action": "evaluate",
                                    "expression": expression,
                                    "suspension_id": suspension,
                                    "frame_index": 0,
                                })

                            async def value(expression: str) -> Any:
                                result = assert_ok(await evaluate(expression))
                                assert result["status"] == "evaluated", result
                                assert result["expression"] == expression, result
                                assert result["result"]["type"], result
                                return result["result"]["value"]

                            try:
                                if iteration == 0:
                                    if mode == "custom_loader":
                                        assert await value(
                                            "this.getClass().getClassLoader().getClass().getName()"
                                        ) == "java.net.URLClassLoader"
                                    assert await value("count") == 4
                                    cached = assert_ok(await evaluate("count"))
                                    assert cached["compile_reused"] is True, cached
                                    assert cached["result"]["value"] == 4, cached
                                    assert await value("this.count") == 99
                                    # Overload resolution uses the local's declared
                                    # Object type, not the live object's String class.
                                    assert await value("choose(candidate)") == "object"
                                    assert await value("choose((String) candidate)") == "string"
                                    assert await value("count * 3 + 2") == 14
                                    assert await value("wide + count") == 3000000004
                                    assert await value("ratio / 2") == 1.25
                                    assert await value("names.get(0).toUpperCase().equals(expected)") is True
                                    assert await value("this.bias + count") == 11
                                    assert await value("((java.util.function.IntSupplier) () -> this.bias).getAsInt()") == 7
                                    assert await value("((java.util.function.IntSupplier) () -> this.twice(count)).getAsInt()") == 8
                                    # A separate upstream snippet codegen defect must not
                                    # kill the compiler or silently return a value.
                                    bare = await evaluate("((java.util.function.IntSupplier) () -> bias).getAsInt()")
                                    assert bare["ok"] is False, bare
                                    if java_major == 8:
                                        assert bare["error_code"] == "EVALUATION_EXCEPTION", bare
                                        assert "VerifyError" in json.dumps(bare), bare
                                    else:
                                        assert bare["error_code"] == "EVALUATION_COMPILE_FAILED", bare
                                        assert "Attempt to push null on operand stack" in json.dumps(bare), bare
                                    assert await value("count") == 4
                                    assert await value("decorate(names.get(0))") == "alice:7"
                                    assert await value("twice(count)") == 8
                                    assert await value("java.lang.Integer.toString(count)") == "4"
                                    assert await value('"中文"') == "中文"
                                    assert await value('"😀"') == "😀"
                                    assert await value('""') == ""
                                    assert await value('"left\\0right"') == "left\x00right"
                                    assert await value("absent") is None
                                    assert await value("absent == null || absent.isEmpty()") is True
                                    names = await value("names")
                                    assert names["_kind"] == "list", names
                                    assert names["items"] == ["alice", "bob"], names
                                    assert (await value("new int[] {count, count + 1}"))["items"] == [4, 5]

                                    failed = await evaluate("count +")
                                    assert failed.get("ok") is False, failed
                                    assert failed["error_code"] == "EVALUATION_COMPILE_FAILED", failed
                                    assert failed["diagnostics"], failed
                                    # A compile error must not poison the current suspension.
                                    assert await value("count") == 4

                                    thrown = await evaluate("explode()")
                                    assert thrown.get("ok") is False, thrown
                                    assert thrown["error_code"] == "EVALUATION_EXCEPTION", thrown
                                    assert "evaluation-boom" in json.dumps(thrown), thrown
                                    # A target exception must leave the original frame usable.
                                    assert await value("count") == 4

                                    # JDT 3.46's private reference-parameter bridge
                                    # currently resolves the wrong reflection type.
                                    # Keep that observed upstream boundary visible;
                                    # it must return an error, not false success or
                                    # corrupt the original suspended frame.
                                    private_reference = await evaluate("hidden(names.get(0))")
                                    assert private_reference.get("ok") is False, private_reference
                                    assert private_reference["error_code"] == "EVALUATION_EXCEPTION", private_reference
                                    assert "NoSuchMethodException" in json.dumps(private_reference), private_reference
                                    assert await value("twice(count)") == 8

                                    assert await value("count = count + 6") == 10
                                    cached = assert_ok(await evaluate("count"))
                                    assert cached["compile_reused"] is True, cached
                                    assert cached["result"]["value"] == 10, cached
                                    assert await value("this.count") == 99
                                    assert await value('names.set(0, "updated")') == "alice"
                                    assert await value("this.bias = 9") == 9
                                    variables = assert_ok(await call_payload(session, {
                                        "action": "variables",
                                        "suspension_id": suspension,
                                        "frame_index": 0,
                                    }))
                                    observed = {item["name"]: item["value"] for item in variables["variables"]}
                                    assert observed["count"] == 10, observed
                                    assert observed["names"]["items"] == ["updated", "bob"], observed
                                else:
                                    # A new request gets fresh locals but sees the real object
                                    # mutation from the earlier evaluation, not a detached copy.
                                    assert await value("count") == 4
                                    assert await value("decorate(names.get(0))") == "alice:9"
                                    assert await value("this.bias") == 9
                            finally:
                                if suspension is not None:
                                    assert_ok(await call_payload(session, {
                                        "action": "resume",
                                        "suspension_id": suspension,
                                    }))
                        assert response["body"] == (
                            "10|11|updated|9" if iteration == 0 else "4|5|alice|9"
                        ), response
                    assert _class_snapshot(classpath) == before
                    assert source.read_text(encoding="utf-8") == SOURCE
                    if mode == "project":
                        assert _compiler_pids(started["pid"]) == compiler_pids
                finally:
                    await call_payload(session, {"action": "cleanup_debug_state"})
                    await call_payload(session, {"action": "detach" if mode == "attach" else "stop"})

    if mode != "attach":
        anyio.run(scenario)
        return

    suffix = ".exe" if os.name == "nt" else ""
    with (tmp_path / "external-jvm.log").open("wb") as output:
        external = subprocess.Popen(
            [str(java_home / f"bin/java{suffix}"),
             f"-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=127.0.0.1:{jdwp_port}",
             "-cp", str(tmp_path), "EvaluationFixture", str(port)],
            cwd=tmp_path, env=environment, stdout=output, stderr=subprocess.STDOUT,
        )

        def ready() -> bool:
            assert external is not None and external.poll() is None
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return True
            except OSError:
                return False

        try:
            wait_until(ready, timeout=20)
            anyio.run(scenario)
            assert external.poll() is None, "Detaching must not stop an attached JVM"
            assert _http_get(port) == "4|5|alice|9"
        finally:
            terminate_process(external)


def test_evaluate_timeout_does_not_retry_target_method(tmp_path: Path) -> None:
    java_home, environment, target, _ = _prepare(tmp_path, 17)
    port = reserve_local_port()

    async def scenario() -> None:
        with (tmp_path / "mcp-stderr.log").open("w+", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=environment) as session:
                try:
                    assert_ok(await call_payload(session, {
                        "action": "run", **target, "main_class": "EvaluationFixture",
                        "java_home": str(java_home), "app_args": [str(port)],
                        "ready_port": port, "jdwp_port": reserve_local_port(),
                    }))
                    assert_ok(await call_payload(session, {
                        "action": "breakpoint", "bp_action": "set",
                        "class_pattern": "EvaluationFixture",
                        "line": source_line(SOURCE, "// evaluation breakpoint"),
                    }))
                    response: dict[str, str] = {}
                    armed = assert_ok(await call_payload(session, {
                        "action": "wait_event", "wait_mode": "arm", "timeout": 20,
                    }))
                    assert armed["status"] == "armed", armed

                    async def request() -> None:
                        response["body"] = await anyio.to_thread.run_sync(_http_get, port)

                    async with anyio.create_task_group() as task_group:
                        task_group.start_soon(request)
                        hit = assert_ok(await call_payload(session, {
                            "action": "wait_event", "wait_mode": "await",
                            "wait_handle": armed["wait_handle"], "timeout": 20,
                        }))
                        assert hit["status"] == "breakpoint_hit", hit
                        args = {"action": "evaluate", "suspension_id": hit["suspension_id"]}
                        try:
                            # Warm the code-snippet compiler/target bridge so the
                            # tiny timeout observes target execution, not startup.
                            assert_ok(await call_payload(session, {**args, "expression": "count"}))
                            timed_out = await call_payload(session, {
                                **args, "expression": "slow()", "timeout": 0.1,
                            })
                            assert timed_out.get("ok") is False, timed_out
                            assert timed_out["error_code"] == "EVALUATION_OUTCOME_UNKNOWN", timed_out
                            # A reply timeout is not a cancellation. Wait for the
                            # original invocation; never submit the expression again.
                            await anyio.sleep(1)
                        finally:
                            assert_ok(await call_payload(session, {"action": "cleanup_debug_state"}))
                    assert response["body"] == "4|5|alice|7", response
                    assert await anyio.to_thread.run_sync(_http_get, port, "/stats") == "1"
                    assert await anyio.to_thread.run_sync(_http_get, port) == "4|5|alice|7"
                    assert await anyio.to_thread.run_sync(_http_get, port, "/stats") == "1"
                finally:
                    await call_payload(session, {"action": "cleanup_debug_state"})
                    await call_payload(session, {"action": "stop"})

    anyio.run(scenario)
