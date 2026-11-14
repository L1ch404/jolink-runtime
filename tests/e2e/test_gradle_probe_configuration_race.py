"""Native Gradle evaluation must not be cached under a later configuration."""

import json
import os
from pathlib import Path
from urllib.request import urlopen

import anyio
import pytest

from java_support import open_mcp_session, require_real_mcp_java_e2e, reserve_local_port


pytestmark = pytest.mark.mcp_java_e2e


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def _fixture(tmp_path, child):
    require_real_mcp_java_e2e()
    gradle = os.environ.get("JOLINK_FAST_TEST_GRADLE")
    home = os.environ.get("JOLINK_TEST_JAVA17_HOME")
    if not gradle or not home:
        pytest.skip("Gradle and JDK17 are required")
    java = Path(home)
    project = tmp_path / "project"
    app = project / "app" if child else project
    gate = tmp_path / "gate"
    gate.mkdir()
    (gate / "armed").touch()
    expected = _write(tmp_path / "expected.txt", "true")
    wrapper = _write(project / ("gradlew.bat" if os.name == "nt" else "gradlew"),
                     f'@call "{gradle}" %*\r\n' if os.name == "nt" else f'#!/bin/sh\nexec "{gradle}" "$@"\n')
    wrapper.chmod(0o755)
    version = os.environ.get("JOLINK_FAST_TEST_GRADLE_VERSION", "8.14")
    _write(project / "gradle/wrapper/gradle-wrapper.properties",
           f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{version}-bin.zip\n")
    _write(project / "settings.gradle", "rootProject.name='probe-race'\n" + ("include 'app'\n" if child else ""))
    if child:
        _write(project / "build.gradle", "// The child owns its compiler configuration.\n")
    junit = Path.home() / ".m2/repository/junit/junit/4.13.2/junit-4.13.2.jar"
    hamcrest = Path.home() / ".m2/repository/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar"
    if not junit.is_file() or not hamcrest.is_file():
        pytest.skip("JUnit fixture dependencies are required")
    config = _write(app / "build.gradle", """plugins { id 'java' }
java { sourceCompatibility='8'; targetCompatibility='8' }
def parameterNames = false
tasks.withType(JavaCompile).configureEach {
 options.encoding='UTF-8'
 if(parameterNames) options.compilerArgs += ['-parameters']
}
dependencies { testImplementation files(JUNIT,HAMCREST) }
def gateDirectory = file(GATE)
def gateTask = tasks.register('fixtureAwaitConfigurationEdit') {
 doLast {
  if(new File(gateDirectory,'armed').isFile()) {
   new File(gateDirectory,'observed').text = tasks.named('compileJava').get().options.compilerArgs.toString()
   long deadline=System.nanoTime()+120000000000L
   while(!new File(gateDirectory,'release').isFile()) {
    if(System.nanoTime()>deadline) throw new GradleException('fixture gate timed out')
    Thread.sleep(25)
   }
  }
 }
}
tasks.named('processResources') { dependsOn(gateTask) }
""".replace("JUNIT", json.dumps(junit.as_posix())).replace("HAMCREST", json.dumps(hamcrest.as_posix()))
        .replace("GATE", json.dumps(gate.as_posix())))
    port = reserve_local_port()
    _write(app / "src/main/java/example/App.java", """package example;
public class App {
 public static void parameter(String expectedName) {}
 public static boolean names() throws Exception {
  return App.class.getMethod("parameter",String.class).getParameters()[0].isNamePresent();
 }
 public static void main(String[] args) throws Exception {
  com.sun.net.httpserver.HttpServer server=com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1",PORT),0);
  server.createContext("/", e -> {try {byte[] value=String.valueOf(names()).getBytes("UTF-8");
   e.sendResponseHeaders(200,value.length);e.getResponseBody().write(value);e.close();
  } catch(Exception failure){throw new RuntimeException(failure);}});server.start();
 }
} """.replace("PORT", str(port)))
    _write(app / "src/main/resources/value.txt", "fixture resource")
    _write(app / "src/test/java/example/AppTest.java", """package example;
public class AppTest {@org.junit.Test public void currentCompilerConfiguration() throws Exception {
 String expected=new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(EXPECTED)),"UTF-8").trim();
 org.junit.Assert.assertEquals(Boolean.parseBoolean(expected),App.names());
}}""".replace("EXPECTED", json.dumps(expected.as_posix())))
    env = {**os.environ, "JAVA_HOME": str(java), "PATH": str(java / "bin") + os.pathsep + os.environ["PATH"],
           "GRADLE_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache"), "JOLINK_LOG_LEVEL": "INFO"}
    if os.name == "nt":
        env["LOCALAPPDATA"] = env["XDG_CACHE_HOME"]
    launch = dict(action="launch", project_path=str(app), main_class="example.App", java_home=str(java),
                  build_java_home=str(java), ready_port=port, jdwp_port=reserve_local_port(), timeout=30)
    test = dict(project_path=str(app), tests=["example.AppTest"], java_home=str(java),
                build_java_home=str(java), timeout=30)
    return project, app, config, gate, port, env, launch, test


def _error(result):
    return result.get("error_code") or (result.get("launch_error") or {}).get("error_code")


@pytest.mark.parametrize("entry,child", [("test", False), ("test", True), ("launch", False), ("launch", True)])
def test_gradle_configuration_edit_during_probe_is_not_published(tmp_path, entry, child):
    project, app, config, gate, port, env, launch, test = _fixture(tmp_path, child)
    observations = {}

    def http():
        with urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            assert response.status == 200
            return response.read().decode("utf-8")

    async def call(session, tool, args):
        result = dict((await session.call_tool(tool, args)).structuredContent)
        with (tmp_path / "calls.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(tool=tool, arguments=args, result=result), ensure_ascii=False) + "\n")
        return result

    async def settled(session, result, kind):
        with anyio.fail_after(180):
            if kind == "test":
                while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                    await anyio.sleep(.1)
                    result = await call(session, "java_fast_test", dict(action="result", test_run_id=result["test_run_id"]))
            else:
                while result.get("launch_phase") not in {"runtime_active", "failed", "cancelled"} and result.get("ok"):
                    await anyio.sleep(.1)
                    result = await call(session, "java_status", dict(action="status", details=True))
        return result

    async def tests(session):
        result = await settled(session, await call(session, "java_fast_test", test), "test")
        assert result.get("passed") and result.get("tests") == 1, result
        return result

    async def start(session):
        result = await settled(session, await call(session, "java_application", launch), "launch")
        assert result.get("startup_state") == "ready", result
        assert http() == "true"
        return result

    async def raced_request(session, tool, args, kind):
        (gate / "observed").unlink(missing_ok=True)
        (gate / "release").unlink(missing_ok=True)
        (gate / "armed").touch()
        initial = await call(session, tool, {**args, "timeout": 0})
        try:
            with anyio.fail_after(90):
                while not (gate / "observed").exists():
                    await anyio.sleep(.05)
            assert "-parameters" not in (gate / "observed").read_text()
            config.write_text(config.read_text().replace("parameterNames = false", "parameterNames = true"))
        finally:
            (gate / "armed").unlink(missing_ok=True)
            (gate / "release").touch()
        return await settled(session, initial, kind)

    async def scenario():
        with (tmp_path / "first-mcp.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                try:
                    tool = "java_fast_test" if entry == "test" else "java_application"
                    rejected = await raced_request(session, tool, test if entry == "test" else launch, entry)
                    observations["raced"] = rejected
                    (tmp_path / "observations.json").write_text(json.dumps(observations, indent=2))
                    assert _error(rejected) == "BUILD_CONFIGURATION_CHANGED", rejected
                    error = rejected if "retryable" in rejected else rejected.get("launch_error", {})
                    assert error.get("retryable") is True, rejected
                    # No poisoned Build World may be published before retrying.
                    assert rejected.get("compiled_source_count", 0) == 0, rejected
                    assert not list((tmp_path / "cache").rglob("build-world.json"))
                    if entry == "test":
                        observations["retry"] = await tests(session)
                        assert (await tests(session))["compiled_source_count"] == 0
                        await start(session)
                    else:
                        observations["retry"] = await start(session)
                        await tests(session)
                        assert (await tests(session))["compiled_source_count"] == 0
                    pid = (await call(session, "java_status", dict(action="status")))["pid"]
                    assert http() == "true"
                    assert (await tests(session))["compiled_source_count"] == 0
                    assert (await call(session, "java_status", dict(action="status")))["pid"] == pid
                    if entry == "launch":
                        # A rejected re-Probe must not overwrite an existing good
                        # model either. Restore true during a restart's false-model
                        # preparation, then explicitly launch again from true.
                        config.write_text(config.read_text().replace("parameterNames = true", "parameterNames = false"))
                        again = await raced_request(session, "java_application", dict(action="restart"), "launch")
                        observations["raced_restart"] = again
                        assert _error(again) == "BUILD_CONFIGURATION_CHANGED", again
                        await start(session)
                        await tests(session)
                finally:
                    (gate / "release").touch()
                    await call(session, "java_application", dict(action="stop"))
        with (tmp_path / "reopen-mcp.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                try:
                    assert (await tests(session))["compiled_source_count"] == 0
                    reopened = await start(session)
                    assert reopened.get("probe_cache_reused") is True
                    assert reopened.get("jdt_bootstrap_reused") is True
                    assert reopened.get("jdt_bootstrap_build_kind") is None
                finally:
                    await call(session, "java_application", dict(action="stop"))
        (tmp_path / "observations.json").write_text(json.dumps(observations, indent=2))

    anyio.run(scenario)
