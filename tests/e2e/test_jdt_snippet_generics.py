"""Keep unresolved type-variable diagnostics distinct from concrete generics.

This is a boundary/recovery regression, not a generic-support acceptance test.
The desired T/U behavior remains raw-red in the full frame-context differential.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
import subprocess

import pytest

from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate, PersistentJdtCompileSession, discover_target_system_entries,
)


SOURCE = """\
import java.lang.reflect.*;
import java.util.*;
public class GenericSubject<T extends Number> {
    public int classScope(T value) { return value.intValue(); }
    public <U extends CharSequence & Comparable<U>> int methodScope(U text) {
        return text.length() + text.compareTo(text);
    }
    public int concrete(int count, List<String> names) {
        return count + names.get(0).length();
    }
    static void field(Class<?> type, Object object, String name, Object value) throws Exception {
        try {
            Field field = type.getDeclaredField(name);
            field.setAccessible(true); field.set(object, value);
        } catch (NoSuchFieldException missing) { field(type.getSuperclass(), object, name, value); }
    }
    public static void main(String[] args) throws Exception {
        GenericSubject<Integer> self = new GenericSubject<>();
        if (args.length == 0) {
            System.out.println(self.classScope(7));
            System.out.println(self.methodScope("abc"));
            System.out.println(self.concrete(3, Arrays.asList("hello")));
            return;
        }
        Class<?> type = Class.forName(args[0]);
        Object snippet = type.newInstance();
        field(type, snippet, args[1], self);
        field(type, snippet, args[2] + "count", 3);
        field(type, snippet, args[2] + "names", Arrays.asList("hello"));
        field(type, snippet, args[2] + "number", 7);
        type.getMethod(args[3]).invoke(snippet);
        System.out.print(type.getMethod("getResultValue").invoke(snippet));
    }
}
"""


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("level", [8, 17, 21])
def test_snippet_reports_unresolved_type_variables_without_widening(tmp_path, level):
    if os.environ.get("JOLINK_RUN_MCP_JAVA_E2E") != "1":
        pytest.skip("set JOLINK_RUN_MCP_JAVA_E2E=1")
    home = os.environ.get(f"JOLINK_TEST_JAVA{level}_HOME")
    if not home:
        pytest.skip(f"set JOLINK_TEST_JAVA{level}_HOME")
    java = Path(home)
    suffix = ".exe" if os.name == "nt" else ""
    source = tmp_path / "GenericSubject.java"
    source.write_text(SOURCE, encoding="utf-8")
    native = tmp_path / "native"
    native.mkdir()
    javac = str(java / f"bin/javac{suffix}")
    subprocess.run([javac, "-g", "-d", str(native), str(source)],
                   check=True, capture_output=True, text=True, timeout=30)
    oracle = subprocess.run([str(java / f"bin/java{suffix}"), "-cp", str(native), "GenericSubject"],
                            check=True, capture_output=True, text=True, timeout=20).stdout.splitlines()
    assert oracle == ["7", "3", "8"]

    # T extends Number is one type, not a freely assignable Number local.
    # Keep this native rejection alongside the suspended snippet regression.
    invalid = tmp_path / "Invalid.java"
    invalid.write_text(
        "class Invalid<T extends Number> { void f(T value) { value = 1.5; } }",
        encoding="utf-8",
    )
    rejected = subprocess.run([javac, "-J-Duser.language=en", str(invalid)],
                              capture_output=True, text=True, timeout=30)
    assert rejected.returncode != 0
    assert "double cannot be converted to T" in rejected.stderr, rejected.stderr

    candidate = JdtCandidate.load_product()
    compiler = PersistentJdtCompileSession(
        root=tmp_path / "worker", candidate=candidate,
        worker_java_home=candidate.select_worker_java().home, source_roots=(),
        classpath_entries=(*discover_target_system_entries(java, level), native),
        source_encoding="UTF-8", source_level=level,
    )
    try:
        assert compiler.start().compile_ok
        compiler.accept_baseline()
        for expression, name, signature, variable in (
            ("value.intValue()", "value", "TT;", "T"),
            ("text.length()", "text", "TU;", "U"),
            ("text.compareTo(text)", "text", "TU;", "U"),
            ("values.get(0).intValue()", "values", "Ljava/util/List<TT;>;", "T"),
            ("value = 1.5", "value", "TT;", "T"),
        ):
            result = compiler.compile_snippet(
                expression, "GenericSubject", local_names=(name,), local_types=(signature,),
            )
            assert result["ok"] is False, (expression, result)
            assert result["error_code"] == "EVALUATION_COMPILE_FAILED", result
            assert any(f"{variable} cannot be resolved to a type" in message
                       for message in result["diagnostics"]), result
            assert not result["classes"], result

        # The Worker still executes ordinary locals and concrete List<String>,
        # including legal writes. This is not a blanket generic/write ban.
        request = dict(
            declaring_type="GenericSubject", local_names=("count", "names", "number"),
            local_types=("I", "Ljava/util/List<Ljava/lang/String;>;", "Ljava/lang/Number;"),
        )
        for expression, expected in (
            ("count + names.get(0).length()", oracle[2]),
            ('names.set(0, "world")', "hello"),
            ("(number = 1.5).doubleValue()", "1.5"),
        ):
            result = compiler.compile_snippet(expression, **request)
            assert result["ok"], (expression, result)
            for artifact in result["classes"]:
                target = native / (artifact["name"].replace(".", "/") + ".class")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(base64.b64decode(artifact["bytes_base64"]))
            ran = subprocess.run([
                str(java / f"bin/java{suffix}"), "-cp", str(native), "GenericSubject",
                result["snippet_class"], result["delegate_this"], result["local_var_prefix"], result["run_method"],
            ], check=True, capture_output=True, text=True, timeout=20)
            assert ran.stdout == expected, (expression, ran.stdout, expected)
    finally:
        compiler.close()
