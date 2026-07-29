from __future__ import annotations

import struct

import pytest

from jolink_runtime.adapters.java.jdwp_client import (
    Cmd,
    IDSizes,
    JDWPClient,
    JDWPCommandOutcomeUnknown,
    JDWPCommandRejected,
)


def _client() -> JDWPClient:
    client = JDWPClient()
    client.ids = IDSizes(8, 8, 8, 8, 8)
    return client


def test_capabilities_new_names_redefine_support(monkeypatch) -> None:
    client = _client()
    payload = bytes([0] * 7 + [1] + [0] * 24)
    monkeypatch.setattr(
        client,
        "command",
        lambda command_set, command, data=b"": (
            0,
            payload,
        ),
    )

    capabilities = client.capabilities_new()

    assert capabilities.can_redefine_classes is True
    assert capabilities.can_add_method is False


def test_classes_by_signature_parses_every_loaded_definition(
    monkeypatch,
) -> None:
    client = _client()
    reply = (
        struct.pack(">I", 2)
        + b"\x01"
        + (41).to_bytes(8, "big")
        + struct.pack(">I", 7)
        + b"\x01"
        + (42).to_bytes(8, "big")
        + struct.pack(">I", 3)
    )
    captured: dict[str, object] = {}

    def command(command_set: int, command_id: int, data: bytes = b""):
        captured.update(
            command_set=command_set,
            command=command_id,
            data=data,
        )
        return 0, reply

    monkeypatch.setattr(client, "command", command)

    loaded = client.classes_by_signature("Lexample/Service;")

    assert captured["command_set"] == Cmd.VM
    assert captured["command"] == 2
    assert captured["data"] == (
        struct.pack(">I", len(b"Lexample/Service;"))
        + b"Lexample/Service;"
    )
    assert [item.reference_type_id for item in loaded] == [41, 42]
    assert [item.status for item in loaded] == [7, 3]


def test_reference_type_class_loader_uses_negotiated_id_sizes(
    monkeypatch,
) -> None:
    client = _client()
    captured: dict[str, object] = {}

    def command(command_set: int, command_id: int, data: bytes = b""):
        captured.update(
            command_set=command_set,
            command=command_id,
            data=data,
        )
        return 0, (99).to_bytes(8, "big")

    monkeypatch.setattr(client, "command", command)

    assert client.reference_type_class_loader(41) == 99
    assert captured == {
        "command_set": Cmd.REF_TYPE,
        "command": 2,
        "data": (41).to_bytes(8, "big"),
    }


def test_redefine_classes_sends_one_atomic_batch(monkeypatch) -> None:
    client = _client()
    captured: dict[str, object] = {}

    def command(
        command_set: int,
        command_id: int,
        data: bytes = b"",
        *,
        outcome_unknown_operation: str | None = None,
    ):
        captured.update(
            command_set=command_set,
            command=command_id,
            data=data,
            outcome_unknown_operation=outcome_unknown_operation,
        )
        return 0, b""

    monkeypatch.setattr(client, "_command", command)

    client.redefine_classes({41: b"\xca\xfe", 42: b"\xba\xbe\x00"})

    assert captured["command_set"] == Cmd.VM
    assert captured["command"] == 18
    assert captured["outcome_unknown_operation"] == (
        "VirtualMachine/RedefineClasses"
    )
    assert captured["data"] == (
        struct.pack(">I", 2)
        + (41).to_bytes(8, "big")
        + struct.pack(">I", 2)
        + b"\xca\xfe"
        + (42).to_bytes(8, "big")
        + struct.pack(">I", 3)
        + b"\xba\xbe\x00"
    )


def test_redefine_classes_distinguishes_rejection_from_unknown_outcome(
    monkeypatch,
) -> None:
    client = _client()
    monkeypatch.setattr(
        client,
        "_command",
        lambda *args, **kwargs: (60, b""),
    )
    with pytest.raises(JDWPCommandRejected) as rejected:
        client.redefine_classes({41: b"\xca\xfe"})
    assert rejected.value.code == 60

    def unknown(*args, **kwargs):
        raise JDWPCommandOutcomeUnknown(
            packet_id=7,
            command_set=Cmd.VM,
            command=18,
            operation="VirtualMachine/RedefineClasses",
            cause=OSError("lost"),
        )

    monkeypatch.setattr(client, "_command", unknown)
    with pytest.raises(JDWPCommandOutcomeUnknown):
        client.redefine_classes({41: b"\xca\xfe"})


def test_redefine_transport_failure_after_send_has_unknown_outcome(
    monkeypatch,
) -> None:
    class SentSocket:
        def __init__(self) -> None:
            self.sent = b""

        def sendall(self, payload: bytes) -> None:
            self.sent = payload

    client = _client()
    sock = SentSocket()
    client._sock = sock

    def fail_reply(*args, **kwargs):
        raise OSError("connection lost")

    monkeypatch.setattr(client, "_read_packet", fail_reply)

    with pytest.raises(JDWPCommandOutcomeUnknown) as unknown:
        client.redefine_classes({41: b"\xca\xfe"})

    assert sock.sent
    assert unknown.value.command_set == Cmd.VM
    assert unknown.value.command == 18
