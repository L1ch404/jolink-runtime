# joLink Runtime Debugger MCP Contract v0.1

Status: implemented minimum stdio boundary.

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

1. This observes and controls a local JVM.
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

Every normal Tool result contains both:

- one JSON `TextContent`;
- the same object in `structuredContent`.

`java_processes` does not include an `ok` field. It is successful unless the
boundary returns an explicit `ok=false` payload.

## Suspension and cancellation

- Only one active suspension is allowed per Runtime session.
- Stack frames, variables, and object references are valid only while their
  suspension remains active.
- `resume` invalidates the suspension and all references obtained from it.
- The MCP implementation serializes calls that operate on the default Runtime.
- Stage two deliberately does not change `wait_event` or add a waiter
  generation state machine.
- Cancellation-hardening remains a later Runtime task and is not claimed by
  the minimum MCP boundary.

## Process ownership

- `run` creates a Runtime-owned JVM.
- `attach` observes an externally managed local JVM.
- This minimum boundary does not add implicit target-JVM cleanup on server
  shutdown.
- Target cleanup remains explicit through `resume`, `cleanup_debug_state`,
  `detach`, or `stop`, using the existing Runtime ownership semantics.
- Remote JDWP attachment is not part of v0.1.

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

## v0.1 minimum-boundary exclusions

- No `wait_event` behavior change
- No cancellation generation state machine
- No new Runtime actions
- No JDWP protocol refactor
- No additional language adapters
- No HTTP transport
- No setup installer
- No implicit target-JVM shutdown cleanup
