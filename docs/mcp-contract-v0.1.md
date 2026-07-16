# joLink Runtime Debugger MCP Contract v0.1

Status: implemented stdio boundary with Stage 2.1 lifecycle hardening.

This is the client-facing MCP contract. The migrated implementation it wraps
is frozen separately in
[`runtime-lineage-contract-2.4.0.md`](runtime-lineage-contract-2.4.0.md).

## Identity and versions

- Package/server version: `0.1.0`
- Migrated Runtime lineage: `2.4.0`
- Runtime lineage is not independently published or incremented during the
  migration.

## Exposed tools

- `java_runtime`
- `java_processes`

The first release exposes Java only. Future languages receive their own tools
and adapters instead of adding a `language` union to `java_runtime`.

## Public Java Runtime actions

- `run`
- `stop`
- `restart`
- `attach`
- `detach`
- `status`
- `logs`
- `breakpoint`
- `exception`
- `wait_event`
- `threads`
- `stack`
- `variables`
- `resume`
- `cleanup_debug_state`

`wait_breakpoint` remains an internal Runtime-lineage compatibility alias. It
is not advertised or accepted as a public MCP action.

## Required tool-description semantics

The compact description must tell the model:

1. This is stateful and observes and controls a local JVM.
2. It is useful when source code, logs, or tests cannot reliably determine the
   executed path or runtime state.
3. It supports lifecycle, breakpoint/exception events, stack, variables, and
   resume.
4. A suspension returned by `wait_event` must be resumed or cleaned up after
   inspection.

The Tool description carries these rules; correct basic use does not depend
on a Resource or Prompt being loaded.

## Result semantics

`ok` describes whether Runtime correctly executed the request.
`observation_state` describes how much evidence the target JVM could provide.

1. Malformed MCP messages and server protocol failures remain protocol errors.
2. Runtime or boundary execution failures return `ok=false` and become MCP
   tool results with `isError=true`.
3. A successful but incomplete observation returns `ok=true`,
   `isError=false`, and `observation_state=complete|partial|unavailable`.

Missing or stale suspension state, invalid arguments, and JDWP connection
failures are execution errors; they are not unavailable observations.

Missing Java `VariableTable` debug metadata also remains an execution error
in MCP v0.1 for compatibility with the migrated Runtime lineage. Semantically,
this is a candidate for a future `ok=true` and
`observation_state=unavailable` result, but Stage 2.1 does not change that
Runtime behavior.

Every normal Tool result contains both:

- one JSON `TextContent`;
- the same object in `structuredContent`.

`java_processes` does not include an `ok` field. It is successful unless the
boundary returns an explicit `ok=false` payload.

Calling a tool name that the server does not advertise follows the official
Python MCP SDK behavior: the SDK returns a Tool Error. It is not converted
into a Runtime `ok=false` payload because the Dispatcher was never invoked.

## Suspension and cancellation

- Only one active suspension is allowed per Runtime session.
- Stack frames, variables, and object references are valid only while their
  suspension remains active.
- `resume` invalidates the suspension and all references obtained from it.
- The MCP implementation serializes calls that operate on the default Runtime.
- Every MCP `wait_event` receives an internal waiter id and monotonically
  increasing wait generation. These identifiers are not public Tool fields.
- Cancelling an MCP request actively cancels its waiter. The worker checks the
  token between short event-wait slices without discarding partial JDWP
  packets.
- An event consumed after its waiter is cancelled is never published as a new
  suspension. It is resumed according to its suspend policy.
- The old worker must finish and cancellation settlement must complete before
  another Runtime call can enter the session.
- Normal cancellation preserves breakpoint and exception requests.
- If the reader does not exit within the cancellation grace period, the
  boundary closes the JDWP connection. Connection-scoped requests are then
  invalidated, and `status` tells the caller to set them again.
- If a worker still cannot exit after forced disconnect, the boundary is
  poisoned and rejects further calls. Reconnect to a new server process.

## Process ownership

- `run` creates a Runtime-owned JVM.
- `attach` observes an externally managed local JVM.
- Normal server shutdown cancels and settles an active waiter before closing
  the Runtime session.
- A Runtime-owned JVM is stopped when the MCP server exits.
- An externally attached JVM is resumed/detached and is never terminated by
  MCP shutdown.
- Shutdown uses bounded grace periods. If normal debugger cleanup blocks, an
  ownership-aware force release closes JDWP, stops only owned targets, and
  only forgets attached targets.
- Explicit `resume`, `cleanup_debug_state`, `detach`, and `stop` remain
  available during normal operation.
- Remote JDWP attachment is not part of v0.1.
- Both tool input Schemas reject unknown properties.
- The `host` parameter accepts only `127.0.0.1` and `localhost`.

## Transport

- Transport: stdio only.
- stdout is reserved exclusively for MCP JSON-RPC messages.
- application and server diagnostics use stderr.
- The official MCP client closes the server by leaving the `ClientSession`
  and `stdio_client` contexts. There is no separate shutdown RPC in the
  Python SDK.

## Schema budget

- Compact `java_runtime` Schema: at most 5 KB.
- All advertised tool Schemas combined: at most 6 KB.
- The budget must not remove the required tool-description semantics above.
- Cross-field action validation belongs in Runtime results rather than large
  `oneOf` branches.

The larger Hermes-era schemas remain frozen as Runtime-lineage compatibility
artifacts; they are never advertised by the MCP server.

## v0.1 exclusions

- No new Runtime actions
- No additional language adapters
- No HTTP transport
- No setup installer
- No remote JDWP attach
- No public waiter/cancellation fields in the Tool Schema
