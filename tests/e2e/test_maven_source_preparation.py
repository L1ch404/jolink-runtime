"""Real Maven generators -> persistent JDT -> MCP test/launch, without mvn compile."""

import json
import shutil
import socket

import anyio
import pytest
from java_support import open_mcp_session, reserve_local_port, temporary_stderr
from test_fast_test_scopes import environment, run


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("reactor", [False, True])
def test_antlr_preparation_and_cache_through_mcp(tmp_path, reactor):
    env = environment(tmp_path, java11=True)
    env["JOLINK_LOG_LEVEL"] = "INFO"
    project = tmp_path / "generated app"
    project.mkdir()
    grammar_module = project / "grammar" if reactor else project
    app = project / "app" if reactor else project
    grammar_module.mkdir(exist_ok=True)
    app.mkdir(exist_ok=True)
    properties = "<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>"
    parent = (
        "<parent><groupId>example.preparation</groupId><artifactId>parent</artifactId><version>1</version></parent>"
        if reactor
        else "<groupId>example.preparation</groupId><version>1</version>"
    )
    runtime = "<dependency><groupId>org.antlr</groupId><artifactId>antlr4-runtime</artifactId><version>4.13.1</version></dependency>"
    junit = "<dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency>"
    sentinel = """<plugin><artifactId>maven-antrun-plugin</artifactId><version>3.1.0</version><executions>
<execution><id>no-compile</id><phase>compile</phase><goals><goal>run</goal></goals><configuration><target><fail message="business Maven compile must not run"/></target></configuration></execution>
<execution><id>no-tests</id><phase>test</phase><goals><goal>run</goal></goals><configuration><target><fail message="native Maven tests must not run"/></target></configuration></execution>
</executions></plugin>"""
    generator = """<plugin><groupId>org.antlr</groupId><artifactId>antlr4-maven-plugin</artifactId><version>4.13.1</version>
<configuration><sourceDirectory>${project.basedir}/syntax</sourceDirectory><outputDirectory>${project.build.directory}/grammar-output</outputDirectory><listener>false</listener></configuration>
<executions><execution><id>grammar</id><goals><goal>antlr4</goal></goals></execution></executions></plugin>"""
    registration = """<plugin><groupId>org.codehaus.mojo</groupId><artifactId>build-helper-maven-plugin</artifactId><version>3.5.0</version>
<executions><execution><id>checks</id><phase>process-resources</phase><goals><goal>add-test-source</goal></goals>
<configuration><sources><source>${project.basedir}/checks</source></sources></configuration></execution></executions></plugin>"""
    if reactor:
        (project / "pom.xml").write_text(
            f"<project><modelVersion>4.0.0</modelVersion><groupId>example.preparation</groupId><artifactId>parent</artifactId><version>1</version><packaging>pom</packaging>{properties}<modules><module>grammar</module><module>app</module></modules></project>"
        )
        (grammar_module / "pom.xml").write_text(
            f"<project><modelVersion>4.0.0</modelVersion>{parent}<artifactId>grammar</artifactId><dependencies>{runtime}</dependencies><build><plugins>{generator}{sentinel}</plugins></build></project>"
        )
    dependency = (
        "<dependency><groupId>example.preparation</groupId><artifactId>grammar</artifactId><version>1</version></dependency>"
        if reactor
        else runtime
    )
    (app / "pom.xml").write_text(
        f"<project><modelVersion>4.0.0</modelVersion>{parent}<artifactId>app</artifactId>{properties}<dependencies>{dependency}{junit}</dependencies><build><plugins>{'' if reactor else generator}{registration}{sentinel}</plugins></build></project>"
    )
    grammar = grammar_module / "syntax/generated/Greeting.g4"
    grammar.parent.mkdir(parents=True)
    grammar_text = (
        "grammar Greeting; entry: WORD EOF; WORD: 'hello'; WS: [ \\t\\r\\n]+ -> skip;"
    )
    grammar.write_text(grammar_text)
    main = app / "src/main/java/example/App.java"
    main.parent.mkdir(parents=True)
    main_text = """package example; public class App {
 public static boolean accepts(String input) {
  generated.GreetingLexer lexer=new generated.GreetingLexer(org.antlr.v4.runtime.CharStreams.fromString(input));
  lexer.removeErrorListeners();
  generated.GreetingParser parser=new generated.GreetingParser(new org.antlr.v4.runtime.CommonTokenStream(lexer));
  parser.removeErrorListeners(); parser.entry(); return parser.getNumberOfSyntaxErrors()==0;
 }
 public static void main(String[] args) throws Exception {
  try(java.net.ServerSocket server=new java.net.ServerSocket(Integer.parseInt(args[0]))) {
   while(true) {try(java.net.Socket socket=server.accept()) {socket.getOutputStream().write(Boolean.toString(accepts("hello")).getBytes("UTF-8"));}}
  }
 }
}"""
    main.write_text(main_text)
    test = app / "src/test/java/example/GrammarTest.java"
    test.parent.mkdir(parents=True)
    test.write_text(
        "package example; public class GrammarTest { @org.junit.Test public void grammar(){org.junit.Assert.assertTrue(App.accepts(Helper.word()));} }"
    )
    helper = app / "checks/example/Helper.java"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        'package example; public class Helper { public static String word(){return "hello";} }'
    )
    port, debug = reserve_local_port(), reserve_local_port()
    (project / ".run").mkdir()
    (project / ".run/App.xml").write_text(
        f'''<component name="ProjectRunConfigurationManager"><configuration name="App" type="Application"><module name="app"/><option name="MAIN_CLASS_NAME" value="example.App"/><option name="WORKING_DIRECTORY" value="$PROJECT_DIR$"/><option name="PROGRAM_PARAMETERS" value="{port}"/><method v="2"><option name="Make" enabled="true"/></method></configuration></component>'''
    )

    def world():
        return next((tmp_path / "cache").rglob("build-world.json"))

    def probe_stamp():
        return world().stat().st_mtime_ns

    def full_builds():
        return sum(
            path.read_text().count("requested=FULL")
            for path in (tmp_path / "cache").rglob("mcp.log")
        )

    async def scenario():
        for cycle in range(2):
            with temporary_stderr() as stderr:
                async with open_mcp_session(stderr, environment=env) as session:

                    async def check():
                        return await run(session, project, ["example.GrammarTest"])

                    baseline = await check()
                    assert baseline.get("passed"), json.dumps(baseline)
                    if cycle:
                        assert baseline["compiled_source_count"] == 0, baseline
                        continue
                    stamp = probe_stamp()
                    full_count = full_builds()
                    assert full_count >= 1
                    assert (await check())["passed"] and probe_stamp() == stamp
                    # Ordinary Java (including a registered custom source root)
                    # stays entirely inside the existing Worker.
                    for source in (main, helper):
                        old = source.read_text()
                        source.write_text(old + "\n// ordinary edit\n")
                        assert (await check())["passed"] and probe_stamp() == stamp
                        source.write_text(old)
                        assert (await check())["passed"]
                    grammar.write_text(grammar_text.replace("'hello'", "'world'"))
                    failed = await check()
                    assert failed.get("failed_count") == 1, json.dumps(failed)
                    assert probe_stamp() != stamp
                    assert full_builds() == full_count
                    grammar.write_text(grammar_text)
                    assert (await check())["passed"]
                    grammar.write_text("this is not a grammar")
                    bad = await check()
                    assert bad.get("ok") is False, bad
                    grammar.write_text(grammar_text)
                    assert (await check())["passed"]
                    shutil.rmtree(grammar_module / "target/grammar-output")
                    assert (await check())["passed"]
                    assert full_builds() == full_count
                    # Application launch uses the same preparation mechanism.
                    state = dict(
                        (
                            await session.call_tool(
                                "java_application",
                                {
                                    "action": "launch",
                                    "project_path": str(project),
                                    "launch_name": "App",
                                    "jdwp_port": debug,
                                    "ready_port": port,
                                    "startup_wait_timeout_seconds": 10,
                                },
                            )
                        ).structuredContent
                        or {}
                    )
                    try:
                        with anyio.fail_after(100):
                            while state.get("launch_phase") != "runtime_active":
                                assert state.get("launch_phase") != "failed", (
                                    json.dumps(state)
                                )
                                await anyio.sleep(0.05)
                                state = dict(
                                    (
                                        await session.call_tool(
                                            "java_status", {"action": "status"}
                                        )
                                    ).structuredContent
                                    or {}
                                )
                        with socket.create_connection(
                            ("127.0.0.1", port), timeout=5
                        ) as connection:
                            assert connection.recv(20) == b"true"
                    finally:
                        await session.call_tool("java_application", {"action": "stop"})
        assert not (grammar_module / "target/classes").exists()
        assert not (app / "target/test-classes").exists()

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_template_generator_uses_native_goal_and_xml_file_inputs(tmp_path):
    env = environment(tmp_path)
    project = tmp_path / "template"
    source = project / "definitions/Generated.java.template"
    source.parent.mkdir(parents=True)
    source.write_text(
        "package generated; public class Generated {public static int get(){return 1;}}"
    )
    test = project / "src/test/java/example/GeneratedTest.java"
    test.parent.mkdir(parents=True)
    test.write_text(
        "package example; public class GeneratedTest {@org.junit.Test public void value(){org.junit.Assert.assertEquals(1,generated.Generated.get());}}"
    )
    (project / "pom.xml").write_text("""<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>template</artifactId><version>1</version>
<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><artifactId>maven-antrun-plugin</artifactId><version>3.1.0</version><executions>
<execution><id>template</id><phase>generate-sources</phase><goals><goal>run</goal></goals><configuration><target>
<copy file="${project.basedir}/definitions/Generated.java.template" tofile="${project.build.directory}/template-java/generated/Generated.java" overwrite="true"/>
<echo file="${project.build.directory}/generation-count" append="true">run</echo>
</target></configuration></execution>
<execution><id>not-a-generator</id><phase>compile</phase><goals><goal>run</goal></goals><configuration><target><fail message="compile must not execute"/></target></configuration></execution>
</executions></plugin>
<plugin><groupId>org.codehaus.mojo</groupId><artifactId>build-helper-maven-plugin</artifactId><version>3.5.0</version>
<executions><execution><id>source</id><phase>generate-sources</phase><goals><goal>add-source</goal></goals><configuration><sources><source>${project.build.directory}/template-java</source></sources></configuration></execution></executions>
</plugin></plugins></build></project>""")

    async def scenario():
        for cycle in range(2):
            with temporary_stderr() as stderr:
                async with open_mcp_session(stderr, environment=env) as session:

                    async def check():
                        return await run(session, project, ["example.GeneratedTest"])

                    result = await check()
                    assert result.get("passed"), json.dumps(result)
                    count = (project / "target/generation-count").read_text()
                    if cycle:
                        assert result["compiled_source_count"] == 0
                        continue
                    assert (await check())["passed"]
                    assert (project / "target/generation-count").read_text() == count
                    original_test = test.read_text()
                    test.write_text(original_test + "\n// edit\n")
                    assert (await check())["passed"]
                    assert (project / "target/generation-count").read_text() == count
                    old = source.read_text()
                    source.write_text(old.replace("return 1", "return 2"))
                    assert (await check())["failed_count"] == 1
                    assert (project / "target/generation-count").read_text() != count
                    source.write_text(old)
                    assert (await check())["passed"]
        assert not (project / "target/classes").exists()

    anyio.run(scenario)
