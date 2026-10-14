"""Application JDK selection, inheritance and replacement through real MCP/JVMs."""

import http.client
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import anyio
import pytest
from java_support import (
    open_mcp_session,
    require_real_mcp_java_e2e,
    reserve_local_port,
    temporary_stderr,
)
from test_build_configuration_mcp import project_fixture

SOURCE = """import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.lang.management.ManagementFactory;
public class RuntimeJdkApp {
 public static void main(String[] args) throws Exception {
  HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", Integer.parseInt(args[0])), 0);
  server.createContext("/", exchange -> {
   String value = System.getProperty("java.specification.version")+"|"+System.getProperty("java.home")+"|"+System.getProperty("fixture.label")+"|"+args[1];
   byte[] bytes = value.getBytes("UTF-8");
   exchange.sendResponseHeaders(200, bytes.length);exchange.getResponseBody().write(bytes);exchange.close();
  });
  server.start();
 }
}"""


def setup(tmp_path):
    require_real_mcp_java_e2e()
    homes = {
        major: os.environ.get(f"JOLINK_TEST_JAVA{major}_HOME") for major in (8, 17)
    }
    if not all(homes.values()):
        pytest.skip("set JOLINK_TEST_JAVA8_HOME and JOLINK_TEST_JAVA17_HOME")
    if os.environ.get("JOLINK_TEST_JAVA21_HOME"):
        homes[21] = os.environ["JOLINK_TEST_JAVA21_HOME"]
    env = {
        **os.environ,
        "JAVA_HOME": homes[17],
        "PATH": homes[17] + "/bin" + os.pathsep + os.environ["PATH"],
        "MAVEN_ARGS": "--offline",
        "GRADLE_ARGS": "--offline",
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "JOLINK_LOG_LEVEL": "INFO",
    }
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")
    return homes, env


def http_response(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.status == 200
        return response.read().decode("utf-8").split("|")
    finally:
        connection.close()


async def call(session, tool, **arguments):
    with anyio.fail_after(180):
        result = dict((await session.call_tool(tool, arguments)).structuredContent)
        while result.get("ok") and (
            result.get("status")
            in {"project_launch_started", "project_launch_restarted", "restart_started"}
            and result.get("launch_phase") != "runtime_active"
            and result.get("stage") != "succeeded"
        ):
            await anyio.sleep(0.1)
            status = dict(
                (
                    await session.call_tool(
                        "java_status", {"action": "status", "details": True}
                    )
                ).structuredContent
            )
            result = (
                (status.get("last_reload") or result)
                if result.get("reload_id")
                else status
            )
        return result


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("mode", ["class", "jar"])
def test_direct_jdk_selection_and_restart_inheritance(tmp_path, mode):
    homes, env = setup(tmp_path)
    root = tmp_path / "direct launch 中文"
    root.mkdir()
    source = root / "RuntimeJdkApp.java"
    source.write_text(SOURCE, encoding="utf-8")
    suffix = ".exe" if os.name == "nt" else ""
    subprocess.run(
        [
            str(Path(homes[8]) / f"bin/javac{suffix}"),
            "-encoding",
            "UTF-8",
            "-g",
            str(source),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    if mode == "jar":
        jar = root / "application 中文.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr(
                "META-INF/MANIFEST.MF",
                "Manifest-Version: 1.0\r\nMain-Class: RuntimeJdkApp\r\n\r\n",
            )
            for file in root.glob("*.class"):
                archive.write(file, file.name)
        target = {"jar_path": str(jar)}
    else:
        target = {"classpath": str(root), "main_class": "RuntimeJdkApp"}
    port = reserve_local_port()
    args = {
        **target,
        "ready_port": port,
        "jdwp_port": reserve_local_port(),
        "app_args": [str(port), "keep"],
        "vm_args": ["-Dfixture.label=keep"],
    }

    def verify(major, label="keep"):
        result = http_response(port)
        assert result[0] == ("1.8" if major == 8 else str(major)), result
        actual = Path(result[1]).resolve()
        assert actual in {
            Path(homes[major]).resolve(),
            (Path(homes[major]) / "jre").resolve(),
        }, result
        assert result[2:] == [label, "keep"], result

    async def scenario():
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                try:
                    result = await call(
                        session,
                        "java_application",
                        action="launch",
                        java_home=homes[8],
                        **args,
                    )
                    assert (
                        result.get("ok") and result.get("startup_state") == "ready"
                    ), result
                    verify(8)
                    previous_pid = result["pid"]
                    selections = [
                        (8, {}),
                        (17, {"java_home": homes[17]}),
                        (17, {}),
                    ]
                    if 21 in homes:
                        selections += [(21, {"java_home": homes[21]}), (21, {})]
                    selections += [
                        (8, {"java_home": homes[8]}),
                    ]
                    for major, overrides in selections:
                        result = await call(
                            session, "java_application", action="restart", **overrides
                        )
                        assert (
                            result.get("ok") and result.get("startup_state") == "ready"
                        ), result
                        assert result["pid"] != previous_pid
                        previous_pid = result["pid"]
                        verify(major)
                    await call(session, "java_application", action="stop")
                    result = await call(session, "java_application", action="restart")
                    assert (
                        result.get("ok") and result.get("startup_state") == "ready"
                    ), result
                    verify(8)
                    await call(session, "java_application", action="stop")
                    # A new launch without java_home still uses the original PATH policy.
                    result = await call(
                        session, "java_application", action="launch", **args
                    )
                    assert result.get("ok"), result
                    verify(17)
                    await call(session, "java_application", action="stop")
                    failed = await call(
                        session,
                        "java_application",
                        action="launch",
                        java_home=str(root / "missing-jdk"),
                        **args,
                    )
                    assert failed.get("ok") is False and "missing-jdk" in failed.get(
                        "error", ""
                    ), failed
                    assert not (await call(session, "java_status", action="status"))[
                        "running"
                    ]
                    recovered = await call(
                        session,
                        "java_application",
                        action="restart",
                        java_home=homes[8],
                    )
                    assert recovered.get("ok"), recovered
                    verify(8)
                finally:
                    await call(session, "java_application", action="stop")

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize(
    "system,multi",
    [("maven", False), ("maven", True), ("gradle", False), ("gradle", True)],
)
def test_project_restart_switches_jdk_without_rebuilding_workspace(
    tmp_path, system, multi
):
    homes, env = setup(tmp_path)
    root, _, config, source, port = project_fixture(tmp_path, system, multi)
    shutil.rmtree(root / ".idea")
    if system == "maven":
        config.write_text(
            config.read_text().replace(
                "<version>3.13.0</version>", "<version>3.11.0</version>"
            )
        )
    elif multi:
        # The shared fixture only supplies a single-project Gradle build.
        (root / "settings.gradle").write_text(
            "rootProject.name='jdk-modules'\ninclude 'lib', 'app'\n"
        )
        (root / "app/build.gradle").write_text(
            config.read_text() + "\ndependencies { implementation project(':lib') }\n"
        )
        config.write_text(
            "subprojects { apply plugin: 'java-library'; java { sourceCompatibility='8'; targetCompatibility='8' } }\n"
        )
        library = root / "lib/src/main/java/example/Library.java"
        library.parent.mkdir(parents=True)
        library.write_text(
            "package example; public class Library { public static int value(){return 42;} }"
        )
        source.write_text(
            source.read_text().replace(
                "public static String value() throws Exception {",
                "public static String value() throws Exception { if(Library.value()!=42)throw new IllegalStateException();",
            )
        )
    original = source.read_text().replace(
        'byte[] b=value().getBytes("UTF-8")',
        'byte[] b=(value()+"|"+System.getProperty("java.specification.version")+"|"+System.getProperty("java.home")+"|v1").getBytes("UTF-8")',
    )
    source.write_text(original)
    args = {
        "project_path": str(root),
        "main_class": "example.App",
        "build_system": system,
        "ready_port": port,
        "jdwp_port": reserve_local_port(),
        "java_home": homes[8],
    }
    if system == "maven" and os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
        args["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]

    def verify(major, revision):
        response = http_response(port)
        assert response[:2] == ["A", "1.8" if major == 8 else str(major)], response
        assert Path(response[2]).resolve() in {
            Path(homes[major]).resolve(),
            (Path(homes[major]) / "jre").resolve(),
        }
        assert response[3] == revision

    def classes():
        return {
            str(p): p.stat().st_mtime_ns for p in (tmp_path / "cache").rglob("*.class")
        }

    async def scenario():
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                try:
                    launched = await call(
                        session, "java_application", action="launch", **args
                    )
                    assert (
                        launched.get("ok") and launched.get("startup_state") == "ready"
                    ), launched
                    verify(8, "v1")
                    stamps = classes()
                    assert stamps
                    switched = await call(
                        session,
                        "java_application",
                        action="restart",
                        java_home=homes[17],
                    )
                    assert (
                        switched.get("ok")
                        and switched.get("restart_reason") == "APPLICATION_JDK_CHANGED"
                    ), switched
                    assert (
                        switched["apply_method"] == "restart"
                        and switched["pid"] != launched["pid"]
                    )
                    assert (
                        switched["compiled_source_count"] == 0 and classes() == stamps
                    )
                    verify(17, "v1")
                    inherited = await call(
                        session, "java_application", action="restart"
                    )
                    assert (
                        inherited.get("ok") and inherited["apply_method"] == "restart"
                    ), inherited
                    assert classes() == stamps
                    verify(17, "v1")
                    source.write_text(original.replace('"|v1"', '"|v2"'))
                    same = await call(
                        session,
                        "java_application",
                        action="restart",
                        java_home=homes[17],
                    )
                    assert same.get("ok") and same["apply_method"] == "hotswap", same
                    assert (await call(session, "java_status", action="status"))[
                        "pid"
                    ] == inherited["pid"]
                    verify(17, "v2")
                    source.write_text(original.replace('"|v1"', '"|v3"'))
                    changed = await call(
                        session,
                        "java_application",
                        action="restart",
                        java_home=homes[8],
                    )
                    assert (
                        changed.get("ok")
                        and changed.get("restart_reason") == "APPLICATION_JDK_CHANGED"
                    ), changed
                    assert (
                        changed["apply_method"] == "restart"
                        and changed["compiled_source_count"] > 0
                    )
                    verify(8, "v3")
                    logs = "\n".join(
                        p.read_text() for p in (tmp_path / "cache").rglob("mcp.log")
                    )
                    assert logs.count("jdt.worker.ready ") == 1, logs
                    stamps = classes()
                    await call(session, "java_application", action="stop")
                    reopened = await call(
                        session,
                        "java_application",
                        action="restart",
                        java_home=homes[17],
                    )
                    assert reopened.get("ok") and reopened["probe_cache_reused"], (
                        reopened
                    )
                    assert (
                        reopened.get("jdt_bootstrap_build_kind") != "FULL"
                        and classes() == stamps
                    ), reopened
                    verify(17, "v3")
                    inherited = await call(
                        session, "java_application", action="restart"
                    )
                    assert inherited.get("ok"), inherited
                    verify(17, "v3")
                finally:
                    await call(session, "java_application", action="stop")

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_incompatible_class_version_fails_on_the_selected_jdk(tmp_path):
    homes, env = setup(tmp_path)
    source = tmp_path / "ModernTarget.java"
    source.write_text(
        "public class ModernTarget { public static void main(String[] args) throws Exception { Thread.sleep(60000); } }"
    )
    suffix = ".exe" if os.name == "nt" else ""
    subprocess.run(
        [str(Path(homes[17]) / f"bin/javac{suffix}"), "--release", "17", str(source)],
        capture_output=True,
        check=True,
        timeout=30,
    )

    async def scenario():
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                try:
                    result = await call(
                        session,
                        "java_application",
                        action="launch",
                        classpath=str(tmp_path),
                        main_class="ModernTarget",
                        java_home=homes[8],
                        jdwp_port=reserve_local_port(),
                    )
                    assert (
                        result.get("ok") is False
                        and result.get("error_code") == "JVM_START_FAILED"
                    ), result
                    output = await call(session, "java_status", action="logs")
                    assert "UnsupportedClassVersionError" in "".join(output["lines"]), (
                        output
                    )
                    assert not (await call(session, "java_status", action="status"))[
                        "running"
                    ]
                finally:
                    await call(session, "java_application", action="stop")

    anyio.run(scenario)
