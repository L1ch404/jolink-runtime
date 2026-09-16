"""Config-only edits must change actual Runtime/Test behavior after re-Probe."""

import http.client
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import anyio
import pytest
from java_support import REPOSITORY_ROOT, require_real_mcp_java_e2e, reserve_local_port
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_mcp_runtime_preparation import make_test_project


def project_fixture(tmp_path, system, multi):
    port = reserve_local_port()
    root = make_test_project(tmp_path, port)
    app = root
    if multi:
        app = root / "app"
        app.mkdir()
        (root / "src").rename(app / "src")
    for value in ("A", "B"):
        resource = app / ("resources" + value) / "value.txt"
        resource.parent.mkdir()
        resource.write_text(value)
    source = app / "src/main/java/example/App.java"
    source.write_text(
        """package example;
public class App {
 public static String value() throws Exception {
  try(java.io.InputStream s=App.class.getResourceAsStream("/value.txt")){return Character.toString((char)s.read());}
 }
 public static void main(String[] args) throws Exception {
  com.sun.net.httpserver.HttpServer s=com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1",PORT),0);
  s.createContext("/",e->{try {byte[] b=value().getBytes("UTF-8");e.sendResponseHeaders(200,b.length);e.getResponseBody().write(b);e.close();}catch(Exception x){throw new RuntimeException(x);}});s.start();
 }
}""".replace("PORT", str(port))
    )
    (app / "src/test/java/example/ReadyTest.java").write_text("""package example;
public class ReadyTest {@org.junit.Test public void resource() throws Exception {org.junit.Assert.assertEquals(System.getProperty("expected"),App.value());}}""")
    if system == "maven":
        config = root / "pom.xml"
        text = (
            config.read_text()
            .replace(
                "<properties>",
                "<properties><resource.folder>resourcesA</resource.folder><expected>A</expected>",
            )
            .replace(
                "<build>",
                "<build><resources><resource><directory>${resource.folder}</directory></resource></resources>",
            )
        )
        text = text.replace(
            "</plugins>",
            """<plugin><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version><configuration><systemPropertyVariables><expected>${expected}</expected></systemPropertyVariables></configuration></plugin></plugins>""",
        )
        if multi:
            text = text.replace(
                "<properties>",
                "<packaging>pom</packaging><modules><module>lib</module><module>app</module></modules><properties>",
            )
            parent = "<parent><groupId>example</groupId><artifactId>preparation</artifactId><version>1</version></parent>"
            (app / "pom.xml").write_text(
                f"<project><modelVersion>4.0.0</modelVersion>{parent}<artifactId>app</artifactId><dependencies><dependency><groupId>example</groupId><artifactId>lib</artifactId><version>1</version></dependency></dependencies></project>"
            )
            lib = root / "lib"
            lib.mkdir()
            (lib / "pom.xml").write_text(
                f"<project><modelVersion>4.0.0</modelVersion>{parent}<artifactId>lib</artifactId></project>"
            )
            java = lib / "src/main/java/example/Library.java"
            java.parent.mkdir(parents=True)
            java.write_text(
                "package example; public class Library { public static final int VALUE=42; }"
            )
            source.write_text(
                source.read_text().replace(
                    "public static String value() throws Exception {",
                    "public static String value() throws Exception { if(Library.VALUE!=42)throw new IllegalStateException();",
                )
            )
            idea = root / ".idea/workspace.xml"
            idea.write_text(
                idea.read_text().replace(
                    '<option name="MAIN_CLASS_NAME"',
                    '<module name="app"/><option name="MAIN_CLASS_NAME"',
                )
            )
        config.write_text(text)
    else:
        gradle = os.environ.get("JOLINK_FAST_TEST_GRADLE")
        if not gradle:
            pytest.skip("set JOLINK_FAST_TEST_GRADLE")
        (root / "pom.xml").unlink()
        wrapper = root / ("gradlew.bat" if os.name == "nt" else "gradlew")
        wrapper.write_text(
            f'@echo off\r\ncall "{gradle}" %*\r\n'
            if os.name == "nt"
            else f'#!/bin/sh\nexec "{gradle}" "$@"\n'
        )
        wrapper.chmod(0o755)
        properties = root / "gradle/wrapper/gradle-wrapper.properties"
        properties.parent.mkdir(parents=True)
        properties.write_text(
            "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.14-bin.zip\n"
        )
        (root / "settings.gradle").write_text("rootProject.name='configuration'\n")
        config = root / "build.gradle"
        junit = (
            Path.home() / ".m2/repository/junit/junit/4.13.2/junit-4.13.2.jar"
        ).as_posix()
        hamcrest = (
            Path.home()
            / ".m2/repository/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar"
        ).as_posix()
        config.write_text(f"""plugins {{ id 'java' }}
sourceCompatibility='8'; targetCompatibility='8'
tasks.withType(JavaCompile).configureEach {{ options.encoding='UTF-8' }}
sourceSets.main.resources.srcDirs=['resourcesA']
dependencies {{ testImplementation files('{junit}','{hamcrest}') }}
test {{ systemProperty 'expected','A' }}
""")
    if system == "maven" and not multi:
        settings_file = root / "custom-settings.xml"
        settings_file.write_text("<settings/>")
        idea = root / ".idea/workspace.xml"
        xml = ET.parse(idea)
        general = xml.getroot().find(".//MavenGeneralSettings")
        ET.SubElement(
            general, "option", name="userSettingsFile", value=str(settings_file)
        )
        xml.write(idea, encoding="unicode")
    return root, app, config, source, port


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize(
    "system,multi", [("maven", False), ("maven", True), ("gradle", False)]
)
def test_configuration_changes_refresh_models_without_mutating_running_jvm(
    tmp_path, system, multi
):
    require_real_mcp_java_e2e()
    java = os.environ.get(
        "JOLINK_TEST_JAVA8_HOME" if system == "maven" else "JOLINK_TEST_JAVA17_HOME"
    )
    if not java:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME / JOLINK_TEST_JAVA17_HOME")
    root, _app, config, source, port = project_fixture(tmp_path, system, multi)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jolink_runtime.transport.stdio"],
        cwd=REPOSITORY_ROOT,
        env={
            **os.environ,
            "JAVA_HOME": java,
            "PATH": str(Path(java) / "bin") + os.pathsep + os.environ["PATH"],
            "MAVEN_ARGS": "--offline",
            "GRADLE_ARGS": "--offline",
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        },
    )

    def response():
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("GET", "/")
            result = connection.getresponse()
            assert result.status == 200
            return result.read().decode()
        finally:
            connection.close()

    async def scenario():
        for reopening in (False, True):
            with (tmp_path / ("reopened.log" if reopening else "first.log")).open(
                "w+"
            ) as log:
                async with (
                    stdio_client(parameters, errlog=log) as (r, w),
                    ClientSession(r, w) as session,
                ):
                    await session.initialize()

                    async def call(action, **args):
                        return dict(
                            (
                                await session.call_tool(
                                    "java_fast_test" if action == "test" else "java_application",
                                    args if action == "test" else {"action": action, **args},
                                )
                            ).structuredContent
                        )

                    async def status():
                        return dict(
                            (
                                await session.call_tool(
                                    "java_status", {"action": "status"}
                                )
                            ).structuredContent
                        )

                    async def launch():
                        result = await call(
                            "launch",
                            project_path=str(root),
                            build_system=system,
                            launch_name="Preparation",
                            ready_port=port,
                            jdwp_port=reserve_local_port(),
                        )
                        with anyio.fail_after(120):
                            while result.get("launch_phase") not in {
                                "runtime_active",
                                "failed",
                            }:
                                await anyio.sleep(0.1)
                                result = await status()
                        assert result.get("launch_phase") == "runtime_active", result
                        return result

                    async def test():
                        result = await call(
                            "test",
                            project_path=str(root),
                            build_system=system,
                            tests=["example.ReadyTest"],
                        )
                        with anyio.fail_after(120):
                            while result.get("status") in {
                                "starting",
                                "bootstrapping",
                                "compiling",
                                "running",
                            }:
                                await anyio.sleep(0.1)
                                result = (await status())["fast_test"]
                        assert result.get("passed") is True, result

                    if reopening:
                        cached = await launch()
                        assert (
                            cached["probe_cache_reused"] is True and response() == "B"
                        ), cached
                        await call("stop")
                        source.write_text(
                            source.read_text().replace(
                                "Character.toString((char)s.read())",
                                'Character.toString((char)s.read())+"!"',
                            )
                        )
                        edited = await launch()
                        assert (
                            edited["probe_cache_reused"] is True and response() == "B!"
                        ), edited
                        await call("stop")
                        settings_file = root / "custom-settings.xml"
                        if settings_file.is_file():
                            settings_file.write_text(
                                "<settings><!-- selected settings updated --></settings>"
                            )
                            refreshed = await launch()
                            assert (
                                refreshed["probe_cache_reused"] is False
                                and response() == "B!"
                            ), refreshed
                            await call("stop")
                        continue

                    active = await launch()
                    assert response() == "A"
                    await test()
                    config.write_text(
                        config.read_text()
                        .replace("resourcesA", "resourcesB")
                        .replace("<expected>A</expected>", "<expected>B</expected>")
                        .replace("'expected','A'", "'expected','B'")
                    )
                    await test()  # New Test model must not bless the old Runtime.
                    for action in ("reload", "restart"):
                        result = await call(
                            action,
                            **(
                                {"source_files": [str(source)]}
                                if action == "reload"
                                else {}
                            ),
                        )
                        assert result["error_code"] == "BUILD_CONFIGURATION_CHANGED", (
                            result
                        )
                        assert result["applied"] is False
                        assert (await status())["pid"] == active[
                            "pid"
                        ] and response() == "A"
                    await call("stop")
                    updated = await launch()
                    assert (
                        updated["probe_cache_reused"] is False and response() == "B"
                    ), updated
                    started = await call("reload", source_files=[str(source)])
                    assert started["status"] == "reload_started", started
                    with anyio.fail_after(10):
                        while True:
                            result = (await status()).get("last_reload", {})
                            if result.get("reload_id") == started["reload_id"]:
                                break
                            await anyio.sleep(0.1)
                    assert result["status"] == "no_changes", result
                    restarted = await call("restart")
                    assert restarted["ok"], restarted
                    with anyio.fail_after(15):
                        while (await status()).get("launch_phase") != "runtime_active":
                            await anyio.sleep(0.1)
                    assert response() == "B"
                    await call("stop")

    anyio.run(scenario)
