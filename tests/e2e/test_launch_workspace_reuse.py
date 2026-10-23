"""Real Probe/MCP/JDT/HTTP coverage for configuration-only cache invalidation."""

import shutil
import http.client
import os

import anyio
import pytest
from java_support import open_mcp_session, reserve_local_port, temporary_stderr
from test_application_java_home import call, setup
from test_build_configuration_mcp import project_fixture
from test_review_model_cache import response


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("system,multi", [("maven", False), ("maven", True), ("gradle", False), ("gradle", True)])
def test_reprobe_keeps_workspace_when_compilation_configuration_is_unchanged(tmp_path, system, multi):
    homes, env = setup(tmp_path)
    root, app, config, source, port = project_fixture(tmp_path, system, multi)
    shutil.rmtree(root / ".idea")
    if system == "maven":
        config.write_text(config.read_text().replace("<version>3.13.0</version>", "<version>3.11.0</version>"))
    elif multi:
        (root / "settings.gradle").write_text("rootProject.name='reuse'\ninclude 'lib', 'app'\n")
        (app / "build.gradle").write_text(config.read_text() + "\ndependencies { implementation project(':lib') }\n")
        config.write_text("subprojects { apply plugin: 'java-library'; java { sourceCompatibility='8'; targetCompatibility='8' } }\n")
        library = root / "lib/src/main/java/example/Library.java"
        library.parent.mkdir(parents=True)
        library.write_text("package example; public class Library { public static final int VALUE=42; }")
        source.write_text(source.read_text().replace("public static String value() throws Exception {",
                          "public static String value() throws Exception { if(Library.VALUE!=42)throw new IllegalStateException();"))
    source.write_text(source.read_text().replace('byte[] b=value().getBytes("UTF-8")',
        'byte[] b=(value()+"|v1|"+App.class.getDeclaredMethod("main",String[].class).getParameters()[0].isNamePresent()).getBytes("UTF-8")'))
    args = dict(project_path=str(root), main_class="example.App", build_system=system,
                java_home=homes[8], ready_port=port, jdwp_port=reserve_local_port())
    settings = root / "custom-settings.xml"
    if system == "maven":
        settings.write_text("<settings/>")
        args["maven_settings"] = str(settings)
    log_root = tmp_path / "cache"

    def logs():
        return "\n".join(p.read_text() for p in log_root.rglob("mcp.log"))

    def classes():
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in log_root.rglob("*.class")}

    def comment(path):
        text = "<!-- documentation only -->" if path.suffix == ".xml" else "// documentation only"
        path.write_text(path.read_text() + "\n" + text + "\n")

    async def scenario():
        async def launch(session, expected):
            value = await call(session, "java_application", action="launch", **args)
            assert value.get("ok") and value.get("startup_state") == "ready", value
            assert response(port) == expected
            return value

        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                try:
                    initial = await launch(session, "A|v1|false")
                    assert initial["jdt_bootstrap_build_kind"] == "FULL"
                    await call(session, "java_application", action="stop")
                    original_classes = classes()
                    assert original_classes
                    # Parent/build-script comments invalidate Probe, not JavaBuilder state.
                    comment(config)
                    unchanged = await launch(session, "A|v1|false")
                    assert not unchanged.get("probe_cache_reused", False), unchanged
                    assert unchanged["jdt_bootstrap_reused"] and unchanged["jdt_bootstrap_build_kind"] is None, unchanged
                    assert classes() == original_classes
                    await call(session, "java_application", action="stop")
                    if system == "maven":
                        comment(settings)
                        unchanged = await launch(session, "A|v1|false")
                        assert not unchanged.get("probe_cache_reused", False), unchanged
                        assert unchanged["jdt_bootstrap_reused"] and classes() == original_classes
                        await call(session, "java_application", action="stop")

                    # New Probe + real edit still uses the same workspace incrementally.
                    source.write_text(source.read_text().replace('"|v1|"', '"|v2|"'))
                    comment(config)
                    changed = await launch(session, "A|v2|false")
                    assert changed["jdt_bootstrap_reused"] and changed["jdt_bootstrap_build_kind"] == "INCREMENTAL", changed
                    assert logs().count("requested=FULL actual=FULL") == 1
                    await call(session, "java_application", action="stop")

                    # A real compiler setting must reach bytecode, not just a report.
                    actual_config = app / "build.gradle" if system == "gradle" and multi else config
                    if system == "maven":
                        config.write_text(config.read_text().replace("<properties>",
                            "<properties><maven.compiler.parameters>true</maven.compiler.parameters>"))
                    else:
                        actual_config.write_text(actual_config.read_text() + "\ntasks.withType(JavaCompile).configureEach { options.compilerArgs += ['-parameters'] }\n")
                    reconfigured = await launch(session, "A|v2|true")
                    assert not reconfigured["jdt_bootstrap_reused"] and reconfigured["jdt_bootstrap_build_kind"] == "FULL", reconfigured
                    assert logs().count("requested=FULL actual=FULL") == 2
                    assert "method_parameters" in logs()
                    await call(session, "java_application", action="stop")
                    saved_classes = classes()
                finally:
                    await call(session, "java_application", action="stop")

        # A different MCP process must restore the persisted compiler settings.
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                try:
                    reopened = await launch(session, "A|v2|true")
                    assert reopened["probe_cache_reused"] and reopened["jdt_bootstrap_reused"], reopened
                    assert reopened["jdt_bootstrap_build_kind"] is None and classes() == saved_classes
                    await call(session, "java_application", action="stop")
                    comment(config)
                    source.write_text(source.read_text().replace('"|v2|"', '"|v3|"'))
                    changed = await launch(session, "A|v3|true")
                    assert changed["jdt_bootstrap_reused"] and changed["jdt_bootstrap_build_kind"] == "INCREMENTAL", changed
                    assert logs().count("requested=FULL actual=FULL") == 2
                finally:
                    await call(session, "java_application", action="stop")

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_public_spring_boot_reprobe_keeps_compilation_and_http_current(tmp_path):
    _, env = setup(tmp_path)
    checkout = os.environ.get("JOLINK_TEST_SPRING_H2")
    java = os.environ.get("JOLINK_TEST_JAVA11_HOME")
    if not checkout or not java:
        pytest.skip("set JOLINK_TEST_SPRING_H2 and JOLINK_TEST_JAVA11_HOME")
    project = tmp_path / "spring-boot"
    shutil.copytree(checkout, project, ignore=shutil.ignore_patterns(".git", ".idea", "target", "build"))
    controller = project / "src/main/java/com/example/rjany/controller/ReuseController.java"
    controller.write_text('''package com.example.rjany.controller;
@org.springframework.web.bind.annotation.RestController
public class ReuseController {
 @org.springframework.web.bind.annotation.GetMapping("/jolink-reuse")
 public String value() { return "v1"; }
}''')
    oracle = project / "expected.txt"
    oracle.write_text("v1")
    test = project / "src/test/java/com/example/rjany/ReuseTest.java"
    test.parent.mkdir(parents=True, exist_ok=True)
    test.write_text('''package com.example.rjany;
public class ReuseTest {
 @org.junit.jupiter.api.Test public void current() throws Exception {
  org.junit.jupiter.api.Assertions.assertEquals(
   new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get("expected.txt")),"UTF-8"),
   new com.example.rjany.controller.ReuseController().value());
 }
}''')
    port = reserve_local_port()
    args = dict(project_path=str(project), main_class="com.example.rjany.SpringEmbeddedH2DbApplication",
                java_home=java, build_java_home=java, ready_port=port, jdwp_port=reserve_local_port(),
                app_args=[f"--server.port={port}", "--spring.datasource.url=jdbc:h2:mem:jolink_reuse"])

    def get(path):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", path)
            value = connection.getresponse()
            assert value.status == 200
            return value.read().decode()
        finally:
            connection.close()

    async def run_test(session):
        value = dict((await session.call_tool("java_fast_test", {
            "project_path": str(project), "tests": ["com.example.rjany.ReuseTest"],
            "build_java_home": java, "java_home": java,
        })).structuredContent)
        with anyio.fail_after(180):
            while value.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                await anyio.sleep(.2)
                value = dict((await session.call_tool("java_fast_test", {
                    "action": "result", "test_run_id": value["test_run_id"],
                })).structuredContent)
        assert value.get("passed") is True, value
        return value

    async def scenario():
        for cycle in range(3):
            with temporary_stderr() as log:
                async with open_mcp_session(log, environment=env) as session:
                    try:
                        launched = await call(session, "java_application", action="launch", **args)
                        assert launched.get("ok") and launched.get("startup_state") == "ready", launched
                        assert get("/student") == "[]"
                        assert get("/jolink-reuse") == oracle.read_text()
                        assert launched["jdt_bootstrap_build_kind"] == ["FULL", "INCREMENTAL", None][cycle]
                        assert launched["jdt_bootstrap_reused"] is bool(cycle)
                        if cycle == 1:
                            assert not launched.get("probe_cache_reused", False)
                        if cycle == 2:
                            assert launched["probe_cache_reused"]
                        result = await run_test(session)
                        if cycle == 2:
                            assert result["compiled_source_count"] == 0
                    finally:
                        await call(session, "java_application", action="stop")
            if cycle == 0:
                pom = project / "pom.xml"
                pom.write_text(pom.read_text() + "\n<!-- documentation changed -->\n")
                controller.write_text(controller.read_text().replace('"v1"', '"v2"'))
                oracle.write_text("v2")
        build_lines = [line for path in (tmp_path / "cache").rglob("mcp.log")
                       for line in path.read_text().splitlines()
                       if "jdt-workspaces" in line and "jdt.build.finished" in line]
        assert len(build_lines) == 2, build_lines
        assert "requested=FULL actual=FULL" in build_lines[0]
        assert "actual=INCREMENTAL" in build_lines[1] and "compiled_sources=1 " in build_lines[1]
    anyio.run(scenario)
