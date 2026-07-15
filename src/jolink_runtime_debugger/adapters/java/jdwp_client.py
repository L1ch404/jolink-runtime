"""
JDWP pure protocol client.

Responsibilities (AND ONLY THESE):
  - TCP connect + JDWP handshake
  - Packet encode / decode
  - Command send
  - Reply receive

Does NOT provide:
  - thread_name(), thread_status(), class_signature(), ...
  - Those belong in JavaRuntime, not here.
"""

from __future__ import annotations

import logging
import socket
import struct
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)


class JDWPError(Exception):
    def __init__(self, code: int, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(f"JDWP error {code}: {message}")


# ---- Packet helpers ----------------------------------------------------

def _pack_cmd(cmd_set: int, cmd: int, data: bytes, counter: int) -> bytes:
    """Pack a JDWP command packet. Returns (packet_bytes, new_counter)."""
    length = 11 + len(data)
    header = struct.pack(">IIB", length, counter, 0x00)
    return header + struct.pack(">BB", cmd_set, cmd) + data


# ---- ID sizes ----------------------------------------------------------

@dataclass
class IDSizes:
    field_id_size: int = 0
    method_id_size: int = 0
    object_id_size: int = 0
    reference_type_id_size: int = 0
    frame_id_size: int = 0

    def pack_field(self, fid: int) -> bytes:
        return fid.to_bytes(self.field_id_size, "big")

    def pack_method(self, mid: int) -> bytes:
        return mid.to_bytes(self.method_id_size, "big")

    def pack_obj(self, oid: int) -> bytes:
        return oid.to_bytes(self.object_id_size, "big")

    def pack_ref(self, rid: int) -> bytes:
        return rid.to_bytes(self.reference_type_id_size, "big")

    def pack_frame(self, fid: int) -> bytes:
        return fid.to_bytes(self.frame_id_size, "big")


# ---- Command set constants ---------------------------------------------

class Cmd:
    VM          = 1
    REF_TYPE    = 2
    CLASS_TYPE  = 3
    METHOD      = 6
    OBJ_REF     = 9   # ObjectReference
    STRING_REF  = 10  # StringReference
    THREAD      = 11
    ARRAY       = 13  # ArrayReference
    EVENT       = 15
    STACK       = 16


class EventKind:
    SINGLE_STEP = 1
    BREAKPOINT = 2
    EXCEPTION = 4
    THREAD_START = 6
    THREAD_DEATH = 7
    CLASS_PREPARE = 8
    CLASS_UNLOAD = 9
    METHOD_ENTRY = 40
    METHOD_EXIT = 41
    VM_START = 90
    VM_DEATH = 99
    VM_DISCONNECTED = 100


class SuspendPolicy:
    NONE = 0
    EVENT_THREAD = 1
    ALL = 2


# ---- JDWP tagged-value type constants ----------------------------------

class Tag:
    """JDWP tagged-value type tags (1 byte each)."""
    BYTE    = 0x42  # 'B'
    CHAR    = 0x43  # 'C'
    CLASS_OBJECT = 0x63  # 'c'
    DOUBLE  = 0x44  # 'D'
    FLOAT   = 0x46  # 'F'
    THREAD_GROUP = 0x67  # 'g'
    INT     = 0x49  # 'I'
    LONG    = 0x4A  # 'J'
    CLASS_LOADER = 0x6C  # 'l'
    OBJECT  = 0x4C  # 'L'
    SHORT   = 0x53  # 'S'
    BOOLEAN = 0x5A  # 'Z'
    STRING  = 0x73  # 's'
    ARRAY   = 0x5B  # '['
    THREAD  = 0x74  # 't'

    # JVM signature → tag mapping (first char of field signature)
    SIG_TO_TAG = {
        "B": BYTE, "C": CHAR, "D": DOUBLE, "F": FLOAT,
        "I": INT, "J": LONG, "S": SHORT, "Z": BOOLEAN,
        "L": OBJECT, "[": ARRAY,
    }

    @classmethod
    def from_sig(cls, sig: str) -> int:
        """Return the JDWP tag for a JVM type signature first char."""
        return cls.SIG_TO_TAG.get(sig[0], cls.OBJECT)


# ---- JDWPClient --------------------------------------------------------


class JDWPClient:
    """JDWP transport with command/reply and event multiplexing.

    A target VM may send an Event/Composite command while the debugger is
    waiting for an unrelated command reply.  Treating the next packet as the
    reply corrupts the stream as soon as a breakpoint is hit.  This client
    routes replies by packet id and queues VM events for ``wait_for_event``.
    """

    def __init__(self):
        self._sock: Optional[socket.socket] = None
        self._counter = 0
        self.ids: Optional[IDSizes] = None
        self._pending_replies: dict[int, tuple[int, bytes]] = {}
        self._pending_events: deque[dict] = deque()

    # -- connection --

    def connect(self, host: str, port: int, timeout: float = 5.0) -> None:
        started_at = time.monotonic()
        logger.info(
            "java_runtime.jdwp.connect.start host=%s port=%s timeout=%s",
            host, port, timeout,
        )
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(timeout)
            self._sock.connect((host, port))
            self._sock.sendall(b"JDWP-Handshake")
            reply = self._recv(14)
            if reply != b"JDWP-Handshake":
                raise JDWPError(-1, "Handshake failed")
            self._query_id_sizes()
        except Exception as exc:
            logger.warning(
                "java_runtime.jdwp.connect.failed host=%s port=%s elapsed_ms=%.1f "
                "error_type=%s error=%s",
                host, port, (time.monotonic() - started_at) * 1000,
                type(exc).__name__, str(exc).splitlines()[0] if str(exc) else "-",
            )
            self.close()
            raise
        ids = self.ids
        logger.info(
            "java_runtime.jdwp.connect.ready host=%s port=%s elapsed_ms=%.1f "
            "field_id=%s method_id=%s object_id=%s ref_type_id=%s frame_id=%s",
            host, port, (time.monotonic() - started_at) * 1000,
            ids.field_id_size if ids else "-",
            ids.method_id_size if ids else "-",
            ids.object_id_size if ids else "-",
            ids.reference_type_id_size if ids else "-",
            ids.frame_id_size if ids else "-",
        )

    def close(self) -> None:
        was_connected = self._sock is not None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._pending_replies.clear()
        self._pending_events.clear()
        if was_connected:
            logger.info("java_runtime.jdwp.connection.closed")

    def _recv(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise JDWPError(-1, "Connection closed by remote")
            buf += chunk
        return buf

    def _query_id_sizes(self) -> None:
        err, data = self.command(Cmd.VM, 7)  # VM/IDSizes
        if err:
            raise JDWPError(err)
        field_id, method_id, object_id, ref_type_id, frame_id = \
            struct.unpack(">IIIII", data)
        self.ids = IDSizes(field_id, method_id, object_id, ref_type_id, frame_id)

    # -- command / reply / event routing --

    def _read_packet(self, timeout: float | None = None) -> dict:
        if self._sock is None:
            raise JDWPError(-1, "Not connected")
        previous_timeout = self._sock.gettimeout()
        if timeout is not None:
            self._sock.settimeout(max(timeout, 0.001))
        try:
            header = self._recv(11)
            length, packet_id, flags = struct.unpack(">IIB", header[:9])
            if length < 11:
                raise JDWPError(-1, f"Invalid packet length: {length}")
            payload = self._recv(length - 11) if length > 11 else b""
        finally:
            if self._sock is not None and timeout is not None:
                self._sock.settimeout(previous_timeout)

        if flags == 0x80:
            return {
                "type": "reply",
                "id": packet_id,
                "error": struct.unpack(">H", header[9:11])[0],
                "data": payload,
            }
        return {
            "type": "command",
            "id": packet_id,
            "command_set": header[9],
            "command": header[10],
            "data": payload,
        }

    def _parse_location(self, data: bytes, offset: int) -> tuple[dict, int]:
        ids = self.ids
        if ids is None:
            raise JDWPError(-1, "ID sizes have not been negotiated")
        type_tag = data[offset]
        offset += 1
        class_id = int.from_bytes(
            data[offset:offset + ids.reference_type_id_size], "big"
        )
        offset += ids.reference_type_id_size
        method_id = int.from_bytes(data[offset:offset + ids.method_id_size], "big")
        offset += ids.method_id_size
        index = struct.unpack_from(">Q", data, offset)[0]
        offset += 8
        return {
            "type_tag": type_tag,
            "class_id": class_id,
            "method_id": method_id,
            "index": index,
        }, offset

    def _parse_tagged_object(self, data: bytes, offset: int) -> tuple[dict, int]:
        ids = self.ids
        if ids is None:
            raise JDWPError(-1, "ID sizes have not been negotiated")
        tag = data[offset]
        offset += 1
        object_id = int.from_bytes(
            data[offset:offset + ids.object_id_size], "big"
        )
        offset += ids.object_id_size
        return {"tag": tag, "object_id": object_id}, offset

    def _parse_composite_event(self, data: bytes) -> dict:
        """Parse Event/Composite data (the 11-byte JDWP header is excluded)."""
        ids = self.ids
        if ids is None:
            raise JDWPError(-1, "ID sizes have not been negotiated")
        if len(data) < 5:
            raise JDWPError(-1, "Composite event payload too short")

        suspend_policy = data[0]
        event_count = struct.unpack_from(">I", data, 1)[0]
        offset = 5
        events: list[dict] = []
        for _ in range(event_count):
            if offset + 5 > len(data):
                raise JDWPError(-1, "Truncated composite event")
            kind = data[offset]
            request_id = struct.unpack_from(">I", data, offset + 1)[0]
            offset += 5
            event: dict = {"kind": kind, "request_id": request_id}

            if kind in {EventKind.SINGLE_STEP, EventKind.BREAKPOINT}:
                event["thread_id"] = int.from_bytes(
                    data[offset:offset + ids.object_id_size], "big"
                )
                offset += ids.object_id_size
                event["location"], offset = self._parse_location(data, offset)
            elif kind == EventKind.EXCEPTION:
                event["thread_id"] = int.from_bytes(
                    data[offset:offset + ids.object_id_size], "big"
                )
                offset += ids.object_id_size
                event["location"], offset = self._parse_location(data, offset)
                event["exception"], offset = self._parse_tagged_object(data, offset)
                event["catch_location"], offset = self._parse_location(data, offset)
            elif kind in {
                EventKind.THREAD_START,
                EventKind.THREAD_DEATH,
                EventKind.VM_START,
            }:
                event["thread_id"] = int.from_bytes(
                    data[offset:offset + ids.object_id_size], "big"
                )
                offset += ids.object_id_size
            elif kind in {EventKind.VM_DEATH, EventKind.VM_DISCONNECTED}:
                pass
            else:
                # Unknown event bodies have kind-specific lengths, so
                # continuing would risk inventing boundaries for later events.
                event["unparsed"] = True
                event["raw_tail"] = data[offset:]
                events.append(event)
                offset = len(data)
                break
            events.append(event)

        return {"suspend_policy": suspend_policy, "events": events}

    def _route_packet(self, packet: dict) -> None:
        if packet["type"] == "reply":
            self._pending_replies[packet["id"]] = (
                packet["error"], packet["data"]
            )
            return

        # Event/Composite is a target-to-debugger notification.  The JDWP
        # specification explicitly says VM events do not require a reply.
        if packet["command_set"] == 64 and packet["command"] == 100:
            composite = self._parse_composite_event(packet["data"])
            self._pending_events.append(composite)
            logger.debug(
                "java_runtime.jdwp.event.queued packet_id=%s suspend_policy=%s "
                "event_count=%s event_kinds=%s request_ids=%s",
                packet["id"], composite["suspend_policy"],
                len(composite["events"]),
                [event.get("kind") for event in composite["events"]],
                [event.get("request_id") for event in composite["events"]],
            )

    def wait_for_event(self, timeout: float = 30.0) -> dict | None:
        """Wait for the next VM event, returning ``None`` on timeout."""
        if self._pending_events:
            logger.debug("java_runtime.jdwp.event.dequeue source=pending")
            return self._pending_events.popleft()

        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.debug("java_runtime.jdwp.event.wait.timeout timeout=%s", timeout)
                return None
            try:
                self._route_packet(self._read_packet(timeout=remaining))
            except socket.timeout:
                logger.debug("java_runtime.jdwp.event.wait.timeout timeout=%s", timeout)
                return None
            if self._pending_events:
                logger.debug("java_runtime.jdwp.event.dequeue source=socket")
                return self._pending_events.popleft()

    def drain_events(self) -> list[dict]:
        """Return already queued/readable VM events without blocking."""
        if self._sock is None:
            return []
        import select

        events = list(self._pending_events)
        self._pending_events.clear()
        while select.select([self._sock], [], [], 0)[0]:
            try:
                self._route_packet(self._read_packet(timeout=0.05))
            except (socket.timeout, JDWPError, OSError):
                break
            while self._pending_events:
                events.append(self._pending_events.popleft())
        return events

    def command(self, cmd_set: int, cmd: int, data: bytes = b"") -> tuple[int, bytes]:
        """Send a command and return (error_code, reply_data)."""
        if self._sock is None:
            raise JDWPError(-1, "Not connected")
        self._counter += 1
        packet_id = self._counter
        raw = _pack_cmd(cmd_set, cmd, data, packet_id)
        started_at = time.monotonic()
        logger.debug(
            "java_runtime.jdwp.command.send packet_id=%s command_set=%s command=%s "
            "request_bytes=%s",
            packet_id, cmd_set, cmd, len(data),
        )
        self._sock.sendall(raw)
        while packet_id not in self._pending_replies:
            self._route_packet(self._read_packet())
        error, reply = self._pending_replies.pop(packet_id)
        logger.debug(
            "java_runtime.jdwp.command.reply packet_id=%s command_set=%s command=%s "
            "error_code=%s response_bytes=%s elapsed_ms=%.1f",
            packet_id, cmd_set, cmd, error, len(reply),
            (time.monotonic() - started_at) * 1000,
        )
        return error, reply
