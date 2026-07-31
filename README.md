# joLink Runtime

Run, observe, and debug local Java applications with coding agents.

> **Design principle:** Everything exists to reduce uncertainty for the LLM.

joLink gives coding agents access to real Java runtime behavior instead of
forcing them to rely only on source code, naming conventions, and assumptions.

It can start or restart a local Java application, inspect its status and logs,
and provide runtime evidence for verifying code changes. When surface-level
evidence is not enough, the agent can continue with breakpoints, exception
events, stack frames, and variables.

Free and local. It does not require a joLink account, model API key, inference
provider, or separate agent application.

## Why joLink

Coding agents are good at reading and changing code, but they can become stuck
in a loop of static assumptions:

```text
analyze
-> patch
-> assume the patch works
-> patch again
```

joLink adds the missing runtime feedback loop:

```text
analyze
-> change
-> run
-> observe
-> update the hypothesis
-> change again if necessary
```

This is useful when:

- the Java application is not running yet;
- a code change needs to be verified against real behavior;
- repeated patches have not solved the problem;
- endpoint results do not match the source-code interpretation;
- logs or tests are insufficient to explain the executed path;
- business naming is inconsistent and static search cannot find the relevant
  code;
- deeper runtime evidence such as breakpoints, stacks, or variables is needed.

The goal is not to use a debugger for every problem.

Start with the cheapest useful evidence:

```text
application status
-> logs and actual outputs
-> exception events
-> executed path
-> breakpoints, stack frames, and variables
```

Debug deeper only when necessary.

## What it can do

joLink currently exposes two MCP tools:

- `java_runtime` — run, operate, observe, and debug one local Java application;
- `java_processes` — discover an already-running local JVM when attach is
  needed.

The Java Runtime currently provides 16 public actions:

```text
run
stop
restart
attach
detach
status
logs
breakpoint
exception
wait_event
threads
stack
variables
resume
cleanup_debug_state
update
```

These actions support:

- launching a Java application as an owned JVM process;
- importing an IntelliJ IDEA Application/Spring Boot launch from a Maven
  project, compiling it, resolving its runtime classpath, and launching it
  without first packaging a fat JAR;
- stopping or restarting an application after code changes;
- compiling explicit method-body edits into private staging and HotSwapping
  them into a JVM started through `project_path`, without writing
  `target/classes`;
- inspecting application status and logs;
- attaching to an already-running local JVM;
- setting semantic breakpoints and exception watches;
- waiting for runtime events;
- inspecting threads, stack frames, and variables;
- resuming suspended execution;
- cleaning up debug state safely.

## Current status

Current package version:

```text
0.1.0a3
```

Status:

```text
Alpha / controlled dogfood
```

The first adapter targets local Java applications through JDWP.

The current MCP implementation includes:

- stdio transport;
- stdout reserved exclusively for MCP protocol messages;
- JSON `TextContent` with matching `structuredContent`;
- Runtime `ok=false` mapped to MCP `isError=true`;
- cancellable `wait_event`;
- optional two-phase waiting with `arm` and `await`;
- an optional loopback HTTP trigger started only after JDWP is armed, with a
  one-call `blocking` shortcut or explicit `arm`/`await`;
- wait-scoped JDWP requests;
- ownership-aware shutdown;
- automatic cleanup and resume paths;
- persistent JDWP packet framing across short polling timeouts.

The current two-phase implementation is intended for controlled dogfood.
Known cancellation, cleanup-preemption, handle-publication, and response
delivery limitations are tracked in:

[`docs/stage-2.1.2-lifecycle-backlog.md`](docs/stage-2.1.2-lifecycle-backlog.md)

Do not use this alpha release for unattended production JVM debugging.

## Requirements

- JDK 8 or newer
- [uv](https://docs.astral.sh/uv/)

`uv` manages the Python environment automatically. A separate Python
installation is normally not required.

Confirm the requirements with:

```bash
java -version
uv --version
```

## Install

### 1. Install uv

Install `uv` once if it is not already available.

Windows PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

macOS or Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Add joLink to the MCP client

Many MCP clients support a stdio server configuration similar to the following:

```json
{
  "mcpServers": {
    "jolink-runtime": {
      "command": "uvx",
      "args": ["jolink-runtime@0.1.0a3"]
    }
  }
}
```

`uvx` downloads the package into an isolated environment and caches it
automatically. No repository clone, virtual environment, or source checkout is
required.

The exact configuration file varies by MCP client.

Restart the MCP client after changing its configuration.

## Quick start

After the MCP server is connected, confirm that these tools are available:

```text
java_runtime
java_processes
```

Open a local Java project and ask the coding agent:

```text
Use joLink to start this Java application, inspect its status and logs,
and verify the latest code changes against real runtime behavior.
```

For a problem that has already survived multiple attempted fixes:

```text
Do not apply another speculative patch yet.

Use joLink to run the current Java application and collect actual runtime
evidence. Start with status, logs, tests, and actual outputs. Re-evaluate the
root-cause hypothesis before changing the code again.
```

For deeper investigation:

```text
Use joLink to reproduce this issue.

Start with actual outputs and logs. If that evidence is insufficient, use a
breakpoint or exception watch, inspect the relevant stack frames and variables,
then resume or clean up the suspended JVM.
```

joLink starts and observes the Java application. The coding agent may use its
normal HTTP, terminal, browser, or testing tools to trigger the scenario.

For a method-body edit in an application launched with `project_path`, the
agent can avoid a full Maven rebuild/restart:

```text
status (confirm runtime_active and fast_update.available=true)
-> update(source_files=[the explicit edited Java files])
-> trigger a fresh request
-> verify the new runtime behavior
```

`update` compiles only to private staging and applies a runtime-only HotSwap.
It rejects class-structure and metadata changes and never silently falls back
to a full Maven build. A successful HotSwap is not proof of business
correctness, so the fresh verification request is required. P0 uses the
selected module's Maven compile classpath and a verified Java target model; it
fails closed when annotation processing or bytecode transformation may affect
the selected build. Breakpoints in redefined classes become stale and must be
set again against the current source. `fast_update.available=true` means the
launch is eligible to try this bounded path, not that every Maven compiler
plugin or source edit is supported.

## Typical workflow

A normal verification flow looks like this:

```text
read the code
-> change the code
-> java_runtime(run or restart, ready_port=<application port>)
-> if startup_state=starting, call java_runtime(status) until ready
-> if startup_state=failed, inspect java_runtime(logs)
-> trigger a test or endpoint
-> inspect the actual result
-> update the diagnosis
```

A deeper debugging flow looks like this:

```text
run or attach
-> for an owned HTTP application, confirm startup_state=ready
-> configure a breakpoint or exception watch
-> for a managed local HTTP request:
   wait_event(wait_mode=blocking, http_trigger=...)
-> otherwise:
   wait_event(wait_mode=arm)
   -> trigger the scenario after status=armed
   -> wait_event(wait_mode=await, wait_handle=...)
-> inspect stack frames and variables
-> resume or cleanup_debug_state
```

For a local HTTP endpoint, `blocking` composes the existing
`arm -> trigger -> await` lifecycle into one call:

```json
{
  "action": "wait_event",
  "wait_mode": "blocking",
  "timeout": 30,
  "http_trigger": {
    "method": "POST",
    "url": "http://127.0.0.1:8080/example",
    "json_body": {"id": 1},
    "timeout_seconds": 30
  }
}
```

Use explicit `arm` followed by `await` when an external action must occur
between arming and observation. A terminal result consumes its `wait_handle`;
the handle observes Runtime events and is not an HTTP-response handle.

For an HTTP application launched by joLink, distinguish process/debugger
startup from application TCP readiness:

```json
{
  "action": "run",
  "jar_path": "target/app.jar",
  "jdwp_port": 5005,
  "ready_port": 8080,
  "startup_wait_timeout_seconds": 30
}
```

`startup_wait_timeout_seconds` limits only how long that `run` call waits.
It defaults to 30 seconds and is capped at 60 seconds so the MCP call remains
bounded.
If the process is alive but the application port is not accepting connections,
the result remains successful with `startup_state=starting`; the process is
kept alive and `next_action=status`. Each later `status` call probes the stored
port again. `startup_state=ready` means only that the configured loopback TCP
port accepted a connection; it does not prove that every dependency, cache, or
business endpoint is healthy.

When `ready_port` is omitted, joLink reports `startup_state=unverified` rather
than claiming application readiness. An HTTP trigger remains allowed for
attached and otherwise unverified JVMs, but its result includes a warning.
When configured readiness is still `starting`, joLink rejects an HTTP trigger
without sending it.

## Runtime safety

joLink `0.1.0a3` is designed for local, trusted development environments.

Current safety boundaries:

- MCP transport is stdio;
- JDWP access is limited to local JVMs;
- one joLink server controls one Java target at a time;
- a JVM launched by joLink is treated as an owned process;
- an owned JVM may be stopped by joLink;
- an externally started JVM is attached, resumed, and detached;
- an attached JVM is never intentionally terminated;
- raw JDWP requests are armed only while a waiter owns them; logical
  breakpoint and exception definitions persist until removed or cleaned up;
- built-in HTTP triggers accept only `http://127.0.0.1`, do not use environment
  proxies or redirects, and never return the request URL, headers, body, or
  their raw values in validation errors;
- a configured `ready_port` must be unused before launch and must differ from
  the JDWP port; the TCP probe is local and does not send an application
  request;
- `response_headers_received` reports only the HTTP status/response headers;
  joLink does not read or return the response body;
- cancelling an HTTP client wait closes joLink's side of the connection but
  cannot guarantee that server-side business work has been undone;
- successful `cleanup_debug_state` includes its own debug-state verification;
  a separate HTTP cleanup state may remain `settling` without delaying JVM
  cleanup;
- after receiving a `suspension_id`, the agent must call `resume` or
  `cleanup_debug_state`.

Do not expose the JDWP port to an untrusted network.

Do not use the current alpha release for remote or production debugging.

## Client notes

### CodeBuddy

Some current CodeBuddy environments may initially display:

```text
Description: No description
```

The full joLink tool description and action schema remain available after the
tool definition is loaded. This is a client-side discovery limitation rather
than a joLink runtime failure.

A project-level agent rule can improve discovery:

```markdown
## joLink Java Runtime

For local Java application tasks, use the `jolink-runtime` MCP to start or
restart the application, inspect status and logs, and verify code changes
against real runtime behavior.

When actual outputs and logs are insufficient, use its breakpoints, exception
events, stack frames, and variables for deeper investigation.

After inspecting a suspended JVM, always call `resume` or
`cleanup_debug_state`.
```

## Development

Clone the repository and install development dependencies:

```bash
uv sync --extra dev --locked
```

Run the default test suite:

```bash
uv run pytest
```

Run the stdio server from the source checkout:

```bash
uv run jolink-runtime
```

Equivalent module entry point:

```bash
uv run python -m jolink_runtime.transport.stdio
```

A generic MCP client configuration can launch it directly from a checkout:

```json
{
  "mcpServers": {
    "jolink-runtime": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/jolink-runtime",
        "run",
        "jolink-runtime"
      ]
    }
  }
}
```

## Tests

The real subprocess acceptance test exercises the MCP stdio boundary:

```bash
uv run pytest -q tests/e2e/test_stdio_mcp.py
```

It performs:

```text
initialize
-> tools/list
-> java_runtime(status)
-> close the stdio client
-> wait for the server process to exit
```

The heavier real MCP/JVM suite is opt-in locally:

```bash
JOLINK_RUN_MCP_JAVA_E2E=1 \
  uv run pytest -q -m mcp_java_e2e tests/e2e/test_stdio_mcp_java.py
```

The canonical CI environment for the heavier suite is:

```text
Linux
Python 3.11
JDK 17
```

## Contracts

- MCP v0.1:
  [`docs/mcp-contract-v0.1.md`](docs/mcp-contract-v0.1.md)
- Runtime lineage 2.4.0:
  [`docs/runtime-lineage-contract-2.4.0.md`](docs/runtime-lineage-contract-2.4.0.md)
