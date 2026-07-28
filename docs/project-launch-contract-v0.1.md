# joLink Project Launch Contract v0.1

Contract-Version: `0.1`

Implementation-Status: `implemented for Alpha dogfood`

The P0 contract is implemented and advertised through the existing
`java_runtime` Tool. The first Alpha path is IDEA → Maven → JVM; Eclipse,
Gradle, HotSwap, and persisted launch-plan caching remain outside this scope.

This extension lets joLink import an existing IDE launch configuration,
compile the corresponding Maven project, resolve a runtime classpath, and
launch the application without requiring a prebuilt fat JAR or writing files
into the user's project.

## Scope and lifecycle owner

One joLink Runtime slot owns:

```text
at most one active LaunchAttempt
→ at most one managed JVM
```

The slot is the Runtime instance selected by `SessionManager.session_key`.
The default stdio server currently uses one slot, but project-launch state is
not tied to an MCP connection and must not be made global across future
Runtime slots.

An attempt is not scoped to an individual MCP request or a transient
ClientSession identity while its server process remains alive. `stop`,
`restart`, and server shutdown are the lifecycle controls.

The current stdio server exits on transport EOF/disconnect. That server
shutdown cancels its active build and stops its owned JVM; a new stdio server
does not inherit the old in-memory attempt. P0 does not promise survival
across server-process reconnects.

P0 supports:

- IDEA `Application` and Spring Boot application configurations;
- the IDEA `Make` intent, implemented by joLink through Maven;
- a single-module Maven project or a reactor module that maps uniquely;
- distinct build and runtime JDKs;
- classpath launch, including a Windows/JDK 8 pathing-JAR fallback;
- application TCP readiness through the existing `ready_port` contract.

P0 does not implement Eclipse, Gradle, HotSwap, arbitrary IDEA before-launch
tasks, project-local `.jolink` files, or parallel launch attempts.

## IDEA import boundary

The importer reads, in deterministic order:

```text
.run/*.xml
.idea/runConfigurations/*.xml
.idea/workspace.xml
```

It accepts only IDEA `Application` and Spring Boot application
configurations. Default templates are ignored. Duplicate configurations with
the same effective runtime intent are collapsed even when IDEA stores one as
`Application` and another as `Spring Boot`. Conflicting configurations are
never selected by fuzzy matching. With multiple differently named candidates
the caller must provide the exact case-sensitive `launch_name`. If
non-equivalent configurations share one name, the caller must rename, remove,
or align them in IDEA because the name alone cannot disambiguate them.

The importer:

- reads regular, non-symlink files contained by the canonical project root;
- bounds XML bytes, element count, and nesting depth;
- rejects DTD and entity declarations;
- expands only `$PROJECT_DIR$`, `$MODULE_DIR$`,
  `$MODULE_WORKING_DIR$`, and `$USER_HOME$`;
- never executes an IDEA before-launch task;
- imports only an explicitly enabled `Make` task as
  `build_before_run=true`;
- imports Spring Boot `ACTIVE_PROFILES` into the JVM launch intent;
- rejects remote targets, other enabled before-launch tasks, and
  `PASS_PARENT_ENVS=false` instead of silently changing their semantics;
- retains environment values only in the in-memory `LaunchIntent`;
- returns candidate and error summaries containing environment names, never
  values.

Unknown configuration types and unresolved macros produce structured
failures or safe source warnings; joLink does not guess their meaning.

## Public action semantics

The existing actions remain `run`, `status`, `stop`, `restart`, and `logs`.
Project launch adds parameters to `run`/`restart`; it does not add another MCP
Tool or another lifecycle action.

### `run`

With no active attempt and no managed JVM, `run(project_path, launch_name)`
creates one background `LaunchAttempt` and immediately returns its current
snapshot. The implementation always uses the same asynchronous path; it does
not switch between synchronous and background implementations based on build
speed.

When an attempt is active, `run` does not cancel or replace it:

```json
{
  "ok": false,
  "error_code": "LAUNCH_ALREADY_IN_PROGRESS",
  "retryable": true,
  "attempt_id": "launch_123",
  "launch_phase": "compiling",
  "suggested_next_step": "Call status to observe this attempt, or call restart to replace it explicitly."
}
```

When a JVM is already managed, `run` does not replace it:

```json
{
  "ok": false,
  "error_code": "RUNTIME_ALREADY_RUNNING",
  "retryable": true,
  "process_state": "running",
  "suggested_next_step": "Call status to inspect it, or call restart to replace it explicitly."
}
```

This intentionally changes the legacy direct-launch behavior in which a
second `run` silently stopped the current owned JVM. The integration commit
must update direct and project launch atomically so `run` has one meaning.

### `restart`

`restart` explicitly replaces the current attempt or managed JVM:

```text
cancel current LaunchAttempt or stop/detach current JVM
→ wait for ownership-safe settlement
→ generation + 1
→ create a new LaunchAttempt
```

Every worker result carries its generation. A stale generation may update
neither public state nor the managed JVM and must never start a late process.

If neither a launch intent nor a previous direct-launch request can be reused,
`restart` returns `NO_RESTARTABLE_LAUNCH`.

### `stop`

`stop` is the uniform, idempotent cancellation entry point:

- importing/resolving/compiling: request cancellation and terminate every
  supervised external process tree;
- starting/waiting/ready: stop the exact owned JVM, or detach an attached JVM;
- idle/failed/cancelled/stopped: return an already-settled success.

`stop` does not require the caller to know whether Maven or Java is active.

### `status`

`status` describes two independent layers:

- `launch_phase`: where the project-launch pipeline is;
- `process_state`: the existing JVM state vocabulary
  `absent|running|exited`.

Before a JVM exists, `process_state=absent` and `startup_state` is omitted.
`startup_state=unverified` means a JVM exists but no readiness port was
configured; it must not be used to mean "the build has not started Java yet."

Build progress may include a small, bounded, redacted
`build.log_tail`. It must never make `status` wait for the complete build or
return an unbounded log.

The IDEA `Make` intent is implemented by invoking the project's Maven
`compile` lifecycle. Maven and the configured compiler plugins decide whether
that build recompiles individual files or the complete module; joLink does not
claim a separate file-level incremental compiler.

### `logs`

`logs` retains its current meaning: a bounded snapshot of stdout/stderr from
the owned JVM launch. It never changes to Maven output while a build is
running.

Build output is exposed only through:

- bounded `status.build.log_tail`;
- bounded `build_log_tail` on a build failure.

Both paths use the same redaction boundary for common password, secret, token,
API/access/private-key, credential, cookie, authorization, CLI-property, and
URL-userinfo forms.

## State machine

```text
idle
→ importing_launch
→ resolving_build
→ compiling                 (optional when build_before_run=false)
→ resolving_runtime
→ starting_jvm
→ waiting_readiness         (optional when readiness is not configured)
→ runtime_active
```

Any non-terminal phase may fail. Work before `runtime_active` can enter
`cancelling → cancelled`; an active JVM uses `stopping → stopped`.

`runtime_active` means the launch pipeline has produced a live managed JVM.
It deliberately does not use the word `ready`: when no `ready_port` is
configured the associated `startup_state` remains `unverified`, and joLink
must not imply TCP or business readiness. With configured readiness,
`runtime_active` is entered only after `startup_state=ready`.

The public phases are:

```text
idle
importing_launch
resolving_build
compiling
resolving_runtime
starting_jvm
waiting_readiness
runtime_active
cancelling
cancelled
failed
stopping
stopped
```

## Result and error invariants

Project-launch failures use the existing Runtime result contract:

```text
ok=false
error_code=<stable machine code>
retryable=<boolean>
suggested_next_step=<one concrete recovery instruction>
```

They do not return a `code` alias. `next_action`, when present, names one
callable action such as `status`; alternatives belong in
`suggested_next_step` or copyable `suggested_next_actions`.

A failure that rejects the current Tool action uses the envelope above and is
returned with MCP `isError=true`. By contrast, when a background attempt
fails and a later `status` successfully observes it, `status` remains
`ok=true`/`isError=false` and returns:

```json
{
  "launch_phase": "failed",
  "process_state": "absent",
  "launch_error": {
    "error_code": "BUILD_FAILED",
    "message": "The supervised Maven process exited unsuccessfully.",
    "retryable": true,
    "suggested_next_step": "Inspect build.log_tail, correct the build failure, and call run again."
  }
}
```

An `attempt_id` is a diagnostic correlation id, not a required argument for
`status`, `stop`, or `restart`. P0 does not expose a separate
`runtime_session_id`; the Runtime slot, process ownership, and PID already
identify the only managed target.

## Process supervision and cancellation

Every external command belongs to the active attempt and is executed by the
same process supervisor, including:

- Maven/JDK detection commands;
- Maven compilation;
- Maven runtime-classpath resolution;
- any command used to materialize the JVM launch.

Adapters create inert operation specifications; they must not privately spawn
subprocesses. Long work occurs outside the Runtime state lock. Only generation
checks and state publication use short critical sections.

Cancellation terminates the POSIX process group plus any previously observed
identity-bound descendants that escaped that group. On Windows, the Alpha
uses `taskkill /T` plus identity-bound `psutil` fallback; strict no-escape
containment through a Windows Job Object remains a pre-stable follow-up.
Server shutdown requests cancellation, waits a bounded grace period, then
performs an ownership-safe forced release.

## Secrets and persistence

IDE-imported environment values may exist only in a materialized in-memory
plan. Sensitive fields are excluded from normal repr, but dataclass
introspection is not a security boundary: only the approved redacted-summary
serializers may feed Tool results, logs, errors, fingerprints, or persisted
files. Serializable summaries contain environment variable names only.

P0 initially runs without a workspace registry. A later commit may write a
last-successful plan under `~/.jolink`, never inside the project.

A plan is eligible for success caching when:

- compilation and runtime resolution succeeded;
- the JVM/JDWP launch succeeded and the process remains running;
- when readiness is configured, `startup_state=ready`;
- when readiness is not configured, the plan records
  `readiness_verified=false` instead of pretending business readiness.

Only the current generation can publish a cache entry. The cache contains no
environment values, credentials, cookies, HTTP headers, or active attempt
state.

## Implementation status

Implemented:

1. Frozen contract and executable state vocabulary.
2. Safe, read-only IDEA importer.
3. Process-tree supervision and generation-safe `LaunchAttempt` ownership.
4. Maven compile/runtime-classpath resolution and Java command
   materialization.
5. Integration into `run`, `status`, `stop`, `restart`, and shutdown.
6. Unit/contract coverage plus real stdio MCP E2E for a single module and a
   `shared → app` reactor dependency using the current workspace classes.

Still requires broader Alpha dogfood:

- real Windows/JDK 8 project launch and cancellation;
- Windows Job Object containment for wrapper processes that can exit before
  their descendants are observed;
- slow corporate Maven builds and readiness transitions;
- varied IDEA/Maven multi-module layouts.

Last-successful-plan persistence remains deferred until the uncached path is
stable.
