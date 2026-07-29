# joLink Runtime MCP Contract v0.1

Status: implemented stdio boundary with Stage 2.1 lifecycle hardening and
deterministic two-phase event waiting.

This is the client-facing MCP contract. The migrated implementation it wraps
is frozen separately in
[`runtime-lineage-contract-2.4.0.md`](runtime-lineage-contract-2.4.0.md).

## Identity and versions

- Package prerelease version: `0.1.0a3`
- MCP Server name: `jolink-runtime`
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
- `update`

`wait_breakpoint` remains an internal Runtime-lineage compatibility alias. It
is not advertised or accepted as a public MCP action.

## Application startup readiness

`run` and `restart` distinguish JVM launch from optional application TCP
readiness.

They support two launch forms:

- direct JVM launch with `jar_path`, or `main_class` plus `classpath`;
- IDEA/Maven project launch with `project_path` and an optional exact
  `launch_name`.

Project launch imports a supported IntelliJ IDEA Application or Spring Boot
configuration, uses the selected Maven/JDK environment, compiles in the
background, resolves the runtime classpath, and starts the managed JVM.
`project_path` is mutually exclusive with direct JVM launch arguments.
Before the JVM exists, `status` reports `process_state=absent` plus the
current `launch_phase` and omits `startup_state`.

`update(source_files)` is available only for the active JVM produced by a
supported `project_path` launch. It compiles explicit Java sources from the
selected Maven module into joLink-owned private staging, accepts only a stable
generated-class set and method-body-compatible class shape, and applies the
changed loaded classes as one JDWP `RedefineClasses` operation. It never
writes Maven output or silently falls back to Maven/restart. Success is
runtime-only evidence and returns `verification_state=not_verified`; a fresh
business request is still required.

P0 uses the resolved build JDK, compile classpath, launch bytecode target,
source encoding, debug metadata, and existing `MethodParameters` convention.
It deliberately disables annotation processing and does not claim to replay
arbitrary Maven compiler-plugin executions. `fast_update.available=true`
means the launch is eligible for the bounded fast path; compilation can still
fail safely and direct the caller to a formal Maven build.

- `ready_port` is an optional loopback application port. It must differ from
  `jdwp_port`.
- `startup_wait_timeout_seconds` limits the direct-launch readiness wait or
  the first project-launch readiness observation window, defaults to 30
  seconds, and is capped at 60 seconds. A wait timeout never terminates a live
  process; a project worker continues observing readiness in the background.
- Readiness configuration is stored with the launched process. `status`
  rechecks the same port without reading or interpreting application logs.
- `restart` reuses the prior launched process's readiness configuration when
  the caller does not provide a replacement.

The public startup states are:

- `unverified`: no readiness port was configured;
- `starting`: the process is alive but the configured port has not accepted a
  TCP connection;
- `ready`: joLink observed the configured loopback TCP port accept a
  connection;
- `failed`: the managed process exited.

TCP readiness proves only that the configured port accepted a connection. It
does not prove database initialization, cache warmup, background jobs, or
individual business endpoints are healthy. `ready_observed_at` records the
first joLink observation, not the exact instant the listener opened.

Before spawning a new JVM, joLink rejects a configured readiness port that is
already accepting connections. The check runs after the previously managed
target has been stopped or detached, so a normal `restart` does not mistake the
old owned process for an unrelated listener.

If `run` finishes its bounded wait while the process remains alive, it returns
`ok=true`, `startup_state=starting`, and `next_action=status`. A later `status`
that observes process exit still returns `ok=true` because the observation
succeeded; application failure is represented by `startup_state=failed`.

A managed HTTP trigger is not sent while configured readiness is
`starting`, and the boundary returns `APPLICATION_NOT_READY` with
`http_trigger_sent=false`. `unverified` readiness remains allowed with a
warning so attach and non-Web workflows remain compatible.

## Launch-log snapshot semantics

`logs` reads a bounded snapshot of stdout/stderr captured from the currently
owned launch. It freezes the file end offset at call time and reads backward
from that offset, so a continuously writing application cannot make the call
chase a moving EOF.

The result includes:

- `requested_lines` and `returned_lines`;
- `snapshot_size_bytes` and `scanned_bytes`;
- `total_lines_exact`; `total_lines` is `null` when the bounded suffix is not
  enough to count the complete file;
- `has_more_before`, `scan_limit_reached`, and `truncated`;
- `growth_state` and, after the first call, the previous size and number of
  newly appended bytes, so repeated observations can detect progress without
  rereading or counting the complete file;
- warnings and `truncation_reasons` when the scan or MCP output bound prevents
  all requested complete lines from being returned.

These fields describe the completeness of the log observation. A bounded or
truncated log result is still a successful Runtime operation and must not be
presented as proof that an unobserved message does not exist elsewhere in the
file.

## Required tool-description semantics

The compact description must tell the model:

1. This is stateful and observes and controls a local JVM.
2. It is useful when source code, logs, or tests cannot reliably determine the
   executed path or runtime state.
3. It supports lifecycle, breakpoint/exception events, stack, variables, and
   resume.
4. Breakpoints and exception watches are armed only while `wait_event` is
   active. Prefer `wait_mode=blocking` with `http_trigger` for a one-call
   arm/trigger/await flow. Use `arm` then `await` when an external action must
   occur between arming and collection.
5. A suspension returned by `wait_event` must be resumed or cleaned up after
   inspection.
6. For an owned HTTP application, `ready_port` lets `run/status` distinguish
   `starting` from TCP `ready`; the model must not trigger HTTP while configured
   readiness is still starting.

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

### Public wait modes

`wait_event` has three modes without adding a new Runtime action:

- `blocking` is the compatibility default. Without `http_trigger`, the call
  directly arms requests and blocks until a hit, timeout, cancellation, or
  error. With `http_trigger`, the boundary reuses the existing protected
  `arm -> trigger -> await` lifecycle in one MCP call.
- `arm` starts one protected background observation and returns only after all
  applicable JDWP EventRequests have been installed. Its successful result
  contains an opaque `wait_handle`, `armed_at`, `expires_at`, and the stable
  logical breakpoint/exception ids armed for that wait. It may also start one
  optional `http_trigger` after arming is confirmed.
- `await` accepts the `wait_handle` returned by `arm`. It returns the event or
  terminal wait result. It also accepts a handle returned when a composed
  `blocking` call reaches only its local await deadline. If the underlying
  observation is still active, it returns `status=waiting`; the same handle
  may be awaited again.

The intended deterministic sequence is:

```text
wait_event(wait_mode=blocking, http_trigger=...)
-> internally arm, start the trigger only after armed, then await
-> inspect the suspension
-> resume(suspension_id=...)
```

When an external action must occur after arming, use:

```text
wait_event(wait_mode=arm)
-> receive status=armed
-> start the scenario through a non-blocking external mechanism
-> wait_event(wait_mode=await, wait_handle=...)
-> inspect the suspension
-> resume(suspension_id=...)
```

The explicit two-phase form may also own a local HTTP trigger when the caller
needs the armed response before awaiting:

```text
wait_event(wait_mode=arm, http_trigger=...)
-> receive status=armed and required_next_action=await
-> wait_event(wait_mode=await, wait_handle=...)
-> inspect the suspension
-> resume(suspension_id=...)
```

Breakpoint and exception hits include copyable `suggested_next_actions` for
`stack`, `variables`, and `resume`, all bound to the exact `suspension_id`.
`stack` and `variables` use that suspension's event-hit thread when
`thread_name` is omitted. An explicit `thread_name` is a fallback selector:
exact matches are preferred, followed by a unique prefix or substring match
across JVM thread names. The selected thread must be suspended before its
stack or variables can be read. Thread names are not lifecycle identifiers
and must not replace `suspension_id`.

### Managed HTTP trigger

The optional `http_trigger` is an MCP-boundary convenience, not a new Runtime
action and not a general-purpose HTTP client.

- It is valid with `wait_event(wait_mode=blocking|arm)` and one trigger belongs
  to one Runtime observation `wait_handle`.
- `blocking` composes the same internal arm, trigger, and await operations;
  it does not implement a second waiter or trigger state machine.
- Supported methods are `GET`, `POST`, `PUT`, `PATCH`, and `DELETE`.
- The target must be `http://127.0.0.1`; redirects and environment proxies are
  disabled.
- The request starts asynchronously only after JDWP reports the wait as armed.
  Explicit `arm` returns without waiting for the HTTP response; composed
  `blocking` awaits the Runtime observation, not HTTP completion.
- If a Runtime result was already published before trigger-start ownership is
  claimed, the trigger is not sent and its status is
  `not_started_event_already_ready`.
- An armed response with a trigger includes `required_next_action` containing
  the exact `await` call shape. The caller must not send the request again.
- A terminal Runtime result consumes and forgets the `wait_handle`. It is not
  an HTTP-completion handle and cannot be awaited after resume to retrieve a
  final response.
- `response_headers_received` proves only that response headers arrived. It
  does not end the Runtime observation or prove that no later asynchronous
  debug event can occur.
- A debug event observed after the trigger does not by itself prove that the
  event occurred on the HTTP request thread. Trigger state therefore keeps
  `server_execution_state=unknown`.
- Trigger output is deliberately bounded. It may expose method, lifecycle
  status, HTTP status, timestamps, and a stable error code, but never echoes
  the URL, headers, request body, or response body. Validation failures follow
  the same rule: they identify the invalid field and rule without echoing its
  value.
- Response headers received without a Runtime event do not close the wait.
  Server work may continue asynchronously after the HTTP response, so the same
  handle remains awaitable until its Runtime deadline or explicit cleanup.
- A definite connection/start failure terminates and safely settles the wait.
  A Runtime result already published at the failure boundary takes priority.
- A configured application in `startup_state=starting` is rejected before the
  waiter or HTTP client is created. No request is sent. An unverified attached
  or launched JVM is allowed with an explicit warning.
- If the Runtime wait reaches a terminal result without a suspension, joLink
  requests cancellation of any still-running client-side HTTP wait before the
  public handle is released.
- Client timeout or connection cancellation leaves server execution as
  `unknown`; it is not reported as proof that business work stopped.
- `cleanup_debug_state`, `stop`, `restart`, `detach`, and MCP shutdown cancel
  joLink's client-side HTTP wait as well as settling Runtime state. This is a
  non-blocking cancellation signal: JVM lifecycle actions do not wait for the
  HTTP client thread to exit. Closing the client connection cannot undo work
  already accepted by the application.
- Limits: 32 headers, 16 KiB aggregate header data, 256 KiB serialized JSON
  body, and a 0.1-120 second HTTP client timeout. An automatically added JSON
  `Content-Type` counts toward both header limits.

Only one `await` request may own a handle at a time. A concurrent duplicate is
rejected with `WAIT_HANDLE_IN_USE`; after a non-terminal `status=waiting`, the
same handle may be awaited again.

There is at most one active wait per Runtime session. While a two-phase wait
is active, normal Runtime observation or mutation calls are rejected with
`ACTIVE_WAITER_EXISTS`; `cleanup_debug_state`, `stop`, `restart`, and `detach`
first cancel and settle the wait safely. Cancelling an `arm` or `await` MCP
request also cancels the underlying observation.

Successful `cleanup_debug_state` results contain `verification_state` and a
`verification` object covering active suspension, logical definitions,
Runtime-tracked JDWP requests, and MCP wait state. A separate
`http_trigger_cleanup_state` reports whether local HTTP-client cancellation is
complete or still settling. A settling client is not evidence that server-side
business execution was cancelled.

If a two-phase wait creates a suspension but no `await` call claims its result
within the bounded delivery grace period, Runtime resumes that exact
suspension (or disconnects JDWP as the safe fallback) and preserves an
explicit `WAIT_RESULT_EXPIRED` result for the handle. The public `wait_handle`
is an opaque observation token; it is not a JDWP request id, suspension id,
internal waiter id, or generation.

#### Current dogfood implementation limitations

The lifecycle statements above are the target v0.1 contract. The current
dogfood implementation still has confirmed concurrency defects, recorded with
reproductions in
[`stage-2.1.2-lifecycle-backlog.md`](stage-2.1.2-lifecycle-backlog.md):

- cancellation of `arm` can still be deferred until its local setup wait
  deadline; passive `await` now uses short local polling and no longer holds
  the global call lock;
- a completed handle has a brief non-atomic transition in which `await` can
  incorrectly return `WAIT_HANDLE_NOT_FOUND`;
- the post-handler MCP response-delivery suspension gap remains unbounded by
  the complete delivery/inspection lease.

Consequently this implementation is suitable for controlled dogfood, not yet
for unattended long-running use. These are documented limitations, not
accepted final semantics.

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
- `breakpoint set` and `exception set` create stable Runtime definitions.
  Their `breakpoint_id` and exception `request_id` values remain stable across
  waits.
- Suspend-capable JDWP EventRequests are created only for the active waiter
  generation. Every hit, timeout, cancellation, and error exit clears them
  before the wait finishes.
- With no active waiter, no Runtime-owned JDWP EventRequest may remain capable
  of suspending the target JVM. Events racing request cleanup are drained and
  automatically resumed instead of becoming public suspensions.
- The next `wait_event` re-arms the same logical definitions. Raw JDWP request
  ids may change between waits and are diagnostics, not operation ids.
- Normal cancellation preserves logical breakpoint and exception definitions,
  not their temporary JDWP requests.
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
- Shutdown waits for bounded Java cleanup primitives and performs best-effort
  fallback cleanup. If normal debugger cleanup exceeds its grace period, an
  ownership-aware force release closes JDWP, stops only the exact owned target,
  and only forgets the exact attached target.
- Shutdown closes the target-publication gate before taking its process
  snapshot. A JVM cannot be spawned after that point, and a JVM already being
  spawned is published atomically before shutdown chooses how to release it.
- v0.1 does not claim a hard process-exit deadline for an arbitrary Python or
  operating-system call that never returns. Real Java shutdown paths are
  covered by subprocess E2E; MCP hosts may still terminate an unresponsive
  stdio child after their own transport deadline.
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

## Schema principles

- Keep advertised Schemas concise, but do not enforce a fixed byte limit.
- Required selection, safety, readiness, and recovery semantics take priority
  over an arbitrary character budget.
- Cross-field action validation belongs in Runtime results rather than large
  `oneOf` branches.

The larger Hermes-era schemas remain frozen as Runtime-lineage compatibility
artifacts; they are never advertised by the MCP server.

## v0.1 exclusions

- No new Runtime actions
- No additional language adapters
- No HTTP MCP transport (the managed loopback request is a scenario trigger,
  not a server transport)
- No setup installer
- No remote JDWP attach
- No public internal waiter/generation/cancellation fields in the Tool Schema;
  only the opaque two-phase `wait_handle` is public
