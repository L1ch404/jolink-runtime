"""Measure real stdio MCP cold/warm launch and single-source reload."""

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


def port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def measure(repository: Path, jdk: Path, source_count: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="jolink-hot-path-") as raw:
        root = Path(raw)
        project = root / "project"
        sources = project / "src/main/java/example"
        sources.mkdir(parents=True)
        (project / "pom.xml").write_text('''<project>
<modelVersion>4.0.0</modelVersion><groupId>example</groupId>
<artifactId>hot-path</artifactId><version>1</version><properties>
<maven.compiler.source>1.8</maven.compiler.source>
<maven.compiler.target>1.8</maven.compiler.target>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
</properties></project>''', encoding="utf-8")
        app_port, debug_port = port(), port()
        (project / ".run").mkdir()
        (project / ".run/App.xml").write_text(f'''<component name="ProjectRunConfigurationManager">
<configuration name="App" type="Application" factoryName="Application">
<option name="MAIN_CLASS_NAME" value="example.App"/>
<option name="PROGRAM_PARAMETERS" value="{app_port}"/>
<option name="WORKING_DIRECTORY" value="$PROJECT_DIR$"/>
<method v="2"><option name="Make" enabled="true"/></method>
</configuration></component>''', encoding="utf-8")
        (sources / "App.java").write_text('''package example;
public class App {
 public static void main(String[] args) throws Exception {
  java.net.ServerSocket server = new java.net.ServerSocket(Integer.parseInt(args[0]));
  while (true) { java.net.Socket client = server.accept();
   client.getOutputStream().write(String.valueOf(Helper.value()).getBytes("UTF-8"));
   client.close();
  }
 }
}''', encoding="utf-8")
        helper = sources / "Helper.java"
        def edit(value: int) -> None:
            helper.write_text(
                f"package example; public class Helper {{ public static int value() {{ return {value}; }} }}",
                encoding="utf-8",
            )
        edit(1)
        for index in range(max(0, source_count - 2)):
            (sources / f"Leaf{index}.java").write_text(
                f"package example; public class Leaf{index} {{ public int value() {{ return {index}; }} }}",
                encoding="utf-8",
            )
        environment = {
            **os.environ, "JAVA_HOME": str(jdk),
            "PATH": str(jdk / "bin") + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONPATH": str(repository / "src"),
            "XDG_CACHE_HOME": str(root / "cache"),
        }
        report = {"source_count": source_count, "launches": [], "reloads": []}
        for run in range(2):
            parameters = StdioServerParameters(
                command=sys.executable, args=["-m", "jolink_runtime.transport.stdio"],
                cwd=str(repository), env=environment,
            )
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
                async with stdio_client(parameters, errlog=stderr) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        async def call(tool, args):
                            value = dict((await session.call_tool(tool, args)).structuredContent or {})
                            if value.get("ok") is not True:
                                raise RuntimeError(value)
                            return value
                        started = time.monotonic()
                        await call("java_application", {
                            "action": "launch", "project_path": str(project),
                            "launch_name": "App", "jdwp_port": debug_port,
                            "ready_port": app_port, "startup_wait_timeout_seconds": 10,
                        })
                        with anyio.fail_after(240):
                            while True:
                                state = await call("java_status", {"action": "status"})
                                if state.get("launch_phase") == "failed":
                                    raise RuntimeError(state)
                                if state.get("launch_phase") == "runtime_active":
                                    break
                                await anyio.sleep(0.05)
                        report["launches"].append({
                            "wall_ms": round((time.monotonic() - started) * 1000, 1),
                            "cache_reused": state.get("probe_cache_reused"),
                            "build_kind": state.get("jdt_bootstrap_build_kind"),
                            "timing_ms": state.get("product_timing_ms"),
                        })
                        def read_value():
                            with socket.create_connection(("127.0.0.1", app_port), timeout=5) as client:
                                return client.recv(64).decode()
                        assert await anyio.to_thread.run_sync(read_value) == "1"
                        if run == 0:
                            for value in (2, 3, 1):
                                edit(value)
                                started = time.monotonic()
                                accepted = await call("java_application", {
                                    "action": "reload", "source_files": ["src/main/java/example/Helper.java"],
                                })
                                with anyio.fail_after(120):
                                    while True:
                                        state = await call("java_status", {"action": "status"})
                                        result = state.get("last_reload")
                                        if result and result.get("reload_id") == accepted["reload_id"]:
                                            break
                                        await anyio.sleep(0.01)
                                assert result.get("applied") is True, result
                                actual = await anyio.to_thread.run_sync(read_value)
                                assert actual == str(value), json.dumps({
                                    "expected": value, "actual": actual, "reload": result,
                                })
                                report["reloads"].append({
                                    "wall_ms": round((time.monotonic() - started) * 1000, 1),
                                    **{key: result.get(key) for key in (
                                        "compile_ms", "compile_total_ms", "jdt_build_ms", "diagnostics_ms",
                                        "apply_ms", "total_ms", "compiled_source_count"
                                    )},
                                })
                        await call("java_application", {"action": "stop"})
        return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--jdk", type=Path, required=True)
    parser.add_argument("--sources", type=int, default=4000)
    arguments = parser.parse_args()
    print(json.dumps(anyio.run(measure, arguments.repository, arguments.jdk, arguments.sources)))


if __name__ == "__main__":
    main()
