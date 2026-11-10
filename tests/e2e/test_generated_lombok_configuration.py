"""APT-generated Lombok sources retain native directory semantics in both products."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path
from urllib.request import urlopen

import anyio
import pytest

from java_support import open_mcp_session, reserve_local_port
from test_application_java_home import call as application_call
from test_lombok_configuration import LOMBOK_VERSION, _environment, _project, _run, _write


pytestmark = pytest.mark.mcp_java_e2e

PROCESSOR = '''package processor;
import java.io.Writer;
import java.util.Set;
import javax.annotation.processing.*;
import javax.lang.model.SourceVersion;
import javax.lang.model.element.*;
@SupportedAnnotationTypes("*") @SupportedSourceVersion(SourceVersion.RELEASE_8)
public class Generator extends AbstractProcessor {
 public boolean process(Set<? extends TypeElement> annotations, RoundEnvironment round) {
  for (Element root : round.getRootElements()) {
   String name = root.getSimpleName().toString();
   String generated = name.equals("Input") ? "Bean" : name.equals("TestInput") ? "TestBean" : null;
   if (generated == null) continue;
   try (Writer out = processingEnv.getFiler().createSourceFile("generated." + generated, root).openWriter()) {
    out.write("package generated; @lombok.Data public class " + generated + " { private String name; }");
   } catch (Exception error) { throw new RuntimeException(error); }
  }
  return false;
 }
}
'''
INPUT = '''package example;
public class Input {
 public static String value() throws Exception {
  generated.Bean bean = new generated.Bean(); bean.setName("before");
  return generated.Bean.class.getMethod("setName", String.class).getReturnType().getName() + "|" + bean.getName();
 }
}
'''
APP = '''package example;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
public class App {
 public static void main(String[] args) throws Exception {
  HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", Integer.parseInt(args[0])), 0);
  server.createContext("/", exchange -> {
   try {
    byte[] value = Input.value().getBytes("UTF-8");
    exchange.sendResponseHeaders(200, value.length); exchange.getResponseBody().write(value); exchange.close();
   } catch (Exception error) { throw new RuntimeException(error); }
  });
  server.start();
 }
}
'''


def _command(argv, cwd, env, log):
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    assert result.returncode == 0, (argv, result.stdout, result.stderr)


def _processor(tmp_path, java, env):
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
    return jar


def _maven_arguments():
    arguments = [shutil.which("mvn"), "--offline", "-B", "-ntp"]
    if os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
        arguments += ["-s", os.environ["JOLINK_TEST_MAVEN_SETTINGS"]]
    return arguments


def _fixture(tmp_path, system, layout):
    env, java, lombok = _environment(tmp_path)
    jar = _processor(tmp_path, java, env)
    project = _project(tmp_path, system, lombok)
    if layout == "custom":
        main_generated, test_generated = project / "generated/main", project / "generated/test"
    elif system == "maven":
        main_generated = project / "target/generated-sources/annotations"
        test_generated = project / "target/generated-test-sources/test-annotations"
    else:
        main_generated = project / "build/generated/sources/annotationProcessor/java/main"
        test_generated = project / "build/generated/sources/annotationProcessor/java/test"
    if system == "maven":
        group = "io.jolink.generated.p" + uuid.uuid4().hex
        _command([*_maven_arguments(), "org.apache.maven.plugins:maven-install-plugin:3.1.2:install-file",
                  f"-Dfile={jar}", f"-DgroupId={group}", "-DartifactId=generator", "-Dversion=1",
                  "-Dpackaging=jar"], tmp_path, env, tmp_path / "processor-install.log")
        pom = project / "pom.xml"
        directories = ('''<generatedSourcesDirectory>${project.basedir}/generated/main</generatedSourcesDirectory>
<generatedTestSourcesDirectory>${project.basedir}/generated/test</generatedTestSourcesDirectory>'''
                       if layout == "custom" else "")
        pom.write_text(pom.read_text().replace(
            "<plugin><artifactId>maven-compiler-plugin</artifactId><version>3.10.1</version></plugin>",
            f'''<plugin><artifactId>maven-compiler-plugin</artifactId><version>3.10.1</version>
<configuration>{directories}
<annotationProcessorPaths>
<path><groupId>{group}</groupId><artifactId>generator</artifactId><version>1</version></path>
<path><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId><version>{LOMBOK_VERSION}</version></path>
</annotationProcessorPaths></configuration></plugin>'''), encoding="utf-8")
    else:
        build = project / "build.gradle"
        directories = ('''tasks.named('compileJava') { options.generatedSourceOutputDirectory = file('generated/main') }
tasks.named('compileTestJava') { options.generatedSourceOutputDirectory = file('generated/test') }'''
                       if layout == "custom" else "")
        build.write_text(build.read_text() + f'''
dependencies {{ annotationProcessor files('{jar.as_posix()}'); testAnnotationProcessor files('{jar.as_posix()}') }}
{directories}
test {{ useJUnit() }}
''', encoding="utf-8")
    _write(project / "lombok.config", "config.stopBubbling=true\nlombok.accessors.chain=false\n")
    # Source origins deliberately disagree with generated-directory configuration.
    _write(project / "src/main/java/lombok.config", "lombok.accessors.chain=false\n")
    _write(project / "src/test/java/lombok.config", "lombok.accessors.chain=true\n")
    _write(main_generated / "lombok.config", "lombok.accessors.chain=true\n")
    main_config = _write(main_generated / "generated/lombok.config", "lombok.accessors.chain=true\n")
    test_config = _write(test_generated / "generated/lombok.config", "lombok.accessors.chain=false\n")
    source = _write(project / "src/main/java/example/Input.java", INPUT)
    _write(project / "src/main/java/example/App.java", APP)
    _write(project / "src/test/java/example/TestInput.java", "package example; public class TestInput {}")
    expected = _write(project / "expected.txt", "generated.Bean|before\nvoid")
    _write(project / "src/test/java/example/GeneratedTest.java", '''package example;
public class GeneratedTest {
 @org.junit.Test public void configuration() throws Exception {
  java.util.List<String> expected = java.nio.file.Files.readAllLines(java.nio.file.Paths.get(EXPECTED), java.nio.charset.StandardCharsets.UTF_8);
  org.junit.Assert.assertEquals(expected.get(0), Input.value());
  org.junit.Assert.assertEquals(expected.get(1), generated.TestBean.class.getMethod("setName", String.class).getReturnType().getName());
 }
}'''.replace("EXPECTED", json.dumps(expected.as_posix())))
    return project, env, java, source, main_config, test_config, expected, (main_generated, test_generated)


def _http(port):
    with urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
        assert response.status == 200
        return response.read().decode("utf-8")


@pytest.mark.parametrize("system,layout", [("maven", "custom"), ("gradle", "custom"),
                                           ("maven", "default")])
def test_generated_config_crosses_test_launch_restart_and_reopen(tmp_path, system, layout):
    project, env, java, source, main_config, test_config, expected, generated_roots = _fixture(tmp_path, system, layout)
    native = tmp_path / "native"
    shutil.copytree(project, native)
    native_command = ([*_maven_arguments(), "-Dtest=GeneratedTest", "test"] if system == "maven"
                      else [str(native / ("gradlew.bat" if os.name == "nt" else "gradlew")),
                            "--offline", "--no-daemon", "test", "--tests", "example.GeneratedTest"])
    _command(native_command, native, env, tmp_path / "native-test.log")
    # Native compilation runs in a separate copy, never pre-generates MCP inputs.
    assert not [p for root in generated_roots for p in root.rglob("*.java")]
    port = reserve_local_port()
    launch = dict(action="launch", project_path=str(project), main_class="example.App",
                  java_home=str(java), build_java_home=str(java), app_args=[str(port)],
                  ready_port=port, jdwp_port=reserve_local_port(), timeout=30)
    if system == "maven" and os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
        launch["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]
    transcript = tmp_path / "calls.jsonl"

    async def application(session, arguments, *, ok=True):
        async def call(tool, args):
            value = dict((await session.call_tool(tool, args)).structuredContent)
            with transcript.open("a", encoding="utf-8") as output:
                output.write(json.dumps(dict(tool=tool, arguments=args, result=value)) + "\n")
            return value
        result = await call("java_application", arguments)
        with anyio.fail_after(180):
            while result.get("ok") and result.get("status") in {
                "project_launch_started", "project_launch_restarted", "restart_started", "reload_started"
            } and result.get("launch_phase") != "runtime_active" and result.get("stage") != "succeeded":
                await anyio.sleep(.5)
                status = await call("java_status", dict(action="status", details=True))
                result = (status.get("last_reload") or result) if result.get("reload_id") else status
        assert result.get("ok") is ok, result
        return result

    async def tests(session):
        return await _run(session, project, java, transcript, selector="example.GeneratedTest")

    async def rejected_tests(session):
        arguments = dict(project_path=str(project), tests=["example.GeneratedTest"],
                         java_home=str(java), build_java_home=str(java), timeout=30)
        if "maven_settings" in launch:
            arguments["maven_settings"] = launch["maven_settings"]
        result = dict((await session.call_tool("java_fast_test", arguments)).structuredContent)
        with anyio.fail_after(180):
            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                await anyio.sleep(.5)
                result = dict((await session.call_tool("java_fast_test", dict(
                    action="result", test_run_id=result["test_run_id"]))).structuredContent)
        result = dict((await session.call_tool("java_fast_test", dict(
            action="result", test_run_id=result["test_run_id"]))).structuredContent)
        with transcript.open("a", encoding="utf-8") as output:
            output.write(json.dumps(dict(tool="java_fast_test", arguments=arguments, result=result)) + "\n")
        assert result.get("ok") is False and result.get("error_code") == "JDT_TEST_COMPILE_FAILED", result
        assert result.get("error_count", 0) > 0 and result.get("diagnostics"), result

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                try:
                    assert (await tests(session))["compiled_source_count"] > 0
                    assert (await tests(session))["compiled_source_count"] == 0
                    await application(session, launch)
                    assert _http(port) == "generated.Bean|before"
                    source.write_text(INPUT.replace('"before"', '"after"'), encoding="utf-8")
                    expected.write_text("generated.Bean|after\nvoid", encoding="utf-8")
                    assert (await tests(session))["compiled_source_count"] > 0
                    assert _http(port) == "generated.Bean|before", "Fast Test must not publish to the running app"
                    changed = await application(session, dict(action="restart", timeout=30))
                    assert changed["apply_method"] == "hotswap", changed
                    assert _http(port) == "generated.Bean|after"

                    # Only generated-source configuration changes; all Java stays identical.
                    java_before = {str(p): p.read_bytes() for p in (project / "src").rglob("*.java")}
                    main_config.write_text("lombok.accessors.chain=false\n", encoding="utf-8")
                    expected.write_text("void|after\nvoid", encoding="utf-8")
                    assert (await tests(session))["compiled_source_count"] > 0
                    assert _http(port) == "generated.Bean|after"
                    changed = await application(session, dict(action="restart", timeout=30))
                    assert changed["apply_method"] == "restart", changed
                    assert changed["compiled_source_count"] > 0
                    assert _http(port) == "void|after"
                    test_config.write_text("lombok.accessors.chain=true\n", encoding="utf-8")
                    expected.write_text("void|after\ngenerated.TestBean", encoding="utf-8")
                    assert (await tests(session))["compiled_source_count"] > 0
                    assert _http(port) == "void|after"
                    assert (await tests(session))["compiled_source_count"] == 0
                    if system == "maven" and layout == "custom":
                        main_config.write_text("lombok.accessors.chain=false\nlombok.accessors.fluent=true\n", encoding="utf-8")
                        await rejected_tests(session)
                        broken = await application(session, dict(action="restart", timeout=30), ok=False)
                        assert broken.get("error_code") == "JDT_COMPILE_FAILED" and broken.get("applied") is False, broken
                        assert _http(port) == "void|after", "Compile failure must not publish problem classes"
                        main_config.write_text("lombok.accessors.chain=false\n", encoding="utf-8")
                        assert (await tests(session))["compiled_source_count"] > 0
                        await application(session, dict(action="restart", timeout=30))
                        assert _http(port) == "void|after"
                    assert {str(p): p.read_bytes() for p in (project / "src").rglob("*.java")} == java_before
                finally:
                    await application(session, dict(action="stop"))
        for phase in ("unchanged", "changed-config"):
            if phase == "changed-config":
                main_config.unlink()  # Parent generated/main config now applies.
                expected.write_text("generated.Bean|after\ngenerated.TestBean", encoding="utf-8")
            with (tmp_path / f"{phase}-stderr.log").open("w", encoding="utf-8") as log:
                async with open_mcp_session(log, environment=env) as session:
                    try:
                        result = await tests(session)
                        assert (result["compiled_source_count"] == 0) is (phase == "unchanged"), result
                        started = await application(session, launch)
                        assert started["jdt_bootstrap_reused"] is True, started
                        assert started["jdt_bootstrap_build_kind"] == (None if phase == "unchanged" else "INCREMENTAL"), started
                        assert _http(port) == ("void|after" if phase == "unchanged" else "generated.Bean|after")
                    finally:
                        await application(session, dict(action="stop"))
        assert not [p for root in generated_roots for p in root.rglob("*.java")], "Outputs belong to private workspaces"
        assert not list((project / "target").rglob("*.class"))

    anyio.run(scenario)


def test_gradle_default_generated_directories_use_project_configuration(tmp_path):
    project, env, java, source, main_config, test_config, expected, generated_roots = _fixture(tmp_path, "gradle", "default")
    # Gradle may clean untracked files inside build/ before its first compile.
    # Keep this oracle's configuration outside outputs, as a real project would.
    main_config.unlink()
    test_config.unlink()
    (generated_roots[0] / "lombok.config").unlink()
    (project / "lombok.config").write_text("config.stopBubbling=true\nlombok.accessors.chain=true\n")
    (project / "src/test/java/lombok.config").write_text("lombok.accessors.chain=false\n")
    expected.write_text("generated.Bean|before\ngenerated.TestBean")
    native = tmp_path / "native"
    shutil.copytree(project, native)
    _command([str(native / ("gradlew.bat" if os.name == "nt" else "gradlew")), "--offline", "--no-daemon",
              "test", "--tests", "example.GeneratedTest"], native, env, tmp_path / "native-test.log")
    port = reserve_local_port()

    async def tests(session):
        return await _run(session, project, java, tmp_path / "calls.jsonl", selector="example.GeneratedTest")

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                try:
                    assert (await tests(session))["compiled_source_count"] > 0
                    assert (await tests(session))["compiled_source_count"] == 0
                    started = await application_call(session, "java_application", action="launch", project_path=str(project),
                        main_class="example.App", java_home=str(java), build_java_home=str(java), app_args=[str(port)],
                        ready_port=port, jdwp_port=reserve_local_port(), timeout=30)
                    assert started.get("ok") and started.get("startup_state") == "ready", started
                    assert _http(port) == "generated.Bean|before"
                    source.write_text(INPUT.replace('"before"', '"after"'))
                    expected.write_text("generated.Bean|after\ngenerated.TestBean")
                    assert (await tests(session))["compiled_source_count"] > 0
                    assert _http(port) == "generated.Bean|before"
                    updated = await application_call(session, "java_application", action="restart", timeout=30)
                    assert updated.get("ok") and updated.get("apply_method") == "hotswap", updated
                    assert _http(port) == "generated.Bean|after"
                finally:
                    await application_call(session, "java_application", action="stop")
        assert not [p for root in generated_roots for p in root.rglob("*.java")]

    anyio.run(scenario)
