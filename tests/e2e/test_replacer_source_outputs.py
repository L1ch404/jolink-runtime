"""Replacer scan and combined output paths must keep developer sources intact."""
import os
from pathlib import Path
import shutil
import subprocess
from urllib.request import urlopen

import anyio
import pytest

from java_support import call_workflow, open_mcp_session, require_real_mcp_java_e2e, reserve_local_port


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("mode", ["scan", "combined-output"])
def test_replacer_preserves_dirty_sources_and_compiles_prepared_bytes(tmp_path, mode):
    require_real_mcp_java_e2e()
    home = os.getenv("JOLINK_TEST_JAVA8_HOME")
    mvn = shutil.which("mvn")
    if not home or not mvn:
        pytest.skip("requires Maven and JDK 8")
    project = tmp_path / "project"

    def write(rel, contents):
        p = project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(contents, encoding="utf-8")
        return p

    def execution(name, routes):
        return (f"<execution><id>{name}</id><phase>process-sources</phase><goals><goal>replace</goal></goals>"
                f"<configuration>{routes}<token>@tag@</token><value>B</value></configuration></execution>")

    source = "${project.build.sourceDirectory}"
    if mode == "scan":
        prepare = execution("bulk", f"<basedir>{source}</basedir><includes><include>**/Value*.java</include></includes>"
                            "<excludes><exclude>**/ValueExcluded.java</exclude></excludes>")
    else:
        prepare = "".join(execution("prepare-" + name, f"<file>{source}/example/{name}.java</file>"
                                   f"<outputFile>{name}.java</outputFile><outputDir>{source}/example</outputDir>")
                          for name in ("ValueOne", "ValueTwo"))
    prepare += execution("target-only", "<file>${project.basedir}/note.txt</file>"
                         "<outputFile>${project.build.directory}/marker.txt</outputFile>")
    write("pom.xml", """<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>source-check</artifactId><version>1</version>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><groupId>com.google.code.maven-replacer-plugin</groupId><artifactId>replacer</artifactId><version>1.5.3</version>
<executions>""" + prepare + """</executions></plugin><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version>
<configuration><source>8</source><target>8</target><fork>true</fork></configuration></plugin>
<plugin><artifactId>maven-resources-plugin</artifactId><version>3.3.0</version></plugin>
<plugin><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version></plugin></plugins></build></project>""")
    write("note.txt", "ordinary-target-@tag@")
    originals = {}
    for name, suffix in (("ValueOne", "1"), ("ValueTwo", "2"), ("ValueExcluded", "3")):
        f = write(f"src/main/java/example/{name}.java", f"""package example;
// Local developer edit not committed to source control.
public class {name} {{ public static String get() {{ return "@tag@{suffix}"; }} }}""")
        originals[f] = f.read_bytes(), f.stat().st_mtime_ns
    app = write("src/main/java/example/App.java", """package example;
public class App { static String value(){return ValueOne.get()+":"+ValueTwo.get()+":"+ValueExcluded.get();}
public static void main(String[] args) throws Exception {
com.sun.net.httpserver.HttpServer s=com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1",Integer.parseInt(args[0])),0);
s.createContext("/",e->{byte[] b=value().getBytes("UTF-8"); e.sendResponseHeaders(200,b.length); e.getResponseBody().write(b); e.close();});s.start();} }""")
    test = write("src/test/java/example/ValueTest.java", """package example; public class ValueTest {
@org.junit.Test public void preparedValue(){ org.junit.Assert.assertEquals("B1:B2:@tag@3", App.value()); } }""")
    originals[app] = app.read_bytes(), app.stat().st_mtime_ns
    env = {**os.environ, "JAVA_HOME": home, "PATH": str(Path(home) / "bin") + os.pathsep + os.environ["PATH"],
           "MAVEN_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache")}
    if os.name == "nt":
        env["LOCALAPPDATA"] = env["XDG_CACHE_HOME"]
    settings = os.getenv("JOLINK_TEST_MAVEN_SETTINGS")
    # Native control intentionally allowed to rewrite only its disposable copy.
    native = tmp_path / "native"
    shutil.copytree(project, native)
    proc = subprocess.run([mvn, "-B", "-o", *(["-s", settings] if settings else []), "-Dtest=ValueTest", "test"],
                          cwd=native, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0 and "Tests run: 1" in proc.stdout, proc.stdout + proc.stderr
    assert (native / "target/marker.txt").read_text() == "ordinary-target-B"
    port = reserve_local_port()
    common = dict(project_path=str(project), java_home=home, build_java_home=home, timeout=0)
    if settings:
        common["maven_settings"] = settings

    def unchanged():
        for path, (data, stamp) in originals.items():
            assert path.read_bytes() == data and path.stat().st_mtime_ns == stamp, path
        assert (project / "target/marker.txt").read_text() == "ordinary-target-B"

    def http():
        with urlopen(f"http://127.0.0.1:{port}", timeout=5) as r:
            return r.read().decode()

    async def scenario():
        with (tmp_path / "mcp.log").open("w") as log:
            async with open_mcp_session(log, environment=env) as session:
                async def run():
                    result = await call_workflow(session, "java_fast_test", dict(common, tests=["example.ValueTest"]))
                    unchanged()
                    return result
                try:
                    r = await run()
                    assert r.get("passed") and r["tests"] == 1, r
                    started = await call_workflow(session, "java_application", dict(common, action="launch", main_class="example.App",
                                                  ready_port=port, jdwp_port=reserve_local_port(), app_args=[str(port)]))
                    assert started.get("startup_state") == "ready" and http() == "B1:B2:@tag@3", started
                    unchanged()
                    changed_file = project / "src/main/java/example/ValueOne.java"
                    changed_file.write_text(changed_file.read_text().replace("@tag@1", "@tag@new"), encoding="utf-8")
                    originals[changed_file] = changed_file.read_bytes(), changed_file.stat().st_mtime_ns
                    r = await run()
                    assert r.get("failed_count") == 1, r
                    test.write_text(test.read_text().replace("B1:", "Bnew:"), encoding="utf-8")
                    r = await run()
                    assert r.get("passed") and http() == "B1:B2:@tag@3", r
                    updated = await call_workflow(session, "java_application", dict(action="restart", timeout=0))
                    assert updated.get("ok") and http() == "Bnew:B2:@tag@3", updated
                    unchanged()
                finally:
                    await session.call_tool("java_application", dict(action="stop"))
    anyio.run(scenario)
