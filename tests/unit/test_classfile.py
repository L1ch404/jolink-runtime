from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from jolink_runtime.adapters.java.classfile import (
    ClassFileChangeKind,
    ClassFileFormatError,
    compare_class_file_bytes,
    parse_class_file,
)


def _compile(
    root: Path,
    variant: str,
    source: str,
    *,
    class_name: str = "Example",
) -> bytes:
    javac = shutil.which("javac")
    if javac is None:
        pytest.skip("javac is required for class-file comparison tests")
    directory = root / variant
    output = directory / "classes"
    output.mkdir(parents=True)
    source_file = directory / f"{class_name}.java"
    source_file.write_text(source, encoding="utf-8")
    subprocess.run(
        [
            javac,
            "-encoding",
            "UTF-8",
            "-g",
            "-d",
            str(output),
            str(source_file),
        ],
        check=True,
        capture_output=True,
    )
    return (output / f"{class_name}.class").read_bytes()


def test_method_body_only_change_is_accepted(tmp_path: Path) -> None:
    baseline = _compile(
        tmp_path,
        "before",
        """\
public class Example {
    public String value() {
        return "旧值";
    }
}
""",
    )
    staged = _compile(
        tmp_path,
        "after",
        """\
public class Example {
    public String value() {
        return "新值";
    }
}
""",
    )

    parsed = parse_class_file(staged)
    comparison = compare_class_file_bytes(baseline, staged)

    assert parsed.binary_name == "Example"
    assert comparison.kind is ClassFileChangeKind.METHOD_BODY_ONLY
    assert comparison.reasons == ()


def test_method_body_change_with_unchanged_static_initializer_is_accepted(
    tmp_path: Path,
) -> None:
    baseline = _compile(
        tmp_path,
        "before",
        """\
public class Example {
    public static String VALUE = "stable";
    public String value() { return "before"; }
}
""",
    )
    staged = _compile(
        tmp_path,
        "after",
        """\
public class Example {
    public static String VALUE = "stable";
    public String value() { return "after"; }
}
""",
    )

    comparison = compare_class_file_bytes(baseline, staged)

    assert comparison.kind is ClassFileChangeKind.METHOD_BODY_ONLY
    assert comparison.reasons == ()


def test_static_initializer_change_is_rejected(tmp_path: Path) -> None:
    baseline = _compile(
        tmp_path,
        "before",
        "public class Example { public static String VALUE = \"old\"; }",
    )
    staged = _compile(
        tmp_path,
        "after",
        "public class Example { public static String VALUE = \"new\"; }",
    )

    comparison = compare_class_file_bytes(baseline, staged)

    assert comparison.kind is ClassFileChangeKind.UNSUPPORTED
    assert "static_initializer_changed" in comparison.reasons


def test_static_initializer_invokedynamic_change_is_rejected(
    tmp_path: Path,
) -> None:
    baseline = _compile(
        tmp_path,
        "before",
        """\
public class Example {
    public static String VALUE =
        "prefix-old-" + System.getProperty("example.value");
}
""",
    )
    staged = _compile(
        tmp_path,
        "after",
        """\
public class Example {
    public static String VALUE =
        "prefix-new-" + System.getProperty("example.value");
}
""",
    )

    comparison = compare_class_file_bytes(baseline, staged)

    assert comparison.kind is ClassFileChangeKind.UNSUPPORTED
    assert "static_initializer_changed" in comparison.reasons


@pytest.mark.parametrize(
    ("before", "after", "expected_reason"),
    [
        (
            "public class Example { public int value() { return 1; } }",
            (
                "public class Example { private int added; "
                "public int value() { return 1; } }"
            ),
            "field_table_changed",
        ),
        (
            "public class Example { public int value() { return 1; } }",
            (
                "public class Example { "
                "public long value() { return 1L; } }"
            ),
            "method_table_changed",
        ),
        (
            (
                "public class Example { "
                "public static final int VALUE = 1; }"
            ),
            (
                "public class Example { "
                "public static final int VALUE = 2; }"
            ),
            "field_metadata_changed",
        ),
        (
            "public class Example { public int value() { return 1; } }",
            (
                "@Deprecated public class Example { "
                "public int value() { return 1; } }"
            ),
            "class_metadata_changed",
        ),
    ],
)
def test_framework_or_linkage_visible_change_is_rejected(
    tmp_path: Path,
    before: str,
    after: str,
    expected_reason: str,
) -> None:
    baseline = _compile(tmp_path, "before", before)
    staged = _compile(tmp_path, "after", after)

    comparison = compare_class_file_bytes(baseline, staged)

    assert comparison.kind is ClassFileChangeKind.UNSUPPORTED
    assert expected_reason in comparison.reasons


def test_identical_class_bytes_are_unchanged(tmp_path: Path) -> None:
    class_bytes = _compile(
        tmp_path,
        "same",
        "public class Example { public int value() { return 1; } }",
    )

    comparison = compare_class_file_bytes(class_bytes, class_bytes)

    assert comparison.kind is ClassFileChangeKind.UNCHANGED
    assert comparison.reasons == ()


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not-a-class",
        b"\xca\xfe\xba\xbe\x00\x00",
        b"\xca\xfe\xba\xbe\x00\x00\x00\x34\x00\x02\xff",
    ],
)
def test_malformed_class_files_are_rejected(payload: bytes) -> None:
    with pytest.raises(ClassFileFormatError):
        parse_class_file(payload)
