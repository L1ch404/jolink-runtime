from __future__ import annotations

import subprocess
import threading
import time
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


class _DaemonHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


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


def test_http_response_headers_can_arrive_before_delayed_real_jvm_hit(
    tmp_path: Path,
) -> None:
    """A completed trigger response does not end the armed Runtime wait."""
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

                        armed = assert_ok(await call_payload(session, {
                            "action": "wait_event",
                            "wait_mode": "arm",
                            "timeout": 10,
                            "http_trigger": {
                                "method": "POST",
                                "url": url,
                                "timeout_seconds": 5,
                            },
                        }))
                        hit = assert_ok(await call_payload(session, {
                            "action": "wait_event",
                            "wait_mode": "await",
                            "wait_handle": armed["wait_handle"],
                            "timeout": 10,
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
