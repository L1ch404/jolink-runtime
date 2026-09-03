from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import anyio
import mcp.types as types
import pytest
from mcp.shared.exceptions import McpError

from java_support import (
    assert_ok,
    call_payload,
    compile_java,
    kill_pid,
    launch_external_java,
    open_mcp_session,
    pid_is_alive,
    require_real_mcp_java_e2e,
    reserve_local_port,
    source_line,
    temporary_stderr,
    terminate_process,
    wait_until,
    wait_until_async,
)


pytestmark = pytest.mark.mcp_java_e2e


def _java8_mcp_environment() -> dict[str, str]:
    java8_home = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not java8_home:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME for JDT-first Java 8 launch")
    return {
        **os.environ,
        "JAVA_HOME": java8_home,
        "PATH": (
            str(Path(java8_home) / "bin")
            + os.pathsep
            + os.environ.get("PATH", "")
        ),
    }


class _DaemonHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


async def _await_reload(
    session: Any,
    started: dict[str, Any],
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    assert started["status"] == "reload_started"
    reload_id = started["reload_id"]
    with anyio.fail_after(timeout):
        while True:
            status = assert_ok(await call_payload(session, {"action": "status"}))
            terminal = status.get("last_reload")
            if (
                isinstance(terminal, dict)
                and terminal.get("reload_id") == reload_id
            ):
                return terminal
            await anyio.sleep(0.05)


@contextmanager
def _java_trigger_server(
    trigger: Path,
    marker: Path,
) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            trigger.write_text("http", encoding="utf-8")
            deadline = time.monotonic() + 20
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.send_response(204 if marker.exists() else 504)
            self.end_headers()

        def log_message(self, _format: str, *args: Any) -> None:
            return

    server = _DaemonHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/trigger"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@contextmanager
def _immediate_java_trigger_server(trigger: Path) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            trigger.write_text("http", encoding="utf-8")
            self.send_response(204)
            self.end_headers()

        def log_message(self, _format: str, *args: Any) -> None:
            return

    server = _DaemonHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/trigger"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


OWNED_SOURCE = """\
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

public class OwnedMcpFixture {
    public static void main(String[] args) throws Exception {
        Path trigger = Paths.get(args[0]);
        Path marker = Paths.get(args[1]);
        while (!Files.exists(trigger)) {
            Thread.sleep(20);
        }
        String sex = "男";
        String missing = null;
        int answer = 42;
        System.out.println(sex + answer);
        Files.write(marker, "resumed".getBytes(StandardCharsets.UTF_8));
        while (true) {
            Thread.sleep(100);
        }
    }
}
"""


ATTACHED_SOURCE = """\
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

public class AttachedMcpFixture {
    public static void main(String[] args) throws Exception {
        Path trigger = Paths.get(args[0]);
        Path marker = Paths.get(args[1]);
        Path stop = Paths.get(args[2]);
        while (!Files.exists(trigger)) {
            Thread.sleep(20);
        }
        int answer = 7;
        System.out.println(answer);
        Files.write(marker, "continued".getBytes(StandardCharsets.UTF_8));
        while (!Files.exists(stop)) {
            Thread.sleep(20);
        }
    }
}
"""


CANCELLATION_SOURCE = """\
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;

public class CancellationMcpFixture {
    public static void main(String[] args) throws Exception {
        Path trigger = Paths.get(args[0]);
        Path marker = Paths.get(args[1]);
        int iteration = 0;
        while (true) {
            if (Files.exists(trigger)) {
                Files.delete(trigger);
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


DELAYED_HTTP_SOURCE = """\
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

public class DelayedHttpMcpFixture {
    public static void main(String[] args) throws Exception {
        Path trigger = Paths.get(args[0]);
        Path marker = Paths.get(args[1]);
        while (!Files.exists(trigger)) {
            Thread.sleep(20);
        }
        Thread.sleep(700);
        int delayedAnswer = 73;
        System.out.println(delayedAnswer);
        Files.write(marker, "resumed".getBytes(StandardCharsets.UTF_8));
        while (true) {
            Thread.sleep(100);
        }
    }
}
"""


READINESS_SOURCE = """\
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

public class ReadinessMcpFixture {
    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(args[0]);
        Path stop = Paths.get(args[1]);
        Thread.sleep(3000);
        try (ServerSocket server = new ServerSocket(
            port,
            16,
            InetAddress.getByName("127.0.0.1")
        )) {
            server.setSoTimeout(100);
            while (!Files.exists(stop)) {
                try (Socket socket = server.accept()) {
                    // A successful accept is enough for the TCP readiness probe.
                } catch (SocketTimeoutException timeout) {
                    // Re-check the stop marker.
                }
            }
        }
    }
}
"""


def test_full_stdio_debug_chain_and_owned_shutdown(tmp_path: Path) -> None:
    require_real_mcp_java_e2e()
    compile_java(tmp_path, "OwnedMcpFixture", OWNED_SOURCE)
    breakpoint_line = source_line(OWNED_SOURCE, "System.out.println(sex + answer)")
    trigger = tmp_path / "owned-trigger"
    marker = tmp_path / "owned-marker"
    jdwp_port = reserve_local_port()
    owned_pid = 0

    async def scenario() -> None:
        nonlocal owned_pid
        with temporary_stderr() as stderr:
            with anyio.fail_after(60):
                async with open_mcp_session(stderr) as session:
                    started = assert_ok(await call_payload(session, {
                        "action": "run",
                        "classpath": str(tmp_path),
                        "main_class": "OwnedMcpFixture",
                        "app_args": [str(trigger), str(marker)],
                        "jdwp_port": jdwp_port,
                    }))
                    owned_pid = int(started["pid"])

                    breakpoint = assert_ok(await call_payload(session, {
                        "action": "breakpoint",
                        "bp_action": "set",
                        "class_pattern": "OwnedMcpFixture",
                        "line": breakpoint_line,
                    }))
                    assert breakpoint["matched_class"] == "OwnedMcpFixture"
                    assert breakpoint["line"] == breakpoint_line
                    assert breakpoint["suspend_policy"] == "EVENT_THREAD"

                    hit_holder: dict[str, Any] = {}

                    async def wait_for_hit() -> None:
                        hit_holder.update(assert_ok(await call_payload(session, {
                            "action": "wait_event",
                            "timeout": 10,
                        })))

                    async with anyio.create_task_group() as task_group:
                        task_group.start_soon(wait_for_hit)
                        await anyio.sleep(0.5)
                        trigger.write_text("go", encoding="utf-8")
                    hit = hit_holder
                    assert hit["status"] == "breakpoint_hit"
                    assert hit["location"]["line"] == breakpoint_line
                    suspension_id = str(hit["suspension_id"])

                    stack = assert_ok(await call_payload(session, {
                        "action": "stack",
                        "suspension_id": suspension_id,
                        "max_frames": 10,
                    }))
                    assert stack["frames"][0]["class"] == "LOwnedMcpFixture;"

                    variables = assert_ok(await call_payload(session, {
                        "action": "variables",
                        "suspension_id": suspension_id,
                        "frame_index": 0,
                    }))
                    values = {
                        variable["name"]: variable["value"]
                        for variable in variables["variables"]
                    }
                    assert values["sex"] == "男"
                    assert values["missing"] is None
                    assert values["answer"] == 42
                    assert variables["observation_state"] == "complete"

                    resumed = assert_ok(await call_payload(session, {
                        "action": "resume",
                        "suspension_id": suspension_id,
                    }))
                    assert resumed["invalidated_suspension_id"] == suspension_id
                    await wait_until_async(marker.exists)

                    logs = assert_ok(await call_payload(session, {
                        "action": "logs",
                        "tail": 10,
                    }))
                    assert any("男42" in line for line in logs["lines"])
                    assert logs["requested_lines"] == 10
                    assert logs["returned_lines"] <= 10
                    assert logs["snapshot_size_bytes"] >= logs["scanned_bytes"]
                    assert logs["growth_state"] == "first_observation"
                    assert logs["truncated"] is False

                    unchanged_logs = assert_ok(await call_payload(session, {
                        "action": "logs",
                        "tail": 10,
                    }))
                    assert unchanged_logs["growth_state"] == "unchanged"
                    assert unchanged_logs["new_bytes_since_previous_read"] == 0

                    # Deliberately omit stop. Server lifespan cleanup owns it.

    try:
        anyio.run(scenario)
        assert owned_pid > 0
        wait_until(lambda: not pid_is_alive(owned_pid), timeout=10)
    finally:
        if owned_pid and pid_is_alive(owned_pid):
            kill_pid(owned_pid)


def test_shutdown_resumes_and_detaches_suspended_attached_jvm(
    tmp_path: Path,
) -> None:
    require_real_mcp_java_e2e()
    compile_java(tmp_path, "AttachedMcpFixture", ATTACHED_SOURCE)
    breakpoint_line = source_line(ATTACHED_SOURCE, "System.out.println(answer)")
    trigger = tmp_path / "attached-trigger"
    marker = tmp_path / "attached-marker"
    stop = tmp_path / "attached-stop"
    jdwp_port = reserve_local_port()
    process = launch_external_java(
        tmp_path,
        "AttachedMcpFixture",
        [str(trigger), str(marker), str(stop)],
        jdwp_port,
    )

    async def scenario() -> None:
        with temporary_stderr() as stderr:
            with anyio.fail_after(60):
                async with open_mcp_session(stderr) as session:
                    attached: dict[str, Any] = {}
                    deadline = anyio.current_time() + 15
                    while True:
                        attached = await call_payload(session, {
                            "action": "attach",
                            "pid": process.pid,
                            "jdwp_port": jdwp_port,
                            "main_class": "AttachedMcpFixture",
                        })
                        if attached.get("ok") is True:
                            break
                        assert process.poll() is None, attached
                        if anyio.current_time() >= deadline:
                            pytest.fail(str(attached))
                        await anyio.sleep(0.1)
                    assert attached["status"] == "attached"

                    assert_ok(await call_payload(session, {
                        "action": "breakpoint",
                        "bp_action": "set",
                        "class_pattern": "AttachedMcpFixture",
                        "line": breakpoint_line,
                    }))
                    hit_holder: dict[str, Any] = {}

                    async def wait_for_hit() -> None:
                        hit_holder.update(assert_ok(await call_payload(session, {
                            "action": "wait_event",
                            "timeout": 10,
                        })))

                    async with anyio.create_task_group() as task_group:
                        task_group.start_soon(wait_for_hit)
                        await anyio.sleep(0.5)
                        trigger.write_text("go", encoding="utf-8")
                    hit = hit_holder
                    assert hit["status"] == "breakpoint_hit"

                    # Keep the event thread suspended. Server shutdown must
                    # resume and detach without terminating this external JVM.

    try:
        anyio.run(scenario)
        wait_until(marker.exists, timeout=10)
        assert marker.read_text(encoding="utf-8") == "continued"
        assert process.poll() is None
        assert pid_is_alive(process.pid)
    finally:
        stop.write_text("stop", encoding="utf-8")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            terminate_process(process)


def test_cancelled_notification_releases_waiter_and_preserves_breakpoint(
    tmp_path: Path,
) -> None:
    require_real_mcp_java_e2e()
    compile_java(tmp_path, "CancellationMcpFixture", CANCELLATION_SOURCE)
    breakpoint_line = source_line(
        CANCELLATION_SOURCE,
        "System.out.println(observed)",
    )
    trigger = tmp_path / "cancel-trigger"
    marker = tmp_path / "cancel-marker"
    jdwp_port = reserve_local_port()

    async def cancel_current_wait(session: Any, iteration: int) -> None:
        # The SDK currently exposes cancellation as a notification but does
        # not expose the in-flight request id publicly. Reading the monotonic
        # id immediately before this single request gives the protocol id the
        # notification must reference.
        request_id = session._request_id
        local_cancel_scope = anyio.CancelScope()
        local_done = anyio.Event()
        cancellation_errors: list[str] = []

        async def invoke_wait() -> None:
            with local_cancel_scope:
                try:
                    await session.call_tool(
                        "java_runtime",
                        {"action": "wait_event", "timeout": 30},
                    )
                except McpError as error:
                    cancellation_errors.append(str(error))
                finally:
                    local_done.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(invoke_wait)
            await anyio.sleep(0.3)
            await session.send_notification(types.CancelledNotification(
                params=types.CancelledNotificationParams(
                    requestId=request_id,
                    reason=f"e2e cancellation iteration {iteration}",
                )
            ))
            with anyio.move_on_after(2):
                await local_done.wait()
            if not local_done.is_set():
                local_cancel_scope.cancel()
                await local_done.wait()

        assert cancellation_errors
        assert "cancel" in cancellation_errors[0].lower()
        with anyio.fail_after(10):
            status = assert_ok(await call_payload(
                session,
                {"action": "status"},
            ))
        assert status["debug_state"] != "suspended"
        assert status.get("suspension_id") in {None, ""}

    async def scenario() -> None:
        with temporary_stderr() as stderr:
            with anyio.fail_after(90):
                async with open_mcp_session(stderr) as session:
                    assert_ok(await call_payload(session, {
                        "action": "run",
                        "classpath": str(tmp_path),
                        "main_class": "CancellationMcpFixture",
                        "app_args": [str(trigger), str(marker)],
                        "jdwp_port": jdwp_port,
                    }))
                    breakpoint = assert_ok(await call_payload(session, {
                        "action": "breakpoint",
                        "bp_action": "set",
                        "class_pattern": "CancellationMcpFixture",
                        "line": breakpoint_line,
                    }))
                    breakpoint_id = breakpoint["breakpoint_id"]

                    for iteration in range(5):
                        await cancel_current_wait(session, iteration)

                    # No waiter is active here. Executing the breakpoint line
                    # must not suspend the JVM; the marker is written after it.
                    trigger.write_text("outside-wait", encoding="utf-8")
                    await wait_until_async(marker.exists)
                    assert marker.read_text(encoding="utf-8") == "0\n"
                    status = assert_ok(await call_payload(session, {
                        "action": "status",
                    }))
                    assert status["debug_state"] != "suspended"
                    assert status.get("suspension_id") in {None, ""}

                    timed_out = assert_ok(await call_payload(session, {
                        "action": "wait_event",
                        "timeout": 0.2,
                    }))
                    assert timed_out["status"] == "timeout"

                    # A request created for a timed-out wait must also be
                    # gone before a later trigger executes.
                    trigger.write_text("after-timeout", encoding="utf-8")
                    with anyio.fail_after(10):
                        while marker.read_text(encoding="utf-8") != "0\n1\n":
                            await anyio.sleep(0.05)
                    status = assert_ok(await call_payload(session, {
                        "action": "status",
                    }))
                    assert status["debug_state"] != "suspended"
                    assert status.get("suspension_id") in {None, ""}

                    listed = assert_ok(await call_payload(session, {
                        "action": "breakpoint",
                        "bp_action": "list",
                    }))
                    assert [
                        item["breakpoint_id"]
                        for item in listed["breakpoints"]
                    ] == [breakpoint_id]

                    hit_holder: dict[str, Any] = {}

                    async def wait_for_real_hit() -> None:
                        hit_holder.update(assert_ok(await call_payload(session, {
                            "action": "wait_event",
                            "timeout": 10,
                        })))

                    async with anyio.create_task_group() as task_group:
                        task_group.start_soon(wait_for_real_hit)
                        await anyio.sleep(0.3)
                        trigger.write_text("go", encoding="utf-8")

                    assert hit_holder["status"] == "breakpoint_hit"
                    assert hit_holder["breakpoint_id"] == breakpoint_id
                    resumed = assert_ok(await call_payload(session, {
                        "action": "resume",
                        "suspension_id": hit_holder["suspension_id"],
                    }))
                    assert resumed["status"] == "resumed"
                    with anyio.fail_after(10):
                        while marker.read_text(encoding="utf-8") != "0\n1\n2\n":
                            await anyio.sleep(0.05)

                    # A completed hit disarms its raw JDWP request but keeps
                    # the logical definition. A new wait must re-arm it.
                    second_hit: dict[str, Any] = {}

                    async def wait_for_second_hit() -> None:
                        second_hit.update(assert_ok(await call_payload(session, {
                            "action": "wait_event",
                            "timeout": 10,
                        })))

                    async with anyio.create_task_group() as task_group:
                        task_group.start_soon(wait_for_second_hit)
                        await anyio.sleep(0.3)
                        trigger.write_text("go-again", encoding="utf-8")

                    assert second_hit["status"] == "breakpoint_hit"
                    assert second_hit["breakpoint_id"] == breakpoint_id
                    assert_ok(await call_payload(session, {
                        "action": "resume",
                        "suspension_id": second_hit["suspension_id"],
                    }))
                    with anyio.fail_after(10):
                        while marker.read_text(encoding="utf-8") != "0\n1\n2\n3\n":
                            await anyio.sleep(0.05)

                    assert_ok(await call_payload(session, {"action": "stop"}))

    anyio.run(scenario)


def test_two_phase_wait_arms_before_immediate_trigger_and_rearms(
    tmp_path: Path,
) -> None:
    """The trigger may run immediately after arm; no sleep window is needed."""
    require_real_mcp_java_e2e()
    compile_java(tmp_path, "CancellationMcpFixture", CANCELLATION_SOURCE)
    breakpoint_line = source_line(
        CANCELLATION_SOURCE,
        "System.out.println(observed)",
    )
    trigger = tmp_path / "two-phase-trigger"
    marker = tmp_path / "two-phase-marker"
    jdwp_port = reserve_local_port()

    async def arm_trigger_await(
        session: Any,
        trigger_value: str,
    ) -> dict[str, Any]:
        armed = assert_ok(await call_payload(session, {
            "action": "wait_event",
            "wait_mode": "arm",
            "timeout": 10,
        }))
        assert armed["status"] == "armed"
        assert armed["armed_breakpoint_ids"] == ["bp_001"]
        wait_handle = str(armed["wait_handle"])

        # This write happens immediately after the MCP response.  Unlike the
        # legacy blocking pattern, correctness does not depend on guessing a
        # sleep long enough for EventRequest.Set to finish.
        trigger.write_text(trigger_value, encoding="utf-8")
        hit = assert_ok(await call_payload(session, {
            "action": "wait_event",
            "wait_mode": "await",
            "wait_handle": wait_handle,
            "timeout": 10,
        }))
        assert hit["status"] == "breakpoint_hit"
        assert hit["breakpoint_id"] == "bp_001"
        assert hit["wait_handle"] == wait_handle
        return hit

    async def scenario() -> None:
        with temporary_stderr() as stderr:
            with anyio.fail_after(60):
                async with open_mcp_session(stderr) as session:
                    assert_ok(await call_payload(session, {
                        "action": "run",
                        "classpath": str(tmp_path),
                        "main_class": "CancellationMcpFixture",
                        "app_args": [str(trigger), str(marker)],
                        "jdwp_port": jdwp_port,
                    }))
                    breakpoint = assert_ok(await call_payload(session, {
                        "action": "breakpoint",
                        "bp_action": "set",
                        "class_pattern": "CancellationMcpFixture",
                        "line": breakpoint_line,
                    }))
                    assert breakpoint["breakpoint_id"] == "bp_001"

                    first = await arm_trigger_await(session, "first")
                    first_request_id = int(first["jdwp"]["request_id"])
                    assert_ok(await call_payload(session, {
                        "action": "resume",
                        "suspension_id": first["suspension_id"],
                    }))
                    await wait_until_async(marker.exists)
                    with anyio.fail_after(10):
                        while marker.read_text(encoding="utf-8") != "0\n":
                            await anyio.sleep(0.02)

                    second = await arm_trigger_await(session, "second")
                    second_request_id = int(second["jdwp"]["request_id"])
                    assert second_request_id != first_request_id
                    assert_ok(await call_payload(session, {
                        "action": "resume",
                        "suspension_id": second["suspension_id"],
                    }))
                    with anyio.fail_after(10):
                        while marker.read_text(encoding="utf-8") != "0\n1\n":
                            await anyio.sleep(0.02)

                    listed = assert_ok(await call_payload(session, {
                        "action": "breakpoint",
                        "bp_action": "list",
                    }))
                    assert [
                        item["breakpoint_id"]
                        for item in listed["breakpoints"]
                    ] == ["bp_001"]
                    assert_ok(await call_payload(session, {"action": "stop"}))

    anyio.run(scenario)


def test_arm_bound_http_trigger_hits_real_jvm_and_unblocks_after_resume(
    tmp_path: Path,
) -> None:
    """The server sends HTTP only after arming and never waits for its reply."""
    require_real_mcp_java_e2e()
    compile_java(tmp_path, "OwnedMcpFixture", OWNED_SOURCE)
    breakpoint_line = source_line(OWNED_SOURCE, "System.out.println(sex + answer)")
    trigger = tmp_path / "http-trigger"
    marker = tmp_path / "http-marker"
    jdwp_port = reserve_local_port()

    with _java_trigger_server(trigger, marker) as url:
        async def scenario() -> None:
            with temporary_stderr() as stderr:
                with anyio.fail_after(60):
                    async with open_mcp_session(stderr) as session:
                        assert_ok(await call_payload(session, {
                            "action": "run",
                            "classpath": str(tmp_path),
                            "main_class": "OwnedMcpFixture",
                            "app_args": [str(trigger), str(marker)],
                            "jdwp_port": jdwp_port,
                        }))
                        assert_ok(await call_payload(session, {
                            "action": "breakpoint",
                            "bp_action": "set",
                            "class_pattern": "OwnedMcpFixture",
                            "line": breakpoint_line,
                        }))

                        armed = assert_ok(await call_payload(session, {
                            "action": "wait_event",
                            "wait_mode": "arm",
                            "timeout": 10,
                            "http_trigger": {
                                "method": "POST",
                                "url": url,
                                "json_body": {"scenario": "local-e2e"},
                                "timeout_seconds": 20,
                            },
                        }))
                        assert armed["status"] == "armed"
                        assert armed["required_next_action"] == {
                            "action": "wait_event",
                            "wait_mode": "await",
                            "wait_handle": armed["wait_handle"],
                        }

                        hit = assert_ok(await call_payload(session, {
                            "action": "wait_event",
                            "wait_mode": "await",
                            "wait_handle": armed["wait_handle"],
                            "timeout": 10,
                        }))
                        assert hit["status"] == "breakpoint_hit"
                        assert hit["location"]["line"] == breakpoint_line
                        assert hit["http_trigger"]["status"] == "running"

                        assert_ok(await call_payload(session, {
                            "action": "resume",
                            "suspension_id": hit["suspension_id"],
                        }))
                        await wait_until_async(marker.exists)
                        assert_ok(await call_payload(session, {"action": "stop"}))

        anyio.run(scenario)


def test_blocking_http_trigger_waits_for_delayed_real_jvm_hit_after_headers(
    tmp_path: Path,
) -> None:
    """One-call trigger waiting survives response headers before a JVM hit."""
    require_real_mcp_java_e2e()
    compile_java(tmp_path, "DelayedHttpMcpFixture", DELAYED_HTTP_SOURCE)
    breakpoint_line = source_line(
        DELAYED_HTTP_SOURCE,
        "System.out.println(delayedAnswer)",
    )
    trigger = tmp_path / "delayed-http-trigger"
    marker = tmp_path / "delayed-http-marker"
    jdwp_port = reserve_local_port()

    with _immediate_java_trigger_server(trigger) as url:
        async def scenario() -> None:
            with temporary_stderr() as stderr:
                with anyio.fail_after(60):
                    async with open_mcp_session(stderr) as session:
                        assert_ok(await call_payload(session, {
                            "action": "run",
                            "classpath": str(tmp_path),
                            "main_class": "DelayedHttpMcpFixture",
                            "app_args": [str(trigger), str(marker)],
                            "jdwp_port": jdwp_port,
                        }))
                        assert_ok(await call_payload(session, {
                            "action": "breakpoint",
                            "bp_action": "set",
                            "class_pattern": "DelayedHttpMcpFixture",
                            "line": breakpoint_line,
                        }))

                        hit = assert_ok(await call_payload(session, {
                            "action": "wait_event",
                            "wait_mode": "blocking",
                            "timeout": 10,
                            "http_trigger": {
                                "method": "POST",
                                "url": url,
                                "timeout_seconds": 5,
                            },
                        }))
                        assert hit["status"] == "breakpoint_hit"
                        assert hit["location"]["line"] == breakpoint_line
                        assert hit["http_trigger"]["status"] == (
                            "response_headers_received"
                        )
                        assert hit["http_trigger"]["http_status"] == 204

                        variables = assert_ok(await call_payload(session, {
                            "action": "variables",
                            "suspension_id": hit["suspension_id"],
                            "frame_index": 0,
                        }))
                        observed = {
                            variable["name"]: variable["value"]
                            for variable in variables["variables"]
                        }
                        assert observed["delayedAnswer"] == 73
                        assert_ok(await call_payload(session, {
                            "action": "resume",
                            "suspension_id": hit["suspension_id"],
                        }))
                        await wait_until_async(marker.exists)
                        assert_ok(await call_payload(session, {"action": "stop"}))

        anyio.run(scenario)


def test_run_status_tracks_delayed_tcp_readiness(tmp_path: Path) -> None:
    require_real_mcp_java_e2e()
    compile_java(tmp_path, "ReadinessMcpFixture", READINESS_SOURCE)
    jdwp_port = reserve_local_port()
    ready_port = reserve_local_port()
    while ready_port == jdwp_port:
        ready_port = reserve_local_port()
    stop = tmp_path / "readiness-stop"

    async def scenario() -> None:
        with temporary_stderr() as stderr:
            with anyio.fail_after(60):
                async with open_mcp_session(stderr) as session:
                    started = assert_ok(await call_payload(session, {
                        "action": "run",
                        "classpath": str(tmp_path),
                        "main_class": "ReadinessMcpFixture",
                        "app_args": [str(ready_port), str(stop)],
                        "jdwp_port": jdwp_port,
                        "ready_port": ready_port,
                        "startup_wait_timeout_seconds": 0.1,
                    }))
                    assert started["status"] == "process_started"
                    assert started["process_state"] == "running"
                    assert started["startup_state"] == "starting"
                    assert started["startup_wait_timed_out"] is True
                    assert started["next_action"] == "status"

                    ready: dict[str, Any] = {}
                    with anyio.fail_after(10):
                        while ready.get("startup_state") != "ready":
                            ready = assert_ok(await call_payload(
                                session,
                                {"action": "status"},
                            ))
                            if ready.get("startup_state") != "ready":
                                await anyio.sleep(0.05)

                    assert ready["process_state"] == "running"
                    readiness = ready["readiness"]
                    assert readiness["type"] == "tcp_port"
                    assert readiness["host"] == "127.0.0.1"
                    assert readiness["port"] == ready_port
                    assert readiness["verified"] is True
                    observed_at = ready["ready_observed_at"]
                    stable = assert_ok(await call_payload(
                        session,
                        {"action": "status"},
                    ))
                    assert stable["startup_state"] == "ready"
                    assert stable["ready_observed_at"] == observed_at

                    assert_ok(await call_payload(
                        session,
                        {"action": "stop"},
                    ))
                    absent = assert_ok(await call_payload(
                        session,
                        {"action": "status"},
                    ))
                    assert absent["process_state"] == "absent"
                    assert "startup_state" not in absent

    anyio.run(scenario)


def test_project_path_launch_uses_jdt_and_reaches_tcp_readiness(
    tmp_path: Path,
) -> None:
    """The public MCP path imports IDEA, builds Maven, and launches classes."""
    require_real_mcp_java_e2e()
    if shutil.which("mvn") is None:
        pytest.skip("Maven is required for the project-launch E2E")

    project = tmp_path / "project"
    source = (
        project
        / "src"
        / "main"
        / "java"
        / "example"
        / "ProjectMcpFixture.java"
    )
    source.parent.mkdir(parents=True)
    source.write_text(
        """\
package example;

import java.net.ServerSocket;

public class ProjectMcpFixture {
    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(args[0]);
        try (ServerSocket server = new ServerSocket(port)) {
            while (true) {
                Thread.sleep(100);
            }
        }
    }
}
""",
        encoding="utf-8",
    )
    (project / "pom.xml").write_text(
        """\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>project-mcp-fixture</artifactId>
  <version>1.0.0</version>
  <properties>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
    <maven.compiler.source>1.8</maven.compiler.source>
    <maven.compiler.target>1.8</maven.compiler.target>
  </properties>
</project>
""",
        encoding="utf-8",
    )
    ready_port = reserve_local_port()
    jdwp_port = reserve_local_port()
    while jdwp_port == ready_port:
        jdwp_port = reserve_local_port()
    run_config = project / ".run" / "ProjectMcpFixture.xml"
    run_config.parent.mkdir()
    run_config.write_text(
        f"""\
<component name="ProjectRunConfigurationManager">
  <configuration name="ProjectMcpFixture" type="Application"
                 factoryName="Application">
    <option name="MAIN_CLASS_NAME" value="example.ProjectMcpFixture" />
    <option name="WORKING_DIRECTORY" value="$PROJECT_DIR$" />
    <option name="PROGRAM_PARAMETERS" value="{ready_port}" />
    <method v="2">
      <option name="Make" enabled="true" />
    </method>
  </configuration>
</component>
""",
        encoding="utf-8",
    )

    async def scenario() -> None:
        with temporary_stderr() as stderr:
            with anyio.fail_after(90):
                async with open_mcp_session(
                    stderr,
                    environment=_java8_mcp_environment(),
                ) as session:
                    started = assert_ok(await call_payload(session, {
                        "action": "run",
                        "project_path": str(project),
                        "launch_name": "ProjectMcpFixture",
                        "jdwp_port": jdwp_port,
                        "ready_port": ready_port,
                        "startup_wait_timeout_seconds": 10,
                    }))
                    assert started["status"] == "project_launch_started"
                    assert started["process_state"] == "absent"

                    active: dict[str, Any] | None = None
                    while active is None:
                        status = assert_ok(await call_payload(session, {
                            "action": "status",
                        }))
                        if status["launch_phase"] == "failed":
                            raise AssertionError(status)
                        if (
                            status["launch_phase"] == "runtime_active"
                            and status.get("fast_update", {}).get("available")
                            is True
                        ):
                            active = status
                            break
                        await anyio.sleep(0.1)

                    assert active["process_state"] == "running"
                    assert active["startup_state"] == "ready"
                    pid = int(active["pid"])
                    assert pid_is_alive(pid)

                    stopped = assert_ok(await call_payload(session, {
                        "action": "stop",
                    }))
                    assert stopped["status"] == "stopped"
                    await wait_until_async(lambda: not pid_is_alive(pid))

    anyio.run(scenario)


def test_jdt_workspace_reopens_and_reload_is_a_background_attempt(
    tmp_path: Path,
) -> None:
    require_real_mcp_java_e2e()
    if shutil.which("mvn") is None:
        pytest.skip("Maven is required for the persistent JDT E2E")

    project = tmp_path / "persistent-project"
    source = project / "src/main/java/example/PersistentFixture.java"
    source.parent.mkdir(parents=True)

    def source_text(value: str) -> str:
        return f"""\
package example;

import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;

public class PersistentFixture {{
    private static String message() {{ return "{value}"; }}
    public static void main(String[] args) throws Exception {{
        int port = Integer.parseInt(args[0]);
        try (ServerSocket server = new ServerSocket(
            port, 8, InetAddress.getByName("127.0.0.1")
        )) {{
            while (true) {{
                try (Socket socket = server.accept()) {{
                    int request = socket.getInputStream().read();
                    String response = request == 'P'
                        ? java.nio.file.Paths.get(PersistentFixture.class
                            .getProtectionDomain().getCodeSource().getLocation().toURI()).toString()
                        : message();
                    socket.getOutputStream().write(
                        response.getBytes(StandardCharsets.UTF_8)
                    );
                }}
            }}
        }}
    }}
}}
"""

    source.write_text(source_text("before"), encoding="utf-8")
    (project / "pom.xml").write_text(
        """\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId><artifactId>persistent</artifactId><version>1</version>
  <properties>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
    <maven.compiler.source>1.8</maven.compiler.source>
    <maven.compiler.target>1.8</maven.compiler.target>
  </properties>
</project>
""",
        encoding="utf-8",
    )
    ready_port = reserve_local_port()
    jdwp_port = reserve_local_port()
    while jdwp_port == ready_port:
        jdwp_port = reserve_local_port()
    run_config = project / ".run/PersistentFixture.xml"
    run_config.parent.mkdir()
    run_config.write_text(
        f"""\
<component name="ProjectRunConfigurationManager">
  <configuration name="PersistentFixture" type="Application" factoryName="Application">
    <option name="MAIN_CLASS_NAME" value="example.PersistentFixture" />
    <option name="WORKING_DIRECTORY" value="$PROJECT_DIR$" />
    <option name="PROGRAM_PARAMETERS" value="{ready_port}" />
    <method v="2"><option name="Make" enabled="true" /></method>
  </configuration>
</component>
""",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    java8_home = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if java8_home:
        environment["JAVA_HOME"] = java8_home
        environment["PATH"] = (
            str(Path(java8_home) / "bin")
            + os.pathsep
            + environment.get("PATH", "")
        )

    async def launch_and_wait(session: Any) -> dict[str, Any]:
        assert_ok(await call_payload(session, {
            "action": "run",
            "project_path": str(project),
            "launch_name": "PersistentFixture",
            "jdwp_port": jdwp_port,
            "ready_port": ready_port,
            "startup_wait_timeout_seconds": 5,
        }))
        with anyio.fail_after(90):
            while True:
                status = assert_ok(await call_payload(
                    session, {"action": "status"}
                ))
                if status.get("launch_phase") == "failed":
                    raise AssertionError(status)
                if (
                    status.get("launch_phase") == "runtime_active"
                    and status.get("compile_ready") is True
                ):
                    return status
                await anyio.sleep(0.05)

    def request_value(request: bytes = b"?") -> str:
        with socket.create_connection(("127.0.0.1", ready_port), timeout=3) as client:
            client.sendall(request)
            return client.makefile("r", encoding="utf-8").read()

    async def scenario() -> None:
        with temporary_stderr() as stderr:
            async with open_mcp_session(
                stderr, environment=environment
            ) as first_session:
                first = await launch_and_wait(first_session)
                assert first["jdt_bootstrap_reused"] is False
                assert first["probe_cache_reused"] is False
                assert first["jdt_bootstrap_build_kind"] == "FULL"
                states = list(
                    (tmp_path / "cache/jolink-runtime/jdt-workspaces").glob(
                        "*/state.json"
                    )
                )
                assert len(states) == 1
                workspace = states[0].parent
                assert (workspace / "source-index.json").is_file()
                assert Path(await anyio.to_thread.run_sync(request_value, b"P")) == (
                    workspace / "workspace/plain-fixture/bin"
                )
                assert not project.joinpath("target/classes").exists()
                assert_ok(await call_payload(first_session, {"action": "stop"}))

        # These persisted files are reused byte-for-byte, without rewriting.
        config_files = [workspace / name for name in (
            "worker-launch.json", "worker-classpath.private.txt", "configuration/config.ini",
            "workspace/plain-fixture/.classpath",
            "workspace/plain-fixture/.settings/org.eclipse.jdt.core.prefs",
        )]
        config_before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in config_files}
        compiler_options = config_files[-1].read_text(encoding="utf-8")
        assert not any(
            line.startswith("org.eclipse.jdt.core.compiler.problem.")
            and line.endswith(("=warning", "=info"))
            for line in compiler_options.splitlines()
        )

        source.write_text(source_text("startup-incremental"), encoding="utf-8")
        with temporary_stderr() as stderr:
            async with open_mcp_session(
                stderr, environment=environment
            ) as second_session:
                second = await launch_and_wait(second_session)
                assert second["jdt_bootstrap_reused"] is True
                assert second["probe_cache_reused"] is True
                assert "log_tail" not in second["build"]
                assert second["jdt_bootstrap_build_kind"] == "INCREMENTAL"
                assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in config_files} == config_before
                assert await anyio.to_thread.run_sync(request_value) == (
                    "startup-incremental"
                )
                assert not project.joinpath("target/classes").exists()

                source.write_text(source_text("after"), encoding="utf-8")
                with anyio.fail_after(1):
                    started = assert_ok(await call_payload(second_session, {
                        "action": "update",
                        "source_files": [
                            "src/main/java/example/PersistentFixture.java"
                        ],
                    }))
                assert started["status"] == "reload_started"
                terminal = await _await_reload(
                    second_session, started, timeout=30
                )
                assert terminal["ok"] is True
                assert terminal["status"] == "reloaded"
                assert terminal["applied"] is True
                assert await anyio.to_thread.run_sync(request_value) == "after"

                old_pid = int(terminal.get("pid") or second["pid"])
                restarting = assert_ok(await call_payload(second_session, {
                    "action": "restart",
                }))
                assert restarting["status"] == "restarting"
                with anyio.fail_after(30):
                    while True:
                        restarted = assert_ok(await call_payload(
                            second_session, {"action": "status"}
                        ))
                        if restarted.get("launch_phase") == "failed":
                            raise AssertionError(restarted)
                        if (
                            restarted.get("launch_phase") == "runtime_active"
                            and restarted.get("pid") != old_pid
                        ):
                            break
                        await anyio.sleep(0.05)
                assert await anyio.to_thread.run_sync(request_value) == (
                    "after"
                )

                reapplied = assert_ok(await call_payload(second_session, {
                    "action": "update",
                    "source_files": [
                        "src/main/java/example/PersistentFixture.java"
                    ],
                }))
                reapply_terminal = await _await_reload(
                    second_session, reapplied, timeout=30
                )
                assert reapply_terminal["status"] == "no_changes"
                assert reapply_terminal["applied"] is True
                assert await anyio.to_thread.run_sync(request_value) == "after"
                assert_ok(await call_payload(second_session, {"action": "stop"}))

        with temporary_stderr() as stderr:
            async with open_mcp_session(
                stderr, environment=environment
            ) as third_session:
                third = await launch_and_wait(third_session)
                assert third["probe_cache_reused"] is True
                assert third["jdt_bootstrap_reused"] is True
                assert third["jdt_bootstrap_build_kind"] is None
                assert "log_tail" not in third["build"]
                assert await anyio.to_thread.run_sync(request_value) == "after"
                assert_ok(await call_payload(third_session, {"action": "stop"}))

        source.write_text("package example; public class {\n", encoding="utf-8")
        await expect_failed_launch()
        # Reopening an unchanged failed workspace must report the actual
        # previous compile failure, not manufacture a successful no-op.
        await expect_failed_launch()

    async def expect_failed_launch() -> None:
        with temporary_stderr() as stderr:
            async with open_mcp_session(
                stderr, environment=environment
            ) as failed_session:
                assert_ok(await call_payload(failed_session, {
                    "action": "run",
                    "project_path": str(project),
                    "launch_name": "PersistentFixture",
                    "jdwp_port": jdwp_port,
                    "ready_port": ready_port,
                    "startup_wait_timeout_seconds": 5,
                }))
                with anyio.fail_after(30):
                    while True:
                        failed = assert_ok(await call_payload(
                            failed_session, {"action": "status"}
                        ))
                        assert failed.get("launch_phase") != "runtime_active", failed
                        if failed.get("launch_phase") == "failed":
                            break
                        await anyio.sleep(0.05)
                assert failed["launch_error"]["error_code"] == (
                    "JDT_STARTUP_COMPILE_FAILED"
                )
                assert failed["process_state"] == "absent"
                assert not failed.get("pid")

    anyio.run(scenario)


def test_project_update_hotswaps_method_body_without_writing_maven_output(
    tmp_path: Path,
) -> None:
    """One update changes live behavior while target/classes stays untouched."""
    require_real_mcp_java_e2e()
    if shutil.which("mvn") is None:
        pytest.skip("Maven is required for the project-update E2E")

    project = tmp_path / "update-project"
    source = (
        project
        / "src"
        / "main"
        / "java"
        / "example"
        / "UpdateMcpFixture.java"
    )
    source.parent.mkdir(parents=True)

    def source_text(value: str) -> str:
        return f"""\
package example;

import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;

public class UpdateMcpFixture {{
    private static String STATIC_VALUE = "stable";

    public static void main(String[] args) throws Exception {{
        int port = Integer.parseInt(args[0]);
        try (ServerSocket server = new ServerSocket(
            port, 16, InetAddress.getByName("127.0.0.1")
        )) {{
            while (true) {{
                try (Socket socket = server.accept()) {{
                    int request = socket.getInputStream().read();
                    String response = request == 'L'
                        ? LazyValue.value()
                        : request == 'S' ? STATIC_VALUE : message();
                    socket.getOutputStream().write(
                        response.getBytes(StandardCharsets.UTF_8)
                    );
                }}
            }}
        }}
    }}

    private static String message() {{
        return "{value}";
    }}
}}
"""

    initial_source = source_text("before-update")
    source.write_text(initial_source, encoding="utf-8")
    (source.parent / "LazyValue.java").write_text(
        """\
package example;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;

public class LazyValue {
    public static String value() throws Exception {
        try (InputStream stream = LazyValue.class.getResourceAsStream(
            "/lazy-value.txt"
        )) {
            if (stream == null) throw new IllegalStateException("missing resource");
            byte[] data = new byte[64];
            int count = stream.read(data);
            return new String(data, 0, count, StandardCharsets.UTF_8);
        }
    }
}
""",
        encoding="utf-8",
    )
    resources = project / "src/main/resources"
    resources.mkdir(parents=True)
    (resources / "lazy-value.txt").write_text(
        "lazy-value", encoding="utf-8"
    )
    breakpoint_line = next(
        index
        for index, line in enumerate(initial_source.splitlines(), start=1)
        if 'return "before-update";' in line
    )
    updated_source = source_text("after-update").replace(
        'return "after-update";',
        'String value = "after-update";\n        return value;',
    )
    second_updated_source = source_text("second-update").replace(
        'return "second-update";',
        'String value = "second-update";\n        return value;',
    )
    updated_breakpoint_line = next(
        index
        for index, line in enumerate(updated_source.splitlines(), start=1)
        if "return value;" in line
    )
    assert updated_breakpoint_line != breakpoint_line
    (project / "pom.xml").write_text(
        """\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>update-mcp-fixture</artifactId>
  <version>1.0.0</version>
  <properties>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
    <maven.compiler.source>1.8</maven.compiler.source>
    <maven.compiler.target>1.8</maven.compiler.target>
  </properties>
</project>
""",
        encoding="utf-8",
    )
    ready_port = reserve_local_port()
    jdwp_port = reserve_local_port()
    while jdwp_port == ready_port:
        jdwp_port = reserve_local_port()
    run_config = project / ".run" / "UpdateMcpFixture.xml"
    run_config.parent.mkdir()
    run_config.write_text(
        f"""\
<component name="ProjectRunConfigurationManager">
  <configuration name="UpdateMcpFixture" type="Application"
                 factoryName="Application">
    <option name="MAIN_CLASS_NAME" value="example.UpdateMcpFixture" />
    <option name="WORKING_DIRECTORY" value="$PROJECT_DIR$" />
    <option name="PROGRAM_PARAMETERS" value="{ready_port}" />
    <method v="2">
      <option name="Make" enabled="true" />
    </method>
  </configuration>
</component>
""",
        encoding="utf-8",
    )

    def request_value(payload: bytes = b"?") -> str:
        with socket.create_connection(
            ("127.0.0.1", ready_port),
            timeout=3,
        ) as client:
            client.sendall(payload)
            return client.recv(256).decode("utf-8")

    async def scenario() -> None:
        with temporary_stderr() as stderr:
            with anyio.fail_after(120):
                async with open_mcp_session(
                    stderr,
                    environment=_java8_mcp_environment(),
                ) as session:
                    async def reload_source() -> dict[str, Any]:
                        started = assert_ok(await call_payload(session, {
                            "action": "update",
                            "source_files": [
                                "src/main/java/example/UpdateMcpFixture.java",
                            ],
                        }))
                        assert started["status"] == "reload_started"
                        return await _await_reload(
                            session, started, timeout=30
                        )

                    assert_ok(await call_payload(session, {
                        "action": "run",
                        "project_path": str(project),
                        "launch_name": "UpdateMcpFixture",
                        "jdwp_port": jdwp_port,
                        "ready_port": ready_port,
                        "startup_wait_timeout_seconds": 10,
                    }))

                    active: dict[str, Any] | None = None
                    while active is None:
                        status = assert_ok(await call_payload(session, {
                            "action": "status",
                        }))
                        if status["launch_phase"] == "failed":
                            raise AssertionError(status)
                        if (
                            status["launch_phase"] == "runtime_active"
                            and status.get("fast_update", {}).get("available")
                            is True
                        ):
                            active = status
                            break
                        await anyio.sleep(0.1)

                    assert active["fast_update"]["available"] is True, (
                        active["fast_update"]
                    )
                    original_breakpoint = assert_ok(
                        await call_payload(session, {
                            "action": "breakpoint",
                            "bp_action": "set",
                            "class_pattern": "example.UpdateMcpFixture",
                            "line": breakpoint_line,
                        })
                    )
                    original_breakpoint_id = original_breakpoint[
                        "breakpoint_id"
                    ]
                    assert await anyio.to_thread.run_sync(request_value) == (
                        "before-update"
                    )
                    formal_class = (
                        project
                        / "target"
                        / "classes"
                        / "example"
                        / "UpdateMcpFixture.class"
                    )
                    assert not formal_class.exists()

                    source.write_text(
                        updated_source,
                        encoding="utf-8",
                    )
                    updated = await reload_source()
                    assert updated["status"] == "reloaded"
                    assert updated["persistence"] == "jdt_workspace"
                    assert updated["runtime_overlay_active"] is True
                    assert updated["runtime_overlay_state"] == "active"
                    assert updated["verification_state"] == "not_verified"
                    assert updated["restart_loses_update"] is False
                    assert updated["restart_will_discard_overlay"] is False
                    assert updated["stale_breakpoint_ids"] == [
                        original_breakpoint_id
                    ]
                    assert updated["breakpoint_refresh_state"] == "partial"
                    unchanged = await reload_source()
                    assert unchanged["status"] == "no_changes"

                    listed = assert_ok(await call_payload(session, {
                        "action": "breakpoint",
                        "bp_action": "list",
                    }))
                    assert len(listed["breakpoints"]) == 1
                    stale_definition = listed["breakpoints"][0]
                    assert stale_definition["breakpoint_id"] == (
                        original_breakpoint_id
                    )
                    assert stale_definition["stale"] is True
                    assert stale_definition["stale_reason"] == (
                        "CLASS_REDEFINED_BREAKPOINT_REQUIRES_RESET"
                    )
                    stale_wait = await call_payload(session, {
                        "action": "wait_event",
                        "timeout": 1,
                    })
                    assert stale_wait["ok"] is False
                    assert stale_wait["error_code"] == (
                        "BREAKPOINT_DEFINITION_STALE"
                    )

                    assert_ok(await call_payload(session, {
                        "action": "breakpoint",
                        "bp_action": "remove",
                        "breakpoint_id": original_breakpoint_id,
                    }))
                    replacement_breakpoint = assert_ok(
                        await call_payload(session, {
                            "action": "breakpoint",
                            "bp_action": "set",
                            "class_pattern": "example.UpdateMcpFixture",
                            "line": updated_breakpoint_line,
                        })
                    )
                    assert replacement_breakpoint["breakpoint_id"] != (
                        original_breakpoint_id
                    )
                    assert_ok(await call_payload(session, {
                        "action": "breakpoint",
                        "bp_action": "remove",
                        "breakpoint_id": replacement_breakpoint[
                            "breakpoint_id"
                        ],
                    }))

                    assert not formal_class.exists()
                    assert await anyio.to_thread.run_sync(request_value) == (
                        "after-update"
                    )
                    observed = assert_ok(await call_payload(session, {
                        "action": "status",
                    }))
                    assert observed["runtime_overlay_active"] is True
                    assert observed["code_revision"] == 1
                    assert observed["generation"] == 1

                    source.write_text(second_updated_source, encoding="utf-8")
                    second_update = await reload_source()
                    assert second_update["status"] == "reloaded"
                    second_status = assert_ok(await call_payload(session, {
                        "action": "status",
                    }))
                    assert second_status["generation"] == 1
                    assert await anyio.to_thread.run_sync(request_value) == (
                        "second-update"
                    )
                    assert await anyio.to_thread.run_sync(
                        request_value, b"L"
                    ) == "lazy-value"

                    source.write_text(
                        second_updated_source.replace(
                            "public class UpdateMcpFixture {",
                            (
                                "public class UpdateMcpFixture {\n"
                                "    private int unsupportedField;"
                            ),
                        ),
                        encoding="utf-8",
                    )
                    rejected = await reload_source()
                    assert rejected["ok"] is False
                    assert rejected["error_code"] == "RELOAD_REQUIRES_RELAUNCH"
                    assert rejected["reason_code"] == "HOT_SWAP_REJECTED"
                    assert rejected["runtime_code_state"] == "unchanged"
                    assert rejected["runtime_overlay_active"] is True
                    assert rejected["code_revision"] == 2
                    assert not formal_class.exists()
                    assert await anyio.to_thread.run_sync(request_value) == (
                        "second-update"
                    )

                    source.write_text(
                        second_updated_source.replace(
                            'STATIC_VALUE = "stable"',
                            'STATIC_VALUE = "changed"',
                        ),
                        encoding="utf-8",
                    )
                    static_applied = await reload_source()
                    assert static_applied["ok"] is True
                    assert static_applied["framework_state_refreshed"] is False
                    assert static_applied["code_revision"] == 3
                    assert await anyio.to_thread.run_sync(request_value, b"S") == "stable"
                    assert not formal_class.exists()
                    assert await anyio.to_thread.run_sync(request_value) == (
                        "second-update"
                    )

                    restarting = assert_ok(await call_payload(session, {
                        "action": "restart",
                    }))
                    assert restarting["status"] == "restarting"
                    assert restarting["applied"] is None
                    while True:
                        restarted_status = assert_ok(
                            await call_payload(session, {"action": "status"})
                        )
                        if restarted_status["launch_phase"] == "failed":
                            raise AssertionError(restarted_status)
                        if restarted_status["launch_phase"] == "runtime_active":
                            break
                        await anyio.sleep(0.1)
                    assert restarted_status["generation"] == 1
                    assert await anyio.to_thread.run_sync(request_value) == (
                        "second-update"
                    )
                    # Restart loads compiled output, not uncompiled source edits.
                    assert await anyio.to_thread.run_sync(request_value, b"S") == "changed"

                    assert_ok(await call_payload(session, {"action": "stop"}))

    anyio.run(scenario)


def test_project_path_reactor_launch_reports_deferred_jdt_support(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Multi-module JDT startup remains an explicit deferred capability."""
    require_real_mcp_java_e2e()
    maven = shutil.which("mvn")
    if maven is None:
        pytest.skip("Maven is required for the project-launch E2E")

    project = tmp_path / "reactor"
    group_id = f"example.jolink.e2e.r{uuid.uuid4().hex}"
    repository_result = subprocess.run(
        [
            maven,
            "--batch-mode",
            "-q",
            "help:evaluate",
            "-Dexpression=settings.localRepository",
            "-DforceStdout",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    repository_text = next(
        (
            line.strip()
            for line in reversed(repository_result.stdout.splitlines())
            if line.strip() and not line.lstrip().startswith("[")
        ),
        "",
    )
    if not repository_text:
        pytest.skip("Maven local repository could not be resolved")
    local_repository = Path(repository_text).expanduser().resolve(
        strict=False
    )
    installed_group = local_repository.joinpath(*group_id.split("."))
    request.addfinalizer(
        lambda: shutil.rmtree(installed_group, ignore_errors=True)
    )
    shared_source = (
        project
        / "shared"
        / "src"
        / "main"
        / "java"
        / "example"
        / "SharedMessage.java"
    )
    shared_source.parent.mkdir(parents=True)
    shared_source.write_text(
        """\
package example;

    public final class SharedMessage {
    private SharedMessage() {}

    public static String value() {
        return "stale-installed-value";
    }
}
""",
        encoding="utf-8",
    )
    app_source = (
        project
        / "app"
        / "src"
        / "main"
        / "java"
        / "example"
        / "ReactorMcpFixture.java"
    )
    app_source.parent.mkdir(parents=True)
    installed_app_source = """\
package example;

import java.net.ServerSocket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;

public class ReactorMcpFixture {
    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(args[0]);
        Files.write(
            Paths.get(args[1]),
            message().getBytes(StandardCharsets.UTF_8)
        );
        try (ServerSocket server = new ServerSocket(port)) {
            while (true) {
                Thread.sleep(100);
            }
        }
    }

    private static String message() {
        return SharedMessage.value();
    }
}
"""
    app_source.write_text(installed_app_source, encoding="utf-8")
    (project / "pom.xml").write_text(
        f"""\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>{group_id}</groupId>
  <artifactId>reactor-root</artifactId>
  <version>1.0.0</version>
  <packaging>pom</packaging>
  <modules>
    <module>shared</module>
    <module>app</module>
  </modules>
  <properties>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
    <maven.compiler.source>1.8</maven.compiler.source>
    <maven.compiler.target>1.8</maven.compiler.target>
  </properties>
</project>
""",
        encoding="utf-8",
    )
    (project / "shared" / "pom.xml").write_text(
        f"""\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>{group_id}</groupId>
    <artifactId>reactor-root</artifactId>
    <version>1.0.0</version>
  </parent>
  <artifactId>shared</artifactId>
</project>
""",
        encoding="utf-8",
    )
    (project / "app" / "pom.xml").write_text(
        f"""\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>{group_id}</groupId>
    <artifactId>reactor-root</artifactId>
    <version>1.0.0</version>
  </parent>
  <artifactId>app</artifactId>
  <dependencies>
    <dependency>
      <groupId>{group_id}</groupId>
      <artifactId>shared</artifactId>
      <version>${{project.version}}</version>
    </dependency>
  </dependencies>
</project>
""",
        encoding="utf-8",
    )
    # Seed an older sibling artifact in the exact local repository joLink
    # will use.  The later joLink build intentionally stops at ``compile``;
    # observing the fresh value therefore proves Maven's reactor workspace
    # output took precedence over this installed JAR.
    subprocess.run(
        [
            maven,
            "--batch-mode",
            "--fail-fast",
            "-T",
            "1",
            "-DskipTests",
            "-f",
            str(project / "pom.xml"),
            "install",
        ],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    shared_source.write_text(
        """\
package example;

public final class SharedMessage {
    private SharedMessage() {}

    public static String value() {
        return "fresh-workspace-value";
    }

    public static String freshOnly() {
        return "fresh-workspace-value";
    }
}
""",
        encoding="utf-8",
    )
    fresh_app_source = installed_app_source.replace(
        "return SharedMessage.value();",
        "return SharedMessage.freshOnly();",
    )
    app_source.write_text(fresh_app_source, encoding="utf-8")

    ready_port = reserve_local_port()
    jdwp_port = reserve_local_port()
    while jdwp_port == ready_port:
        jdwp_port = reserve_local_port()
    marker = tmp_path / "reactor-workspace-value"
    run_config = project / ".run" / "ReactorMcpFixture.xml"
    run_config.parent.mkdir()
    run_config.write_text(
        f"""\
<component name="ProjectRunConfigurationManager">
  <configuration name="ReactorMcpFixture" type="Application"
                 factoryName="Application">
    <option name="MAIN_CLASS_NAME" value="example.ReactorMcpFixture" />
    <module name="app" />
    <option name="WORKING_DIRECTORY" value="$PROJECT_DIR$" />
    <option name="PROGRAM_PARAMETERS"
            value="{ready_port} &quot;{marker}&quot;" />
    <method v="2">
      <option name="Make" enabled="true" />
    </method>
  </configuration>
</component>
""",
        encoding="utf-8",
    )

    async def scenario() -> None:
        with temporary_stderr() as stderr:
            with anyio.fail_after(120):
                async with open_mcp_session(stderr) as session:
                    started = assert_ok(await call_payload(session, {
                        "action": "run",
                        "project_path": str(project),
                        "launch_name": "ReactorMcpFixture",
                        "jdwp_port": jdwp_port,
                        "ready_port": ready_port,
                        "startup_wait_timeout_seconds": 10,
                    }))
                    assert started["status"] == "project_launch_started"

                    while True:
                        status = assert_ok(await call_payload(session, {
                            "action": "status",
                        }))
                        if status["launch_phase"] == "failed":
                            break
                        await anyio.sleep(0.1)
                    assert status["launch_error"]["error_code"] == (
                        "JDT_MULTI_MODULE_NOT_IMPLEMENTED"
                    )
                    assert status["process_state"] == "absent"

    anyio.run(scenario)
