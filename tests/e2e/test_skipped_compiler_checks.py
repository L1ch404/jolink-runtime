"""A javac check can be skipped while the real APT/build/test pipeline stays intact."""

import json
import os
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import anyio
import pytest
from java_support import (
    open_mcp_session,
    require_real_mcp_java_e2e,
    reserve_local_port,
    temporary_stderr,
)
from test_build_configuration_mcp import project_fixture
from test_review_model_cache import response


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("system", ["maven", "gradle"])
def test_checks_skipped_but_processors_and_parameters_work(tmp_path, system):
    require_real_mcp_java_e2e()
    java = os.environ.get("JOLINK_TEST_JAVA17_HOME")
    if not java:
        pytest.skip("set JOLINK_TEST_JAVA17_HOME")
    root, app, config, source, port = project_fixture(tmp_path, system, False)
    env = {
        **os.environ,
        "JAVA_HOME": java,
        "PATH": java + "/bin" + os.pathsep + os.environ["PATH"],
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "MAVEN_ARGS": "--offline",
        "GRADLE_ARGS": "--offline",
    }
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")
    settings = os.environ.get("JOLINK_TEST_MAVEN_SETTINGS")
    mvn = [shutil.which("mvn.cmd" if os.name == "nt" else "mvn"), "--offline", "-B"]
    if settings:
        mvn.extend(["-s", settings])

    def command(args, *, cwd=root):
        return subprocess.run(
            args,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )

    processor = tmp_path / "Generator.java"
    processor.write_text("""package processor;
@javax.annotation.processing.SupportedAnnotationTypes("*")
@javax.annotation.processing.SupportedSourceVersion(javax.lang.model.SourceVersion.RELEASE_8)
@javax.annotation.processing.SupportedOptions("marker")
public class Generator extends javax.annotation.processing.AbstractProcessor {
 public boolean process(java.util.Set<? extends javax.lang.model.element.TypeElement> a, javax.annotation.processing.RoundEnvironment round) {
  if(!"preserved".equals(processingEnv.getOptions().get("marker")))throw new AssertionError("APT option lost");
  for(javax.lang.model.element.Element type:round.getRootElements()) {
   if(!type.getSimpleName().contentEquals("App"))continue;
   for(javax.lang.model.element.Element field:type.getEnclosedElements()) {
    if(!field.getSimpleName().contentEquals("CHECK_VALUE"))continue;
    Object value=((javax.lang.model.element.VariableElement)field).getConstantValue();
    try(java.io.Writer w=processingEnv.getFiler().createSourceFile("generated.Value",type).openWriter()) {
     w.write("package generated; public class Value {public static int get(){return "+value+";}}");
    } catch(Exception e){throw new RuntimeException(e);}
   }
  }
  return false;
 }
}""")
    classes = tmp_path / "processor-classes"
    classes.mkdir()
    compiled = command(
        [
            str(Path(java) / ("bin/javac.exe" if os.name == "nt" else "bin/javac")),
            "-source",
            "8",
            "-target",
            "8",
            "-d",
            str(classes),
            str(processor),
        ]
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    jar = tmp_path / "generator.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        for file in classes.rglob("*.class"):
            archive.write(file, file.relative_to(classes).as_posix())
    group = "io.jolink.checks.p" + uuid.uuid4().hex
    installed = command(
        [
            *mvn,
            "org.apache.maven.plugins:maven-install-plugin:3.1.2:install-file",
            f"-Dfile={jar}",
            f"-DgroupId={group}",
            "-DartifactId=generator",
            "-Dversion=1",
            "-Dpackaging=jar",
        ]
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    source.write_text(
        source.read_text()
        .replace(
            "public class App {",
            "public class App { public static final int CHECK_VALUE=73; public static int identity(int input){return input;}",
        )
        .replace(
            "public static String value() throws Exception {",
            'public static String value() throws Exception { new IllegalStateException("unused"); if(generated.Value.get()!=CHECK_VALUE)throw new AssertionError("stale generated output");',
        )
    )
    test = app / "src/test/java/example/ReadyTest.java"
    test.write_text(
        test.read_text().replace(
            "public void resource() throws Exception {",
            'public void resource() throws Exception { org.junit.Assert.assertTrue(App.class.getMethod("identity",int.class).getParameters()[0].isNamePresent()); org.junit.Assert.assertEquals(App.CHECK_VALUE,generated.Value.get());',
        )
    )
    exports = [
        "--add-exports=jdk.compiler/com.sun.tools.javac." + name + "=ALL-UNNAMED"
        for name in (
            "api",
            "file",
            "main",
            "model",
            "parser",
            "processing",
            "tree",
            "util",
            "code",
            "comp",
        )
    ]
    exports += ["--add-opens=jdk.compiler/com.sun.tools.javac.comp=ALL-UNNAMED"]
    plugin_arg = "-Xplugin:ErrorProne -XepDisableAllChecks -Xep:DeadException:ERROR"
    common = [
        "-XDcompilePolicy=simple",
        "--should-stop=ifError=FLOW",
        "-parameters",
        "-processor",
        "processor.Generator",
        "-Amarker=preserved",
    ]
    if system == "maven":
        xml = ET.parse(config)
        compiler = next(
            p
            for p in xml.getroot().findall("./{*}build/{*}plugins/{*}plugin")
            if p.findtext("{*}artifactId") == "maven-compiler-plugin"
        )
        options = compiler.find("{*}configuration")
        if options is None:
            options = ET.SubElement(compiler, "configuration")
        ET.SubElement(options, "fork").text = "true"
        paths = ET.SubElement(options, "annotationProcessorPaths")
        for g, a, v in (
            ("com.google.errorprone", "error_prone_core", "2.20.0"),
            (group, "generator", "1"),
        ):
            path = ET.SubElement(paths, "path")
            for key, value in (("groupId", g), ("artifactId", a), ("version", v)):
                ET.SubElement(path, key).text = value
        arguments = ET.SubElement(options, "compilerArgs")
        for arg in [*common, plugin_arg, *("-J" + item for item in exports)]:
            ET.SubElement(arguments, "arg").text = arg
        xml.write(config, encoding="utf-8")
        native_command = [*mvn, "compile"]
    else:
        config.write_text(
            config.read_text()
            + f"""
repositories {{ maven {{ url=uri({json.dumps((Path.home() / ".m2/repository").as_posix())}) }} }}
dependencies {{ annotationProcessor 'com.google.errorprone:error_prone_core:2.20.0'; annotationProcessor files({json.dumps(jar.as_posix())})
 testAnnotationProcessor 'com.google.errorprone:error_prone_core:2.20.0'; testAnnotationProcessor files({json.dumps(jar.as_posix())}) }}
tasks.withType(JavaCompile).configureEach {{
 options.fork=true
 options.forkOptions.jvmArgs={json.dumps(exports)}
 options.compilerArgs.addAll({json.dumps(common)})
 options.compilerArgumentProviders.add({{ -> [{json.dumps(plugin_arg)}] }} as org.gradle.process.CommandLineArgumentProvider)
}}
"""
        )
        native_command = [
            str(root / ("gradlew.bat" if os.name == "nt" else "gradlew")),
            "--offline",
            "--no-daemon",
            "compileJava",
        ]
    native = command(native_command)
    assert (
        native.returncode != 0 and "[DeadException]" in native.stdout + native.stderr
    ), native.stdout + native.stderr
    shutil.rmtree(
        root / ("target" if system == "maven" else "build"), ignore_errors=True
    )

    async def scenario():
        for cycle in range(2):
            with temporary_stderr() as log:
                async with open_mcp_session(log, environment=env) as session:

                    async def call(tool, **args):
                        return dict(
                            (await session.call_tool(tool, args)).structuredContent
                        )

                    async def run():
                        args = {
                            "project_path": str(root),
                            "tests": ["example.ReadyTest"],
                        }
                        if settings and system == "maven":
                            args["maven_settings"] = settings
                        result = await call("java_fast_test", **args)
                        with anyio.fail_after(180):
                            while result.get("status") in {
                                "starting",
                                "bootstrapping",
                                "compiling",
                                "running",
                            }:
                                await anyio.sleep(0.2)
                                result = await call(
                                    "java_fast_test",
                                    action="result",
                                    test_run_id=result["test_run_id"],
                                )
                        if not result.get("passed"):
                            result = await call(
                                "java_fast_test",
                                action="result",
                                test_run_id=result["test_run_id"],
                            )
                        assert result.get("passed") is True, json.dumps(result)
                        assert result.get("skipped_compiler_checks") == [
                            "Error Prone"
                        ], result
                        return result

                    try:
                        await run()
                        warm = await run()
                        assert (
                            "bootstrap_ms" not in warm
                            and warm["compiled_source_count"] == 0
                        ), warm
                        if not cycle:
                            source.write_text(
                                source.read_text().replace(
                                    "CHECK_VALUE=73", "CHECK_VALUE=74"
                                )
                            )
                            assert (await run())["compiled_source_count"] >= 1
                            before = {
                                str(p): p.stat().st_mtime_ns
                                for p in (tmp_path / "cache").rglob("*.class")
                            }
                            config.write_text(
                                config.read_text().replace(
                                    "DeadException:ERROR", "DeadException:WARN"
                                )
                            )
                            await run()
                            assert {
                                str(p): p.stat().st_mtime_ns
                                for p in (tmp_path / "cache").rglob("*.class")
                            } == before
                        for _ in range(2):
                            args = {
                                "action": "launch",
                                "project_path": str(root),
                                "main_class": "example.App",
                                "ready_port": port,
                                "jdwp_port": reserve_local_port(),
                            }
                            if settings and system == "maven":
                                args["maven_settings"] = settings
                            launched = await call("java_application", **args)
                            with anyio.fail_after(120):
                                while launched.get("launch_phase") not in {
                                    "runtime_active",
                                    "failed",
                                } and launched.get("ok", True):
                                    await anyio.sleep(0.2)
                                    launched = await call(
                                        "java_status", action="status", details=True
                                    )
                            assert launched.get("startup_state") == "ready", json.dumps(
                                launched
                            )
                            assert any(
                                "Error Prone" in item
                                for item in launched.get("warnings", [])
                            ), launched
                            assert response(port) == "A"
                            await call("java_application", action="stop")
                    finally:
                        await call("java_application", action="stop")
                    if cycle:
                        original = source.read_bytes()
                        config.write_text(
                            config.read_text().replace(
                                "DeadException:WARN",
                                "DeadException:WARN -XepPatchChecks:DeadException -XepPatchLocation:IN_PLACE",
                            )
                        )
                        args = {
                            "project_path": str(root),
                            "tests": ["example.ReadyTest"],
                        }
                        if settings and system == "maven":
                            args["maven_settings"] = settings
                        refused = await call("java_fast_test", **args)
                        with anyio.fail_after(120):
                            while refused.get("status") in {
                                "starting",
                                "bootstrapping",
                                "compiling",
                                "running",
                            }:
                                await anyio.sleep(0.2)
                                refused = await call(
                                    "java_fast_test",
                                    action="result",
                                    test_run_id=refused["test_run_id"],
                                )
                        assert refused.get("error_code") == (
                            "FAST_TEST_COMPILER_ARGUMENT_UNSUPPORTED"
                            if system == "maven"
                            else "GRADLE_COMPILE_CONFIGURATION_UNMODELED"
                        ), refused
                        assert source.read_bytes() == original

    anyio.run(scenario)
