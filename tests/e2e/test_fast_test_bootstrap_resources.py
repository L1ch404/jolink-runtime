"""Real MCP: resource filters retain the live compiler; bootstrap work is reported."""

import json
import os
from pathlib import Path

import anyio
import pytest

from java_support import open_mcp_session, require_real_mcp_java_e2e, temporary_stderr


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def edit_main(root, version):
    for i in range(5):
        write(root / f"src/main/java/example/Value{i}.java",
              f'package example; public class Value{i} {{ public static String value() {{ return "{version}"; }} }}')
    write(root / "expected.txt", ":".join([version] * 5))


def fixture(root, resources_version):
    edit_main(root, "v1")
    write(root / "pom.xml", f'''<project xmlns="http://maven.apache.org/POM/4.0.0">
<modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>resource-caching</artifactId><version>1</version>
<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build>
 <resources><resource><directory>src/main/java</directory><includes><include>**/*.xml</include></includes><excludes><exclude>**/ignored/**</exclude></excludes></resource></resources>
 <testResources><testResource><directory>src/test/java</directory><includes><include>**/*.xml</include></includes><excludes><exclude>**/ignored/**</exclude></excludes></testResource></testResources>
 <plugins><plugin><artifactId>maven-resources-plugin</artifactId><version>{resources_version}</version>
 <executions><execution><id>extra</id><phase>process-resources</phase><goals><goal>copy-resources</goal></goals><configuration>
 <outputDirectory>${{project.build.outputDirectory}}</outputDirectory><resources><resource><directory>extra resources 中文</directory>
 <includes><include>**/*.xml</include></includes><excludes><exclude>**/ignored/**</exclude></excludes></resource></resources>
 </configuration></execution></executions></plugin></plugins>
</build></project>''')
    for scope, name in (("src/main/java", "main.xml"), ("src/test/java", "test.xml"), ("extra resources 中文", "copy.xml")):
        write(root / scope / name, "<v1/>")
        write(root / f"expected-{name}.txt", "<v1/>")
    values = ' + ":" + '.join(f"Value{i}.value()" for i in range(5))
    write(root / "src/test/java/example/ValuesTest.java", '''package example;
import java.nio.file.*; import java.io.*; import org.junit.Test; import static org.junit.Assert.*;
public class ValuesTest {
 static String expected(String name) throws Exception {return new String(Files.readAllBytes(Paths.get(name)),"UTF-8");}
 @Test public void latest() throws Exception {
  String actual=VALUES;
  System.out.println("ACTUAL="+actual); assertEquals(expected("expected.txt"),actual);
  for(String name:new String[]{"main.xml","test.xml","copy.xml"}) {
   String wanted=expected("expected-"+name+".txt");
   try(InputStream in=getClass().getResourceAsStream("/"+name)) {
    if(wanted.equals("MISSING")){assertNull(name,in);continue;}
    assertNotNull(name,in); ByteArrayOutputStream bytes=new ByteArrayOutputStream();
    int b; while((b=in.read())!=-1)bytes.write(b);
    assertEquals(wanted,new String(bytes.toByteArray(),"UTF-8"));
   }
  }
  assertNull(getClass().getResource("/ignored/ignored.xml"));
  if(Files.exists(Paths.get("defaults-off"))) assertNotNull(getClass().getResource("/.git/ignored.xml"));
  else assertNull(getClass().getResource("/.git/ignored.xml"));
 }
}'''.replace("VALUES", values))


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("resources_version", ["2.6", "3.3.1"])
def test_resource_exclusions_and_bootstrap_compile_counts_through_mcp(tmp_path, resources_version):
    require_real_mcp_java_e2e()
    java8 = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not java8:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    root = tmp_path / "project"
    fixture(root, resources_version)
    env = {**os.environ, "MAVEN_ARGS": "--offline", "JOLINK_LOG_LEVEL": "INFO",
           "XDG_CACHE_HOME": str(tmp_path / "cache")}
    options = {"project_path": str(root), "tests": ["example.ValuesTest"],
               "build_java_home": java8, "java_home": java8, "timeout": 30}

    async def run(session, *, expect_pass=True, **extra):
        result = dict((await session.call_tool("java_fast_test", {**options, **extra})).structuredContent)
        with anyio.fail_after(180):
            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                await anyio.sleep(.25)
                result = dict((await session.call_tool("java_fast_test", {
                    "action": "result", "test_run_id": result["test_run_id"],
                })).structuredContent)
        if result.get("passed") is not True:
            result = dict((await session.call_tool("java_fast_test", {
                "action": "result", "test_run_id": result["test_run_id"],
            })).structuredContent)
        assert result.get("passed") is expect_pass, json.dumps(result, ensure_ascii=True)
        return result

    async def scenario():
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                cold = await run(session)
                assert cold["compiled_source_count"] == 6, cold
                assert cold["compile_ms"] > 0 and cold["bootstrap_ms"] > 0, cold
                snapshots = list((tmp_path / "cache/jolink-runtime/fast-test").glob("*/build-world.json"))
                saved = json.loads(snapshots[0].read_text(encoding="utf-8"))
                assert len(saved["preparation_resource_sets"]) == 3
                assert all(not key.endswith(".java") for key in saved["preparation_stamps"])
                # Add/edit/delete files excluded by Maven. No Java, XML selection,
                # configuration or invocation changes in these rounds.
                ignored = [root / scope / name
                           for scope in ("src/main/java", "src/test/java", "extra resources 中文")
                           for name in ("debug.py", "result.json", "ignored/ignored.xml", ".git/ignored.xml")]
                for change in ("added", "edited", None):
                    for path in ignored:
                        if change is None:
                            path.unlink()
                        else:
                            write(path, change)
                    warm = await run(session)
                    assert "bootstrap_ms" not in warm and warm["compiled_source_count"] == 0, warm
                edit_main(root, "v2")
                changed = await run(session)
                assert "bootstrap_ms" not in changed and changed["compiled_source_count"] == 5, changed
                assert changed["compile_ms"] > 0
                # Edit, delete, and add matching resources in every supported scope.
                for value in ("<changed/>", "MISSING", "<restored/>"):
                    for scope, name in (("src/main/java", "main.xml"), ("src/test/java", "test.xml"), ("extra resources 中文", "copy.xml")):
                        if value == "MISSING":
                            (root / scope / name).unlink()
                        else:
                            write(root / scope / name, value)
                        write(root / f"expected-{name}.txt", value)
                    refreshed = await run(session)
                    assert "bootstrap_ms" in refreshed, refreshed
                    assert refreshed["compiled_source_count"] == 0, refreshed
                    warm = await run(session)
                    assert "bootstrap_ms" not in warm, warm
                if resources_version == "3.3.1":
                    pom = root / "pom.xml"
                    original = pom.read_text(encoding="utf-8")
                    write(pom, original.replace("<executions>",
                          "<configuration><addDefaultExcludes>false</addDefaultExcludes></configuration><executions>"))
                    write(root / "extra resources 中文/.git/ignored.xml", "included by Maven")
                    write(root / "defaults-off", "")
                    await run(session)
                    write(root / "extra resources 中文/.git/ignored.xml", "changed selected resource")
                    refreshed = await run(session)
                    assert "bootstrap_ms" in refreshed
                    write(pom, original)
                    (root / "defaults-off").unlink()
                    await run(session)
        # A new MCP process must preserve selection metadata and report the
        # incremental compilation performed while reopening its saved workspace.
        edit_main(root, "v3")
        hints = [f"src/main/java/example/Value{i}.java" for i in range(5)]
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                reopened = await run(session, source_files=hints)
                assert reopened["compiled_source_count"] == 5, reopened
                assert reopened["compile_ms"] > 0 and "bootstrap_ms" in reopened, reopened
                warm = await run(session)
                assert warm["compiled_source_count"] == 0 and "compile_ms" not in warm
                assert "bootstrap_ms" not in warm
        broken = root / "src/main/java/example/Value0.java"
        write(broken, 'package example; public class Value0 { public static String value(){ return missingName; } }')
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                failed = await run(session, expect_pass=False)
                assert failed["ok"] is False and failed["error_count"] > 0, failed
                assert failed["compiled_source_count"] > 0 and failed["compile_ms"] > 0, failed
                edit_main(root, "v3")
                recovered = await run(session)
                assert recovered["compiled_source_count"] > 0, recovered
    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_launch_cache_uses_same_filtered_resource_inputs(tmp_path):
    import http.client
    from java_support import reserve_local_port
    from test_build_configuration_mcp import project_fixture

    require_real_mcp_java_e2e()
    java8 = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not java8:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    root, _, pom, source, port = project_fixture(tmp_path, "maven", False)
    write(pom, pom.read_text(encoding="utf-8").replace(
        "<resource.folder>resourcesA</resource.folder>", "<resource.folder>src/main/java</resource.folder>").replace(
        "<directory>${resource.folder}</directory>",
        "<directory>${resource.folder}</directory><includes><include>**/*.txt</include></includes>"))
    resource = root / "src/main/java/value.txt"
    write(resource, "A")
    env = {**os.environ, "JAVA_HOME": java8, "MAVEN_ARGS": "--offline",
           "PATH": str(Path(java8) / "bin") + os.pathsep + os.environ["PATH"],
           "XDG_CACHE_HOME": str(tmp_path / "cache")}

    async def scenario():
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                async def launch(expected, reused):
                    result = dict((await session.call_tool("java_application", {
                        "action": "launch", "project_path": str(root), "main_class": "example.App",
                        "ready_port": port, "jdwp_port": reserve_local_port(), "timeout": 30,
                    })).structuredContent)
                    with anyio.fail_after(120):
                        while result.get("launch_phase") not in {"runtime_active", "failed"}:
                            await anyio.sleep(.25)
                            result = dict((await session.call_tool("java_status", {
                                "action": "status", "details": True,
                            })).structuredContent)
                    assert result.get("launch_phase") == "runtime_active", result
                    assert result["probe_cache_reused"] is reused, result
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    try:
                        connection.request("GET", "/")
                        response = connection.getresponse()
                        assert response.status == 200 and response.read().decode() == expected
                    finally:
                        connection.close()
                    stopped = await session.call_tool("java_application", {"action": "stop"})
                    assert stopped.structuredContent.get("ok"), stopped

                await launch("A", False)
                write(source, source.read_text(encoding="utf-8").replace(
                    "Character.toString((char)s.read())", 'Character.toString((char)s.read())+"!"'))
                write(root / "src/main/java/debug.json", "excluded")
                await launch("A!", True)
                write(resource, "B")
                await launch("B!", False)
                await launch("B!", True)
    anyio.run(scenario)
