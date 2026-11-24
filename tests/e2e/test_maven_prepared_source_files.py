"""A source generator may change runtime behavior, never the developer's checkout."""
import os
from pathlib import Path
import shutil
from urllib.request import urlopen

import anyio
import pytest

from java_support import call_workflow, open_mcp_session, require_real_mcp_java_e2e, reserve_local_port


@pytest.mark.mcp_java_e2e
def test_maven_named_source_is_generated_privately_even_on_failure(tmp_path):
    require_real_mcp_java_e2e()
    home = os.getenv("JOLINK_TEST_JAVA8_HOME")
    if not home or not shutil.which("mvn"):
        pytest.skip("requires Maven and a JDK 8")
    project = tmp_path / "project"
    def write(name, contents):
        path = project / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(contents, encoding="utf-8")
        return path
    write("pom.xml", """<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>source-output</artifactId><version>1</version>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><groupId>com.google.code.maven-replacer-plugin</groupId><artifactId>replacer</artifactId>
<version>1.5.3</version><executions><execution><phase>process-sources</phase><goals><goal>replace</goal></goals>
</execution></executions><configuration><file>${project.basedir}/input.txt</file>
<outputFile>${project.build.sourceDirectory}/example/Value.java</outputFile><token>@word@</token><value>A</value></configuration></plugin>
<plugin><artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version><configuration>
<source>8</source><target>8</target><compilerVersion>1.8</compilerVersion><fork>true</fork></configuration></plugin>
<plugin><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version></plugin></plugins></build></project>""")
    template = 'package example; public class Value { public static String value(){ return "@word@"; } }'
    original = write("src/main/java/example/Value.java", "// unsaved-to-git developer work; not valid input for javac\n")
    contents, stamp = original.read_bytes(), original.stat().st_mtime_ns
    generated = write("input.txt", template)
    write("src/main/java/example/App.java", """package example;
public class App { public static void main(String[] args) throws Exception {
com.sun.net.httpserver.HttpServer s=com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1",Integer.parseInt(args[0])),0);
s.createContext("/",e->{byte[] b=Value.value().getBytes("UTF-8"); e.sendResponseHeaders(200,b.length); e.getResponseBody().write(b); e.close();});s.start();} }""")
    test = write("src/test/java/example/ValueTest.java", """package example; public class ValueTest {
@org.junit.Test public void generatedValue(){ org.junit.Assert.assertEquals("A", Value.value()); } }""")
    port = reserve_local_port()
    common = dict(project_path=str(project), java_home=home, build_java_home=home, timeout=0)
    if os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
        common["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]
    env = {**os.environ,"JAVA_HOME":home,"PATH":home+"/bin"+os.pathsep+os.environ["PATH"],
           "XDG_CACHE_HOME":str(tmp_path/"cache"),"MAVEN_ARGS":"--offline"}
    if os.name=="nt":
        env["LOCALAPPDATA"]=env["XDG_CACHE_HOME"]
    def http():
        with urlopen(f"http://127.0.0.1:{port}",timeout=5) as r:
            return r.read().decode()
    def untouched():
        assert original.read_bytes() == contents and original.stat().st_mtime_ns == stamp
    async def scenario():
        for reopened in (False,True):
            with (tmp_path/f"mcp-{reopened}.log").open("w") as log:
                async with open_mcp_session(log,environment=env) as session:
                    async def run():
                        r = await call_workflow(session,"java_fast_test",dict(common,tests=["example.ValueTest"]), tmp_path / "calls.jsonl")
                        untouched()
                        return r
                    try:
                        value = await run()
                        assert value.get("passed") and value["tests"]==1, value
                        if reopened:
                            assert value["compiled_source_count"]==0
                            continue
                        starting=await call_workflow(session,"java_application",dict(common,action="launch",main_class="example.App",
                              ready_port=port,jdwp_port=reserve_local_port(),app_args=[str(port)]))
                        assert starting.get("startup_state")=="ready" and http()=="A",starting
                        untouched()
                        generated.write_text("not java",encoding="utf-8")
                        assert (await run()).get("error_code")=="MAVEN_NATIVE_COMPILE_FAILED"
                        assert http()=="A"
                        generated.write_text(template.replace("@word@", "B"),encoding="utf-8")
                        assert (await run()).get("failed_count")==1
                        test.write_text(test.read_text().replace('"A"', '"B"'),encoding="utf-8")
                        assert (await run()).get("passed") and http()=="A"
                        changed=await call_workflow(session,"java_application",dict(action="restart",timeout=0))
                        assert changed.get("ok") and http()=="B",changed
                        untouched()
                    finally:
                        await session.call_tool("java_application",dict(action="stop"))
    anyio.run(scenario)
