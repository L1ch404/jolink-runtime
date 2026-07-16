# Runtime Lineage Contract 2.4.0

This document freezes the Java Runtime behavior migrated from the Hermes
dogfood implementation. It is an internal compatibility contract, not the
MCP interface advertised to clients.

## Identity

- Runtime lineage: `2.4.0`
- Frozen Hermes source commit:
  `cc726310c7d9d7981ef3f0bf9e2d27513d0c9515`
- Standalone package/server version: independent from the lineage version

The lineage version is not incremented while the transport and distribution
boundary changes without changing Runtime behavior.

## Frozen implementation boundary

The lineage contract covers:

- `adapters/java/jdwp_adapter.py` and the migrated JDWP implementation
- Java process management, discovery, and captured launch logs
- `core/dispatcher.py`, including argument defaults and coercions
- `core/models.py` and `core/session_manager.py`
- the Hermes-era schemas in `adapters/java/tool_schema.py`
- normalized Dispatcher and process-discovery results in the golden fixtures

The internal Dispatcher continues to recognize the historical
`wait_breakpoint` alias. The MCP v0.1 Schema does not advertise that alias.

## Frozen schema fingerprints

- `JAVA_RUNTIME_SCHEMA`:
  `264b4899a8bcec75bca2f0ce38e21999ed8356c4e5ed9af325f1dc125f44af54`
- `JAVA_PROCESSES_SCHEMA`:
  `0c3739a5a920eab41d5d9d7fe48a1be452de2342a40a2ab474119c4e55b8fbac`

The complete fixtures and their metadata live under:

```text
tests/fixtures/runtime-lineage-2.4.0/
```

They can be regenerated only from the pinned Hermes source revision with:

```bash
uv run python scripts/generate_runtime_lineage_fixtures.py \
  --hermes-source /path/to/hermes-agent
```

Regenerating fixtures is a deliberate lineage update, not a normal test fix.

## Verification

The offline contract does not require a Hermes checkout:

```bash
uv run pytest -q tests/contract/test_runtime_lineage_golden.py
```

When a Hermes checkout is available, the differential tests provide an
additional source-to-source comparison. CI correctness does not depend on
that optional checkout.

## Relationship to MCP

The MCP boundary may:

- advertise a smaller Schema;
- hide compatibility-only actions;
- validate MCP arguments;
- serialize calls into the Dispatcher;
- wrap Dispatcher dictionaries as MCP content.

It must not silently change the migrated JDWP lifecycle, event handling,
process ownership, observation semantics, or Runtime result payloads. Those
changes require a separately reviewed Runtime change.
