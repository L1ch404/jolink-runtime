"""A real Maven model must not be cached under configuration edited mid-Probe."""

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


def _fixture(tmp_path, multi):
    require_real_mcp_java_e2e()
    home = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not home:
        pytest.skip("JOLINK_TEST_JAVA8_HOME is required")
    java = Path(home)
    root = tmp_path / "project"
    app = root / "app" if multi else root
    gate = root / "gate"
    gate.mkdir(parents=True)
    expected = _write(root / "expected.txt", "false")
    properties = """<properties><maven.compiler.source>8</maven.compiler.source>
<maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
<maven.compiler.parameters>false</maven.compiler.parameters></properties>"""
    plugins = """<plugin><artifactId>maven-resources-plugin</artifactId><version>3.3.0</version></plugin>
<plugin><artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version>
<configuration><parameters>${maven.compiler.parameters}</parameters></configuration></plugin>
<plugin><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version></plugin>"""
    # This goal executes inside the actual Maven Probe session, after Maven has
    # parsed the POM. Its marker records that session's old effective property.
    barrier = f"""<plugin><artifactId>maven-antrun-plugin</artifactId><version>3.1.0</version>
<executions><execution><id>configuration-window</id><phase>process-sources</phase><goals><goal>run</goal></goals>
<configuration><target>
<echo file="{gate.as_posix()}/entered">${{maven.compiler.parameters}}</echo>
<waitfor maxwait="60" maxwaitunit="second" checkevery="25" checkeveryunit="millisecond" timeoutproperty="gate.timedout">
<available file="{gate.as_posix()}/release"/></waitfor><fail if="gate.timedout">configuration test gate timed out</fail>
</target></configuration></execution></executions></plugin>"""
    junit = "<dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency>"
    if multi:
        config = _write(root / "pom.xml", "<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>race-root</artifactId><version>1</version><packaging>pom</packaging><modules><module>lib</module><module>app</module></modules>" + properties + "<build><plugins>" + plugins + "</plugins></build></project>")
        parent = "<parent><groupId>example</groupId><artifactId>race-root</artifactId><version>1</version></parent>"
        _write(root / "lib/pom.xml", "<project><modelVersion>4.0.0</modelVersion>" + parent + "<artifactId>lib</artifactId></project>")
        _write(root / "lib/src/main/java/example/Library.java", "package example; public class Library {public static void libraryArgument(String actualName){}}")
        _write(app / "pom.xml", "<project><modelVersion>4.0.0</modelVersion>" + parent + "<artifactId>app</artifactId><dependencies><dependency><groupId>example</groupId><artifactId>lib</artifactId><version>1</version></dependency>" + junit + "</dependencies><build><plugins>" + barrier + "</plugins></build></project>")
    else:
        config = _write(root / "pom.xml", "<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>race-app</artifactId><version>1</version>" + properties + "<dependencies>" + junit + "</dependencies><build><plugins>" + plugins + barrier + "</plugins></build></project>")
    proof = (' && Library.class.getMethod("libraryArgument",String.class).getParameters()[0].isNamePresent()'
             if multi else "")
    source = _write(app / "src/main/java/example/App.java", """package example;
public class App {
 public static void argument(String actualName) {}
 public static String value() throws Exception {return String.valueOf(App.class.getMethod("argument",String.class).getParameters()[0].isNamePresent()PROOF);}
 public static void main(String[] args) throws Exception {
  com.sun.net.httpserver.HttpServer server=com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1",Integer.parseInt(args[0])),0);
  server.createContext("/", e->{try {byte[] b=value().getBytes("UTF-8");e.sendResponseHeaders(200,b.length);e.getResponseBody().write(b);e.close();}catch(Exception x){throw new RuntimeException(x);}});
  server.start();
 }
}
""".replace("PROOF", proof))
    _write(app / "src/test/java/example/ConfigTest.java", """package example;
public class ConfigTest { @org.junit.Test public void usesCurrentParameters() throws Exception {
 String expected=new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(EXPECTED)),"UTF-8").trim();
 org.junit.Assert.assertEquals(expected,App.value());
}}
""".replace("EXPECTED", json.dumps(expected.as_posix())))
    env = {**os.environ, "JAVA_HOME": str(java), "PATH": str(java / "bin") + os.pathsep + os.environ["PATH"],
           "MAVEN_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache"), "JOLINK_LOG_LEVEL": "INFO"}
    if os.name == "nt":
        env["LOCALAPPDATA"] = env["XDG_CACHE_HOME"]
    return root, app, config, gate, expected, source, java, env


@pytest.mark.parametrize("multi", [False, True], ids=["single", "reactor-parent"])
@pytest.mark.parametrize("entry", ["test", "launch"])
def test_configuration_changed_inside_native_probe_is_not_published(tmp_path, multi, entry):
    root, app, config, gate, expected, source, java, env = _fixture(tmp_path, multi)
    original_source = source.read_bytes()
    port = reserve_local_port()
    common = dict(project_path=str(app), java_home=str(java), build_java_home=str(java), timeout=0)
    if os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
        common["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]
    test_args = dict(common, tests=["example.ConfigTest"])
    launch_args = dict(common, action="launch", main_class="example.App", ready_port=port,
                       jdwp_port=reserve_local_port(), app_args=[str(port)])

    def http():
        with urlopen(f"http://127.0.0.1:{port}/", timeout=5) as reply:
            assert reply.status == 200
            return reply.read().decode("utf-8")

    def configure(value):
        text = config.read_text()
        old = "true" if value == "false" else "false"
        config.write_text(text.replace(f"<maven.compiler.parameters>{old}</maven.compiler.parameters>",
                                      f"<maven.compiler.parameters>{value}</maven.compiler.parameters>"))
        expected.write_text(value)

    async def call(session, tool, args):
        result = dict((await session.call_tool(tool, args)).structuredContent)
        with (tmp_path / "calls.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(dict(tool=tool, arguments=args, result=result)) + "\n")
        return result

    async def finish(session, result, kind):
        with anyio.fail_after(150):
            if kind == "test":
                while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                    await anyio.sleep(.1)
                    result = await call(session, "java_fast_test", dict(action="result", test_run_id=result["test_run_id"]))
            else:
                while result.get("ok") and result.get("launch_phase") not in {"runtime_active", "failed", "cancelled"}:
                    await anyio.sleep(.1)
                    result = await call(session, "java_status", dict(action="status", details=True))
        return result

    async def run(session, kind):
        return await call(session, "java_fast_test" if kind == "test" else "java_application",
                          test_args if kind == "test" else launch_args if kind == "launch" else dict(action="restart", timeout=0))

    async def race(session, kind):
        gate.joinpath("entered").unlink(missing_ok=True)
        gate.joinpath("release").unlink(missing_ok=True)
        configure("false")
        initial = await run(session, kind)
        try:
            with anyio.fail_after(90):
                while not (gate / "entered").is_file():
                    await anyio.sleep(.05)
            assert (gate / "entered").read_text().strip() == "false", "Maven must have evaluated the old model before the edit"
            configure("true")
        finally:
            (gate / "release").write_text("go")
        result = await finish(session, initial, "test" if kind == "test" else "launch")
        error = result.get("launch_error", result)
        assert error.get("error_code") == "BUILD_CONFIGURATION_CHANGED", result
        assert error.get("retryable") is True, result
        assert source.read_bytes() == original_source
        if kind == "test":
            assert result.get("passed") is not True, result
        else:
            assert result.get("launch_phase") != "runtime_active", result
        # Repeating the same user request must use the new POM, not a stale model
        # stamped with that POM's hash. No cache deletion or source edit is needed.
        retried = await finish(session, await run(session, kind), "test" if kind == "test" else "launch")
        if kind == "test":
            assert retried.get("passed") and retried.get("tests") == 1, retried
        else:
            assert retried.get("startup_state") == "ready", retried
            assert http() == "true"

    async def scenario():
        with (tmp_path / "mcp.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                try:
                    await race(session, entry)
                    if entry == "test":
                        launched = await finish(session, await run(session, "launch"), "launch")
                        assert launched.get("startup_state") == "ready", launched
                    assert http() == "true"
                    await race(session, "restart")
                    tested = await finish(session, await run(session, "test"), "test")
                    assert tested.get("passed"), tested
                    cached = await finish(session, await run(session, "test"), "test")
                    assert cached.get("compiled_source_count") == 0 and "bootstrap_ms" not in cached, cached
                finally:
                    (gate / "release").write_text("go")
                    await call(session, "java_application", dict(action="stop"))
        with (tmp_path / "reopen.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                try:
                    tested = await finish(session, await run(session, "test"), "test")
                    assert tested.get("passed") and tested.get("compiled_source_count") == 0, tested
                    launched = await finish(session, await run(session, "launch"), "launch")
                    assert launched.get("jdt_bootstrap_reused") is True and http() == "true", launched
                finally:
                    await call(session, "java_application", dict(action="stop"))

    anyio.run(scenario)
