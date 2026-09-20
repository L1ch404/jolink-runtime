"""Regress the review gaps through native Maven, persistent Workers and MCP."""

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from java_support import REPOSITORY_ROOT, require_real_mcp_java_e2e, reserve_local_port
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_build_configuration_mcp import project_fixture


@asynccontextmanager
async def client(tmp_path, label, cache):
    java = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not java:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jolink_runtime.transport.stdio"],
        cwd=REPOSITORY_ROOT,
        env={
            **os.environ,
            "JAVA_HOME": java,
            "PATH": str(Path(java) / "bin") + os.pathsep + os.environ["PATH"],
            "MAVEN_ARGS": "--offline",
            "XDG_CACHE_HOME": str(cache),
        },
    )
    with (tmp_path / f"{label}.log").open("w+") as log:
        async with (
            stdio_client(parameters, errlog=log) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()

            async def call(tool, args):
                return dict((await session.call_tool(tool, args)).structuredContent)

            yield call


async def run_test(call, root, selector):
    result = await call(
        "java_fast_test",
        {"action": "run", "project_path": str(root), "tests": [selector]},
    )
    with anyio.fail_after(120):
        while result.get("status") in {
            "starting",
            "bootstrapping",
            "compiling",
            "running",
        }:
            await anyio.sleep(0.1)
            result = (await call("java_status", {"action": "status"}))["fast_test"]
    assert result.get("passed") is True, result
    return result


async def launch(call, root, port):
    result = await call(
        "java_application",
        {
            "action": "launch",
            "project_path": str(root),
            "launch_name": "Preparation",
            "ready_port": port,
            "jdwp_port": reserve_local_port(),
        },
    )
    return await wait_ready(call, result)


async def wait_ready(call, result):
    with anyio.fail_after(120):
        while result.get("launch_phase") not in {"runtime_active", "failed"}:
            await anyio.sleep(0.1)
            result = await call("java_status", {"action": "status"})
    assert result.get("startup_state") == "ready", result
    return result


def response(port):
    from http.client import HTTPConnection

    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", "/")
        result = connection.getresponse()
        assert result.status == 200
        return result.read().decode()
    finally:
        connection.close()


def settings(value):
    return f"""<settings><profiles><profile><id>fixture</id><properties><resource.folder>resources{value}</resource.folder><expected>{value}</expected></properties></profile></profiles><activeProfiles><activeProfile>fixture</activeProfile></activeProfiles></settings>"""


@pytest.mark.mcp_java_e2e
def test_fast_test_settings_refresh_and_noop_reopen(tmp_path):
    require_real_mcp_java_e2e()
    root, app, pom, _, _ = project_fixture(tmp_path, "maven", False)
    pom.write_text(
        pom.read_text()
        .replace("<resource.folder>resourcesA</resource.folder>", "")
        .replace("<expected>A</expected>", "")
    )
    selected = root / "custom-settings.xml"
    selected.write_text(settings("A"))
    oracle = root / "expected.txt"
    oracle.write_text("A")
    (app / "src/test/java/example/ReadyTest.java").write_text(
        """package example;
public class ReadyTest {@org.junit.Test public void resource() throws Exception {
 String expected=new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(ORACLE)),"UTF-8");
 org.junit.Assert.assertEquals(expected,System.getProperty("expected"));
 org.junit.Assert.assertEquals(expected,App.value());
}}""".replace("ORACLE", json.dumps(oracle.as_posix()))
    )
    cache = tmp_path / "cache"

    async def scenario():
        async with client(tmp_path, "first", cache) as call:
            await run_test(call, root, "example.ReadyTest")
            unchanged = await run_test(call, root, "example.ReadyTest")
            assert (
                "freshness_ms" in unchanged and unchanged["compiled_source_count"] == 0
            ), unchanged
            selected.write_text(settings("B"))
            oracle.write_text("B")
            refreshed = await run_test(call, root, "example.ReadyTest")
            assert "bootstrap_ms" in refreshed, refreshed
        raw = json.loads(
            next(
                (cache / "jolink-runtime/fast-test").glob("*/build-world.json")
            ).read_text()
        )
        assert str(selected.resolve()) in raw["configuration_files"]
        assert all(
            "effective-pom" not in p and "resourcesB" not in p
            for p in raw["configuration_files"]
        )
        async with client(tmp_path, "reopened", cache) as call:
            await run_test(call, root, "example.ReadyTest")
            warm = await run_test(call, root, "example.ReadyTest")
            assert "freshness_ms" in warm and warm["compiled_source_count"] == 0, warm

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_profile_module_parent_change_updates_runtime_metadata(tmp_path):
    require_real_mcp_java_e2e()
    root, _, pom, source, port = project_fixture(tmp_path, "maven", True)
    pom.write_text(
        pom.read_text()
        .replace("<module>lib</module>", "")
        .replace(
            "</project>",
            "<profiles><profile><id>extra</id><modules><module>lib</module></modules></profile></profiles></project>",
        )
    )
    (root / ".mvn").mkdir()
    (root / ".mvn/maven.config").write_text("-Pextra\n")
    parent = root / "lib-parent/pom.xml"
    parent.parent.mkdir()
    parent.write_text("""<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>lib-parent</artifactId><version>1</version><packaging>pom</packaging>
<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding><maven.compiler.parameters>false</maven.compiler.parameters></properties>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version><configuration><parameters>${maven.compiler.parameters}</parameters></configuration></plugin></plugins></build></project>""")
    (root / "lib/pom.xml").write_text(
        """<project><modelVersion>4.0.0</modelVersion><parent><groupId>example</groupId><artifactId>lib-parent</artifactId><version>1</version><relativePath>../lib-parent/pom.xml</relativePath></parent><artifactId>lib</artifactId></project>"""
    )
    (root / "lib/src/main/java/example/Library.java").write_text(
        "package example; public class Library {public static String echo(String named){return named;} }"
    )
    source.write_text(
        """package example; public class App {public static void main(String[] args) throws Exception {
 com.sun.net.httpserver.HttpServer s=com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1",PORT),0);
 s.createContext("/",e->{try {byte[] b=String.valueOf(Library.class.getMethod("echo",String.class).getParameters()[0].isNamePresent()).getBytes("UTF-8");e.sendResponseHeaders(200,b.length);e.getResponseBody().write(b);e.close();}catch(Exception x){throw new RuntimeException(x);}});s.start();
}}""".replace("PORT", str(port))
    )

    async def scenario():
        async with client(tmp_path, "profile", tmp_path / "cache") as call:
            original = await launch(call, root, port)
            assert response(port) == "false"
            parent.write_text(
                parent.read_text().replace(
                    "<maven.compiler.parameters>false",
                    "<maven.compiler.parameters>true",
                )
            )
            restarted = await call("java_application", {"action": "restart"})
            assert restarted["ok"] and restarted["apply_method"] == "restart", restarted
            updated = await wait_ready(call, restarted)
            assert updated["pid"] != original["pid"], updated
            assert (
                updated["probe_cache_reused"] is False and response(port) == "true"
            ), updated
            await call("java_application", {"action": "stop"})

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_two_live_mcps_do_not_confuse_shared_disk_model_with_their_own(tmp_path):
    require_real_mcp_java_e2e()
    root, _, pom, _, _ = project_fixture(tmp_path, "maven", False)
    # Two target modules give the Workers separate native Eclipse workspaces,
    # while FastTestCache intentionally shares the root project's model file.
    (root / "src/test/java/example/ReadyTest.java").unlink()
    pom.write_text(
        pom.read_text().replace(
            "<properties>",
            "<packaging>pom</packaging><modules><module>left</module><module>right</module></modules><properties>",
        )
    )
    expected = root / "expected.txt"
    expected.write_text("A")
    for name in ("left", "right"):
        module = root / name
        source = module / f"src/test/java/example/{name.title()}Test.java"
        source.parent.mkdir(parents=True)
        (module / "pom.xml").write_text(
            f"""<project><modelVersion>4.0.0</modelVersion><parent><groupId>example</groupId><artifactId>preparation</artifactId><version>1</version></parent><artifactId>{name}</artifactId></project>"""
        )
        main = module / f"src/main/java/example/{name.title()}.java"
        main.parent.mkdir(parents=True)
        main.write_text(f"package example; public class {name.title()} {{}}")
        source.write_text(
            """package example; public class NAME {
@org.junit.Test public void value() throws Exception {
 String expected=new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(ORACLE)),"UTF-8");
 org.junit.Assert.assertEquals(expected,System.getProperty("expected"));
}}""".replace("NAME", name.title() + "Test").replace(
                "ORACLE", json.dumps(expected.as_posix())
            )
        )
    cache = tmp_path / "cache"

    async def scenario():
        async with (
            client(tmp_path, "mcp-a", cache) as a,
            client(tmp_path, "mcp-b", cache) as b,
        ):
            await run_test(a, root, "example.LeftTest")
            model_file = next(
                (cache / "jolink-runtime/fast-test").glob("*/build-world.json")
            )
            assert (
                Path(json.loads(model_file.read_text())["world"]["module_root"]).name
                == "left"
            )
            pom.write_text(
                pom.read_text().replace(
                    "<expected>A</expected>", "<expected>B</expected>"
                )
            )
            expected.write_text("B")
            await run_test(b, root, "example.RightTest")
            assert (
                Path(json.loads(model_file.read_text())["world"]["module_root"]).name
                == "right"
            )
            assert (await a("java_status", {"action": "status"}))["fast_test"][
                "test_compile_ready"
            ] is True
            refreshed = await run_test(a, root, "example.LeftTest")
            assert "bootstrap_ms" in refreshed, refreshed
            assert (
                Path(json.loads(model_file.read_text())["world"]["module_root"]).name
                == "left"
            )
            # B remains valid even though A has now replaced the disk model.
            still_current = await run_test(b, root, "example.RightTest")
            assert "freshness_ms" in still_current, still_current

    anyio.run(scenario)
