"""Real stdio MCP and pinned JARs: handshake, progress, continuation and restart."""

import http.client
import json
import os
import shutil
import sys
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET

import anyio
import pytest
from java_support import (
    REPOSITORY_ROOT,
    require_real_mcp_java_e2e,
    reserve_local_port,
    temporary_stderr,
)
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from jolink_runtime.launch.jdt_compile_session import JdtCandidate


@contextmanager
def paused_mirror(candidate, blocked_index, jdk_archive=None):
    release = threading.Event()
    requests = Counter()
    artifacts = [a["filename"] for a in candidate.lock["artifacts"]]
    if jdk_archive is not None:
        artifacts.append(jdk_archive.name)
    blocked = artifacts[blocked_index]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            name = self.path.rsplit("/", 1)[-1]
            if name not in artifacts:
                self.send_error(404)
                return
            requests[name] += 1
            path = (
                jdk_archive
                if jdk_archive is not None and name == jdk_archive.name
                else candidate.root / "plugins" / name
            )
            self.send_response(200)
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            try:
                with path.open("rb") as stream:
                    if name == blocked:
                        self.wfile.write(stream.read(65536))
                        self.wfile.flush()
                        if not release.wait(45):
                            return
                    shutil.copyfileobj(stream, self.wfile, 65536)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", release, requests, artifacts
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@asynccontextmanager
async def mcp(tmp_path, env, label):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    exited = tmp_path / (label + ".exited")
    # Only Python's runtime asset cache is isolated. Java retains its existing
    # Maven repository; this is not a product environment override/backdoor.
    script = (
        "from pathlib import Path; import atexit; "
        f"Path.home=staticmethod(lambda:Path({str(home)!r})); "
        f"atexit.register(lambda:Path({str(exited)!r}).write_text('done')); "
        "from jolink_runtime.transport.stdio import main; main()"
    )
    parameters = StdioServerParameters(
        command=sys.executable, args=["-c", script], cwd=REPOSITORY_ROOT, env=env
    )
    with temporary_stderr() as stderr:
        try:
            async with (
                stdio_client(parameters, errlog=stderr) as (read, write),
                ClientSession(read, write) as session,
            ):
                with anyio.fail_after(5):
                    await session.initialize()
                yield session
        finally:
            stderr.seek(0)
            (tmp_path / (label + ".log")).write_text(stderr.read())
    assert exited.is_file(), "MCP was killed instead of exiting normally"


async def status(session):
    return dict(
        (await session.call_tool("java_status", {"action": "status"})).structuredContent
    )


async def wait_progress(session, filename):
    with anyio.fail_after(10):
        while True:
            result = await status(session)
            progress = result.get("runtime_preparation", {})
            if (
                progress.get("current_artifact") == filename
                and progress.get("downloaded_bytes", 0) > 0
            ):
                return result
            await anyio.sleep(0.05)


def environment(tmp_path, candidate, mirror):
    home = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not home:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    return {
        **os.environ,
        "JAVA_HOME": home,
        "PATH": str(Path(home) / "bin") + os.pathsep + os.environ.get("PATH", ""),
        "JOLINK_WORKER_JAVA_HOME": str(candidate.select_worker_java().home),
        "JOLINK_DOWNLOAD_MIRROR": mirror,
        "MAVEN_ARGS": "--offline",
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "JOLINK_LOG_LEVEL": "INFO",
    }


def make_test_project(tmp_path, http_port=None):
    project = tmp_path / "project"
    source = project / "src/test/java/example/ReadyTest.java"
    source.parent.mkdir(parents=True)
    idea = project / ".idea"
    idea.mkdir()
    settings = ET.Element("project")
    component = ET.SubElement(settings, "component", name="MavenImportPreferences")
    option = ET.SubElement(component, "option", name="generalSettings")
    general = ET.SubElement(option, "MavenGeneralSettings")
    ET.SubElement(
        general,
        "option",
        name="localRepository",
        value=str(Path.home() / ".m2/repository"),
    )
    if http_port is not None:
        manager = ET.SubElement(settings, "component", name="RunManager")
        launch = ET.SubElement(
            manager,
            "configuration",
            name="Preparation",
            type="Application",
            factoryName="Application",
        )
        ET.SubElement(launch, "option", name="MAIN_CLASS_NAME", value="example.App")
        main = project / "src/main/java/example/App.java"
        main.parent.mkdir(parents=True)
        main.write_text(
            """package example; public class App {
public static void main(String[] args) throws Exception {
 com.sun.net.httpserver.HttpServer s=com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1", PORT),0);
 s.createContext("/", e -> {byte[] b="42".getBytes("UTF-8");e.sendResponseHeaders(200,b.length);e.getResponseBody().write(b);e.close();});s.start();
}}""".replace("PORT", str(http_port))
        )
    (idea / "workspace.xml").write_text(ET.tostring(settings, encoding="unicode"))
    source.write_text(
        "package example; public class ReadyTest { @org.junit.Test public void ready(){org.junit.Assert.assertEquals(42, 40+2);} }"
    )
    (project / "pom.xml").write_text("""<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>preparation</artifactId><version>1</version>
<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version></plugin></plugins></build></project>""")
    return project


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("action", ["test", "launch"])
@pytest.mark.parametrize("private_jdk", [False, True])
def test_mcp_prepares_before_request_and_continues_test(tmp_path, action, private_jdk):
    require_real_mcp_java_e2e()
    candidate = JdtCandidate.load_product()
    archive = None
    if private_jdk:
        supplied = os.environ.get("JOLINK_TEST_WORKER_JDK_ARCHIVE")
        if not supplied:
            pytest.skip(
                "set JOLINK_TEST_WORKER_JDK_ARCHIVE to this host's pinned JDK archive"
            )
        archive = Path(supplied)
    port = reserve_local_port() if action == "launch" else None
    project = make_test_project(tmp_path, port)
    arguments = {"action": action, "project_path": str(project)}
    if action == "test":
        arguments["tests"] = ["example.ReadyTest"]
    else:
        arguments.update(
            launch_name="Preparation",
            jdwp_port=reserve_local_port(),
            ready_port=port,
            startup_wait_timeout_seconds=5,
        )

    blocked_index = len(candidate.lock["artifacts"]) if private_jdk else 0
    with paused_mirror(candidate, blocked_index, archive) as (
        mirror,
        release,
        requests,
        names,
    ):
        env = environment(tmp_path, candidate, mirror)
        if private_jdk:
            env.pop("JOLINK_WORKER_JAVA_HOME", None)

        async def scenario():
            async with mcp(tmp_path, env, "request") as session:
                observed = await wait_progress(session, names[blocked_index])
                progress = observed["runtime_preparation"]
                assert progress["state"] == "preparing"
                assert progress["phase"] == (
                    "download_jdk" if private_jdk else "download_jdt"
                )
                assert progress["total_files"] == (1 if private_jdk else len(names))
                assert progress["downloaded_bytes"] < progress["total_bytes"]
                with anyio.fail_after(8):
                    started = dict(
                        (
                            await session.call_tool(
                                "java_application",
                                arguments,
                            )
                        ).structuredContent
                    )
                assert started["runtime_preparation"]["state"] == "preparing", started
                assert started.get("test_run_id") or started.get("attempt_id")
                release.set()
                with anyio.fail_after(120):
                    while True:
                        final = await status(session)
                        result = (
                            final.get("fast_test", {}) if action == "test" else final
                        )
                        if action == "test":
                            if result.get("status") in {
                                "completed",
                                "failed",
                                "cancelled",
                            }:
                                break
                        elif result.get("launch_phase") in {"runtime_active", "failed"}:
                            break
                        await anyio.sleep(0.1)
                (tmp_path / "result.json").write_text(json.dumps(result, indent=2))
                if action == "test":
                    assert result.get("passed") is True, json.dumps(result)
                    assert result["test_run_id"] == started["test_run_id"]
                else:
                    assert result.get("launch_phase") == "runtime_active", json.dumps(
                        result
                    )
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=5
                    )
                    try:
                        connection.request("GET", "/")
                        response = connection.getresponse()
                        assert response.status == 200 and response.read() == b"42"
                    finally:
                        connection.close()
                    await session.call_tool("java_application", {"action": "stop"})
                assert "runtime_preparation" not in final
            assert all(requests[name] == 1 for name in names)
            before = requests.copy()
            async with mcp(tmp_path, env, "cached") as session:
                assert "runtime_preparation" not in await status(session)
            assert requests == before

        anyio.run(scenario)


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("with_request", [False, True])
def test_mcp_close_during_download_preserves_completed_jars(tmp_path, with_request):
    require_real_mcp_java_e2e()
    candidate = JdtCandidate.load_product()
    project = make_test_project(tmp_path) if with_request else None
    with paused_mirror(candidate, 2) as (mirror, release, requests, names):
        env = environment(tmp_path, candidate, mirror)

        async def scenario():
            async with mcp(tmp_path, env, "interrupted") as session:
                observed = await wait_progress(session, names[2])
                assert observed["runtime_preparation"]["completed_files"] == 2
                if project is not None:
                    started = dict(
                        (
                            await session.call_tool(
                                "java_application",
                                {
                                    "action": "test",
                                    "project_path": str(project),
                                    "tests": ["example.ReadyTest"],
                                },
                            )
                        ).structuredContent
                    )
                    assert started["runtime_preparation"]["state"] == "preparing"
                before = time.monotonic()
            assert time.monotonic() - before < 5
            cache = (
                tmp_path
                / "home/.cache/jolink-runtime/jdt-worker/candidates"
                / candidate.candidate_id
                / "plugins"
            )
            assert (cache / names[0]).is_file() and (cache / names[1]).is_file()
            release.set()
            async with mcp(tmp_path, env, "resumed") as session:
                with anyio.fail_after(30):
                    while "runtime_preparation" in await status(session):
                        await anyio.sleep(0.1)
                    # A completed candidate has its bundled config installed.
                    while not list(cache.parent.glob("*/configuration/config.ini")):
                        await anyio.sleep(0.1)
            assert requests[names[0]] == requests[names[1]] == 1
            assert requests[names[2]] == 2

        anyio.run(scenario)
