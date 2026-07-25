# Java Runtime dogfood logging

Java Runtime writes structured lifecycle events to Hermes' normal log files.
The default `INFO` level is intended for day-to-day dogfood:

```bash
hermes logs -f
```

Useful event names include:

- `java_runtime.action.start` / `java_runtime.action.finish`: every tool action,
  result, duration, and returned object counts.
- `java_runtime.process.*`: JVM spawn, readiness, exit, timeout, and shutdown.
- `java_runtime.jdwp.connect.*`: debugger connection and negotiated ID sizes.
- `java_runtime.breakpoint.*`: breakpoint creation, wait, timeout, and hit.
- `java_runtime.exception.*`: exception event creation, removal, wait, and hit.
- `java_runtime.suspension.*`: suspension invalidation and resume.
- `java_runtime.threads.observed`, `java_runtime.stack.observed`, and
  `java_runtime.variables.observed`: observation counts and completeness.

Warnings and failures can be watched separately:

```bash
hermes logs errors -f
```

For a protocol-level investigation, temporarily set the following in
`~/.hermes/config.yaml`, restart Hermes, and follow the log:

```yaml
logging:
  level: DEBUG
```

```bash
hermes logs -f --level DEBUG
```

`DEBUG` records JDWP command IDs, command sets, error codes, byte counts, and
latency. It does not record protocol payloads. Runtime logs also omit variable
values, application arguments, JVM argument values, and captured console text.
The Java application's own console output remains in the `log_file` returned by
the `run` action and is available through `java_runtime(action="logs")`.

## Launching a Spring Boot executable JAR

Use `jar_path` instead of overloading `classpath` or inventing a main class:

```json
{
  "action": "run",
  "jar_path": "C:\\work\\demo\\target\\demo.jar",
  "app_args": ["--spring.profiles.active=local"],
  "jdwp_port": 5005,
  "ready_port": 8080,
  "startup_wait_timeout_seconds": 30
}
```

`jar_path` selects `java -jar` mode. `main_class` selects `java -cp` mode; the
two fields are intentionally mutually exclusive.

When `ready_port` is configured, do not use logs to guess whether the service
is ready. If `run` returns `startup_state=starting`, keep the JVM running and
call `status` until it reports `ready` or the process exits. The readiness
probe verifies only that the local TCP port accepts connections.

## Slow-start readiness regression

Use a local Java service that opens its application port at least three
seconds after JDWP becomes reachable.

1. Launch it with a free `jdwp_port`, its application `ready_port`, and
   `startup_wait_timeout_seconds=0.1`.
2. Confirm `run` returns `ok=true`, `status=process_started`,
   `startup_state=starting`, `startup_wait_timed_out=true`, and
   `next_action=status`. The PID must remain alive.
3. Before the port opens, attempt `wait_event(wait_mode=arm,
   http_trigger=...)`. Confirm `APPLICATION_NOT_READY` and
   `http_trigger_sent=false`; no request or waiter may be created.
4. Call `status` until it returns `startup_state=ready`. Confirm
   `readiness.type=tcp_port`, the expected port, and `verified=true`.
5. Call `status` again. `ready_observed_at` and the completed
   `startup_elapsed_ms` must remain stable.
6. Arm the same HTTP trigger after readiness. It must follow the normal
   arm/await flow.
7. Stop the JVM and call `status`. It must report `process_state=absent`
   without retaining `starting` or `ready`.

Additional guards:

- Omit `ready_port`: `startup_state` must be `unverified`, never `ready`.
- Occupy `ready_port` before `run`: expect `READY_PORT_ALREADY_IN_USE`, and
  confirm no new JVM was spawned.
- Set `ready_port` equal to `jdwp_port`: expect
  `READY_PORT_CONFLICTS_WITH_JDWP`.
- Restart without readiness arguments: confirm the prior port and wait timeout
  are reused with `readiness_config_source=previous_run`.

## Breakpoint and variable inspection tips

List active breakpoints before or after `resume`:

```json
{"action": "breakpoint", "bp_action": "list"}
```

Remove one breakpoint by the stable `breakpoint_id` returned from `set` or
`list`:

```json
{"action": "breakpoint", "bp_action": "remove", "breakpoint_id": "bp_001"}
```

`remove` can also filter by `class_pattern` and/or `line`. Calling `remove`
with no selector still clears all breakpoints for backward compatibility.

`variables` skips the local variable named `this` by default because Spring
beans often expand into a huge dependency graph. Use `include_this=true` only
when the receiver object is important. Use `max_value_depth` to control object
expansion depth; the default is `1`.

## Exception events

Use exception events when an API returns a vague framework error and you need
the exact throw location:

```json
{
  "action": "exception",
  "exception_class": "java.lang.NullPointerException"
}
```

`exception_class` is normalized internally, so these forms are equivalent:

- `java.lang.NullPointerException`
- `java/lang/NullPointerException`
- `Ljava/lang/NullPointerException;`

Common `java.lang` simple names such as `NullPointerException` and
`NumberFormatException` are also accepted.

If the exception class is not loaded yet, Runtime returns
`error_code=exception_class_not_loaded`, `retryable=true`, and
`next_action=trigger_code_path_then_retry_exception_set`.

Breakpoint and exception definitions are armed only while `wait_event` is
active. Prefer the deterministic two-phase form. First arm and keep the
returned `wait_handle`:

```json
{"action": "wait_event", "wait_mode": "arm", "timeout": 30}
```

After `status=armed`, start the trigger without waiting for its HTTP result,
then collect the event:

```json
{
  "action": "wait_event",
  "wait_mode": "await",
  "wait_handle": "<wait_handle>",
  "timeout": 30
}
```

No guessed sleep is required. Do not synchronously wait for an HTTP trigger
that can suspend at the breakpoint, because it cannot finish until `resume`.
The original blocking form remains available for compatibility.

This lifecycle is intentional:

- A trigger that runs with no active waiter must continue normally and must
  not leave the JVM suspended.
- After `wait_event` times out, its JDWP requests have been disarmed. A later
  trigger must also continue normally.
- A hit ends that wait and disarms its JDWP requests. After inspection and
  `resume`, arm a new `wait_event`, trigger a new request after `status=armed`,
  and await the returned `wait_handle` to observe the next hit. The stable
  breakpoint/exception definition is re-armed for that new wait.
- Do not expect one HTTP request to pause at several breakpoints across
  several `wait_event` calls. For an ordered multi-breakpoint investigation,
  replay the scenario once per observation point and remove earlier
  breakpoints before waiting for a downstream one.

An exception hit includes both `throw_location` and the backward-compatible
`location` field. `throw_location` may point into JDK or framework code, so
inspect the stack to find the first relevant application frame.

Specific exceptions default to `caught=true` and `uncaught=true`, because Spring
and similar frameworks often catch the real exception and wrap it into a generic
API response. Broad caught exception watches are refused by default:

```json
{
  "action": "exception",
  "exception_class": "java.lang.Exception",
  "caught": true
}
```

That request can be extremely noisy in Spring/MyBatis/etc. Use a specific
exception class instead. If you intentionally need it, pass
`allow_broad_caught=true`.

List and remove exception events the same way as breakpoints:

```json
{"action": "exception", "exception_action": "list"}
{"action": "exception", "exception_action": "remove", "request_id": 1081}
```

## Stage 2.1 wait-scoped arming regression case

Use one known executable application line and keep the returned logical
`breakpoint_id` for all checks below:

1. Set the breakpoint, but do not call `wait_event`. Trigger the line in the
   foreground. The request must complete, `status` must not report a
   suspension, and `breakpoint list` must still contain the definition.
2. Call `wait_event` with a short timeout and let it return `timeout`. Trigger
   the line afterwards. The request must again complete without suspending the
   JVM.
3. Call `wait_event` with `wait_mode=arm`. For a local HTTP scenario, include
   `http_trigger`; otherwise wait for `status=armed` and start the trigger
   externally without blocking. Then `await` its handle.
   Confirm `breakpoint_hit`, inspect if useful, and `resume` with the returned
   `suspension_id`.
4. Without setting the breakpoint again, arm a second observation, trigger
   immediately after its armed result, then await it. Confirm another hit with
   the same logical `breakpoint_id`, then resume. A nested raw JDWP request id
   should differ and is diagnostic only.
5. Repeat the no-wait and active-wait checks with a specific exception watch
   when the test application has a deterministic exception path.

Always finish with breakpoint/exception removal and
`cleanup_debug_state`. A foreground request hanging in steps 1 or 2, a stale
suspension in `status`, or a definition disappearing after timeout is a
Stage 2.1.1 regression.

## Arm-bound HTTP trigger regression case

Run this case against a disposable local endpoint on `127.0.0.1` whose request
path crosses a known executable breakpoint:

1. Set one focused breakpoint. Call `arm` with `http_trigger`; verify the
   request is not received before JDWP is armed, `arm` returns without waiting
   for the HTTP response, and its output includes the exact
   `required_next_action=await` shape.
2. Await the returned handle. Verify a hit includes the same handle,
   `suspension_id`, and bounded `http_trigger` state, but never echoes the URL,
   headers, request body, response body, or credentials.
3. Resume the suspension and verify the original HTTP handler can complete.
4. Let an HTTP response complete without hitting the breakpoint. `await` must
   expose `response_headers_received` and return `status=waiting` rather than
   falsely declaring that no later event is possible. Add a delayed asynchronous
   Java path and verify the same handle can still hit after the 204 response.
5. Use an unused loopback port. A definite connection failure must return
   `HTTP_TRIGGER_FAILED`, disarm the wait, and leave no suspension or active
   waiter.
6. Race event publication against connection failure. A result published at
   the atomic decision point must win; no hidden suspension may remain.
7. Start a long `await`, then concurrently call each of
   `cleanup_debug_state`, `stop`, `restart`, and `detach` in applicable target
   states. The lifecycle action must not wait for the await deadline or one or
   more HTTP client grace periods. Cleanup must return its own verification
   counts; a pending HTTP cancellation is reported separately as `settling`.
8. Start two concurrent `await` calls for one handle. Exactly one owns it; the
   other returns `WAIT_HANDLE_IN_USE` without consuming the result.
9. Shut down the MCP server while both the Runtime waiter and HTTP client are
   active. Verify an owned JVM is stopped, an attached JVM is resumed/detached
   and still functional, and no HTTP/JDWP worker remains capable of publishing
   a stale suspension.
10. Reject non-loopback URLs, HTTPS, redirects as follow-up targets, URL
    credentials/fragments, unsupported methods, unsafe framing headers,
    oversized headers/bodies, and `http_trigger` used outside `wait_mode=arm`.
    Assert that schema and parser errors contain no supplied URL, header name,
    header value, or body value. Verify auto-added JSON `Content-Type` is
    included in the 32-header and 16-KiB limits.
11. Let the Runtime wait finish with a terminal non-suspension result while the
    HTTP client is still waiting. The returned trigger state must show client
    cancellation requested, and the forgotten handle must not leave an
    uncancelled background client wait.

Run the same breakpoint once more through the existing external background
trigger flow to confirm backward compatibility. HTTP client cancellation is
only evidence that joLink stopped waiting or reading; it must never be reported
as proof that application-side business work was rolled back.
