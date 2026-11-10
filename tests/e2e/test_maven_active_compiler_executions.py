"""Maven's active compile executions, not their IDs, define both JDT scopes."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import uuid
import zipfile
from pathlib import Path

import anyio
import pytest

from java_support import open_mcp_session, require_real_mcp_java_e2e, reserve_local_port
from test_application_java_home import call as application


def _fixture(tmp_path, layout):
    require_real_mcp_java_e2e()
    java = os.environ.get("JOLINK_TEST_JAVA17_HOME")
    maven = os.environ.get("JOLINK_TEST_MAVEN") or shutil.which("mvn")
    if not java or not maven:
        pytest.skip("Maven and JOLINK_TEST_JAVA17_HOME are required")
    env = {**os.environ, "JAVA_HOME": java,
           "PATH": str(Path(java) / "bin") + os.pathsep + os.environ.get("PATH", ""),
           "XDG_CACHE_HOME": str(tmp_path / "cache"), "MAVEN_ARGS": "--offline",
           "JOLINK_LOG_LEVEL": "INFO"}
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")
    group = "io.jolink.execution.p" + uuid.uuid4().hex
    suffix = ".exe" if os.name == "nt" else ""

    def command(argv, name, cwd=tmp_path):
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                                text=True, timeout=120, check=False)
        (tmp_path / (name + ".log")).write_text(result.stdout + result.stderr, encoding="utf-8")
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    configurations = {}
    for scope, root_name, output_name, level, offset in (
        ("main", "MainInput", "MainValue", 8, 2),
        ("test", "ScopeTest", "TestValue", 11, 7),
    ):
        artifact = tmp_path / (scope + "-processor")
        artifact.mkdir()
        source = artifact / "Generator.java"
        source.write_text('''package processor.SCOPE;
import java.io.Writer;
import java.util.Set;
import javax.annotation.processing.*;
import javax.lang.model.SourceVersion;
import javax.lang.model.element.*;
@SupportedAnnotationTypes("*") @SupportedSourceVersion(SourceVersion.RELEASE_8)
@SupportedOptions({"scope", "offset"})
public class Generator extends AbstractProcessor {
 public boolean process(Set<? extends TypeElement> annotations, RoundEnvironment round) {
  if (!"SCOPE".equals(processingEnv.getOptions().get("scope")))
   throw new AssertionError("wrong scope options: " + processingEnv.getOptions());
  for (Element root : round.getRootElements()) {
   if (!root.getSimpleName().contentEquals("ROOT")) continue;
   for (Element field : root.getEnclosedElements()) {
    if (!field.getSimpleName().contentEquals("VALUE")) continue;
    int value = ((Number)((VariableElement)field).getConstantValue()).intValue()
      + Integer.parseInt(processingEnv.getOptions().get("offset"));
    try (Writer out = processingEnv.getFiler().createSourceFile("generated.OUTPUT",root).openWriter()) {
     out.write("package generated; public class OUTPUT {public static int value(){return " + value + ";}}");
    } catch (Exception error) { throw new RuntimeException(error); }
   }
  }
  return false;
 }
}'''.replace("SCOPE", scope).replace("ROOT", root_name).replace("OUTPUT", output_name), encoding="utf-8")
        classes = artifact / "classes"
        classes.mkdir()
        command([str(Path(java) / f"bin/javac{suffix}"), "--release", "8", "-proc:none",
                 "-d", str(classes), str(source)], scope + "-processor-compile")
        jar = artifact / "generator.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            for path in classes.rglob("*.class"):
                archive.write(path, path.relative_to(classes).as_posix())
            archive.writestr("META-INF/services/javax.annotation.processing.Processor",
                             f"processor.{scope}.Generator\n")
        command([maven, "--offline", "-B", "-ntp",
                 "org.apache.maven.plugins:maven-install-plugin:3.1.2:install-file",
                 f"-Dfile={jar}", f"-DgroupId={group}", f"-DartifactId={scope}",
                 "-Dversion=1", "-Dpackaging=jar"], scope + "-processor-install")
        configurations[scope] = f'''<release>{level}</release><encoding>UTF-8</encoding>
<annotationProcessorPaths><path><groupId>{group}</groupId><artifactId>{scope}</artifactId>
<version>1</version></path></annotationProcessorPaths>
<annotationProcessors><annotationProcessor>processor.{scope}.Generator</annotationProcessor></annotationProcessors>
<compilerArgs><arg>-Ascope={scope}</arg><arg>-Aoffset={offset}</arg></compilerArgs>'''
        if layout != "default":
            parameter = "generatedSourcesDirectory" if scope == "main" else "generatedTestSourcesDirectory"
            configurations[scope] += f"<{parameter}>${{project.basedir}}/custom-generated/{scope}</{parameter}>"

    project = tmp_path / "project"
    main = project / "src/main/java/example/MainInput.java"
    main.parent.mkdir(parents=True)
    main.write_text("package example; public class MainInput { public static final int VALUE=10; }", encoding="utf-8")
    (main.parent / "App.java").write_text('''package example;
// 编码必须来自实际生效的execution，而不是已经禁用的配置。
public class App {
 public static int value(){return generated.MainValue.value();}
 public static void main(String[] args) throws Exception {
  try(java.net.ServerSocket server=new java.net.ServerSocket(Integer.parseInt(args[0]))) {
   while(true) {try(java.net.Socket socket=server.accept()) {
    socket.getOutputStream().write(String.valueOf(value()).getBytes("UTF-8"));
   }}
  }
 }
}''', encoding="utf-8")
    test = project / "src/test/java/example/ScopeTest.java"
    test.parent.mkdir(parents=True)
    test.write_text('''package example; public class ScopeTest {
 public static final int VALUE=3;
 @org.junit.Test public void generated() throws Exception {
  org.junit.Assert.assertEquals(MainInput.VALUE+2, App.value());
  org.junit.Assert.assertEquals(VALUE+7, generated.TestValue.value());
  org.junit.Assert.assertEquals("xx", "x".repeat(2));
  org.junit.Assert.assertTrue(App.class.getMethod("main",String[].class).getParameters()[0].isNamePresent());
  for(String name:new String[]{"processor.main.Generator","processor.test.Generator"}) {
   try {Class.forName(name);org.junit.Assert.fail("processor leaked into runtime");}
   catch(ClassNotFoundException expected){}
  }
 }
}''', encoding="utf-8")
    executions = []
    for scope, goal, phase in (("main", "compile", "compile"), ("test", "testCompile", "test-compile")):
        if layout != "default":
            # Unresolvable processor paths and conflicting levels on a disabled
            # execution must never leak into the active scope or be resolved.
            executions.append(f'''<execution><id>default-{goal}</id><phase>none</phase>
<configuration><release>{17 if scope == "main" else 8}</release><encoding>US-ASCII</encoding>
<annotationProcessorPaths><path><groupId>invalid.disabled</groupId><artifactId>missing</artifactId>
<version>1</version></path></annotationProcessorPaths></configuration></execution>''')
        identity = "default-" + goal if layout == "default" else "user-selected-" + scope
        executions.append(f'''<execution><id>{identity}</id><phase>{phase}</phase>
<goals><goal>{goal}</goal></goals><configuration>{configurations[scope]}</configuration></execution>''')
    if layout == "equivalent-duplicate":
        executions.append(f'''<execution><id>another-equivalent-main-pass</id><phase>compile</phase>
<goals><goal>compile</goal></goals><configuration>{configurations["main"]}</configuration></execution>''')
    if layout != "default":
        executions.append('''<execution><id>packaging-only-compile</id><phase>package</phase>
<goals><goal>compile</goal></goals><configuration><release>17</release>
<annotationProcessorPaths><path><groupId>invalid.packaging</groupId><artifactId>missing</artifactId>
<version>1</version></path></annotationProcessorPaths></configuration></execution>''')
    pom = project / "pom.xml"
    pom.write_text(f'''<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>active-executions</artifactId><version>1</version>
<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.1</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.10.1</version>
<configuration><parameters>true</parameters></configuration>
<executions>{''.join(executions)}</executions></plugin>
<plugin><artifactId>maven-surefire-plugin</artifactId><version>3.0.0-M7</version></plugin></plugins></build></project>''', encoding="utf-8")
    native = tmp_path / "native"
    shutil.copytree(project, native)
    command([maven, "--offline", "-B", "-ntp", "-Dtest=ScopeTest", "test"], "native-test", native)
    for relative, major in (("classes/example/App.class", 52), ("test-classes/example/ScopeTest.class", 55)):
        assert int.from_bytes((native / "target" / relative).read_bytes()[6:8], "big") == major
    return project, main, test, pom, env, java


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("layout", ["default", "replacement", "equivalent-duplicate"])
def test_active_compiler_executions_generate_and_run_both_scopes(tmp_path, layout):
    project, main, test, pom, env, java = _fixture(tmp_path, layout)
    transcript = tmp_path / "mcp-calls.jsonl"

    async def run(session):
        arguments = dict(project_path=str(project), tests=["example.ScopeTest"], java_home=java,
                         build_java_home=java, timeout=30)
        result = dict((await session.call_tool("java_fast_test", arguments)).structuredContent)
        with anyio.fail_after(180):
            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                await anyio.sleep(.2)
                result = dict((await session.call_tool("java_fast_test", {
                    "action": "result", "test_run_id": result["test_run_id"],
                })).structuredContent)
        details = dict((await session.call_tool("java_fast_test", {
            "action": "result", "test_run_id": result["test_run_id"],
        })).structuredContent)
        with transcript.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"arguments": arguments, "result": details}, ensure_ascii=False) + "\n")
        assert details.get("passed") is True and details.get("tests") == 1, details
        return result

    def assert_class_versions():
        for filename, major in (("App.class", 52), ("ScopeTest.class", 55)):
            files = [path for path in (tmp_path / "cache").rglob(filename) if path.parent.name == "example"]
            assert files, filename
            assert all(int.from_bytes(path.read_bytes()[6:8], "big") == major for path in files), files
        worlds = list((tmp_path / "cache/jolink-runtime/fast-test").rglob("build-world.json"))
        assert len(worlds) == 1, worlds
        world = json.loads(worlds[0].read_text())["world"]
        module = next(item for item in world["modules"] if item["module_root"] == str(project))
        for scope, compiler in (("main", module), ("test", module["test_compiler"])):
            expected = project / ("target/generated-sources/annotations" if scope == "main"
                                  else "target/generated-test-sources/test-annotations") if layout == "default" \
                else project / "custom-generated" / scope
            assert compiler["generated_source_directory"] == str(expected)
        for path in (tmp_path / "cache/jolink-runtime/project-launch").rglob("build-world.json"):
            plan = json.loads(path.read_text())["jdt_plan"]
            assert plan["generated_source_directory"] == module["generated_source_directory"]

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w+", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                assert (await run(session))["compiled_source_count"] > 0
                assert (await run(session))["compiled_source_count"] == 0
                assert_class_versions()
                main.write_text(main.read_text().replace("VALUE=10", "VALUE=20"), encoding="utf-8")
                assert (await run(session))["compiled_source_count"] > 0
                test.write_text(test.read_text().replace("VALUE=3", "VALUE=4"), encoding="utf-8")
                assert (await run(session))["compiled_source_count"] > 0
                port = reserve_local_port()
                try:
                    launched = await application(session, "java_application", action="launch", project_path=str(project),
                        main_class="example.App", java_home=java, build_java_home=java,
                        app_args=[str(port)], ready_port=port, jdwp_port=reserve_local_port())
                    assert launched.get("startup_state") == "ready", launched
                    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
                        assert connection.recv(100).decode() == "22"
                finally:
                    await application(session, "java_application", action="stop")
        with (tmp_path / "reopen-stderr.log").open("w+", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                assert (await run(session))["compiled_source_count"] == 0
                assert_class_versions()
        assert not list((project / "target").rglob("*.class")), "MCP must not invoke native compiler goals"

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_differing_active_compiler_executions_are_not_silently_last_wins(tmp_path):
    project, _, _, pom, env, java = _fixture(tmp_path, "replacement")
    pom.write_text(pom.read_text().replace("</executions>", '''<execution>
<id>different-main-platform</id><phase>compile</phase><goals><goal>compile</goal></goals>
<configuration><release>17</release><proc>none</proc></configuration>
</execution></executions>'''), encoding="utf-8")

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w+", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                result = dict((await session.call_tool("java_fast_test", dict(
                    project_path=str(project), tests=["example.ScopeTest"], java_home=java,
                    build_java_home=java, timeout=30,
                ))).structuredContent)
                with anyio.fail_after(180):
                    while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                        await anyio.sleep(.2)
                        result = dict((await session.call_tool("java_fast_test", {
                            "action": "result", "test_run_id": result["test_run_id"],
                        })).structuredContent)
                details = dict((await session.call_tool("java_fast_test", {
                    "action": "result", "test_run_id": result["test_run_id"],
                })).structuredContent)
                (tmp_path / "result.json").write_text(json.dumps(details, indent=2), encoding="utf-8")
                assert details["ok"] is False and details["compiled_source_count"] == 0, details
                assert details.get("error_code") == "MAVEN_COMPILER_EXECUTION_AMBIGUOUS", details
                assert details.get("scope") == "main", details
                assert set(details.get("execution_ids", ())) == {
                    "user-selected-main", "different-main-platform",
                }, details
    anyio.run(scenario)
