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
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Mapping, Optional


logger = logging.getLogger(__name__)


class JDWPError(Exception):
    def __init__(self, code: int, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(f"JDWP error {code}: {message}")


class JDWPCommandRejected(JDWPError):
    """The target VM replied with an explicit non-zero JDWP error code."""

    def __init__(
        self,
        code: int,
        *,
        command_set: int,
        command: int,
        operation: str,
    ):
        self.command_set = command_set
        self.command = command
        self.operation = operation
        super().__init__(code, f"{operation} was rejected by the target VM")


class JDWPCommandOutcomeUnknown(JDWPError):
    """A state-changing command lost its transport before a reply was observed."""

    def __init__(
        self,
        *,
        packet_id: int,
        command_set: int,
        command: int,
        operation: str,
        cause: BaseException,
    ):
        self.packet_id = packet_id
        self.command_set = command_set
        self.command = command
        self.operation = operation
        self.cause_type = type(cause).__name__
        super().__init__(
            -1,
            (
                f"{operation} outcome is unknown because the transport failed "
                "after command transmission began"
            ),
        )


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


@dataclass(frozen=True)
class JDWPCapabilities:
    """Named fields returned by VirtualMachine/CapabilitiesNew."""

    can_watch_field_modification: bool
    can_watch_field_access: bool
    can_get_bytecodes: bool
    can_get_synthetic_attribute: bool
    can_get_owned_monitor_info: bool
    can_get_current_contended_monitor: bool
    can_get_monitor_info: bool
    can_redefine_classes: bool
    can_add_method: bool
    can_unrestrictedly_redefine_classes: bool
    can_pop_frames: bool
    can_use_instance_filters: bool
    can_get_source_debug_extension: bool
    can_request_vm_death_event: bool
    can_set_default_stratum: bool
    can_get_instance_info: bool
    can_request_monitor_events: bool
    can_get_monitor_frame_info: bool
    can_use_source_name_filters: bool
    can_get_constant_pool: bool
    can_force_early_return: bool


@dataclass(frozen=True)
class JDWPReferenceType:
    """One loaded type returned by VirtualMachine/ClassesBySignature."""

    type_tag: int
    reference_type_id: int
    status: int


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
        # Packet bytes remain here until a complete JDWP packet is available.
        # In particular, socket.timeout must not discard a partial header/body.
        self._recv_buffer = bytearray()
        self._connection_state_lock = threading.Lock()
        self._connection_generation = 0
        # A JDWP connection has exactly one reader.  The lock is re-entrant
        # because command()/wait_for_event() own the read operation while
        # _read_packet() and _recv() enforce the same invariant internally.
        self._reader_lock = threading.RLock()

    # -- connection --

    def connect(self, host: str, port: int, timeout: float = 5.0) -> None:
        started_at = time.monotonic()
        logger.info(
            "java_runtime.jdwp.connect.start host=%s port=%s timeout=%s",
            host, port, timeout,
        )
        with self._reader_lock:
            # A new transport must never inherit framing bytes from an old one.
            self.close()
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                sock.connect((host, port))
                with self._connection_state_lock:
                    self._sock = sock
                    self._recv_buffer.clear()
                    self._connection_generation += 1
                sock.sendall(b"JDWP-Handshake")
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
        # Do not take _reader_lock here: closing the socket is also the
        # emergency mechanism for waking a reader blocked in recv().
        with self._connection_state_lock:
            sock = self._sock
            was_connected = sock is not None
            self._sock = None
            self._recv_buffer.clear()
            self._connection_generation += 1
            self._pending_replies.clear()
            self._pending_events.clear()
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass
            try:
                sock.close()
            except Exception:
                pass
        if was_connected:
            logger.info("java_runtime.jdwp.connection.closed")

    def _fill_recv_buffer(
        self,
        n: int,
        *,
        deadline: float | None = None,
    ) -> None:
        """Read until at least ``n`` bytes are buffered, preserving timeouts."""
        while True:
            with self._connection_state_lock:
                if len(self._recv_buffer) >= n:
                    return
                sock = self._sock
                buffered = len(self._recv_buffer)
            if sock is None:
                raise JDWPError(-1, "Not connected")
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("JDWP packet read timed out")
                try:
                    sock.settimeout(max(remaining, 0.001))
                except OSError:
                    with self._connection_state_lock:
                        if self._sock is sock:
                            self._recv_buffer.clear()
                    raise
            try:
                chunk = sock.recv(n - buffered)
            except socket.timeout:
                # The bytes already received are valid framing state and must
                # be continued by the next read attempt.
                raise
            except OSError:
                with self._connection_state_lock:
                    if self._sock is sock:
                        self._recv_buffer.clear()
                raise
            if not chunk:
                with self._connection_state_lock:
                    if self._sock is sock:
                        self._recv_buffer.clear()
                raise JDWPError(-1, "Connection closed by remote")
            with self._connection_state_lock:
                if self._sock is not sock:
                    raise JDWPError(-1, "Connection closed while receiving")
                self._recv_buffer.extend(chunk)

    def _recv(self, n: int) -> bytes:
        with self._reader_lock:
            self._fill_recv_buffer(n)
            with self._connection_state_lock:
                result = bytes(self._recv_buffer[:n])
                del self._recv_buffer[:n]
            return result

    def _query_id_sizes(self) -> None:
        err, data = self.command(Cmd.VM, 7)  # VM/IDSizes
        if err:
            raise JDWPError(err)
        field_id, method_id, object_id, ref_type_id, frame_id = \
            struct.unpack(">IIIII", data)
        self.ids = IDSizes(field_id, method_id, object_id, ref_type_id, frame_id)

    # -- command / reply / event routing --

    def _read_packet(self, timeout: float | None = None) -> dict:
        with self._reader_lock:
            with self._connection_state_lock:
                sock = self._sock
                connection_generation = self._connection_generation
            if sock is None:
                raise JDWPError(-1, "Not connected")
            try:
                previous_timeout = sock.gettimeout()
                deadline = (
                    time.monotonic() + max(timeout, 0.0)
                    if timeout is not None
                    else None
                )
                if timeout is not None:
                    sock.settimeout(max(timeout, 0.001))
            except OSError:
                with self._connection_state_lock:
                    if self._sock is sock:
                        self._recv_buffer.clear()
                raise
            try:
                # Peek at the header first.  It is intentionally left in the
                # persistent buffer until the entire body has arrived.
                self._fill_recv_buffer(11, deadline=deadline)
                with self._connection_state_lock:
                    if (
                        self._sock is not sock
                        or self._connection_generation != connection_generation
                        or len(self._recv_buffer) < 11
                    ):
                        raise JDWPError(
                            -1,
                            "Connection changed while reading packet header",
                        )
                    header = bytes(self._recv_buffer[:11])
                length, packet_id, flags = struct.unpack(">IIB", header[:9])
                if length < 11:
                    with self._connection_state_lock:
                        if (
                            self._sock is sock
                            and self._connection_generation
                            == connection_generation
                        ):
                            self._recv_buffer.clear()
                    raise JDWPError(-1, f"Invalid packet length: {length}")
                self._fill_recv_buffer(length, deadline=deadline)
                with self._connection_state_lock:
                    if (
                        self._sock is not sock
                        or self._connection_generation != connection_generation
                        or len(self._recv_buffer) < length
                    ):
                        raise JDWPError(
                            -1,
                            "Connection changed while reading packet",
                        )
                    raw = bytes(self._recv_buffer[:length])
                    del self._recv_buffer[:length]
                header = raw[:11]
                payload = raw[11:]
            finally:
                if timeout is not None:
                    with self._connection_state_lock:
                        still_connected = self._sock is sock
                    if still_connected:
                        try:
                            sock.settimeout(previous_timeout)
                        except OSError:
                            with self._connection_state_lock:
                                if self._sock is sock:
                                    self._recv_buffer.clear()

        if flags == 0x80:
            return {
                "type": "reply",
                "id": packet_id,
                "error": struct.unpack(">H", header[9:11])[0],
                "data": payload,
                "_connection_generation": connection_generation,
            }
        return {
            "type": "command",
            "id": packet_id,
            "command_set": header[9],
            "command": header[10],
            "data": payload,
            "_connection_generation": connection_generation,
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
        packet_generation = packet.pop(
            "_connection_generation",
            self._connection_generation,
        )
        with self._connection_state_lock:
            connection_is_current = (
                self._sock is not None
                and packet_generation == self._connection_generation
            )
            if not connection_is_current:
                logger.debug(
                    "java_runtime.jdwp.packet.discard_stale "
                    "packet_generation=%s connection_generation=%s",
                    packet_generation,
                    self._connection_generation,
                )
                return
            if packet["type"] == "reply":
                self._pending_replies[packet["id"]] = (
                    packet["error"], packet["data"]
                )
                return

        # Event/Composite is a target-to-debugger notification.  The JDWP
        # specification explicitly says VM events do not require a reply.
        if packet["command_set"] == 64 and packet["command"] == 100:
            composite = self._parse_composite_event(packet["data"])
            with self._connection_state_lock:
                if (
                    self._sock is None
                    or packet_generation != self._connection_generation
                ):
                    logger.debug(
                        "java_runtime.jdwp.event.discard_stale "
                        "packet_id=%s packet_generation=%s "
                        "connection_generation=%s",
                        packet["id"],
                        packet_generation,
                        self._connection_generation,
                    )
                    return
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
        with self._reader_lock:
            with self._connection_state_lock:
                if self._pending_events:
                    logger.debug("java_runtime.jdwp.event.dequeue source=pending")
                    return self._pending_events.popleft()

            deadline = time.monotonic() + max(timeout, 0.0)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.debug(
                        "java_runtime.jdwp.event.wait.timeout timeout=%s", timeout
                    )
                    return None
                try:
                    self._route_packet(self._read_packet(timeout=remaining))
                except socket.timeout:
                    logger.debug(
                        "java_runtime.jdwp.event.wait.timeout timeout=%s", timeout
                    )
                    return None
                with self._connection_state_lock:
                    if self._pending_events:
                        logger.debug("java_runtime.jdwp.event.dequeue source=socket")
                        return self._pending_events.popleft()

    def drain_events(self) -> list[dict]:
        """Return already queued/readable VM events without blocking."""
        with self._reader_lock:
            import select

            with self._connection_state_lock:
                sock = self._sock
                if sock is None:
                    return []
                events = list(self._pending_events)
                self._pending_events.clear()
            while True:
                try:
                    readable = select.select([sock], [], [], 0)[0]
                except (OSError, ValueError):
                    break
                if not readable:
                    break
                try:
                    self._route_packet(self._read_packet(timeout=0.05))
                except (socket.timeout, JDWPError, OSError):
                    break
                with self._connection_state_lock:
                    while self._pending_events:
                        events.append(self._pending_events.popleft())
            return events

    def command(self, cmd_set: int, cmd: int, data: bytes = b"") -> tuple[int, bytes]:
        """Send a command and return (error_code, reply_data)."""
        return self._command(cmd_set, cmd, data)

    def _command(
        self,
        cmd_set: int,
        cmd: int,
        data: bytes = b"",
        *,
        outcome_unknown_operation: str | None = None,
    ) -> tuple[int, bytes]:
        """Send a command, optionally preserving an unknown mutation outcome.

        Ordinary read-only commands keep the historical exception behavior.
        State-changing commands such as RedefineClasses opt in to an
        ``JDWPCommandOutcomeUnknown`` when transport or packet processing fails
        after transmission begins: the caller must not claim that the target
        VM still contains the old definition.
        """
        with self._reader_lock:
            with self._connection_state_lock:
                sock = self._sock
            if sock is None:
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
            try:
                sock.sendall(raw)
            except OSError as exc:
                with self._connection_state_lock:
                    if self._sock is sock:
                        self._recv_buffer.clear()
                if outcome_unknown_operation is not None:
                    raise JDWPCommandOutcomeUnknown(
                        packet_id=packet_id,
                        command_set=cmd_set,
                        command=cmd,
                        operation=outcome_unknown_operation,
                        cause=exc,
                    ) from exc
                raise
            try:
                while packet_id not in self._pending_replies:
                    self._route_packet(self._read_packet())
            except Exception as exc:
                if outcome_unknown_operation is not None:
                    raise JDWPCommandOutcomeUnknown(
                        packet_id=packet_id,
                        command_set=cmd_set,
                        command=cmd,
                        operation=outcome_unknown_operation,
                        cause=exc,
                    ) from exc
                raise
            error, reply = self._pending_replies.pop(packet_id)
            logger.debug(
                "java_runtime.jdwp.command.reply packet_id=%s command_set=%s command=%s "
                "error_code=%s response_bytes=%s elapsed_ms=%.1f",
                packet_id, cmd_set, cmd, error, len(reply),
                (time.monotonic() - started_at) * 1000,
            )
            return error, reply

    @staticmethod
    def _raise_rejected(
        error: int,
        *,
        command_set: int,
        command: int,
        operation: str,
    ) -> None:
        if error:
            raise JDWPCommandRejected(
                error,
                command_set=command_set,
                command=command,
                operation=operation,
            )

    def capabilities_new(self) -> JDWPCapabilities:
        """Return the target VM's extended JDWP capabilities."""
        command = 17  # VirtualMachine/CapabilitiesNew
        operation = "VirtualMachine/CapabilitiesNew"
        error, data = self.command(Cmd.VM, command)
        self._raise_rejected(
            error,
            command_set=Cmd.VM,
            command=command,
            operation=operation,
        )
        if len(data) != 32:
            raise JDWPError(
                -1,
                f"{operation} returned {len(data)} bytes; expected 32",
            )
        values = [bool(value) for value in data[:21]]
        return JDWPCapabilities(*values)

    def classes_by_signature(self, signature: str) -> list[JDWPReferenceType]:
        """Return all loaded reference types matching one JVM signature."""
        if not signature:
            raise ValueError("signature must not be empty")
        encoded_signature = signature.encode("utf-8")
        if len(encoded_signature) > 0x7FFFFFFF:
            raise ValueError("signature is too large for a JDWP string")

        command = 2  # VirtualMachine/ClassesBySignature
        operation = "VirtualMachine/ClassesBySignature"
        payload = struct.pack(">I", len(encoded_signature)) + encoded_signature
        error, data = self.command(Cmd.VM, command, payload)
        self._raise_rejected(
            error,
            command_set=Cmd.VM,
            command=command,
            operation=operation,
        )

        ids = self.ids
        if ids is None or ids.reference_type_id_size <= 0:
            raise JDWPError(-1, "ID sizes have not been negotiated")
        if len(data) < 4:
            raise JDWPError(-1, f"{operation} reply is truncated")
        count = struct.unpack_from(">I", data, 0)[0]
        entry_size = 1 + ids.reference_type_id_size + 4
        expected_size = 4 + count * entry_size
        if len(data) != expected_size:
            raise JDWPError(
                -1,
                (
                    f"{operation} returned {len(data)} bytes; "
                    f"expected {expected_size} for {count} classes"
                ),
            )

        result: list[JDWPReferenceType] = []
        offset = 4
        for _ in range(count):
            type_tag = data[offset]
            offset += 1
            reference_type_id = int.from_bytes(
                data[offset:offset + ids.reference_type_id_size],
                "big",
            )
            offset += ids.reference_type_id_size
            status = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            result.append(
                JDWPReferenceType(
                    type_tag=type_tag,
                    reference_type_id=reference_type_id,
                    status=status,
                )
            )
        return result

    def reference_type_class_loader(self, reference_type_id: int) -> int:
        """Return the defining class loader object id, or 0 for bootstrap."""
        if reference_type_id <= 0:
            raise ValueError("reference_type_id must be positive")
        ids = self.ids
        if ids is None or ids.reference_type_id_size <= 0 or ids.object_id_size <= 0:
            raise JDWPError(-1, "ID sizes have not been negotiated")

        command = 2  # ReferenceType/ClassLoader
        operation = "ReferenceType/ClassLoader"
        try:
            payload = ids.pack_ref(reference_type_id)
        except OverflowError as exc:
            raise ValueError("reference_type_id does not fit the negotiated size") from exc
        error, data = self.command(Cmd.REF_TYPE, command, payload)
        self._raise_rejected(
            error,
            command_set=Cmd.REF_TYPE,
            command=command,
            operation=operation,
        )
        if len(data) != ids.object_id_size:
            raise JDWPError(
                -1,
                (
                    f"{operation} returned {len(data)} bytes; "
                    f"expected {ids.object_id_size}"
                ),
            )
        return int.from_bytes(data, "big")

    def redefine_classes(self, definitions: Mapping[int, bytes]) -> None:
        """Atomically submit a batch to VirtualMachine/RedefineClasses.

        A non-zero JDWP reply raises ``JDWPCommandRejected`` and proves that
        the VM rejected the batch.  A transport or packet-processing failure
        after transmission begins raises ``JDWPCommandOutcomeUnknown`` because
        the VM may already have applied the definitions.
        """
        if not definitions:
            raise ValueError("at least one class definition is required")
        if len(definitions) > 0x7FFFFFFF:
            raise ValueError("too many class definitions")
        ids = self.ids
        if ids is None or ids.reference_type_id_size <= 0:
            raise JDWPError(-1, "ID sizes have not been negotiated")

        payload = bytearray(struct.pack(">I", len(definitions)))
        for reference_type_id, classfile in definitions.items():
            if reference_type_id <= 0:
                raise ValueError("reference_type_id must be positive")
            if not isinstance(classfile, bytes):
                raise TypeError("class definitions must be bytes")
            if not classfile:
                raise ValueError("class definition must not be empty")
            if len(classfile) > 0x7FFFFFFF:
                raise ValueError("class definition is too large")
            try:
                payload.extend(ids.pack_ref(reference_type_id))
            except OverflowError as exc:
                raise ValueError(
                    "reference_type_id does not fit the negotiated size"
                ) from exc
            payload.extend(struct.pack(">I", len(classfile)))
            payload.extend(classfile)

        command = 18  # VirtualMachine/RedefineClasses
        operation = "VirtualMachine/RedefineClasses"
        error, _reply = self._command(
            Cmd.VM,
            command,
            bytes(payload),
            outcome_unknown_operation=operation,
        )
        self._raise_rejected(
            error,
            command_set=Cmd.VM,
            command=command,
            operation=operation,
        )
