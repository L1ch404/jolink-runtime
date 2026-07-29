"""Small, side-effect-free Java class-file parser for HotSwap preflight.

The parser deliberately models linkage structure and metadata rather than
bytecode instructions.  A changed ``Code`` attribute (including its nested
line-number, local-variable, and stack-map attributes) is therefore accepted
as a method-body change, while changes that can alter linkage or reflective
framework behaviour are rejected before JDWP ``RedefineClasses`` is attempted.
"""

from __future__ import annotations

import hashlib
import stat
import struct
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


_CLASS_MAGIC = 0xCAFEBABE
_MAX_CLASS_FILE_BYTES = 16 * 1024 * 1024


class ClassFileFormatError(ValueError):
    """Raised when untrusted input is not a bounded, valid class-file shape."""


class ClassFileChangeKind(str, Enum):
    """Conservative result of comparing staged and previously built bytes."""

    UNCHANGED = "unchanged"
    METHOD_BODY_ONLY = "method_body_only"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ClassMember:
    """A field or method table entry with normalized non-Code metadata."""

    name: str
    descriptor: str
    access_flags: int
    metadata: tuple[tuple[str, Any], ...]

    @property
    def layout(self) -> tuple[str, str, int]:
        return (self.name, self.descriptor, self.access_flags)


@dataclass(frozen=True)
class ParsedClassFile:
    """The class structure needed by the standard-HotSwap safety gate."""

    binary_name: str
    internal_name: str
    minor_version: int
    major_version: int
    access_flags: int
    super_binary_name: str | None
    interfaces: tuple[str, ...]
    fields: tuple[ClassMember, ...]
    methods: tuple[ClassMember, ...]
    metadata: tuple[tuple[str, Any], ...]
    byte_sha256: str


@dataclass(frozen=True)
class ClassFileComparison:
    """A stable comparison result suitable for a higher-level update plan."""

    kind: ClassFileChangeKind
    baseline_binary_name: str
    staged_binary_name: str
    baseline_major_version: int
    staged_major_version: int
    reasons: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return self.kind is not ClassFileChangeKind.UNSUPPORTED

    def to_dict(self) -> dict[str, object]:
        return {
            "change_kind": self.kind.value,
            "supported": self.supported,
            "baseline_binary_name": self.baseline_binary_name,
            "staged_binary_name": self.staged_binary_name,
            "baseline_major_version": self.baseline_major_version,
            "staged_major_version": self.staged_major_version,
            "reasons": list(self.reasons),
        }


class _Reader:
    def __init__(self, data: bytes, *, context: str = "class file") -> None:
        self._data = memoryview(data)
        self._offset = 0
        self._context = context

    def take(self, size: int) -> bytes:
        if size < 0 or self._offset + size > len(self._data):
            raise ClassFileFormatError(
                f"Truncated {self._context} at byte {self._offset}."
            )
        start = self._offset
        self._offset += size
        return bytes(self._data[start : start + size])

    def u1(self) -> int:
        return self.take(1)[0]

    def u2(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def u4(self) -> int:
        return struct.unpack(">I", self.take(4))[0]

    def finish(self) -> None:
        if self._offset != len(self._data):
            raise ClassFileFormatError(
                f"Unexpected trailing bytes in {self._context}."
            )


@dataclass(frozen=True)
class _ConstantPool:
    entries: tuple[tuple[int, Any] | None, ...]

    def _entry(self, index: int, expected: int | None = None) -> tuple[int, Any]:
        if index <= 0 or index >= len(self.entries):
            raise ClassFileFormatError(
                f"Constant-pool index {index} is out of range."
            )
        entry = self.entries[index]
        if entry is None:
            raise ClassFileFormatError(
                f"Constant-pool index {index} is not addressable."
            )
        if expected is not None and entry[0] != expected:
            raise ClassFileFormatError(
                f"Constant-pool index {index} has tag {entry[0]}, "
                f"expected {expected}."
            )
        return entry

    def utf8(self, index: int) -> str:
        raw = self._entry(index, 1)[1]
        try:
            # Modified UTF-8 encodes NUL as C0 80 and supplementary code
            # points as a UTF-16 surrogate pair.  Semantic identifiers and
            # signatures must not depend on their constant-pool index.
            replaced = raw.replace(b"\xc0\x80", b"\x00")
            decoded = replaced.decode("utf-8", errors="surrogatepass")
            return decoded.encode(
                "utf-16", errors="surrogatepass"
            ).decode("utf-16")
        except UnicodeError as error:
            raise ClassFileFormatError(
                f"Constant-pool UTF-8 entry {index} is invalid."
            ) from error

    def class_internal_name(self, index: int) -> str:
        return self.utf8(self._entry(index, 7)[1])

    def class_binary_name(self, index: int) -> str:
        return self.class_internal_name(index).replace("/", ".")

    def name_and_type(self, index: int) -> tuple[str, str]:
        name_index, descriptor_index = self._entry(index, 12)[1]
        return (self.utf8(name_index), self.utf8(descriptor_index))

    def constant(self, index: int) -> tuple[str, Any]:
        tag, value = self._entry(index)
        if tag == 1:
            return ("utf8", self.utf8(index))
        if tag == 3:
            return ("integer", struct.unpack(">i", struct.pack(">I", value))[0])
        if tag == 4:
            return ("float_bits", value)
        if tag == 5:
            return ("long", struct.unpack(">q", struct.pack(">Q", value))[0])
        if tag == 6:
            return ("double_bits", value)
        if tag == 7:
            return ("class", self.class_internal_name(index))
        if tag == 8:
            return ("string", self.utf8(value))
        raise ClassFileFormatError(
            f"Constant-pool entry {index} is not a supported constant value."
        )


def _parse_constant_pool(reader: _Reader) -> _ConstantPool:
    count = reader.u2()
    if count == 0:
        raise ClassFileFormatError("A class file has no constant pool.")
    entries: list[tuple[int, Any] | None] = [None] * count
    index = 1
    while index < count:
        tag = reader.u1()
        if tag == 1:
            entries[index] = (tag, reader.take(reader.u2()))
        elif tag in {3, 4}:
            entries[index] = (tag, reader.u4())
        elif tag in {5, 6}:
            high = reader.u4()
            low = reader.u4()
            entries[index] = (tag, (high << 32) | low)
            index += 1
            if index >= count:
                raise ClassFileFormatError(
                    "A wide constant occupies a missing pool slot."
                )
        elif tag in {7, 8, 16, 19, 20}:
            entries[index] = (tag, reader.u2())
        elif tag in {9, 10, 11, 12, 17, 18}:
            entries[index] = (tag, (reader.u2(), reader.u2()))
        elif tag == 15:
            entries[index] = (tag, (reader.u1(), reader.u2()))
        else:
            raise ClassFileFormatError(
                f"Unsupported constant-pool tag {tag} at index {index}."
            )
        index += 1
    return _ConstantPool(tuple(entries))


def _annotation_value(reader: _Reader, pool: _ConstantPool) -> Any:
    tag = chr(reader.u1())
    if tag in "BCDFIJSZs":
        return (tag, pool.constant(reader.u2()))
    if tag == "e":
        return (tag, pool.utf8(reader.u2()), pool.utf8(reader.u2()))
    if tag == "c":
        return (tag, pool.utf8(reader.u2()))
    if tag == "@":
        return (tag, _annotation(reader, pool))
    if tag == "[":
        return (
            tag,
            tuple(_annotation_value(reader, pool) for _ in range(reader.u2())),
        )
    raise ClassFileFormatError(f"Unknown annotation element tag {tag!r}.")


def _annotation(reader: _Reader, pool: _ConstantPool) -> Any:
    annotation_type = pool.utf8(reader.u2())
    pairs = tuple(
        (pool.utf8(reader.u2()), _annotation_value(reader, pool))
        for _ in range(reader.u2())
    )
    return (annotation_type, pairs)


def _annotations(reader: _Reader, pool: _ConstantPool) -> Any:
    return tuple(_annotation(reader, pool) for _ in range(reader.u2()))


def _parameter_annotations(reader: _Reader, pool: _ConstantPool) -> Any:
    return tuple(
        tuple(_annotation(reader, pool) for _ in range(reader.u2()))
        for _ in range(reader.u1())
    )


def _type_annotation_target(reader: _Reader, target_type: int) -> Any:
    if target_type in {0x00, 0x01, 0x16}:
        return (reader.u1(),)
    if target_type in {0x10, 0x17, 0x42, 0x43, 0x44, 0x45, 0x46}:
        return (reader.u2(),)
    if target_type in {0x11, 0x12}:
        return (reader.u1(), reader.u1())
    if target_type in {0x13, 0x14, 0x15}:
        return ()
    if target_type in {0x40, 0x41}:
        return tuple(
            (reader.u2(), reader.u2(), reader.u2())
            for _ in range(reader.u2())
        )
    if 0x47 <= target_type <= 0x4B:
        return (reader.u2(), reader.u1())
    raise ClassFileFormatError(
        f"Unknown type-annotation target type 0x{target_type:02x}."
    )


def _type_annotations(reader: _Reader, pool: _ConstantPool) -> Any:
    values: list[Any] = []
    for _ in range(reader.u2()):
        target_type = reader.u1()
        target = _type_annotation_target(reader, target_type)
        path = tuple((reader.u1(), reader.u1()) for _ in range(reader.u1()))
        values.append((target_type, target, path, _annotation(reader, pool)))
    return tuple(values)


def _normalize_attribute(
    name: str,
    payload: bytes,
    pool: _ConstantPool,
    *,
    owner: str,
) -> Any:
    reader = _Reader(payload, context=f"{owner} attribute {name}")
    if name in {"Synthetic", "Deprecated"}:
        value: Any = True
    elif name in {"Signature", "SourceFile"}:
        value = pool.utf8(reader.u2())
    elif name == "ConstantValue":
        value = pool.constant(reader.u2())
    elif name == "Exceptions":
        value = tuple(
            pool.class_binary_name(reader.u2()) for _ in range(reader.u2())
        )
    elif name == "MethodParameters":
        value = tuple(
            (
                pool.utf8(name_index) if (name_index := reader.u2()) else None,
                reader.u2(),
            )
            for _ in range(reader.u1())
        )
    elif name in {
        "RuntimeVisibleAnnotations",
        "RuntimeInvisibleAnnotations",
    }:
        value = _annotations(reader, pool)
    elif name in {
        "RuntimeVisibleParameterAnnotations",
        "RuntimeInvisibleParameterAnnotations",
    }:
        value = _parameter_annotations(reader, pool)
    elif name in {
        "RuntimeVisibleTypeAnnotations",
        "RuntimeInvisibleTypeAnnotations",
    }:
        value = _type_annotations(reader, pool)
    elif name == "AnnotationDefault":
        value = _annotation_value(reader, pool)
    elif name == "InnerClasses":
        records = []
        for _ in range(reader.u2()):
            inner = reader.u2()
            outer = reader.u2()
            inner_name = reader.u2()
            records.append(
                (
                    pool.class_binary_name(inner) if inner else None,
                    pool.class_binary_name(outer) if outer else None,
                    pool.utf8(inner_name) if inner_name else None,
                    reader.u2(),
                )
            )
        value = tuple(records)
    elif name == "EnclosingMethod":
        enclosing_class = pool.class_binary_name(reader.u2())
        method_index = reader.u2()
        value = (
            enclosing_class,
            pool.name_and_type(method_index) if method_index else None,
        )
    elif name == "NestHost":
        value = pool.class_binary_name(reader.u2())
    elif name in {"NestMembers", "PermittedSubclasses"}:
        value = tuple(
            pool.class_binary_name(reader.u2()) for _ in range(reader.u2())
        )
    elif name == "Record":
        components = []
        for _ in range(reader.u2()):
            component_name = pool.utf8(reader.u2())
            descriptor = pool.utf8(reader.u2())
            components.append(
                (
                    component_name,
                    descriptor,
                    _parse_attributes(reader, pool, owner="record component"),
                )
            )
        value = tuple(components)
    else:
        # Unknown attributes are retained conservatively.  A changed digest
        # rejects the update instead of silently accepting metadata that a
        # framework or a newer VM may interpret.
        return ("raw_sha256", hashlib.sha256(payload).hexdigest())
    reader.finish()
    return value


def _parse_attributes(
    reader: _Reader,
    pool: _ConstantPool,
    *,
    owner: str,
) -> tuple[tuple[str, Any], ...]:
    attributes: list[tuple[str, Any]] = []
    for _ in range(reader.u2()):
        name = pool.utf8(reader.u2())
        payload = reader.take(reader.u4())
        if owner == "method" and name == "Code":
            continue
        if owner == "class" and name in {
            "BootstrapMethods",
            "SourceDebugExtension",
        }:
            # Both describe executable/debug details rather than class schema.
            continue
        attributes.append(
            (
                name,
                _normalize_attribute(name, payload, pool, owner=owner),
            )
        )
    return tuple(attributes)


def _parse_members(
    reader: _Reader,
    pool: _ConstantPool,
    *,
    owner: str,
) -> tuple[ClassMember, ...]:
    members = []
    for _ in range(reader.u2()):
        access_flags = reader.u2()
        name = pool.utf8(reader.u2())
        descriptor = pool.utf8(reader.u2())
        members.append(
            ClassMember(
                name=name,
                descriptor=descriptor,
                access_flags=access_flags,
                metadata=_parse_attributes(reader, pool, owner=owner),
            )
        )
    return tuple(members)


def parse_class_file(data: bytes) -> ParsedClassFile:
    """Parse bounded class bytes without loading or executing the class."""

    if not isinstance(data, bytes):
        raise TypeError("class-file input must be bytes")
    if len(data) > _MAX_CLASS_FILE_BYTES:
        raise ClassFileFormatError("Class file exceeds the size limit.")
    reader = _Reader(data)
    if reader.u4() != _CLASS_MAGIC:
        raise ClassFileFormatError("Input does not have the Java class magic.")
    minor_version = reader.u2()
    major_version = reader.u2()
    pool = _parse_constant_pool(reader)
    access_flags = reader.u2()
    this_class = reader.u2()
    super_class = reader.u2()
    internal_name = pool.class_internal_name(this_class)
    interfaces = tuple(
        pool.class_binary_name(reader.u2()) for _ in range(reader.u2())
    )
    fields = _parse_members(reader, pool, owner="field")
    methods = _parse_members(reader, pool, owner="method")
    metadata = _parse_attributes(reader, pool, owner="class")
    reader.finish()
    return ParsedClassFile(
        binary_name=internal_name.replace("/", "."),
        internal_name=internal_name,
        minor_version=minor_version,
        major_version=major_version,
        access_flags=access_flags,
        super_binary_name=(
            pool.class_binary_name(super_class) if super_class else None
        ),
        interfaces=interfaces,
        fields=fields,
        methods=methods,
        metadata=metadata,
        byte_sha256=hashlib.sha256(data).hexdigest(),
    )


def read_class_file(path: str | Path) -> ParsedClassFile:
    """Read one regular, non-symlink class file and parse its structure."""

    source = Path(path)
    try:
        metadata = source.lstat()
    except OSError as error:
        raise ClassFileFormatError("Class file is not readable.") from error
    if not stat.S_ISREG(metadata.st_mode) or source.is_symlink():
        raise ClassFileFormatError("Class file must be a regular file.")
    if metadata.st_size > _MAX_CLASS_FILE_BYTES:
        raise ClassFileFormatError("Class file exceeds the size limit.")
    try:
        data = source.read_bytes()
    except OSError as error:
        raise ClassFileFormatError("Class file is not readable.") from error
    return parse_class_file(data)


def compare_class_files(
    baseline: ParsedClassFile,
    staged: ParsedClassFile,
) -> ClassFileComparison:
    """Classify a staged class as unchanged, body-only, or unsupported."""

    if not isinstance(baseline, ParsedClassFile) or not isinstance(
        staged, ParsedClassFile
    ):
        raise TypeError("compare_class_files expects parsed class files")

    if baseline.byte_sha256 == staged.byte_sha256:
        kind = ClassFileChangeKind.UNCHANGED
        reasons: tuple[str, ...] = ()
    else:
        detected: list[str] = []
        if baseline.binary_name != staged.binary_name:
            detected.append("binary_name_changed")
        if (
            baseline.major_version,
            baseline.minor_version,
        ) != (
            staged.major_version,
            staged.minor_version,
        ):
            detected.append("class_version_changed")
        if baseline.access_flags != staged.access_flags:
            detected.append("class_access_flags_changed")
        if baseline.super_binary_name != staged.super_binary_name:
            detected.append("super_class_changed")
        if baseline.interfaces != staged.interfaces:
            detected.append("interfaces_changed")

        baseline_field_layout = tuple(item.layout for item in baseline.fields)
        staged_field_layout = tuple(item.layout for item in staged.fields)
        if baseline_field_layout != staged_field_layout:
            detected.append("field_table_changed")
        elif tuple(item.metadata for item in baseline.fields) != tuple(
            item.metadata for item in staged.fields
        ):
            detected.append("field_metadata_changed")

        baseline_method_layout = tuple(item.layout for item in baseline.methods)
        staged_method_layout = tuple(item.layout for item in staged.methods)
        if baseline_method_layout != staged_method_layout:
            detected.append("method_table_changed")
        elif tuple(item.metadata for item in baseline.methods) != tuple(
            item.metadata for item in staged.methods
        ):
            detected.append("method_metadata_changed")

        if baseline.metadata != staged.metadata:
            detected.append("class_metadata_changed")

        reasons = tuple(detected)
        kind = (
            ClassFileChangeKind.UNSUPPORTED
            if reasons
            else ClassFileChangeKind.METHOD_BODY_ONLY
        )

    return ClassFileComparison(
        kind=kind,
        baseline_binary_name=baseline.binary_name,
        staged_binary_name=staged.binary_name,
        baseline_major_version=baseline.major_version,
        staged_major_version=staged.major_version,
        reasons=reasons,
    )


def compare_class_file_bytes(
    baseline: bytes,
    staged: bytes,
) -> ClassFileComparison:
    """Convenience wrapper used by staging code that already owns bytes."""

    return compare_class_files(
        parse_class_file(baseline),
        parse_class_file(staged),
    )


__all__ = [
    "ClassFileChangeKind",
    "ClassFileComparison",
    "ClassFileFormatError",
    "ClassMember",
    "ParsedClassFile",
    "compare_class_file_bytes",
    "compare_class_files",
    "parse_class_file",
    "read_class_file",
]
