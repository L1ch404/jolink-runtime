# joLink Runtime MCP Contract v0.1

Status: current stdio product interface, persistent JDT compilation and
deterministic two-phase event waiting. Historical experiments are indexed
separately in [the archive](archive/README.md).

This is the client-facing MCP contract. The migrated implementation it wraps
is frozen separately in
[`runtime-lineage-contract-2.4.0.md`](runtime-lineage-contract-2.4.0.md).

## Identity and versions

- Package prerelease version: `0.1.0a6`
- MCP Server name: `jolink-runtime`
- Migrated Runtime lineage: `2.4.0`
- Runtime lineage is not independently published or incremented during the
  migration.

## Exposed tools

- `java_application`: `launch`, `attach`, `restart`, `stop`, `detach`
- `java_fast_test`: optional `action=run` (default), `action=cancel`, `action=result`
- `java_status`: `processes`, `status`, `logs`
- `java_debugger`: `breakpoint`, `exception`, `wait_event`, `threads`,
  `stack`, `variables`, `resume`, `cleanup_debug_state`

`java_status(status)` returns a compact overview with readiness, process/debug
state, current operation and recent restart/Test summaries. Internal Worker/cache
configuration, output/source inventories, detailed timings and full reload/launch
errors are available on demand through `java_status(action=status, details=true)`. The last reload
and launch-error summaries include a `next_action` requesting that flag. No extra
status action is added. This is a
current-state observation, not an archive of earlier launches or reloads.
Completed `launch/restart` calls return their own detailed outcome. The optional
status flag retrieves the later outcome if the original call returned pending.
Both status modes omit build-log text and do not read the build log.
`java_status(logs)` defaults to `source=application`; `source=build` reads the
current launch's build log with the existing encoding/redaction policy, 512 KiB
scan and 32 KiB log-body return budgets. `tail` defaults to 50, maximum 500.
An absent build log returns `BUILD_LOG_UNAVAILABLE` without starting any work.
Cached launches may not create a new build log because no build-tool invocation
was needed; their compiler errors remain available through `details`.
`mcp.log` continues to be written locally; neither status mode includes
`server_diagnostics` or the private logging configuration.

`java_fast_test` is independent of an application JVM. It requires `project_path`
and explicit `tests` for run, or `test_run_id` for cancel/result. Omitted action is
defaulted by dispatch, not just by a Schema annotation. The old application
test/cancel_test actions are not public aliases. First use obtains the Maven or
Gradle model through the existing Probe and source preparation, then initializes
persistent JDT main/test projects. Later calls detect changed sources (optionally
supplemented by explicit `source_files`), use JDT incremental compilation,
and launch an isolated Java 8 bytecode Test Runner for explicit JUnit 4/5 or TestNG
`Class` or `Class#method` selectors. Test assertion failures are successful Tool
execution (`ok=true`, `passed=false`); compiler, protocol, timeout, and process
failures use `ok=false`. A test never promotes or mutates a Runtime Generation.
Explicit Fast Test `source_files` may describe ordinary Java source additions
or deletions. For deletion, the Worker must return the exact private
`deleted_source_units`; omission poisons the CompileSession even if a class
appears to disappear. Application updates use `java_application(action='restart')`;
Fast Test does not modify the application JVM.

`run` (including its synchronous wait) and `java_status(status).fast_test` return
summaries with state, counts, timings and the original `test_run_id`. Compiler
diagnostics, failed-test stacks and bootstrap error details belong to `result`.
Failures include `next_action = {tool: "java_fast_test", arguments: {action:
"result", test_run_id: ...}}`. Reading details neither reruns nor consumes a test.
`compiled_source_count` is retained; bulk `compiled_source_units` is not returned.
`result` addresses the active or most recently finished attempt by ID in the
current MCP process; unknown, superseded or pre-reconnection IDs return
`TEST_RUN_NOT_FOUND`, never a different test's result. An active attempt may still
be pending. The original outcome semantics also apply to detail reads: assertion
failure is `ok=true, passed=false`; compilation/infrastructure failure is `ok=false`.

Current Fast Test uses independent main/test compiler settings and supports
Maven/Gradle module dependencies; current validation includes Java 8/11/17/21.
See `fast-test-v0.1.zh-CN.md` and the compatibility follow-up for current boundaries.
When Surefire normally
supplies a missing JUnit Platform Launcher, selection is deterministic: a
project-declared launcher, a local exact engine version, or a Maven-resolved
exact engine version wins in that order. Only when the exact version is
unavailable may an already resolved same-major launcher no older than the engine
be used. Version, source, and fallback reason are reported without local paths;
cross-major guessing is forbidden. Unsupported Surefire VM/system
property configuration fails closed. `java_fast_test(action='cancel')` addresses one active
`test_run_id`; `java_status(status)` exposes the current or last TestAttempt.
After any compile failure the private working compile state remains `failed`;
no Test Runner may start until a later compile succeeds. Runner
classpath is passed through a Java 8 pathing JAR Manifest, so dependency count
does not expand the operating-system command line while project classes remain
visible to the normal Application/System ClassLoader. Cancellation is settled
only after the complete TestAttempt exits, not
merely after its currently supervised subprocesses stop. Temporary Maven
settings are deleted immediately after the Probe snapshot is read. The bundled
content-checked Probe coordinate is always seeded into the selected local Maven
repository so implicit offline policy is safe. A source/resource/POM/settings
change is handled by the existing model cache and workspace change detection:
build configuration changes refresh the model; source edits compile incrementally;
runtime resource directories are used directly.

For Maven reactors and Gradle multi-project builds, Probe exports the selected
module and needed upstream modules. Each needed module has persistent JDT
main/test projects in the same Worker workspace. Dependencies determine build
order and incremental propagation. Main/test source levels, encoding and
Processor configuration are independent. Source-only upstream changes do not
force a new Maven/Gradle application compile.

The first release exposes Java only. Future languages receive their own tools
and adapters instead of adding a `language` union to these Java tools.

Gradle coverage includes Wrapper 7.4.2/8.10/8.14, multi-project dependencies,
resolved source roots, build logic, resources and supported APT configurations.
It is not a promise to execute every custom Test task, SourceSet or javac-only
compiler extension. Current unresolved cases and advanced test configuration
boundaries are tracked in [the compatibility follow-up](java-compatibility-followup-2026-09.md).
Target system libraries and the Test executable come from
Gradle's resolved Compiler Toolchain and Test JavaLauncher rather than the
Gradle Daemon JDK.

`wait_breakpoint` remains an internal Runtime-lineage compatibility alias. It
is not advertised or accepted as a public MCP action.

## Application startup readiness

Product `launch/restart` replies include `previous_startup_ms`, captured before
the operation from the same launch's previously observed successful startup,
stored in the local joLink cache. This value is not replaced by the current
result; only an unavailable record yields `null`. It excludes Probe/JDT preparation. With a configured
ready port it measures JVM startup through TCP readiness; otherwise it is only
the completed JVM/JDWP startup. HotSwap and failed startups do not replace the
record. A successful startup immediately writes one JSON file under
`startup-timings/`, using a stable launch-identity filename and file replacement.
The next launch reads that file, including from a different MCP process; repeated
status calls do not rewrite it. There is no TTL or build-input fingerprint check.
Stop and MCP shutdown retain the observation; distinct project/launch
configurations and direct JAR/classpath targets do not share timings. The value
is a waiting reference, not a timeout or a new readiness predicate.

`launch` and `restart` distinguish JVM launch from optional application TCP
readiness.

They support two launch forms:

- direct JVM launch with `jar_path`, or `main_class` plus `classpath`;
- Maven/Gradle project launch with `project_path` plus `main_class`, or an
  imported IDEA configuration selected by optional exact `launch_name`.

IDEA launch configuration is optional when `main_class` is supplied. `java_home`
selects the application JDK, not the build-tool/Worker JDK. Explicit launch
parameters override imported values; see [project launch](project-launch-contract-v0.1.md)
for JDK selection and cached-setting refresh. Maven or Gradle exports the Build World but does not compile
application classes. JDT is initialized before the JVM: a cold workspace uses
FULL build, while a reusable workspace calls `workspace_source_changes()` and
uses INCREMENTAL only when sources changed. The managed JVM directly uses the
persistent JDT class output; resource roots remain direct classpath entries.
`project_path` is mutually exclusive with `classpath` and `jar_path`; it accepts
`main_class`, `java_home`, `app_args`, and `vm_args`.
Before the JVM exists, `status` reports `process_state=absent` plus the
current `launch_phase` and omits `startup_state`.

`restart` replaces the public `reload` action. For a live project JDT session,
it discovers changed sources (including upstream modules), incrementally compiles
once, and prefers HotSwap by default. `hotswap=false`, no pending class changes,
deleted/unloaded/ambiguous classes, generated resource changes, or explicit JVM
redefinition rejection use the resulting workspace output to restart the JVM.
Compilation errors do not stop the existing JVM. A lost HotSwap reply remains
`HOT_SWAP_OUTCOME_UNKNOWN`, not a reason to assume rejection. Force a real restart
with `hotswap=false` when application initialization must run again.
`apply_method=hotswap/restart` states the actual mechanism. Pending requests retain
the existing `reload_id` and are visible under `active_operation` / `last_reload`.
Successful compilation persists before application; HotSwap rejection does not
cause a second compilation. See `project-restart.zh-CN.md` for the complete flow.

The CompileSession freezes Probe-derived Java/resource roots, compile
dependencies, target platform, source encoding, Lombok/JSR-269 processor
inputs, and the configuration fingerprint. Probe facts are persisted locally;
later launches reuse these facts while the tracked build configuration is unchanged.
Changed configuration refreshes through the existing project launch flow (which
stops the old JVM first); there is no Candidate/rollback transaction. Source-only
restart reuses the current compiler and Eclipse's output delta without full output hashes.

The IDEA Make/Build flag is imported as intent metadata but does not run the
build-system compiler. JDT bootstrap completes before JVM startup.

Changed class bytes are sent to JDWP without schema/metadata preflight. JVM
rejection and deleted/unloaded classes select an actual restart. Accepted HotSwap
does not rerun static initialization or imply refreshed framework state.

Startup does not copy class output. The JVM reads the current persistent JDT
output and resource roots directly on its classpath. Runtime scope never reads
the test world.

After a class is redefined, every logical breakpoint belonging to that class
is retained for inspection but marked stale. joLink does not rebind the old
numeric line to potentially different code. The caller must remove and set
those breakpoint definitions again against the current source before arming a
new breakpoint wait. To avoid silently partial evidence, any remaining stale
definition blocks breakpoint arming; the error returns all
`stale_breakpoint_ids` that must be removed or reset.

- `ready_port` is an optional loopback application port. It must differ from
  `jdwp_port`.
- For `java_application(launch/restart)` / `java_fast_test(run)`, `timeout` bounds the whole synchronous
  result wait (default 30, zero for immediate submission). Values above 30
  are accepted and wait only 30 seconds. Expiry returns the original task ID
  and current state, never cancels or resubmits the operation. Tests keep an
  independent internal Runner execution limit of 300 seconds. The old
  readiness-wait argument is removed. Debugger event timeout semantics are
  unchanged.
- Direct JAR/classpath submission still performs the existing process/JDWP
  initialization synchronously. The reply-wait deadline does not interrupt
  that initialization; zero skips the subsequent readiness wait. Project
  launches and Test attempts already submit through background workers.
- Waiting happens outside the MCP control lock. Explicit stop / Fast Test cancel and
  status remain available. Cancelling only the MCP reply wait leaves the
  background operation available to status/stop/Fast Test cancel; server shutdown
  still closes owned work through the existing lifecycle.
- Readiness configuration is stored with the launched process. `status`
  rechecks the same port without reading or interpreting application logs.
- `restart` inherits prior readiness unless replaced. Direct JAR/classpath
  restarts reuse their artifacts; project restarts first incorporate source edits
  through JDT. An explicit project selection uses the project launch path.

## JDT Worker distribution

The product lock identity is part of the cache path. joLink atomically installs
the exact locked Candidate under the user cache, reuses matching Eclipse
bundles from pre-product caches, and downloads only missing official bundles.
The product Worker JAR and Equinox configuration ship with the Python package.
Every file is SHA-256 verified before publication. Integrity errors may expose
the public artifact filename, but never project paths or source content.
Concurrent MCP server installers revalidate an already-published winner after
an atomic-rename race. Worker `Xms`/`Xmx` are either mapped from safe Maven
compiler process-memory arguments or use bounded product defaults.

The product uses Eclipse 4.40 / JDT 3.46 and a managed Temurin 21 Worker JDK,
independent of the project's build, target and application JDK. An explicit
`JOLINK_WORKER_JAVA_HOME` selects an existing compatible Worker JDK instead of
downloading the managed one. The joLink Worker bundle itself has Java 17 bytecode;
this does not imply that the complete Eclipse dependency closure supports every
Java 17 installation. Maven/Gradle Probe and Test Runner artifacts retain Java 8
bytecode. See [JDT and JDK distribution](jdt-346-upgrade.zh-CN.md).

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

If `launch` finishes its bounded wait while the process remains alive, it returns
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
6. For an owned HTTP application, `ready_port` lets `launch/status` distinguish
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

`java_status(action="processes")` does not include an `ok` field. It is successful unless the
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
[`stage-2.1.2-lifecycle-backlog.md`](archive/stage-2.1.2-lifecycle-backlog.md):

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

- `launch` creates a Runtime-owned JVM.
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

- No additional language adapters
- No HTTP MCP transport (the managed loopback request is a scenario trigger,
  not a server transport)
- No setup installer
- No remote JDWP attach
- No caller-managed compiler generations; background result handles such as
  `test_run_id`, `reload_id`, and debug `wait_handle` are returned by joLink
