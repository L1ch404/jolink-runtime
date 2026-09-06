#!/usr/bin/env python3
"""Exercise an existing Gradle project through fresh stdio MCP and a warm repeat."""

from __future__ import annotations

import argparse
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import sys
import tempfile

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(args):
    environment = dict(os.environ)
    if args.java_home:
        environment.update(
            JAVA_HOME=str(args.java_home),
            PATH=str(args.java_home / "bin") + os.pathsep + environment.get("PATH", ""),
        )
    if args.cache_root:
        environment["XDG_CACHE_HOME"] = str(args.cache_root)
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jolink_runtime.transport.stdio"],
        env=environment,
    )
    reports = []
    async with (
        AsyncExitStack() as stack,
        stdio_client(
            server, errlog=stack.enter_context(tempfile.TemporaryFile(mode="w+"))
        ) as (read, write),
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for _ in range(args.repeats):
                response = await session.call_tool(
                    "java_application",
                    {
                        "action": "test",
                        "build_system": "gradle",
                        "project_path": str(args.project.resolve()),
                        "tests": args.tests,
                        "timeout": 300,
                    },
                )
                result = dict(response.structuredContent or {})
                with anyio.fail_after(1200):
                    while result.get("status") in {
                        "starting",
                        "bootstrapping",
                        "compiling",
                        "running",
                    }:
                        await anyio.sleep(0.2)
                        response = await session.call_tool(
                            "java_status", {"action": "status"}
                        )
                        result = dict(
                            (response.structuredContent or {}).get("fast_test", {})
                        )
                reports.append(result)
                if args.report:
                    args.report.write_text(
                        json.dumps(reports, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                summary = dict(result)
                if "bootstrap_log_tail" in summary:
                    summary["bootstrap_log_tail"] = summary["bootstrap_log_tail"][:35]
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                if not result.get("passed"):
                    break
    return 0 if reports and all(item.get("passed") for item in reports) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("tests", nargs="+")
    parser.add_argument("--java-home", type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--report", type=Path)
    raise SystemExit(anyio.run(run, parser.parse_args()))
