"""Native Maven root configuration and nested aggregation through public MCP."""

import json
import os
import shutil
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
@pytest.mark.parametrize("layout", ["nested", "profile"])
def test_child_entry_uses_root_configuration_and_preserves_target(tmp_path, layout):
    require_real_mcp_java_e2e()
    root, app, pom, _, port = project_fixture(tmp_path, "maven", True)
    if layout == "nested":
        group = root / "modules"
        group.mkdir()
        for name in ("app", "lib"):
            old = root / name
            old.rename(group / name)
            file = group / name / "pom.xml"
            file.write_text(file.read_text().replace("</parent>", "<relativePath>../../pom.xml</relativePath></parent>"))
        (group / "pom.xml").write_text('''<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>group</artifactId><version>1</version><packaging>pom</packaging>
<modules><module>lib</module><module>app</module></modules></project>''')
        pom.write_text(pom.read_text().replace("<module>lib</module><module>app</module>", "<module>modules</module>"))
        app = group / "app"
    else:
        xml = ET.parse(pom)
        modules = xml.getroot().find("{*}modules")
        xml.getroot().remove(modules)
        profile = ET.SubElement(ET.SubElement(xml.getroot(), "profiles"), "profile")
        ET.SubElement(profile, "id").text = "services"
        profile.append(modules)
        xml.write(pom, encoding="utf-8")
    config = root / ".mvn/maven.config"
    config.parent.mkdir()
    prefix = "-Pservices\n" if layout == "profile" else ""
    config.write_text(prefix + "-Dexpected=A\n")
    # The module has no Wrapper; both flows must select the enclosing build's.
    marker = root / "wrapper-used"
    maven = shutil.which("mvn.cmd" if os.name == "nt" else "mvn")
    if maven is None:
        pytest.skip("Maven is required")
    wrapper = root / ("mvnw.cmd" if os.name == "nt" else "mvnw")
    wrapper.write_text(
        f'@echo off\r\necho used>"{marker}"\r\ncall "{maven}" %*\r\n' if os.name == "nt"
        else f'#!/bin/sh\ntouch "{marker}"\nexec "{maven}" "$@"\n'
    )
    wrapper.chmod(0o755)
    # Test behavior depends on the root config, not a joLink-specific parameter.
    env = {**os.environ, "MAVEN_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache")}
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")

    async def scenario():
        for cycle in range(2):
            with temporary_stderr() as log:
                async with open_mcp_session(log, environment=env) as session:
                    async def call(tool, **args):
                        return dict((await session.call_tool(tool, args)).structuredContent)

                    async def run():
                        result = await call("java_fast_test", project_path=str(app), tests=["example.ReadyTest"])
                        with anyio.fail_after(120):
                            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                                await anyio.sleep(.2)
                                result = await call("java_fast_test", action="result", test_run_id=result["test_run_id"])
                        if not result.get("passed"):
                            return await call("java_fast_test", action="result", test_run_id=result["test_run_id"])
                        return result

                    try:
                        result = await run()
                        assert result.get("passed") is True, json.dumps(result)
                        assert marker.is_file(), "Test must use the root Maven Wrapper"
                        marker.unlink()
                        assert (await run())["compiled_source_count"] == 0
                        assert not marker.exists(), "Unchanged test must reuse its model"
                        launched = await call("java_application", action="launch", project_path=str(app), main_class="example.App",
                                              ready_port=port, jdwp_port=reserve_local_port())
                        with anyio.fail_after(120):
                            while launched.get("launch_phase") not in {"runtime_active", "failed"} and launched.get("ok", True):
                                await anyio.sleep(.2)
                                launched = await call("java_status", action="status", details=True)
                        assert launched.get("startup_state") == "ready", json.dumps(launched)
                        assert marker.is_file(), "Launch must use the root Maven Wrapper"
                        assert response(port) == "A"
                        # Root config edits must invalidate both entry-point caches.
                        config.write_text(prefix + "-Dexpected=B\n-Dresource.folder=resourcesB\n")
                        result = await run()
                        assert result.get("passed") is True, json.dumps(result)
                        restarted = await call("java_application", action="restart", hotswap=False)
                        with anyio.fail_after(120):
                            while restarted.get("applied") is None and restarted.get("ok", True):
                                await anyio.sleep(.2)
                                restarted = (await call("java_status", action="status", details=True)).get("last_reload") or restarted
                        assert restarted.get("applied") is True, json.dumps(restarted)
                        assert response(port) == "B"
                    finally:
                        await call("java_application", action="stop")
            # Next MCP must notice root-only config changes too.
            config.write_text(prefix + "-Dexpected=A\n")
    anyio.run(scenario)
