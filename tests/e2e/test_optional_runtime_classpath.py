"""Absent runtime outputs follow Java semantics, not Python/Runner existence checks."""

import json
import os
import subprocess
from pathlib import Path
from urllib.request import urlopen

import anyio
import pytest

from java_support import open_mcp_session, require_real_mcp_java_e2e, reserve_local_port


@pytest.mark.mcp_java_e2e
def test_absent_runtime_entries_work_for_fast_test_and_application(tmp_path):
    require_real_mcp_java_e2e()
    home = os.environ.get("JOLINK_TEST_JAVA17_HOME")
    if not home:
        pytest.skip("JDK17 is required")
    java = Path(home)
    project = tmp_path / "project"
    source = project / "src/main/java/example/App.java"
    source.parent.mkdir(parents=True)
    source.write_text('''package example;
import com.sun.net.httpserver.HttpServer; import java.net.InetSocketAddress;
public class App {
 public static String value(){return "ready";}
 public static void main(String[] args) throws Exception {
  HttpServer s=HttpServer.create(new InetSocketAddress("127.0.0.1",Integer.parseInt(args[0])),0);
  s.createContext("/", e->{byte[] b=value().getBytes("UTF-8"); e.sendResponseHeaders(200,b.length); e.getResponseBody().write(b);e.close();});s.start();
 }
}''', encoding="utf-8")
    missing = project / "uncreated output"
    absent_jar = project / "optional.jar"
    test = project / "src/test/java/example/RuntimePathTest.java"
    test.parent.mkdir(parents=True)
    test.write_text('''package example; public class RuntimePathTest {
 @org.junit.Test public void runs(){
  org.junit.Assert.assertEquals("ready",App.value());
  org.junit.Assert.assertTrue(System.getProperty("java.class.path").contains("uncreated output"));
 }
}''', encoding="utf-8")
    (project / "pom.xml").write_text('''<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>runtime-path</artifactId><version>1</version>
<properties><maven.compiler.release>17</maven.compiler.release><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins>
<plugin><artifactId>maven-resources-plugin</artifactId><version>3.3.0</version></plugin>
<plugin><artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version></plugin>
<plugin><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version><configuration>
<additionalClasspathElements><additionalClasspathElement>${project.basedir}/uncreated output</additionalClasspathElement>
<additionalClasspathElement>${project.basedir}/optional.jar</additionalClasspathElement></additionalClasspathElements>
</configuration></plugin></plugins></build></project>''', encoding="utf-8")
    classes = tmp_path / "native classes"
    suffix = ".exe" if os.name == "nt" else ""
    subprocess.run([str(java / f"bin/javac{suffix}"), "-d", str(classes), str(source)],
                   check=True, capture_output=True, timeout=30)
    env = {**os.environ, "JAVA_HOME": str(java), "PATH": str(java / "bin") + os.pathsep + os.environ["PATH"],
           "MAVEN_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache")}
    if os.name == "nt":
        env["LOCALAPPDATA"] = env["XDG_CACHE_HOME"]
    port = reserve_local_port()
    def http():
        with urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            assert response.status == 200
            return response.read().decode()
    async def scenario():
        with (tmp_path / "mcp.log").open("w") as log:
            async with open_mcp_session(log, environment=env) as session:
                async def call(tool, args):
                    result = dict((await session.call_tool(tool, args)).structuredContent)
                    with (tmp_path / "calls.jsonl").open("a") as stream:
                        stream.write(json.dumps({"tool": tool, "args": args, "result": result}) + "\n")
                    return result
                try:
                    launched = await call("java_application", dict(action="launch", main_class="example.App",
                        classpath=os.pathsep.join(map(str, (classes, missing, absent_jar))), java_home=str(java),
                        app_args=[str(port)], ready_port=port, jdwp_port=reserve_local_port(), timeout=30))
                    assert launched.get("ok") and launched.get("startup_state") == "ready", launched
                    assert http() == "ready"
                    for warm in (False, True):
                        args = dict(project_path=str(project), tests=["example.RuntimePathTest"],
                                    java_home=str(java), build_java_home=str(java), timeout=30)
                        if os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
                            args["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]
                        result = await call("java_fast_test", args)
                        with anyio.fail_after(120):
                            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                                await anyio.sleep(.2)
                                result = await call("java_fast_test", {"action": "result", "test_run_id": result["test_run_id"]})
                        assert result.get("passed") and result.get("tests") == 1, result
                        if warm:
                            assert result["compiled_source_count"] == 0
                        assert (await call("java_status", {"action": "status"}))["pid"] == launched["pid"]
                        assert http() == "ready"
                        assert not missing.exists() and not absent_jar.exists()
                    restarted = await call("java_application", {"action": "restart", "timeout": 30})
                    assert restarted.get("ok") and restarted.get("startup_state") == "ready", restarted
                    assert restarted["pid"] != launched["pid"] and http() == "ready"
                    await call("java_application", {"action": "stop"})
                    args = dict(action="launch", project_path=str(project), main_class="example.App",
                                java_home=str(java), build_java_home=str(java), ready_port=port,
                                jdwp_port=reserve_local_port(), app_args=[str(port)], timeout=30)
                    if os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
                        args["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]
                    project_launch = await call("java_application", args)
                    assert project_launch.get("ok") and project_launch.get("startup_state") == "ready", project_launch
                    assert http() == "ready"
                    project_restart = await call("java_application", {"action": "restart", "timeout": 30})
                    assert project_restart.get("ok") and project_restart.get("startup_state") == "ready", project_restart
                    assert project_restart["pid"] != project_launch["pid"] and http() == "ready"
                    assert not missing.exists() and not absent_jar.exists()
                finally:
                    await call("java_application", {"action": "stop"})
    anyio.run(scenario)
