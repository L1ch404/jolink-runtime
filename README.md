# joLink Runtime Debugger

An LLM-facing runtime debugger for coding agents.

Free and local. It does not require a joLink account, model API key,
inference provider, or separate agent application.

The first adapter targets local Java applications through the existing joLink
Java Runtime implementation. The Runtime behavior was migrated from the
Hermes dogfood integration; the MCP boundary will be added only after the
migrated Runtime test suite is green.

## Current development stage

Stage one is a behavior-preserving extraction of the Java Runtime core. It
does not yet expose an MCP server.

Requirements:

- Python 3.11–3.13
- JDK 8 or newer for Java integration tests and real debugging

Development setup:

```bash
uv sync --extra dev
uv run pytest
```
