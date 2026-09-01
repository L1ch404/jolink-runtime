from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TextIO

import anyio
import mcp.types as types
import psutil
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUN_REAL_MCP_JAVA_E2E = os.environ.get("JOLINK_RUN_MCP_JAVA_E2E") == "1"


def require_real_mcp_java_e2e() -> None:
    if not RUN_REAL_MCP_JAVA_E2E:
        pytest.skip(
            "set JOLINK_RUN_MCP_JAVA_E2E=1 to run the real MCP/JVM E2E suite"
        )
    missing = [
        command
        for command in ("java", "javac")
        if shutil.which(command) is None
    ]
    if missing:
        pytest.skip(f"JDK commands are required: {', '.join(missing)}")


def compile_java(
    directory: Path,
    class_name: str,
    source: str,
) -> Path:
    source_path = directory / f"{class_name}.java"
    source_path.write_text(source, encoding="utf-8")
    subprocess.run(
        ["javac", "-encoding", "UTF-8", "-g", str(source_path)],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return source_path


def source_line(source: str, fragment: str) -> int:
    return next(
        line_number
        for line_number, line in enumerate(source.splitlines(), start=1)
        if fragment in line
    )


def reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@asynccontextmanager
async def open_mcp_session(
    stderr: TextIO,
    *,
    environment: dict[str, str] | None = None,
) -> AsyncIterator[ClientSession]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jolink_runtime.transport.stdio"],
        cwd=REPOSITORY_ROOT,
        env=environment,
    )
    async with stdio_client(parameters, errlog=stderr) as (
        read_stream,
        write_stream,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "jolink-runtime"
            assert initialized.serverInfo.version == "0.1.0a3"
            yield session


def temporary_stderr() -> TextIO:
    return tempfile.TemporaryFile(mode="w+", encoding="utf-8")


async def call_payload(
    session: ClientSession,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    external = dict(arguments)
    action = str(external.get("action", ""))
    if action in {"run", "stop", "restart", "attach", "detach", "update"}:
        tool_name = "java_application"
        external["action"] = {"run": "launch", "update": "reload"}.get(
            action,
            action,
        )
    elif action in {"status", "logs"}:
        tool_name = "java_status"
    else:
        tool_name = "java_debugger"
    result = await session.call_tool(tool_name, external)
    assert result.structuredContent is not None
    assert len(result.content) == 1
    assert isinstance(result.content[0], types.TextContent)
    payload = dict(result.structuredContent)
    assert json.loads(result.content[0].text) == payload
    return payload


def assert_ok(payload: dict[str, Any]) -> dict[str, Any]:
    assert payload.get("ok") is True, payload
    return payload


async def wait_until_async(
    predicate: Callable[[], bool],
    *,
    timeout: float = 10.0,
    interval: float = 0.05,
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        await anyio.sleep(interval)


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 10.0,
    interval: float = 0.05,
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        time.sleep(interval)


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


def kill_pid(pid: int) -> None:
    try:
        process = psutil.Process(pid)
        process.kill()
        process.wait(timeout=5)
    except psutil.Error:
        pass


def launch_external_java(
    directory: Path,
    class_name: str,
    app_args: list[str],
    jdwp_port: int,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            "java",
            (
                "-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,"
                f"address=127.0.0.1:{jdwp_port}"
            ),
            "-cp",
            str(directory),
            class_name,
            *app_args,
        ],
        cwd=directory,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
    )
