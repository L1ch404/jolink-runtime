"""The development loop must neither ignore a Java 6 target nor silently raise it to 8."""
import os
from pathlib import Path
import shutil
from urllib.request import urlopen

import anyio
import pytest

from java_support import call_workflow, open_mcp_session, require_real_mcp_java_e2e, reserve_local_port


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("main_level", [6, 8])
def test_maven_legacy_compiler_obeys_scope_bytecode_and_language_level(tmp_path, main_level):
    require_real_mcp_java_e2e()
    jdk = os.getenv("JOLINK_TEST_JAVA8_HOME")
    if not jdk or not shutil.which("mvn"):
        pytest.skip("requires Maven and JOLINK_TEST_JAVA8_HOME")
    project = tmp_path / "project"
    source = project / "src/main/java/example/App.java"
    test = project / "src/test/java/example/AppTest.java"
    for p in (source, test):
        p.parent.mkdir(parents=True)
    (project / "pom.xml").write_text(f"""<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>legacy-scope</artifactId><version>1</version>
<properties><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId>
<version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version>
<configuration><source>1.{main_level}</source><target>1.{main_level}</target>
<testSource>1.6</testSource><testTarget>1.6</testTarget></configuration></plugin>
<plugin><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version></plugin>
</plugins></build></project>""", encoding="utf-8")
    app = """package example;
import com.sun.net.httpserver.*; import java.io.*; import java.net.*;
public class App {
 public static String value(){return "A";}
 public static int major(Class<?> c) throws IOException {
   DataInputStream in = new DataInputStream(c.getResourceAsStream(c.getSimpleName()+".class"));
   try {if(in.readInt()!=0xcafebabe) throw new IOException("not bytecode"); in.readUnsignedShort(); return in.readUnsignedShort();}
   finally {in.close();}
 }
 public static void main(String[] args) throws Exception {
   HttpServer s=HttpServer.create(new InetSocketAddress("127.0.0.1",Integer.parseInt(args[0])),0);
   s.createContext("/",new HttpHandler(){public void handle(HttpExchange e) throws IOException {
     byte[] body=(value()+":"+major(App.class)).getBytes("UTF-8");
     e.sendResponseHeaders(200,body.length);e.getResponseBody().write(body);e.close();
   }}); s.start();
 }
}"""
    check = """package example;
import org.junit.Test; import static org.junit.Assert.*;
public class AppTest { @Test public void works() throws Exception {
 assertEquals("A",App.value()); assertEquals(MAJOR,App.major(App.class)); assertEquals(50,App.major(AppTest.class)); }
}""".replace("MAJOR", str(main_level + 44))
    source.write_text(app, encoding="utf-8")
    test.write_text(check, encoding="utf-8")
    common = dict(project_path=str(project), build_java_home=jdk, java_home=jdk, timeout=30)
    if os.getenv("JOLINK_TEST_MAVEN_SETTINGS"):
        common["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]
    env = {**os.environ, "MAVEN_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache"),
           "JAVA_HOME": jdk, "PATH": str(Path(jdk) / "bin") + os.pathsep + os.environ["PATH"]}
    if os.name == "nt":
        env["LOCALAPPDATA"] = env["XDG_CACHE_HOME"]
    port = reserve_local_port()
    def http():
        with urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            return r.read().decode()

    async def scenario():
        with (tmp_path / "mcp.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                async def run():
                    return await call_workflow(session, "java_fast_test", dict(common, tests=["example.AppTest"]))
                cold = await run()
                assert cold.get("passed") and cold["compile_backend"] == "maven_native", cold
                assert (await run()).get("compiled_source_count") == 0
                try:
                    started = await call_workflow(session, "java_application", dict(
                        common, action="launch", main_class="example.App", app_args=[str(port)],
                        jdwp_port=reserve_local_port(), ready_port=port))
                    assert started.get("startup_state") == "ready", started
                    expected_backend = "maven_native" if main_level == 6 else "jdt_incremental"
                    assert started["fast_update"]["strategy"] == expected_backend
                    assert http() == f"A:{44 + main_level}"
                    source.write_text(app.replace('return "A"', 'return "B"'), encoding="utf-8")
                    red = await run()
                    assert red.get("tests") == red.get("failed_count") == 1, red
                    green = check.replace('assertEquals("A"', 'assertEquals("B"')
                    test.write_text(green, encoding="utf-8")
                    assert (await run()).get("passed")
                    assert http() == f"A:{44 + main_level}"
                    changed = await call_workflow(session, "java_application", {"action":"restart","timeout":30})
                    assert changed.get("ok") and http() == f"B:{44 + main_level}", changed
                    # Maven must reject an actual Java 8-only construct in the Java 6 test scope.
                    test.write_text(green.replace("@Test public", "Runnable r = () -> {}; @Test public"), encoding="utf-8")
                    error = await run()
                    assert error.get("error_code") == "MAVEN_NATIVE_COMPILE_FAILED", error
                    assert http() == f"B:{44 + main_level}"
                    test.write_text(green, encoding="utf-8")
                    assert (await run()).get("passed")
                finally:
                    await session.call_tool("java_application", {"action": "stop"})
    anyio.run(scenario)
