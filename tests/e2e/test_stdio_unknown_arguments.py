"""Unknown top-level arguments must not block real MCP calls or reach dispatch."""

import json
import os
import threading
from copy import deepcopy
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
import pytest
from java_support import (
    compile_java,
    open_mcp_session,
    reserve_local_port,
    source_line,
    temporary_stderr,
)
from test_fast_test_output import fixture
from test_test_execution_config import environment


async def call_checked(session, tool, arguments, ignored=()):
    original = deepcopy(arguments)
    response = await session.call_tool(tool, arguments)
    payload = dict(response.structuredContent)
    assert json.loads(response.content[0].text) == payload
    assert response.isError is (payload.get("ok") is False)
    assert arguments == original
    if ignored:
        assert payload.get("ignored_arguments") == sorted(ignored), payload
    else:
        assert "ignored_arguments" not in payload, payload
    return payload


@pytest.mark.mcp_java_e2e
def test_unknown_parameters_over_stdio(tmp_path):
    env = environment(tmp_path)

    async def scenario():
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                assert "verbose" not in tools["java_status"].inputSchema["properties"]

                call = partial(call_checked, session)
                status = await call(
                    "java_status", {"action": "status", "verbose": True}, ("verbose",)
                )
                assert status["ok"] is True and status["running"] is False, status
                await call("java_status", {"action": "status"})
                processes = await call(
                    "java_status",
                    {"action": "processes", "detail_level": 2},
                    ("detail_level",),
                )
                assert processes.get("ok") is not False, processes
                assert processes["count"] == len(processes["processes"])
                missing = await call(
                    "java_fast_test",
                    {
                        "tests": ["example.OutputTest#prints"],
                        "project_paht": str(tmp_path),
                    },
                    ("project_paht",),
                )
                assert (
                    missing["error_code"] == "INVALID_ARGUMENT"
                    and missing["argument"] == "project_path"
                )
                wrong_type = await call(
                    "java_status",
                    {"action": "logs", "tail": "50", "extra": True},
                    ("extra",),
                )
                assert (
                    wrong_type["error_code"] == "INVALID_ARGUMENT"
                    and wrong_type["argument"] == "tail"
                )
                wrong_action = await call(
                    "java_status", {"action": "statuz", "extra": True}, ("extra",)
                )
                assert (
                    wrong_action["error_code"] == "INVALID_ARGUMENT"
                    and wrong_action["argument"] == "action"
                )
                wait = await call(
                    "java_debugger",
                    {
                        "action": "wait_event",
                        "wait_mode": "await",
                        "wait_handle": "not-created",
                        "extra": 1,
                    },
                    ("extra",),
                )
                assert wait["error_code"] == "WAIT_HANDLE_NOT_FOUND", wait
                nested = await call(
                    "java_debugger",
                    {
                        "action": "wait_event",
                        "extra": True,
                        "http_trigger": {
                            "method": "POST",
                            "url": "http://127.0.0.1:9/test",
                            "unmodeled": 1,
                        },
                    },
                    ("extra",),
                )
                assert (
                    nested["error_code"] == "INVALID_ARGUMENT"
                    and nested["argument"] == "http_trigger"
                )
                for operation in ("result", "cancel"):
                    extra = {"project_path": {"unused": True}, "tests": False, "timeout": -1, "extra": None}
                    missing = await call("java_fast_test", {"action": operation, **extra}, extra)
                    assert missing["error_code"] == "INVALID_ARGUMENT" and missing["argument"] == "test_run_id"
                    invalid = await call("java_fast_test", {"action": operation, "test_run_id": [], **extra}, extra)
                    assert invalid["error_code"] == "INVALID_ARGUMENT" and invalid["argument"] == "test_run_id"
                    absent = await call("java_fast_test", {"action": operation, "test_run_id": "not-retained", **extra}, extra)
                    assert absent["error_code"] == "TEST_RUN_NOT_FOUND", absent

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_fast_test_runs_and_reads_results_despite_unknown_fields(tmp_path):
    env = environment(tmp_path)
    root, source = fixture(tmp_path, "UTF-8")
    executions = root / "executions.txt"
    source.write_text(source.read_text(encoding="utf-8").replace("public class OutputTest {", '''public class OutputTest {
 @org.junit.Before public void recordExecution() throws Exception {
  java.nio.file.Files.write(java.nio.file.Paths.get("executions.txt"),
   java.util.Arrays.asList("executed"), java.nio.charset.StandardCharsets.UTF_8,
   java.nio.file.StandardOpenOption.CREATE, java.nio.file.StandardOpenOption.APPEND);
 }'''), encoding="utf-8")

    def execution_count():
        return len(executions.read_text(encoding="utf-8").splitlines()) if executions.exists() else 0

    async def scenario():
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                call = partial(call_checked, session)
                for expected_count, method in enumerate(("prints", "failed"), 1):
                    args = {
                        "project_path": str(root),
                        "tests": ["example.OutputTest#" + method],
                        "timeout": 0,
                        "verbose": True,
                        "maxOutputLength": 1000,
                    }
                    if os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
                        args["maven_settings"] = os.environ[
                            "JOLINK_TEST_MAVEN_SETTINGS"
                        ]
                    started = await call(
                        "java_fast_test", args, ("verbose", "maxOutputLength")
                    )
                    assert started["ok"] is True, started
                    run_id = started["test_run_id"]
                    with anyio.fail_after(180):
                        while True:
                            result = await call(
                                "java_fast_test",
                                {
                                    **args,
                                    "action": "result",
                                    "test_run_id": run_id,
                                    "tail": 1,
                                },
                                (*args, "tail"),
                            )
                            if result["status"] not in {
                                "starting",
                                "bootstrapping",
                                "compiling",
                                "running",
                            }:
                                break
                            await anyio.sleep(0.1)
                    assert result["ok"] is True and result["passed"] is (
                        method == "prints"
                    ), result
                    assert result["tests"] == 1 and result["output"]
                    assert execution_count() == expected_count
                    clean = await call(
                        "java_fast_test", {"action": "result", "test_run_id": run_id}
                    )
                    assert (
                        clean["test_run_id"] == run_id
                        and "ignored_arguments" not in clean
                    )
                    status = await call("java_status", {"action": "status"})
                    assert "ignored_arguments" not in status["fast_test"]
                    # Even malformed/stale run fields cannot retarget a read.
                    extra = {"project_path": str(root / "must-not-create"), "tests": ["example.OutputTest#slow"],
                             "source_files": {}, "java_home": [], "build_java_home": False,
                             "maven_home": {}, "maven_settings": None, "build_system": "unused", "timeout": -1}
                    repeated = await call("java_fast_test", {
                        **extra, "action": "result", "test_run_id": run_id,
                    }, extra)
                    assert {k: v for k, v in repeated.items() if k != "ignored_arguments"} == clean
                    assert execution_count() == expected_count and not (root / "must-not-create").exists()
                    print(
                        json.dumps(
                            {"submission": started, "result": result},
                            ensure_ascii=False,
                        )
                    )

                slow = {**args, "tests": ["example.OutputTest#slow"]}
                started = await call("java_fast_test", slow, ("verbose", "maxOutputLength"))
                run_id = started["test_run_id"]
                with anyio.fail_after(60):
                    while True:
                        active = await call("java_fast_test", {"action": "result", "test_run_id": run_id})
                        if "live-start" in active.get("output", ""):
                            break
                        assert active["status"] in {"starting", "bootstrapping", "compiling", "running"}, active
                        await anyio.sleep(.1)
                assert execution_count() == 3
                missing = await call("java_fast_test", {
                    **slow, "action": "cancel", "test_run_id": "not-the-active-run",
                }, slow)
                assert missing["error_code"] == "TEST_RUN_NOT_FOUND"
                active = await call("java_fast_test", {"action": "result", "test_run_id": run_id})
                assert active["status"] == "running" and not active["cancel_requested"], active
                cancelled = await call("java_fast_test", {**slow, "action": "cancel", "test_run_id": run_id}, slow)
                assert cancelled["ok"] and cancelled["status"] == "cancel_requested", cancelled
                with anyio.fail_after(30):
                    while True:
                        stopped = await call("java_fast_test", {**slow, "action": "result", "test_run_id": run_id}, slow)
                        if stopped["status"] == "cancelled":
                            break
                        await anyio.sleep(.1)
                assert stopped["error_code"] == "TEST_CANCELLED" and execution_count() == 3
                quiet = {**args, "tests": ["example.OutputTest#quiet"], "timeout": 30}
                recovered = await call("java_fast_test", quiet, ("verbose", "maxOutputLength"))
                assert recovered.get("passed") is True and execution_count() == 4, recovered
                missing = await call("java_fast_test", {**quiet, "action": "result", "test_run_id": run_id}, quiet)
                assert missing["error_code"] == "TEST_RUN_NOT_FOUND"  # Do not substitute the newest result.
                original = source.read_bytes()
                try:
                    source.write_bytes(original + b"\ninvalid java source\n")
                    failed = await call("java_fast_test", quiet, ("verbose", "maxOutputLength"))
                    assert failed.get("ok") is False and failed.get("error_count", 0) > 0, failed
                    assert failed.get("diagnostics") and execution_count() == 4
                finally:
                    source.write_bytes(original)
                recovered = await call("java_fast_test", quiet, ("verbose", "maxOutputLength"))
                assert recovered.get("passed") is True and execution_count() == 5, recovered

    anyio.run(scenario)


ARGUMENT_FIXTURE = """import java.nio.file.*;
public class ArgumentFixture {
 public static void main(String[] args) throws Exception {
  while(!Files.exists(Paths.get(args[0]))) Thread.sleep(20);
  int answer = 42;
  System.out.println(answer);
  while(true) Thread.sleep(20);
 }
}"""


@pytest.mark.mcp_java_e2e
def test_launch_and_debug_wait_returns_keep_ignored_arguments(tmp_path, monkeypatch):
    env = environment(tmp_path)
    monkeypatch.setenv("PATH", env["PATH"])
    compile_java(tmp_path, "ArgumentFixture", ARGUMENT_FIXTURE)
    trigger = tmp_path / "trigger"
    line = source_line(ARGUMENT_FIXTURE, "System.out.println(answer)")
    received = []
    body = {"extra": "business-field", "project_path": "business-path",
            "nested": {"tests": ["payload"], "ignored_arguments": ["must-stay"]}}

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.headers.get("X-Custom"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            trigger.write_text("go", encoding="utf-8")
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    server.daemon_threads = True
    receiver_thread = threading.Thread(target=server.serve_forever, daemon=True)
    receiver_thread.start()

    async def scenario():
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                call = partial(call_checked, session)
                try:
                    launched = await call(
                        "java_application",
                        {
                            "action": "launch",
                            "classpath": str(tmp_path),
                            "main_class": "ArgumentFixture",
                            "app_args": [str(trigger)],
                            "jdwp_port": reserve_local_port(),
                            "verbose": True,
                        },
                        ("verbose",),
                    )
                    assert launched["ok"] is True and launched["pid"] > 0, launched
                    status = await call("java_status", {"action": "status"})
                    assert status["running"] is True and status["pid"] == launched["pid"], status
                    breakpoint = await call(
                        "java_debugger",
                        {
                            "action": "breakpoint",
                            "bp_action": "set",
                            "class_pattern": "ArgumentFixture",
                            "line": line,
                            "extra": 1,
                        },
                        ("extra",),
                    )
                    assert breakpoint["ok"] is True, breakpoint
                    armed = await call(
                        "java_debugger",
                        {
                            "action": "wait_event",
                            "wait_mode": "arm",
                            "timeout": 10,
                            "extra": 1,
                            "http_trigger": {
                                "method": "POST", "url": f"http://127.0.0.1:{server.server_port}/",
                                "headers": {"X-Custom": "kept"}, "json_body": body,
                            },
                        },
                        ("extra",),
                    )
                    assert armed["ok"] is True and armed["status"] == "armed", armed
                    hit = await call(
                        "java_debugger",
                        {
                            "action": "wait_event",
                            "wait_mode": "await",
                            "wait_handle": armed["wait_handle"],
                            "timeout": 10,
                            "extra": 2,
                        },
                        ("extra",),
                    )
                    assert hit["ok"] is True and hit["suspension_id"], hit
                    assert received == [("kept", body)]
                    resumed = await call(
                        "java_debugger",
                        {"action": "resume", "suspension_id": hit["suspension_id"]},
                    )
                    assert resumed["ok"] is True, resumed
                finally:
                    stopped = await call(
                        "java_application",
                        {"action": "stop", "verbose": True},
                        ("verbose",),
                    )
                    assert stopped["ok"] is True, stopped
                    status = await call("java_status", {"action": "status"})
                    assert status["running"] is False and status["debug_state"] == "detached", status

    try:
        anyio.run(scenario)
    finally:
        server.shutdown()
        server.server_close()
        receiver_thread.join(5)
