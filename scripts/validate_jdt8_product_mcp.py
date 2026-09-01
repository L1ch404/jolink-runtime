#!/usr/bin/env python3
"""Validate the Java 8 product JDT lifecycle through the real MCP boundary."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


APP_SOURCE = """\
package example;

import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;

public final class HotReloadApp {
    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(args[0]);
        try (ServerSocket server = new ServerSocket(port)) {
            while (true) {
                try (Socket socket = server.accept();
                     OutputStream output = socket.getOutputStream()) {
                    output.write((message() + "\\n").getBytes(StandardCharsets.UTF_8));
                }
            }
        }
    }

    private static String message() {
        return "before";
    }
}
"""


def _reserve_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _write_project(
    project: Path,
    *,
    maven_home: Path,
    ready_port: int,
) -> Path:
    source = project / "src/main/java/example/HotReloadApp.java"
    source.parent.mkdir(parents=True)
    source.write_text(APP_SOURCE, encoding="utf-8")
    (project / "pom.xml").write_text(
        """\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>jolink-jdt8-product-e2e</artifactId>
  <version>1.0.0</version>
  <properties>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
    <maven.compiler.source>1.8</maven.compiler.source>
    <maven.compiler.target>1.8</maven.compiler.target>
  </properties>
</project>
""",
        encoding="utf-8",
    )
    maven_config = project / ".mvn/maven.config"
    maven_config.parent.mkdir()
    maven_config.write_text("-o\n", encoding="utf-8")
    workspace = project / ".idea/workspace.xml"
    workspace.parent.mkdir()
    workspace.write_text(
        f"""\
<project version="4">
  <component name="MavenImportPreferences">
    <option name="generalSettings">
      <MavenGeneralSettings>
        <option name="customMavenHome" value="{maven_home}" />
        <option name="mavenHomeTypeForPersistence" value="CUSTOM" />
      </MavenGeneralSettings>
    </option>
  </component>
</project>
""",
        encoding="utf-8",
    )
    run_config = project / ".run/HotReloadApp.xml"
    run_config.parent.mkdir()
    run_config.write_text(
        f"""\
<component name="ProjectRunConfigurationManager">
  <configuration name="HotReloadApp" type="Application" factoryName="Application">
    <option name="MAIN_CLASS_NAME" value="example.HotReloadApp" />
    <option name="WORKING_DIRECTORY" value="$PROJECT_DIR$" />
    <option name="PROGRAM_PARAMETERS" value="{ready_port}" />
    <method v="2"><option name="Make" enabled="true" /></method>
  </configuration>
</component>
""",
        encoding="utf-8",
    )
    return source


async def _payload(
    session: ClientSession,
    tool: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    result = await session.call_tool(tool, arguments)
    return dict(result.structuredContent or {})


async def _wait_status(
    session: ClientSession,
    predicate,
    *,
    timeout: float = 120,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while True:
        status = await _payload(session, "java_status", {"action": "status"})
        if status.get("launch_phase") == "failed":
            raise RuntimeError(status)
        if predicate(status):
            return status
        if time.monotonic() >= deadline:
            raise TimeoutError(status)
        await anyio.sleep(0.1)


def _message(port: int) -> str:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        return client.recv(128).decode("utf-8").strip()


async def _run(
    *,
    repository: Path,
    project: Path,
    source: Path,
    jdk8_home: Path,
    ready_port: int,
    jdwp_port: int,
) -> dict[str, object]:
    environment = dict(os.environ)
    environment["JAVA_HOME"] = str(jdk8_home)
    environment["PATH"] = str(jdk8_home / "bin") + os.pathsep + environment.get(
        "PATH", ""
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jolink_runtime.transport.stdio"],
        cwd=repository,
        env=environment,
    )
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        async with stdio_client(parameters, errlog=stderr) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                launch = await _payload(
                    session,
                    "java_application",
                    {
                        "action": "launch",
                        "project_path": str(project),
                        "launch_name": "HotReloadApp",
                        "jdwp_port": jdwp_port,
                        "ready_port": ready_port,
                        "startup_wait_timeout_seconds": 10,
                    },
                )
                if launch.get("ok") is not True:
                    raise RuntimeError(launch)
                status = await _wait_status(
                    session,
                    lambda value: value.get("compile_ready") is True,
                )
                if status.get("jdt_worker") != {
                    "java_major": 8,
                    "data_model": 64,
                }:
                    raise RuntimeError(status)
                if _message(ready_port) != "before":
                    raise RuntimeError("initial behavior mismatch")

                source.write_text(
                    source.read_text(encoding="utf-8").replace(
                        'return "before";', 'return "after";'
                    ),
                    encoding="utf-8",
                )
                hot = await _payload(
                    session,
                    "java_application",
                    {
                        "action": "reload",
                        "source_files": [
                            "src/main/java/example/HotReloadApp.java"
                        ],
                    },
                )
                hot_id = hot.get("reload_id")
                if hot.get("status") != "reload_started" or not hot_id:
                    raise RuntimeError(hot)
                hot_status = await _wait_status(
                    session,
                    lambda value: value.get("active_operation") is None
                    and value.get("last_reload", {}).get("reload_id") == hot_id,
                )
                hot = hot_status["last_reload"]
                if hot.get("ok") is not True or hot.get("apply_method") != "hotswap":
                    raise RuntimeError(hot_status)
                if _message(ready_port) != "after":
                    raise RuntimeError("HotSwap behavior mismatch")
                status = await _payload(session, "java_status", {"action": "status"})
                hot_generation = status["generation"]
                old_pid = status["pid"]

                restart = await _payload(
                    session, "java_application", {"action": "restart"}
                )
                if restart.get("ok") is not True:
                    raise RuntimeError(restart)
                status = await _wait_status(
                    session,
                    lambda value: value.get("launch_phase") == "runtime_active"
                    and value.get("pid") != old_pid,
                )
                if status.get("generation") != hot_generation:
                    raise RuntimeError(status)
                if _message(ready_port) != "after":
                    raise RuntimeError("restart lost HotSwap generation")

                source.write_text(
                    source.read_text(encoding="utf-8")
                    .replace(
                        "public final class HotReloadApp {",
                        "public final class HotReloadApp {\n"
                        '    private static final String VERSION = "structural";',
                    )
                    .replace('return "after";', "return VERSION;"),
                    encoding="utf-8",
                )
                old_pid = status["pid"]
                structural = await _payload(
                    session,
                    "java_application",
                    {
                        "action": "reload",
                        "source_files": [
                            "src/main/java/example/HotReloadApp.java"
                        ],
                        "hotswap": False,
                    },
                )
                if structural.get("ok") is not True:
                    raise RuntimeError(structural)
                status = await _wait_status(
                    session,
                    lambda value: value.get("pid") != old_pid
                    and value.get("active_operation") is None,
                )
                if status.get("last_reload", {}).get("applied") is not True:
                    raise RuntimeError(status)
                if _message(ready_port) != "structural":
                    raise RuntimeError("Candidate restart behavior mismatch")
                structural_generation = status["generation"]

                source.write_text(
                    source.read_text(encoding="utf-8").replace(
                        "int port = Integer.parseInt(args[0]);",
                        'if (true) { throw new IllegalStateException("fail"); }\n'
                        "        int port = Integer.parseInt(args[0]);",
                    ),
                    encoding="utf-8",
                )
                old_pid = status["pid"]
                rollback = await _payload(
                    session,
                    "java_application",
                    {
                        "action": "reload",
                        "source_files": [
                            "src/main/java/example/HotReloadApp.java"
                        ],
                        "hotswap": False,
                    },
                )
                if rollback.get("ok") is not True:
                    raise RuntimeError(rollback)
                status = await _wait_status(
                    session,
                    lambda value: value.get("pid") != old_pid
                    and value.get("active_operation") is None,
                )
                last = status.get("last_reload", {})
                if last.get("applied") is not False or last.get("rolled_back") is not True:
                    raise RuntimeError(status)
                if status.get("generation") != structural_generation:
                    raise RuntimeError(status)
                if _message(ready_port) != "structural":
                    raise RuntimeError("rollback did not restore last-good behavior")
                await _payload(session, "java_application", {"action": "stop"})
                return {
                    "worker": status["jdt_worker"],
                    "hot_reload_ms": hot.get("compile_ms"),
                    "restart_persisted": True,
                    "candidate_restart": True,
                    "rollback": last,
                }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jdk8-home", type=Path, required=True)
    parser.add_argument("--maven-home", type=Path, required=True)
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args()
    ready_port = _reserve_port()
    jdwp_port = _reserve_port()
    with tempfile.TemporaryDirectory(prefix="jolink-jdt8-mcp-") as raw:
        project = Path(raw) / "project"
        source = _write_project(
            project,
            maven_home=args.maven_home,
            ready_port=ready_port,
        )
        async def scenario() -> dict[str, object]:
            return await _run(
                repository=args.repository,
                project=project,
                source=source,
                jdk8_home=args.jdk8_home,
                ready_port=ready_port,
                jdwp_port=jdwp_port,
            )

        result = anyio.run(scenario)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
