from __future__ import annotations

import os
from pathlib import Path
import socket

import anyio
import pytest

from java_support import open_mcp_session, temporary_stderr, require_real_mcp_java_e2e, reserve_local_port


@pytest.mark.mcp_java_e2e
def test_three_module_mcp_compile_reload_test_and_reopen(tmp_path: Path):
    require_real_mcp_java_e2e()
    project = tmp_path / "模块 workspace"
    project.mkdir()
    (project / "pom.xml").write_text('''<project><modelVersion>4.0.0</modelVersion>
<groupId>example.modules</groupId><artifactId>parent</artifactId><version>1</version><packaging>pom</packaging>
<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<modules><module>base</module><module>core</module><module>app</module><module>unused</module></modules></project>''')
    for name, upstream in (("base",None),("core","base"),("app","core"),("unused",None)):
        module = project / name
        (module / "src/main/java/example").mkdir(parents=True)
        dependencies = (f'<dependency><groupId>example.modules</groupId><artifactId>{upstream}</artifactId><version>1</version></dependency>' if upstream else '')
        if name == "app": dependencies += '<dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency><dependency><groupId>example.modules</groupId><artifactId>base</artifactId><version>1</version><type>test-jar</type><scope>test</scope></dependency>'
        (module / "pom.xml").write_text(f'<project><modelVersion>4.0.0</modelVersion><parent><groupId>example.modules</groupId><artifactId>parent</artifactId><version>1</version></parent><artifactId>{name}</artifactId><dependencies>{dependencies}</dependencies></project>')
        if name == "base":
            pom = module / "pom.xml"
            pom.write_text(pom.read_text().replace("</project>", "<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><configuration><parameters>true</parameters></configuration></plugin></plugins></build></project>"))
    base = project / "base/src/main/java/example/Base.java"
    original = 'package example; public class Base { public static final int NUMBER=40; public static int value(){return NUMBER;} public static int identity(int input){return input;} }'
    base.write_text(original)
    helper = project / "base/src/test/java/example/TestHelper.java"
    helper.parent.mkdir(parents=True)
    helper.write_text('package example; public class TestHelper { public static int expected(){ return 42; } }')
    (project / "core/src/main/java/example/Core.java").write_text('package example; public class Core { public static int value(){ return Base.value()+Base.NUMBER-40+2; } }')
    (project / "unused/src/main/java/example/Broken.java").write_text('deliberately invalid java')
    (project / "app/src/main/java/example/App.java").write_text('''package example; public class App {
public static void main(String[] args) throws Exception {
 try(java.net.ServerSocket server=new java.net.ServerSocket(Integer.parseInt(args[0]))) {
  while(true) { try(java.net.Socket socket=server.accept()) { socket.getOutputStream().write(String.valueOf(Core.value()).getBytes("UTF-8")); } }
 }
}}''')
    test = project / "app/src/test/java/example/AppTest.java"
    test.parent.mkdir(parents=True)
    test.write_text('package example; public class AppTest { @org.junit.Test public void value() throws Exception { org.junit.Assert.assertEquals(TestHelper.expected(),Core.value()); org.junit.Assert.assertTrue(Base.class.getMethod("identity",int.class).getParameters()[0].isNamePresent()); } }')
    # Extra Surefire runtime paths must survive Reactor world conversion too.
    extra = project / "app/runtime-extra"
    extra.mkdir()
    (extra / "fixture.txt").write_text("extra runtime resource")
    app_pom = project / "app/pom.xml"
    app_pom.write_text(app_pom.read_text().replace("</project>",
        '<build><plugins><plugin><artifactId>maven-surefire-plugin</artifactId><configuration>'
        '<additionalClasspathElements><additionalClasspathElement>runtime-extra</additionalClasspathElement></additionalClasspathElements>'
        '</configuration></plugin></plugins></build></project>'))
    test.write_text(test.read_text().replace("org.junit.Assert.assertEquals",
        'org.junit.Assert.assertNotNull(getClass().getClassLoader().getResource("fixture.txt")); org.junit.Assert.assertEquals'))
    port, debug = reserve_local_port(), reserve_local_port()
    (project / ".run").mkdir()
    (project / ".run/App.xml").write_text(f'''<component name="ProjectRunConfigurationManager"><configuration name="App" type="Application"><module name="app"/><option name="MAIN_CLASS_NAME" value="example.App"/><option name="WORKING_DIRECTORY" value="$PROJECT_DIR$"/><option name="PROGRAM_PARAMETERS" value="{port}"/><method v="2"><option name="Make" enabled="true"/></method></configuration></component>''')
    env = {**os.environ, "XDG_CACHE_HOME": str(tmp_path / "cache"), "MAVEN_ARGS": "-o"}
    jdk = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if jdk:
        env.update(JAVA_HOME=jdk, PATH=str(Path(jdk)/"bin")+os.pathsep+env.get("PATH",""))

    async def scenario():
        for cycle in range(2):
            with temporary_stderr() as stderr:
                async with open_mcp_session(stderr, environment=env) as session:
                    async def call(tool, args):
                        return dict((await session.call_tool(tool, args)).structuredContent or {})
                    async def poll(predicate):
                        with anyio.fail_after(120):
                            while True:
                                state = await call("java_status", {"action":"status"})
                                assert state.get("launch_phase") != "failed", state
                                if predicate(state): return state
                                await anyio.sleep(.03)
                    async def run_test():
                        value = await call("java_application", {"action":"test","project_path":str(project),"tests":["example.AppTest#value"],"timeout":20})
                        if value.get("status") in {"starting","bootstrapping","compiling","running"}:
                            state = await poll(lambda s:s.get("fast_test",{}).get("status") not in {"starting","bootstrapping","compiling","running"})
                            value = state["fast_test"]
                        return value
                    baseline = await run_test()
                    assert baseline["passed"] is True, baseline
                    if cycle == 0:
                        base.write_text(original.replace('NUMBER=40','NUMBER=39'))
                        failed = await run_test()
                        assert failed["passed"] is False, failed
                        assert failed["compiled_source_count"] >= 2, failed
                        base.write_text(original)
                        assert (await run_test())["passed"] is True
                        # An upstream test-jar is visible to app tests, not app main code.
                        app = project / "app/src/main/java/example/App.java"
                        app_original = app.read_text()
                        app.write_text(app_original.replace("public class App {", "public class App { int illegal = TestHelper.expected();"))
                        assert (await run_test())["ok"] is False
                        app.write_text(app_original)
                        assert (await run_test())["passed"] is True
                        # Upstream signature change must invalidate downstream compilation.
                        base.write_text(original.replace("int value(){return NUMBER;}", 'String value(){return "bad";}'))
                        assert (await run_test())["ok"] is False
                        base.write_text(original)
                        assert (await run_test())["passed"] is True
                        extra = base.with_name("Extra.java")
                        extra.write_text("package example; public class Extra { static int number(){return 40;} }")
                        base.write_text(original.replace("return NUMBER;", "return Extra.number();"))
                        assert (await run_test())["passed"] is True
                        extra.unlink()
                        base.write_text(original)
                        assert (await run_test())["passed"] is True
                        base.write_text('package example; broken')
                        assert (await run_test())["ok"] is False
                        assert (await run_test())["ok"] is False
                        base.write_text(original)
                        assert (await run_test())["passed"] is True
                    accepted = await call("java_application", {"action":"launch","project_path":str(project),"launch_name":"App","jdwp_port":debug,"ready_port":port,"startup_wait_timeout_seconds":10})
                    assert accepted["ok"], accepted
                    active = await poll(lambda s:s.get("launch_phase")=="runtime_active")
                    assert active["jdt_bootstrap_reused"] is bool(cycle)
                    if cycle: assert active["jdt_bootstrap_build_kind"] is None
                    def value():
                        with socket.create_connection(("127.0.0.1",port),timeout=3) as s:
                            return s.recv(64).decode()
                    assert await anyio.to_thread.run_sync(value) == "42"
                    for replacement, expected in ((original.replace('NUMBER=40','NUMBER=39'),"40"),(original,"42")):
                        base.write_text(replacement)
                        reload = await call("java_application", {"action":"reload","source_files":["base/src/main/java/example/Base.java"]})
                        assert reload["status"]=="reload_started", reload
                        completed = await poll(lambda s:s.get("last_reload",{}).get("reload_id")==reload["reload_id"] if s.get("last_reload") else False)
                        assert completed["last_reload"]["applied"] is True, completed
                        assert await anyio.to_thread.run_sync(value) == expected
                    assert (await call("java_application",{"action":"stop"}))["ok"]
        for name in ("base","core","app","unused"):
            assert not (project/name/"target").exists()
        assert len(list((tmp_path/"cache").rglob("modules.properties"))) == 2
    anyio.run(scenario)
