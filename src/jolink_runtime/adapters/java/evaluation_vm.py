"""JDWP operations used to execute JDT snippets in the suspended application.

This is a VM bridge, not a Java expression interpreter. Values retain their
wire representation so references and numeric precision survive round trips.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .jdwp_client import Cmd, JDWPClient, JDWPError, Tag
from .jdwp_strings import decode_jdwp_string


def utf(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack(">I", len(raw)) + raw


class Reader:
    def __init__(self, data: bytes):
        self.data, self.offset = data, 0

    def take(self, count: int) -> bytes:
        result = self.data[self.offset:self.offset + count]
        if len(result) != count:
            raise JDWPError(-1, "Truncated evaluation response")
        self.offset += count
        return result

    def number(self, count=4) -> int:
        return int.from_bytes(self.take(count), "big")

    def string(self) -> str:
        return decode_jdwp_string(self.take(self.number()))


@dataclass(frozen=True)
class Value:
    tag: int
    data: bytes

    @property
    def reference(self) -> int:
        return int.from_bytes(self.data, "big") if self.tag in b"[Lstglc" else 0

    def tagged(self) -> bytes:
        return bytes([self.tag]) + self.data


class EvaluationThrown(Exception):
    def __init__(self, value: Value):
        self.value = value
        super().__init__("The target JVM threw an exception during evaluation")


class EvaluationVM:
    def __init__(self, jdwp: JDWPClient, thread: int, timeout: float):
        self.jdwp, self.ids = jdwp, jdwp.ids
        self.thread, self.timeout = thread, max(0.01, timeout)
        self.pinned: set[int] = set()

    def command(self, group: int, command: int, data=b"") -> bytes:
        error, reply = self.jdwp.command(group, command, data)
        if error:
            raise JDWPError(error, f"Evaluation command {group}/{command}")
        return reply

    def value(self, reader: Reader) -> Value:
        tag = reader.number(1)
        size = {ord("V"): 0, ord("B"): 1, ord("Z"): 1,
                ord("C"): 2, ord("S"): 2, ord("I"): 4, ord("F"): 4,
                ord("J"): 8, ord("D"): 8}.get(tag, self.ids.object_id_size)
        return Value(tag, reader.take(size))

    def obj(self, value: int, tag=Tag.OBJECT) -> Value:
        return Value(tag, self.ids.pack_obj(value))

    def integer(self, value: int) -> Value:
        return Value(Tag.INT, struct.pack(">i", value))

    def pin(self, value: Value) -> Value:
        reference = value.reference
        if reference and reference not in self.pinned:
            self.command(Cmd.OBJ_REF, 7, self.ids.pack_obj(reference))
            self.pinned.add(reference)
        return value

    def close(self) -> None:
        for reference in self.pinned:
            try:
                self.command(Cmd.OBJ_REF, 8, self.ids.pack_obj(reference))
            except (JDWPError, OSError):
                pass
        self.pinned.clear()

    def class_id(self, signature: str) -> int:
        classes = self.jdwp.classes_by_signature(signature)
        if not classes:
            raise JDWPError(-1, f"Evaluation requires loaded type {signature}")
        return classes[0].reference_type_id

    def object_type(self, reference: int) -> int:
        reader = Reader(self.command(Cmd.OBJ_REF, 1, self.ids.pack_obj(reference)))
        reader.take(1)
        return reader.number(self.ids.reference_type_id_size)

    def reflected_type(self, class_object: Value) -> int:
        reader = Reader(self.command(17, 1, class_object.data))
        reader.take(1)
        return reader.number(self.ids.reference_type_id_size)

    def signature(self, type_id: int) -> str:
        return Reader(self.command(Cmd.REF_TYPE, 1, self.ids.pack_ref(type_id))).string()

    def loader(self, type_id: int) -> int:
        return int.from_bytes(self.command(Cmd.REF_TYPE, 2, self.ids.pack_ref(type_id)), "big")

    def methods(self, type_id: int):
        reader = Reader(self.command(Cmd.REF_TYPE, 5, self.ids.pack_ref(type_id)))
        return [(reader.number(self.ids.method_id_size), reader.string(),
                 reader.string(), reader.number()) for _ in range(reader.number())]

    def method(self, type_id: int, name: str, signature: str) -> tuple[int, int]:
        while type_id:
            for method_id, method_name, descriptor, _modifiers in self.methods(type_id):
                if (method_name, descriptor) == (name, signature):
                    return type_id, method_id
            type_id = int.from_bytes(self.command(Cmd.CLASS_TYPE, 1, self.ids.pack_ref(type_id)), "big")
        raise JDWPError(-1, f"Method not found: {name}{signature}")

    def invoke(self, type_id: int, name: str, signature: str,
               arguments=(), *, receiver: int | None = None, constructor=False) -> Value:
        declaring, method = self.method(type_id, name, signature)
        if constructor:
            group, command = Cmd.CLASS_TYPE, 4
            prefix = self.ids.pack_ref(type_id) + self.ids.pack_obj(self.thread)
        elif receiver is None:
            group, command = Cmd.CLASS_TYPE, 3
            prefix = self.ids.pack_ref(declaring) + self.ids.pack_obj(self.thread)
        else:
            group, command = Cmd.OBJ_REF, 6
            prefix = (self.ids.pack_obj(receiver) + self.ids.pack_obj(self.thread)
                      + self.ids.pack_ref(declaring))
        payload = (prefix + self.ids.pack_method(method) + struct.pack(">I", len(arguments))
                   + b"".join(value.tagged() for value in arguments)
                   + struct.pack(">I", 1))  # INVOKE_SINGLE_THREADED
        error, reply = self.jdwp._command(
            group, command, payload,
            outcome_unknown_operation=f"Evaluation {name}", reply_timeout=self.timeout,
        )
        if error:
            raise JDWPError(error, f"Evaluation invocation {name}")
        reader = Reader(reply)
        result, exception = self.value(reader), self.value(reader)
        if exception.reference:
            raise EvaluationThrown(self.pin(exception))
        return self.pin(result)

    def string(self, text: str) -> Value:
        return self.pin(Value(Tag.STRING, self.command(Cmd.VM, 11, utf(text))))

    def byte_array(self, data: bytes) -> Value:
        type_id = self.class_id("[B")
        value = self.pin(self.value(Reader(self.command(
            4, 1, self.ids.pack_ref(type_id) + struct.pack(">I", len(data)),
        ))))
        self.command(Cmd.ARRAY, 3, value.data + struct.pack(">II", 0, len(data)) + data)
        return value

    def define(self, loader: int, name: str, data: bytes) -> int:
        loader_type = self.object_type(loader)
        result = self.invoke(loader_type, "defineClass", "(Ljava/lang/String;[BII)Ljava/lang/Class;",
                             (self.string(name), self.byte_array(data), self.integer(0), self.integer(len(data))),
                             receiver=loader)
        # defineClass/resolveClass alone can leave HotSpot's JDWP type in the
        # unprepared state. Initialize the generated class before inspecting it.
        self.invoke(self.class_id("Ljava/lang/Class;"), "forName",
                    "(Ljava/lang/String;ZLjava/lang/ClassLoader;)Ljava/lang/Class;",
                    (self.string(name), Value(Tag.BOOLEAN, b"\x01"), self.obj(loader, Tag.CLASS_LOADER)))
        return self.reflected_type(result)

    def fields(self, type_id: int) -> dict[str, tuple[int, str]]:
        fields = {}
        while type_id:
            reader = Reader(self.command(Cmd.REF_TYPE, 4, self.ids.pack_ref(type_id)))
            for _ in range(reader.number()):
                field, name, signature, flags = (reader.number(self.ids.field_id_size),
                                               reader.string(), reader.string(), reader.number())
                if not flags & 8:
                    fields.setdefault(name, (field, signature))
            type_id = int.from_bytes(self.command(Cmd.CLASS_TYPE, 1, self.ids.pack_ref(type_id)), "big")
        return fields

    def get_fields(self, receiver: int, fields: dict[str, tuple[int, str]]) -> dict[str, Value]:
        if not fields:
            return {}
        reader = Reader(self.command(Cmd.OBJ_REF, 2, self.ids.pack_obj(receiver)
                                    + struct.pack(">I", len(fields))
                                    + b"".join(self.ids.pack_field(field) for field, _ in fields.values())))
        count = reader.number()
        if count != len(fields):
            raise JDWPError(-1, "Incomplete evaluation field values")
        return {name: self.pin(self.value(reader)) for name in fields}

    def set_fields(self, receiver: int, values: dict[int, Value]) -> None:
        if values:
            self.command(Cmd.OBJ_REF, 3, self.ids.pack_obj(receiver)
                         + struct.pack(">I", len(values))
                         + b"".join(self.ids.pack_field(field) + value.data for field, value in values.items()))

    def locals(self, frame: dict) -> list[dict]:
        data = self.ids.pack_ref(frame["class_id"]) + self.ids.pack_method(frame["method_id"])
        error, reply = self.jdwp.command(Cmd.METHOD, 5, data)  # VariableTableWithGeneric
        generic = not error
        if error:
            error, reply = self.jdwp.command(Cmd.METHOD, 2, data)
        if error == 101:  # ABSENT_INFORMATION; this and static expressions still work.
            return []
        if error:
            raise JDWPError(error, "Cannot read evaluation local variables")
        reader = Reader(reply)
        reader.take(4)
        visible = {}
        for _ in range(reader.number()):
            start, name, signature = reader.number(8), reader.string(), reader.string()
            generic_signature = reader.string() if generic else ""
            length, slot = reader.number(), reader.number()
            if start <= frame["location_index"] < start + length and name != "this":
                visible[slot] = dict(name=name, signature=signature,
                                     type=generic_signature or signature, slot=slot)
        values = list(visible.values())
        if values:
            reader = Reader(self.command(Cmd.STACK, 1,
                self.ids.pack_obj(self.thread) + self.ids.pack_frame(frame["frame_id"])
                + struct.pack(">I", len(values)) + b"".join(
                    struct.pack(">IB", value["slot"], Tag.from_sig(value["signature"])) for value in values)))
            if reader.number() != len(values):
                raise JDWPError(-1, "Incomplete evaluation locals")
            for value in values:
                value["value"] = self.pin(self.value(reader))
        return values

    def write_locals(self, frame_id: int, locals_: list[dict], updated: dict[str, Value]) -> None:
        changed = [(local["slot"], updated[local["name"]]) for local in locals_
                   if local["name"] in updated and updated[local["name"]].data != local["value"].data]
        if changed:
            self.command(Cmd.STACK, 2, self.ids.pack_obj(self.thread) + self.ids.pack_frame(frame_id)
                         + struct.pack(">I", len(changed)) + b"".join(
                             struct.pack(">I", slot) + value.tagged() for slot, value in changed))
