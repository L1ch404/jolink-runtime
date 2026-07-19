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
  "jdwp_port": 5005
}
```

`jar_path` selects `java -jar` mode. `main_class` selects `java -cp` mode; the
two fields are intentionally mutually exclusive.

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
  `resume`, start a new `wait_event` and trigger a new request to observe the
  next hit. The stable breakpoint/exception definition is re-armed for that
  new wait.
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
3. Call `wait_event` with `wait_mode=arm`, wait for `status=armed`, start the
   trigger immediately without a guessed delay, then `await` its handle.
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
