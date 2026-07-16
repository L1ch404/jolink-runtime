from __future__ import annotations

import socket
import struct
import threading
from collections import deque

import pytest

import jolink_runtime_debugger.adapters.java.jdwp_client as jdwp_module
from jolink_runtime_debugger.adapters.java.jdwp_client import (
    IDSizes,
    JDWPClient,
    JDWPError,
)


def _reply_packet(packet_id: int, payload: bytes = b"") -> bytes:
    return struct.pack(">IIBH", 11 + len(payload), packet_id, 0x80, 0) + payload


class ScriptedSocket:
    def __init__(self, actions: list[bytes | BaseException]):
        self.actions = deque(actions)
        self.timeout: float | None = None
        self.closed = False
        self.connected_to: tuple[str, int] | None = None
        self.sent: list[bytes] = []

    def gettimeout(self) -> float | None:
        return self.timeout

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def connect(self, address: tuple[str, int]) -> None:
        self.connected_to = address

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, size: int) -> bytes:
        if self.closed:
            raise OSError("socket is closed")
        if not self.actions:
            raise AssertionError("unexpected recv")
        action = self.actions.popleft()
        if isinstance(action, BaseException):
            raise action
        if len(action) > size:
            self.actions.appendleft(action[size:])
            return action[:size]
        return action

    def close(self) -> None:
        self.closed = True


def test_header_fragment_survives_socket_timeout() -> None:
    packet = _reply_packet(17, b"abc")
    sock = ScriptedSocket([
        packet[:5],
        socket.timeout("header slice"),
        packet[5:],
    ])
    client = JDWPClient()
    client._sock = sock

    with pytest.raises(socket.timeout):
        client._read_packet(timeout=0.1)

    assert bytes(client._recv_buffer) == packet[:5]
    observed = client._read_packet(timeout=0.1)
    generation = observed.pop("_connection_generation")
    assert generation == client._connection_generation
    assert observed == {
        "type": "reply",
        "id": 17,
        "error": 0,
        "data": b"abc",
    }
    assert client._recv_buffer == bytearray()


def test_body_fragment_keeps_header_until_packet_is_complete() -> None:
    packet = _reply_packet(23, b"abcdef")
    sock = ScriptedSocket([
        packet[:11],
        packet[11:13],
        socket.timeout("body slice"),
        packet[13:],
    ])
    client = JDWPClient()
    client._sock = sock

    with pytest.raises(socket.timeout):
        client._read_packet(timeout=0.1)

    # The complete header is intentionally not consumed while the body is
    # incomplete, so the next attempt can resume packet framing safely.
    assert bytes(client._recv_buffer) == packet[:13]
    assert client._read_packet(timeout=0.1)["data"] == b"abcdef"
    assert client._recv_buffer == bytearray()


def test_fatal_socket_error_discards_partial_packet() -> None:
    packet = _reply_packet(29, b"payload")
    client = JDWPClient()
    client._sock = ScriptedSocket([packet[:4], OSError("connection reset")])

    with pytest.raises(OSError, match="connection reset"):
        client._read_packet(timeout=0.1)

    assert client._recv_buffer == bytearray()


def test_close_and_reconnect_never_reuse_buffered_bytes(monkeypatch) -> None:
    old_packet = _reply_packet(31, b"old")
    old_sock = ScriptedSocket([
        old_packet[:6],
        socket.timeout("old connection"),
    ])
    client = JDWPClient()
    client._sock = old_sock

    with pytest.raises(socket.timeout):
        client._read_packet(timeout=0.1)
    assert client._recv_buffer

    client.close()
    assert old_sock.closed is True
    assert client._recv_buffer == bytearray()

    # connect() also resets receive state defensively, even if stale bytes
    # somehow exist after the previous transport has gone away.
    client._recv_buffer.extend(b"stale")
    id_sizes = struct.pack(">IIIII", 8, 8, 8, 8, 8)
    new_sock = ScriptedSocket([
        b"JDWP-Handshake",
        _reply_packet(1, id_sizes),
    ])
    monkeypatch.setattr(jdwp_module.socket, "socket", lambda *_args: new_sock)

    client.connect("127.0.0.1", 5005)

    assert new_sock.connected_to == ("127.0.0.1", 5005)
    assert new_sock.sent[0] == b"JDWP-Handshake"
    assert client.ids == IDSizes(8, 8, 8, 8, 8)
    assert client._recv_buffer == bytearray()
    client.close()


class CoordinatedSocket(ScriptedSocket):
    def __init__(self, actions: list[bytes | BaseException]):
        super().__init__(actions)
        self.first_recv_entered = threading.Event()
        self.release_first_recv = threading.Event()
        self.second_recv_entered = threading.Event()
        self._call_count = 0
        self._call_count_lock = threading.Lock()

    def recv(self, size: int) -> bytes:
        with self._call_count_lock:
            self._call_count += 1
            call_number = self._call_count
        if call_number == 1:
            self.first_recv_entered.set()
            assert self.release_first_recv.wait(timeout=1)
        elif call_number == 2:
            self.second_recv_entered.set()
        return super().recv(size)


def test_concurrent_packet_readers_are_serialized() -> None:
    sock = CoordinatedSocket([_reply_packet(41), _reply_packet(42)])
    client = JDWPClient()
    client._sock = sock
    results: list[dict] = []
    errors: list[BaseException] = []
    second_started = threading.Event()

    def read_packet(started: threading.Event | None = None) -> None:
        if started is not None:
            started.set()
        try:
            results.append(client._read_packet(timeout=0.5))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=read_packet)
    first.start()
    assert sock.first_recv_entered.wait(timeout=1)

    second = threading.Thread(target=read_packet, args=(second_started,))
    second.start()
    assert second_started.wait(timeout=1)

    # The second path has entered _read_packet(), but cannot call recv until
    # the first reader has completed the whole packet.
    assert not sock.second_recv_entered.wait(timeout=0.05)
    sock.release_first_recv.set()

    first.join(timeout=1)
    second.join(timeout=1)
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert [packet["id"] for packet in results] == [41, 42]


def test_packet_from_closed_connection_is_not_routed() -> None:
    client = JDWPClient()
    client._sock = ScriptedSocket([_reply_packet(51)])

    packet = client._read_packet(timeout=0.1)
    client.close()
    client._route_packet(packet)

    assert client._pending_replies == {}


def test_old_reader_failure_cannot_clear_new_connection_buffer() -> None:
    old_sock = CoordinatedSocket([b"x"])
    new_sock = ScriptedSocket([])
    client = JDWPClient()
    client._sock = old_sock
    errors: list[BaseException] = []

    def old_read() -> None:
        try:
            client._recv(1)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    reader = threading.Thread(target=old_read)
    reader.start()
    assert old_sock.first_recv_entered.wait(timeout=1)

    client.close()
    with client._connection_state_lock:
        client._sock = new_sock
        client._connection_generation += 1
        client._recv_buffer.extend(b"new")

    old_sock.release_first_recv.set()
    reader.join(timeout=1)

    assert not reader.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], OSError)
    assert bytes(client._recv_buffer) == b"new"


def test_close_after_complete_body_is_detected_before_packet_slice() -> None:
    client = JDWPClient()
    client._sock = ScriptedSocket([_reply_packet(61, b"body")])
    original_fill = client._fill_recv_buffer

    def fill_then_close(
        size: int,
        *,
        deadline: float | None = None,
    ) -> None:
        original_fill(size, deadline=deadline)
        if size > 11:
            client.close()

    client._fill_recv_buffer = fill_then_close

    with pytest.raises(
        JDWPError,
        match="Connection changed while reading packet",
    ):
        client._read_packet(timeout=0.1)


def test_close_after_complete_header_is_detected_before_unpack() -> None:
    client = JDWPClient()
    client._sock = ScriptedSocket([_reply_packet(62, b"body")])
    original_fill = client._fill_recv_buffer

    def fill_then_close(
        size: int,
        *,
        deadline: float | None = None,
    ) -> None:
        original_fill(size, deadline=deadline)
        if size == 11:
            client.close()

    client._fill_recv_buffer = fill_then_close

    with pytest.raises(
        JDWPError,
        match="Connection changed while reading packet header",
    ):
        client._read_packet(timeout=0.1)


def test_close_during_event_parse_prevents_stale_queue_append() -> None:
    client = JDWPClient()
    client._sock = ScriptedSocket([])
    generation = client._connection_generation

    def parse_then_close(_data: bytes) -> dict:
        client.close()
        return {"suspend_policy": 0, "events": []}

    client._parse_composite_event = parse_then_close
    client._route_packet({
        "type": "command",
        "id": 71,
        "command_set": 64,
        "command": 100,
        "data": b"",
        "_connection_generation": generation,
    })

    assert list(client._pending_events) == []


def test_packet_timeout_is_absolute_across_trickled_fragments(
    monkeypatch,
) -> None:
    packet = _reply_packet(81, b"body")
    client = JDWPClient()
    client._sock = ScriptedSocket([
        packet[:1],
        packet[1:2],
        packet[2:],
    ])
    now = 0.0

    def monotonic() -> float:
        nonlocal now
        value = now
        now += 0.04
        return value

    monkeypatch.setattr(jdwp_module.time, "monotonic", monotonic)

    with pytest.raises(socket.timeout):
        client._read_packet(timeout=0.1)

    assert 0 < len(client._recv_buffer) < len(packet)
