"""Real MCP, Probe, JDT and HTTP: headless launch plus cached JDK changes."""

import http.client
import os
import shutil
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import anyio
import pytest
from java_support import REPOSITORY_ROOT, require_real_mcp_java_e2e, reserve_local_port
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_build_configuration_mcp import project_fixture


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("system,multi", [("maven", False), ("maven", True), ("gradle", False)])
def test_headless_launch_jdk_changes_cache_and_restart(tmp_path, system, multi):
    require_real_mcp_java_e2e()
    jdk8 = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    jdk17 = os.environ.get("JOLINK_TEST_JAVA17_HOME")
    if not jdk8 or not jdk17:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME and JOLINK_TEST_JAVA17_HOME")
    root, _app, _config, source, port = project_fixture(tmp_path, system, multi)
    shutil.rmtree(root / ".idea")
    text = source.read_text().replace(
        'byte[] b=value().getBytes("UTF-8")',
        'byte[] b=(value()+"|"+System.getProperty("java.specification.version")+"|"+args[0]+"|"+System.getProperty("demo.flag")+"|v1").getBytes("UTF-8")',
    )
    source.write_text(text)
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "jolink_runtime.transport.stdio"],
        cwd=REPOSITORY_ROOT,
        env={**os.environ, "JAVA_HOME": jdk17,
             "PATH": str(Path(jdk17) / "bin") + os.pathsep + os.environ["PATH"],
             "MAVEN_ARGS": "--offline", "GRADLE_ARGS": "--offline",
             "XDG_CACHE_HOME": str(tmp_path / "cache")},
    )

    def response():
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        try:
            connection.request("GET", "/")
            reply = connection.getresponse()
            assert reply.status == 200
            return reply.read().decode()
        finally:
            connection.close()

    async def scenario():
        for reopened in (False, True):
            if reopened:
                # Same launch/cache identity, but the launch settings now come from IDEA.
                idea = root / ".idea"
                idea.mkdir()
                document = ET.Element("project")
                manager = ET.SubElement(document, "component", name="RunManager")
                config = ET.SubElement(manager, "configuration", name="example.App", type="Application")
                for name, value in {
                    "MAIN_CLASS_NAME": "example.App", "ALTERNATIVE_JRE_PATH": jdk8,
                    "ALTERNATIVE_JRE_PATH_ENABLED": "true", "PROGRAM_PARAMETERS": "idea-new",
                    "VM_PARAMETERS": "-Ddemo.flag=idea-new",
                }.items():
                    ET.SubElement(config, "option", name=name, value=value)
                ET.ElementTree(document).write(idea / "workspace.xml", encoding="unicode")
            with (tmp_path / f"mcp-{reopened}.log").open("w+") as log:
                async with (stdio_client(parameters, errlog=log) as (r, w), ClientSession(r, w) as session):
                    await session.initialize()
                    tools = {t.name: t for t in (await session.list_tools()).tools}
                    assert "java_home" in tools["java_application"].inputSchema["properties"]
                    assert "java_home" not in tools["java_debugger"].inputSchema["properties"]

                    async def call(tool, **args):
                        with anyio.fail_after(40):
                            return dict((await session.call_tool(tool, args)).structuredContent)

                    async def details():
                        return await call("java_status", action="status", details=True)

                    async def launch(**overrides):
                        arguments = dict(action="launch", project_path=str(root), build_system=system,
                                         ready_port=port, jdwp_port=reserve_local_port())
                        arguments.update(dict(launch_name="example.App") if reopened else
                                         dict(main_class="example.App", app_args=["first"], vm_args=["-Ddemo.flag=first"]))
                        arguments.update(overrides)
                        result = await call("java_application", **arguments)
                        with anyio.fail_after(180):
                            while result.get("launch_phase") not in {"runtime_active", "failed"} and result.get("ok"):
                                await anyio.sleep(.2)
                                result = await details()
                        assert result.get("ok") and result.get("launch_phase") == "runtime_active", await details()
                        return await details()

                    try:
                        first = await launch()
                        if not reopened:
                            assert not first["probe_cache_reused"]
                            assert response() == "A|1.8|first|first|v1"
                            assert not (root / ".idea").exists()
                        else:
                            # Previous MCP saved JDK17; new IDEA settings must select JDK8.
                            assert first["probe_cache_reused"]
                            assert first["jdt_bootstrap_build_kind"] is None
                            assert response() == "A|1.8|idea-new|idea-new|v1"
                        await call("java_application", action="stop")
                        second = await launch(java_home=jdk17, app_args=["override"], vm_args=["-Ddemo.flag=override"])
                        assert second["probe_cache_reused"]
                        assert second["jdt_bootstrap_build_kind"] is None
                        assert response() == "A|17|override|override|v1"
                        if reopened:
                            source.write_text(text.replace('"|v1"', '"|v2"'))
                            result = await call("java_application", action="restart")
                            with anyio.fail_after(90):
                                while result.get("applied") is None and result.get("ok"):
                                    await anyio.sleep(.2)
                                    result = (await details()).get("last_reload") or result
                            assert result.get("applied") is True, result
                            assert response() == "A|17|override|override|v2"
                            result = await call("java_application", action="restart", hotswap=False)
                            with anyio.fail_after(90):
                                while result.get("applied") is None and result.get("ok"):
                                    await anyio.sleep(.2)
                                    result = (await details()).get("last_reload") or result
                            assert result.get("applied") is True and result["apply_method"] == "restart", result
                            assert response() == "A|17|override|override|v2"
                            tested = await call("java_fast_test", project_path=str(root), tests=["example.ReadyTest"])
                            with anyio.fail_after(180):
                                while tested.get("status") not in {"completed", "failed", "cancelled"}:
                                    await anyio.sleep(.2)
                                    tested = await call("java_fast_test", action="result", test_run_id=tested["test_run_id"])
                            assert tested.get("passed") is True, tested
                    finally:
                        await call("java_application", action="stop")
                        assert (await details())["process_state"] == "absent"

    anyio.run(scenario)
