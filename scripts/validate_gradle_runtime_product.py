#!/usr/bin/env python3
"""Validate Gradle Runtime launch, HotSwap, restart candidate, and rollback."""

from __future__ import annotations

import argparse
import importlib.util
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


ROOT = Path(__file__).resolve().parents[1]
G1 = ROOT / "experiments/gradle-build-world-probe/run_gradle_probe_spike.py"

APP = """\
package example;

import java.io.OutputStream;
import java.io.InputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;

public final class GradleRuntimeApp {
    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(args[0]);
        ServerSocket server = new ServerSocket(port);
        while (true) {
            Socket socket = server.accept();
            OutputStream output = socket.getOutputStream();
            output.write(message().concat(":").concat(resource())
                    .concat("\\n").getBytes(StandardCharsets.UTF_8));
            output.close();
            socket.close();
        }
    }

    private static String message() {
        return "before";
    }

    private static String resource() throws Exception {
        InputStream input = GradleRuntimeApp.class.getClassLoader()
                .getResourceAsStream("message.txt");
        byte[] data = new byte[64];
        int length = input.read(data);
        input.close();
        return new String(data, 0, length, StandardCharsets.UTF_8).trim();
    }
}
"""


def _load_g1():
    spec = importlib.util.spec_from_file_location("jolink_gradle_fixture", G1)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load Gradle fixture helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _message(port: int) -> str:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        return client.recv(128).decode("utf-8").strip()


async def _payload(
    session: ClientSession,
    tool: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    result = await session.call_tool(tool, arguments)
    return dict(result.structuredContent or {})


async def _status(
    session: ClientSession,
    predicate,
    *,
    timeout: float = 120,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await _payload(session, "java_status", {"action": "status"})
        active = status.get("active_operation")
        reload_active = isinstance(active, dict) and active.get("operation") == "reload"
        if status.get("launch_phase") == "failed" and not reload_active:
            raise RuntimeError(status)
        if status.get("jdt_bootstrap_state") == "unavailable":
            raise RuntimeError(status)
        if predicate(status):
            return status
        await anyio.sleep(0.1)
    raise TimeoutError(status)


async def _failed_status(
    session: ClientSession,
    *,
    timeout: float = 120,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await _payload(session, "java_status", {"action": "status"})
        if status.get("launch_phase") == "failed":
            return status
        await anyio.sleep(0.1)
    raise TimeoutError(status)


def _write_project(project: Path, port: int, *, target_java: int) -> Path:
    source = project / "src/main/java/example/GradleRuntimeApp.java"
    source.parent.mkdir(parents=True)
    source.write_text(APP, encoding="utf-8")
    broken_test = project / "src/test/java/example/BrokenTest.java"
    broken_test.parent.mkdir(parents=True)
    broken_test.write_text(
        "package example; class BrokenTest { this does not compile }\n",
        encoding="utf-8",
    )
    resource = project / "src/main/resources/message.txt"
    resource.parent.mkdir(parents=True)
    resource.write_text("resource-v1\n", encoding="utf-8")
    (project / "settings.gradle").write_text(
        "rootProject.name = 'jolink-gradle-runtime'\n", encoding="utf-8"
    )
    java_version = "VERSION_1_8" if target_java == 8 else "VERSION_11"
    (project / "build.gradle").write_text(
        f"""\
plugins {{
    id 'application'
}}

java {{
    sourceCompatibility = JavaVersion.{java_version}
    targetCompatibility = JavaVersion.{java_version}
    toolchain {{ languageVersion = JavaLanguageVersion.of({target_java}) }}
}}

application {{
    mainClass = 'example.GradleRuntimeApp'
}}

dependencies {{
    testImplementation 'example.private:missing-test-only:1.0'
}}

test {{
    systemProperty 'jolink.secret.token', 'must-not-be-exported'
    environment 'JOLINK_TEST_SECRET', 'must-not-be-exported'
}}

tasks.withType(JavaCompile).configureEach {{
    options.encoding = 'UTF-8'
}}
""",
        encoding="utf-8",
    )
    run = project / ".run/GradleRuntimeApp.xml"
    run.parent.mkdir()
    run.write_text(
        f"""\
<component name="ProjectRunConfigurationManager">
  <configuration name="GradleRuntimeApp" type="Application"
                 factoryName="Application">
    <option name="MAIN_CLASS_NAME" value="example.GradleRuntimeApp" />
    <option name="WORKING_DIRECTORY" value="$PROJECT_DIR$" />
    <option name="PROGRAM_PARAMETERS" value="{port}" />
    <method v="2"><option name="Make" enabled="true" /></method>
  </configuration>
</component>
""",
        encoding="utf-8",
    )
    return source


async def _run(
    *,
    project: Path,
    source: Path,
    environment: dict[str, str],
    ready_port: int,
    jdwp_port: int,
    offline: bool,
) -> dict[str, object]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jolink_runtime.transport.stdio"],
        cwd=ROOT,
        env=environment,
    )
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        async with stdio_client(parameters, errlog=stderr) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                launched = await _payload(
                    session,
                    "java_application",
                    {
                        "action": "launch",
                        "project_path": str(project),
                        "launch_name": "GradleRuntimeApp",
                        "jdwp_port": jdwp_port,
                        "ready_port": ready_port,
                        "startup_wait_timeout_seconds": 20,
                    },
                )
                if launched.get("ok") is not True:
                    raise RuntimeError(launched)
                status = await _status(
                    session,
                    lambda value: value.get("compile_ready") is True,
                )
                if status.get("startup_state") != "ready":
                    raise RuntimeError(status)
                if status.get("fast_update", {}).get("build_system") != "gradle":
                    raise RuntimeError(status)
                if status.get("fast_update", {}).get("offline") is not offline:
                    raise RuntimeError(status)
                build_log = Path(status["build"]["log_tail"]["log_file"])
                private_model = (
                    build_log.parent
                    / "gradle-probe/gradle-build-world.private.json"
                )
                if private_model.exists():
                    raise RuntimeError("Gradle private model was retained")
                if _message(ready_port) != "before:resource-v1":
                    raise RuntimeError("Gradle baseline behavior mismatch")

                formal_resource = project / "build/resources/main/message.txt"
                formal_resource.write_text(
                    "resource-external\n", encoding="utf-8"
                )
                old_pid = int(status["pid"])
                restarted = await _payload(
                    session, "java_application", {"action": "restart"}
                )
                if restarted.get("ok") is not True:
                    raise RuntimeError(restarted)
                status = await _status(
                    session,
                    lambda value: value.get("launch_phase") == "runtime_active"
                    and value.get("pid") != old_pid,
                )
                if _message(ready_port) != "before:resource-v1":
                    raise RuntimeError("Formal resource mutation escaped sealing")
                formal_resource.unlink()
                old_pid = int(status["pid"])
                restarted = await _payload(
                    session, "java_application", {"action": "restart"}
                )
                if restarted.get("ok") is not True:
                    raise RuntimeError(restarted)
                status = await _status(
                    session,
                    lambda value: value.get("launch_phase") == "runtime_active"
                    and value.get("pid") != old_pid,
                )
                if _message(ready_port) != "before:resource-v1":
                    raise RuntimeError("Deleted formal resource escaped sealing")
                formal_resource.parent.mkdir(parents=True, exist_ok=True)
                formal_resource.write_text("resource-v1\n", encoding="utf-8")

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
                        "source_files": ["src/main/java/example/GradleRuntimeApp.java"],
                    },
                )
                hot_id = hot.get("reload_id")
                if hot.get("status") != "reload_started" or not hot_id:
                    raise RuntimeError(hot)
                hot_status = await _status(
                    session,
                    lambda value: value.get("active_operation") is None
                    and value.get("last_reload", {}).get("reload_id") == hot_id,
                )
                hot = hot_status["last_reload"]
                if hot.get("ok") is not True or hot.get("apply_method") != "hotswap":
                    raise RuntimeError(hot_status)
                if _message(ready_port) != "after:resource-v1":
                    raise RuntimeError("Gradle HotSwap behavior mismatch")

                source.write_text(
                    source.read_text(encoding="utf-8")
                    .replace(
                        "public final class GradleRuntimeApp {",
                        "public final class GradleRuntimeApp {\n"
                        '    private static final String VERSION = "structural";',
                    )
                    .replace('return "after";', "return VERSION;"),
                    encoding="utf-8",
                )
                old_pid = int(status["pid"])
                structural = await _payload(
                    session,
                    "java_application",
                    {
                        "action": "reload",
                        "source_files": ["src/main/java/example/GradleRuntimeApp.java"],
                        "hotswap": False,
                    },
                )
                if structural.get("ok") is not True:
                    raise RuntimeError(structural)
                status = await _status(
                    session,
                    lambda value: (
                        value.get("active_operation") is None
                        and value.get("pid") != old_pid
                    ),
                )
                if _message(ready_port) != "structural:resource-v1":
                    raise RuntimeError("Gradle Candidate restart mismatch")

                source.write_text(
                    source.read_text(encoding="utf-8").replace(
                        "int port = Integer.parseInt(args[0]);",
                        "if (System.currentTimeMillis() >= 0) {\n"
                        '            throw new IllegalStateException("candidate");\n'
                        "        }\n"
                        "        int port = Integer.parseInt(args[0]);",
                    ),
                    encoding="utf-8",
                )
                failed_candidate = await _payload(
                    session,
                    "java_application",
                    {
                        "action": "reload",
                        "source_files": ["src/main/java/example/GradleRuntimeApp.java"],
                        "hotswap": False,
                    },
                )
                if failed_candidate.get("ok") is not True:
                    raise RuntimeError(failed_candidate)
                rolled_back = await _status(
                    session,
                    lambda value: (
                        value.get("active_operation") is None
                        and isinstance(value.get("last_reload"), dict)
                        and value["last_reload"].get("rolled_back") is True
                    ),
                )
                if _message(ready_port) != "structural:resource-v1":
                    raise RuntimeError("Gradle rollback did not restore behavior")

                resource_source = project / "src/main/resources/message.txt"
                resource_source.write_text("resource-v2\n", encoding="utf-8")
                stale = await _payload(
                    session,
                    "java_application",
                    {
                        "action": "reload",
                        "source_files": [
                            "src/main/java/example/GradleRuntimeApp.java"
                        ],
                    },
                )
                if stale.get("error_code") != "JDT_BUILD_WORLD_STALE":
                    raise RuntimeError(stale)

                stopped = await _payload(
                    session, "java_application", {"action": "stop"}
                )
                if stopped.get("ok") is not True:
                    raise RuntimeError(stopped)
                build_file = project / "build.gradle"
                with build_file.open("a", encoding="utf-8") as stream:
                    stream.write(
                        """
compileJava.doLast {
    def target = file("$buildDir/classes/java/main/example/GradleRuntimeApp.class")
    target << (byte) 0
}
"""
                    )
                rejected = await _payload(
                    session,
                    "java_application",
                    {
                        "action": "launch",
                        "project_path": str(project),
                        "launch_name": "GradleRuntimeApp",
                        "jdwp_port": jdwp_port,
                        "ready_port": ready_port,
                        "startup_wait_timeout_seconds": 20,
                    },
                )
                if rejected.get("ok") is not True:
                    raise RuntimeError(rejected)
                failed = await _failed_status(session)
                if (
                    failed.get("launch_error", {}).get("error_code")
                    != "GRADLE_BYTECODE_TRANSFORM_UNMODELED"
                ):
                    raise RuntimeError(failed)
                return {
                    "ok": True,
                    "build_system": "gradle",
                    "target_java": int(status["fast_update"]["target_level"]),
                    "baseline_ready": True,
                    "hotswap_passed": True,
                    "candidate_restart_passed": True,
                    "rollback_passed": rolled_back["last_reload"]["rolled_back"],
                    "resources_sealed": True,
                    "resource_drift_rejected": True,
                    "runtime_probe_ignored_test_world": True,
                    "private_model_deleted": True,
                    "bytecode_transform_rejected": True,
                    "final_process_state": "absent",
                }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gradle", type=Path, required=True)
    parser.add_argument("--gradle-version", required=True)
    parser.add_argument("--java8-home", type=Path, required=True)
    parser.add_argument("--java11-home", type=Path, required=True)
    parser.add_argument("--java17-home", type=Path, required=True)
    parser.add_argument("--target-java", type=int, choices=(8, 11), default=11)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    g1 = _load_g1()
    gradle = args.gradle.expanduser().resolve(strict=True)
    java11 = args.java11_home.expanduser().resolve(strict=True)
    java17 = args.java17_home.expanduser().resolve(strict=True)
    environment = {
        **os.environ,
        "JAVA_HOME": str(java17),
        "PATH": str(java17 / "bin") + os.pathsep + os.environ.get("PATH", ""),
    }
    if args.offline:
        environment["GRADLE_ARGS"] = "--offline"
    with tempfile.TemporaryDirectory(prefix="jolink-gradle-runtime-") as raw:
        root = Path(raw)
        environment["GRADLE_USER_HOME"] = str(root / "gradle-user-home")
        os.environ["GRADLE_USER_HOME"] = environment["GRADLE_USER_HOME"]
        project = root / "project"
        project.mkdir()
        ready_port = _port()
        jdwp_port = _port()
        while jdwp_port == ready_port:
            jdwp_port = _port()
        source = _write_project(project, ready_port, target_java=args.target_java)
        distribution = g1.private_distribution_zip(
            root, gradle=gradle, version=args.gradle_version
        )
        g1.create_wrapper(
            project,
            gradle=gradle,
            version=args.gradle_version,
            distribution_zip=distribution,
            environment=environment,
        )
        # Toolchain discovery is local and deterministic for the private fixture.
        properties = project / "gradle.properties"
        target_home = (
            args.java8_home.expanduser().resolve(strict=True)
            if args.target_java == 8
            else java11
        )
        properties.write_text(
            "org.gradle.java.installations.auto-download=false\n"
            f"org.gradle.java.installations.paths={target_home}\n",
            encoding="utf-8",
        )

        async def scenario() -> dict[str, object]:
            return await _run(
                project=project,
                source=source,
                environment=environment,
                ready_port=ready_port,
                jdwp_port=jdwp_port,
                offline=args.offline,
            )

        result = anyio.run(scenario)
        print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
