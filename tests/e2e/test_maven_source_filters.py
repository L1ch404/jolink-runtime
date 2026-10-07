"""Real MCP: original-path source selection across build, reload and persistence."""

import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET

import anyio
import pytest
from java_support import (
    open_mcp_session,
    require_real_mcp_java_e2e,
    reserve_local_port,
    temporary_stderr,
)
from test_build_configuration_mcp import project_fixture
from test_review_model_cache import response


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("placement,packaging,multi", [
    ("shared", "jar", False), ("execution", "jar", False),
    ("profile", "war", False), ("shared", "war", True),
])
def test_maven_source_patterns_full_incremental_and_reopen(tmp_path, placement, packaging, multi):
    require_real_mcp_java_e2e()
    root, app, pom, app_source, port = project_fixture(tmp_path, "maven", multi)
    main = app / "src/main/java"
    tests = app / "src/test/java"
    xml = ET.parse(pom)
    project = xml.getroot()
    compiler = next(p for p in project.findall("./{*}build/{*}plugins/{*}plugin") if p.findtext("{*}artifactId") == "maven-compiler-plugin")
    if placement == "profile":
        profile = ET.SubElement(ET.SubElement(project, "profiles"), "profile")
        ET.SubElement(profile, "id").text = "source-selection"
        ET.SubElement(ET.SubElement(profile, "activation"), "activeByDefault").text = "true"
        compiler = ET.SubElement(ET.SubElement(ET.SubElement(profile, "build"), "plugins"), "plugin")
        ET.SubElement(compiler, "artifactId").text = "maven-compiler-plugin"
    if placement == "execution":
        executions = ET.SubElement(compiler, "executions")
        scopes = []
        for goal in ("compile", "testCompile"):
            execution = ET.SubElement(executions, "execution")
            ET.SubElement(execution, "id").text = "default-" + goal
            ET.SubElement(ET.SubElement(execution, "goals"), "goal").text = goal
            scopes.append(ET.SubElement(execution, "configuration"))
    else:
        configuration = compiler.find("{*}configuration")
        if configuration is None:
            configuration = ET.SubElement(compiler, "configuration")
        scopes = [configuration, configuration]
    properties = project.find("{*}properties")
    ET.SubElement(properties, "selected.sources").text = "example/**"
    ET.SubElement(ET.SubElement(scopes[0], "includes"), "include").text = "${selected.sources}"
    exclude = ET.SubElement(ET.SubElement(scopes[0], "excludes"), "exclude")
    exclude.text = "example/ignore/**"
    ET.SubElement(ET.SubElement(scopes[1], "testIncludes"), "testInclude").text = "example/**"
    ET.SubElement(ET.SubElement(scopes[1], "testExcludes"), "testExclude").text = "example/skip/**"
    xml.write(pom, encoding="utf-8")
    if packaging != "jar":
        module_pom = app / "pom.xml"
        module_xml = ET.parse(module_pom)
        ET.SubElement(module_xml.getroot(), "packaging").text = packaging
        module_xml.write(module_pom, encoding="utf-8")
        if app == root:
            xml = module_xml
            # Use the same mutable exclusion after parsing the packaging edit.
            exclude = next(item for item in xml.getroot().iter("exclude") if item.text == "example/ignore/**")

    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path
    hidden = write(main / "example/ignore/Hidden.java", "not valid Java")
    write(main / "outside/NotIncluded.java", "not valid Java")
    skipped_test = write(tests / "example/skip/BrokenTest.java", "not valid Java")
    write(tests / "example/ignore/TestOnly.java", "package example.ignore; public class TestOnly {public static int value(){return 5;}}")
    # Physical layout differs from declared package; selection must happen first.
    helper = write(main / "example/unusual/Helper.java", 'package example; public class Helper {public static String value(){return "v1";}}')
    marker = app / "hidden.expected"
    expected = app / "version.expected"
    expected.write_text("v1")
    test = tests / "example/ReadyTest.java"
    test.write_text('''package example; public class ReadyTest {
 @org.junit.Test public void check() throws Exception {
  org.junit.Assert.assertEquals(5,example.ignore.TestOnly.value());
  org.junit.Assert.assertEquals(new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(EXPECTED)),"UTF-8"),Helper.value());
  boolean hidden; try { Class.forName("example.Hidden"); hidden=true; } catch(ClassNotFoundException e) { hidden=false; }
  org.junit.Assert.assertEquals(java.nio.file.Files.exists(java.nio.file.Paths.get(MARKER)),hidden);
 }
}'''.replace("EXPECTED", json.dumps(expected.as_posix())).replace("MARKER", json.dumps(marker.as_posix())))
    app_source.write_text(app_source.read_text().replace('byte[] b=value().getBytes("UTF-8")', 'byte[] b=(value()+"|"+Helper.value()).getBytes("UTF-8")'))
    env = {**os.environ, "MAVEN_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache"), "JOLINK_LOG_LEVEL": "INFO"}
    # Native Maven first: same source set and filters, no project mutation to pass joLink.
    native = subprocess.run(["mvn", "--offline", "-B", "-f", str(pom), "test"], env=env, capture_output=True, text=True, check=False)
    assert native.returncode == 0, native.stdout + native.stderr

    async def scenario():
        for cycle in range(2):
            with temporary_stderr() as log:
                async with open_mcp_session(log, environment=env) as session:
                    async def call(tool, **args):
                        return dict((await session.call_tool(tool, args)).structuredContent)

                    async def run(**args):
                        result = await call("java_fast_test", project_path=str(root), tests=["example.ReadyTest"], **args)
                        with anyio.fail_after(180):
                            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                                await anyio.sleep(.2)
                                result = await call("java_fast_test", action="result", test_run_id=result["test_run_id"])
                        if not result.get("passed"):
                            result = await call("java_fast_test", action="result", test_run_id=result["test_run_id"])
                        assert result.get("passed") is True, json.dumps(result)
                        return result

                    async def launch():
                        result = await call("java_application", action="launch", project_path=str(root), main_class="example.App",
                                            ready_port=port, jdwp_port=reserve_local_port())
                        with anyio.fail_after(180):
                            while result.get("launch_phase") not in {"runtime_active", "failed"} and result.get("ok", True):
                                await anyio.sleep(.2)
                                result = await call("java_status", action="status", details=True)
                        assert result.get("startup_state") == "ready", result
                        return result

                    try:
                        await run()
                        assert (await run())["compiled_source_count"] == 0
                        await launch()
                        assert response(port) == "A|" + expected.read_text()
                        # A cold launch also prepares shared native resources;
                        # establish the Test baseline after that preparation.
                        await run()
                        if not cycle:
                            hidden.write_bytes(b"\xff even invalid encoding is excluded")
                            skipped_test.write_text("still invalid")
                            ignored = await run(source_files=[str(hidden.relative_to(root)), str(skipped_test.relative_to(root))])
                            assert ignored["compiled_source_count"] == 0 and "bootstrap_ms" not in ignored, ignored
                            added = write(main / "example/new/Added.java", "package example; public class Added {}")
                            helper.write_text(helper.read_text().replace("v1", "v2"))
                            expected.write_text("v2")
                            incremental = await run()
                            assert "bootstrap_ms" not in incremental and incremental["compiled_source_count"] >= 1, incremental
                            added.unlink()
                            await run()
                            result = await call("java_application", action="restart", hotswap=False)
                            with anyio.fail_after(180):
                                while result.get("applied") is None and result.get("ok", True):
                                    await anyio.sleep(.2)
                                    status = await call("java_status", action="status", details=True)
                                    result = status.get("last_reload") or result
                            assert result.get("applied") is True, result
                            assert response(port) == "A|v2"
                            await call("java_application", action="stop")
                            # Change the source set, then restore it. A previously
                            # compiled Hidden.class must not leak from another workspace.
                            hidden.write_text("package example; public class Hidden {}")
                            marker.touch()
                            exclude.text = "does-not-match/**"
                            xml.write(pom, encoding="utf-8")
                            await run()
                            marker.unlink()
                            exclude.text = "example/ignore/**"
                            xml.write(pom, encoding="utf-8")
                            await run()
                    finally:
                        await call("java_application", action="stop")
    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_model_schema_refresh_preserves_unfiltered_jdt_output(tmp_path):
    require_real_mcp_java_e2e()
    root, _, _, _, port = project_fixture(tmp_path, "maven", False)
    cache = tmp_path / "cache"
    env = {**os.environ, "MAVEN_ARGS": "--offline", "XDG_CACHE_HOME": str(cache)}

    async def scenario():
        output_stamps = {}
        for cycle in range(2):
            with temporary_stderr() as log:
                async with open_mcp_session(log, environment=env) as session:
                    async def call(tool, **args):
                        return dict((await session.call_tool(tool, args)).structuredContent)
                    try:
                        launch = await call("java_application", action="launch", project_path=str(root), main_class="example.App",
                                            ready_port=port, jdwp_port=reserve_local_port())
                        with anyio.fail_after(120):
                            while launch.get("launch_phase") not in {"runtime_active", "failed"} and launch.get("ok", True):
                                await anyio.sleep(.2)
                                launch = await call("java_status", action="status", details=True)
                        assert launch.get("startup_state") == "ready", launch
                        assert response(port) == "A"
                        result = await call("java_fast_test", project_path=str(root), tests=["example.ReadyTest"])
                        with anyio.fail_after(120):
                            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                                await anyio.sleep(.2)
                                result = await call("java_fast_test", action="result", test_run_id=result["test_run_id"])
                        assert result.get("passed") is True, result
                        if cycle:
                            assert launch.get("jdt_bootstrap_reused") is True, launch
                            assert result["compiled_source_count"] == 0, result
                    finally:
                        await call("java_application", action="stop")
            stamps = {str(p): p.stat().st_mtime_ns for p in cache.rglob("*.class")}
            assert stamps
            if cycle:
                assert stamps == output_stamps, "Model refresh must not recompile unchanged Java"
            else:
                output_stamps = stamps
                # Simulate pre-filter model records. Re-Probe is needed, but its
                # unchanged compiler configuration should keep the JDT workspace.
                models = list(cache.rglob("build-world.json"))
                assert len(models) == 2
                for path in models:
                    payload = json.loads(path.read_text())
                    payload["schema"] = ("jolink.fast-test-world.v7" if payload["schema"].startswith("jolink.fast-test")
                                         else "jolink.project-launch-cache.v4")
                    path.write_text(json.dumps(payload))
    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("with_upstream", [False, True])
def test_war_module_selection_reports_ambiguity_and_accepts_explicit_module(tmp_path, with_upstream):
    require_real_mcp_java_e2e()
    root, app, pom, _, _ = project_fixture(tmp_path, "maven", True)
    project = ET.parse(pom)
    ET.SubElement(project.getroot().find("{*}modules"), "module").text = "other"
    project.write(pom, encoding="utf-8")
    module = ET.parse(app / "pom.xml")
    ET.SubElement(module.getroot(), "packaging").text = "war"
    if not with_upstream:
        module.getroot().remove(module.getroot().find("{*}dependencies"))
        source = app / "src/main/java/example/App.java"
        source.write_text(source.read_text().replace("if(Library.VALUE!=42)throw new IllegalStateException();", ""))
    module.write(app / "pom.xml", encoding="utf-8")
    other = root / "other"
    shutil.copytree(app, other)
    other_pom = other / "pom.xml"
    other_pom.write_text(other_pom.read_text().replace("<artifactId>app</artifactId>", "<artifactId>other</artifactId>"))
    env = {**os.environ, "MAVEN_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache")}

    async def scenario():
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                async def run(directory):
                    result = dict((await session.call_tool("java_fast_test", {
                        "project_path": str(directory), "tests": ["example.ReadyTest"],
                    })).structuredContent)
                    with anyio.fail_after(120):
                        while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                            await anyio.sleep(.2)
                            result = dict((await session.call_tool("java_fast_test", {
                                "action": "result", "test_run_id": result["test_run_id"],
                            })).structuredContent)
                    return dict((await session.call_tool("java_fast_test", {
                        "action": "result", "test_run_id": result["test_run_id"],
                    })).structuredContent)

                ambiguous = await run(root)
                assert ambiguous.get("error_code") == "FAST_TEST_MODULE_AMBIGUOUS", ambiguous
                for directory in (app, other):
                    result = await run(directory)
                    assert result.get("passed") is True and result["tests"] == 1, json.dumps(result)
    anyio.run(scenario)
