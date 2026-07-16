# joLink Runtime Debugger

An LLM-facing runtime debugger for coding agents.

Free and local. It does not require a joLink account, model API key,
inference provider, or separate agent application.

The first adapter targets local Java applications through the existing joLink
Java Runtime implementation. The Runtime behavior was migrated from the
Hermes dogfood integration and is now exposed through a compact MCP stdio
boundary.

## Current development stage

Stage two provides the minimum MCP path:

- two tools: `java_runtime` and `java_processes`;
- 15 public Java Runtime actions;
- JSON `TextContent` plus matching `structuredContent`;
- Runtime `ok=false` mapped to MCP `isError=true`;
- stdio transport with stdout reserved for protocol messages.

The MCP boundary does not change the migrated JDWP Runtime behavior.

## Requirements

- Python 3.11–3.13
- JDK 8 or newer for Java integration tests and real debugging

## Development

```bash
uv sync --extra dev --locked
uv run pytest
```

Run the stdio server:

```bash
uv run jolink-runtime-debugger
```

Equivalent module entry point:

```bash
uv run python -m jolink_runtime_debugger.transport.stdio
```

A generic MCP client configuration can launch it from a checkout with:

```json
{
  "command": "uv",
  "args": [
    "--directory",
    "/absolute/path/to/jolink-runtime-debugger",
    "run",
    "jolink-runtime-debugger"
  ]
}
```

The real subprocess acceptance test exercises:

```bash
uv run pytest -q tests/e2e/test_stdio_mcp.py
```

It performs `initialize`, `tools/list`, `java_runtime(status)`, then closes the
stdio client contexts and waits for the server process to exit.

## Contracts

- MCP v0.1: [`docs/mcp-contract-v0.1.md`](docs/mcp-contract-v0.1.md)
- Runtime lineage 2.4.0:
  [`docs/runtime-lineage-contract-2.4.0.md`](docs/runtime-lineage-contract-2.4.0.md)
