from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

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

                    trigger.write_text("go", encoding="utf-8")
                    hit = assert_ok(await call_payload(session, {
                        "action": "wait_event",
                        "timeout": 10,
                    }))
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
                    trigger.write_text("go", encoding="utf-8")
                    hit = assert_ok(await call_payload(session, {
                        "action": "wait_event",
                        "timeout": 10,
                    }))
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
                    resumed = assert_ok(await call_payload(session, {
                        "action": "resume",
                        "suspension_id": hit_holder["suspension_id"],
                    }))
                    assert resumed["status"] == "resumed"
                    await wait_until_async(marker.exists)

                    assert_ok(await call_payload(session, {"action": "stop"}))

    anyio.run(scenario)
