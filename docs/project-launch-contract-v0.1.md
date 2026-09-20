# joLink project launch

This page describes the current product. The earlier P0 design, direct-javac
proposal and Candidate/rollback design are [historical records](archive/project-launch-contract-v0.1.md),
not current startup requirements.

## Public entry points

- `java_application`: `launch`, `attach`, `restart`, `stop`, `detach`.
- `java_status`: `status`, `logs`, `processes`.
- `java_fast_test`: selected tests, independent of an application launch.
- `java_debugger`: breakpoints, exceptions, waits and suspended-state inspection.

See the [MCP contract](mcp-contract-v0.1.md) for argument/result semantics.

## Project launch path

```text
IDEA launch configuration
→ cached Maven/Gradle Probe model (refresh when tracked build configuration changes)
→ persistent JDT projects in dependency order
→ FULL for a cold workspace / INCREMENTAL for changed sources / reuse for no changes
→ launch the application's JVM from the JDT outputs
→ optional TCP readiness
```

The Probe exports the build system's source roots, ordered classpaths, compiler
and Processor configuration. Necessary source preparation may execute build
tasks; joLink does not run a blanket Maven/Gradle application compile on every
launch. Source-only edits do not require rediscovering an unchanged build model.

Each needed module has persistent JDT state; main/test use separate compiler
settings. Upstream source changes are compiled in dependency order rather than
automatically forcing a new formal Maven build. Runtime uses the persistent
JDT output directly, without copying the whole class tree for every launch.
Resources remain part of the runtime classpath.

The import reads `.run/*.xml`, `.idea/runConfigurations/*.xml`, and
`.idea/workspace.xml` for supported Application/Spring Boot configurations.
Equivalent duplicated configurations collapse; genuine ambiguity requires an
exact `launch_name` or fixing conflicting IDE configurations. joLink does not
execute arbitrary IDEA before-launch tasks or create a project-local `.jolink`.

Direct `jar_path` and `main_class`/`classpath` launch remain available. They do
not create a source compilation session and therefore cannot use source-based
incremental restart.

## Waiting and readiness

Launch waits for completion using `timeout`, capped at 30 seconds. If work is
still active it returns the attempt state; `java_status(action="status")`
observes the same attempt. A short call timeout does not cancel compilation or
stop the JVM. The server owns the attempt; callers do not need to construct an
attempt ID.

`ready_port` adds TCP readiness. Without it application readiness is
`unverified`, not a claim that Spring or external dependencies are healthy.
See [startup details](jdt-first-launch.zh-CN.md).

## Restart and lifecycle

`restart` compiles detected changes first. With `hotswap=true` (default), a live
project session tries HotSwap and reports `apply_method="hotswap"`; incompatible
changes take the ordinary restart path. `hotswap=false` requests a new JVM from
the newly compiled output. Compilation failure is not reported as a successful
restart. The public operation replaces the former separate `reload` action;
internal `reload_id` / `last_reload` fields still identify background work.
See [restart details](project-restart.zh-CN.md).

Stop cancels owned startup/build work and stops the managed application. Detach
from an externally owned JVM does not kill that process. Compiler workspace
state is persisted for subsequent launches; it does not mean the Worker or
application survives MCP process exit. One stdio server manages one application
slot; Worker cross-server residency is not implemented.

## Boundaries and evidence

Current coverage includes Maven/Gradle modules, independent main/test compiler
configuration, APT and tested Java 8/11/17/21 combinations. This does not promise
all build plugins, all Gradle versions, arbitrary SourceSets, JPMS or every
javac-specific compiler extension. The [compatibility follow-up](java-compatibility-followup-2026-09.md)
is the current unresolved-issue list.

Release validation must use real MCP/JVM flows from installed wheel/sdist,
including compile, launch, changed-source restart and Fast Test; source-checkout
unit tests alone are not distribution acceptance.
