---
name: jolink-java
description: "Use joLink MCP for Java development: fast selected-test feedback after edits, running or updating local applications, and investigating real JVM behavior in Maven or Gradle projects."
---

# Java development with joLink

Use joLink to obtain execution evidence for the user's Java task. Prefer its
persistent incremental compilation for repeated test and application feedback.
Keep the user's chosen scope: a request to explain code is not a request to run it.

## How it works

- Native Maven/Gradle probes export resolved source roots, classpaths, compiler and
  processor settings. Supported source/resource preparation still uses the build
  system; repeated business-source compilation uses headless Eclipse JDT's Java
  Builder (ECJ), not a fresh `mvn test` or `gradle test` lifecycle on every edit.
- JDT keeps workspace/build state on disk, tracks source deltas and dependent
  modules, and reuses unchanged output. Main/test have separate compilation
  settings within one Worker. Cold or invalidated workspaces can require a full build.
- Tests execute in an independent JVM using the project's JUnit/TestNG libraries.
  Application updates send compiled class bytes to the running JVM through JDWP
  `RedefineClasses`, or restart on those same outputs when HotSwap is unsuitable.
  Debugging observes actual JVM events, stacks and values. No running IDE is required.

## Verified workflow coverage

Real MCP/JVM regression coverage, as of September 2026, includes:

- Java 8, 11, 17 and 21, including different main/test language levels; Maven
  single-module and Reactor projects; Gradle 7.4.2, 8.10 and 8.14, including
  multi-project builds and Groovy/Kotlin build scripts.
- Selected JUnit 4/5 and TestNG tests; explicit/implicit annotation-processor
  paths and options, with tested Lombok and MapStruct combinations. Public-project
  checks include selected Spring Petclinic and MapStruct/Lombok workflows.
- Incremental dependency propagation, added/deleted sources, compile-error recovery,
  no-change reuse and cross-MCP reopening; HTTP-verified HotSwap, structural-change
  restart, timeout/background continuation, cancellation and JVM debug workflows.

These are tested examples, not a project/version allowlist. Try other ordinary
Java projects through the tools and use their actual diagnostics. Coverage does
not mean every plugin, processor or complete test suite was reproduced. ECJ/javac
source-compatibility differences, some older Lombok combinations, custom build
steps and untracked build inputs remain known gaps. Keep full build/CI validation
with the native build system; passing selected tests is not a substitute for it.

## Find the tools

Look for `java_fast_test`, `java_application`, `java_status`, and `java_debugger`.
The host may add a server prefix to these names. If tools are deferred, use the
host's tool search or definition-loading facility to obtain the actual schema.
Read the relevant schema instead of inventing arguments from the name alone.

If joLink is missing or the installed release has an older interface, explain
the connection/version gap. Do not repeatedly reinstall it during development
or silently switch installation sources. Installation is a separate user task.

## Choose the shortest useful workflow

- **Selected tests:** use `java_fast_test` with the project directory and test
  class or `Class#method` selectors. `action` defaults to `run`. An application
  launch or IDEA run configuration is not required just to execute these tests.
- **Run an application:** use `java_application(action="launch")`. For project
  launch, supply `project_path` and `main_class`, or use an existing IDEA
  Application/Spring Boot configuration (`launch_name` selects one). IDEA is
  optional. Use `java_home` to select the application JDK;
  `app_args` and `vm_args` override imported launch arguments.
  Specify `build_system` when both Maven and Gradle are present and the authority
  is ambiguous.
- **Apply edits to a managed application:** use `java_application(action="restart")`.
  It detects source changes, incrementally compiles, prefers HotSwap, and restarts
  the JVM when HotSwap cannot apply the output. No explicit source list is needed.
  Check `apply_method`; HotSwap is not process or framework reinitialization.
  Use `hotswap=false` to rerun initialization or reload startup/framework state.
- **Explain runtime behavior:** start with test failures, actual responses and
  logs. Use `java_debugger` breakpoints or exception watches when deeper evidence
  is useful. With a managed HTTP trigger, `wait_mode="blocking"` combines arming,
  triggering and waiting. Use separate `arm`/`await` for an intervening external action.

Example — tool `java_fast_test` (replace the example project and selector):

```json
{"action":"run","project_path":"/path/to/project","tests":["example.ServiceTest#works"]}
```

Example — tool `java_application` (an application already managed by joLink):

```json
{"action":"restart"}
```

## Read results, then continue

- Launch, restart and Fast Test wait up to `timeout`, capped at 30 seconds.
  Cold preparation can take minutes. A returned background attempt is not failure
  or completion. Keep its ID, choose an appropriate waiting interval, then call
  `java_status(action="status")`; do not rapidly poll or submit duplicate work.
  Follow `fast_test`, or `active_operation` / `last_reload`, for the original task.
  `previous_startup_ms` on launch/restart is a locally persisted JVM startup observation,
  excluding compilation, not a readiness guarantee; it can be null.
- `java_status(action="status")` is a compact overview. Follow its `next_action`
  or use `java_status(action="status", details=true)` for current launch/restart error details.
  Read build logs with `java_status(action="logs", source="build")`; logs default
  to application output when source is omitted. Do not fetch details on every poll.
- For HTTP applications, provide the actual `ready_port`. `starting` means wait;
  `unverified` is not a claim of readiness. TCP readiness is not endpoint correctness.
- Fast Test `run` includes compiler diagnostics when compilation has failed before
  the reply; use those errors directly. `java_status` stays compact. For errors
  after a timeout reply or failed-test details, follow `next_action` or call
  `java_fast_test(action="result", test_run_id=...)`; this reads the retained
  result without rerunning tests. Read details when needed, not on every poll.
  Results are retained only for the active/latest completed run in this MCP session.
  `passed=false` is a test outcome;
  compilation/infrastructure errors are different. Report the tests actually run,
  not a claim that the entire project's build or test suite passed.
- After HotSwap, verify with a fresh request or relevant test. It does not rerun
  constructors/static initialization or reset existing framework state. Unknown
  HotSwap outcome is not definite failure; follow the returned guidance.
- Use the returned `suspension_id` for inspection. Always resume or clean up a
  suspension you create. Redefined-class breakpoints can become stale; reset them
  against the current source when reported.
- Cancel a Test with `java_fast_test(action="cancel", test_run_id=...)`. Stop only
  the managed application when the user's task calls for it; do not stop unrelated JVMs.

Keep the project's intended JDK and build settings.
Use normal Maven/Gradle for packaging, full lifecycle/CI validation, or work the
current joLink result shows it cannot perform. Report that concrete gap; do not
silently weaken the project to turn a rejected workflow into a claimed success.
