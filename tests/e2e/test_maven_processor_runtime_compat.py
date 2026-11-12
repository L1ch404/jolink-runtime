"""Explicit APT paths keep native resolution across old and current Maven runtimes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path

import anyio
import pytest

from java_support import open_mcp_session, require_real_mcp_java_e2e, reserve_local_port
from test_generated_lombok_configuration import APP
from test_lombok_configuration import _write
from test_processor_auxiliary_inputs import Calls, _http


pytestmark = pytest.mark.mcp_java_e2e


def _fixture(tmp_path, version, java_major):
    require_real_mcp_java_e2e()
    java = os.environ.get(f"JOLINK_TEST_JAVA{java_major}_HOME")
    maven_home = os.environ.get(f"JOLINK_TEST_MAVEN{version.replace('.', '')}_HOME")
    if not maven_home and version == "3.9.11" and shutil.which("mvn"):
        maven_home = str(Path(shutil.which("mvn")).resolve().parent.parent)
    if not java or not maven_home:
        pytest.skip(f"JDK{java_major} and Maven{version} homes are required")
    java, maven_home = Path(java), Path(maven_home)
    suffix = ".exe" if os.name == "nt" else ""
    maven = maven_home / "bin" / ("mvn.cmd" if os.name == "nt" else "mvn")
    env = {**os.environ, "JAVA_HOME": str(java),
           "PATH": str(java / "bin") + os.pathsep + os.environ.get("PATH", ""),
           "XDG_CACHE_HOME": str(tmp_path / "cache"), "MAVEN_ARGS": "--offline", "JOLINK_LOG_LEVEL": "INFO"}
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")
    settings = os.environ.get("JOLINK_TEST_MAVEN_SETTINGS")
    base = [str(maven), "--offline", "-B"] + (["-s", settings] if settings else [])

    def command(argv, name, cwd=tmp_path):
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
        (tmp_path / (name + ".log")).write_text(result.stdout + result.stderr, encoding="utf-8")
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    identity = command([str(maven), "-v"], "maven-version").stdout
    assert f"Apache Maven {version}" in identity, identity
    group = "io.jolink.runtimecompat.p" + uuid.uuid4().hex

    def dependency(artifact, *, scope="compile", optional=False):
        return (f"<dependency><groupId>{group}</groupId><artifactId>{artifact}</artifactId><version>1</version>"
                f"<scope>{scope}</scope>" + ("<optional>true</optional>" if optional else "") + "</dependency>")

    def artifact(name, class_name, source, *, dependencies="", classpath=(), processor=False):
        folder = tmp_path / name
        folder.mkdir()
        src = _write(folder / (class_name + ".java"), source)
        classes = folder / "classes"
        classes.mkdir()
        command([str(java / f"bin/javac{suffix}"), "-encoding", "UTF-8", "-source", "8", "-target", "8",
                 "-proc:none", "-cp", os.pathsep.join(map(str, classpath)) or ".",
                 "-d", str(classes), str(src)], name + "-compile")
        jar = folder / (name + "-1.jar")
        with zipfile.ZipFile(jar, "w") as archive:
            for path in classes.rglob("*.class"):
                archive.write(path, path.relative_to(classes).as_posix())
            if processor:
                archive.writestr("META-INF/services/javax.annotation.processing.Processor", f"processor.{name}.Generator\n")
        pom = _write(folder / "pom.xml", f"""<project><modelVersion>4.0.0</modelVersion>
<groupId>{group}</groupId><artifactId>{name}</artifactId><version>1</version>
<dependencies>{dependencies}</dependencies></project>""")
        command([*base, "org.apache.maven.plugins:maven-install-plugin:2.5.2:install-file",
                 f"-Dfile={jar}", f"-DpomFile={pom}"], name + "-install")
        return jar

    helpers = []
    for name, value in (("CompileHelper", 2), ("RuntimeHelper", 3), ("ProvidedOnly", 100),
                        ("TestOnly", 100), ("OptionalOnly", 100), ("ExcludedOnly", 100)):
        helpers.append(artifact(name.lower(), name, f"package helper; public class {name} {{ public static int value(){{return {value};}} }}"))
    helper_dependencies = (dependency("compilehelper") + dependency("runtimehelper", scope="runtime")
                           + dependency("providedonly", scope="provided") + dependency("testonly", scope="test")
                           + dependency("optionalonly", optional=True) + dependency("excludedonly", scope="runtime"))
    configurations = {}
    for scope, input_type, output_type, offset in (("main", "Input", "MainValue", 0), ("test", "TestInput", "TestValue", 100)):
        code = '''package processor.SCOPE;
import java.io.Writer;
import java.util.Set;
import javax.annotation.processing.*;
import javax.lang.model.SourceVersion;
import javax.lang.model.element.*;
import javax.tools.Diagnostic;
@SupportedAnnotationTypes("*") @SupportedSourceVersion(SourceVersion.RELEASE_8)
@SupportedOptions("scope")
public class Generator extends AbstractProcessor {
 public boolean process(Set<? extends TypeElement> annotations, RoundEnvironment round) {
  if (!"SCOPE".equals(processingEnv.getOptions().get("scope"))) throw new AssertionError("Wrong scope options");
  for (String name : new String[]{"ProvidedOnly","TestOnly","OptionalOnly","ExcludedOnly"}) {
   try { Class.forName("helper."+name, false, getClass().getClassLoader()); throw new AssertionError("Unexpected processor dependency: "+name); }
   catch (ClassNotFoundException expected) { }
  }
  for (Element root : round.getRootElements()) {
   if (!root.getSimpleName().contentEquals("INPUT")) continue;
   for (Element field : root.getEnclosedElements()) {
    if (!field.getSimpleName().contentEquals("VALUE")) continue;
    try {
     int input=((Number)((VariableElement)field).getConstantValue()).intValue();
     if(input<0) { processingEnv.getMessager().printMessage(Diagnostic.Kind.ERROR,"PROCESSOR_INPUT_INVALID",root); continue; }
     int value=input+helper.CompileHelper.value()+helper.RuntimeHelper.value()+OFFSET;
     try(Writer writer=processingEnv.getFiler().createSourceFile("generated.OUTPUT",root).openWriter()) {
      writer.write("package generated; public class OUTPUT {public static int value(){return "+value+";}}");
     }
    } catch(Exception error) {throw new RuntimeException(error);}
   }
  }
  return false;
 }
}'''.replace("SCOPE", scope).replace("INPUT", input_type).replace("OUTPUT", output_type).replace("OFFSET", str(offset))
        # Keep the diagnostic token independent of the template's INPUT substitution.
        code = code.replace(f"PROCESSOR_{input_type}_INVALID", "PROCESSOR_INPUT_INVALID")
        artifact(scope, "Generator", code, dependencies=helper_dependencies, classpath=helpers[:2], processor=True)
        configurations[scope] = f"""<annotationProcessorPaths><path><groupId>{group}</groupId>
<artifactId>{scope}</artifactId><version>1</version><exclusions><exclusion><groupId>{group}</groupId>
<artifactId>excludedonly</artifactId></exclusion></exclusions></path></annotationProcessorPaths>
<annotationProcessors><annotationProcessor>processor.{scope}.Generator</annotationProcessor></annotationProcessors>
<compilerArgs><arg>-Ascope={scope}</arg></compilerArgs>"""

    project = tmp_path / "project"
    main = _write(project / "src/main/java/example/Input.java", """package example;
public class Input { public static final int VALUE=10;
public static String value(){return String.valueOf(generated.MainValue.value());} }
""")
    _write(project / "src/main/java/example/App.java", APP)
    test_input = _write(project / "src/test/java/example/TestInput.java",
                        "package example; public class TestInput { public static final int VALUE=20; }")
    _write(project / "src/test/java/example/GeneratedTest.java", """package example;
public class GeneratedTest { @org.junit.Test public void generated() throws Exception {
 org.junit.Assert.assertEquals(String.valueOf(Input.VALUE+5),Input.value());
 org.junit.Assert.assertEquals(TestInput.VALUE+105,generated.TestValue.value());
 for(String name:new String[]{"processor.main.Generator","processor.test.Generator","helper.CompileHelper","helper.RuntimeHelper"}) {
  try{Class.forName(name); org.junit.Assert.fail("Processor dependency leaked into runtime: "+name);}
  catch(ClassNotFoundException expected){}
 }
} }
""")
    pom = _write(project / "pom.xml", f"""<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>processor-runtime-compat</artifactId><version>1</version>
<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version>
<executions><execution><id>default-compile</id><goals><goal>compile</goal></goals>
<configuration>{configurations['main']}</configuration></execution>
<execution><id>default-testCompile</id><goals><goal>testCompile</goal></goals>
<configuration>{configurations['test']}</configuration></execution></executions></plugin>
<plugin><artifactId>maven-resources-plugin</artifactId><version>3.3.0</version></plugin>
<plugin><artifactId>maven-surefire-plugin</artifactId><version>3.0.0-M7</version></plugin></plugins></build></project>""")
    native = tmp_path / "native"
    shutil.copytree(project, native)
    command([*base, "-Dtest=GeneratedTest", "test"], "native-test", native)
    return project, env, java, maven_home, main, test_input, pom


@pytest.mark.parametrize("version,java_major", [("3.3.3", 8), ("3.3.9", 8), ("3.9.11", 17)])
def test_explicit_processor_runtime_dependencies_across_maven_versions(tmp_path, version, java_major):
    project, env, java, maven_home, main, test_input, pom = _fixture(tmp_path, version, java_major)
    original_pom = pom.read_bytes()
    port = reserve_local_port()

    async def tests(client, *, passed=True):
        result = await client.call("java_fast_test", dict(client.common, tests=["example.GeneratedTest"]))
        with anyio.fail_after(180):
            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                await anyio.sleep(.5)
                result = await client.call("java_fast_test", dict(action="result", test_run_id=result["test_run_id"]))
        if not result.get("passed"):
            result = await client.call("java_fast_test", dict(action="result", test_run_id=result["test_run_id"]))
        if passed:
            assert result.get("passed") is True and result.get("tests") == 1, result
        else:
            assert result.get("error_code") == "JDT_TEST_COMPILE_FAILED", result
            assert any("PROCESSOR_INPUT_INVALID" in row["message"] for row in result["diagnostics"]), result
        return result

    def client_for(session):
        client = Calls(session, project, java, tmp_path / "calls.jsonl", "maven")
        client.common["maven_home"] = str(maven_home)
        return client

    def assert_processor_paths():
        files = list((tmp_path / "cache/jolink-runtime/fast-test").rglob("build-world.json"))
        assert len(files) == 1, files
        world = json.loads(files[0].read_text())["world"]
        target = next(module for module in world["modules"] if module["module_root"] == str(project))
        for scope, model in (("main", target), ("test", target["test_compiler"])):
            assert {Path(path).name for path in model["processor_entries"]} == {
                scope + "-1.jar", "compilehelper-1.jar", "runtimehelper-1.jar"}, model

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                client = client_for(session)
                try:
                    assert (await tests(client))["compiled_source_count"] > 0
                    assert (await tests(client))["compiled_source_count"] == 0
                    assert_processor_paths()
                    await client.application(client.launch_args(port))
                    assert _http(port) == "15"
                    main.write_text(main.read_text().replace("VALUE=10", "VALUE=11"))
                    assert (await tests(client))["compiled_source_count"] > 0
                    assert _http(port) == "15"
                    updated = await client.application(dict(action="restart", timeout=30))
                    assert updated["apply_method"] == "hotswap", updated
                    assert _http(port) == "16"
                    test_input.write_text(test_input.read_text().replace("VALUE=20", "VALUE=21"))
                    assert (await tests(client))["compiled_source_count"] > 0
                    assert _http(port) == "16"
                    main.write_text(main.read_text().replace("VALUE=11", "VALUE=-1"))
                    await tests(client, passed=False)
                    failed = await client.application(dict(action="restart", timeout=30), ok=False)
                    assert failed["error_code"] == "JDT_COMPILE_FAILED", failed
                    assert _http(port) == "16"
                    main.write_text(main.read_text().replace("VALUE=-1", "VALUE=12"))
                    await tests(client)
                    await client.application(dict(action="restart", timeout=30))
                    assert _http(port) == "17"
                    assert (await tests(client))["compiled_source_count"] == 0
                finally:
                    await client.application(dict(action="stop"))
        with (tmp_path / "reopen-stderr.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                client = client_for(session)
                try:
                    assert (await tests(client))["compiled_source_count"] == 0
                    started = await client.application(client.launch_args(port))
                    assert started["jdt_bootstrap_reused"] and started["jdt_bootstrap_build_kind"] is None, started
                    assert _http(port) == "17"
                finally:
                    await client.application(dict(action="stop"))
        assert pom.read_bytes() == original_pom
        assert not list((project / "target").rglob("*.class")), "Native compiler only runs in the independent oracle copy"

    anyio.run(scenario)
