"""A post-test-classes goal must consume real freshly compiled test classes."""
import os
from pathlib import Path
import shutil
import socket
import subprocess

import anyio
import pytest
from java_support import call_workflow, open_mcp_session, reserve_local_port, temporary_stderr
from test_fast_test_scopes import environment


@pytest.mark.mcp_java_e2e
def test_packaged_test_classes_follow_java_and_resource_changes(tmp_path):
    env = environment(tmp_path, java11=True)
    project = tmp_path / "project"
    def write(name, text):
        path = project / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(text, encoding="utf-8")
        return path
    pom = write("pom.xml", """<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId>
<artifactId>pack</artifactId><version>1</version><properties><maven.compiler.source>8</maven.compiler.source>
<maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties><dependencies><dependency><groupId>junit</groupId>
<artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version></plugin>
<plugin><artifactId>maven-antrun-plugin</artifactId><version>3.2.0</version><executions><execution>
<id>bundle</id><phase>process-test-classes</phase><goals><goal>run</goal></goals><configuration><target>
<delete file="${project.build.directory}/my-test.zip" quiet="true"/><zip destfile="${project.build.directory}/my-test.zip">
<fileset dir="${project.build.testOutputDirectory}" includes="example/Marker.class"/>
<fileset dir="${project.basedir}/payload"/></zip></target></configuration>
</execution></executions></plugin>
<plugin><groupId>org.codehaus.mojo</groupId><artifactId>animal-sniffer-maven-plugin</artifactId><version>1.24</version>
<executions><execution><id>check-api</id><goals><goal>check</goal></goals></execution></executions>
<configuration><signature><groupId>org.codehaus.mojo.signature</groupId><artifactId>java18</artifactId><version>1.0</version>
</signature></configuration></plugin></plugins></build></project>""")
    api = write("src/main/java/example/Api.java", """package example;
public class Api {
 public static String value(){return "api-A";}
 public static void main(String[] args) throws Exception {
  try(java.net.ServerSocket s=new java.net.ServerSocket(Integer.parseInt(args[0]))) {
   while(true) try(java.net.Socket c=s.accept()){c.getOutputStream().write(value().getBytes("UTF-8"));}
  }
 }
}""")
    expected_api = write("expected-api", "api-A")
    marker = write("src/test/java/example/Marker.java",
                   'package example; class Marker {static String id(){return "marker-A";}}')
    payload = write("payload/entry", "payload-A")
    oracle = write("expected", "marker-A:payload-A")
    write("src/test/java/example/ArtifactTest.java", """package example;
import java.nio.file.*; import java.util.zip.*; import java.io.*; import static org.junit.Assert.*;
public class ArtifactTest {
 static String text(ZipFile z, String path) throws Exception {
   try(InputStream in=z.getInputStream(z.getEntry(path)); ByteArrayOutputStream out=new ByteArrayOutputStream()){
     byte[] b=new byte[4096]; int n; while((n=in.read(b))!=-1)out.write(b,0,n);return out.toString("ISO-8859-1");}}
 @org.junit.Test public void verify() throws Exception {
  assertEquals(new String(Files.readAllBytes(Paths.get("expected-api")),"UTF-8"), Api.value());
  String[] e=new String(Files.readAllBytes(Paths.get("expected")),"UTF-8").split(":");
  try(ZipFile z=new ZipFile("target/my-test.zip")){
    assertTrue(text(z,"example/Marker.class").contains(e[0]));assertEquals(e[1],text(z,"entry"));}}
}""")
    native = tmp_path / "native"
    shutil.copytree(project, native)
    settings = os.environ.get("JOLINK_TEST_MAVEN_SETTINGS")
    command = [shutil.which("mvn"), "-B", "-o", *(["-s", settings] if settings else []), "-Dtest=ArtifactTest", "test"]
    native_test = subprocess.run(command, cwd=native, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert b"(check-api)" in native_test.stdout and b"org.codehaus.mojo.signature:java18:1.0" in native_test.stdout
    args = dict(project_path=str(project), tests=["example.ArtifactTest"], timeout=30,
                **({"maven_settings":settings} if settings else {}))
    async def scenario(reopened=False):
        with temporary_stderr() as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                async def check(warm=False):
                    r = dict((await session.call_tool("java_fast_test",args)).structuredContent)
                    with anyio.fail_after(150):
                        while r.get("status") in {"starting","bootstrapping","compiling","running"}:
                            await anyio.sleep(.2)
                            r=dict((await session.call_tool("java_fast_test",
                                {"action":"result","test_run_id":r["test_run_id"]})).structuredContent)
                    if not r.get("passed") and r.get("test_run_id"):
                        r = dict((await session.call_tool("java_fast_test", {"action":"result","test_run_id":r["test_run_id"]})).structuredContent)
                        (tmp_path/"failed-result.txt").write_text(str(r), encoding="utf-8")
                    assert r.get("tests")==1 and r.get("passed") and r.get("compile_backend")=="maven_native",r
                    assert "org.codehaus.mojo:animal-sniffer-maven-plugin:check@check-api" in r["skipped_build_steps"], r
                    assert (r.get("compiled_source_count")==0) if warm else ("compiled_source_count" not in r)
                await check(warm=reopened)
                if reopened:
                    # An explicit m2e execution still wins over the development
                    # default. Its missing signature must fail rather than pass.
                    original = pom.read_text()
                    pom.write_text(original.replace("<id>check-api</id>", "<id>check-api</id><?m2e execute?>")
                                   .replace("<artifactId>java18</artifactId>", "<artifactId>jolink-missing-api</artifactId>"))
                    try:
                        failed = await call_workflow(session, "java_fast_test", args)
                        assert failed.get("error_code") == "MAVEN_NATIVE_COMPILE_FAILED", failed
                        diagnostic = str(failed.get("diagnostics"))
                        assert "check@check-api" in diagnostic and "jolink-missing-api" in diagnostic, failed
                    finally:
                        pom.write_text(original)
                    return
                await check(warm=True)
                payload.write_text("payload-B+changed")
                oracle.write_text("marker-A:payload-B+changed")
                await check()
                marker.write_text(marker.read_text().replace("marker-A","marker-new-B"))
                oracle.write_text("marker-new-B:payload-B+changed")
                await check()
                (project/"target/my-test.zip").unlink()
                await check()
                port = reserve_local_port()
                def live():
                    with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
                        return c.makefile("rb").read().decode("utf-8")
                try:
                    start = await call_workflow(session, "java_application", {
                        "action":"launch", "project_path":str(project), "main_class":"example.Api",
                        "ready_port":port, "jdwp_port":reserve_local_port(), "app_args":[str(port)], "timeout":0,
                        **({"maven_settings":settings} if settings else {}),
                    })
                    assert start.get("startup_state") == "ready" and live() == "api-A", start
                    api.write_text(api.read_text().replace("api-A", "api-B"))
                    expected_api.write_text("api-B")
                    await check()
                    assert live() == "api-A"
                    update = await call_workflow(session, "java_application", {"action":"restart","timeout":0})
                    assert update.get("ok") and live() == "api-B", update
                finally:
                    await session.call_tool("java_application", {"action":"stop"})
    anyio.run(scenario)
    anyio.run(scenario, True)
