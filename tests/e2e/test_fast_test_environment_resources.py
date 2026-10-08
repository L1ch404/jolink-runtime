"""Native resources and explicit toolchains through the public stdio MCP tools."""

import json
import os
import shutil
from pathlib import Path

import anyio
import pytest
from java_support import open_mcp_session, require_real_mcp_java_e2e, temporary_stderr
from test_build_configuration_mcp import project_fixture


async def finished(session, root, *, expect_pass=True, **options):
    response = await session.call_tool("java_fast_test", {
        "project_path": str(root), "tests": ["example.ReadyTest"], **options,
    })
    result = dict(response.structuredContent)
    with anyio.fail_after(180):
        while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
            await anyio.sleep(.1)
            result = dict((await session.call_tool("java_fast_test", {
                "action": "result", "test_run_id": result["test_run_id"],
            })).structuredContent)
    if result.get("next_action"):
        result = dict((await session.call_tool("java_fast_test", {
            "action": "result", "test_run_id": result["test_run_id"],
        })).structuredContent)
    assert result.get("passed") is expect_pass, json.dumps(result, ensure_ascii=False)
    return result


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("system", ["maven", "gradle"])
@pytest.mark.parametrize("multi", [False, True])
def test_processed_resources_cold_edit_delete_reopen(tmp_path, system, multi):
    require_real_mcp_java_e2e()
    root, app, config, source, _ = project_fixture(tmp_path, system, multi if system == "maven" else False)
    if system == "gradle" and multi:
        # Move the selected module below a root which contains no Java plugin.
        app = root / "app"
        app.mkdir()
        for name in ("src", "resourcesA", "resourcesB", "build.gradle"):
            (root / name).rename(app / name)
        config = app / "build.gradle"
        source = app / "src/main/java/example/App.java"
        (root / "settings.gradle").write_text("rootProject.name='resource-modules'; include 'app', 'lib'\n")
        lib = root / "lib"
        (lib / "src/main/resources").mkdir(parents=True)
        (lib / "src/main/resources/upstream.txt").write_text("from upstream")
        (lib / "build.gradle").write_text("plugins {id 'java-library'}; sourceCompatibility='8'; targetCompatibility='8'\n")
        config.write_text(config.read_text() + "dependencies { implementation project(':lib') }\n")
    test_resources = app / "test data"
    test_resources.mkdir()
    (test_resources / "same.txt").write_text("test")
    (app / "resourcesA/same.txt").write_text("main")
    resource = test_resources / "filtered.txt"
    resource.write_text("@label@")
    (test_resources / "omit.ignore").write_text("must not leak")
    (test_resources / "payload.bin").write_bytes(bytes([0, 128, 255, 1]))
    (test_resources / "fixture.class").write_bytes(b"class file used as test data")
    removable = test_resources / "gone.txt"
    removable.write_text("remove me")
    filter_file = app / "filters.properties"
    filter_file.write_text("label=A\n")
    secondary = app / "secondary resources"
    secondary.mkdir()
    (secondary / "extra.txt").write_text("secondary")
    native = app / ("target/test-output" if system == "maven" else "build/test-output")
    if system == "maven":
        config.write_text(config.read_text().replace("<build>", """<build>
<testOutputDirectory>${project.basedir}/target/test-output</testOutputDirectory>
<testResources><testResource><directory>test data</directory><filtering>true</filtering><excludes><exclude>**/*.ignore</exclude></excludes></testResource><testResource><directory>secondary resources</directory><targetPath>nested</targetPath><includes><include>*.txt</include></includes></testResource></testResources>
<filters><filter>${project.basedir}/filters.properties</filter></filters>
""").replace("<plugins>", """<plugins><plugin><artifactId>maven-resources-plugin</artifactId><version>3.3.1</version>
<configuration><nonFilteredFileExtensions><nonFilteredFileExtension>bin</nonFilteredFileExtension></nonFilteredFileExtensions></configuration></plugin>"""))
        if multi:
            (root / "lib/filters.properties").write_text("label=A\n")
    else:
        config.write_text(config.read_text() + """
sourceSets.test.resources.setSrcDirs(['test data'])
sourceSets.test.output.resourcesDir=file('build/test-output')
processTestResources {
 from('secondary resources') { into 'nested'; include '*.txt' }
 exclude '**/*.ignore'
 inputs.file('filters.properties')
 filesMatching('*.txt') {
   filter { line -> def p=new Properties(); file('filters.properties').withInputStream {p.load(it)}; line.replace('@label@',p.getProperty('label')) }
 }
}
tasks.withType(JavaCompile).configureEach { doFirst { throw new GradleException('native compile must not run') } }
test.doFirst { throw new GradleException('native test must not run') }
""")
    test = app / "src/test/java/example/ReadyTest.java"
    oracle = app / "expected.txt"
    test.write_text('''package example;
public class ReadyTest {
 static byte[] resource(String name) throws Exception {
  try(java.io.InputStream s=ReadyTest.class.getResourceAsStream("/"+name)) {
   org.junit.Assert.assertNotNull(name,s);
   java.io.ByteArrayOutputStream b=new java.io.ByteArrayOutputStream(); int c; while((c=s.read())!=-1)b.write(c); return b.toByteArray();
  }
 }
 @org.junit.Test public void resource() throws Exception {
  String expected=new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(ORACLE)),"UTF-8");
  org.junit.Assert.assertEquals(expected,new String(resource("filtered.txt"),"UTF-8"));
  org.junit.Assert.assertEquals(expected,new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(NATIVE,"filtered.txt")),"UTF-8"));
  org.junit.Assert.assertEquals("test",new String(resource("same.txt"),"UTF-8"));
  org.junit.Assert.assertEquals("secondary",new String(resource("nested/extra.txt"),"UTF-8"));
  org.junit.Assert.assertNull(ReadyTest.class.getResource("/omit.ignore"));
  org.junit.Assert.assertArrayEquals(new byte[]{0,(byte)128,(byte)255,1},resource("payload.bin"));
  org.junit.Assert.assertEquals("class file used as test data",new String(resource("fixture.class"),"UTF-8"));
  org.junit.Assert.assertEquals("A".equals(expected),ReadyTest.class.getResource("/gone.txt")!=null);
  org.junit.Assert.assertEquals("A".equals(expected),java.nio.file.Files.exists(java.nio.file.Paths.get(NATIVE,"gone.txt")));
  org.junit.Assert.assertNull(ReadyTest.class.getResource("/stale.class"));
 }
}'''.replace("ORACLE", json.dumps(oracle.as_posix())).replace("NATIVE", json.dumps(native.as_posix())))
    env = {**os.environ, "XDG_CACHE_HOME": str(tmp_path / "cache"), "MAVEN_ARGS": "--offline", "GRADLE_ARGS": "--offline", "JOLINK_LOG_LEVEL": "INFO"}
    prepared_resources = tmp_path / "prepared resources"
    if system == "maven":
        env["JAVA_TOOL_OPTIONS"] = f'-Djolink.probe.resourceCache="{prepared_resources.as_posix()}"'

    def full_builds():
        return sum("jdt.build.finished" in line and "actual=FULL" in line
                   for log in (tmp_path / "cache").rglob("mcp.log") for line in log.read_text().splitlines())

    async def scenario():
        oracle.write_text("A")
        assert not native.exists()
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                await finished(session, root)
                initial_full = full_builds()
                assert initial_full > 0
                # Native outputs can contain stale javac bytecode. It must not
                # enter the Runner when adding the prepared resource paths.
                if system == "maven":
                    (native / "stale.class").write_bytes(b"old class")
                warm = await finished(session, root)
                assert "freshness_ms" in warm and "bootstrap_ms" not in warm, warm
                assert warm["compiled_source_count"] == 0
                filter_file.unlink()
                oracle.write_text("B")
                removable.unlink()
                broken = await finished(session, root, expect_pass=False)
                assert broken["ok"] is False and broken["error_code"] == "FAST_TEST_BOOTSTRAP_FAILED", broken
                if system == "maven":
                    assert not list(prepared_resources.glob("resources-*"))
                    assert native.joinpath("filtered.txt").read_text() == "A"
                    assert native.joinpath("gone.txt").is_file()
                filter_file.write_text("label=B\n")
                changed = await finished(session, root)
                assert changed["compiled_source_count"] == 0, changed
                assert full_builds() == initial_full, "resource edits must not force JDT FULL"
                if system == "maven":
                    assert (native / "stale.class").read_bytes() == b"old class"
                    assert not list(prepared_resources.glob("resources-*"))
                source.write_text(source.read_text().replace("public class App", "/* Java-only edit */ public class App"))
                java_only = await finished(session, root)
                assert "bootstrap_ms" not in java_only, java_only
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                assert (await finished(session, root))["compiled_source_count"] == 0
                (native / "filtered.txt").unlink()
                await finished(session, root)
                assert native.joinpath("filtered.txt").read_text() == "B"
    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_two_gradle_resource_bootstraps_can_overlap(tmp_path):
    require_real_mcp_java_e2e()
    projects = []
    for name in ("first", "second"):
        folder = tmp_path / name
        folder.mkdir()
        root, *_ = project_fixture(folder, "gradle", False)
        projects.append(root)
    env = {**os.environ, "JAVA_HOME": os.environ.get("JOLINK_TEST_JAVA17_HOME", os.environ.get("JAVA_HOME", "")),
           "GRADLE_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache")}

    async def one(root):
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                await finished(session, root)
                warm = await finished(session, root)
                assert "bootstrap_ms" not in warm, warm

    async def scenario():
        async with anyio.create_task_group() as group:
            for root in projects:
                group.start_soon(one, root)
    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_explicit_java_maven_settings_override_process_defaults(tmp_path):
    require_real_mcp_java_e2e()
    home = os.environ.get("JOLINK_TEST_JAVA17_HOME")
    java8 = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not home or not java8:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME and JOLINK_TEST_JAVA17_HOME")
    root, _, pom, _, _ = project_fixture(tmp_path, "maven", False)
    shutil.rmtree(root / ".idea")
    pom.write_text(pom.read_text().replace("<maven.compiler.source>8<", "<maven.compiler.source>17<")
                  .replace("<maven.compiler.target>8<", "<maven.compiler.target>17<"))
    test = root / "src/test/java/example/ReadyTest.java"
    test.write_text('''package example; public class ReadyTest {
 @org.junit.Test public void selectedJava() {org.junit.Assert.assertEquals("17",System.getProperty("java.specification.version"));}
}''')
    settings = root / "settings with spaces.xml"
    settings.write_text("<settings><localRepository>" + str(Path.home() / ".m2/repository") + "</localRepository></settings>")
    maven = Path(shutil.which("mvn")).resolve().parent.parent
    env = {**os.environ, "JAVA_HOME": java8, "MAVEN_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache")}
    async def scenario():
        for _ in range(2):
            with temporary_stderr() as log:
                async with open_mcp_session(log, environment=env) as session:
                    opts = {"java_home": home, "maven_home": str(maven), "maven_settings": str(settings)}
                    await finished(session, root, **opts)
                    warm = await finished(session, root, **opts)
                    assert "bootstrap_ms" not in warm and warm["compiled_source_count"] == 0, warm
    anyio.run(scenario)
