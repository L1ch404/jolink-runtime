# joLink Runtime Debugger

An LLM-facing runtime debugger for coding agents.

Free and local. It does not require a joLink account, model API key,
inference provider, or separate agent application.

The first adapter targets local Java applications through the existing joLink
Java Runtime implementation. The Runtime behavior was migrated from the
Hermes dogfood integration and is now exposed through a compact MCP stdio
boundary.

## Current development stage

Stage 2.1 provides the MCP path plus cancellation and shutdown safety:

- two tools: `java_runtime` and `java_processes`;
- 15 public Java Runtime actions;
- JSON `TextContent` plus matching `structuredContent`;
- Runtime `ok=false` mapped to MCP `isError=true`;
- stdio transport with stdout reserved for protocol messages.
- cancellable `wait_event` with generation ownership and automatic resume;
- optional two-phase `wait_event` (`arm` -> external trigger -> `await`) so a
  client can know that JDWP requests are installed before firing a scenario;
- wait-scoped JDWP requests, so no breakpoint can suspend a JVM without an
  active waiter;
- ownership-aware shutdown: stop launched JVMs, detach attached JVMs;
- persistent JDWP packet framing across short polling timeouts.

The current two-phase implementation is intended for controlled dogfood. Its
confirmed cancellation, cleanup-preemption, handle-publication, and response
delivery limitations are tracked in
[`docs/stage-2.1.2-lifecycle-backlog.md`](docs/stage-2.1.2-lifecycle-backlog.md).

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

The heavier real MCP/JVM suite is opt-in locally and runs once in the
canonical Linux/Python 3.11/JDK 17 CI job:

```bash
JOLINK_RUN_MCP_JAVA_E2E=1 \
  uv run pytest -q -m mcp_java_e2e tests/e2e/test_stdio_mcp_java.py
```

## Contracts

- MCP v0.1: [`docs/mcp-contract-v0.1.md`](docs/mcp-contract-v0.1.md)
- Runtime lineage 2.4.0:
  [`docs/runtime-lineage-contract-2.4.0.md`](docs/runtime-lineage-contract-2.4.0.md)
