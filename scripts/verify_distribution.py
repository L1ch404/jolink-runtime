#!/usr/bin/env python3
"""Verify an installed joLink distribution against a real local JVM.

This script is intentionally independent of the pytest suite.  Run it with
the Python interpreter from a clean environment where either the wheel or the
sdist has been installed, and point ``--server`` at that environment's
``jolink-runtime`` executable.
"""

from __future__ import annotations

import argparse
import http.client
import hashlib
import importlib.metadata
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

import anyio
import mcp.types as types
import psutil
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import jolink_runtime


JAVA_SOURCE = """\
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;

public class DistributionFixture {
    public static void main(String[] args) throws Exception {
        Path trigger = Paths.get(args[0]);
        Path marker = Paths.get(args[1]);
        Path stop = Paths.get(args[2]);
        int iteration = 0;
        while (!Files.exists(stop)) {
            if (Files.exists(trigger)) {
                Files.deleteIfExists(trigger);
                int observed = iteration++;
                System.out.println(observed);
                Files.write(
                    marker,
                    (Integer.toString(observed) + "\\n").getBytes(StandardCharsets.UTF_8),
                    StandardOpenOption.CREATE,
                    StandardOpenOption.APPEND
                );
            }
            Thread.sleep(20);
        }
    }
}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--server",
        required=True,
        type=Path,
        help="Absolute path to the installed jolink-runtime executable.",
    )
    parser.add_argument(
        "--expected-source-root",
        type=Path,
        help="Fail if the imported package comes from this source checkout.",
    )
    parser.add_argument("--expected-version", default=jolink_runtime.__version__)
    return parser.parse_args()


def reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def source_line(fragment: str) -> int:
    return next(
        number
        for number, line in enumerate(JAVA_SOURCE.splitlines(), start=1)
        if fragment in line
    )


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 15.0,
    interval: float = 0.05,
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        time.sleep(interval)


async def wait_until_async(
    predicate: Callable[[], bool],
    *,
    timeout: float = 15.0,
    interval: float = 0.05,
) -> None:
    deadline = anyio.current_time() + timeout
    while not predicate():
        if anyio.current_time() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        await anyio.sleep(interval)


def pid_is_alive(pid: int) -> bool:
    if not psutil.pid_exists(pid):
        return False
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def compile_fixture(directory: Path) -> None:
    source_path = directory / "DistributionFixture.java"
    source_path.write_text(JAVA_SOURCE, encoding="utf-8")
    subprocess.run(
        ["javac", "-encoding", "UTF-8", "-g", str(source_path)],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def launch_external_java(
    directory: Path,
    trigger: Path,
    marker: Path,
    stop: Path,
    jdwp_port: int,
) -> subprocess.Popen[bytes]:
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        [
            "java",
            (
                "-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,"
                f"address=127.0.0.1:{jdwp_port}"
            ),
            "-cp",
            str(directory),
            "DistributionFixture",
            str(trigger),
            str(marker),
            str(stop),
        ],
        cwd=directory,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
        creationflags=creationflags,
    )


async def call_payload(
    session: ClientSession,
    arguments: dict[str, Any],
    *,
    tool: str | None = None,
) -> dict[str, Any]:
    if tool is None:
        action = arguments["action"]
        if action in {"launch", "attach", "restart", "stop", "detach"}:
            tool = "java_application"
        elif action in {"status", "logs", "processes"}:
            tool = "java_status"
        else:
            tool = "java_debugger"
    result = await session.call_tool(tool, arguments)
    assert result.structuredContent is not None, result
    assert len(result.content) == 1, result
    assert isinstance(result.content[0], types.TextContent), result
    payload = dict(result.structuredContent)
    assert json.loads(result.content[0].text) == payload
    assert result.isError is (payload.get("ok") is False)
    return payload


def assert_ok(payload: dict[str, Any]) -> dict[str, Any]:
    assert payload.get("ok") is True, payload
    return payload


async def arm_trigger_await(
    session: ClientSession,
    trigger: Path,
) -> dict[str, Any]:
    armed = assert_ok(await call_payload(session, {
        "action": "wait_event",
        "wait_mode": "arm",
        "timeout": 15,
    }))
    assert armed["status"] == "armed", armed
    wait_handle = str(armed["wait_handle"])

    trigger.write_text("go", encoding="utf-8")
    hit = assert_ok(await call_payload(session, {
        "action": "wait_event",
        "wait_mode": "await",
        "wait_handle": wait_handle,
        "timeout": 15,
    }))
    assert hit["status"] == "breakpoint_hit", hit
    assert hit["wait_handle"] == wait_handle, hit
    return hit


async def attach_with_retry(
    session: ClientSession,
    *,
    process: subprocess.Popen[bytes],
    jdwp_port: int,
) -> dict[str, Any]:
    deadline = anyio.current_time() + 15
    while True:
        payload = await call_payload(session, {
            "action": "attach",
            "pid": process.pid,
            "jdwp_port": jdwp_port,
            "main_class": "DistributionFixture",
        })
        if payload.get("ok") is True:
            return payload
        assert process.poll() is None, payload
        if anyio.current_time() >= deadline:
            raise AssertionError(payload)
        await anyio.sleep(0.1)


async def verify_owned_flow(
    session: ClientSession,
    directory: Path,
    breakpoint_line: int,
) -> None:
    trigger = directory / "owned-trigger"
    marker = directory / "owned-marker"
    stop = directory / "owned-stop"
    jdwp_port = reserve_local_port()

    started = assert_ok(await call_payload(session, {
        "action": "launch",
        "classpath": str(directory),
        "main_class": "DistributionFixture",
        "app_args": [str(trigger), str(marker), str(stop)],
        "jdwp_port": jdwp_port,
    }))
    owned_pid = int(started["pid"])
    assert pid_is_alive(owned_pid)

    status = assert_ok(await call_payload(session, {"action": "status"}))
    assert status["running"] is True, status
    assert status["process_state"] == "running", status
    assert status["debug_state"] == "attached", status

    breakpoint = assert_ok(await call_payload(session, {
        "action": "breakpoint",
        "bp_action": "set",
        "class_pattern": "DistributionFixture",
        "line": breakpoint_line,
    }))
    assert breakpoint["matched_class"] == "DistributionFixture", breakpoint

    hit = await arm_trigger_await(session, trigger)
    suspension_id = str(hit["suspension_id"])
    assert hit["location"]["line"] == breakpoint_line, hit

    resumed = assert_ok(await call_payload(session, {
        "action": "resume",
        "suspension_id": suspension_id,
    }))
    assert resumed["invalidated_suspension_id"] == suspension_id, resumed
    await wait_until_async(marker.exists)

    cleaned = assert_ok(await call_payload(session, {
        "action": "cleanup_debug_state",
    }))
    assert cleaned["status"] == "debug_state_cleaned", cleaned

    stopped = assert_ok(await call_payload(session, {"action": "stop"}))
    assert stopped["status"] in {"stopped", "not_running"}, stopped
    await wait_until_async(lambda: not pid_is_alive(owned_pid))


async def verify_attached_flow(
    session: ClientSession,
    directory: Path,
    breakpoint_line: int,
) -> subprocess.Popen[bytes]:
    trigger = directory / "attached-trigger"
    marker = directory / "attached-marker"
    stop = directory / "attached-stop"
    jdwp_port = reserve_local_port()
    process = launch_external_java(
        directory,
        trigger,
        marker,
        stop,
        jdwp_port,
    )

    try:
        attached = assert_ok(await attach_with_retry(
            session,
            process=process,
            jdwp_port=jdwp_port,
        ))
        assert attached["status"] == "attached", attached

        assert_ok(await call_payload(session, {
            "action": "breakpoint",
            "bp_action": "set",
            "class_pattern": "DistributionFixture",
            "line": breakpoint_line,
        }))
        hit = await arm_trigger_await(session, trigger)
        suspension_id = str(hit["suspension_id"])

        assert_ok(await call_payload(session, {
            "action": "resume",
            "suspension_id": suspension_id,
        }))
        await wait_until_async(marker.exists)

        assert_ok(await call_payload(session, {
            "action": "cleanup_debug_state",
        }))
        detached = assert_ok(await call_payload(session, {"action": "detach"}))
        assert detached["status"] in {"detached", "not_attached"}, detached
        assert process.poll() is None
        assert pid_is_alive(process.pid)
        return process
    except BaseException:
        terminate_process(process)
        raise


def clean_server_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["JOLINK_LOG_LEVEL"] = "INFO"
    return environment


def project_fixture(directory: Path, port: int) -> tuple[Path, Path]:
    """A real Maven project; no checkout imports or precompiled classes."""
    project = directory / "maven-project"
    source = project / "src/main/java/example/Reply.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        'package example; public class Reply { public static String value() { return "before"; } }',
        encoding="utf-8",
    )
    source.with_name("App.java").write_text(
        'package example; public class App { public static void main(String[] args) throws Exception {'
        ' com.sun.net.httpserver.HttpServer server = com.sun.net.httpserver.HttpServer.create('
        f'new java.net.InetSocketAddress("127.0.0.1", {port}), 0);'
        ' server.createContext("/", e -> { byte[] data = Reply.value().getBytes("UTF-8");'
        ' e.sendResponseHeaders(200, data.length); e.getResponseBody().write(data); e.close(); });'
        ' server.start(); }}',
        encoding="utf-8",
    )
    test = project / "src/test/java/example/ReplyTest.java"
    test.parent.mkdir(parents=True)
    test.write_text(
        'package example; public class ReplyTest { @org.junit.Test public void value() {'
        ' org.junit.Assert.assertEquals("before", Reply.value()); }}',
        encoding="utf-8",
    )
    (project / "pom.xml").write_text(
        '<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId>'
        '<artifactId>distribution-smoke</artifactId><version>1</version><properties>'
        '<maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target>'
        '<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>'
        '<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId>'
        '<version>4.13.2</version><scope>test</scope></dependency></dependencies>'
        '<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version>'
        '</plugin></plugins></build></project>',
        encoding="utf-8",
    )
    launch = ET.Element("component", name="ProjectRunConfigurationManager")
    configuration = ET.SubElement(launch, "configuration", name="Distribution", type="Application")
    ET.SubElement(configuration, "option", name="MAIN_CLASS_NAME", value="example.App")
    path = project / ".run/Distribution.run.xml"
    path.parent.mkdir()
    path.write_text(ET.tostring(launch, encoding="unicode"), encoding="utf-8")
    return project, source


def http_value(port: int) -> str:
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        client.request("GET", "/")
        response = client.getresponse()
        assert response.status == 200
        return response.read().decode("utf-8")
    finally:
        client.close()


async def verify_project_flow(session: ClientSession, directory: Path) -> None:
    port = reserve_local_port()
    project, source = project_fixture(directory, port)

    async def status():
        return assert_ok(await call_payload(session, {"action": "status"}))

    async def test():
        result = assert_ok(await call_payload(session, {
            "action": "run", "project_path": str(project), "tests": ["example.ReplyTest"],
        }, tool="java_fast_test"))
        with anyio.fail_after(300):
            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                await anyio.sleep(0.2)
                result = (await status())["fast_test"]
        assert result.get("ok") and result.get("passed") and result["tests"] == 1, result

    async def restart(**arguments):
        result = assert_ok(await call_payload(session, {"action": "restart", **arguments}))
        with anyio.fail_after(180):
            while result.get("applied") is None:
                state = await status()
                finished = state.get("last_reload") or {}
                if finished.get("reload_id") == result.get("reload_id"):
                    result = finished
                    break
                await anyio.sleep(0.2)
        assert result.get("ok") and result.get("applied"), result
        assert await anyio.to_thread.run_sync(http_value, port) == "after"
        return result

    try:
        assert_ok(await call_payload(session, {
            "action": "launch", "project_path": str(project), "launch_name": "Distribution",
            "ready_port": port, "jdwp_port": reserve_local_port(),
        }))
        with anyio.fail_after(300):
            while True:
                state = await status()
                assert state.get("launch_phase") != "failed", state
                if state.get("startup_state") == "ready":
                    break
                await anyio.sleep(0.2)
        pid = state["pid"]
        assert await anyio.to_thread.run_sync(http_value, port) == "before"
        await test()
        source.write_text(source.read_text(encoding="utf-8").replace('"before"', '"after"'), encoding="utf-8")
        applied = await restart()
        assert applied["apply_method"] == "hotswap", applied
        assert (await status())["pid"] == pid
        applied = await restart(hotswap=False)
        assert applied["apply_method"] == "restart", applied
        assert (await status())["pid"] != pid
        test_source = project / "src/test/java/example/ReplyTest.java"
        test_source.write_text(test_source.read_text(encoding="utf-8").replace('"before"', '"after"'), encoding="utf-8")
        await test()
    finally:
        assert_ok(await call_payload(session, {"action": "stop"}))


async def verify_mcp(
    server: Path,
    directory: Path,
    stderr: TextIO,
    expected_version: str,
) -> subprocess.Popen[bytes]:
    parameters = StdioServerParameters(
        command=str(server),
        args=[],
        cwd=directory,
        env=clean_server_environment(),
    )
    breakpoint_line = source_line("System.out.println(observed)")
    attached_process: subprocess.Popen[bytes] | None = None
    try:
        with anyio.fail_after(900):
            async with stdio_client(parameters, errlog=stderr) as (
                read_stream,
                write_stream,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "jolink-runtime"
                    assert initialized.serverInfo.version == expected_version

                    listed = await session.list_tools()
                    assert {tool.name for tool in listed.tools} == {
                        "java_application", "java_fast_test", "java_status", "java_debugger",
                    }

                    await verify_owned_flow(session, directory, breakpoint_line)
                    await verify_project_flow(session, directory)
                    attached_process = await verify_attached_flow(
                        session,
                        directory,
                        breakpoint_line,
                    )
        assert attached_process is not None
        return attached_process
    except BaseException:
        if attached_process is not None:
            terminate_process(attached_process)
        raise


def validate_install_location(expected_source_root: Path | None) -> Path:
    package_file = Path(jolink_runtime.__file__).resolve()
    if expected_source_root is not None:
        source_root = expected_source_root.resolve()
        assert not package_file.is_relative_to(source_root), (
            f"distribution verification imported source checkout: {package_file}"
        )
    return package_file


def validate_legal_materials() -> None:
    distribution = importlib.metadata.distribution("jolink-runtime")
    notice = next((file for file in distribution.files or ()
                   if str(file).endswith(".dist-info/licenses/THIRD_PARTY_NOTICES.md")), None)
    assert notice is not None, "Installed distribution is missing third-party notices"
    root = Path(distribution.locate_file(notice)).parent
    assert (root / "LICENSE").is_file()
    index = json.loads((root / "licenses/runtime-sources.json").read_text())
    assert index["source_archives"]
    materials = json.loads((root / "licenses/materials.json").read_text())
    for item in materials["files"]:
        data = (root / item["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"], item["path"]


def main() -> None:
    args = parse_args()
    server = args.server.resolve()
    assert server.is_absolute()
    assert server.is_file(), f"installed server executable not found: {server}"
    assert jolink_runtime.__version__ == args.expected_version
    assert importlib.metadata.version("jolink-runtime") == args.expected_version
    package_file = validate_install_location(args.expected_source_root)
    validate_legal_materials()
    for command in ("java", "javac"):
        assert shutil.which(command), f"required JDK command not found: {command}"
    assert shutil.which("mvn") or shutil.which("mvn.cmd"), "Maven is required for project/Fast Test verification"

    attached_process: subprocess.Popen[bytes] | None = None
    with tempfile.TemporaryDirectory(prefix="jolink-dist-") as temporary:
        directory = Path(temporary).resolve()
        compile_fixture(directory)
        stderr_path = directory / "mcp-stderr.log"
        try:
            with stderr_path.open("w+", encoding="utf-8") as stderr:
                attached_process = anyio.run(
                    verify_mcp,
                    server,
                    directory,
                    stderr,
                    args.expected_version,
                )
                stderr.flush()

            log_text = stderr_path.read_text(encoding="utf-8")
            assert "java_runtime.action.start" in log_text, log_text
            assert "action=run" in log_text, log_text
            assert "action=attach" in log_text, log_text

            # The external JVM must remain alive after detach and MCP shutdown.
            assert attached_process.poll() is None
            assert pid_is_alive(attached_process.pid)
            (directory / "attached-stop").write_text("stop", encoding="utf-8")
            wait_until(lambda: attached_process.poll() is not None)
        finally:
            if attached_process is not None:
                terminate_process(attached_process)

    print(json.dumps({
        "ok": True,
        "server_name": "jolink-runtime",
        "version": args.expected_version,
        "package_file": str(package_file),
        "verified": [
            "packaged_licenses_and_sources_index",
            "initialize",
            "tools/list",
            "launch",
            "project_jdt_launch",
            "project_hotswap_http",
            "project_restart_http",
            "java_fast_test_before_and_after_edit",
            "status",
            "wait",
            "resume",
            "cleanup_debug_state",
            "stop",
            "attach",
            "detach",
            "stderr_logging",
        ],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
