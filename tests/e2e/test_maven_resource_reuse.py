"""Unchanged native resource preparation must not invalidate another consumer."""

import json
import os

import anyio
import pytest
from java_support import (
    open_mcp_session,
    require_real_mcp_java_e2e,
    reserve_local_port,
    temporary_stderr,
)
from test_build_configuration_mcp import project_fixture
from test_fast_test_environment_resources import finished
from test_review_model_cache import response


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("multi,first,overlap", [
    (False, "test", False), (True, "test", False),
    (False, "launch", False), (True, "launch", False),
    pytest.param(False, "test", True, id="ordered-overwrite"),
])
def test_launch_and_test_preserve_unchanged_resource_outputs(tmp_path, multi, first, overlap):
    require_real_mcp_java_e2e()
    root, app, _, _, port = project_fixture(tmp_path, "maven", multi)
    resource_cache = tmp_path / "prepared resources"
    env = {**os.environ, "MAVEN_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache"),
           "JAVA_TOOL_OPTIONS": f'-Djolink.probe.resourceCache="{resource_cache.as_posix()}"'}
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")
    oracle = app / "expected.txt"
    oracle.write_text("A")
    if overlap:
        oracle.write_text("B")
        extra = app / "override"
        extra.mkdir()
        (extra / "value.txt").write_text("B")
        pom = app / "pom.xml"
        pom.write_text(pom.read_text().replace("<plugins>", '''<plugins>
<plugin><artifactId>maven-resources-plugin</artifactId><version>3.3.1</version><executions><execution>
<id>override</id><phase>process-resources</phase><goals><goal>copy-resources</goal></goals><configuration>
<outputDirectory>${project.build.outputDirectory}</outputDirectory><overwrite>true</overwrite>
<resources><resource><directory>override</directory></resource></resources>
</configuration></execution></executions></plugin>'''))
    test = app / "src/test/java/example/ReadyTest.java"
    test.write_text('''package example; public class ReadyTest {
 @org.junit.Test public void resource() throws Exception {
  String expected=new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(ORACLE)),"UTF-8");
  org.junit.Assert.assertEquals(expected,App.value());
  org.junit.Assert.assertEquals(expected,new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(NATIVE)),"UTF-8"));
 }
}'''.replace("ORACLE", json.dumps(oracle.as_posix())).replace("NATIVE", json.dumps((app / "target/classes/value.txt").as_posix())))
    # Include binary and legitimately named .class resources as well.
    binary = app / "resourcesA/payload.bin"
    binary.write_bytes(bytes(range(256)) * 65 + b"tail")
    (app / "resourcesA/empty.dat").touch()
    (app / "resourcesA/data.class").write_bytes(b"resource, not compiled Java")

    def outputs():
        files = list(resource_cache.rglob("value.txt")) + list(resource_cache.rglob("payload.bin"))
        files += list(resource_cache.rglob("data.class"))
        files += list(resource_cache.rglob("empty.dat"))
        files += list((app / "target/classes").glob("*"))
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in files if p.is_file()}

    async def scenario():
        baseline = None
        for cycle in range(2):
            with temporary_stderr() as log:
                async with open_mcp_session(log, environment=env) as session:
                    async def call(tool, **args):
                        return dict((await session.call_tool(tool, args)).structuredContent)

                    async def launch():
                        value = await call("java_application", action="launch", project_path=str(root),
                                           main_class="example.App", ready_port=port, jdwp_port=reserve_local_port())
                        with anyio.fail_after(120):
                            while value.get("launch_phase") not in {"runtime_active", "failed"} and value.get("ok", True):
                                await anyio.sleep(.2)
                                value = await call("java_status", action="status", details=True)
                        assert value.get("startup_state") == "ready", json.dumps(value)
                        assert response(port) == oracle.read_text()
                        return value

                    try:
                        if first == "test":
                            await finished(session, root)
                        else:
                            await launch()
                        before = outputs()
                        assert before
                        if cycle:
                            assert before == baseline, "Reopening must not rewrite resources"
                        if first == "test":
                            await launch()
                        else:
                            await finished(session, root)
                        after = outputs()
                        if overlap and after != before:
                            assert after.keys() == before.keys()
                            assert all(after[p][0] == before[p][0] for p in before), "Content must still be correct"
                            return "Ordered resource goals still rewrite identical final output"
                        assert after == before, "A cold second consumer changed identical resource files"
                        for _ in range(3):
                            warm = await finished(session, root)
                            assert "bootstrap_ms" not in warm and warm["compiled_source_count"] == 0, warm
                            await call("java_application", action="stop")
                            assert (await launch()).get("probe_cache_reused") is True
                            assert outputs() == before
                        if not cycle:
                            # Source timestamp changes legitimately cause preparation,
                            # but identical outputs must keep their own timestamps.
                            binary.write_bytes(binary.read_bytes())
                            await finished(session, root)
                            await call("java_application", action="stop")
                            await launch()
                            assert outputs() == before
                            # Real content change, including same-size replacement,
                            # must still reach native output and the running program.
                            (app / "resourcesA/value.txt").write_text("B")
                            binary.write_bytes(binary.read_bytes()[:-1] + b"!")
                            oracle.write_text("B")
                            await finished(session, root)
                            await call("java_application", action="stop")
                            await launch()
                            assert response(port) == "B"
                            changed = outputs()
                            for path, old in before.items():
                                if path.endswith("payload.bin"):
                                    assert changed[path][0] == old[0][:-1] + b"!"
                                elif not path.endswith("value.txt"):
                                    assert changed[path] == old
                            assert "bootstrap_ms" not in await finished(session, root)
                        baseline = outputs()
                        assert not list(resource_cache.glob("resources-*")), "Preparation scratch directories must be removed"
                    finally:
                        await call("java_application", action="stop")
    known_issue = anyio.run(scenario)
    if known_issue:
        pytest.xfail(known_issue)
