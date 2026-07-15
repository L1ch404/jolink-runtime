# joLink Runtime Debugger MCP Contract v0.1

Status: frozen for the behavior-preserving Runtime extraction.

This file records the target MCP boundary. Stage one migrates the existing
Runtime implementation and tests without implementing this boundary.

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

`wait_breakpoint` remains an internal compatibility alias during the
migration. It is not advertised by the MCP Schema and must return a
deprecation warning when called through a compatible internal boundary.

## Required tool-description semantics

The compact description must tell the model:

1. This is an LLM-facing debugger for a local JVM.
2. It is useful when source code, logs, or tests cannot reliably determine the
   executed path or runtime state.
3. It supports lifecycle, breakpoint/exception events, stack, variables, and
   resume.
4. A suspension returned by `wait_event` must be resumed or cleaned up after
   inspection.

Server instructions, Resources, and Prompts are optional enhancements and
must not carry rules required for safe basic use.

## Result semantics

`ok` describes whether Runtime correctly executed the request.
`observation_state` describes how much evidence the target JVM could provide.

1. MCP/JSON-RPC protocol errors are reserved for unknown tools, malformed MCP
   messages, and server protocol failures.
2. Runtime execution failures return `ok=false` and become MCP tool results
   with `isError=true`.
3. A successful but incomplete observation returns `ok=true`,
   `isError=false`, and `observation_state=complete|partial|unavailable`.

Missing or stale suspension state, invalid arguments, and JDWP connection
failures are execution errors; they are not unavailable observations.

## Suspension and cancellation

- Only one active suspension is allowed per Runtime session.
- Stack frames, variables, and object references are valid only while their
  suspension remains active.
- `resume` invalidates the suspension and all references obtained from it.
- The MCP implementation must serialize calls that operate on one Runtime.
- A cancelled waiter must never publish a suspension. If it consumes a
  suspending event, it must resume that event before a newer waiter may run.
- Wait cancellation uses an internal monotonic generation/waiter identifier;
  it does not add a public Schema field.

Stage one does not change the current `wait_event` implementation.

## Process ownership

- `run` creates a Runtime-owned JVM.
- `attach` observes an externally managed local JVM.
- Normal server shutdown may stop a Runtime-owned JVM.
- Normal server shutdown must resume and detach, never terminate, an
  externally attached JVM.
- Remote JDWP attachment is not part of v0.1.

## Schema budget

- Compact `java_runtime` Schema: at most 5 KB.
- All advertised tool Schemas combined: at most 6 KB.
- The budget must not remove the required tool-description semantics above.
- Cross-field action validation belongs in Runtime results rather than large
  `oneOf` branches.

Stage one preserves the migrated Hermes-era Schema for differential testing.
Schema compaction happens only in stage two.

## Stage-one exclusions

- No MCP Server or transport implementation
- No Schema compaction
- No `wait_event` behavior change
- No new Runtime actions
- No JDWP protocol refactor
- No additional language adapters
- No changes to established Runtime result semantics
