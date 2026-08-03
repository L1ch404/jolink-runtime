# Fast Update Safety Boundaries

This document records correctness boundaries discovered through adversarial
review and dogfood. The normative public behavior remains in
`project-launch-contract-v0.1.md`; this file preserves why the guards exist so
later refactors do not accidentally remove them.

## 2026-08 P0 adversarial review

### Maven compiler user properties

Maven Compiler Plugin parameters can be supplied through plugin
`<configuration>` or user properties. Checking only configuration allowed the
formal build to use ECJ, a forked/custom compiler, preview features, or reduced
debug metadata while joLink still generated ordinary `javac -g` bytecode.

Fast update now validates both declaration paths for compiler identity, fork,
executable, debug/debuglevel, preview, and legacy compiler API settings. Any
value that cannot be reproduced exactly makes Fast Update unavailable with
`FAST_COMPILE_MODEL_UNVERIFIED`; normal project launch remains available.

### Maven extensions and external project inputs

Maven build/core extensions and `.mvn`/environment arguments can change the
effective build without appearing as ordinary compiler-plugin configuration.
The FastCompilePlan therefore fails closed for active extensions or compiler
and extension-path overrides. It fingerprints these paths even while absent:

```text
.mvn/maven.config
.mvn/jvm.config
.mvn/extensions.xml
```

It also fingerprints the active `MAVEN_ARGS` and `MAVEN_OPTS` values. Raw
environment values are never retained in public summaries, logs, or errors.
Creating or changing one of these inputs after launch makes the plan stale.

### Static initializer state

JDWP `RedefineClasses` accepts method-body bytecode but does not run class
initializers again. Treating a changed `<clinit>` as an ordinary method-body
update produced a false success: the new bytes were loaded while preexisting
static field values remained unchanged.

joLink now fingerprints `<clinit>` executable semantics, including referenced
constant-pool and bootstrap-method values. A changed, added, or removed static
initializer is rejected before JDWP transmission with:

```text
STATIC_INITIALIZER_CHANGE_REQUIRES_RESTART
runtime_code_state=unchanged
restart_required=true
```

Ordinary method bodies remain eligible. Constructor changes may be loaded, but
only subsequently created objects execute the new constructor; existing
objects are not reinitialized.

## Invariant

Every guard in this document may reduce Fast Update availability, but it must
not fail an otherwise valid managed project launch. When joLink cannot prove
that private compilation and HotSwap preserve the formal build/runtime
semantics, it must direct the caller to the formal build and restart path
instead of reporting an ambiguous or false `updated` result.
