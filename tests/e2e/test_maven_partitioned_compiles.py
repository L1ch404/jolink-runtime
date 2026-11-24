"""Maven, not JDT, executes differently filtered javac passes in lifecycle order."""
import os
import shutil
import socket
import subprocess
from pathlib import Path
import anyio
import pytest
from java_support import call_workflow, open_mcp_session, reserve_local_port, temporary_stderr
from test_maven_compiler_diagnostics import environment, make_project


@pytest.mark.mcp_java_e2e
def test_filtered_compiler_passes_share_output_without_flattening_platforms(tmp_path):
    env, java = environment(tmp_path)
    project, main, test = make_project(tmp_path)
    pom = project / "pom.xml"
    pom.write_text(pom.read_text().replace("<configuration></configuration>", """
<configuration><release>8</release><excludes><exclude>**/Later.java</exclude></excludes></configuration>
<executions><execution><id>new-platform</id><phase>compile</phase><goals><goal>compile</goal></goals>
<configuration><release>11</release><includes><include>**/Later.java</include></includes>
<excludes combine.self="override"/></configuration></execution></executions>""", 1))
    later = main.parent / "Later.java"
    later.write_text('package repro; public class Later { public static String value(){return " A ".strip();} }')
    main.write_text("""package repro;
import java.io.*; import java.net.*;
public class Calculator {
 static int major(Class<?> c) throws Exception {
  try(DataInputStream in = new DataInputStream(c.getResourceAsStream(c.getSimpleName()+".class"))) {
   in.readInt(); in.readUnsignedShort(); return in.readUnsignedShort(); }
 }
 public static String value() throws Exception {
  Class<?> c=Class.forName("repro.Later");
  return c.getMethod("value").invoke(null)+":"+major(Calculator.class)+":"+major(c);
 }
 public static void main(String[] args) throws Exception {try(ServerSocket s=new ServerSocket(Integer.parseInt(args[0]))) {
  while(true) try(Socket p=s.accept()){p.getOutputStream().write(value().getBytes("UTF-8"));} }}
}""")
    check = 'package repro; public class SmokeTest { @org.junit.Test public void check() throws Exception {org.junit.Assert.assertEquals("A:52:55",Calculator.value());} }'
    test.write_text(check)
    oracle = tmp_path / "native"
    shutil.copytree(project, oracle)
    settings = os.getenv("JOLINK_TEST_MAVEN_SETTINGS")
    native = subprocess.run(["mvn","--offline","-B",*(["-s",settings] if settings else []),"test"],cwd=oracle,
                            env=env, capture_output=True,text=True,errors="replace",timeout=120)
    assert native.returncode == 0, native.stdout + native.stderr
    common = dict(project_path=str(project), java_home=str(java), build_java_home=str(java), timeout=30)
    if settings: common["maven_settings"] = settings
    port = reserve_local_port()
    def observed():
        with socket.create_connection(("127.0.0.1",port),timeout=5) as s:
            return s.makefile("rb").read().decode()
    async def scenario():
        with temporary_stderr() as log:
            async with open_mcp_session(log,environment=env) as s:
                async def run():
                    return await call_workflow(s,"java_fast_test",dict(common,tests=["repro.SmokeTest"]))
                cold = await run()
                assert cold.get("passed") and cold["compile_backend"] == "maven_native", cold
                assert (await run())["compiled_source_count"] == 0
                try:
                    launch=await call_workflow(s,"java_application",dict(common,action="launch",main_class="repro.Calculator",
                        app_args=[str(port)],jdwp_port=reserve_local_port(),ready_port=port))
                    assert launch.get("startup_state")=="ready" and observed()=="A:52:55",launch
                    later.write_text(later.read_text().replace('" A "', '" B "'))
                    assert (await run()).get("failed_count")==1
                    test.write_text(check.replace("A:52:55","B:52:55"))
                    assert (await run()).get("passed") and observed()=="A:52:55"
                    update=await call_workflow(s,"java_application",dict(action="restart",timeout=30))
                    assert update.get("ok") and observed()=="B:52:55",update
                finally: await s.call_tool("java_application",dict(action="stop"))
    anyio.run(scenario)
