"""stdio entry point for the joLink Runtime MCP server."""

from __future__ import annotations

import logging
import sys

import anyio
import mcp.server.stdio
from mcp.server.lowlevel import NotificationOptions

from ..server.mcp_server import create_mcp_server


def _configure_stderr_logging() -> None:
    """Keep stdout exclusively reserved for MCP protocol messages."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
        force=True,
    )
    # HTTP trigger URLs and headers may contain application-specific data.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def run_stdio() -> None:
    server = create_mcp_server()
    async with mcp.server.stdio.stdio_server() as (
        read_stream,
        write_stream,
    ):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
        )


def main() -> None:
    _configure_stderr_logging()
    anyio.run(run_stdio)


if __name__ == "__main__":
    main()


__all__ = ["main", "run_stdio"]
