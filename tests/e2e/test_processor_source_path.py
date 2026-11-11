"""SOURCE_PATH resource reads participate in both persistent compile workflows."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import anyio
import pytest

import test_processor_auxiliary_inputs as auxiliary
from java_support import open_mcp_session, reserve_local_port
from test_generated_lombok_configuration import _command, _maven_arguments
from test_lombok_configuration import _write


pytestmark = pytest.mark.mcp_java_e2e

PROCESSOR = '''package processor;
import java.io.*;
import java.util.Set;
import javax.annotation.processing.*;
import javax.lang.model.SourceVersion;
import javax.lang.model.element.*;
import javax.tools.*;
@SupportedAnnotationTypes("*") @SupportedSourceVersion(SourceVersion.RELEASE_8)
public class Generator extends AbstractProcessor {
 public boolean process(Set<? extends TypeElement> annotations, RoundEnvironment round) {
  for (Element root : round.getRootElements()) {
   String name = root.getSimpleName().toString();
   String generated = name.equals("Input") ? "Aux" : name.equals("TestInput") ? "TestAux" : null;
   if (generated == null) continue;
   try {
    Filer filer = processingEnv.getFiler();
    FileObject input = filer.getResource(StandardLocation.SOURCE_PATH,"example","template.txt");
    String value = input.getCharContent(true).toString().trim();
    try (Reader reader = input.openReader(true)) {
     StringBuilder text = new StringBuilder();
     for (int c; (c=reader.read()) != -1;) text.append((char)c);
     if (!value.equals(text.toString().trim())) throw new IOException("Reader decoding differs");
    }
    try (InputStream stream = input.openInputStream()) {
     if (stream.read() < 0) throw new IOException("Expected nonempty SOURCE_PATH bytes");
    }
    // Existing output-location APIs must still delegate to Eclipse unchanged.
    for (StandardLocation location : new StandardLocation[]{StandardLocation.SOURCE_OUTPUT,StandardLocation.CLASS_OUTPUT}) {
     try { filer.getResource(location,"generated",generated+"-previous.txt").getCharContent(true); }
     catch (IOException absent) { /* No previous output on the first build. */ }
     try (Writer out=filer.createResource(location,"generated",generated+"-previous.txt",root).openWriter()) { out.write("output-control"); }
    }
    try (Writer out = filer.createSourceFile("generated." + generated, root).openWriter()) {
     out.write("package generated; public class " + generated + " { public static String value() {return \\\"" + value + "\\\";} }");
    }
   } catch (Exception error) {
    processingEnv.getMessager().printMessage(Diagnostic.Kind.ERROR,"AUX_INPUT_FAILED " + error,root);
   }
  }
  return false;
 }
}
'''


def _fixture(tmp_path, monkeypatch, system, fallback=False):
    monkeypatch.setattr(auxiliary, "PROCESSOR", PROCESSOR)
    project, env, java, old_main, old_test, expected = auxiliary._fixture(tmp_path, system)
    main = _write(project / "src/main/java/example/template.txt", "alpha\n")
    test = _write(project / "src/test/java/example/template.txt", "test-alpha\n")
    if system == "maven":
        pom = project / "pom.xml"
        text = pom.read_text().replace(f"<arg>-Ainput={old_main.as_posix()}</arg>", "").replace(
            f"<arg>-Ainput={old_test.as_posix()}</arg>", "")
        if fallback:
            main.unlink()
            main = _write(project / "fallback sources/example/template.txt", "alpha\n")
            text = text.replace("</plugins>", '''<plugin>
<groupId>org.codehaus.mojo</groupId><artifactId>build-helper-maven-plugin</artifactId><version>3.5.0</version>
<executions><execution><id>source-resource-fallback</id><phase>generate-sources</phase>
<goals><goal>add-source</goal></goals><configuration><sources><source>${project.basedir}/fallback sources</source></sources></configuration>
</execution></executions></plugin></plugins>''')
        pom.write_text(text)
    else:
        gradle = project / "build.gradle"
        gradle.write_text(gradle.read_text().replace(f"['-Ainput={old_main.as_posix()}']", "[]").replace(
            f"['-Ainput={old_test.as_posix()}']", "[]") + '''
tasks.named('compileJava') { options.sourcepath = files(sourceSets.main.java.srcDirs) }
tasks.named('compileTestJava') { options.sourcepath = files(sourceSets.test.java.srcDirs) }
''')
    return project, env, java, main, test, expected


@pytest.mark.parametrize("system,fallback", [("maven", False), ("gradle", False), ("maven", True)])
def test_source_path_resources_reach_tests_launch_and_restart(tmp_path, monkeypatch, system, fallback):
    project, env, java, main_input, test_input, expected = _fixture(tmp_path, monkeypatch, system, fallback)
    native = tmp_path / "native"
    shutil.copytree(project, native)
    native_args = ([*_maven_arguments(), "-Dtest=AuxTest", "clean", "test"] if system == "maven"
                   else [str(native / ("gradlew.bat" if os.name == "nt" else "gradlew")),
                         "--offline", "--no-daemon", "clean", "test", "--tests", "example.AuxTest"])
    _command(native_args, native, env, tmp_path / "native-alpha.log")
    immutable = {p: p.read_bytes() for p in project.rglob("*")
                 if p.is_file() and (p.suffix == ".java" or p.name in {"pom.xml", "build.gradle"})}
    port = reserve_local_port()

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                client = auxiliary.Calls(session, project, java, tmp_path / "calls.jsonl", system)
                try:
                    assert (await client.tests())["compiled_source_count"] > 0
                    assert (await client.tests())["compiled_source_count"] == 0
                    await client.application(client.launch_args(port))
                    assert auxiliary._http(port) == "alpha"
                    main_input.write_text("bravo\n", encoding="utf-8")
                    expected.write_text("bravo\ntest-alpha", encoding="utf-8")
                    (native / main_input.relative_to(project)).write_text("bravo\n", encoding="utf-8")
                    _command(native_args, native, env, tmp_path / "native-bravo.log")
                    assert (await client.tests())["compiled_source_count"] > 0
                    assert auxiliary._http(port) == "alpha"
                    changed = await client.application(dict(action="restart", timeout=30))
                    assert changed["compiled_source_count"] > 0 and changed["apply_method"] == "hotswap", changed
                    assert auxiliary._http(port) == "bravo"

                    main_outputs = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                                    for p in (tmp_path / "cache").rglob("Aux.class")}
                    assert main_outputs
                    test_input.write_text("test-beta\n", encoding="utf-8")
                    expected.write_text("bravo\ntest-beta", encoding="utf-8")
                    assert (await client.tests())["compiled_source_count"] > 0
                    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in main_outputs} == main_outputs
                    assert auxiliary._http(port) == "bravo"
                    assert (await client.tests())["compiled_source_count"] == 0

                    if fallback:
                        # Earlier root was consulted while absent; creating its
                        # resource must replace the later-root selection.
                        earlier = _write(project / "src/main/java/example/template.txt", "preferred\n")
                        expected.write_text("preferred\ntest-beta", encoding="utf-8")
                        assert (await client.tests())["compiled_source_count"] > 0
                        await client.application(dict(action="restart", timeout=30))
                        assert auxiliary._http(port) == "preferred"
                        earlier.unlink()
                        expected.write_text("bravo\ntest-beta", encoding="utf-8")
                        assert (await client.tests())["compiled_source_count"] > 0
                        await client.application(dict(action="restart", timeout=30))
                        assert auxiliary._http(port) == "bravo"

                    main_input.unlink()
                    await client.tests(passed=False)
                    failed = await client.application(dict(action="restart", timeout=30), ok=False)
                    assert failed.get("error_code") == "JDT_COMPILE_FAILED" and failed.get("applied") is False, failed
                    assert auxiliary._http(port) == "bravo"
                    main_input.write_text("charlie\n", encoding="utf-8")
                    expected.write_text("charlie\ntest-beta", encoding="utf-8")
                    assert (await client.tests())["compiled_source_count"] > 0
                    await client.application(dict(action="restart", timeout=30))
                    assert auxiliary._http(port) == "charlie"
                finally:
                    await client.application(dict(action="stop"))
        for phase in ("unchanged", "changed-offline"):
            if phase == "changed-offline":
                main_input.write_text("delta\n", encoding="utf-8")
                expected.write_text("delta\ntest-beta", encoding="utf-8")
            with (tmp_path / f"{phase}-stderr.log").open("w", encoding="utf-8") as stderr:
                async with open_mcp_session(stderr, environment=env) as session:
                    client = auxiliary.Calls(session, project, java, tmp_path / "calls.jsonl", system)
                    try:
                        result = await client.tests()
                        assert (result["compiled_source_count"] == 0) is (phase == "unchanged"), result
                        started = await client.application(client.launch_args(port))
                        assert started["jdt_bootstrap_reused"] is True, started
                        assert started["jdt_bootstrap_build_kind"] == (None if phase == "unchanged" else "INCREMENTAL"), started
                        assert auxiliary._http(port) == ("charlie" if phase == "unchanged" else "delta")
                    finally:
                        await client.application(dict(action="stop"))
        assert {p: p.read_bytes() for p in immutable} == immutable
        assert not list((project / "target").rglob("*.class"))

    anyio.run(scenario)


def test_missing_source_path_resource_can_be_created_without_java_change(tmp_path, monkeypatch):
    project, env, java, main_input, _, _ = _fixture(tmp_path, monkeypatch, "maven")
    main_input.unlink()
    port = reserve_local_port()

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                client = auxiliary.Calls(session, project, java, tmp_path / "calls.jsonl", "maven")
                try:
                    await client.tests(passed=False, error_code="JDT_TEST_FULL_COMPILE_FAILED")
                    failed = await client.application(client.launch_args(port), ok=False)
                    assert failed.get("error_code") == "JDT_STARTUP_COMPILE_FAILED", failed
                    main_input.write_text("alpha\n", encoding="utf-8")
                    assert (await client.tests())["compiled_source_count"] > 0
                    await client.application(client.launch_args(port))
                    assert auxiliary._http(port) == "alpha"
                    assert (await client.tests())["compiled_source_count"] == 0
                finally:
                    await client.application(dict(action="stop"))

    anyio.run(scenario)


def test_source_path_characters_use_project_encoding(tmp_path, monkeypatch):
    project, env, java, main_input, test_input, expected = _fixture(tmp_path, monkeypatch, "maven")
    pom = project / "pom.xml"
    pom.write_text(pom.read_text().replace("<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>",
                                         "<project.build.sourceEncoding>GBK</project.build.sourceEncoding>"))
    main_input.write_text("中文甲\n", encoding="gbk")
    test_input.write_text("中文测试\n", encoding="gbk")
    expected.write_text("中文甲\n中文测试", encoding="utf-8")
    native = tmp_path / "native"
    shutil.copytree(project, native)
    _command([*_maven_arguments(), "-Dtest=AuxTest", "clean", "test"], native, env, tmp_path / "native-gbk.log")
    port = reserve_local_port()

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                client = auxiliary.Calls(session, project, java, tmp_path / "calls.jsonl", "maven")
                try:
                    assert (await client.tests())["compiled_source_count"] > 0
                    assert (await client.tests())["compiled_source_count"] == 0
                    await client.application(client.launch_args(port))
                    assert auxiliary._http(port) == "中文甲"
                    main_input.write_text("中文乙\n", encoding="gbk")
                    expected.write_text("中文乙\n中文测试", encoding="utf-8")
                    assert (await client.tests())["compiled_source_count"] > 0
                    await client.application(dict(action="restart", timeout=30))
                    assert auxiliary._http(port) == "中文乙"
                finally:
                    await client.application(dict(action="stop"))

    anyio.run(scenario)


def test_gradle_explicit_source_path_overrides_java_roots_and_preserves_empty(tmp_path, monkeypatch):
    project, env, java, wrong_main, wrong_test, expected = _fixture(tmp_path, monkeypatch, "gradle")
    wrong_main.write_text("wrong-main\n")
    wrong_test.write_text("wrong-test\n")
    main = _write(project / "processor-main/example/template.txt", "alpha\n")
    _write(project / "processor-test/example/template.txt", "test-alpha\n")
    build = project / "build.gradle"
    configured = build.read_text().replace("files(sourceSets.main.java.srcDirs)", "files('processor-main')").replace(
        "files(sourceSets.test.java.srcDirs)", "files('processor-test')")
    build.write_text(configured)
    native = tmp_path / "native"
    shutil.copytree(project, native)
    native_args = [str(native / ("gradlew.bat" if os.name == "nt" else "gradlew")),
                   "--offline", "--no-daemon", "clean", "test", "--tests", "example.AuxTest"]
    _command(native_args, native, env, tmp_path / "native-external-roots.log")
    port = reserve_local_port()

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                client = auxiliary.Calls(session, project, java, tmp_path / "calls.jsonl", "gradle")
                try:
                    assert (await client.tests())["compiled_source_count"] > 0
                    await client.application(client.launch_args(port))
                    assert auxiliary._http(port) == "alpha"
                    wrong_main.write_text("still-not-a-processor-input\n")
                    assert (await client.tests())["compiled_source_count"] == 0
                    main.write_text("bravo\n")
                    expected.write_text("bravo\ntest-alpha")
                    assert (await client.tests())["compiled_source_count"] > 0
                    await client.application(dict(action="restart", timeout=30))
                    assert auxiliary._http(port) == "bravo"

                    # Empty is intentional, not a request to fall back to Java roots.
                    empty = configured.replace("files('processor-main')", "files()")
                    build.write_text(empty)
                    (native / "build.gradle").write_text(empty)
                    failed_native = subprocess.run(native_args, cwd=native, env=env, capture_output=True,
                                                   text=True, timeout=120)
                    (tmp_path / "native-empty-roots.log").write_text(failed_native.stdout + failed_native.stderr)
                    assert failed_native.returncode != 0 and "FileNotFoundException" in failed_native.stderr
                    await client.tests(passed=False, error_code="JDT_TEST_FULL_COMPILE_FAILED")
                    failed = await client.application(dict(action="restart", timeout=30), ok=False)
                    assert failed.get("error_code") == "JDT_STARTUP_COMPILE_FAILED", failed
                finally:
                    await client.application(dict(action="stop"))

    anyio.run(scenario)
