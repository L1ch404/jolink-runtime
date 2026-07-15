---
name: observation
description: Use the Java Runtime as an LLM-facing Java debugger to diagnose bugs and inspect actual execution paths, exceptions, call stacks, variables, object state, data flow, configuration, permission context, and application behavior.
---

# Java Runtime Debugging and Observation

Think of the Java Runtime first as an LLM-facing debugger for a running
Java application.

These debugging primitives are not limited to bug fixing. They can also be
combined to inspect execution paths, data flow, configuration, permission
context, and actual application behavior.

The Runtime provides real execution-time evidence, including:

- The actual execution path taken by the application
- The actual call stack of a suspended thread
- Method arguments, local variables, the current `this` object, and values available at an observed execution point
- Runtime state at the point where an exception is thrown
- Whether a breakpoint was hit and the executable location it actually matched
- The current debug connection, registered events, and suspended-thread state

Actively consider using the Runtime when an important question depends on actual runtime behavior, especially when source code, logs, tests, or error messages are insufficient to resolve it.

Freely choose and combine Runtime capabilities according to the current task. There is no required fixed workflow.

Do not limit the Runtime to bug diagnosis. Also use it to understand unfamiliar code, inspect runtime configuration, verify permission context, observe data flow, validate application behavior, and explore other runtime-dependent questions.

## Quick Start

The following is an illustrative first-use flow, not a required workflow.
Choose and combine different actions when the task or application lifecycle
requires them.

1. Make a Java application available:
    - use `run` for an application launched and managed by the Runtime, or
    - use `attach` for an already running local JVM.
2. Call `status` to confirm the process and debug connection state.
3. Set a focused `breakpoint`, or register an `exception` watch when
   investigating a thrown exception.
4. Trigger the relevant scenario without blocking the agent:
    - when using a tool that supports background execution, such as
      `background=true`, use that mode;
    - otherwise trigger it manually, from another terminal/client, or through
      another non-blocking asynchronous mechanism.

   Do not wait synchronously for a request that may pause at the breakpoint,
   because the request may not complete until the suspension is resumed.

   After resuming the suspension, inspect the background task or request result
   when its outcome is relevant to the investigation.

5. Call `wait_event` and keep the returned `suspension_id`.
6. Use `stack` to inspect the actual call path of the suspended event thread.
7. Use `variables` only on the relevant stack frames and objects.
8. Call `resume` with the active `suspension_id` after completing the required
   inspection.
9. Repeat the observation loop only when more evidence is required.
10. When finished:
    - If the debug connection will remain active, remove breakpoints or
      exception watches that are no longer needed.
    - Use `stop` to end an application launched and owned by the Runtime.
    - Use `detach` to end debugging of an externally attached JVM without
      stopping it.
    - Reserve `cleanup_debug_state` for inconsistent state or suspected
      residual suspensions or event requests.

### Choosing how to launch

- Use `jar_path` for an executable JAR, including a Spring Boot fat JAR.
- Use `main_class` with `classpath` for compiled classes or a
  classpath-based application.
- Do not provide both `jar_path` and `main_class`.
- Use `attach` instead of `run` when the JVM is started by an IDE, script, or
  another process manager.
- For a containerized JVM, attach is supported only when its JVM PID is visible
  to the Runtime locally and its JDWP endpoint is reachable through localhost.

### First-use notes

- The JDWP port is the debugger connection port; it is not the application's
  HTTP or service port.
- A successful JDWP connection does not prove that application startup has
  completed or that its service port is ready.
- `logs` contains only stdout and stderr captured from an application launched
  by this Runtime. It does not capture output from an externally attached JVM.
- If a port is already occupied or an earlier process may still be alive,
  inspect `status` and the local process state before repeatedly calling `run`.

### Preparing a trigger request

Before triggering an endpoint, determine the target environment, application
host and port, base path, readiness state, HTTP method, authentication, and
required parameters.

Prefer to derive the request from controllers, DTOs, frontend call sites,
OpenAPI documentation, tests, or existing examples. Do not invent required
identifiers, credentials, or production business data. Synthetic data is
acceptable only in a clearly disposable local or test environment.

If required runtime values such as a token, user ID, or task ID remain unknown,
ask the user only for the missing information.

Before triggering production writes or operations with destructive,
irreversible, or external side effects, obtain explicit user confirmation or
ask the user to trigger the operation manually.

Prefer an existing authenticated client or temporary, test-only,
least-privileged credentials. Do not extract and reuse credentials from logs or
Runtime observations, and do not unnecessarily expose full credential values.

Trigger requests that may hit a breakpoint through a non-blocking mechanism,
such as `background=true`. If credentials cannot be safely provided, ask the
user to trigger the scenario manually.

## 1. Distinguish facts, inferences, and unknowns

- Treat information directly observed through the Runtime as an observed fact about the current execution.
- Treat an explanation derived from observed facts as an inference.
- Leave anything that has not been observed unknown; do not fill it in through speculation.
- Do not treat two events occurring together as proof of a causal relationship.
- Clearly separate observed evidence from assumptions and hypotheses in conclusions.

## 2. Respect suspension lifecycles

- Inspect stack frames and variables only while the corresponding thread suspension is active.
- After `resume`, do not reuse previously returned frame references, variable references, or object references; they may be invalid.
- Do not leave application threads suspended longer than necessary.
- Resume the suspended thread after completing the required inspection.
- Use `cleanup_debug_state` only when the current state is inconsistent or suspended threads, breakpoints, or event requests may have been left behind. For normal completion, `resume` and `remove` the relevant watches, or use `stop`/`detach` as appropriate.
- Do not interpret observations made from stale or invalid suspension state as reliable evidence.

## 3. Interpret breakpoint locations correctly

- Do not assume that every source-code line has a corresponding executable bytecode location.
- Treat `nearby_locations` as nearby executable candidates, not as locations guaranteed to have the same business meaning as the requested source line.
- When the actual breakpoint location differs from the requested location, distinguish `requested_line` from `matched_line` when those fields are available.
- Do not blindly retry line numbers by adding or subtracting one when an executable location is unavailable.
- If a breakpoint is not hit, do not immediately conclude that the code was not executed.

Possible causes of a breakpoint not being hit include:

- The expected request or operation was not triggered
- The target class has not been loaded
- The source code does not match the bytecode running in the JVM
- A proxy or generated class was matched incorrectly
- The selected source line has no executable location
- The debug session contains stale or inconsistent state
- The application is suspended elsewhere
- The actual execution path differs from the expected path

Use the available evidence to distinguish between these possibilities.

## 4. Understand the boundaries of Runtime evidence

- Treat a stack trace as the call chain of the currently suspended thread only.
- Do not assume it represents execution across thread pools, asynchronous callbacks, message queues, RPC boundaries, reactive pipelines, or distributed services.
- Treat observed Mapper arguments and return values as proof of inputs and outputs at the data-access boundary, not proof of the final SQL statement generated or executed.
- Treat a single execution as proof only of what happened for that specific input, environment, and runtime state.
- Do not assume an observed execution proves that all branches, inputs, or environments behave the same way.
- Remember that Runtime evidence can be interpreted incorrectly.
- Use Runtime observation to reduce uncertainty, not replace careful reasoning.

## 5. Keep observations focused

- Inspect only the threads, stack frames, variables, and objects relevant to the current question.
- Avoid expanding the entire Spring container, complete bean graphs, large collections, or unrelated object trees.
- Use `include_this=true` only when the current object is relevant.
- When inspecting `this`, first inspect shallow fields and then selectively expand only the relevant target objects.
- Increase `max_value_depth` only when deeper inspection is necessary.
- Avoid exposing or repeating unrelated sensitive values such as passwords, access tokens, API keys, private credentials, personal data, or security-related configuration.
- Do not collect more runtime state than is necessary to answer the current question.

## 6. Adapt dynamically to tool results

- Carefully inspect `error_code`, `retryable`, `warnings`, `nearby_locations`, and `suggested_next_step`.
- Treat `suggested_next_step` as a contextual recommendation, not a mandatory workflow.
- When an action fails, use the structured error information to decide how to recover.
- Do not repeatedly retry the same failed action with unchanged parameters unless the tool indicates that retrying is appropriate.
- Use identifiers returned by the Runtime: `breakpoint_id` for breakpoints, `request_id` for exception events, and `suspension_id` for active suspensions.
- Do not invent identifiers or confuse identifiers belonging to different resource types.
- Adjust the investigation plan as new evidence becomes available instead of continuing with an outdated initial hypothesis.

## 7. Use the Runtime purposefully

- Do not call Runtime actions merely because they are available.
- Prefer the smallest number of observations needed to answer the question with sufficient evidence.
- Begin with a clear uncertainty or hypothesis whenever possible.
- Use Runtime observations to confirm, reject, or refine that hypothesis.
- Stop collecting data once the available evidence is sufficient to support the conclusion.
- When the current evidence is insufficient, explicitly state what remains unknown and what additional observation would be needed.

When presenting the final analysis, include whenever relevant:

- Confirmed runtime facts
- Inferences supported by those facts
- Remaining unknowns or unverified hypotheses
- The key observed classes, methods, source locations, stack frames, and variables supporting the conclusion
- Important limitations of the observation
- Whether suspended threads were resumed and the debug state was cleaned up

Do not maximize the number of Runtime calls. Use the minimum effective set of runtime observations required to obtain reliable evidence and reduce uncertainty.
