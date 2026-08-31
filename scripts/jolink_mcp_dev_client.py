#!/usr/bin/env python3
"""Interactive JSONL MCP client that always starts the current worktree."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def _source_fingerprint(repository: Path) -> str:
    digest = hashlib.sha256()
    for source in sorted((repository / "src/jolink_runtime").rglob("*.py")):
        digest.update(source.relative_to(repository).as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(source.read_bytes()).digest())
    return digest.hexdigest()


async def _run(args: argparse.Namespace) -> None:
    repository = args.repository.expanduser().resolve(strict=True)
    stderr_path = (
        args.stderr_log.expanduser().resolve(strict=False)
        if args.stderr_log is not None
        else Path(tempfile.gettempdir()) / "jolink-mcp-dev-stderr.log"
    )
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stderr = stderr_path.open("a", encoding="utf-8")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jolink_runtime.transport.stdio"],
        cwd=repository,
    )
    try:
        async with stdio_client(parameters, errlog=stderr) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                status = _git(repository, "status", "--porcelain")
                print(
                    "READY "
                    + json.dumps(
                        {
                            "name": initialized.serverInfo.name,
                            "version": initialized.serverInfo.version,
                            "repository": str(repository),
                            "git_commit": _git(repository, "rev-parse", "HEAD"),
                            "dirty_worktree": bool(status),
                            "source_fingerprint": _source_fingerprint(repository),
                            "python": sys.executable,
                            "stderr_log": str(stderr_path),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                while True:
                    line = await anyio.to_thread.run_sync(sys.stdin.readline)
                    if not line:
                        return
                    command = json.loads(line)
                    if command.get("command") == "quit":
                        return
                    result = await session.call_tool(
                        command["tool"],
                        command.get("arguments", {}),
                    )
                    print(
                        "RESULT "
                        + json.dumps(
                            result.structuredContent,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        flush=True,
                    )
    finally:
        stderr.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--stderr-log", type=Path)
    anyio.run(_run, parser.parse_args())


if __name__ == "__main__":
    main()
