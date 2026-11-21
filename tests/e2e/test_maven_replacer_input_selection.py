"""A preparation selector is an input even if it first matches after the Probe."""
import os
from pathlib import Path
from urllib.request import urlopen

import anyio
import pytest

from java_support import open_mcp_session, temporary_stderr, reserve_local_port
from test_fast_test_scopes import environment


@pytest.mark.mcp_java_e2e
def test_replacer_tracks_matches_not_unrelated_compiler_outputs(tmp_path):
    env = environment(tmp_path, java11=True)
    root = tmp_path / "project"
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path
    write(root/"pom.xml", """<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId>
<artifactId>selection</artifactId><version>1</version><properties><maven.compiler.source>8</maven.compiler.source>
<maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version></plugin>
<plugin><groupId>com.google.code.maven-replacer-plugin</groupId><artifactId>replacer</artifactId><version>1.5.3</version>
<executions><execution><id>transform</id><phase>process-resources</phase><goals><goal>replace</goal></goals></execution></executions>
<configuration><basedir>${project.build.directory}</basedir><includes><include>marker.txt</include></includes>
<filesToInclude>extras/**/*.txt</filesToInclude><excludes><exclude>**/ignored/**</exclude></excludes>
<token>TOKEN</token><value>READY</value></configuration></plugin></plugins></build></project>""")
    write(root/"src/main/java/example/App.java", """package example;
import com.sun.net.httpserver.HttpServer; import java.net.*; import java.nio.file.*;
public class App {
 public static String value() throws Exception {
  Path input=Paths.get("target/marker.txt");
  return Files.isRegularFile(input)?new String(Files.readAllBytes(input),"UTF-8"):"absent";}
 public static void main(String[] args)throws Exception{
  HttpServer http=HttpServer.create(new InetSocketAddress("127.0.0.1",Integer.parseInt(args[0])),0);
  http.createContext("/",e->{try {byte[] data=value().getBytes("UTF-8");e.sendResponseHeaders(200,data.length);
  try(java.io.OutputStream out=e.getResponseBody()){out.write(data);}}catch(Exception x){throw new RuntimeException(x);}});
  http.start();}}""")
    write(root/"src/test/java/example/MarkerTest.java","""package example;
public class MarkerTest {@org.junit.Test public void check() throws Exception {
 org.junit.Assert.assertEquals(new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get("expected")),"UTF-8"), App.value());}}""")
    expected=write(root/"expected","absent")
    target=root/"target";target.mkdir()
    opts={"maven_settings":os.environ["JOLINK_TEST_MAVEN_SETTINGS"]} if os.environ.get("JOLINK_TEST_MAVEN_SETTINGS") else {}
    async def scenario():
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                async def call(tool,args):
                    return dict((await session.call_tool(tool,args)).structuredContent)
                async def check(*, reused):
                    r=await call("java_fast_test",dict(project_path=str(root),tests=["example.MarkerTest"],timeout=30,**opts))
                    with anyio.fail_after(120):
                        while r.get("status") in {"starting","bootstrapping","compiling","running"}:
                            await anyio.sleep(.2)
                            r=await call("java_fast_test",dict(action="result",test_run_id=r["test_run_id"]))
                    assert r.get("passed") and r.get("tests")==1,r
                    assert ("freshness_ms" in r) == reused,r
                    if reused: assert r.get("compiled_source_count")==0,r
                await check(reused=False)
                write(target/"classes/unrelated.class","unrelated")
                ignored=write(target/"extras/ignored/m.txt","TOKEN")
                await check(reused=True)
                assert ignored.read_text()=="TOKEN"
                marker=write(target/"marker.txt","TOKEN-test");expected.write_text("READY-test")
                extra=write(target/"extras/added.txt","TOKEN-first")
                await check(reused=False)
                assert extra.read_text()=="READY-first"
                extra.unlink()
                await check(reused=False)
                await check(reused=True)
                # Do not let the test path prepare these values for launch/restart.
                port=reserve_local_port()
                try:
                    for action in ("launch","restart"):
                        marker.write_text("TOKEN-"+action)
                        args=(dict(project_path=str(root),main_class="example.App",jdwp_port=reserve_local_port(),ready_port=port,
                            app_args=[str(port)],**opts) if action=="launch" else dict(hotswap=False))
                        result=await call("java_application",dict(action=action,timeout=30,**args))
                        with anyio.fail_after(120):
                            while result.get("ok", True) and result.get("startup_state")!="ready":
                                await anyio.sleep(.2)
                                state=await call("java_status",dict(action="status",details=True))
                                result=state.get("last_reload") or state if action=="restart" else state
                        assert result.get("startup_state")=="ready",result
                        with urlopen(f"http://127.0.0.1:{port}/",timeout=5) as resp:
                            assert resp.read().decode()=="READY-"+action
                finally: await call("java_application",dict(action="stop"))
    anyio.run(scenario)
