from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator

import anyio

from jolink_runtime.core.wait_state import WaitControl
from jolink_runtime.server.http_trigger import (
    HTTPTriggerValidationError,
    parse_http_trigger,
)
from jolink_runtime.server.mcp_server import RuntimeMCPBoundary


class _DaemonHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


@contextmanager
def _http_server(
    callback: Callable[[BaseHTTPRequestHandler], None],
) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            callback(self)

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


def _send_response(handler: BaseHTTPRequestHandler, status: int = 204) -> None:
    handler.send_response(status)
    handler.end_headers()


class _TriggerDispatcher:
    def __init__(self, *, immediate_result: bool = False) -> None:
        self.immediate_result = immediate_result
        self.armed = threading.Event()
        self.event_triggered = threading.Event()
        self.response_release = threading.Event()
        self.cancel_seen = threading.Event()
        self.suspension_active = False
        self.calls: list[str] = []
        self.settled = 0

    def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        session_key: str = "default",
        wait_control: WaitControl | None = None,
    ) -> dict[str, Any]:
        action = str((arguments or {}).get("action", tool_name))
        self.calls.append(action)
        if action == "resume":
            self.suspension_active = False
            self.response_release.set()
            return {"ok": True, "status": "resumed"}
        if action != "wait_event":
            return {"ok": True, "status": action}

        assert wait_control is not None
        wait_control.mark_armed(breakpoint_ids=["bp_001"])
        self.armed.set()
        if not self.immediate_result:
            while not self.event_triggered.wait(0.005):
                if wait_control.cancelled:
                    self.cancel_seen.set()
                    return {"ok": True, "status": "internally_cancelled"}
        self.suspension_active = True
        return {
            "ok": True,
            "status": "breakpoint_hit",
            "breakpoint_id": "bp_001",
            "suspension_id": "susp_http_trigger",
        }

    def settle_cancelled_wait(
        self,
        wait_control: WaitControl,
        *,
        session_key: str = "default",
    ) -> bool:
        assert wait_control.worker_done is True
        self.settled += 1
        self.suspension_active = False
        self.response_release.set()
        return True

    def interrupt_wait(self, session_key: str = "default") -> bool:
        self.response_release.set()
        return True

    def close_session(self, session_key: str = "default") -> bool:
        self.response_release.set()
        return True

    def force_close_session(self, session_key: str = "default") -> bool:
        self.response_release.set()
        return True

    def wait_for_close_session(
        self,
        session_key: str = "default",
        timeout: float | None = None,
    ) -> bool:
        return True


class _ReadinessDispatcher(_TriggerDispatcher):
    def __init__(
        self,
        startup_state: str,
        *,
        process_state: str = "running",
    ) -> None:
        super().__init__()
        self.startup_state = startup_state
        self.process_state = process_state

    def startup_observation(
        self,
        session_key: str = "default",
    ) -> dict[str, Any]:
        observation: dict[str, Any] = {
            "process_state": self.process_state,
            "startup_state": self.startup_state,
            "startup_elapsed_ms": 30_000,
            "readiness_configured": self.startup_state != "unverified",
        }
        if self.startup_state != "unverified":
            observation["readiness"] = {
                "type": "tcp_port",
                "host": "127.0.0.1",
                "port": 8080,
                "verified": self.startup_state == "ready",
            }
        return observation


class _TerminalWaitDispatcher(_TriggerDispatcher):
    def __init__(self) -> None:
        super().__init__()
        self.finish_wait = threading.Event()

    def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        session_key: str = "default",
        wait_control: WaitControl | None = None,
    ) -> dict[str, Any]:
        action = str((arguments or {}).get("action", tool_name))
        if action != "wait_event":
            return super().dispatch(
                tool_name,
                arguments,
                session_key=session_key,
                wait_control=wait_control,
            )
        assert wait_control is not None
        self.calls.append(action)
        wait_control.mark_armed(breakpoint_ids=["bp_001"])
        self.armed.set()
        self.finish_wait.wait(2)
        return {"ok": True, "status": "timeout"}


class _ExceptionTriggerDispatcher(_ReadinessDispatcher):
    def __init__(self) -> None:
        super().__init__("ready")

    def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        session_key: str = "default",
        wait_control: WaitControl | None = None,
    ) -> dict[str, Any]:
        action = str((arguments or {}).get("action", tool_name))
        if action != "wait_event":
            return super().dispatch(
                tool_name,
                arguments,
                session_key=session_key,
                wait_control=wait_control,
            )

        assert wait_control is not None
        self.calls.append(action)
        wait_control.mark_armed(exception_ids=[7])
        self.armed.set()
        while not self.event_triggered.wait(0.005):
            if wait_control.cancelled:
                self.cancel_seen.set()
                return {"ok": True, "status": "internally_cancelled"}
        self.suspension_active = True
        return {
            "ok": True,
            "status": "exception_hit",
            "event_type": "exception",
            "exception_id": 7,
            "suspension_id": "susp_http_exception",
        }


def _arm_arguments(url: str) -> dict[str, Any]:
    return {
        "action": "wait_event",
        "wait_mode": "arm",
        "timeout": 5,
        "http_trigger": {
            "method": "POST",
            "url": url,
            "headers": {
                "Authorization": "Bearer secret-not-for-output",
            },
            "json_body": {"id": 7},
            "timeout_seconds": 2,
        },
    }


def _blocking_arguments(url: str) -> dict[str, Any]:
    arguments = _arm_arguments(url)
    arguments["wait_mode"] = "blocking"
    return arguments


def test_blocking_http_trigger_composes_arm_trigger_and_await() -> None:
    dispatcher = _ReadinessDispatcher("ready")
    request_seen = threading.Event()
    request_done = threading.Event()

    def handle(handler: BaseHTTPRequestHandler) -> None:
        assert dispatcher.armed.is_set()
        request_seen.set()
        dispatcher.event_triggered.set()
        dispatcher.response_release.wait(2)
        _send_response(handler, 200)
        request_done.set()

    with _http_server(handle) as url:
        boundary = RuntimeMCPBoundary(dispatcher)

        async def scenario() -> None:
            hit = await boundary.call_tool(
                "java_runtime",
                _blocking_arguments(url),
            )
            payload = dict(hit.structuredContent or {})

            assert hit.isError is False
            assert payload["status"] == "breakpoint_hit"
            assert payload["suspension_id"] == "susp_http_trigger"
            assert payload["http_trigger"]["status"] == "running"
            assert request_seen.is_set()
            wait_handle = str(payload["wait_handle"])
            assert boundary._find_wait(wait_handle) is None

            consumed = await boundary.call_tool(
                "java_runtime",
                {
                    "action": "wait_event",
                    "wait_mode": "await",
                    "wait_handle": wait_handle,
                    "timeout": 1,
                },
            )
            assert consumed.isError is True
            assert consumed.structuredContent["error_code"] == (
                "WAIT_HANDLE_NOT_FOUND"
            )

            resumed = await boundary.call_tool(
                "java_runtime",
                {
                    "action": "resume",
                    "suspension_id": payload["suspension_id"],
                },
            )
            assert resumed.structuredContent["status"] == "resumed"
            with anyio.fail_after(1):
                while not request_done.is_set():
                    await anyio.sleep(0.005)

        anyio.run(scenario)


def test_http_trigger_uses_blocking_when_wait_mode_is_omitted() -> None:
    dispatcher = _ReadinessDispatcher("ready")

    def handle(handler: BaseHTTPRequestHandler) -> None:
        assert dispatcher.armed.is_set()
        dispatcher.event_triggered.set()
        _send_response(handler)

    with _http_server(handle) as url:
        boundary = RuntimeMCPBoundary(dispatcher)

        async def scenario() -> None:
            arguments = _blocking_arguments(url)
            arguments.pop("wait_mode")
            hit = await boundary.call_tool("java_runtime", arguments)
            assert hit.structuredContent["status"] == "breakpoint_hit"
            await boundary.call_tool(
                "java_runtime",
                {
                    "action": "resume",
                    "suspension_id": hit.structuredContent["suspension_id"],
                },
            )

        anyio.run(scenario)


def test_blocking_http_trigger_returns_and_consumes_exception_hit() -> None:
    dispatcher = _ExceptionTriggerDispatcher()
    request_done = threading.Event()

    def handle(handler: BaseHTTPRequestHandler) -> None:
        assert dispatcher.armed.is_set()
        dispatcher.event_triggered.set()
        dispatcher.response_release.wait(2)
        _send_response(handler)
        request_done.set()

    with _http_server(handle) as url:
        boundary = RuntimeMCPBoundary(dispatcher)

        async def scenario() -> None:
            hit = await boundary.call_tool(
                "java_runtime",
                _blocking_arguments(url),
            )
            payload = dict(hit.structuredContent or {})

            assert hit.isError is False
            assert payload["status"] == "exception_hit"
            assert payload["event_type"] == "exception"
            assert payload["exception_id"] == 7
            assert payload["suspension_id"] == "susp_http_exception"
            assert boundary._find_wait(str(payload["wait_handle"])) is None

            resumed = await boundary.call_tool(
                "java_runtime",
                {
                    "action": "resume",
                    "suspension_id": payload["suspension_id"],
                },
            )
            assert resumed.structuredContent["status"] == "resumed"
            with anyio.fail_after(1):
                while not request_done.is_set():
                    await anyio.sleep(0.005)

        anyio.run(scenario)


def test_http_trigger_is_sent_only_after_arm_and_does_not_block_arm() -> None:
    dispatcher = _ReadinessDispatcher("ready")
    request_seen = threading.Event()
    request_done = threading.Event()
    received_body: list[dict[str, Any]] = []

    def handle(handler: BaseHTTPRequestHandler) -> None:
        assert dispatcher.armed.is_set()
        length = int(handler.headers.get("Content-Length", "0"))
        received_body.append(json.loads(handler.rfile.read(length)))
        request_seen.set()
        dispatcher.event_triggered.set()
        dispatcher.response_release.wait(2)
        _send_response(handler, 200)
        request_done.set()

    with _http_server(handle) as url:
        boundary = RuntimeMCPBoundary(dispatcher)

        async def scenario() -> None:
            armed = await boundary.call_tool(
                "java_runtime",
                _arm_arguments(url),
            )
            armed_payload = dict(armed.structuredContent or {})
            assert armed_payload["status"] == "armed"
            assert armed_payload["required_next_action"] == {
                "action": "wait_event",
                "wait_mode": "await",
                "wait_handle": armed_payload["wait_handle"],
            }
            assert armed_payload["http_trigger"]["status"] in {
                "running",
                "response_headers_received",
            }
            assert "Authorization" not in json.dumps(armed_payload)
            assert "secret-not-for-output" not in json.dumps(armed_payload)

            with anyio.fail_after(1):
                while not request_seen.is_set():
                    await anyio.sleep(0.005)
            assert received_body == [{"id": 7}]

            hit = await boundary.call_tool(
                "java_runtime",
                {
                    "action": "wait_event",
                    "wait_mode": "await",
                    "wait_handle": armed_payload["wait_handle"],
                    "timeout": 1,
                },
            )
            hit_payload = dict(hit.structuredContent or {})
            assert hit_payload["status"] == "breakpoint_hit"
            assert hit_payload["suspension_id"] == "susp_http_trigger"
            assert hit_payload["http_trigger"]["status"] == "running"

            resumed = await boundary.call_tool(
                "java_runtime",
                {
                    "action": "resume",
                    "suspension_id": hit_payload["suspension_id"],
                },
            )
            assert resumed.structuredContent["status"] == "resumed"
            with anyio.fail_after(1):
                while not request_done.is_set():
                    await anyio.sleep(0.005)

        anyio.run(scenario)


def test_http_trigger_is_rejected_while_application_is_starting() -> None:
    dispatcher = _ReadinessDispatcher("starting")
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario() -> None:
        result = await boundary.call_tool(
            "java_runtime",
            _arm_arguments("http://127.0.0.1:8080/trigger"),
        )
        payload = dict(result.structuredContent or {})

        assert result.isError is True
        assert payload["error_code"] == "APPLICATION_NOT_READY"
        assert payload["startup_state"] == "starting"
        assert payload["http_trigger_sent"] is False
        assert payload["next_action"] == "status"
        assert dispatcher.calls == []
        assert dispatcher.armed.is_set() is False

    anyio.run(scenario)


def test_blocking_http_trigger_is_rejected_before_waiter_while_starting() -> None:
    dispatcher = _ReadinessDispatcher("starting")
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario() -> None:
        result = await boundary.call_tool(
            "java_runtime",
            _blocking_arguments("http://127.0.0.1:8080/trigger"),
        )
        payload = dict(result.structuredContent or {})

        assert result.isError is True
        assert payload["error_code"] == "APPLICATION_NOT_READY"
        assert payload["http_trigger_sent"] is False
        assert dispatcher.calls == []
        assert boundary._active_background_waiter() is None

    anyio.run(scenario)


def test_http_trigger_is_rejected_during_project_build_without_fake_readiness() -> None:
    class BuildingDispatcher(_TriggerDispatcher):
        def startup_observation(
            self,
            session_key: str = "default",
        ) -> dict[str, Any]:
            return {
                "process_state": "absent",
                "launch_phase": "compiling",
            }

    dispatcher = BuildingDispatcher()
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario() -> None:
        result = await boundary.call_tool(
            "java_runtime",
            _blocking_arguments("http://127.0.0.1:8080/trigger"),
        )
        payload = dict(result.structuredContent or {})

        assert result.isError is True
        assert payload["error_code"] == "APPLICATION_NOT_READY"
        assert payload["process_state"] == "absent"
        assert payload["launch_phase"] == "compiling"
        assert "startup_state" not in payload
        assert payload["http_trigger_sent"] is False
        assert dispatcher.calls == []
        assert boundary._active_background_waiter() is None

    anyio.run(scenario)


def test_unverified_readiness_allows_http_trigger_with_warning() -> None:
    dispatcher = _ReadinessDispatcher("unverified")

    def handle(handler: BaseHTTPRequestHandler) -> None:
        dispatcher.event_triggered.set()
        _send_response(handler)

    with _http_server(handle) as url:
        boundary = RuntimeMCPBoundary(dispatcher)

        async def scenario() -> None:
            armed = await boundary.call_tool(
                "java_runtime",
                _arm_arguments(url),
            )
            payload = dict(armed.structuredContent or {})

            assert armed.isError is False
            assert payload["status"] == "armed"
            assert any(
                "readiness is unverified" in warning.lower()
                for warning in payload["warnings"]
            )

            hit = await boundary.call_tool(
                "java_runtime",
                {
                    "action": "wait_event",
                    "wait_mode": "await",
                    "wait_handle": payload["wait_handle"],
                    "timeout": 1,
                },
            )
            assert hit.structuredContent["status"] == "breakpoint_hit"
            resumed = await boundary.call_tool(
                "java_runtime",
                {
                    "action": "resume",
                    "suspension_id": hit.structuredContent["suspension_id"],
                },
            )
            assert resumed.structuredContent["status"] == "resumed"

        anyio.run(scenario)


def test_blocking_result_preserves_unverified_readiness_warning() -> None:
    dispatcher = _ReadinessDispatcher("unverified")

    def handle(handler: BaseHTTPRequestHandler) -> None:
        dispatcher.event_triggered.set()
        _send_response(handler)

    with _http_server(handle) as url:
        boundary = RuntimeMCPBoundary(dispatcher)

        async def scenario() -> None:
            hit = await boundary.call_tool(
                "java_runtime",
                _blocking_arguments(url),
            )
            payload = dict(hit.structuredContent or {})

            assert payload["status"] == "breakpoint_hit"
            assert any(
                "readiness is unverified" in warning.lower()
                for warning in payload["warnings"]
            )
            await boundary.call_tool(
                "java_runtime",
                {
                    "action": "resume",
                    "suspension_id": payload["suspension_id"],
                },
            )

        anyio.run(scenario)


def test_http_response_without_event_keeps_wait_active() -> None:
    dispatcher = _TriggerDispatcher()

    def handle(handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", "0"))
        handler.rfile.read(length)
        _send_response(handler)

    with _http_server(handle) as url:
        boundary = RuntimeMCPBoundary(dispatcher)

        async def scenario() -> None:
            armed = await boundary.call_tool("java_runtime", _arm_arguments(url))
            wait_handle = str(armed.structuredContent["wait_handle"])
            waiting = await boundary.call_tool(
                "java_runtime",
                {
                    "action": "wait_event",
                    "wait_mode": "await",
                    "wait_handle": wait_handle,
                    "timeout": 0.2,
                },
            )
            payload = dict(waiting.structuredContent or {})
            assert payload["status"] == "waiting"
            assert payload["http_trigger"]["status"] in {
                "running",
                "response_headers_received",
            }
            assert boundary._active_background_waiter() is not None

            cleaned = await boundary.call_tool(
                "java_runtime",
                {"action": "cleanup_debug_state"},
            )
            assert cleaned.structuredContent["status"] == "cleanup_debug_state"
            assert dispatcher.cancel_seen.is_set()

        anyio.run(scenario)


def test_blocking_await_timeout_keeps_handle_for_later_event() -> None:
    dispatcher = _TriggerDispatcher()

    def handle(handler: BaseHTTPRequestHandler) -> None:
        _send_response(handler)

    with _http_server(handle) as url:
        boundary = RuntimeMCPBoundary(dispatcher)

        async def scenario() -> None:
            arguments = _blocking_arguments(url)
            arguments["timeout"] = 0.1
            waiting = await boundary.call_tool("java_runtime", arguments)
            payload = dict(waiting.structuredContent or {})

            assert payload["status"] == "waiting"
            wait_handle = str(payload["wait_handle"])
            assert boundary._find_wait(wait_handle) is not None
            # The Runtime await deadline and HTTP client run concurrently.
            # A slower scheduler may publish the waiting result just before
            # the client records response headers; both snapshots are valid.
            assert payload["http_trigger"]["status"] in {
                "running",
                "response_headers_received",
            }

            dispatcher.event_triggered.set()
            hit = await boundary.call_tool(
                "java_runtime",
                {
                    "action": "wait_event",
                    "wait_mode": "await",
                    "wait_handle": wait_handle,
                    "timeout": 1,
                },
            )
            assert hit.structuredContent["status"] == "breakpoint_hit"
            await boundary.call_tool(
                "java_runtime",
                {
                    "action": "resume",
                    "suspension_id": hit.structuredContent["suspension_id"],
                },
            )

        anyio.run(scenario)


def test_definite_http_connection_failure_cancels_runtime_wait() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    dispatcher = _TriggerDispatcher()
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario() -> None:
        blocking_arguments = _blocking_arguments(
            f"http://127.0.0.1:{port}/missing"
        )
        blocking_arguments["http_trigger"]["timeout_seconds"] = 0.5
        failed = await boundary.call_tool(
            "java_runtime",
            blocking_arguments,
        )
        payload = dict(failed.structuredContent or {})
        assert failed.isError is True
        assert payload["error_code"] == "HTTP_TRIGGER_FAILED"
        assert payload["http_trigger"]["error_code"] == (
            "HTTP_TRIGGER_CONNECTION_FAILED"
        )
        assert dispatcher.cancel_seen.is_set()
        assert dispatcher.settled == 1

    try:
        anyio.run(scenario)
    finally:
        probe.close()


def test_cancelling_blocking_http_trigger_cleans_wait_and_client() -> None:
    dispatcher = _TriggerDispatcher()
    request_seen = threading.Event()

    def handle(handler: BaseHTTPRequestHandler) -> None:
        request_seen.set()
        dispatcher.response_release.wait(2)
        _send_response(handler)

    with _http_server(handle) as url:
        boundary = RuntimeMCPBoundary(
            dispatcher,
            cancellation_grace_seconds=0.5,
        )

        async def scenario() -> None:
            holder: dict[str, anyio.CancelScope] = {}
            caller_done = anyio.Event()

            async def blocking_call() -> None:
                with anyio.CancelScope() as scope:
                    holder["scope"] = scope
                    try:
                        await boundary.call_tool(
                            "java_runtime",
                            _blocking_arguments(url),
                        )
                    finally:
                        caller_done.set()

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(blocking_call)
                with anyio.fail_after(1):
                    while not request_seen.is_set():
                        await anyio.sleep(0.005)
                holder["scope"].cancel()
                with anyio.fail_after(1):
                    await caller_done.wait()

            assert dispatcher.cancel_seen.is_set()
            assert dispatcher.settled == 1
            assert boundary._active_background_waiter() is None
            with anyio.fail_after(3):
                while boundary._http_triggers:
                    await anyio.sleep(0.005)

        anyio.run(scenario)


def test_cleanup_preempts_passive_await_with_running_http_trigger() -> None:
    dispatcher = _TriggerDispatcher()
    request_seen = threading.Event()

    def handle(handler: BaseHTTPRequestHandler) -> None:
        request_seen.set()
        dispatcher.response_release.wait(2)
        _send_response(handler)

    with _http_server(handle) as url:
        boundary = RuntimeMCPBoundary(
            dispatcher,
            cancellation_grace_seconds=0.5,
        )

        async def scenario() -> None:
            armed = await boundary.call_tool("java_runtime", _arm_arguments(url))
            wait_handle = str(armed.structuredContent["wait_handle"])
            control = boundary._find_wait(wait_handle)
            assert control is not None
            trigger = control.trigger_control
            assert trigger is not None
            await_result: list[dict[str, Any]] = []

            async def await_event() -> None:
                result = await boundary.call_tool(
                    "java_runtime",
                    {
                        "action": "wait_event",
                        "wait_mode": "await",
                        "wait_handle": wait_handle,
                        "timeout": 30,
                    },
                )
                await_result.append(dict(result.structuredContent or {}))

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(await_event)
                with anyio.fail_after(1):
                    while not request_seen.is_set():
                        await anyio.sleep(0.005)
                with anyio.fail_after(1):
                    started_at = time.monotonic()
                    cleaned = await boundary.call_tool(
                        "java_runtime",
                        {"action": "cleanup_debug_state"},
                    )
                    elapsed = time.monotonic() - started_at
                assert cleaned.structuredContent["status"] == (
                    "cleanup_debug_state"
                )
                assert elapsed < 0.4
                cleanup_state = cleaned.structuredContent[
                    "http_trigger_cleanup_state"
                ]
                assert cleanup_state in {"complete", "settling"}
                assert trigger.snapshot()[
                    "client_wait_cancel_requested"
                ] is True
                assert cleaned.structuredContent["verification"][
                    "active_wait"
                ] is False
                client_wait_count = cleaned.structuredContent[
                    "verification"
                ]["http_trigger_client_wait_count"]
                if cleanup_state == "settling":
                    assert client_wait_count >= 1
                else:
                    assert client_wait_count == 0

            assert await_result[0]["error_code"] == "WAIT_CANCELLED"
            assert dispatcher.settled == 1

        anyio.run(scenario)


def test_event_ready_before_arm_response_skips_http_trigger(
    monkeypatch: Any,
) -> None:
    dispatcher = _TriggerDispatcher(immediate_result=True)
    request_count = 0

    def handle(handler: BaseHTTPRequestHandler) -> None:
        nonlocal request_count
        request_count += 1
        _send_response(handler)

    original_wait_until_ready = WaitControl.wait_until_ready

    def wait_until_result_ready(
        control: WaitControl,
        timeout: float | None = None,
    ) -> bool:
        if not original_wait_until_ready(control, timeout):
            return False
        return control.wait_until_result(timeout)

    monkeypatch.setattr(
        WaitControl,
        "wait_until_ready",
        wait_until_result_ready,
    )

    with _http_server(handle) as url:
        boundary = RuntimeMCPBoundary(dispatcher)

        async def scenario() -> None:
            armed = await boundary.call_tool("java_runtime", _arm_arguments(url))
            payload = dict(armed.structuredContent or {})
            assert payload["result_ready"] is True
            assert payload["http_trigger"]["status"] == (
                "not_started_event_already_ready"
            )
            assert request_count == 0

            hit = await boundary.call_tool(
                "java_runtime",
                {
                    "action": "wait_event",
                    "wait_mode": "await",
                    "wait_handle": payload["wait_handle"],
                    "timeout": 1,
                },
            )
            assert hit.structuredContent["status"] == "breakpoint_hit"
            await boundary.call_tool(
                "java_runtime",
                {
                    "action": "resume",
                    "suspension_id": hit.structuredContent["suspension_id"],
                },
            )

        anyio.run(scenario)


def test_http_trigger_rejects_non_loopback_and_unsafe_headers() -> None:
    for value, expected_code in (
        (
            {"method": "GET", "url": "http://example.com/"},
            "HTTP_TRIGGER_TARGET_NOT_ALLOWED",
        ),
        (
            {
                "method": "POST",
                "url": "http://127.0.0.1:8080/",
                "headers": {"Host": "example.com"},
            },
            "HTTP_TRIGGER_HEADER_NOT_ALLOWED",
        ),
        (
            {"method": "GET", "url": "https://127.0.0.1:8443/"},
            "HTTP_TRIGGER_TARGET_NOT_ALLOWED",
        ),
        (
            {"method": "GET", "url": "http://localhost:8080/"},
            "HTTP_TRIGGER_TARGET_NOT_ALLOWED",
        ),
        (
            {"method": "CONNECT", "url": "http://127.0.0.1:8080/"},
            "HTTP_TRIGGER_METHOD_NOT_ALLOWED",
        ),
        (
            {
                "method": "POST",
                "url": "http://127.0.0.1:8080/",
                "json_body": "x" * (256 * 1024),
            },
            "HTTP_TRIGGER_BODY_TOO_LARGE",
        ),
        (
            {
                "method": "GET",
                "url": "http://127.0.0.1:8080/",
                "headers": {"X-Large": "x" * (16 * 1024)},
            },
            "HTTP_TRIGGER_HEADERS_TOO_LARGE",
        ),
    ):
        try:
            parse_http_trigger(value)
        except HTTPTriggerValidationError as error:
            assert error.code == expected_code
        else:  # pragma: no cover - failure path
            raise AssertionError("unsafe HTTP trigger must be rejected")


def test_http_trigger_validation_errors_do_not_echo_sensitive_values() -> None:
    boundary = RuntimeMCPBoundary(_TriggerDispatcher())
    header_name_secret = "X-TOP-SECRET-NAME"
    header_value_secret = "TOP_SECRET_VALUE"
    url_secret = "URL_SECRET_QUERY_VALUE"

    async def scenario() -> None:
        invalid_header = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "arm",
                "http_trigger": {
                    "method": "POST",
                    "url": "http://127.0.0.1:8080/",
                    "headers": {
                        header_name_secret: {"token": header_value_secret},
                    },
                },
            },
        )
        header_payload = dict(invalid_header.structuredContent or {})
        header_output = (
            json.dumps(header_payload)
            + str(invalid_header.content[0].text)
        )
        assert header_payload["argument"] == "http_trigger.headers.<header>"
        assert header_payload["validation_rule"] == "type"
        assert header_payload["expected"] == "string"
        assert header_name_secret not in header_output
        assert header_value_secret not in header_output

        invalid_url = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "arm",
                "http_trigger": {
                    "method": "GET",
                    "url": (
                        "http://127.0.0.1/"
                        + ("x" * 2048)
                        + "?token="
                        + url_secret
                    ),
                },
            },
        )
        url_payload = dict(invalid_url.structuredContent or {})
        url_output = json.dumps(url_payload) + str(invalid_url.content[0].text)
        assert url_payload["argument"] == "http_trigger.url"
        assert url_payload["validation_rule"] == "maxLength"
        assert url_secret not in url_output
        assert "http://127.0.0.1/" not in url_output

        body_secret = "BODY_SECRET_VALUE"
        invalid_body = await boundary.call_tool(
            "java_runtime",
            {
                "action": "wait_event",
                "wait_mode": "arm",
                "http_trigger": {
                    "method": "POST",
                    "url": "http://127.0.0.1:8080/",
                    "json_body": {
                        "secret": body_secret,
                        "padding": "x" * (256 * 1024),
                    },
                },
            },
        )
        body_payload = dict(invalid_body.structuredContent or {})
        body_output = (
            json.dumps(body_payload)
            + str(invalid_body.content[0].text)
        )
        assert body_payload["error_code"] == "HTTP_TRIGGER_BODY_TOO_LARGE"
        assert body_secret not in body_output

    anyio.run(scenario)

    port_secret = "PORT_SECRET"
    try:
        parse_http_trigger({
            "method": "GET",
            "url": f"http://127.0.0.1:{port_secret}/",
        })
    except HTTPTriggerValidationError as error:
        assert error.code == "INVALID_HTTP_TRIGGER_URL"
        assert str(error) == "Invalid http_trigger.url."
        assert port_secret not in str(error)
    else:  # pragma: no cover - failure path
        raise AssertionError("invalid port must be rejected")


def test_automatic_content_type_counts_toward_header_limits() -> None:
    base = {
        "method": "POST",
        "url": "http://127.0.0.1:8080/",
        "json_body": {"id": 7},
    }
    accepted = parse_http_trigger({
        **base,
        "headers": {f"X-Test-{index}": "v" for index in range(31)},
    })
    assert len(accepted.headers) == 32
    assert accepted.headers["Content-Type"] == "application/json"

    try:
        parse_http_trigger({
            **base,
            "headers": {f"X-Test-{index}": "v" for index in range(32)},
        })
    except HTTPTriggerValidationError as error:
        assert error.code == "HTTP_TRIGGER_HEADERS_TOO_LARGE"
    else:  # pragma: no cover - failure path
        raise AssertionError("auto-added Content-Type must count as a header")


def test_runtime_terminal_result_cancels_unobservable_http_client_wait() -> None:
    dispatcher = _TerminalWaitDispatcher()
    request_seen = threading.Event()
    release_response = threading.Event()

    def handle(handler: BaseHTTPRequestHandler) -> None:
        request_seen.set()
        release_response.wait(2)
        _send_response(handler)

    with _http_server(handle) as url:
        boundary = RuntimeMCPBoundary(dispatcher)

        async def scenario() -> None:
            armed = await boundary.call_tool(
                "java_runtime",
                _arm_arguments(url),
            )
            wait_handle = str(armed.structuredContent["wait_handle"])
            with anyio.fail_after(1):
                while not request_seen.is_set():
                    await anyio.sleep(0.005)
            dispatcher.finish_wait.set()

            terminal = await boundary.call_tool(
                "java_runtime",
                {
                    "action": "wait_event",
                    "wait_mode": "await",
                    "wait_handle": wait_handle,
                    "timeout": 1,
                },
            )
            payload = dict(terminal.structuredContent or {})
            assert payload["status"] == "timeout"
            assert payload["http_trigger"][
                "client_wait_cancel_requested"
            ] is True
            assert payload["http_trigger"]["status"] in {
                "client_wait_cancel_requested",
                "client_wait_cancelled",
            }
            assert boundary._find_wait(wait_handle) is None

            release_response.set()
            with anyio.fail_after(3):
                while boundary._http_triggers:
                    await anyio.sleep(0.005)

        anyio.run(scenario)


def test_event_publication_and_http_failure_have_one_atomic_winner() -> None:
    event_first = WaitControl(
        waiter_id="event-first",
        wait_generation=1,
    )
    event_first.publish_result({
        "ok": True,
        "status": "breakpoint_hit",
        "suspension_id": "susp_event_first",
    })
    assert event_first.cancel_if_result_pending("http_failed") is False
    assert event_first.cancelled is False
    assert event_first.result_copy()["suspension_id"] == "susp_event_first"

    failure_first = WaitControl(
        waiter_id="failure-first",
        wait_generation=2,
    )
    assert failure_first.cancel_if_result_pending("http_failed") is True
    failure_first.publish_result({
        "ok": True,
        "status": "breakpoint_hit",
        "suspension_id": "susp_too_late",
    })
    assert failure_first.cancelled is True
    assert failure_first.cancel_reason == "http_failed"


def test_http_trigger_does_not_follow_redirects() -> None:
    dispatcher = _TriggerDispatcher()
    paths: list[str] = []

    def handle(handler: BaseHTTPRequestHandler) -> None:
        paths.append(handler.path)
        if handler.path == "/trigger":
            handler.send_response(302)
            handler.send_header("Location", "/followed")
            handler.end_headers()
        else:
            _send_response(handler)

    with _http_server(handle) as url:
        boundary = RuntimeMCPBoundary(dispatcher)

        async def scenario() -> None:
            armed = await boundary.call_tool("java_runtime", _arm_arguments(url))
            wait_handle = str(armed.structuredContent["wait_handle"])
            with anyio.fail_after(1):
                while paths != ["/trigger"]:
                    await anyio.sleep(0.005)
            await anyio.sleep(0.05)
            assert paths == ["/trigger"]

            waiting = await boundary.call_tool(
                "java_runtime",
                {
                    "action": "wait_event",
                    "wait_mode": "await",
                    "wait_handle": wait_handle,
                    "timeout": 0.1,
                },
            )
            assert waiting.structuredContent["status"] == "waiting"
            assert waiting.structuredContent["http_trigger"]["http_status"] == 302
            await boundary.call_tool(
                "java_runtime",
                {"action": "cleanup_debug_state"},
            )

        anyio.run(scenario)


def test_http_trigger_is_rejected_outside_blocking_or_arm_mode() -> None:
    boundary = RuntimeMCPBoundary(_TriggerDispatcher())

    async def scenario() -> None:
        for arguments in (
            {
                "action": "wait_event",
                "wait_mode": "await",
                "wait_handle": "wait_missing",
                "http_trigger": {
                    "method": "GET",
                    "url": "http://127.0.0.1:8080/",
                },
            },
            {
                "action": "status",
                "http_trigger": {
                    "method": "GET",
                    "url": "http://127.0.0.1:8080/",
                },
            },
        ):
            result = await boundary.call_tool("java_runtime", arguments)
            assert result.isError is True
            assert result.structuredContent["error_code"] == (
                "INVALID_WAIT_ARGUMENTS"
            )

    anyio.run(scenario)


def test_only_one_await_call_can_own_a_wait_handle() -> None:
    dispatcher = _TriggerDispatcher()
    boundary = RuntimeMCPBoundary(dispatcher)

    async def scenario() -> None:
        armed = await boundary.call_tool(
            "java_runtime",
            {"action": "wait_event", "wait_mode": "arm", "timeout": 5},
        )
        wait_handle = str(armed.structuredContent["wait_handle"])
        first_result: list[dict[str, Any]] = []

        async def first_await() -> None:
            result = await boundary.call_tool(
                "java_runtime",
                {
                    "action": "wait_event",
                    "wait_mode": "await",
                    "wait_handle": wait_handle,
                    "timeout": 5,
                },
                request_id="await_one",
            )
            first_result.append(dict(result.structuredContent or {}))

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(first_await)
            control = boundary._find_wait(wait_handle)
            assert control is not None
            with anyio.fail_after(1):
                while not control.await_in_progress:
                    await anyio.sleep(0.005)

            duplicate = await boundary.call_tool(
                "java_runtime",
                {
                    "action": "wait_event",
                    "wait_mode": "await",
                    "wait_handle": wait_handle,
                    "timeout": 1,
                },
                request_id="await_two",
            )
            assert duplicate.isError is True
            assert duplicate.structuredContent["error_code"] == (
                "WAIT_HANDLE_IN_USE"
            )
            await boundary.call_tool(
                "java_runtime",
                {"action": "cleanup_debug_state"},
            )

        assert first_result[0]["error_code"] == "WAIT_CANCELLED"

    anyio.run(scenario)


def test_shutdown_cancels_http_client_wait_and_runtime_waiter() -> None:
    dispatcher = _TriggerDispatcher()
    request_seen = threading.Event()

    def handle(handler: BaseHTTPRequestHandler) -> None:
        request_seen.set()
        dispatcher.response_release.wait(2)
        _send_response(handler)

    with _http_server(handle) as url:
        boundary = RuntimeMCPBoundary(dispatcher)

        async def scenario() -> None:
            armed = await boundary.call_tool(
                "java_runtime",
                _arm_arguments(url),
            )
            control = boundary._find_wait(
                str(armed.structuredContent["wait_handle"])
            )
            assert control is not None
            trigger = control.trigger_control
            assert trigger is not None
            with anyio.fail_after(1):
                while not request_seen.is_set():
                    await anyio.sleep(0.005)

            await boundary.shutdown()

            assert dispatcher.cancel_seen.is_set()
            snapshot = trigger.snapshot()
            assert snapshot["client_wait_cancel_requested"] is True
            assert snapshot["status"] in {
                "client_wait_cancel_requested",
                "client_wait_cancelled",
            }

        anyio.run(scenario)
