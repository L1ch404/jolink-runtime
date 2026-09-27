"""Changing the selected config must refresh models even when file contents stay fixed."""

import json
import xml.etree.ElementTree as ET

import anyio
import pytest
from java_support import require_real_mcp_java_e2e
from test_build_configuration_mcp import project_fixture
from test_review_model_cache import client, launch, response, run_test, settings


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("change", ["settings", "profiles"])
@pytest.mark.parametrize("operation", ["launch", "restart", "test"])
@pytest.mark.parametrize("multi", [False, True])
def test_selected_build_preferences_refresh_and_reopen(tmp_path, change, operation, multi):
    require_real_mcp_java_e2e()
    root, app, pom, source, port = project_fixture(tmp_path, "maven", multi)
    text = pom.read_text().replace("<resource.folder>resourcesA</resource.folder>", "").replace("<expected>A</expected>", "")
    if change == "profiles":
        text = text.replace("</project>", "<profiles>" + "".join(
            f"<profile><id>{value}</id><properties><resource.folder>resources{value}</resource.folder>"
            f"<expected>{value}</expected><maven.compiler.parameters>{str(value == 'B').lower()}</maven.compiler.parameters>"
            "</properties></profile>" for value in ("A", "B")
        ) + "</profiles></project>")
        source.write_text(source.read_text().replace('byte[] b=value().getBytes("UTF-8")',
            'byte[] b=(value()+"|"+App.class.getMethod("echo",String.class).getParameters()[0].isNamePresent()).getBytes("UTF-8")')
            .replace(" public static void main", " public static String echo(String value) {return value;}\n public static void main"))
    pom.write_text(text)
    configs = [pom]
    for value in ("A", "B"):
        path = root / f"settings-{value}.xml"
        path.write_text(settings(value))
        configs.append(path)
    oracle = root / "expected.txt"
    test_source = app / "src/test/java/example/ReadyTest.java"
    test_source.write_text("""package example;
public class ReadyTest {@org.junit.Test public void resource() throws Exception {
 String expected=new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(ORACLE)),"UTF-8");
 org.junit.Assert.assertEquals(expected,System.getProperty("expected"));
 org.junit.Assert.assertEquals(expected,App.value());
}}""".replace("ORACLE", json.dumps(oracle.as_posix())))
    if change == "profiles":
        test_source.write_text(test_source.read_text().replace("org.junit.Assert.assertEquals(expected,App.value());",
            'org.junit.Assert.assertEquals(expected,App.value());\n'
            'org.junit.Assert.assertEquals("B".equals(expected), App.class.getMethod("echo",String.class).getParameters()[0].isNamePresent());'))
    unchanged = {p: p.read_bytes() for p in [*configs, source, test_source]}

    def select(value):
        workspace = root / ".idea/workspace.xml"
        xml = ET.parse(workspace)
        general = xml.getroot().find(".//MavenGeneralSettings")
        for option in list(general):
            if option.get("name") in {"userSettingsFile", "activeProfiles"}:
                general.remove(option)
        ET.SubElement(general, "option", name="userSettingsFile" if change == "settings" else "activeProfiles",
                      value=str(root / f"settings-{value}.xml") if change == "settings" else value)
        xml.write(workspace, encoding="unicode")
        oracle.write_text(value)
        assert all(p.read_bytes() == data for p, data in unchanged.items())

    async def scenario():
        cache = tmp_path / "cache"
        async def verify(call, value, reused):
            if operation in {"launch", "restart"}:
                try:
                    current = await call("java_status", {"action": "status"})
                    if operation == "restart" and current.get("running"):
                        result = await call("java_application", {"action": "restart"})
                        with anyio.fail_after(120):
                            while result.get("status") in {"restart_started", "restarting"} or result.get("applied") is None:
                                await anyio.sleep(.1)
                                details = await call("java_status", {"action": "status", "details": True})
                                if details.get("last_reload"):
                                    result = details["last_reload"]
                                elif details.get("launch_phase") == "runtime_active":
                                    result = details
                                    break
                        assert result.get("ok", True), result
                        result = await call("java_status", {"action": "status", "details": True})
                        assert result.get("startup_state") == "ready", result
                    else:
                        result = await launch(call, root, port)
                    expected = value + "|" + str(value == "B").lower() if change == "profiles" else value
                    assert response(port) == expected, result
                    if operation == "launch":
                        assert result["probe_cache_reused"] is reused, result
                finally:
                    if operation == "launch":
                        await call("java_application", {"action": "stop"})
            else:
                result = await run_test(call, root, "example.ReadyTest")
                if reused:
                    assert "freshness_ms" in result and result["compiled_source_count"] == 0, result
            return result

        select("A")
        async with client(tmp_path, "first", cache) as call:
            await verify(call, "A", False)
            await verify(call, "A", True)
            select("B")
            await verify(call, "B", False)
            await verify(call, "B", True)
            await call("java_application", {"action": "stop"})
        # Change only selection while the MCP is closed; disk recovery must notice.
        select("A")
        async with client(tmp_path, "changed-reopen", cache) as call:
            await verify(call, "A", False)
            await verify(call, "A", True)
            await call("java_application", {"action": "stop"})
        async with client(tmp_path, "unchanged-reopen", cache) as call:
            result = await verify(call, "A", operation == "launch")
            if operation == "test":
                assert result["compiled_source_count"] == 0, result
            else:
                assert result["jdt_bootstrap_reused"], result
            await verify(call, "A", True)
            await call("java_application", {"action": "stop"})

    anyio.run(scenario)
