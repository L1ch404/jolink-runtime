from __future__ import annotations

import struct

import pytest

from jolink_runtime.adapters.java.evaluation_vm import (
    EvaluationThrown, EvaluationVM, Reader, Value, utf,
)
from jolink_runtime.adapters.java.jdwp_client import (
    Cmd, IDSizes, JDWPCommandOutcomeUnknown, JDWPError, Tag,
)


class _Client:
    def __init__(self, ids):
        self.ids = ids
        self.calls = []
        self.replies = {}
        self.invoke_reply = b""
        self.invoke_error = None

    def command(self, group, command, data=b""):
        self.calls.append((group, command, data, {}))
        response = self.replies.get((group, command), (0, b""))
        if callable(response):
            return response(data)
        return response

    def _command(self, group, command, data=b"", **kwargs):
        self.calls.append((group, command, data, kwargs))
        if self.invoke_error is not None:
            raise self.invoke_error
        return 0, self.invoke_reply


@pytest.fixture(params=[IDSizes(8, 4, 8, 8, 4), IDSizes(4, 8, 4, 4, 8)])
def vm(request):
    return EvaluationVM(_Client(request.param), thread=0x101, timeout=2.5)


def _methods(vm, name="run", signature="()V"):
    vm.jdwp.replies[Cmd.REF_TYPE, 5] = (
        0, struct.pack(">I", 1) + vm.ids.pack_method(0x77)
        + utf(name) + utf(signature) + struct.pack(">I", 1),
    )


@pytest.mark.parametrize("tag,payload", [
    (Tag.BYTE, struct.pack(">b", -100)),
    (Tag.SHORT, struct.pack(">h", -30000)),
    (Tag.INT, struct.pack(">i", -2147483648)),
    (Tag.LONG, struct.pack(">q", 9223372036854775807)),
    (Tag.FLOAT, struct.pack(">I", 0x7FC00001)),
    (Tag.DOUBLE, struct.pack(">d", -0.0)),
    (Tag.CHAR, struct.pack(">H", 0xD800)),
    (Tag.BOOLEAN, b"\x01"),
    (ord("V"), b""),
])
def test_values_preserve_raw_precision_and_tags(vm, tag, payload):
    reader = Reader(bytes([tag]) + payload + b"tail")
    value = vm.value(reader)
    assert value.tagged() == bytes([tag]) + payload
    assert value.reference == 0
    assert reader.take(4) == b"tail"


def test_reader_rejects_truncated_wire_value(vm):
    with pytest.raises(JDWPError, match="Truncated"):
        vm.value(Reader(bytes([Tag.LONG]) + b"\0" * 7))


@pytest.mark.parametrize("mode", ["instance", "static", "constructor"])
def test_invocation_packs_jdwp_ids_arguments_options_and_timeout(vm, mode):
    name = "<init>" if mode == "constructor" else "run"
    _methods(vm, name, "(IJ)V")
    result = vm.obj(0x200) if mode == "constructor" else Value(ord("V"), b"")
    vm.jdwp.invoke_reply = result.tagged() + vm.obj(0).tagged()
    args = (vm.integer(-123), Value(Tag.LONG, struct.pack(">q", 2**62 + 7)))

    returned = vm.invoke(
        0x44, name, "(IJ)V", args,
        receiver=0x55 if mode == "instance" else None,
        constructor=mode == "constructor",
    )

    invocation = next(call for call in vm.jdwp.calls if call[3])
    if mode == "instance":
        prefix = vm.ids.pack_obj(0x55) + vm.ids.pack_obj(0x101) + vm.ids.pack_ref(0x44)
        assert invocation[:2] == (Cmd.OBJ_REF, 6)
    else:
        prefix = vm.ids.pack_ref(0x44) + vm.ids.pack_obj(0x101)
        assert invocation[:2] == (Cmd.CLASS_TYPE, 4 if mode == "constructor" else 3)
    assert invocation[2] == (
        prefix + vm.ids.pack_method(0x77) + struct.pack(">I", 2)
        + b"".join(value.tagged() for value in args) + struct.pack(">I", 1)
    )
    assert invocation[3]["reply_timeout"] == 2.5
    assert invocation[3]["outcome_unknown_operation"] == f"Evaluation {name}"
    assert returned == result
    if mode == "constructor":
        assert vm.pinned == {0x200}


def test_invocation_exception_is_pinned_and_not_treated_as_return_value(vm):
    _methods(vm)
    exception = vm.obj(0x1234)
    vm.jdwp.invoke_reply = Value(ord("V"), b"").tagged() + exception.tagged()
    with pytest.raises(EvaluationThrown) as caught:
        vm.invoke(0x44, "run", "()V", receiver=0x55)
    assert caught.value.value == exception
    assert vm.pinned == {0x1234}
    vm.close()
    assert vm.pinned == set()
    assert (Cmd.OBJ_REF, 8, exception.data, {}) in vm.jdwp.calls


def test_invocation_unknown_outcome_is_not_retried(vm):
    _methods(vm)
    unknown = JDWPCommandOutcomeUnknown(
        packet_id=12, command_set=Cmd.OBJ_REF, command=6,
        operation="run", cause=TimeoutError(),
    )
    vm.jdwp.invoke_error = unknown
    with pytest.raises(JDWPCommandOutcomeUnknown) as caught:
        vm.invoke(0x44, "run", "()V", receiver=0x55)
    assert caught.value is unknown
    assert len([call for call in vm.jdwp.calls if call[3]]) == 1


def test_pin_is_once_per_reference_and_close_releases_remaining_after_error(vm):
    for tag in (Tag.OBJECT, Tag.STRING, Tag.CLASS_LOADER, Tag.ARRAY):
        vm.pin(vm.obj(9, tag))
    vm.pin(vm.obj(10))
    vm.pin(vm.obj(0))
    assert [call[2] for call in vm.jdwp.calls] == [vm.ids.pack_obj(9), vm.ids.pack_obj(10)]
    vm.jdwp.replies[Cmd.OBJ_REF, 8] = (20, b"")
    vm.close()
    assert vm.pinned == set()
    assert sorted(call[2] for call in vm.jdwp.calls if call[:2] == (Cmd.OBJ_REF, 8)) == [
        vm.ids.pack_obj(9), vm.ids.pack_obj(10),
    ]


def test_set_fields_uses_untagged_values_but_set_locals_uses_tagged_values(vm):
    integer, reference = vm.integer(-7), vm.obj(0x123, Tag.STRING)
    vm.set_fields(9, {10: integer, 11: reference})
    assert vm.jdwp.calls[-1][:3] == (Cmd.OBJ_REF, 3,
        vm.ids.pack_obj(9) + struct.pack(">I", 2)
        + vm.ids.pack_field(10) + integer.data
        + vm.ids.pack_field(11) + reference.data)
    locals_ = [
        {"slot": 1, "name": "a", "value": vm.integer(0)},
        {"slot": 2, "name": "s", "value": vm.obj(0)},
        {"slot": 3, "name": "unchanged", "value": integer},
    ]
    vm.write_locals(0x456, locals_, {"a": integer, "s": reference, "unchanged": integer})
    assert vm.jdwp.calls[-1][:3] == (Cmd.STACK, 2,
        vm.ids.pack_obj(0x101) + vm.ids.pack_frame(0x456) + struct.pack(">I", 2)
        + struct.pack(">I", 1) + integer.tagged()
        + struct.pack(">I", 2) + reference.tagged())


def test_instance_fields_read_tagged_values_and_pin_references(vm):
    count = Value(Tag.LONG, struct.pack(">q", 2**62 + 7))
    reference = vm.obj(0x123, Tag.STRING)
    fields = {"count": (21, "J"), "record": (22, "Ljava/lang/Object;")}
    vm.jdwp.replies[Cmd.OBJ_REF, 2] = (0, struct.pack(">I", 2) + count.tagged() + reference.tagged())

    values = vm.get_fields(0x44, fields)

    assert values == {"count": count, "record": reference}
    assert vm.jdwp.calls[0][:3] == (Cmd.OBJ_REF, 2,
        vm.ids.pack_obj(0x44) + struct.pack(">I", 2)
        + vm.ids.pack_field(21) + vm.ids.pack_field(22))
    assert vm.pinned == {0x123}

def test_empty_instance_slots_do_not_send_commands_and_count_mismatch_is_error(vm):
    assert vm.get_fields(0x44, {}) == {}
    vm.set_fields(0x44, {})
    assert vm.jdwp.calls == []
    vm.jdwp.replies[Cmd.OBJ_REF, 2] = (0, struct.pack(">I", 0))
    with pytest.raises(JDWPError, match="Incomplete evaluation field values"):
        vm.get_fields(0x44, {"count": (21, "J")})


def test_field_lookup_ignores_static_slots_and_keeps_nearest_instance_declaration(vm):
    declarations = {
        1: [(11, "shared", "I", 8), (12, "field", "I", 0)],
        2: [(21, "shared", "J", 8), (22, "field", "J", 0), (23, "inherited", "J", 0)],
    }

    def fields(data):
        rows = declarations[int.from_bytes(data, "big")]
        return 0, struct.pack(">I", len(rows)) + b"".join(
            vm.ids.pack_field(field) + utf(name) + utf(signature) + struct.pack(">I", flags)
            for field, name, signature, flags in rows
        )

    vm.jdwp.replies[Cmd.REF_TYPE, 4] = fields
    vm.jdwp.replies[Cmd.CLASS_TYPE, 1] = lambda data: (
        0, vm.ids.pack_ref(2 if int.from_bytes(data, "big") == 1 else 0)
    )
    assert vm.fields(1) == {"field": (12, "I"), "inherited": (23, "J")}


@pytest.mark.parametrize("generic", [True, False])
def test_locals_respect_scope_slot_width_and_generic_signature(vm, generic):
    declarations = [
        (0, "this", "Lexample/App;", "", 20, 0),
        (0, "gone", "I", "", 3, 1),
        (3, "count", "J", "", 10, 1),
        (3, "names", "Ljava/util/List;", "Ljava/util/List<Ljava/lang/String;>;", 10, 3),
        (12, "future", "I", "", 10, 4),
    ]
    reply = struct.pack(">II", 1, len(declarations)) + b"".join(
        struct.pack(">Q", start) + utf(name) + utf(signature)
        + (utf(generic_signature) if generic else b"") + struct.pack(">II", length, slot)
        for start, name, signature, generic_signature, length, slot in declarations
    )
    vm.jdwp.replies[Cmd.METHOD, 5] = (0, reply) if generic else (99, b"")
    vm.jdwp.replies[Cmd.METHOD, 2] = (0, reply)
    count, names = Value(Tag.LONG, struct.pack(">q", 2**62 + 7)), vm.obj(0x321)
    vm.jdwp.replies[Cmd.STACK, 1] = (0, struct.pack(">I", 2) + count.tagged() + names.tagged())
    frame = {"class_id": 0x44, "method_id": 0x77, "location_index": 5, "frame_id": 0x456}

    locals_ = vm.locals(frame)

    assert [(item["slot"], item["name"]) for item in locals_] == [(1, "count"), (3, "names")]
    assert locals_[0]["value"] == count
    assert locals_[1]["value"] == names
    assert locals_[1]["type"] == (declarations[3][3] if generic else declarations[3][2])
    get_values = next(call for call in vm.jdwp.calls if call[:2] == (Cmd.STACK, 1))
    assert get_values[2] == (vm.ids.pack_obj(0x101) + vm.ids.pack_frame(0x456)
        + struct.pack(">I", 2) + struct.pack(">IBIB", 1, Tag.LONG, 3, Tag.OBJECT))


def test_locals_without_debug_table_allow_this_or_static_evaluation(vm):
    vm.jdwp.replies[Cmd.METHOD, 5] = (101, b"")
    vm.jdwp.replies[Cmd.METHOD, 2] = (101, b"")
    assert vm.locals({"class_id": 1, "method_id": 2}) == []
    assert all(call[0] == Cmd.METHOD for call in vm.jdwp.calls)
