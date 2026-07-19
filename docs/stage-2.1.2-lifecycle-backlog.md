# Stage 2.1.2 Lifecycle Backlog

> Status: P1 deterministic arming is implemented in the MCP v0.1 contract.
> A follow-up concurrency review confirmed two new P0 timing/lock defects and
> one P1 handle-publication race. P0's complete response-delivery/inspection
> lease also remains backlog work. None of the confirmed items below has been
> fixed yet.
>
> Origin: lifecycle race review after Stage 2.1.1.

## Decision summary

Controlled dogfood may continue. Before claiming safe unattended or
long-running Agent operation, add a bounded suspension lease for the response
delivery gap described below.

Current priority order:

1. P0: make `arm`/`await` cancellation prompt and allow lifecycle cleanup to
   preempt a passive `await`.
2. P0: suspension delivery/inspection lease.
3. P1: atomically publish a completed public `wait_handle`.
4. P1 deterministic arming is implemented through optional
   `wait_mode=arm|await`; preserve its existing behavior while fixing the
   concurrency defects.
5. P2: make the armed hint respect `result_ready` and remove/rebuild stale
   distribution artifacts.
6. Do not change the current `status` drain sequence without a new
   reproduction; the reported extra-drain issue is not currently established.

## Confirmed two-phase wait concurrency defects

Status: reproduced independently on 2026-07-19; recorded only, not fixed.

The deterministic reproductions used the real `RuntimeMCPBoundary` with a
controlled dispatcher. This isolates MCP boundary scheduling from JDWP and
HTTP timing. The existing real JVM `arm -> trigger -> await -> resume ->
re-arm` path still passes; these findings concern cancellation, preemption,
and handle publication around that successful main path.

### P0: cancellation is deferred by non-abandonable Event.wait calls

Both arm setup and await result waiting call `anyio.to_thread.run_sync()` on a
long `threading.Event.wait()` with `abandon_on_cancel=False`. AnyIO therefore
defers cancellation until that blocking wait returns.

Measured reproductions:

```text
await timeout=3s, outer cancellation after 0.1s
-> cancellation completed after 3.013s

arm setup deadline=5s, outer cancellation after 0.1s
-> cancellation completed after 5.008s
```

The arm reproduction deliberately used the minimum five-second setup window;
the current setup deadline can reach 30 seconds. An await timeout can reach
the public 300-second maximum.

The stalled arm also confirms a dependency cycle:

```text
worker waits for WaitControl.cancelled
-> handler waits for ready/setup deadline
-> outer cancellation cannot reach the handler's cancellation branch
-> setup deadline is the only progress mechanism
```

Required fix properties:

- Waiting for a local readiness/result event must be asynchronously
  cancellable at a short interval.
- Cancellation must still enter the existing ordered settlement path:
  request cancel, wait for the single worker, resume/disconnect if needed,
  then release session ownership.
- Do not interrupt an in-progress JDWP packet read or introduce a second JDWP
  reader merely to make local Event waiting cancellable.

### P0: passive await holds the global call lock and blocks cleanup

`_call_wait_event_await()` holds `_call_lock` for the full result wait. A
concurrent `cleanup_debug_state`, `stop`, `restart`, or `detach` cannot acquire
the lock and therefore cannot reach the logic intended to cancel the active
observation.

Measured reproduction:

```text
start await(timeout=3s)
-> start cleanup_debug_state 50ms later
-> cleanup is still blocked after 250ms
-> trigger the fake event
-> await exits and cleanup completes after 0.252s
```

Without the forced event, cleanup remains queued until await reaches its own
timeout. `status` is affected by the same lock: rather than promptly returning
`ACTIVE_WAITER_EXISTS`, it can wait behind the passive await.

Required fix properties:

- Do not hold the global Runtime call lock while passively waiting for a local
  result event.
- Claiming a result and lifecycle transitions must remain serialized.
- Introduce explicit per-handle await ownership before allowing the global
  lock to be released, so two concurrent await calls cannot both claim one
  result.
- Cleanup/stop/restart/detach must be able to request cancellation promptly,
  then wait for ordered settlement before mutating Runtime state.

### P1: active-to-completed handle publication is not atomic

`_background_wait_finished()` currently clears `_current_waiter`, releases
`_state_lock`, and only then calls `_remember_completed_wait()` under a second
lock acquisition. During that gap `_find_wait(wait_handle)` sees neither an
active nor a completed handle.

A deterministic reproduction paused `_remember_completed_wait()` after the
active slot was cleared. Awaiting the public handle during the pause returned:

```json
{"error_code":"WAIT_HANDLE_NOT_FOUND"}
```

The internal invariant must become:

```text
every published, non-evicted wait_handle is exactly one of active or completed
```

Moving the handle between those states and applying completed-result eviction
must happen under one `_state_lock` critical section.

### P2: result_ready hint can ask for the wrong next action

The armed result correctly exposes `result_ready=true` when an event arrived
before the arm response was constructed, but `suggested_next_step` always asks
the Agent to trigger the scenario first. This can create a second request while
await still contains evidence from the first request.

The hint should depend on the same captured `result_ready` value:

- `true`: await the existing result before triggering anything else;
- `false`: trigger once, then await the handle.

Capture `result_ready` once when constructing the response instead of reading
the mutable result twice.

### Relationship to the existing response-delivery P0

The 45-second unclaimed-result grace covers a hit between `arm` and successful
claim by `await`. It does not cover cancellation after await claims the result
but before the MCP SDK delivers the response. The complete generation-bound
delivery/inspection lease below remains required and is not replaced by these
concurrency fixes.

## P0: response-delivery cancellation gap

### Verified failure window

The Runtime can create and publish an active suspension to the MCP boundary,
then lose the result before the MCP SDK sends it to the client:

```text
Runtime creates suspension
-> boundary receives breakpoint_hit / exception_hit
-> boundary clears its active waiter and returns CallToolResult
-> MCP SDK has not sent the response yet
-> cancellation is handled by RequestResponder
-> client receives Request cancelled, not suspension_id
-> target JVM thread can remain suspended with no client able to resume it
```

The window extends through the SDK response path. Enqueuing a response into
the SDK transport is also not a client acknowledgement.

`EVENT_THREAD` reduces the blast radius but does not remove the risk: the
suspended thread may own locks, transactions, or connections needed by other
threads.

### Planned safety mechanism

Add a bounded suspension lease as a Stage 2.1.2 lifecycle feature. Treat it as
a concurrency change, not as a standalone timer.

Initial timing targets to validate during dogfood:

- delivery lease: 30-60 seconds after a suspension is created;
- inspection idle lease: 90-120 seconds after a successful suspension-bound
  observation.

Successful `threads`, `stack`, `variables`, or equivalent suspension-bound
inspection acknowledges that the client received the suspension and changes
or extends the lease. Explicit `resume` ends it.

### Required invariants

- A lease is bound to the exact Runtime session, target identity,
  `suspension_id`, and suspension generation.
- An old lease must never resume a newer suspension.
- The lease task lives in the server lifespan task group, outside the MCP
  request cancellation scope.
- On expiry, acquire the boundary call lock and re-check the exact generation
  before resuming.
- Lease expiry must use the suspension's real suspend policy.
- If normal resume fails, disconnect JDWP as the safety fallback. Disconnecting
  an attached JVM must not terminate it.
- Result and `status` payloads should expose the active lease deadline and an
  automatic-resume warning without expanding the public input Schema.
- Compatibility calls that omit `suspension_id` may acknowledge only the
  currently active, generation-checked suspension.

### Required tests

- Exercise the real MCP `Server._handle_message` and `RequestResponder` path.
- Cancel after the handler returns but before `respond()` sends the result.
- Cancel while the response send/enqueue path is in progress.
- Verify the client can receive only `Request cancelled` while the lease still
  restores the JVM within the bound.
- Race lease expiry against `stack`, `variables`, explicit `resume`, shutdown,
  and creation of a newer suspension.
- Verify an expired lease cannot resume a newer generation.
- Verify resume failure falls back to a safe JDWP disconnect.

### Scope note

The lease bounds orphaned suspension time; it does not prove response delivery.
A future stronger design may expose an SDK response-outcome hook or capture an
immutable observation snapshot and immediately resume. Neither is part of this
backlog item.

## P1: deterministic wait arming signal

Status: implemented on 2026-07-19.

### Current limitation

Logical breakpoint and exception definitions are armed only inside
`wait_event`. Earlier dogfood and E2E triggers used fixed sleeps. On a slow
machine or a large Spring application, the trigger could reach the target line
before JDWP EventRequest registration finished, causing a safe miss and
timeout.

Increasing sleeps is not a protocol.

### Design constraints

- Provide deterministic evidence that the active waiter has finished arming
  all temporary JDWP requests before the scenario trigger runs.
- Do not create an "armed but no protected waiter" interval.
- Do not rely solely on MCP progress notifications; clients and models may not
  surface them consistently.
- A two-phase protocol must not reintroduce an unbounded suspension window.
- Keep waiter/request-generation identifiers internal. A public token, if
  required, must be opaque and must not expose JDWP or ownership ids.

### Implemented contract

- `wait_mode=blocking` preserves the original one-call behavior.
- `wait_mode=arm` starts the protected waiter and returns only after every
  wait-scoped JDWP request has received a successful install reply.
- The armed result exposes an opaque `wait_handle`, timestamps, and stable
  logical definition ids. It does not expose the internal waiter id or wait
  generation.
- `wait_mode=await` collects the terminal result for that handle. A short
  await timeout returns `status=waiting` without disarming the observation.
- Only one observation may be active. Cleanup, stop, restart, detach,
  cancellation, and shutdown settle it before another Runtime action runs.
- A hit that is never claimed through `await` is automatically resumed after
  a bounded grace period and becomes `WAIT_RESULT_EXPIRED`.
- Real MCP/JVM E2E triggers immediately after `status=armed`, hits the same
  logical breakpoint twice, and verifies that the raw JDWP request id changes.

This unclaimed-result grace protects the gap between `arm` and `await`. It does
not close the broader P0 window after an await/blocking handler returns but
before the MCP SDK delivers its response; that still requires the full
generation-bound delivery/inspection lease described above.

## Release hygiene: stale local wheel

The local `dist/jolink_runtime_debugger-0.1.0-py3-none-any.whl` may predate the
latest lifecycle fixes. `dist/` is gitignored, so this is not a source-tree or
push defect, but installing that wheel can test old behavior under the same
package version.

Confirmed on 2026-07-19: the existing 64 KB wheel was timestamped July 17 and
its extracted Python sources contained none of `wait_mode`,
`_call_wait_event_arm`, or `checkpoint_if_cancelled`. Installing it therefore
does not test the current checkout.

Before any installation or distribution test:

1. remove old local build artifacts;
2. build from the current clean source revision;
3. install the newly built wheel into a fresh environment;
4. record the source revision used for the artifact.

Do not distribute or validate a previously built `0.1.0` wheel after source
changes.

## Deferred/non-issue: extra `status` drain

Do not currently add a second drain after the later `VM.Version` call solely
from the review suggestion.

On an existing persistent JDWP connection, `_connect()` already performs a
`VM.Version` command before returning. Interleaved event packets are queued by
the command path, and `status` then drains pending events. The effective normal
sequence is:

```text
VM.Version barrier -> drain pending events
```

On a new connection, Runtime-owned connection-scoped suspend requests from an
older connection cannot remain active. The request cleanup path also uses a
clear/barrier/drain sequence and disconnects on failure.

Revisit this only if a deterministic reproduction shows an event becoming
pending after the existing `status` drain under a reachable invariant.
