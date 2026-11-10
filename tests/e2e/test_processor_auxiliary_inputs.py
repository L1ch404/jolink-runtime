"""File-valued APT options invalidate both test and application compilation."""

from __future__ import annotations

import json
import os
import shutil
import uuid
import zipfile
from pathlib import Path
from urllib.request import urlopen

import anyio
import pytest

from java_support import open_mcp_session, require_real_mcp_java_e2e, reserve_local_port
from test_generated_lombok_configuration import APP, _command, _maven_arguments
from test_lombok_configuration import _write


pytestmark = pytest.mark.mcp_java_e2e

PROCESSOR = '''package processor;
import java.io.Writer;
import java.nio.file.*;
import java.util.Set;
import javax.annotation.processing.*;
import javax.lang.model.SourceVersion;
import javax.lang.model.element.*;
import javax.tools.Diagnostic;
@SupportedAnnotationTypes("*") @SupportedSourceVersion(SourceVersion.RELEASE_8)
@SupportedOptions({"input", "receipt"})
public class Generator extends AbstractProcessor {
 public boolean process(Set<? extends TypeElement> annotations, RoundEnvironment round) {
  for (Element root : round.getRootElements()) {
   String name = root.getSimpleName().toString();
   String generated = name.equals("Input") ? "Aux" : name.equals("TestInput") ? "TestAux" : null;
   if (generated == null) continue;
   try {
    if (processingEnv.getOptions().get("receipt") != null) {
     Files.write(Paths.get(processingEnv.getOptions().get("receipt")), Long.toString(System.nanoTime()).getBytes("UTF-8"));
    }
    String value = new String(Files.readAllBytes(Paths.get(processingEnv.getOptions().get("input"))), "UTF-8").trim();
    try (Writer out = processingEnv.getFiler().createSourceFile("generated." + generated, root).openWriter()) {
     out.write("package generated; public class " + generated + " { public static String value() {return \\\"" + value + "\\\";} }");
    }
   } catch (Exception error) {
    processingEnv.getMessager().printMessage(Diagnostic.Kind.ERROR, "AUX_INPUT_FAILED " + error, root);
   }
  }
  return false;
 }
}
'''


def _fixture(tmp_path, system):
    require_real_mcp_java_e2e()
    home = os.environ.get("JOLINK_TEST_JAVA17_HOME") or os.environ.get("JAVA_HOME")
    if not home or not shutil.which("mvn"):
        pytest.skip("JDK17 and Maven are required")
    java = Path(home)
    env = {**os.environ, "JAVA_HOME": home,
           "PATH": str(java / "bin") + os.pathsep + os.environ.get("PATH", ""),
           "MAVEN_ARGS": "--offline", "GRADLE_ARGS": "--offline",
           "JOLINK_LOG_LEVEL": "INFO", "XDG_CACHE_HOME": str(tmp_path / "cache")}
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")
    source = _write(tmp_path / "processor-src/Generator.java", PROCESSOR)
    classes = tmp_path / "processor-classes"
    classes.mkdir()
    suffix = ".exe" if os.name == "nt" else ""
    _command([str(java / f"bin/javac{suffix}"), "--release", "8", "-proc:none",
              "-d", str(classes), str(source)], tmp_path, env, tmp_path / "processor-compile.log")
    jar = tmp_path / "generator.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        for path in classes.rglob("*.class"):
            archive.write(path, path.relative_to(classes).as_posix())
        archive.writestr("META-INF/services/javax.annotation.processing.Processor", "processor.Generator\n")
    project = tmp_path / "project"
    project.mkdir()
    # Files outside source roots; spaces also guard against tokenizing -A values.
    main_input = _write(project / "config files/main.txt", "alpha\n")
    test_input = _write(project / "config files/test.txt", "test-alpha\n")
    expected = _write(project / "expected.txt", "alpha\ntest-alpha")
    if system == "maven":
        group = "io.jolink.aux.p" + uuid.uuid4().hex
        _command([*_maven_arguments(), "org.apache.maven.plugins:maven-install-plugin:3.1.2:install-file",
                  f"-Dfile={jar}", f"-DgroupId={group}", "-DartifactId=generator", "-Dversion=1",
                  "-Dpackaging=jar"], tmp_path, env, tmp_path / "processor-install.log")
        _write(project / "pom.xml", f'''<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>processor-inputs</artifactId><version>1</version>
<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><artifactId>maven-resources-plugin</artifactId><version>3.3.0</version></plugin>
<plugin><artifactId>maven-compiler-plugin</artifactId><version>3.10.1</version><configuration>
<annotationProcessorPaths><path><groupId>{group}</groupId><artifactId>generator</artifactId><version>1</version></path></annotationProcessorPaths>
<compilerArgs><arg>-Ainput={main_input.as_posix()}</arg></compilerArgs></configuration>
<executions><execution><id>default-testCompile</id><configuration><compilerArgs combine.self="override">
<arg>-Ainput={test_input.as_posix()}</arg></compilerArgs></configuration></execution></executions></plugin>
<plugin><artifactId>maven-surefire-plugin</artifactId><version>3.0.0-M7</version></plugin>
</plugins></build></project>''')
    else:
        gradle = os.environ.get("JOLINK_FAST_TEST_GRADLE")
        if not gradle:
            pytest.skip("JOLINK_FAST_TEST_GRADLE is required")
        wrapper = _write(project / ("gradlew.bat" if os.name == "nt" else "gradlew"),
                         f'@call "{gradle}" %*\r\n' if os.name == "nt" else f'#!/bin/sh\nexec "{gradle}" "$@"\n')
        wrapper.chmod(0o755)
        version = os.environ.get("JOLINK_FAST_TEST_GRADLE_VERSION", "8.14")
        _write(project / "gradle/wrapper/gradle-wrapper.properties",
               f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{version}-bin.zip\n")
        _write(project / "settings.gradle", "rootProject.name='processor-inputs'\n")
        junit = Path.home() / ".m2/repository/junit/junit/4.13.2/junit-4.13.2.jar"
        hamcrest = Path.home() / ".m2/repository/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar"
        if not junit.is_file() or not hamcrest.is_file():
            pytest.skip("cached JUnit4 dependencies are required")
        _write(project / "build.gradle", f'''plugins {{ id 'java' }}
java {{ sourceCompatibility='8'; targetCompatibility='8' }}
dependencies {{
 annotationProcessor files('{jar.as_posix()}'); testAnnotationProcessor files('{jar.as_posix()}')
 testImplementation files('{junit.as_posix()}', '{hamcrest.as_posix()}')
}}
tasks.named('compileJava') {{ options.compilerArgs = ['-Ainput={main_input.as_posix()}'] }}
tasks.named('compileTestJava') {{ options.compilerArgs = ['-Ainput={test_input.as_posix()}'] }}
test {{ useJUnit() }}
''')
    _write(project / "src/main/java/example/Input.java",
           "package example; public class Input {public static String value(){return generated.Aux.value();}}")
    _write(project / "src/main/java/example/App.java", APP)
    _write(project / "src/test/java/example/TestInput.java", "package example; public class TestInput {}")
    _write(project / "src/test/java/example/AuxTest.java", '''package example;
public class AuxTest { @org.junit.Test public void generatedValues() throws Exception {
 java.util.List<String> expected = java.nio.file.Files.readAllLines(java.nio.file.Paths.get(EXPECTED), java.nio.charset.StandardCharsets.UTF_8);
 org.junit.Assert.assertEquals(expected.get(0), Input.value());
 org.junit.Assert.assertEquals(expected.get(1), generated.TestAux.value());
}}'''.replace("EXPECTED", json.dumps(expected.as_posix())))
    return project, env, java, main_input, test_input, expected


class Calls:
    def __init__(self, session, project, java, log, system):
        self.session, self.project, self.java, self.log = session, project, java, log
        self.common = dict(project_path=str(project), java_home=str(java), build_java_home=str(java), timeout=30)
        if system == "maven" and os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
            self.common["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]

    async def call(self, tool, args):
        result = dict((await self.session.call_tool(tool, args)).structuredContent)
        with self.log.open("a", encoding="utf-8") as output:
            output.write(json.dumps(dict(tool=tool, arguments=args, result=result)) + "\n")
        return result

    async def tests(self, *, passed=True, error_code="JDT_TEST_COMPILE_FAILED"):
        result = await self.call("java_fast_test", dict(self.common, tests=["example.AuxTest"]))
        with anyio.fail_after(180):
            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                await anyio.sleep(.5)
                result = await self.call("java_fast_test", dict(action="result", test_run_id=result["test_run_id"]))
        if passed:
            if not result.get("passed"):
                result = await self.call("java_fast_test", dict(action="result", test_run_id=result["test_run_id"]))
            assert result.get("passed") is True and result.get("tests") == 1, result
        else:
            assert result.get("ok") is False and result.get("error_code") == error_code, result
            assert any("AUX_INPUT_FAILED" in d["message"] for d in result.get("diagnostics", [])), result
        return result

    async def application(self, args, *, ok=True):
        result = await self.call("java_application", args)
        with anyio.fail_after(180):
            while result.get("ok") and result.get("status") in {
                "project_launch_started", "project_launch_restarted", "restart_started", "reload_started"
            } and result.get("launch_phase") != "runtime_active" and result.get("stage") != "succeeded":
                await anyio.sleep(.5)
                status = await self.call("java_status", dict(action="status", details=True))
                result = (status.get("last_reload") or result) if result.get("reload_id") else status
        assert result.get("ok") is ok, result
        return result

    def launch_args(self, port):
        return dict(self.common, action="launch", main_class="example.App", ready_port=port,
                    jdwp_port=reserve_local_port(), app_args=[str(port)])


def _http(port):
    with urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
        assert response.status == 200
        return response.read().decode()


@pytest.mark.parametrize("system", ["maven", "gradle"])
def test_auxiliary_file_changes_reach_test_and_application(tmp_path, system):
    project, env, java, main_input, test_input, expected = _fixture(tmp_path, system)
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
                client = Calls(session, project, java, tmp_path / "calls.jsonl", system)
                try:
                    assert (await client.tests())["compiled_source_count"] > 0
                    assert (await client.tests())["compiled_source_count"] == 0
                    await client.application(client.launch_args(port))
                    assert _http(port) == "alpha"
                    main_input.write_text("bravo\n", encoding="utf-8")  # Same byte length, different contents.
                    expected.write_text("bravo\ntest-alpha", encoding="utf-8")
                    _command(native_args, native, env, tmp_path / "native-bravo.log")
                    assert (await client.tests())["compiled_source_count"] > 0
                    assert _http(port) == "alpha", "Fast Test must not publish to the running application"
                    changed = await client.application(dict(action="restart", timeout=30))
                    assert changed["compiled_source_count"] > 0 and changed["apply_method"] == "hotswap", changed
                    assert _http(port) == "bravo"
                    # Only test scope's input changes. Main class outputs must stay untouched.
                    main_outputs = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (tmp_path / "cache").rglob("Aux.class")}
                    assert main_outputs
                    test_input.write_text("test-beta\n", encoding="utf-8")
                    expected.write_text("bravo\ntest-beta", encoding="utf-8")
                    assert (await client.tests())["compiled_source_count"] > 0
                    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in main_outputs} == main_outputs
                    assert _http(port) == "bravo"
                    assert (await client.tests())["compiled_source_count"] == 0
                    main_input.unlink()
                    await client.tests(passed=False)
                    failed = await client.application(dict(action="restart", timeout=30), ok=False)
                    assert failed.get("error_code") == "JDT_COMPILE_FAILED" and failed.get("applied") is False, failed
                    assert _http(port) == "bravo"
                    main_input.write_text("charlie\n", encoding="utf-8")
                    expected.write_text("charlie\ntest-beta", encoding="utf-8")
                    assert (await client.tests())["compiled_source_count"] > 0
                    await client.application(dict(action="restart", timeout=30))
                    assert _http(port) == "charlie"
                finally:
                    await client.application(dict(action="stop"))
        for phase in ("unchanged", "changed-offline"):
            if phase == "changed-offline":
                main_input.write_text("delta\n", encoding="utf-8")
                expected.write_text("delta\ntest-beta", encoding="utf-8")
            with (tmp_path / f"{phase}-stderr.log").open("w", encoding="utf-8") as stderr:
                async with open_mcp_session(stderr, environment=env) as session:
                    client = Calls(session, project, java, tmp_path / "calls.jsonl", system)
                    try:
                        result = await client.tests()
                        assert (result["compiled_source_count"] == 0) is (phase == "unchanged"), result
                        started = await client.application(client.launch_args(port))
                        assert started["jdt_bootstrap_reused"] is True, started
                        assert started["jdt_bootstrap_build_kind"] == (None if phase == "unchanged" else "INCREMENTAL"), started
                        assert _http(port) == ("charlie" if phase == "unchanged" else "delta")
                    finally:
                        await client.application(dict(action="stop"))
        assert {p: p.read_bytes() for p in immutable} == immutable
        assert not list((project / "target").rglob("*.class"))

    anyio.run(scenario)


def test_initially_missing_processor_input_can_be_created_without_java_edits(tmp_path):
    project, env, java, main_input, _, _ = _fixture(tmp_path, "maven")
    main_input.unlink()
    port = reserve_local_port()

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                client = Calls(session, project, java, tmp_path / "calls.jsonl", "maven")
                try:
                    await client.tests(passed=False, error_code="JDT_TEST_FULL_COMPILE_FAILED")
                    failed = await client.application(client.launch_args(port), ok=False)
                    assert failed.get("error_code") == "JDT_STARTUP_COMPILE_FAILED", failed
                    main_input.write_text("alpha\n", encoding="utf-8")
                    assert (await client.tests())["compiled_source_count"] > 0
                    await client.application(client.launch_args(port))
                    assert _http(port) == "alpha"
                    assert (await client.tests())["compiled_source_count"] == 0
                finally:
                    await client.application(dict(action="stop"))

    anyio.run(scenario)


@pytest.mark.parametrize("system", ["maven", "gradle"])
def test_removing_processor_parameter_refreshes_both_compilers(tmp_path, system):
    project, env, java, main_input, _, _ = _fixture(tmp_path, system)
    receipt = project / "processor-receipt.txt"
    build_file = project / ("pom.xml" if system == "maven" else "build.gradle")
    original = build_file.read_text(encoding="utf-8")
    if system == "maven":
        argument = f"<arg>-Ainput={main_input.as_posix()}</arg>"
        configured = original.replace(argument, argument + f"<arg>-Areceipt={receipt.as_posix()}</arg>")
        without_input = original.replace(argument, "")
    else:
        argument = f"['-Ainput={main_input.as_posix()}']"
        configured = original.replace(argument, f"['-Ainput={main_input.as_posix()}', '-Areceipt={receipt.as_posix()}']")
        without_input = original.replace(argument, "[]")
    assert configured != original and without_input != original
    build_file.write_text(configured, encoding="utf-8")
    port = reserve_local_port()

    def assert_parameter_removed_from_inputs(scope):
        # Workspace paths are stable; an options change rebuilds their state in
        # place. Check the contents, not whether a new directory was allocated.
        indices = [path for path in (tmp_path / "cache").rglob("source-index.json") if scope in path.parts]
        assert indices
        for path in indices:
            inputs = json.loads(path.read_text(encoding="utf-8"))["processor_file_inputs"]
            assert any(str(main_input) in files for files in inputs.values()), inputs
            assert all(str(receipt) not in files for files in inputs.values()), inputs

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                calls = Calls(session, project, java, tmp_path / "calls.jsonl", system)
                try:
                    await calls.tests()
                    await calls.tests()
                    # Output-like options are still passed to the Processor.
                    # Do not require extra compilations: they are not a contract.
                    assert receipt.is_file() and receipt.read_text(encoding="utf-8")
                    await calls.application(calls.launch_args(port))
                    assert _http(port) == "alpha"
                    unchanged_report = (receipt.read_bytes(), receipt.stat().st_mtime_ns)
                    build_file.write_text(original, encoding="utf-8")
                    await calls.tests()
                    assert_parameter_removed_from_inputs("fast-test")
                    assert (receipt.read_bytes(), receipt.stat().st_mtime_ns) == unchanged_report
                    assert (await calls.tests())["compiled_source_count"] == 0

                    # Restart refreshes a changed Build World itself, without
                    # asking the user to issue a separate stop/launch sequence.
                    restarted = await calls.application(dict(action="restart", timeout=30))
                    assert restarted["apply_method"] == "restart" and restarted["startup_state"] == "ready", restarted
                    assert_parameter_removed_from_inputs("jdt-workspaces")
                    assert _http(port) == "alpha"
                    assert (receipt.read_bytes(), receipt.stat().st_mtime_ns) == unchanged_report

                    receipt.write_text("changed-by-user-after-removal", encoding="utf-8")
                    assert (await calls.tests())["compiled_source_count"] == 0
                    restarted = await calls.application(dict(action="restart", timeout=30))
                    assert restarted["compiled_source_count"] == 0, restarted
                    assert receipt.read_text(encoding="utf-8") == "changed-by-user-after-removal"
                    assert _http(port) == "alpha"
                finally:
                    await calls.application(dict(action="stop"))

        with (tmp_path / "reopen-stderr.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                calls = Calls(session, project, java, tmp_path / "calls.jsonl", system)
                try:
                    assert (await calls.tests())["compiled_source_count"] == 0
                    launched = await calls.application(calls.launch_args(port))
                    assert launched["jdt_bootstrap_reused"] is True and launched["jdt_bootstrap_build_kind"] is None, launched
                    assert _http(port) == "alpha"
                    assert receipt.read_text(encoding="utf-8") == "changed-by-user-after-removal"
                    # A genuinely required option is not silently inherited
                    # from the previous configuration when the user removes it.
                    build_file.write_text(without_input, encoding="utf-8")
                    await calls.tests(passed=False, error_code="JDT_TEST_FULL_COMPILE_FAILED")
                    await calls.application(dict(action="stop"))
                    failed = await calls.application(calls.launch_args(port), ok=False)
                    assert failed["error_code"] == "JDT_STARTUP_COMPILE_FAILED", failed
                finally:
                    await calls.application(dict(action="stop"))

    anyio.run(scenario)
