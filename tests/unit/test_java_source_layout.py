from pathlib import Path

import pytest

from jolink_runtime.launch.java_source_layout import source_relative_path


@pytest.mark.parametrize(
    ("text", "expected"),
    [
    ("class Foo {}", "Foo.java"),
    ("public record Foo(int value) {}", "Foo.java"),
        ("package a.b; class Foo {}", "a/b/Foo.java"),
        (
            "/* package fake; */ // class Fake {}\npackage a /* gap */ . b;",
            "a/b/Foo.java",
        ),
        (
            '@Note(text="package wrong;", type=Object.class) package real; ',
            "real/Foo.java",
        ),
        ('class Foo { String x="package wrong;"; }', "Foo.java"),
        ("import a.B; class Foo {}", "Foo.java"),
        (r"\u0070ackage a.\u0062; class Foo {}", "a/b/Foo.java"),
        (r"// comment\u000apackage a;", "a/Foo.java"),
        (r"// comment\\u000apackage fake;", "Foo.java"),
        ("package $a.中文; class Foo {}", "$a/中文/Foo.java"),
        ("package a..b;", None),
        ("package a", None),
        ("package ../../a;", None),
        ("@Note(value={Object.class, String.class}) package a;", "a/Foo.java"),
    ],
)
def test_source_header(text, expected):
    result = source_relative_path(text.encode(), "Foo.java", "UTF-8")
    assert result == (Path(expected) if expected else None)


def test_source_encoding_and_package_info():
    text = "@Deprecated package 中文;"
    assert source_relative_path(text.encode("gbk"), "package-info.java", "GBK") == Path(
        "中文/package-info.java"
    )
