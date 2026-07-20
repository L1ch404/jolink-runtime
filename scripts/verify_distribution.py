#!/usr/bin/env python3
"""Verify an installed joLink distribution against a real local JVM.

This script is intentionally independent of the pytest suite.  Run it with
the Python interpreter from a clean environment where either the wheel or the
sdist has been installed, and point ``--server`` at that environment's
``jolink-runtime-debugger`` executable.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

import anyio
import mcp.types as types
import psutil
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import jolink_runtime_debugger


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
        help="Absolute path to the installed jolink-runtime-debugger executable.",
    )
    parser.add_argument(
        "--expected-source-root",
        type=Path,
        help="Fail if the imported package comes from this source checkout.",
    )
    parser.add_argument("--expected-version", default="0.1.0a1")
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
) -> dict[str, Any]:
    result = await session.call_tool("java_runtime", arguments)
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
        "action": "run",
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
    return environment


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
        with anyio.fail_after(120):
            async with stdio_client(parameters, errlog=stderr) as (
                read_stream,
                write_stream,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "jolink-runtime"
                    assert initialized.serverInfo.version == expected_version

                    listed = await session.list_tools()
                    assert [tool.name for tool in listed.tools] == [
                        "java_runtime",
                        "java_processes",
                    ]

                    await verify_owned_flow(session, directory, breakpoint_line)
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
    package_file = Path(jolink_runtime_debugger.__file__).resolve()
    if expected_source_root is not None:
        source_root = expected_source_root.resolve()
        assert not package_file.is_relative_to(source_root), (
            f"distribution verification imported source checkout: {package_file}"
        )
    return package_file


def main() -> None:
    args = parse_args()
    server = args.server.resolve()
    assert server.is_absolute()
    assert server.is_file(), f"installed server executable not found: {server}"
    assert jolink_runtime_debugger.__version__ == args.expected_version
    package_file = validate_install_location(args.expected_source_root)
    for command in ("java", "javac"):
        assert shutil.which(command), f"required JDK command not found: {command}"

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
            "initialize",
            "tools/list",
            "run",
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
