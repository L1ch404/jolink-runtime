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

This contract records provenance, not an immutable claim that migrated code
can never receive a reviewed defect fix. Any intentional deviation is listed
below so lineage compatibility and current correctness remain distinguishable.

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

## Reviewed deviations from the frozen source

### RuntimeResult reserved fields

The standalone Runtime fixes an invariant violation in the migrated
`RuntimeResult.to_json()` implementation:

- `RuntimeResult.ok` exclusively determines the serialized `ok` field unless
  the formal `RuntimeResult.error` field forces failure;
- `RuntimeResult.error` exclusively determines the serialized `error` field;
- arbitrary `data` cannot override either reserved field;
- a successful result drops any `error` key supplied through `data`.

This is a correctness and safety repair, not a new Runtime action or a change
to JDWP behavior. The frozen schema fingerprints and golden lineage fixtures
remain unchanged.

### Cancellable wait ownership

The standalone MCP boundary adds internal `WaitControl` ownership to
`wait_event` without adding a public Runtime action or Tool parameter:

- every wait has a waiter id and generation;
- cancelled waiters cannot publish a suspension;
- a breakpoint/exception event arriving during cancellation is automatically
  resumed using its JDWP suspend policy;
- a later waiter cannot run until the earlier worker has exited and settled;
- a Composite with multiple `EVENT_THREAD` threads records and resumes every
  suspended thread.

Normal, non-cancelled Runtime results remain unchanged.

### Wait-scoped suspend-capable requests

Stage 2.1.1 fixes a lifecycle race inherited from the migrated implementation.
Breakpoint and exception configuration is now split into two layers:

- stable Runtime definitions remain visible through `list` and retain their
  public `breakpoint_id` or exception `request_id`;
- suspend-capable JDWP EventRequests exist only while one `wait_event` owns the
  session and are cleared on hit, timeout, cancellation, or error.

Late events racing request cleanup are automatically resumed. Raw JDWP request
ids are connection-scoped diagnostics and are never stable operation ids. This
prevents a target thread from being suspended when no waiter can receive and
own the event.

Breakpoint removal accepts the stable `breakpoint_id` (or class/line
selectors), not a raw JDWP `request_id`. The integer `request_id` remains the
stable public identifier for exception watches only.

### JDWP receive framing and connection generation

The migrated socket reader discarded partial JDWP headers/bodies when a
short timeout interrupted `recv()`. The standalone implementation keeps a
connection-scoped receive buffer and enforces a single-reader invariant.
Closing or replacing a connection clears its buffer and generation; packets
from an old generation cannot enter reply/event queues.

This is a protocol correctness repair required to make cancellable short
polling safe.

### Internal ownership-aware shutdown

The standalone session lifecycle has internal `close` and `force_close`
operations used by MCP shutdown:

- launched JVMs are stopped;
- attached JVMs are resumed/detached and never terminated;
- shutdown closes a process-manager gate before inspecting the current target;
  OS process creation and target publication are atomic under that gate, so a
  JVM cannot appear after shutdown has concluded;
- each released target is identified by process object and generation, so a
  late cleanup for target A cannot stop or forget replacement target B;
- forced JDWP disconnect invalidates connection-scoped event requests instead
  of reporting stale breakpoint/exception ids as active;
- normal public `stop` and `detach` behavior is unchanged.

These hooks are transport lifecycle behavior and are not advertised Runtime
actions.
