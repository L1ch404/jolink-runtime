"""Maven's private source replacements must not move Lombok's config origin."""
import os
from pathlib import Path
from shutil import which
from urllib.request import urlopen

import anyio
import pytest

from java_support import call_workflow, open_mcp_session, require_real_mcp_java_e2e, reserve_local_port


@pytest.mark.mcp_java_e2e
def test_native_replacement_preserves_lombok_imports_and_config_only_edits(tmp_path):
    require_real_mcp_java_e2e()
    home = os.getenv("JOLINK_TEST_JAVA8_HOME")
    if not home or not which("mvn"):
        pytest.skip("requires Maven and JDK 8")
    project = tmp_path / "space project"

    def write(name, value):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    write("pom.xml", """<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>native-lombok</artifactId><version>1</version>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency>
<dependency><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId><version>1.18.30</version><scope>provided</scope></dependency></dependencies>
<build><plugins><plugin><groupId>com.google.code.maven-replacer-plugin</groupId><artifactId>replacer</artifactId>
<version>1.5.3</version><executions><execution><phase>process-sources</phase><goals><goal>replace</goal></goals>
</execution></executions><configuration><file>${project.basedir}/input.txt</file>
<outputFile>${project.build.sourceDirectory}/example/Pojo.java</outputFile><token>@type@</token><value>Pojo</value></configuration></plugin>
<plugin><artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version><configuration>
<source>8</source><target>8</target><compilerVersion>1.8</compilerVersion><fork>true</fork></configuration></plugin>
<plugin><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version></plugin></plugins></build></project>""")
    # Two independent scopes: parent location + an import from a source package
    # to a sibling outside src/main/java. Both must resolve from original paths.
    write("lombok.config", "import config/root.config\nconfig.stopBubbling = true\n")
    write("config/root.config", "lombok.accessors.chain = true\n")
    write("src/main/java/example/lombok.config", "import ../../../../config/package.config\n")
    package_config = write("config/package.config", "lombok.accessors.fluent = false\n")
    java = write("input.txt", "package example; @lombok.Setter public class @type@ { private int value; }")
    original = write("src/main/java/example/Pojo.java", "// Developer's uncompiled original\n")
    original_info = original.read_bytes(), original.stat().st_mtime_ns
    write("src/main/java/example/App.java", """package example;
public class App {
  static String result() { for(java.lang.reflect.Method m:Pojo.class.getDeclaredMethods())
    if(m.getParameterTypes().length==1) return m.getName()+":"+m.getReturnType().getSimpleName(); return "missing"; }
  public static void main(String[] args)throws Exception {
    com.sun.net.httpserver.HttpServer s=com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1",Integer.parseInt(args[0])),0);
    s.createContext("/",e->{byte[] b=result().getBytes("UTF-8"); e.sendResponseHeaders(200,b.length); e.getResponseBody().write(b); e.close();});s.start();
  }}""")
    test = write("src/test/java/example/AppTest.java", """package example; public class AppTest {
@org.junit.Test public void config(){ org.junit.Assert.assertEquals("setValue:Pojo",App.result()); }}""")
    port = reserve_local_port()
    common = dict(project_path=str(project), java_home=home, build_java_home=home, timeout=0)
    if os.getenv("JOLINK_TEST_MAVEN_SETTINGS"):
        common["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]
    env = {**os.environ, "JAVA_HOME": home, "PATH": str(Path(home)/"bin") + os.pathsep + os.environ["PATH"],
           "XDG_CACHE_HOME": str(tmp_path/"cache"), "MAVEN_ARGS": "--offline"}
    if os.name == "nt":
        env["LOCALAPPDATA"] = env["XDG_CACHE_HOME"]

    def http():
        with urlopen(f"http://127.0.0.1:{port}", timeout=5) as r:
            return r.read().decode()

    async def scenario():
        for reopen in (False, True):
            with (tmp_path / f"mcp-{reopen}.log").open("w") as log:
                async with open_mcp_session(log, environment=env) as session:
                    async def call(tool, args):
                        return await call_workflow(session, tool, args, tmp_path / "calls.jsonl")

                    async def run():
                        return await call("java_fast_test", dict(common, tests=["example.AppTest"]))

                    cold = await run()
                    assert cold.get("passed") and cold["tests"] == 1, cold
                    if reopen:
                        assert cold.get("compiled_source_count") == 0, cold
                        continue
                    try:
                        app = await call("java_application", dict(common, action="launch", main_class="example.App",
                            ready_port=port, jdwp_port=reserve_local_port(), app_args=[str(port)]))
                        assert app.get("startup_state") == "ready" and http() == "setValue:Pojo", app
                        # No Java edit: both independent compiler sessions must notice the import.
                        package_config.write_text("lombok.accessors.fluent = true\n", encoding="utf-8")
                        observed = await run()
                        assert observed.get("failed_count") == 1 and http() == "setValue:Pojo", observed
                        updated = await call("java_application", dict(action="restart", hotswap=False, timeout=0))
                        assert updated.get("startup_state") == "ready" and http() == "value:Pojo", updated
                        test.write_text(test.read_text().replace("setValue:Pojo", "value:Pojo"), encoding="utf-8")
                        assert (await run()).get("passed")
                    finally:
                        await session.call_tool("java_application", dict(action="stop"))
        assert (original.read_bytes(), original.stat().st_mtime_ns) == original_info
        assert "@type@" in java.read_text()
    anyio.run(scenario)
