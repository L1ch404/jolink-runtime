"""Bounded loopback HTTP triggers for two-phase Runtime observations."""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx


_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_FORBIDDEN_HEADERS = frozenset({
    "connection",
    "content-length",
    "host",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
})
_MAX_BODY_BYTES = 256 * 1024
_MAX_HEADER_BYTES = 16 * 1024
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _iso_timestamp(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


class HTTPTriggerValidationError(ValueError):
    """Raised before a wait starts when an HTTP trigger is unsafe or invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HTTPTriggerSpec:
    """Validated request data that is safe to execute against loopback."""

    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout_seconds: float


def parse_http_trigger(value: dict[str, Any]) -> HTTPTriggerSpec:
    """Validate the compact MCP HTTP trigger contract and security boundary."""
    method = str(value.get("method", "")).upper()
    if method not in _ALLOWED_METHODS:
        raise HTTPTriggerValidationError(
            "HTTP_TRIGGER_METHOD_NOT_ALLOWED",
            "http_trigger.method must be GET, POST, PUT, PATCH, or DELETE.",
        )

    raw_url = str(value.get("url", ""))
    if raw_url != raw_url.strip() or any(ord(char) < 32 for char in raw_url):
        raise HTTPTriggerValidationError(
            "INVALID_HTTP_TRIGGER_URL",
            "http_trigger.url contains whitespace or control characters.",
        )
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as error:
        raise HTTPTriggerValidationError(
            "INVALID_HTTP_TRIGGER_URL",
            "Invalid http_trigger.url.",
        ) from error
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise HTTPTriggerValidationError(
            "HTTP_TRIGGER_TARGET_NOT_ALLOWED",
            "http_trigger.url must use http://127.0.0.1 with no credentials or fragment.",
        )
    if port is not None and not 1 <= port <= 65535:
        raise HTTPTriggerValidationError(
            "INVALID_HTTP_TRIGGER_URL",
            "http_trigger.url contains an invalid port.",
        )

    raw_headers = value.get("headers", {})
    if not isinstance(raw_headers, dict):
        raise HTTPTriggerValidationError(
            "INVALID_HTTP_TRIGGER_HEADERS",
            "http_trigger.headers must be an object of string values.",
        )
    headers: dict[str, str] = {}
    for raw_name, raw_value in raw_headers.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise HTTPTriggerValidationError(
                "INVALID_HTTP_TRIGGER_HEADERS",
                "http_trigger header names and values must be strings.",
            )
        name = raw_name.strip()
        if not _HEADER_NAME.fullmatch(name):
            raise HTTPTriggerValidationError(
                "INVALID_HTTP_TRIGGER_HEADERS",
                "http_trigger contains an invalid header name.",
            )
        if "\r" in raw_value or "\n" in raw_value:
            raise HTTPTriggerValidationError(
                "INVALID_HTTP_TRIGGER_HEADERS",
                "http_trigger contains an invalid header value.",
            )
        if name.lower() in _FORBIDDEN_HEADERS:
            raise HTTPTriggerValidationError(
                "HTTP_TRIGGER_HEADER_NOT_ALLOWED",
                "http_trigger contains a header that may not be set.",
            )
        headers[name] = raw_value

    body: bytes | None = None
    if "json_body" in value:
        try:
            body = json.dumps(
                value["json_body"],
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise HTTPTriggerValidationError(
                "INVALID_HTTP_TRIGGER_BODY",
                "http_trigger.json_body is not valid JSON.",
            ) from error
        if len(body) > _MAX_BODY_BYTES:
            raise HTTPTriggerValidationError(
                "HTTP_TRIGGER_BODY_TOO_LARGE",
                "http_trigger.json_body exceeds the 256 KiB limit.",
            )
        if not any(name.lower() == "content-type" for name in headers):
            headers["Content-Type"] = "application/json"

    if len(headers) > 32:
        raise HTTPTriggerValidationError(
            "HTTP_TRIGGER_HEADERS_TOO_LARGE",
            "http_trigger supports at most 32 headers including Content-Type.",
        )
    header_bytes = sum(
        len(name.encode("utf-8")) + len(header_value.encode("utf-8"))
        for name, header_value in headers.items()
    )
    if header_bytes > _MAX_HEADER_BYTES:
        raise HTTPTriggerValidationError(
            "HTTP_TRIGGER_HEADERS_TOO_LARGE",
            "http_trigger headers exceed the 16 KiB limit including Content-Type.",
        )

    timeout_seconds = float(value.get("timeout_seconds", 30.0))
    if not 0.1 <= timeout_seconds <= 120.0:
        raise HTTPTriggerValidationError(
            "INVALID_HTTP_TRIGGER_TIMEOUT",
            "http_trigger.timeout_seconds must be between 0.1 and 120.",
        )

    return HTTPTriggerSpec(
        method=method,
        url=raw_url,
        headers=headers,
        body=body,
        timeout_seconds=timeout_seconds,
    )


class HTTPTriggerControl:
    """Own one bounded background HTTP client request."""

    def __init__(
        self,
        spec: HTTPTriggerSpec,
        *,
        on_done: Callable[["HTTPTriggerControl"], None] | None = None,
    ) -> None:
        self.spec = spec
        self._on_done = on_done
        self._lock = threading.Lock()
        self._done_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: httpx.Client | None = None
        self._cancel_requested = False
        self._cancel_reason = ""
        self._status = "pending"
        self._started_at: float | None = None
        self._completed_at: float | None = None
        self._http_status: int | None = None
        self._error_code = ""
        self._server_execution_state = "not_started"
        self._finish_notified = False

    @property
    def done(self) -> bool:
        return self._done_event.is_set()

    @property
    def definite_failure(self) -> bool:
        with self._lock:
            return self._status == "failed_before_send"

    def start(self) -> bool:
        """Start the request without waiting for an HTTP response."""
        with self._lock:
            if self._status != "pending":
                return False
            self._status = "running"
            self._server_execution_state = "unknown"
            self._started_at = time.time()
            thread = threading.Thread(
                target=self._run,
                name="jolink-http-trigger",
                daemon=True,
            )
            self._thread = thread
        try:
            thread.start()
            return True
        except Exception:
            with self._lock:
                self._status = "failed_before_send"
                self._error_code = "HTTP_TRIGGER_START_FAILED"
                self._completed_at = time.time()
            self._finish()
            return False

    def skip_event_already_ready(self) -> None:
        with self._lock:
            if self._status != "pending":
                return
            self._status = "not_started_event_already_ready"
            self._completed_at = time.time()
        self._finish()

    def cancel_client_wait(self, reason: str) -> None:
        """Stop client-side waiting without claiming server execution stopped."""
        with self._lock:
            if self._done_event.is_set() or self._cancel_requested:
                return
            self._cancel_requested = True
            if not self._cancel_reason:
                self._cancel_reason = reason
            client = self._client
            if self._status == "pending":
                self._status = "client_wait_cancelled"
                self._server_execution_state = "not_started"
                self._completed_at = time.time()
                finish_now = True
            else:
                if self._status == "running":
                    self._status = "client_wait_cancel_requested"
                    self._server_execution_state = "unknown"
                finish_now = False
        if client is not None:
            # Closing a live client can wait on socket state.  Never do that
            # on the MCP event loop or make Runtime settlement depend on it.
            threading.Thread(
                target=self._close_client,
                args=(client,),
                name="jolink-http-trigger-cancel",
                daemon=True,
            ).start()
        if finish_now:
            self._finish()

    def wait_until_done(self, timeout: float | None = None) -> bool:
        return self._done_event.wait(timeout)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload: dict[str, Any] = {
                "type": "http",
                "method": self.spec.method,
                "status": self._status,
                "server_execution_state": self._server_execution_state,
            }
            if self._started_at is not None:
                payload["started_at"] = _iso_timestamp(self._started_at)
            if self._completed_at is not None:
                timestamp = _iso_timestamp(self._completed_at)
                if self._status == "response_headers_received":
                    payload["response_headers_at"] = timestamp
                else:
                    payload["completed_at"] = timestamp
            if self._http_status is not None:
                payload["http_status"] = self._http_status
            if self._error_code:
                payload["error_code"] = self._error_code
            if self._cancel_requested:
                payload["client_wait_cancel_requested"] = True
            if self._status == "client_wait_cancelled":
                payload["client_wait_cancelled"] = True
            return payload

    def _run(self) -> None:
        client: httpx.Client | None = None
        try:
            client = httpx.Client(
                follow_redirects=False,
                timeout=self.spec.timeout_seconds,
                trust_env=False,
            )
            with self._lock:
                self._client = client
                cancelled = self._cancel_requested
            if cancelled:
                with self._lock:
                    self._status = "client_wait_cancelled"
                    self._completed_at = time.time()
                return

            with client.stream(
                self.spec.method,
                self.spec.url,
                headers=self.spec.headers,
                content=self.spec.body,
            ) as response:
                with self._lock:
                    self._http_status = response.status_code
                    self._status = "response_headers_received"
                    self._server_execution_state = "response_headers_received"
                    self._completed_at = time.time()
        except (httpx.ConnectError, httpx.ConnectTimeout):
            with self._lock:
                cancelled = self._cancel_requested
                self._status = (
                    "client_wait_cancelled" if cancelled else "failed_before_send"
                )
                self._error_code = (
                    "" if cancelled else "HTTP_TRIGGER_CONNECTION_FAILED"
                )
                self._server_execution_state = (
                    "unknown" if cancelled else "not_started"
                )
                self._completed_at = time.time()
        except httpx.TimeoutException:
            with self._lock:
                cancelled = self._cancel_requested
                self._status = (
                    "client_wait_cancelled" if cancelled else "client_wait_ended"
                )
                self._error_code = (
                    "" if cancelled else "HTTP_TRIGGER_CLIENT_TIMEOUT"
                )
                self._server_execution_state = "unknown"
                self._completed_at = time.time()
        except httpx.RequestError:
            with self._lock:
                cancelled = self._cancel_requested
                self._status = (
                    "client_wait_cancelled" if cancelled else "client_wait_ended"
                )
                self._error_code = (
                    "" if cancelled else "HTTP_TRIGGER_CLIENT_ERROR"
                )
                self._server_execution_state = "unknown"
                self._completed_at = time.time()
        except Exception:
            with self._lock:
                cancelled = self._cancel_requested
                self._status = (
                    "client_wait_cancelled" if cancelled else "client_wait_ended"
                )
                self._error_code = (
                    "" if cancelled else "HTTP_TRIGGER_INTERNAL_ERROR"
                )
                self._server_execution_state = "unknown"
                self._completed_at = time.time()
        finally:
            if client is not None:
                self._close_client(client)
            with self._lock:
                self._client = None
                if self._cancel_requested and self._status in {
                    "running",
                    "client_wait_cancel_requested",
                }:
                    self._status = "client_wait_cancelled"
                    self._server_execution_state = "unknown"
                    self._completed_at = time.time()
            self._finish()

    def _finish(self) -> None:
        with self._lock:
            if self._finish_notified:
                return
            self._finish_notified = True
        self._done_event.set()
        if self._on_done is not None:
            self._on_done(self)

    @staticmethod
    def _close_client(client: httpx.Client) -> None:
        try:
            client.close()
        except Exception:
            pass


__all__ = [
    "HTTPTriggerControl",
    "HTTPTriggerSpec",
    "HTTPTriggerValidationError",
    "parse_http_trigger",
]
