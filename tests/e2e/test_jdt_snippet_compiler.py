"""Compile real suspended-context snippets, then execute them in target JDKs."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import subprocess

import pytest

from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate, PersistentJdtCompileSession, discover_target_system_entries,
)
from jolink_runtime.launch.jdt_modules import ModuleCompileSession


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("level", [8, 17])
@pytest.mark.parametrize("package_name", ["example", ""])
def test_snippets_use_java_semantics_without_changing_business_outputs(tmp_path, level, package_name):
    if os.environ.get("JOLINK_RUN_MCP_JAVA_E2E") != "1":
        pytest.skip("set JOLINK_RUN_MCP_JAVA_E2E=1")
    home = os.environ.get(f"JOLINK_TEST_JAVA{level}_HOME")
    if not home:
        pytest.skip(f"set JOLINK_TEST_JAVA{level}_HOME")
    jdk = Path(home)
    sources = tmp_path / "sources"
    package = sources / package_name
    package.mkdir(parents=True)
    declaring_type = (package_name + "." if package_name else "") + "Subject"
    (package / "Subject.java").write_text(("package " + package_name + ";\n" if package_name else "") + '''
import java.util.List;
import static java.lang.Math.max;
public class Subject {
    private int secret = 7;
    private int twice(int value) { return value * 2; }
    private int seven() { return 7; }
    public String decorate(String name) { return name + ":" + secret; }
    public int paused(int count, List<String> names) { return count; }
}
''')
    candidate = JdtCandidate.load_product()
    compiler = PersistentJdtCompileSession(
        root=tmp_path / "worker", candidate=candidate,
        worker_java_home=candidate.select_worker_java().home,
        source_roots=(sources,), classpath_entries=discover_target_system_entries(jdk, level),
        source_encoding="UTF-8", source_level=level,
    )

    def output_tree():
        return {str(p.relative_to(compiler.output_directory)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in compiler.output_directory.rglob("*.class")}

    try:
        full = compiler.start()
        assert full.compile_ok, full.diagnostics
        compiler.accept_baseline()
        before = output_tree()
        request = dict(declaring_type=declaring_type, local_names=("count", "names"),
                       local_types=("I", "Ljava/util/List<Ljava/lang/String;>;"))
        original_literal = (package_name + "." if package_name else "") + "CodeSnippet_1"
        literal = compiler.compile_snippet('"' + original_literal + '"', **request)
        assert literal["ok"], literal
        expression = 'twice(count) + secret + seven() - 7 + names.get(0).length() + Math.max(1, 2)'
        result = compiler.compile_snippet(expression, **request)
        assert result["ok"], result
        assert result["local_var_prefix"].startswith("val$__jolink_")
        assert "local_variables_class" not in result
        assert compiler.compile_snippet("doesNotExist + 1", **request)["ok"] is False
        repeated = compiler.compile_snippet(expression, **request)
        assert repeated["ok"], repeated
        assert repeated["snippet_class"] != result["snippet_class"]
        static = compiler.compile_snippet("count + 1", **request, is_static=True)
        assert static["ok"], static
        invalid_this = compiler.compile_snippet("this.secret", **request, is_static=True)
        assert invalid_this["ok"] is False
        assert output_tree() == before
        anonymous = compiler.compile_snippet(
            "new java.util.function.IntSupplier() { public int getAsInt() { return 21; } }.getAsInt()", **request)
        assert anonymous["ok"], anonymous
        reference_method = compiler.compile_snippet("decorate(names.get(0))", **request)
        assert reference_method["ok"], reference_method
        assert any(item["name"] == "org.eclipse.jdt.internal.eval.target.CodeSnippet" for item in result["classes"])
        runner = tmp_path / "RunSnippet.java"
        runner.write_text('''import java.lang.reflect.*;
import java.util.*;
public class RunSnippet {
 static void field(Class<?> type, Object target, String name, Object value) throws Exception {
  Field field;
  try { field = type.getDeclaredField(name); }
  catch (NoSuchFieldException missing) { field(type.getSuperclass(), target, name, value); return; }
  if (Modifier.isStatic(field.getModifiers())) throw new AssertionError("Shared evaluation input: " + name);
  field.setAccessible(true); field.set(target, value);
 }
 public static void main(String[] args) throws Exception {
  Class<?> type = Class.forName(args[0]); Object snippet = type.newInstance();
  field(type, snippet, args[1], Class.forName(args[4]).newInstance());
  field(type, snippet, args[2] + "count", 3);
  field(type, snippet, args[2] + "names", Arrays.asList("hello"));
  type.getMethod(args[3]).invoke(snippet);
  System.out.print(type.getMethod("getResultValue").invoke(snippet));
 }
}
''')
        suffix = ".exe" if os.name == "nt" else ""
        evaluation = tmp_path / "evaluation"
        evaluation.mkdir()
        subprocess.run([str(jdk / f"bin/javac{suffix}"), "-d", str(evaluation), str(runner)], check=True, capture_output=True)
        for compiled, expected in ((result, "20"), (literal, original_literal), (anonymous, "21"),
                                   (reference_method, "hello:7")):
            for item in compiled["classes"]:
                destination = evaluation / (item["name"].replace(".", "/") + ".class")
                destination.parent.mkdir(parents=True, exist_ok=True)
                code = base64.b64decode(item["bytes_base64"])
                assert int.from_bytes(code[6:8], "big") <= level + 44
                destination.write_bytes(code)
            ran = subprocess.run([str(jdk / f"bin/java{suffix}"), "-cp",
                                  os.pathsep.join((str(evaluation), str(compiler.output_directory))),
                                  "RunSnippet", compiled["snippet_class"], compiled["delegate_this"],
                                  compiled["local_var_prefix"], compiled["run_method"], declaring_type],
                                 check=True, capture_output=True, text=True, timeout=20)
            assert ran.stdout == expected
        assert output_tree() == before
        assert compiler.compile(()).actual_build_kind is None
        binary_only = PersistentJdtCompileSession(
            root=tmp_path / "binary-worker", candidate=candidate,
            worker_java_home=candidate.select_worker_java().home, source_roots=(),
            classpath_entries=(*discover_target_system_entries(jdk, level), compiler.output_directory),
            source_encoding="UTF-8", source_level=level,
        )
        try:
            assert binary_only.start().compile_ok
            binary_only.accept_baseline()
            binary_result = binary_only.compile_snippet(expression, **request)
            assert binary_result["ok"], binary_result
        finally:
            binary_only.close()
    finally:
        compiler.close()


class KnownJdtSnippetDefect(AssertionError):
    """Only the exact diagnosed upstream failure qualifies as an expected failure."""


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("level", [8, 17])
@pytest.mark.parametrize("case", [
    pytest.param("private-reference", marks=pytest.mark.xfail(
        strict=True, raises=KnownJdtSnippetDefect,
        reason="ECJ3.46 CodeStream.generateEmulationForMethod uses declaring class for reference parameter types")),
    pytest.param("static-import", marks=pytest.mark.xfail(
        strict=True, raises=KnownJdtSnippetDefect,
        reason="JDT Core snippet parser resolves unqualified static-import calls against the suspended class")),
])
def test_known_upstream_snippet_semantics(tmp_path, level, case):
    if os.environ.get("JOLINK_RUN_MCP_JAVA_E2E") != "1":
        pytest.skip("set JOLINK_RUN_MCP_JAVA_E2E=1")
    home = os.environ.get(f"JOLINK_TEST_JAVA{level}_HOME")
    if not home:
        pytest.skip(f"set JOLINK_TEST_JAVA{level}_HOME")
    jdk = Path(home)
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "Subject.java").write_text('''import static java.lang.Math.max;
public class Subject {
    private String decorate(String name) { return name + "!"; }
}
''')
    candidate = JdtCandidate.load_product()
    compiler = PersistentJdtCompileSession(
        root=tmp_path / "worker", candidate=candidate,
        worker_java_home=candidate.select_worker_java().home,
        source_roots=(sources,), classpath_entries=discover_target_system_entries(jdk, level),
        source_encoding="UTF-8", source_level=level,
    )
    try:
        assert compiler.start().compile_ok
        compiler.accept_baseline()
        expression = 'decorate("hello")' if case == "private-reference" else "max(1, 2)"
        result = compiler.compile_snippet(expression, "Subject")
        if case == "static-import":
            if result.get("diagnostics") == ["The method max(int, int) is undefined for the type Subject"]:
                raise KnownJdtSnippetDefect(result["diagnostics"][0])
            assert result["ok"], result
            return
        assert result["ok"], result
        evaluation = tmp_path / "evaluation"
        for item in result["classes"]:
            destination = evaluation / (item["name"].replace(".", "/") + ".class")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(base64.b64decode(item["bytes_base64"]))
        runner = tmp_path / "RunSnippet.java"
        runner.write_text('''import java.lang.reflect.*;
public class RunSnippet {
 public static void main(String[] args) throws Exception {
  Class<?> type = Class.forName(args[0]); Object snippet = type.newInstance();
  Field self = type.getDeclaredField(args[1]); self.setAccessible(true);
  self.set(snippet, Class.forName("Subject").newInstance());
  type.getMethod("run").invoke(snippet);
  System.out.print(type.getMethod("getResultValue").invoke(snippet));
 }
}
''')
        suffix = ".exe" if os.name == "nt" else ""
        subprocess.run([str(jdk / f"bin/javac{suffix}"), "-d", str(evaluation), str(runner)], check=True, capture_output=True)
        ran = subprocess.run([str(jdk / f"bin/java{suffix}"), "-cp",
                              os.pathsep.join((str(evaluation), str(compiler.output_directory))),
                              "RunSnippet", result["snippet_class"], result["delegate_this"]],
                             capture_output=True, text=True, timeout=20)
        if "java.lang.NoSuchMethodException: Subject.decorate(Subject)" in ran.stderr:
            raise KnownJdtSnippetDefect(ran.stderr)
        assert ran.returncode == 0, ran.stderr
        assert ran.stdout == "hello!"
    finally:
        compiler.close()


@pytest.mark.mcp_java_e2e
def test_snippet_selects_declaring_module_in_shared_worker(tmp_path):
    if os.environ.get("JOLINK_RUN_MCP_JAVA_E2E") != "1":
        pytest.skip("set JOLINK_RUN_MCP_JAVA_E2E=1")
    home = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not home:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    java = Path(home)
    base, app = tmp_path / "base", tmp_path / "app"
    for root in (base, app):
        (root / "src").mkdir(parents=True)
    (base / "src/Base.java").write_text("public class Base { private int secret = 17; }")
    (app / "src/App.java").write_text("public class App extends Base { private String message = \"ok\"; }")
    candidate = JdtCandidate.load_product()
    modules = tuple(dict(
        module_root=str(root), source_roots=[str(root / "src")],
        output_directory=str(root / "target/classes"),
        test_output_directory=str(root / "target/test-classes"),
        target_java_home=str(java), source_level=8, source_encoding="UTF-8",
        classpath=[] if root == base else [str(base / "target/classes")],
    ) for root in (base, app))
    compiler = ModuleCompileSession(
        root=tmp_path / "worker", candidate=candidate,
        worker_java_home=candidate.select_worker_java().home,
        source_roots=(app / "src",), classpath_entries=discover_target_system_entries(java, 8),
        source_encoding="UTF-8", modules=modules, target_module=app,
    )
    try:
        built = compiler.start()
        assert built.compile_ok, built.diagnostics
        compiler.accept_baseline()
        upstream = compiler.compile_snippet("secret + 3", "Base")
        assert upstream["ok"], upstream
        downstream = compiler.compile_snippet("message.length()", "App")
        assert downstream["ok"], downstream
        missing = compiler.compile_snippet("1", "MissingType")
        assert missing["error_code"] == "EVALUATION_CONTEXT_UNAVAILABLE"
        assert compiler.ready
    finally:
        compiler.close()
