"""Real MCP: declaration-based Java paths, resources, incremental and reopen."""

import json
import socket

import anyio
import pytest
from java_support import open_mcp_session, reserve_local_port, temporary_stderr
from test_fast_test_scopes import environment, run


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("reactor", [False, True])
def test_source_layout_through_mcp(tmp_path, reactor):
    env = environment(tmp_path)
    env["JOLINK_LOG_LEVEL"] = "INFO"
    project = tmp_path / "layout project"
    app = project / "app" if reactor else project
    library = project / "library" if reactor else project
    for path in {project, app, library}:
        path.mkdir(parents=True, exist_ok=True)
    properties = "<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>"
    identity = "<groupId>example.layout</groupId><version>1</version>"
    if reactor:
        (project / "pom.xml").write_text(
            f"<project><modelVersion>4.0.0</modelVersion>{identity}<artifactId>parent</artifactId><packaging>pom</packaging>{properties}<modules><module>library</module><module>app</module></modules></project>"
        )
        identity = "<parent><groupId>example.layout</groupId><artifactId>parent</artifactId><version>1</version></parent>"
        (library / "pom.xml").write_text(
            f"<project><modelVersion>4.0.0</modelVersion>{identity}<artifactId>library</artifactId></project>"
        )
    dependency = (
        "<dependency><groupId>example.layout</groupId><artifactId>library</artifactId><version>1</version></dependency>"
        if reactor
        else ""
    )
    (
        app / "pom.xml"
    ).write_text(f"""<project><modelVersion>4.0.0</modelVersion>{identity}<artifactId>app</artifactId>{properties}
<dependencies>{dependency}<dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><groupId>org.codehaus.mojo</groupId><artifactId>build-helper-maven-plugin</artifactId><version>3.5.0</version><executions>
<execution><id>inputs</id><phase>process-resources</phase><goals><goal>add-test-source</goal></goals><configuration><sources><source>${{project.basedir}}/src/test/resources</source></sources></configuration></execution>
</executions></plugin></plugins></build></project>""")
    foo = library / "src/main/java/fixtures/deep/Foo.java"
    foo.parent.mkdir(parents=True)
    original = "public class Foo { public static int value(){return 1;} public static class Nested {} }"
    foo.write_text(original)
    resource = app / "src/test/resources/inputs/deep/ResourceInput.java"
    resource.parent.mkdir(parents=True)
    resource_text = (
        "// Input deliberately has no package\npublic class ResourceInput {}"
    )
    resource.write_text(resource_text)
    test = app / "src/test/java/checks/LayoutTest.java"
    test.parent.mkdir(parents=True)

    def expect(name="Foo", value=1, present=True, absent=()):
        expression = (
            f'org.junit.Assert.assertEquals({value}, ((Integer)Class.forName("{name}").getMethod("value").invoke(null)).intValue());'
            if present
            else f'try{{Class.forName("{name}");org.junit.Assert.fail("stale class survived");}}catch(ClassNotFoundException expected){{}}'
        )
        for missing in absent:
            expression += f'try{{Class.forName("{missing}");org.junit.Assert.fail("stale class survived");}}catch(ClassNotFoundException expected){{}}'
        test.write_text(
            """package checks; public class LayoutTest {
@org.junit.Test public void layout() throws Exception {
BODY
org.junit.Assert.assertEquals("ResourceInput", Class.forName("ResourceInput").getName());
try(java.io.InputStream stream=getClass().getResourceAsStream("/inputs/deep/ResourceInput.java")) {
 org.junit.Assert.assertNotNull(stream);
 java.io.ByteArrayOutputStream bytes=new java.io.ByteArrayOutputStream(); int b;
 while((b=stream.read())!=-1) bytes.write(b);
 org.junit.Assert.assertEquals("// Input deliberately has no package\\npublic class ResourceInput {}", bytes.toString("UTF-8"));
}
}}""".replace("BODY", expression)
        )

    expect()
    main = app / "src/main/java/unrelated/directory/App.java"
    main.parent.mkdir(parents=True)
    main.write_text("""package example; public class App {
public static void main(String[] args) throws Exception {
 try(java.net.ServerSocket server=new java.net.ServerSocket(Integer.parseInt(args[0]))) {
  while(true) {try(java.net.Socket socket=server.accept()) {
   if(socket.getInputStream().read()=='N') Class.forName("Foo$Nested");
   socket.getOutputStream().write(String.valueOf(Class.forName("Foo").getMethod("value").invoke(null)).getBytes("UTF-8"));
  }}
 }
}}""")
    port, debug = reserve_local_port(), reserve_local_port()
    (project / ".run").mkdir()
    (project / ".run/App.xml").write_text(
        f'''<component name="ProjectRunConfigurationManager"><configuration name="App" type="Application"><module name="app"/><option name="MAIN_CLASS_NAME" value="example.App"/><option name="WORKING_DIRECTORY" value="$PROJECT_DIR$"/><option name="PROGRAM_PARAMETERS" value="{port}"/><method v="2"><option name="Make" enabled="true"/></method></configuration></component>'''
    )

    def full_count():
        return sum(
            p.read_text().count("requested=FULL")
            for p in (tmp_path / "cache").rglob("mcp.log")
        )

    async def scenario():
        before_reopen = None
        for cycle in range(2):
            with temporary_stderr() as stderr:
                async with open_mcp_session(stderr, environment=env) as session:

                    async def check():
                        return await run(session, project, ["checks.LayoutTest"])

                    baseline = await check()
                    assert baseline.get("passed"), json.dumps(baseline)
                    count = full_count()
                    if cycle:
                        assert baseline["compiled_source_count"] == 0
                        assert full_count() == before_reopen
                        continue
                    assert (await check())["compiled_source_count"] == 0
                    foo.write_text(original.replace("return 1", "return 2"))
                    expect(value=2)
                    assert (await check())["passed"]
                    # Same file moves into/out of a package; stale class must go.
                    foo.write_text("package moved; " + original)
                    expect("moved.Foo", absent=("Foo", "Foo$Nested"))
                    assert (await check())["passed"]
                    foo.write_text("package moved; public class Foo { invalid syntax }")
                    failed = await check()
                    assert failed.get("ok") is False
                    assert any(
                        d.get("resource") == str(foo) for d in failed["diagnostics"]
                    ), failed
                    foo.write_text("package moved; " + original)
                    assert (await check())["passed"]
                    foo.unlink()
                    expect("moved.Foo", present=False, absent=("moved.Foo$Nested",))
                    assert (await check())["passed"]
                    foo.write_text(original)
                    expect(absent=("moved.Foo", "moved.Foo$Nested"))
                    assert (await check())["passed"]
                    # Rename the physical file's directory; declaration unchanged.
                    relocated = foo.parent.parent / "other/Foo.java"
                    relocated.parent.mkdir()
                    foo.rename(relocated)
                    assert (await check())["passed"]
                    relocated.rename(foo)
                    assert (await check())["passed"]
                    assert full_count() == count

                    async def call(tool, arguments):
                        return dict(
                            (await session.call_tool(tool, arguments)).structuredContent
                            or {}
                        )

                    async def poll(predicate):
                        with anyio.fail_after(100):
                            while True:
                                state = await call("java_status", {"action": "status"})
                                assert state.get("launch_phase") != "failed", state
                                if predicate(state):
                                    return state
                                await anyio.sleep(0.05)

                    accepted = await call(
                        "java_application",
                        {
                            "action": "launch",
                            "project_path": str(project),
                            "launch_name": "App",
                            "jdwp_port": debug,
                            "ready_port": port,
                            "timeout": 10,
                        },
                    )
                    assert accepted["ok"], accepted
                    try:
                        await poll(lambda s: s.get("launch_phase") == "runtime_active")

                        def response(load_nested=False):
                            with socket.create_connection(
                                ("127.0.0.1", port), timeout=3
                            ) as connection:
                                connection.sendall(b"N" if load_nested else b"V")
                                return connection.recv(20)

                        assert await anyio.to_thread.run_sync(response) == b"1"
                        for value in (2, 1):
                            foo.write_text(
                                original.replace("return 1", f"return {value}")
                            )
                            reload = await call(
                                "java_application",
                                {"action": "restart", "timeout": 0, "source_files": [str(foo)]},
                            )
                            assert reload["status"] == "restart_started", reload
                            state = await poll(
                                lambda s, rid=reload["reload_id"]: (
                                    (s.get("last_reload") or {}).get("reload_id") == rid
                                )
                            )
                            if value == 2:
                                # A same-source nested class has not been loaded;
                                # restart applies the already compiled output.
                                assert (
                                    state["last_reload"]["restart_reason"]
                                    == "CLASS_NOT_LOADED"
                                ), state
                                assert state["last_reload"]["apply_method"] == "restart"
                                assert (
                                    await anyio.to_thread.run_sync(response, True)
                                    == b"2"
                                )
                            assert state["last_reload"]["applied"], json.dumps(
                                state["last_reload"]
                            )
                            assert (
                                await anyio.to_thread.run_sync(response)
                                == str(value).encode()
                            )
                    finally:
                        await call("java_application", {"action": "stop"})
                    assert (await check())["passed"]
                    before_reopen = full_count()
        assert resource.read_text() == resource_text
        assert foo.read_text() == original

    anyio.run(scenario)
