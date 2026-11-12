"""Named-module compile/test/runtime evidence, not classpath-only lookalikes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

import anyio
import pytest

from java_support import open_mcp_session, require_real_mcp_java_e2e, reserve_local_port
from test_lombok_configuration import _write


pytestmark = pytest.mark.mcp_java_e2e

APP = '''package example;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
public class App {
 public static void main(String[] args) throws Exception {
  HttpServer server=HttpServer.create(new InetSocketAddress("127.0.0.1",Integer.parseInt(args[0])),0);
  server.createContext("/", exchange -> {
   try { byte[] body=Service.describe().getBytes("UTF-8"); exchange.sendResponseHeaders(200,body.length);
    exchange.getResponseBody().write(body); exchange.close();
   } catch(Exception failure) {throw new RuntimeException(failure);}
  }); server.start();
 }
}
'''


def _fixture(tmp_path, system, reactor, patch_tests, *, legacy_surefire=False):
    require_real_mcp_java_e2e()
    java_home = os.environ.get("JOLINK_TEST_JAVA17_HOME") or os.environ.get("JAVA_HOME")
    if not java_home or not shutil.which("mvn"):
        pytest.skip("JDK17 and Maven are required")
    java = Path(java_home)
    project = tmp_path / "project"
    app = project / "app" if reactor else project
    env = {**os.environ, "JAVA_HOME": str(java), "MAVEN_ARGS": "--offline", "GRADLE_ARGS": "--offline",
           "PATH": str(java / "bin") + os.pathsep + os.environ.get("PATH", ""),
           "XDG_CACHE_HOME": str(tmp_path / "cache"), "JOLINK_LOG_LEVEL": "INFO"}
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")
    module = "module example.app { requires jdk.httpserver; exports example;"
    if reactor:
        module += " requires example.lib;"
        _write(project / "lib/src/main/java/module-info.java", "module example.lib { exports library; }")
        _write(project / "lib/src/main/java/library/Library.java",
               "package library; public class Library {public static int value(){return 40;}}")
        _write(project / "lib/src/main/java/internal/Hidden.java",
               "package internal; public class Hidden {public static String value(){return \"not-exported\";}}")
    _write(app / "src/main/java/module-info.java", module + " }")
    upstream = 'String.valueOf(library.Library.class.getModule().getName())+":"+library.Library.value()' if reactor else '"none"'
    source = _write(app / "src/main/java/example/Service.java", '''package example;
public class Service {
 static String hidden(){return "v1";}
 public static String upstream(){return UPSTREAM;}
 public static String resource() throws Exception {
  try(java.io.InputStream in=Service.class.getResourceAsStream("message.txt")) {
   if(in==null) throw new AssertionError("module resource unavailable");
   return new String(in.readAllBytes(),java.nio.charset.StandardCharsets.UTF_8).trim();
  }
 }
 public static String describe() throws Exception {
  return String.valueOf(Service.class.getModule().getName())+"|"+upstream()+"|"+hidden()+"|"+resource();
 }
}
'''.replace("UPSTREAM", upstream))
    _write(app / "src/main/java/example/App.java", APP)
    _write(app / "src/main/resources/example/message.txt", "resource-v1\n")
    expected = _write(project / "expected.txt", "v1")
    module_name = '"example.app"' if patch_tests else "null"
    test = _write(app / "src/test/java/example/ModuleTest.java", '''package example;
public class ModuleTest {
 @org.junit.Test public void whiteBoxModuleAndResources() throws Exception {
  String expected=java.nio.file.Files.readString(java.nio.file.Paths.get(EXPECTED)).trim();
  org.junit.Assert.assertEquals(expected,Service.hidden());
  org.junit.Assert.assertEquals(MODULE,Service.class.getModule().getName());
  org.junit.Assert.assertEquals(MODULE,ModuleTest.class.getModule().getName());
  org.junit.Assert.assertEquals(NAMED,Service.class.getModule().isNamed());
  org.junit.Assert.assertEquals(NAMED,ModuleTest.class.getModule().isNamed());
  org.junit.Assert.assertEquals("resource-v1",Service.resource());
  org.junit.Assert.assertEquals(1,1); // test edit anchor
 }
}
'''.replace("EXPECTED", json.dumps(expected.as_posix())).replace("MODULE", module_name)
             .replace("NAMED", str(patch_tests).lower()))
    if system == "maven":
        plugins = '''<build><plugins><plugin><artifactId>maven-resources-plugin</artifactId><version>3.3.0</version></plugin>
<plugin><artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version></plugin>
<plugin><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version></plugin></plugins></build>'''
        if legacy_surefire:
            plugins = plugins.replace('<version>3.2.5</version>', '<version>2.12.4</version>')
            assert "useModulePath" not in plugins
        elif not patch_tests:
            # Surefire's runtime policy is independent of the compiler's native
            # white-box patch-module setup.
            plugins = plugins.replace('<version>3.2.5</version>',
                '<version>3.2.5</version><configuration><useModulePath>false</useModulePath></configuration>')
        props = "<properties><maven.compiler.release>17</maven.compiler.release><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>"
        junit = "<dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency>"
        if reactor:
            _write(project / "pom.xml", '<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>reactor</artifactId><version>1</version><packaging>pom</packaging><modules><module>lib</module><module>app</module></modules>' + props + plugins + '</project>')
            parent = '<project><modelVersion>4.0.0</modelVersion><parent><groupId>example</groupId><artifactId>reactor</artifactId><version>1</version></parent>'
            _write(project / "lib/pom.xml", parent + '<artifactId>lib</artifactId></project>')
            _write(app / "pom.xml", parent + '<artifactId>app</artifactId><dependencies><dependency><groupId>example</groupId><artifactId>lib</artifactId><version>1</version></dependency>' + junit + '</dependencies></project>')
        else:
            _write(project / "pom.xml", '<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>app</artifactId><version>1</version>' + props + '<dependencies>' + junit + '</dependencies>' + plugins + '</project>')
    else:
        gradle = os.environ.get("JOLINK_FAST_TEST_GRADLE")
        if not gradle:
            pytest.skip("JOLINK_FAST_TEST_GRADLE is required")
        wrapper = _write(project / ("gradlew.bat" if os.name == "nt" else "gradlew"),
                         f'@call "{gradle}" %*\r\n' if os.name == "nt" else f'#!/bin/sh\nexec "{gradle}" "$@"\n')
        wrapper.chmod(0o755)
        _write(project / "gradle/wrapper/gradle-wrapper.properties", "distributionUrl=https\\://services.gradle.org/distributions/gradle-" + os.environ.get("JOLINK_FAST_TEST_GRADLE_VERSION", "8.14") + "-bin.zip\n")
        _write(project / "settings.gradle", "rootProject.name='named-app'\n" + ("include 'lib','app'\n" if reactor else ""))
        junit = Path.home() / ".m2/repository/junit/junit/4.13.2/junit-4.13.2.jar"
        hamcrest = Path.home() / ".m2/repository/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar"
        assert junit.is_file() and hamcrest.is_file()
        common = "apply plugin:'java'\njava {sourceCompatibility='17';targetCompatibility='17';modularity.inferModulePath=true}\ntasks.withType(JavaCompile).configureEach {options.encoding='UTF-8'}\n"
        test_config = f"dependencies {{testImplementation files('{junit.as_posix()}','{hamcrest.as_posix()}')}}\ntest {{useJUnit()}}\n"
        if reactor:
            test_config += "dependencies {implementation project(':lib')}\n"
        if patch_tests:
            test_config += '''tasks.named('compileTestJava') {
 options.compilerArgs += ['--module-path',files(sourceSets.main.output.classesDirs,configurations.compileClasspath).asPath,
  '--patch-module','example.app='+sourceSets.main.output.classesDirs.asPath+File.pathSeparator+sourceSets.test.java.srcDirs.join(File.pathSeparator),
  '--add-reads','example.app=ALL-UNNAMED']
}
test {
 jvmArgs '--module-path',files(sourceSets.main.output.classesDirs,configurations.compileClasspath).asPath,
  '--patch-module','example.app='+files(sourceSets.test.output.classesDirs,sourceSets.main.output.resourcesDir,sourceSets.test.output.resourcesDir).asPath,
  '--add-modules','example.app','--add-reads','example.app=ALL-UNNAMED','--add-opens','example.app/example=ALL-UNNAMED'
}
'''
        if reactor:
            _write(project / "build.gradle", "subprojects {\n" + common + "}\nproject(':app') {\n" + test_config + "}\n")
            _write(project / "lib/build.gradle", "")
            _write(app / "build.gradle", "")
        else:
            _write(project / "build.gradle", common + test_config)
    return project, app, source, test, expected, java, env


def _native_command(project, system, *, clean=True):
    if system == "maven":
        args = [shutil.which("mvn"), "--offline", "-B"]
        if os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
            args += ["-s", os.environ["JOLINK_TEST_MAVEN_SETTINGS"]]
        return [*args, *(["clean"] if clean else []), "test"]
    return [str(project / ("gradlew.bat" if os.name == "nt" else "gradlew")),
            "--offline", "--no-daemon", *(["clean"] if clean else []), "test", "jar"]


def _native(argv, cwd, env, log, *, succeeds=True):
    result = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=180)
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    assert (result.returncode == 0) is succeeds, (argv, result.stdout, result.stderr)


def _http(port):
    with urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
        assert response.status == 200
        return response.read().decode("utf-8")


def _native_launch(native, system, reactor, java, env, expected, log):
    app = native / "app" if reactor else native
    outputs = [app / "target/classes"] if system == "maven" else list((app / "build/libs").glob("*.jar"))
    if reactor:
        outputs += [native / "lib/target/classes"] if system == "maven" else list((native / "lib/build/libs").glob("*.jar"))
    port = reserve_local_port()
    command = [str(java / ("bin/java.exe" if os.name == "nt" else "bin/java")), "--module-path",
               os.pathsep.join(map(str, outputs)), "--module", "example.app/example.App", str(port)]
    with log.open("w", encoding="utf-8") as output:
        child = subprocess.Popen(command, cwd=native, env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    assert _http(port) == expected
                    break
                except OSError:
                    if child.poll() is not None or time.monotonic() > deadline:
                        raise
                    time.sleep(.1)
        finally:
            child.terminate()
            try:
                child.wait(5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(5)


@pytest.mark.parametrize("system,reactor,patch_tests", [
    ("maven", False, True), ("maven", True, True),
    ("gradle", False, True), ("gradle", True, True),
    ("gradle", False, False),
    ("maven", False, False),
])
def test_jpms_native_test_and_application_lifecycle(tmp_path, system, reactor, patch_tests):
    _run_jpms_lifecycle(tmp_path, system, reactor, patch_tests)


def _run_jpms_lifecycle(tmp_path, system, reactor, patch_tests, *, legacy_surefire=False):
    project, app, source, test_source, expected_file, java, env = _fixture(
        tmp_path, system, reactor, patch_tests, legacy_surefire=legacy_surefire)
    native = tmp_path / "native"
    shutil.copytree(project, native)
    _native(_native_command(native, system), native, env, tmp_path / "native-test.log")
    upstream = "example.lib:40" if reactor else "none"

    def expected(value):
        return f"example.app|{upstream}|{value}|resource-v1"

    _native_launch(native, system, reactor, java, env, expected("v1"), tmp_path / "native-launch.log")
    port = reserve_local_port()
    launch = dict(action="launch", project_path=str(app), main_class="example.App", java_home=str(java),
                  build_java_home=str(java), ready_port=port, jdwp_port=reserve_local_port(), app_args=[str(port)], timeout=30)
    if system == "maven" and os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
        launch["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]

    async def call(session, tool, args):
        result = dict((await session.call_tool(tool, args)).structuredContent)
        with (tmp_path / "calls.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(dict(tool=tool, arguments=args, result=result)) + "\n")
        return result

    async def tests(session, succeeds=True):
        args = dict(project_path=str(app), tests=["example.ModuleTest"], java_home=str(java), build_java_home=str(java), timeout=30)
        if "maven_settings" in launch:
            args["maven_settings"] = launch["maven_settings"]
        result = await call(session, "java_fast_test", args)
        with anyio.fail_after(180):
            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                await anyio.sleep(.5)
                result = await call(session, "java_fast_test", dict(action="result", test_run_id=result["test_run_id"]))
        if result.get("passed") is not succeeds:
            result = await call(session, "java_fast_test", dict(action="result", test_run_id=result["test_run_id"]))
        assert result.get("passed") is succeeds, result
        if succeeds:
            assert result.get("tests") == result.get("passed_count") == 1, result
        else:
            assert result.get("error_count", 0) > 0, result
        return result

    async def application(session, args, succeeds=True):
        result = await call(session, "java_application", args)
        with anyio.fail_after(180):
            while result.get("ok") and result.get("status") in {
                "project_launch_started", "project_launch_restarted", "restart_started", "reload_started"
            } and result.get("launch_phase") != "runtime_active" and result.get("stage") != "succeeded":
                await anyio.sleep(.5)
                state = await call(session, "java_status", dict(action="status", details=True))
                result = (state.get("last_reload") or result) if result.get("reload_id") else state
        assert result.get("ok") is succeeds, result
        return result

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                try:
                    assert (await tests(session))["compiled_source_count"] > 0
                    assert (await tests(session))["compiled_source_count"] == 0
                    started = await application(session, launch)
                    assert started.get("startup_state") == "ready", started
                    assert _http(port) == expected("v1")
                    source.write_text(source.read_text().replace('"v1"', '"v2"'), encoding="utf-8")
                    expected_file.write_text("v2")
                    assert (await tests(session))["compiled_source_count"] > 0
                    assert _http(port) == expected("v1"), "Fast Test must not publish into the live app"
                    changed = await application(session, dict(action="restart", timeout=30))
                    assert changed.get("apply_method") == "hotswap", changed
                    assert _http(port) == expected("v2")
                    test_source.write_text(test_source.read_text().replace("assertEquals(1,1)", "assertEquals(2,2)"))
                    assert (await tests(session))["compiled_source_count"] > 0
                    assert (await tests(session))["compiled_source_count"] == 0
                    assert _http(port) == expected("v2")
                    original = source.read_text()
                    broken = original.replace('return "v2";', 'return internal.Hidden.value();') if reactor else original.replace('return "v2";', 'return missingValue;')
                    source.write_text(broken)
                    if reactor:
                        native_source = native / source.relative_to(project)
                        native_source.write_text(broken)
                        _native(_native_command(native, system, clean=False), native, env,
                                tmp_path / "native-nonexported.log", succeeds=False)
                    failure = await tests(session, succeeds=False)
                    if reactor:
                        assert any("Hidden" in d.get("message", "") or "internal" in d.get("message", "")
                                   for d in failure.get("diagnostics", [])), failure
                    failure = await application(session, dict(action="restart", timeout=30), succeeds=False)
                    assert failure.get("applied") is False
                    assert _http(port) == expected("v2")
                    source.write_text(original)
                    assert (await tests(session))["passed"]
                    await application(session, dict(action="restart", timeout=30))
                    assert _http(port) == expected("v2")
                finally:
                    await application(session, dict(action="stop"))
        with (tmp_path / "reopen-stderr.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                try:
                    assert (await tests(session))["compiled_source_count"] == 0
                    started = await application(session, launch)
                    assert started.get("jdt_bootstrap_reused") is True, started
                    assert started.get("jdt_bootstrap_build_kind") is None, started
                    assert _http(port) == expected("v2")
                finally:
                    await application(session, dict(action="stop"))

    anyio.run(scenario)


def test_legacy_surefire_default_keeps_classpath_tests(tmp_path):
    # Older Surefire has no useModulePath parameter. No explicit false override
    # is supplied: native plugin capability determines the default runtime mode.
    _run_jpms_lifecycle(tmp_path, "maven", False, False, legacy_surefire=True)


def test_module_descriptor_edit_refreshes_test_and_restarts_runtime(tmp_path):
    project, app, source, test_source, expected_file, java, env = _fixture(tmp_path, "maven", False, True)
    old = 'return String.valueOf(Service.class.getModule().getName())+"|"+upstream()+"|"+hidden()+"|"+resource();'
    new = '''java.lang.Module logging=ModuleLayer.boot().findModule("java.logging").orElse(null);
  return String.valueOf(Service.class.getModule().getName())+"|"+(logging!=null && Service.class.getModule().canRead(logging));'''
    assert old in source.read_text()
    source.write_text(source.read_text().replace(old, new))
    module = app / "src/main/java/module-info.java"
    expected_graph = _write(project / "expected-graph.txt", "example.app|false")
    test_source.write_text(test_source.read_text().replace('org.junit.Assert.assertEquals(1,1);',
        'org.junit.Assert.assertEquals(java.nio.file.Files.readString(java.nio.file.Paths.get('
        + json.dumps(expected_graph.as_posix()) + ')).trim(),Service.describe());'))
    native = tmp_path / "native"
    shutil.copytree(project, native)
    _native(_native_command(native, "maven"), native, env, tmp_path / "native-before.log")
    _native_launch(native, "maven", False, java, env, "example.app|false", tmp_path / "native-before-launch.log")
    unchanged_java = {path: path.read_bytes() for path in (app / "src").rglob("*.java") if path != module}
    port = reserve_local_port()
    launch = dict(action="launch", project_path=str(app), main_class="example.App", java_home=str(java),
                  build_java_home=str(java), ready_port=port, jdwp_port=reserve_local_port(), app_args=[str(port)], timeout=30)
    test_args = dict(project_path=str(app), tests=["example.ModuleTest"], java_home=str(java), build_java_home=str(java), timeout=30)
    if os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
        launch["maven_settings"] = test_args["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]

    async def call(session, tool, args):
        result = dict((await session.call_tool(tool, args)).structuredContent)
        with (tmp_path / "calls.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(dict(tool=tool, arguments=args, result=result)) + "\n")
        return result

    async def application(session, args):
        result = await call(session, "java_application", args)
        with anyio.fail_after(180):
            while result.get("ok") and result.get("status") in {
                "project_launch_started", "project_launch_restarted", "restart_started", "reload_started"
            } and result.get("launch_phase") != "runtime_active" and result.get("stage") != "succeeded":
                await anyio.sleep(.5)
                status = await call(session, "java_status", dict(action="status", details=True))
                result = (status.get("last_reload") or result) if result.get("reload_id") else status
        assert result.get("ok"), result
        return result

    async def tests(session):
        result = await call(session, "java_fast_test", test_args)
        with anyio.fail_after(180):
            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                await anyio.sleep(.5)
                result = await call(session, "java_fast_test", dict(action="result", test_run_id=result["test_run_id"]))
        if not result.get("passed"):
            result = await call(session, "java_fast_test", dict(action="result", test_run_id=result["test_run_id"]))
        assert result.get("passed") and result.get("tests") == 1, result
        return result

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                try:
                    await tests(session)
                    await application(session, launch)
                    assert _http(port) == "example.app|false"
                    module.write_text(module.read_text().replace(" }", " requires java.logging; }"))
                    expected_graph.write_text("example.app|true")
                    (native / module.relative_to(project)).write_bytes(module.read_bytes())
                    _native(_native_command(native, "maven", clean=False), native, env, tmp_path / "native-after.log")
                    _native_launch(native, "maven", False, java, env, "example.app|true", tmp_path / "native-after-launch.log")
                    updated = await tests(session)
                    assert updated["compiled_source_count"] > 0, updated
                    assert _http(port) == "example.app|false", "Test compile must not change the running module graph"
                    restarted = await application(session, dict(action="restart", timeout=30))
                    assert restarted.get("apply_method") == "restart", restarted
                    assert _http(port) == "example.app|true"
                    # Native JavaBuilder may require FULL on module descriptor
                    # changes. Record its actual kind, do not call it incremental.
                    (tmp_path / "descriptor-update.json").write_text(json.dumps(
                        dict(test=updated, application=restarted), indent=2))
                    assert (await tests(session))["compiled_source_count"] == 0
                    assert {path: path.read_bytes() for path in unchanged_java} == unchanged_java
                finally:
                    await application(session, dict(action="stop"))
        with (tmp_path / "reopen-stderr.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                try:
                    assert (await tests(session))["compiled_source_count"] == 0
                    await application(session, launch)
                    assert _http(port) == "example.app|true"
                finally:
                    await application(session, dict(action="stop"))
    anyio.run(scenario)


def test_method_only_edit_after_reopen_records_actual_builder_kind(tmp_path):
    project, app, source, _, expected_file, java, env = _fixture(tmp_path, "maven", False, True)
    before_build_files = {path: path.read_bytes() for path in (project / "pom.xml", app / "src/main/java/module-info.java")}
    port = reserve_local_port()
    launch = dict(action="launch", project_path=str(app), main_class="example.App", java_home=str(java),
                  build_java_home=str(java), ready_port=port, jdwp_port=reserve_local_port(), app_args=[str(port)], timeout=30)
    test_args = dict(project_path=str(app), tests=["example.ModuleTest"], java_home=str(java), build_java_home=str(java), timeout=30)
    if os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
        launch["maven_settings"] = test_args["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]
    observations = {}

    async def call(session, tool, args):
        result = dict((await session.call_tool(tool, args)).structuredContent)
        with (tmp_path / "calls.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(dict(tool=tool, arguments=args, result=result)) + "\n")
        return result

    async def tests(session):
        result = await call(session, "java_fast_test", test_args)
        with anyio.fail_after(180):
            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                await anyio.sleep(.5)
                result = await call(session, "java_fast_test", dict(action="result", test_run_id=result["test_run_id"]))
        if not result.get("passed"):
            result = await call(session, "java_fast_test", dict(action="result", test_run_id=result["test_run_id"]))
        assert result.get("passed") and result.get("tests") == 1, result
        return result

    async def application(session, args):
        result = await call(session, "java_application", args)
        with anyio.fail_after(180):
            while result.get("ok") and result.get("status") in {
                "project_launch_started", "project_launch_restarted", "restart_started", "reload_started"
            } and result.get("launch_phase") != "runtime_active" and result.get("stage") != "succeeded":
                await anyio.sleep(.5)
                status = await call(session, "java_status", dict(action="status", details=True))
                result = (status.get("last_reload") or result) if result.get("reload_id") else status
        assert result.get("ok"), result
        return result

    async def scenario():
        with (tmp_path / "initial-stderr.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                try:
                    observations["initial_test"] = await tests(session)
                    observations["initial_application"] = await application(session, launch)
                    assert _http(port) == "example.app|none|v1|resource-v1"
                finally:
                    await application(session, dict(action="stop"))
        with (tmp_path / "reopen-stderr.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                try:
                    observations["reopen_test"] = await tests(session)
                    assert observations["reopen_test"]["compiled_source_count"] == 0
                    observations["reopen_application"] = await application(session, launch)
                    assert observations["reopen_application"].get("jdt_bootstrap_reused") is True
                    assert observations["reopen_application"].get("jdt_bootstrap_build_kind") is None
                    source.write_text(source.read_text().replace('"v1"', '"v2"'))
                    expected_file.write_text("v2")
                    assert {path: path.read_bytes() for path in before_build_files} == before_build_files
                    observations["method_edit_test"] = await tests(session)
                    assert observations["method_edit_test"]["compiled_source_count"] > 0
                    assert "bootstrap_ms" not in observations["method_edit_test"]
                    assert _http(port) == "example.app|none|v1|resource-v1"
                    observations["method_edit_application"] = await application(session, dict(action="restart", timeout=30))
                    assert _http(port) == "example.app|none|v2|resource-v1"
                    observations["next_test"] = await tests(session)
                    assert observations["next_test"]["compiled_source_count"] == 0
                finally:
                    await application(session, dict(action="stop"))
        # Correctness remains strict. Actual FULL vs INCREMENTAL is evidence,
        # not a frozen assertion that would bless an unnecessary FULL rebuild.
        observations["build_finished_events"] = [line for name in ("initial-stderr.log", "reopen-stderr.log")
            for line in (tmp_path / name).read_text().splitlines() if "jdt.build.finished" in line]
        (tmp_path / "post-reopen-method-edit.json").write_text(json.dumps(observations, indent=2))

    anyio.run(scenario)
