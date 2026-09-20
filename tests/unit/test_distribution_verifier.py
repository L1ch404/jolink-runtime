"""The release smoke test must exercise public tools, not hidden legacy aliases."""

import importlib.util
import json
from pathlib import Path

import anyio
import mcp.types as types
import pytest

from jolink_runtime.server.tool_schema import get_mcp_tools


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("jolink_distribution_verifier", ROOT / "scripts/verify_distribution.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


@pytest.mark.parametrize("tool", get_mcp_tools(), ids=lambda t: t.name)
def test_release_calls_route_to_advertised_tool(tool):
    calls = []

    class Session:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            payload = {"ok": True}
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(payload))],
                structuredContent=payload,
                isError=False,
            )

    async def scenario():
        for action in tool.inputSchema["properties"]["action"]["enum"]:
            kwargs = {"tool": tool.name} if tool.name == "java_fast_test" else {}
            await verifier.call_payload(Session(), {"action": action}, **kwargs)
            assert calls[-1] == (tool.name, {"action": action})

    anyio.run(scenario)


def test_release_fixture_has_real_sources_and_no_precompiled_output(tmp_path):
    project, source = verifier.project_fixture(tmp_path, 12345)
    assert source.is_file()
    assert source.with_name("App.java").is_file()
    assert (project / "src/test/java/example/ReplyTest.java").is_file()
    assert (project / ".run/Distribution.run.xml").is_file()
    assert not (project / "target").exists()
    assert not list(project.rglob("*.class"))


def test_release_logging_is_explicit_and_checkout_path_is_not_inherited(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))
    monkeypatch.setenv("JOLINK_LOG_LEVEL", "WARNING")
    environment = verifier.clean_server_environment()
    assert "PYTHONPATH" not in environment
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["JOLINK_LOG_LEVEL"] == "INFO"
