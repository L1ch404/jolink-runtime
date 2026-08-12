# joLink Headless JDT Incremental POC Contract

Contract-Version: `0.1`

Design-Status: `approved for Phase 1A experiment`

Implementation-Status: `A1-A9 canonical clean-worktree evidence passed; A10 pending`

Product-Status: `experiment only / no MCP or Runtime behavior`

Chinese edition:
[`jdt-incremental-poc-contract.zh-CN.md`](jdt-incremental-poc-contract.zh-CN.md)

This contract defines the evidence required before joLink may treat a
headless Eclipse JDT Java Builder as a viable incremental compiler worker. It
does not select JDT as a production architecture.

This is an internal experiment contract, not an MCP or Project Launch public
contract. The current public `update` implementation, Schema, annotation-
processor rejection, and standard HotSwap safety boundaries remain unchanged.

The related direct-javac research remains preserved but frozen. Its Maven
model, fingerprints, private staging, process supervision, and fail-closed
boundaries may be reused; complete Maven-to-direct-javac equivalence is no
longer the active compile strategy. See
[`java-compile-strategy-roadmap.md`](java-compile-strategy-roadmap.md).

## Decision to be tested

The experiment tests this hypothesis:

```text
Maven, later
    constructs a versioned Build World

Headless JDT Java Builder
    performs one private full build
    retains dependency and last-build state
    applies ordinary Java deltas incrementally

joLink, later
    invalidates stale Build Worlds
    supervises the compiler worker
    chooses HotSwap or restart
    verifies the running application
```

The POC is not successful merely because ECJ can compile Java. It must show
that joLink can isolate, control, measure, stop, and maintain the real JDT
incremental project builder at an acceptable engineering and resource cost.

## Frozen terminology

- `Worker JDK`: the Java runtime that starts Equinox and the compiler worker.
- `Source compliance`: the Java language level accepted by JDT.
- `Class target`: the bytecode level emitted by JDT.
- `TargetSystemLibrarySnapshot`: one ordered, state- and content-fingerprinted
  view of the compiler platform libraries exposed by an exact JDK 8
  installation. It includes bootstrap and extension mechanisms and defines
  the API world seen by the compiler independently of the Worker JDK.
- `Target JVM`: the JVM that would eventually execute the emitted application
  classes. It is not started in Phase 1.
- `Java Builder`: the builder registered as
  `org.eclipse.jdt.core.javabuilder`, running inside an Eclipse Workspace/Core
  Resources environment.
- `Real incremental build`: an `IProject.build(INCREMENTAL_BUILD, ...)` or
  equivalent Workspace build that receives a resource delta and uses the Java
  Builder's previous build state. Choosing source files in Python and invoking
  ECJ batch compilation is not a real incremental build for this contract.
- `Workspace lineage`: one private workspace plus its compiler/project
  identity, persistent JDT build state, ownership history, and saved-state
  manifest. Its `workspace_lineage_id` remains stable across source edits,
  builds, graceful Worker restart, and validated offline source deltas. A
  cancelled/aborted build may move the lineage to `RECOVERY_REQUIRED`; it does
  not rewrite the immutable result of that build.
- `Build generation`: one immutable Workspace build operation identified by
  `build_generation_id`, with an exact source-tree fingerprint, request and
  operation identity, operation result, nullable compiler result, diagnostics,
  and observed output state. It covers `CLEAN`, `FULL`, and `INCREMENTAL`.
  Its terminal status is one of `SUCCEEDED`, `FAILED_COMPILE`, `CANCELLED`, or
  `ABORTED` and never changes afterward. Results from different build
  generations may not be mixed.
- `Publication transaction`: a future product-integration boundary that keeps
  the Runtime on a committed last-good build generation while a candidate build
  generation is built and verified. The existing `generation_publishable`
  field belongs to the build generation and is only the current logical
  publication gate; it does not make JDT's mutable `bin` directory a physical
  last-good store.
- `Clean-full oracle`: a new private workspace lineage and full-build
  generation built from the same frozen inputs with the same pinned JDT stack.
  It is the Phase 1 correctness oracle for an incremental result.
- `Evidence candidate`: one exact experimental stack comprising the Worker
  JDK, Equinox and bundle lock, JDT identity, `TargetSystemLibrarySnapshot`,
  compiler/project options, and instrumentation artifact/configuration.
  Changing any of these creates a different candidate.

## Scope

Phase 1 is deliberately split into two independent gates.

```text
Phase 1A
    plain Java fixture
    real headless Java Builder
    full and incremental correctness
    lifecycle and resource measurements

Phase 1B
    the same worker model
    Java 8 source/target
    exact Lombok 1.18.20
    Lombok-generated member and dependency correctness
```

Phase 1A is authorized by this contract. Phase 1B may begin only after a
recorded Phase 1A Go decision. Passing both permits a new review for Phase 2;
it does not authorize Phase 2 implementation.

Phase 2 would introduce Maven Bootstrap and a real-project Build World.
Phase 3 would introduce Runtime launch, class-shape comparison, JDWP HotSwap,
Fast Restart, readiness, and HTTP verification. Those phases are recorded to
keep the direction coherent, but they remain out of scope here.

## Explicit non-goals

The first POC must not:

- add or change an MCP Tool, action, Schema, result, or description;
- change production `run`, `update`, restart, JDWP, or class-reading behavior;
- run Maven, import a Maven or IDEA project, or inspect a company repository;
- write into a user's source tree, `target`, IDE workspace, or Maven local
  repository;
- use JDT LS as the worker implementation;
- include LSP4J, M2E, Buildship, JDT UI, Eclipse UI, completion, navigation,
  refactoring, language-server, or debug bundles merely for convenience;
- implement resources, Maven filtering, generated sources, MapStruct,
  QueryDSL, Dagger, Spring metadata generation, or arbitrary JSR-269;
- implement HotSwap, enhanced HotSwap, Fast Restart, or HTTP verification;
- add ECJ BatchCompiler plus a joLink-owned dependency graph as a second
  implementation in parallel;
- claim compatibility with arbitrary Eclipse, JDT, Lombok, JDK, or operating
  system versions.

JDT LS may be measured separately as a comparison baseline. Passing through
JDT LS is not a fallback implementation and cannot satisfy Phase 1.

## Dependency and version boundary

The worker must use the smallest proven Equinox/JDT bundle set capable of
running the real Java Builder. Expected capabilities include:

```text
Equinox launcher and OSGi runtime
Eclipse core runtime/jobs/filesystem/resources
JDT Core and the Java project nature/builder
only the transitive bundles required by those capabilities
```

The actual set is discovered by resolving bundle requirements, not by copying
an Eclipse IDE or deleting apparently unused JARs until it starts. Bootstrap
discovery may add or remove bundles while finding a viable closure, but none
of its results count as Phase 1 evidence.

Before the first evidence-bearing Phase 1A run, the discovered candidate must
commit an artifact and configuration lock containing, for every bundle and
launcher artifact:

```text
symbolic name
exact version
origin repository/release
SHA-256
license identity
compressed bytes, when distributed as an archive
installed bytes
bundle start level and activation policy
Equinox application/configuration identity
every selected bundle's `osgi.ee` requirement and matching Worker JDK
execution-environment capability
```

Floating `latest`, version ranges at runtime, snapshots, and silent artifact
replacement are forbidden. The worker performs no dependency download while
building a fixture. The resolver must fail closed when a selected bundle's
mandatory `osgi.ee` filter cannot be parsed or cannot be satisfied by the
locked Worker JDK. Merely observing that Equinox happened to start is not
execution-environment evidence.

At most two locked evidence candidates may be evaluated before the Phase 1
decision. Bootstrap attempts that never become evidence candidates do not
consume this limit:

1. A maintainable current candidate, pinned before its first evidence-bearing
   run.
2. If required, an Eclipse 2021-03 compatibility anchor because that release
   line is explicitly mentioned by the Lombok 1.18.20 changelog. This is not
   assumed compatibility. Its JDT Core identity is
   `3.25.0.v20210223-0522`; all remaining platform artifacts and the Worker JDK
   must still be pinned by the lock.

The compatibility anchor is evidence, not an automatic product choice. If
Lombok works only on an obsolete stack, the result is `conditional`, pending a
maintenance and security decision.

### Evidence candidate lineage

Every evidence-bearing result belongs to exactly one locked evidence
candidate. A candidate used for Phase 1B must independently satisfy the
complete Phase 1A Go gate on that exact stack before it may produce Phase 1B
evidence.

Evidence may not be accumulated across candidates. In particular, Phase 1A
success from one Worker JDK/Equinox/JDT/system-library/configuration stack
cannot be combined with Phase 1B success from another stack to claim a Phase 1
Go. Re-locking any candidate identity creates a new lineage and invalidates
prior gate inheritance, even when the fixture sources are unchanged.

### Required version matrix

Every result must report these as separate identities:

| Dimension | Phase 1A | Phase 1B |
| --- | --- | --- |
| Worker JDK vendor/version | Pinned candidate value | Same unless the compatibility test requires another pinned value |
| Equinox/Platform release | Exact locked value | Exact locked value |
| JDT Core | Exact locked value | Exact locked value |
| Source compliance | `1.8` | `1.8` |
| Class target | `1.8` | `1.8` |
| Target system library | Exact JDK 8 library fingerprint | Exact JDK 8 library fingerprint |
| Target JVM | Not started; compatibility target is Java 8 | Not started; compatibility target is Java 8 |
| Lombok | Absent | Exactly `1.18.20` |

The Worker JDK is not evidence of source or bytecode compatibility. Conversely,
`target=1.8` does not prove that Lombok 1.18.20 can run inside the selected
Worker JDK/JDT process.

### TargetSystemLibrarySnapshot

Phase 1 uses the minimal JDT Core classpath model rather than
`org.eclipse.jdt.launching`. The launching bundle, its `JRE_CONTAINER`, VM
install model, and transitive closure are excluded from the preferred worker.

For the exact target JDK 8 installation, a bounded helper derives javac's
effective platform-class-path view from that installation itself and captures:

```text
java vendor/version and JDK-home identity
sun.boot.class.path as ordered advertised bootstrap entries
java.ext.dirs as ordered advertised extension directories
java.endorsed.dirs as recorded provenance
the ordered effective PLATFORM_CLASS_PATH reported by target javac
entry state: PRESENT or ABSENT
entry type: archive, class directory, or absent placeholder
path-identity fingerprint for every advertised entry
SHA-256 for every present archive
deterministic content fingerprint for every present class directory
optional runtime Extension ClassLoader URLs as cross-validation only
the exact effective ordering materialized into the JDT project
```

joLink must not approximate this snapshot by sorting every JAR found under a
JRE directory or by applying directory-layout heuristics. It must never use
the Worker JDK as the discovery source. Runtime extension-loader URLs may
cross-check the compiler view but are not its normative source.

An advertised entry that the exact target JDK consistently reports as absent
is a tolerated placeholder and remains part of the snapshot with
`state=ABSENT`; it is not materialized into JDT. Javac may preserve the same
absent placeholder in `PLATFORM_CLASS_PATH`; it remains evidence but is still
not materialized. An absent compiler entry that does not correspond to an
advertised absent bootstrap placeholder is unresolved and invalidates the
workspace lineage. A transition in either direction between `PRESENT` and `ABSENT`,
a content-fingerprint change, or an unreadable present compiler entry also
invalidates it. Every present javac platform entry is materialized in javac
order as `JavaCore.newLibraryEntry(...)`. No entry may fall back to Worker JDK
libraries, and public reports expose fingerprints and target-JDK identity
rather than sensitive absolute paths.

Phase 1A records endorsed-directory provenance but does not model a target JDK
with effective endorsed archives. Discovering one makes the candidate
conditional and requires a contract review rather than silent approximation.

Introducing `org.eclipse.jdt.launching` later would change the dependency and
resource experiment and therefore requires a contract amendment; it may not
be added silently to make Phase 1 pass.

## Isolation and lifecycle contract

The POC worker is a separate supervised JVM. It is not loaded into the Python
MCP process or the target application JVM.

For every run:

- create a new private attempt root outside the fixture checkout;
- create a private Eclipse configuration area and workspace data area;
- bind the project to the exact `TargetSystemLibrarySnapshot` rather than
  compiling against the Worker JDK APIs;
- copy fixture inputs into that workspace lineage or otherwise prove that no build
  operation can mutate the fixture checkout;
- direct every class, generated file, marker, log, cache, and workspace state
  into the private attempt root;
- disable automatic builds and invoke builds explicitly;
- allow only one command to mutate one workspace at a time;
- refresh external source changes into the Eclipse resource model before the
  incremental build;
- use bounded startup, build, cancellation, and shutdown deadlines;
- on cancellation or timeout, request cooperative cancellation first and then
  terminate the exact owned process tree;
- make every cancelled or timed-out build generation non-publishable and move
  its workspace lineage to `RECOVERY_REQUIRED`; an infrastructure abort or
  crash does the same;
- leave no worker, Equinox, compiler, or fixture application process behind;
- never attach to JDWP or start the fixture as an application in Phase 1;
- preserve a failed attempt only when the runner explicitly requests local
  diagnostic retention.

If stdio is used for worker control, stdout contains protocol frames only and
all diagnostics go to stderr or a private log file. Public results must not
include raw environment values, user-home paths, repository credentials, or
unbounded compiler output.

Worker shutdown and restart are part of the experiment. A full Workspace save
must be requested before a graceful shutdown. Phase 1A then tests whether a
new Worker can reopen that exact workspace lineage and perform a valid
incremental build.

Each saved workspace lineage has a manifest and a Runner-owned clean-shutdown
marker. The manifest fingerprints the Worker, bundle set, compiler/project
model, system library, saved source snapshot, and classpath. The Worker may
acknowledge `SAVE`, but it never writes or renews the clean marker. Before
starting a Worker, the Runner atomically consumes any previous marker and
thereby marks the workspace owned/dirty. Only after `SAVE_ACK`, a zero-exit
Worker, and confirmed settlement of the complete identity-bound process tree
may the Runner publish a new marker. Publication uses a temporary file,
flush/file sync where supported, same-filesystem atomic replacement, and parent
directory sync where meaningful; unsupported durability primitives and any
failure are reported explicitly rather than overstated.

A missing marker, failed save, abnormal exit, unsettled process tree, or change
to an invariant compiler/project fingerprint invalidates remembered build
state. A source change made while the Worker is stopped is not itself an
invalidation: it is an untrusted offline delta that must be bounded to the
owned source root, refreshed into the reopened resource model, and proved by
incremental/build-kind and clean-full-oracle evidence. One workspace lineage
may be owned by only one Worker. An idle graceful shutdown has an initial
five-second settlement budget before the Runner terminates only the
identity-verified owned process tree.

Cross-process build-state recovery is measured but is not an unconditional
Phase 1A failure:

```text
preferred
    restart restores state and the next eligible edit is incremental

acceptable for continued evaluation
    state is reliable while the worker lives;
    reopening requires one private full build

no-go
    the same healthy worker repeatedly loses build state or silently performs
    full builds for ordinary deltas
```

## Phase 1A fixture and cases

The plain Java fixture must be intentionally small and must not use Maven,
Gradle, Lombok, annotation processing, resources, modules, or external
dependencies. It contains at least:

```text
Api.java
    public API used by Service
    one compile-time constant used downstream

Service.java
    implements or calls Api

Application.java
    calls Service
```

The runner must execute the following cases from clean private inputs.

### A1 — Full build

```text
create Java project with JavaCore.NATURE_ID
→ inspect its build spec and ensure exactly one JavaCore.BUILDER_ID
→ reject a missing or duplicate Java Builder
→ configure source/output entries and the TargetSystemLibrarySnapshot
→ invoke the real Java Builder with FULL_BUILD
→ require no ERROR markers or build-path errors
→ require the complete expected class family
→ require Java 8 class-file major version
```

A companion negative source referencing a post-Java-8 platform API such as
`List.of` must fail. Class-file major version 52 alone is not Java 8 API
compatibility evidence.

### A2 — No-op incremental build

```text
change no input
→ invoke INCREMENTAL_BUILD
→ require an empty resource delta or `build_outcome=NO_COMPILE` with no
  compilation callback and no compiled units
→ keep `actual_build_kind` unavailable when no Java compilation callback
  directly reveals the effective build kind
→ require identical output class set and SHA-256
```

Wall-clock speed alone is not evidence of a no-op build.
Some JDT versions emit neither `buildStarting()` nor `buildFinished()` for a
true no-op. The runner must record that fact rather than inventing a callback.
In that case it must also prove that `project.build()` returned normally, that
the same enabled participant observed an adjacent compilation in the same
Worker, and that no source unit, diagnostic, or output hash changed.
The absence of participant callbacks is not, by itself, direct evidence that
the Java Builder was not invoked; the report must not claim that fact until
builder or resource-delta instrumentation observes it.

### A3 — Leaf method-body edit

Edit only a method body in `Application.java` without changing its schema.
Require the incremental output to equal a clean-full oracle from the same
edited inputs.

### A4 — Upstream method-body edit

Edit an implementation body in `Api.java` without changing its public schema.
Require correct incremental output and no unnecessary clean/full build. The
contract does not assume that unchanged consumers must be recompiled.

### A5 — Dependency-propagating edit

Perform both:

- a public API change that affects `Service.java` and `Application.java`;
- a compile-time constant change whose inlined consumers must be refreshed.

Require affected downstream units or diagnostics to match a clean-full oracle.

The first macOS evidence run used a dedicated dependency fixture where both
`Service.java` and `Application.java` consume `Api` directly. Changing only
the upstream method signature, and separately changing only its compile-time
constant, caused the real Java Builder to incrementally compile all three
affected units. Both resulting class families and diagnostics matched separate
clean-full oracles exactly. This proves the fixture's A5 propagation behavior;
it is not yet evidence for arbitrary enterprise dependency graphs.

### A6 — Delete and rename

Delete one source, then separately rename one source/type. Require obsolete
top-level, inner, anonymous, and synthetic class-family outputs to be removed.
The output and diagnostic state must equal a clean-full oracle.

The first macOS A6 evidence fixture includes a top-level class, member classes,
an anonymous class, a local class, and a generic override. The runner discovers
the candidate's class family from the full-build output and directly verifies
that the override class contains an `ACC_BRIDGE | ACC_SYNTHETIC` method.
Deleting the source removed the complete discovered class family while leaving
an unrelated class unchanged. A separate
source/type rename removed that same old family and produced the complete new
family. Both incremental output trees and diagnostics matched independent
clean-full oracles exactly.

### A7 — Broken edit and recovery

Introduce a deterministic compilation error. The worker must:

- return bounded structured diagnostics derived from problem markers;
- never report a publishable build generation;
- not present stale changed classes as successful output.

Fix the source and require the next build to recover without recreating the
worker unless the Java Builder itself requests a full build.

The first macOS A7 evidence run introduces an unresolved symbol into a class
that previously compiled. The same Worker returns a bounded structured ERROR
diagnostic with resource, line, character range, severity, and message; marks
the build generation non-publishable; and exposes no publishable changed classes.
After replacing the broken body with a valid edit, that same Worker performs an
incremental recovery, clears all diagnostics, and produces a publishable class
tree exactly equal to an independent clean-full oracle. Any class emitted or
retained during the failed build is recorded as evidence only and is never
presented as publishable output.

#### Future Runtime publication boundary (recorded, not implemented in Phase 1)

JDT builds directly into the private workspace lineage's mutable output tree. A failed
build may retain, delete, or replace class files there. Therefore
`generation_publishable=false` is a logical safety gate, not physical
last-good-output isolation. Product Runtime integration must never scan the
current `bin` tree and HotSwap whatever it finds. It must treat compilation as
a publication transaction:

```text
committed last-good build generation N remains active
    candidate build generation N+1 fails  -> ABORT; publish no classes from N+1
    candidate build generation N+1 passes -> COMMIT an explicitly verified
                                              output set before HotSwap/restart
```

The physical representation (separate output roots, immutable snapshots,
copy-on-commit, or another reviewed design) is intentionally deferred until
productization. Phase 1 records compiler reality and enforces the logical gate;
it does not claim transactional output storage.

### A8 — Workspace restart

After a successful build, gracefully save and stop the worker, restart it on
the same private workspace lineage, apply one ordinary method-body edit, and record
whether the result is incremental or a required full rebuild. Either result
must be explicit and correct; silent fallback is forbidden.

The first macOS A8 evidence run performs a full build, receives an explicit
Workspace save acknowledgement, cooperatively stops the Worker, and starts a
new Worker process on the same configuration area and workspace lineage.
The reopened Worker then receives an ordinary method-body edit and a requested
incremental build. The real Java Builder reports `actual_build_kind` as
`INCREMENTAL`, compiles only `Application.java`, changes only
`Application.class`, and produces an output tree and diagnostics exactly equal
to an independent clean-full oracle. All three A8 Worker instances settle
cooperatively. This proves state recovery only for the frozen candidate and
fixture; it does not yet prove crash recovery, fingerprint invalidation, or
long-run stability.

### A9 — Repeated-build stability

Run at least 100 deterministic edit/build cycles across method-body, constant,
error/recovery, delete/restore, and no-op changes. Every successful incremental
build generation must equal its clean-full oracle. There must be no worker crash,
stuck build, stale class family, or unbounded memory trend.

#### A9 design status and decomposition

This design is frozen and approved for implementation. Amend it only when
implementation exposes a concrete contradiction, and record that amendment
before changing behavior. A9 is split into independent evidence lanes so that oracle workers and
destructive lifecycle tests do not contaminate the long-lived Worker's memory
curve:

```text
A9-S  same-Worker deterministic stability workload
A9-M  heap / Metaspace / `process_tree_rss_sum_bytes` measurement for A9-S
A9-L  cooperative cancellation, recovery, shutdown, and process ownership
```

All lanes use the same locked candidate and target-system snapshot, but use
separate private workspace lineages. A result from one lane may not hide or
repair a failure in another.

#### A9-S — deterministic long-lived workload

Use one dedicated mixed plain-Java fixture containing the existing dependency,
recovery, and class-family shapes. One Worker first performs a full baseline,
then one warm-up epoch that is excluded from trend calculations, followed by
ten measured epochs. Each epoch contains this exact eleven-operation sequence
and returns the source tree to its frozen baseline:

```text
1   leaf method-body edit
2   no-op incremental request
3   leaf method-body restore
4   upstream method-body edit
5   upstream method-body restore
6   compile-time constant edit
7   compile-time constant restore
8   deterministic unresolved-symbol error
9   error recovery to the baseline source
10  delete one source and its complete class family
11  restore that source and class family
```

This produces 110 measured build requests after warm-up. The baseline full
build, all 11/11 warm-up requests, and all 110/110 measured requests must pass
the same applicable correctness, diagnostic, output-family, and oracle gates.
Warm-up is excluded only from resource-trend calculations; it is never exempt
from correctness. Mutations are deterministic, bounded, and applied only to
the private source copy. Every epoch must begin and end at the same source-tree
fingerprint. The same Worker and workspace must serve all 121 warm-up-plus-
measured requests; recreating it resets the A9-S evidence.

For every eligible source compilation request, `actual_build_kind` must be
`INCREMENTAL`; the no-op must explicitly report `NO_COMPILE`. A source deletion
is a resource-only state change: it must report `actual_build_kind=null`,
`build_outcome=NO_COMPILE`, no compiled units, and the complete removed class
family in `deleted_classes`, with the resulting output equal to its clean-full
oracle. Restoring that source must again report `INCREMENTAL`. The broken edit
must produce the expected structured ERROR. That source error is a completed
build, not an infrastructure abort: it emits `BUILD_COMPLETED` with
`operation_kind=INCREMENTAL`, `operation_ok=true`, and `compile_ok=false`;
leaves the build generation terminal as `FAILED_COMPILE` and non-publishable;
and leaves the workspace lineage `READY` for incremental source recovery. The
recovery build must remain incremental and receives a new
`build_generation_id`. Any silent full fallback, stuck request, unexpected
diagnostic, stale/deleted class-family mismatch, or Worker restart fails A9-S.

#### Oracle policy

Every successful source state is compared with a clean-full oracle. To avoid
launching an unnecessary oracle Worker for every repeated state, the runner may
cache an oracle only by this complete key:

```text
candidate identity
TargetSystemLibrarySnapshot fingerprint
project_model_fingerprint
exact source-tree fingerprint
```

`project_model_fingerprint` includes the ordered source roots, output roots,
compile classpath content/identity, Java nature, builder identity and order,
resource encoding, and effective compiler/project options. The first
occurrence of a key must create a separate private workspace lineage, run a
real clean full build, record its complete class SHA tree and diagnostics, and
cooperatively stop that oracle Worker. Later cycles may reuse only that exact
immutable oracle result. The oracle catalog is attempt-scoped and must be
precomputed before the measured A9-S Worker starts; no oracle process may
overlap the measured workload, and no report or output from a previous attempt
may be trusted as its oracle. Cache hits and misses are reported. Oracle Worker
memory is excluded from A9-M. A no-op must match both its pre-request output and
the oracle for its source fingerprint. The intentionally broken state is
compared with a clean-full diagnostic oracle, but neither output tree is ever
publishable.

#### A9-M — resource measurement

The long-lived A9-S Worker is the measured subject. The runner samples the
identity-bound process tree at intervals no greater than 100 ms and records
root and child RSS, child count, sampling gaps, and each build's observed
sampled RSS peak plus sample count. The machine field
`process_tree_rss_sum_bytes` is the arithmetic sum of root and observed child
RSS. It may double-count shared pages and is not PSS, USS, or unique physical
memory; the Phase 1 decision bands deliberately use this reproducible
engineering metric. A Worker-side bounded
metrics command records heap used, committed, and maximum; Metaspace used and
committed; Compressed Class Space used and committed when that pool exists;
loaded-class count; thread count; and uptime. `class_metadata_used_bytes` is
reported as Metaspace plus Compressed Class Space when the latter exists, while
the two pools remain separately visible. A missing pool is explicitly
`not_applicable` or `unavailable`, never zero-filled. Before each full or
incremental build the Worker resets the relevant `MemoryPoolMXBean` peak
counters and afterward records per-pool peak usage. Any aggregate of per-pool
peaks is labeled as an upper bound because pool peaks may occur at different
instants. This avoids adding an in-process polling thread to the measured
subject.

After the warm-up epoch and after each measured epoch, while no build is
active, the runner first takes a pre-GC checkpoint, requests explicit GC, and
uses a bounded one-second settlement before taking an after-request checkpoint.
The Worker records every available `GarbageCollectorMXBean` collection count
and collection time before and after the request. The report always sets
`gc_request_sent=true`; it sets `gc_collection_observed=true` and names the
sample `post_gc_checkpoint` only if at least one supported collection count
increased. Otherwise it sets `gc_collection_observed=false` and names the
sample `after_gc_request_checkpoint`. Unsupported counters remain explicitly
unavailable. After the final checkpoint it also records
`process_tree_rss_sum_bytes` after 30 seconds of true idle. `System.gc()` is a
request, not proof that every
collector ran or reclaimed all eligible objects. Resource sampling is complete
only when at least 95% of the expected intervals were observed and no
unexplained sampling gap exceeds 500 ms; otherwise A9-M requires a diagnostic
rerun.

The report preserves every raw checkpoint and computes early and tail medians
from the comparable after-request checkpoints without pretending GC was
observed when it was not. In addition to the existing absolute RSS/peak
decision bands, the first run requires a diagnostic rerun when the last three
checkpoint median exceeds the first three by any of:

```text
process_tree_rss_sum_bytes > max(64 MiB, 20%)
heap used         > max(32 MiB, 20%)
class metadata    > max(16 MiB, 20%)
```

or when the final five checkpoints are strictly increasing and exceed the same
absolute threshold. Thread count also requires a diagnostic rerun when its tail
median exceeds its early median by more than four, or its final five values are
strictly increasing with a total increase greater than four. Loaded-class count
does so when the corresponding increase exceeds `max(128, 10%)`. These are
rerun triggers, not declarations of a leak. One noisy run must be repeated with
the exact workload; only persistent replicated growth plus heap/native-memory
evidence blocks approval as a growth failure.

A9-M has exactly four decision states:

```text
PASS
    complete measurements, stable trend, and an absolute resource value in the
    Preferred or Acceptable band
CONDITIONAL
    complete and stable, but an absolute resource value is in the documented
    conditional band
DIAGNOSTIC_RERUN_REQUIRED
    a growth trigger fired, sampling was noisy/incomplete, or a required RSS,
    heap, class-metadata, peak, GC-checkpoint, thread, or class-count value is
    unavailable
NO_GO
    an absolute No-Go band was crossed, or the exact diagnostic rerun confirms
    persistent growth with supporting evidence
```

Missing measurements are never coerced to zero, and a single noisy run is
never labeled a leak. Latency distributions are reported by operation type but
are not Phase 1 performance promotion thresholds.

#### A9-L — cancellation, recovery, and ownership

Cancellation uses a separate workspace lineage and requires a real
asynchronous Worker protocol: one build may run in the background while the
control loop accepts only `STATUS`, `CANCEL`, and bounded shutdown for that
build generation. The protocol must carry command/request IDs and
`build_generation_id`; it may not infer ownership from the latest response. A
deterministic test-only barrier may pause the observed Java Builder after it
starts, but it may change timing only and is excluded from correctness and
latency evidence.

The asynchronous protocol, metrics support, and dormant barrier are part of
the locked Worker artifact. Adding them changes candidate identity, so A1-A8
must be rerun on that exact artifact before A9 can be approved. The barrier is
activated only by an A9-L lifecycle command, must not change source, classpath,
compiler options, markers, or class bytes, and no output from its cancelled
operation is accepted as correctness evidence. A1 instrumentation parity must
remain exact with all lifecycle barriers inactive.

The protocol state machine is frozen before implementation. Workspace lineage
state and build-generation outcome are separate state machines. Every
`BUILD_COMPLETED` carries `operation_kind`, `operation_ok`, and nullable
`compile_ok`:

```text
workspace lineage
  READY
    BUILD_ASYNC(request_id, build_generation_id, kind)
      -> BUILDING(build_generation_id)
          STATUS(build_generation_id) -> snapshot only
          CANCEL(build_generation_id) -> CANCEL_REQUESTED when accepted
          STOP                        -> CLOSING and cancellation request
      -> exactly one terminal event for the build generation:
          BUILD_COMPLETED(
              operation_kind=CLEAN|FULL|INCREMENTAL,
              operation_ok=true,
              compile_ok=null|true|false
          )
          BUILD_CANCELLED
          BUILD_ABORTED

build generation outcome / resulting workspace state
  BUILD_COMPLETED, CLEAN, operation_ok=true, compile_ok=null
      -> SUCCEEDED / state chosen by the enclosing recovery transaction
  BUILD_COMPLETED, FULL|INCREMENTAL, operation_ok=true, compile_ok=true
      -> SUCCEEDED / READY outside recovery
      -> SUCCEEDED / remain RECOVERING inside recovery until oracle equality
  BUILD_COMPLETED, FULL|INCREMENTAL, operation_ok=true, compile_ok=false
      -> FAILED_COMPILE / READY outside recovery
      -> FAILED_COMPILE / LINEAGE_DISCARDED inside recovery
  BUILD_CANCELLED
      -> CANCELLED / RECOVERY_REQUIRED
  BUILD_ABORTED or abnormal Worker exit
      -> ABORTED / RECOVERY_REQUIRED

RECOVERY_REQUIRED
  RECOVER(recovery_id)
      -> RECOVERING
          CLEAN_BUILD -> FULL_BUILD -> clean-full oracle verification
      -> READY only after the complete transaction succeeds
      -> LINEAGE_DISCARDED if any step fails

LINEAGE_DISCARDED
  -> create a new private workspace lineage and perform a full build
```

`SUCCEEDED` means the compiler/workspace operation completed successfully; it
does not by itself make the build generation publishable. The generation stays
behind `generation_publishable=false` until every oracle and publication gate
required by its case succeeds.

Only one build may exist per Worker. Workspace mutation remains on the build
thread; `STATUS` reads immutable/atomic snapshots and `CANCEL` only cancels the
exact build monitor. Every request response and asynchronous event carries its
request ID, `build_generation_id`, and a monotonically increasing protocol
sequence. Exactly one terminal event is emitted per accepted build operation.
For `CLEAN`, `operation_ok=true` and `compile_ok=null`; for `FULL` or
`INCREMENTAL`, `operation_ok=true` means the Workspace operation itself
completed and `compile_ok` reports whether Java compilation passed. A Java
compiler error is therefore `BUILD_COMPLETED` with `compile_ok=false`; it is not
`BUILD_ABORTED`, does not poison the workspace lineage, and permits the A7-style
incremental repair request. `BUILD_ABORTED` is reserved for a Worker, JDT,
protocol, I/O, or other infrastructure failure that prevents a trustworthy
completed build result.

The single terminal record is authoritative at the Runner boundary, not
dependent on the Worker surviving long enough to emit it. If the Worker exits,
the protocol stream breaks, or forced termination occurs before a valid
terminal frame is accepted, the Runner records exactly one `BUILD_ABORTED` and
rejects any late frame. An accepted cooperative cancellation that settles
normally records `BUILD_CANCELLED`; one that requires force is `BUILD_ABORTED`.

If completion wins before cancellation is accepted, `BUILD_COMPLETED` wins and
`CANCEL` returns `ALREADY_FINISHED`. If cancellation is accepted first,
`BUILD_CANCELLED` wins and no class or diagnostic output from that operation is
publishable even if JDT wrote files before settling. `STOP` follows the same
single-terminal-event rule: when completion already won it performs the normal
save/close sequence; otherwise it requests cancellation, waits for the one
cancel/abort terminal event, and then closes. Unknown/stale IDs are rejected
and cannot affect the active build. A cancelled build is settled only after
its build thread exits; the Worker may not start recovery or release workspace
ownership earlier.

Recovery is one atomic workspace-lineage recovery transaction, not merely a
second full-build request. It is distinct from the future Runtime publication
transaction and does not commit artifacts to HotSwap or restart. While
`RECOVERING`, both `CLEAN_BUILD` and `FULL_BUILD` receive their
own immutable build-generation records, but no intermediate output is
publishable and the workspace does not return to `READY`. Only a successful
clean (`operation_ok=true`, `compile_ok=null`), a successful full build
(`operation_ok=true`, `compile_ok=true`), and exact clean-full-oracle equality
commit the recovery transaction and return the lineage to `READY`. This means
only that the workspace lineage is trusted again; Runtime publication remains
a separate future gate. Any cancellation, compiler
failure, infrastructure abort, or oracle mismatch during recovery discards the
lineage; the runner must create a new private workspace lineage and establish a
fresh full baseline.

At least these lifecycle cases are required:

```text
active incremental build -> CANCEL -> bounded cooperative cancellation
cancelled build generation -> non-publishable; lineage is RECOVERY_REQUIRED
same Worker lineage      -> CLEAN + FULL recovery transaction -> oracle exact
build deadline expires   -> cooperative cancel -> release barrier ->
                            BUILD_CANCELLED -> RECOVERY_REQUIRED
STOP after build wins    -> one BUILD_COMPLETED -> clean save/close
STOP after cancel wins   -> one BUILD_CANCELLED -> bounded close
clean stopped Worker     -> offline body edit -> reopen -> incremental + oracle
abnormal Worker exit     -> missing clean marker invalidates saved state
invalid saved state      -> no incremental reopen; new private lineage + full
```

The initial cooperative cancellation/shutdown budget is five seconds. A
build deadline first triggers cooperative cancellation. Only if cancellation
or shutdown then fails to settle within its five-second budget may the Runner
terminate the exact identity-bound process tree and record that forced path; it
cannot be reported as cooperative success. The timeout case uses the
deterministic barrier so that deadline, accepted cancellation, barrier release,
build-thread exit, terminal event, and state transition are all observed rather
than inferred.

Workspace-lineage reuse requires a manifest containing
`workspace_lineage_id`, the last completed `build_generation_id`, and
fingerprints of the candidate, Worker/bundles, target-system library,
`project_model_fingerprint`, and source state at the last clean save. Only the
Runner owns the clean-shutdown marker. On startup it atomically consumes the
old marker before granting ownership. On shutdown it may atomically publish a
new marker only after Worker `SAVE_ACK`, zero exit, and confirmation that the
entire identity-bound owned process tree settled. Marker publication follows
the platform-safe file/replace/sync behavior defined by the general lifecycle
contract; the Worker cannot self-certify cleanliness. Invariant identity
changes invalidate reuse. A bounded source-only difference from the saved
fingerprint is recorded as an offline delta, not silently treated as
configuration drift. This upgrades A8's runner lineage label into an
independently persisted reuse precondition.

Every owned PID is recorded with creation time to prevent PID-reuse mistakes.
The runner continuously observes descendants, never signals an unowned process,
and verifies that all observed owned processes are absent after settlement.
A9-L does not require manufacturing an uncooperative JDT failure merely to
exercise force-kill; the force fallback remains unit-tested, while real A9
evidence must prove the normal cooperative path and abnormal-exit invalidation.

#### A9 acceptance record

A9 passes only when A9-S and A9-L pass and A9-M reports `PASS` on the same
locked candidate. `CONDITIONAL` is complete evidence but produces a
Conditional phase decision; `DIAGNOSTIC_RERUN_REQUIRED` blocks a decision until
the exact rerun is complete; `NO_GO` fails A9.

```text
baseline full build passed its correctness and oracle gates
11/11 warm-up requests passed their correctness and oracle gates
110/110 measured requests completed with the expected outcome
all successful states exactly matched their keyed clean-full oracle
all compiler-error/cancelled/aborted build generations were non-publishable
no silent full fallback, stale class family, stuck build, or Worker recreation
resource measurements are complete, stable, and in the A9-M PASS bands
cooperative cancel/stop settled within budget
cancel/abort recovery used the atomic CLEAN -> FULL -> oracle transaction
abnormal state was rejected rather than trusted
all identity-bound Worker/oracle process trees settled with no residue
```

The machine report records the workload version, operation index/type, source
fingerprint, requested/actual build kind, `operation_kind`, `operation_ok`,
nullable `compile_ok`, diagnostics/publication state,
oracle key and hit/miss, output equality, timing, raw resource samples,
cancellation timeline, process identities, `workspace_lineage_id`, immutable
`build_generation_id` and terminal outcome, recovery transaction, marker
ownership/publication, and every limitation. A9 remains tiny-fixture evidence
and makes no company-project, Lombok, HotSwap, or production
publication-performance claim.

Resource-delta instrumentation is still a separate Phase 1A Go requirement.
A9 must preserve its current explicit `unavailable` evidence and must not infer
a Java Builder delta from source fingerprints. If safe read-only delta
instrumentation is not reviewed as part of A9, it remains an explicit pre-Go
item rather than being silently closed by the repeated workload.

### A10 — Platform and path boundary

Phase 1A must pass on Windows, because that is the primary company dogfood
environment. It must also pass on at least one POSIX environment. At least one
run uses spaces and non-ASCII characters in the attempt and source paths.

## Proving incremental behavior

The experiment must not infer compiled units solely from class timestamps or
from the elapsed duration. It must capture builder-side or filesystem-write
evidence sufficient to distinguish:

```text
requested build kind
actual build kind
Java nature and configured builder identity
delta available / unavailable
resource delta summary
compiled source units, if observable
created/changed/deleted class families
full-build fallback and its reason
```

The instrumentation must not choose the affected sources on behalf of JDT.
Acceptable mechanisms include bounded Java Builder tracing, a read-only build
participant, or output-write observation combined with resource-delta and
actual-build-kind evidence. If the POC cannot prove whether Java Builder did
incremental work, Phase 1A has not passed.

If a `CompilationParticipant` is used, it is an observer under this contract,
not another build stage. Its extension and implementation must satisfy:

```text
modifiesEnvironment=false
createsProblems=false
aboutToBuild() returns READY_FOR_BUILD only
isAnnotationProcessor() returns false
isPostProcessor() returns false
no generated sources or folders
no BuildContext mutation or recorded dependencies
no added diagnostics
no class-byte modification
```

Before its observations are accepted, A1 must be repeated once with the
instrumentation disabled and once enabled. The complete output class set,
class SHA-256 values, and diagnostics must match exactly. A participant that
changes output or requests a full build invalidates the evidence candidate.

Requesting `INCREMENTAL_BUILD` is not sufficient evidence: when no usable
delta exists, Eclipse may invoke the builder as a full build. A normal return
from the build API is also not compilation success; every result must inspect
ERROR markers and build-path problems before a build generation can be accepted.

For every state-changing case, compare the complete incremental output tree
with a clean-full oracle using:

```text
relative class-file set
SHA-256 of every class file
error/warning diagnostic identity and source location
absence of obsolete output
```

Small runtime assertions may supplement this comparison, but they cannot
replace complete output and diagnostic comparison.

## Phase 1B Lombok fixture and cases

Phase 1B adds only Lombok `1.18.20`; it does not generalize annotation
processing. The fixture uses at least:

```text
@Data
@Builder
@Slf4j
@NonNull
a dependent source that calls a Lombok-generated getter/builder
a method that uses the Lombok-generated log field
a bounded lombok.config whose effect is observable in generated classes
```

The exact Lombok integration mechanism must be recorded. If Lombok must patch
the ECJ/Equinox process as a Java agent, that is part of the worker identity;
the experiment may not silently substitute delombok or javac-generated
classes.

Resolving Lombok annotations is not success by itself. The worker must prove
that the transform is active by compiling downstream calls to generated
members and code that uses the generated `log` field. The Lombok JAR SHA-256,
classpath presence, agent arguments when applicable, and activation evidence
belong to every Phase 1B report.

Phase 1B must execute:

1. a clean full build with all expected generated members;
2. a plain method-body edit in a Lombok-annotated class;
3. a field edit that changes Lombok-generated accessors and affects a
   dependent source;
4. a Lombok annotation edit that changes generated schema;
5. an edit to a source that consumes a generated getter or builder;
6. a `lombok.config` change, conservatively forced through a new workspace
   lineage and full build;
7. an error/recovery cycle involving a generated member;
8. at least 100 mixed incremental/no-op cycles after warm-up.

Every successful result must equal a clean-full oracle made with the same
pinned JDT/Lombok stack. Phase 1B does not compare ECJ bytes with Maven/javac
bytes and does not claim cross-compiler equivalence.

The project dependency remains exactly Lombok 1.18.20. Upgrading it to make the
experiment pass answers a different question and is prohibited.

## Measurements and decision bands

Measurements must separate process configuration from observed use.
`-Xmx` is a maximum heap setting, not an RSS measurement.

For each platform and version candidate, record at least:

```text
artifact download/archive bytes
installed bundle bytes and bundle count
Worker JDK identity and JVM arguments
configured Xms/Xmx
cold/warm process-tree identity
cold start to ready
full-build duration
no-op incremental duration
leaf-edit incremental duration
dependency-edit incremental duration
shutdown duration
heap and Metaspace used after an explicit GC request and bounded settlement
process_tree_rss_sum_bytes after the same settlement plus 30 seconds idle
peak heap and process_tree_rss_sum_bytes during full and incremental builds
process_tree_rss_sum_bytes after the repeated-build run
child-process count before, during, and after shutdown
```

The first POC uses these product decision bands, not performance claims:

| Observed stripped-worker state | Interpretation |
| --- | --- |
| Idle `process_tree_rss_sum_bytes` `<256 MiB` | Preferred |
| Idle `process_tree_rss_sum_bytes` `256–512 MiB` | Acceptable for continued evaluation |
| Idle `process_tree_rss_sum_bytes` `>512 MiB` and `<=768 MiB` | Conditional; requires explicit product review |
| Idle `process_tree_rss_sum_bytes` `>768 MiB`, or close to a measured full JDT LS baseline | No-Go for the preferred architecture |
| Tiny-fixture full-build `process_tree_rss_sum_bytes` peak `>1 GiB` | No-Go unless measurement error is proven |

Full-build peak memory on a later four-thousand-source company project is a
separate Phase 2 measurement and is not constrained by the tiny-fixture peak
alone.

Phase 1 does not impose a sub-second latency target. It requires that an
already-running worker demonstrably avoids a full build for eligible ordinary
edits. Performance promotion thresholds belong to Phase 2 on a real project.

Repeated-build `process_tree_rss_sum_bytes` may fluctuate, but after warm-up it must not show a
monotonic or unbounded trend. Any apparent growth must be rerun with heap and
native-memory evidence before a Go decision.

## Structured experiment report

Each run emits one machine-readable report and a short Markdown summary. The
report must include:

```text
attempt_id, workspace_lineage_id, and build_generation_id
git revision and dirty-worktree flag
operating system and architecture
locked artifact identities and hashes
Worker JDK / compliance / target / Lombok identities
target JDK 8 system-library fingerprint
system_library_discovery_method
fixture input fingerprint
requested and actual build kind per case
delta, compiled-unit, output-family, and diagnostic summaries
clean-full comparison result
timing and resource measurements
cancellation/shutdown settlement
workspace-restart result
warnings, limitations, and retained local artifact location
```

Absolute user paths and raw environment values stay local. A failed or partial
measurement is reported as unavailable; it is never coerced to zero. Facts,
inferences, and product recommendations must be separate fields or sections.

## Phase gates

### Phase 1A Go

All of the following are required:

- the worker uses the real Java Builder and proves actual incremental builds;
- the Java nature, Java Builder build spec, effective build kind, and resource
  delta are observed rather than inferred from the requested operation;
- A1 through A10 pass on Windows and at least one POSIX environment;
- every incremental build generation matches its clean-full oracle;
- dependency propagation, diagnostic recovery, deletion, rename, and stale
  output cleanup are correct;
- no healthy same-process edit silently loses incremental state;
- cancellation and shutdown are bounded and leave no owned process;
- resource use is within the acceptable or preferred decision bands;
- the minimal locked distribution excludes JDT LS and unrelated IDE services.

### Phase 1B Go

All of the following are required:

- the exact Lombok 1.18.20 works without changing project source semantics;
- the pinned JDK 8 system library rejects post-Java-8 platform APIs;
- Lombok full and incremental cases match their clean-full oracle;
- dependent sources observe generated-member changes correctly;
- config and annotation changes do not leave stale generated schema;
- lifecycle and repeated-build stability remain within Phase 1A bounds.

### Conditional result

Examples that require another design review rather than an automatic Go:

- state survives only while the worker remains alive;
- resource use is in the conditional band;
- Lombok works only with the 2021-03 compatibility anchor;
- one POSIX result differs while the required Windows path is stable;
- instrumentation proves correct output but cannot yet expose exact compiled
  source units.

### No-Go

Stop this preferred route if any of these remains after at most the two pinned
JDT candidates:

- a real incremental build cannot be distinguished from ECJ batch or full
  recompilation;
- ordinary deltas produce nondeterministic diagnostics, missing recompilation,
  or stale class families;
- Lombok 1.18.20 cannot be made correct without upgrading Lombok or patching
  user source;
- the worker requires JDT LS, M2E, Buildship, UI, or language-server services
  to remain stable;
- cancellation or shutdown is unreliable on Windows;
- the same live worker repeatedly loses its Java Builder state;
- observed resource use enters a No-Go band;
- the bundle set or platform lifecycle cannot be pinned and reproduced.

A No-Go freezes the result and reopens evaluation of ECJ Batch plus a bounded
joLink-owned dependency graph. It does not authorize implementing both routes
inside this POC.

## Phase 1A deliverables

Before a Phase 1A decision, the experiment branch must contain:

```text
this reviewed contract
an exact artifact lock with hashes and licenses
the minimal headless worker source/product definition
the plain Java fixture
a deterministic cross-platform runner
clean-full comparison tooling
bounded lifecycle and resource measurement tooling
Phase 1A machine-readable reports from Windows and one POSIX environment
a Phase 1A decision record: Go / Conditional / No-Go
```

## Phase 1B additional deliverables

Only after a recorded Phase 1A Go, Phase 1B additionally requires:

```text
the Lombok fixture
the exact Lombok 1.18.20 artifact lock and integration evidence
Phase 1B machine-readable reports from Windows and one POSIX environment
a Phase 1B decision record: Go / Conditional / No-Go
```

Experimental code should remain under a clearly isolated experiment surface
and must not be imported by production Runtime modules. Building or testing
the existing package without an explicit experiment command must not download
or start Eclipse artifacts.

## Promotion sequence after Phase 1

Passing this contract permits only the next design review:

```text
Phase 2 contract
    Maven Bootstrap
    versioned BuildWorldSnapshot
    conservative invalidation
    real company-project full/incremental measurements

Phase 3 contract
    JVM launched from one complete, verified ECJ build generation
    method-body delta → standard JDWP HotSwap
    schema delta → restart from a complete committed build generation
    readiness and HTTP business verification
```

Neither phase may mix Maven/javac baseline classes with later ECJ classes in
one Runtime artifact generation without independent compatibility proof.

MapStruct, QueryDSL, Spring metadata generation, resource fidelity, enhanced
HotSwap, MCP integration, worker idle policies in production, and public
installation remain later capability decisions.
