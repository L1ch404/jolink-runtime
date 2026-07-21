from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import TextIO

import anyio
import mcp.types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_real_stdio_subprocess_initialize_list_status_and_shutdown() -> None:
    repository_root = Path(__file__).resolve().parents[2]

    async def scenario(stderr: TextIO) -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "jolink_runtime.transport.stdio"],
            cwd=repository_root,
        )
        with anyio.fail_after(30):
            async with stdio_client(parameters, errlog=stderr) as (
                read_stream,
                write_stream,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "jolink-runtime"
                    assert initialized.serverInfo.version == "0.1.0a2"

                    listed = await session.list_tools()
                    assert [tool.name for tool in listed.tools] == [
                        "java_runtime",
                        "java_processes",
                    ]

                    result = await session.call_tool(
                        "java_runtime",
                        {"action": "status"},
                    )
                    assert result.isError is False
                    assert result.structuredContent is not None
                    assert result.structuredContent["ok"] is True
                    assert result.structuredContent["process_state"] == "absent"
                    assert result.structuredContent["debug_state"] == "detached"

                    assert len(result.content) == 1
                    assert isinstance(result.content[0], types.TextContent)
                    assert json.loads(result.content[0].text) == (
                        result.structuredContent
                    )

            # Exiting both contexts closes stdin and waits for the subprocess.
            # A log written to stdout would corrupt the protocol before here.

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        anyio.run(scenario, stderr)
        stderr.seek(0)
        log_text = stderr.read()
        assert "java_runtime.action.start action=status" in log_text
        assert "java_runtime.action.finish action=status" in log_text
