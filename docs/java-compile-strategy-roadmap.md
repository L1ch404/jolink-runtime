# Java Compile Strategy Roadmap

Status: `experimental / non-public`

This document is a design and evidence ledger. It is not an MCP contract.
The production `update` action continues to reject annotation processors, the
MCP Schema is unchanged, and no experiment described here may publish into the
user's Maven output or attach/redefine through JDWP.

## Purpose

joLink evaluated two direct-javac strategies for avoiding the full Maven
lifecycle on every edit:

```text
explicit_sources
    compile only 1..N source files explicitly selected by the caller

module_full_javac
    compile every main Java source in one selected Maven module
```

That research line is now frozen. The experiments were designed to answer,
with evidence rather than assumptions:

1. Can joLink faithfully reproduce the Maven compiler and Processor model?
2. How often is `explicit_sources` sufficient for real edits?
3. Which edits require a complete module generation?
4. Is direct module compilation materially faster on large real projects?
5. Can a verified private generation later support HotSwap, Fast Restart, or
   targeted tests without reviving stale build output?

The retained branch is `experiment/lombok-processor-model`. It records the
Maven/compiler model and the cost exposed by real-project exploration. New
boundaries are not added merely because another project plugin or Processor is
discovered; work resumes there only when a later approved strategy needs a
specific capability.

## Non-negotiable invariants

- A fresh Maven compile from the same frozen inputs is the semantic oracle.
- Direct javac runs only when every modeled compiler input is reproducible.
- All experiment output stays under a user-selected/private attempt directory.
  POSIX applies mode `0700`; Windows inherits the directory ACL, so ACL
  verification and an explicit cleanup command remain promotion debts.
- Maven runs against a private workspace snapshot containing no copied build
  output; the user's `target` tree is fingerprinted and must remain unchanged.
- javac uses private `-d`, `-s`, and `-h` directories and an empty
  `-sourcepath`.
- The experiment does not register an MCP action, publish classes, HotSwap,
  restart a JVM, or run tests.
- POMs, classpaths, Build JDK, Processor artifacts/options, source set, source
  contents, and Lombok configuration are frozen and revalidated.
- Baseline gates compare ordered dependency and Processor artifact bytes, not
  only their filesystem paths. Explicit Processor artifacts are resolved both
  before and after the Maven baseline so an in-place repository replacement
  invalidates the evidence.
- Dependency and Processor artifacts are copied into the private generation;
  direct javac never keeps using a mutable repository path after validation.
- Compile dependencies with a manifest `Class-Path` are rejected because
  content-addressed relocation would change their relative lookup semantics.
- A successful javac exit is not correctness proof. A successful class
  redefinition would not be business-correctness proof either.
- Processor option values, environment values, and sensitive paths stay out
  of public summaries and errors.
- An unsupported model fails closed; it never becomes a partial success.

The workspace snapshot is build-output isolation, not an OS security sandbox.
Both the Maven oracle and javac are supervised subprocesses and may execute
trusted project build code. The experiment statically rejects project Maven
extensions/options and unmodeled compile-phase plugins before the baseline,
but it must still be run only against trusted source and build configuration.
Python never imports or instantiates an Annotation Processor.

## Shared direct-javac compile model

The retained direct-javac strategies share one compile-fidelity model. They
may differ only in source selection and the output-generation rules that
follow from it. This model is not the contract for the separate headless JDT
experiment.

The common model includes:

```text
workspace and selected module identity
effective POM and Maven compiler-plugin version
Build JDK and javac executable
source / target / release exactly as Maven configured them
encoding, debug/debuglevel, and -parameters
compile/provided/system dependency classpath
actual Reactor dependency outputs, not every Reactor module
main source roots and formal output location
Annotation Processor search path, selection, and -A options
configuration/environment fingerprints
```

Runtime classpath must never be used as a substitute for compile classpath.
Runtime-only, test-only, or unrelated Reactor entries must not make direct
javac succeed when Maven compile would fail.

The fidelity model is intentionally separate from the production
`FastCompilePlan`. Fast Update may safely tighten Maven `-source/-target` into
`--release`; the fidelity experiment must not, because that would compare a
different javac invocation with Maven.

## Maven plugin goal classification roadmap

The current Alpha implementation uses a small audited allowlist for Maven
executions whose POM omits an explicit phase. This is a fail-closed bootstrap
mechanism, not the intended long-term plugin architecture. A harmless goal may
be added to that list only when its lifecycle contract is independently
verified. For example, `maven-source-plugin:jar-no-fork` is safe for the
compile model because its declared default phase is `package` and, unlike
`source:jar`, it does not fork an earlier lifecycle.

The Alpha allowlist is keyed by plugin coordinate and goal, not by exact plugin
version or artifact fingerprint. It is therefore an audited compatibility
assumption, not proof of exact executable semantics. Adding version ranges,
property interpolation, plugin-management inheritance, or repository artifact
resolution to this temporary table would duplicate the general resolver
described below and is deliberately deferred.

joLink must not grow into one code branch per Maven plugin. The target design
separates three responsibilities:

```text
MavenGoalDescriptorResolver
    resolve exact goal identity and lifecycle metadata

MavenExecutionClassifier
    determine whether the requested operation schedules the goal or its forks

BuildCapabilityAdapter
    classify and reproduce the goal's build effects
```

`MavenGoalDescriptorResolver` reads the exact resolved plugin artifact's
`META-INF/maven/plugin.xml` without importing, instantiating, or executing
plugin code. Resolution evidence must include:

```text
plugin group/artifact/version and artifact fingerprint
goal name
POM explicit phase, if any
descriptor default phase
descriptor executePhase / executeGoal lifecycle fork
the lifecycle window relevant to the requested joLink operation
```

`executeGoal` references must be followed recursively within the resolved
plugin. The resulting goal-execution graph requires cycle detection, recursive
`executePhase` handling, and explicit `executeLifecycle` handling; an unknown
lifecycle or unresolved graph fails closed.

The execution classifier first asks whether the goal is scheduled at all. For
example, `source:jar` has a `package` default phase and can fork
`generate-sources` when invoked, but a `compile` operation never reaches its
`package` binding, so neither the goal nor its fork executes. Lifecycle forks
matter only after a goal has entered the requested operation. The ordering is:

```text
resolve explicit/default phase
→ determine whether this operation invokes the goal
→ if invoked, recursively follow lifecycle/goal forks
→ classify OUTSIDE_WINDOW / INSIDE_WINDOW / FORKS_INTO_WINDOW / UNRESOLVED
```

A goal outside the requested lifecycle window may be ignored only when exact
descriptor evidence proves that it will not be invoked. A normally harmless
goal becomes compile-affecting when the POM explicitly binds it to an earlier
phase. Operation context remains part of the evidence: equivalence to
`mvn compile` and provenance of classes loaded from a packaged artifact are not
interchangeable questions.

Plugin descriptors establish reachability and lifecycle behavior; they do not
generally describe semantic effects. An unknown goal bound to
`generate-sources`, for example, may generate Java, mutate the classpath,
change properties, transform resources, rewrite classes, or merely log a
message. `BuildCapabilityAdapter` therefore classifies and reproduces effects
by capability rather than product name:

| Capability | Examples | Default policy |
| --- | --- | --- |
| Test/report/deploy | Surefire, Site, Deploy | Ignore when outside the relevant lifecycle window |
| Auxiliary packaging | non-forking sources JAR | Allow when descriptor proves it cannot enter the compile window |
| Resources | Maven resources processing | Use a shared resource model |
| Source generation | Protobuf, OpenAPI, build-helper source roots | Require a source-generator model or reject |
| Annotation processing | Lombok, MapStruct, custom Processor | Require a Processor model or reject |
| Bytecode transformation | AspectJ, Hibernate enhancer | Require a transform pipeline with output parity or reject |
| Maven/core extension | `.mvn/extensions.xml`, build extensions | Treat as a separate high-risk Maven execution boundary |

Any execution inside the relevant window without an applicable capability
adapter remains a structured rejection. Descriptor metadata alone must never
be treated as proof that the resulting sources, resources, classpath, compiler
behavior, or class bytes are reproducible.

Plugin descriptors are untrusted input. They must be parsed with archive/XML
size limits, canonical artifact paths, no external entity resolution, and a
content fingerprint that is revalidated before compilation. The resolver may
download or locate metadata through the project's actual Maven/settings/local
repository context, but it must never execute the target goal to discover its
behavior.

The raw and effective Maven stages have different responsibilities. The first
item below describes the target boundary; current raw preflight covers only
unconditional project build configuration, with the active-profile extension
gap recorded under "Verified debts deferred during experimentation":

1. Raw preflight rejects visible project/core extensions and explicit early
   lifecycle steps that can already be proven unsafe.
2. Effective-POM validation resolves inherited versions, profiles, and plugin
   management.
3. Exact plugin descriptors and the execution classifier supply scheduling,
   default-phase, and lifecycle-fork evidence.
4. Capability adapters prove semantic reproducibility; unknown
   compile-affecting capabilities remain structured rejections with a reason
   category and never become partial success.

Specialized code should therefore be written for a small number of build
capabilities, not for every plugin coordinate. Hibernate runtime proxies, for
example, are not a compile plugin concern, while Hibernate bytecode enhancement
belongs to the shared bytecode-transform category. A future adapter may model
that transform category; until then it remains a safe false negative.

Migration from the Alpha allowlist should proceed only after the Lombok/direct
javac experiments establish value:

```text
audited allowlist for immediate dogfood unblock
→ exact descriptor resolver + reachability classifier + rejection diagnostics
→ capability-level Processor/generator/transform adapters
→ evidence cache keyed by POM/settings/profile/JDK/Maven/plugin fingerprints
→ shrink the static allowlist to compatibility fallback only
```

The objective is not to accept every Maven project. It is to make ordinary,
non-compile-affecting plugins classify automatically while keeping an explicit
evidence boundary around anything that can change sources, compiler behavior,
resources, or final class bytes.

## Phase 0: Lombok AnnotationProcessorModel

The model freezes:

```text
proc mode
ordered Processor search path (private)
artifact fingerprints
META-INF/services/javax.annotation.processing.Processor providers
explicit Processor names
-A option names and private values
Lombok version when present in the manifest
generated-source/header/class output directories
Build JDK and compiler arguments
```

Python only reads artifact metadata. Maven baseline and javac execution both
remain supervised subprocesses; neither is presented as a security sandbox.

### Processor precedence

```text
proc=none
    reject the Lombok experiment; never enable a Processor behind this policy

proc=only
    reject because it does not produce a complete module class generation

annotationProcessorPaths present
    resolve the declared org.projectlombok:lombok artifact with the same
    Maven/JDK/settings/profile/local repository and use it as the only search
    path

annotationProcessors present without an explicit path
    load only the named Lombok Processor from compile classpath; valid on
    JDK 23+ because processing is explicitly requested

proc=full without an explicit path
    use compile classpath discovery explicitly; valid on JDK 23+

none of the above
    allow implicit compile-classpath discovery only through JDK 22
```

Lombok normally advertises both:

```text
lombok.launch.AnnotationProcessorHider$AnnotationProcessor
lombok.launch.AnnotationProcessorHider$ClaimingProcessor
```

They are one Lombok distribution, not two unrelated Processors. Any other
service provider, a mixed Processor set, a duplicate Lombok transformation
artifact, or an incomplete Processor path is rejected.

The P0 artifact-copy shortcut is intentionally Lombok-specific. A general
Processor model must resolve the complete ordered transitive Processor path;
it must not generalize the single-artifact shortcut.

### `lombok.config`

joLink does not reimplement individual Lombok settings. Lombok remains the
interpreter. The experiment only freezes the configuration graph it can read:

- search upward from every source directory;
- record present files and missing candidate paths;
- honor per-file last-wins `config.stopBubbling` operations (`true`, `false`,
  and `clear`), process each imported config once per source resolution, and
  stop parent lookup when any visited config contributes an effective `true`;
- recursively copy workspace-relative file imports into the source mirror;
- fingerprint existence, relative path, content, and import role;
- recompute the source set and configuration graph before and after javac;
- put a synthetic `config.stopBubbling=true` above the private workspace so a
  host temp-directory config cannot leak into Maven or javac.

The current baseline snapshot also requires conventional Maven
`target/classes` output for every Reactor module. Custom build/output
directories remain rejected until joLink can prove that no stale generated
source, resource, or class directory was copied into the baseline.

The prerequisite P0 command is narrower still: it accepts one standalone Maven
`jar` module. Reactor `-am` builds are rejected until every participating
module has the same Processor/configuration/transform validation.

Absolute imports, `~`, environment imports (`<...>`), archive imports (`!`),
workspace escapes, symlinks, cycles, excessive depth, and external effective
configuration are rejected in P0.

## Strategy differences

| Property | `explicit_sources` | `module_full_javac` |
| --- | --- | --- |
| Source input | Caller-selected 1..N files in one module | Every main Java source in one module |
| Own old output on compile classpath | Usually required for unchanged sibling types, so it is a weaker model | Forbidden; an empty private classes directory replaces it |
| Output completeness | Only class families generated by selected sources | Complete Java class generation for modeled sources |
| Best first use | Method-body HotSwap and coverage measurement | Fidelity/performance experiment and future Fast Restart |
| Main stale risk | Deleted anonymous/inner/generated classes in an old family | Resources or generated sources outside the modeled javac invocation |
| Automatic fallback during experiments | Never | Never |

Both strategies compile one module per attempt. Cross-module source batches
remain unsupported until Reactor propagation has an explicit model.

Experiments must not automatically upgrade `explicit_sources` to
`module_full_javac`; doing so would hide the real coverage and rejection data.

## Evidence ladder and fresh Maven baseline

Evidence levels are cumulative:

```text
probe_ready
    effective compiler and Lombok model can be resolved

direct_javac_success
    one private direct invocation completed

direct_deterministic
    repeated direct invocations have identical class sets and SHA-256 values

verified_exact
    direct output is deterministic and exactly equals a fresh Maven baseline
    from the same private frozen workspace
```

The experiment creates its own baseline:

```text
copy workspace without target/build output
→ put it below a synthetic Lombok bubbling guard
→ run supervised Maven compile with the selected IDEA JDK/settings/profile
→ scan the selected module's fresh class output
→ run direct javac twice from the same frozen source/configuration inputs
```

Effective compiler metadata is resolved before the lifecycle runs and again
after it. A changed source/configuration manifest or semantic compiler model
invalidates the evidence. Generated effective-POM comments are not compared as
raw bytes; authoritative POM/settings inputs and the parsed compiler,
dependency, and Processor models are compared instead. Javac argfiles are
written as UTF-8 and javac is
started with `-J-Dfile.encoding=UTF-8`; this keeps Windows/POSIX parsing tied
to the frozen compiler model instead of Python's locale.

`--probe-only` returns immediately after the effective compiler, classpath,
Processor, and Lombok configuration models are resolved. It does not execute
the Maven compile baseline or direct javac, and therefore proves model support
only—not compilation fidelity.

On JDK 8, Maven Compiler Plugin 3.13+ translates `<release>` into
`-source`/`-target`; the replay model mirrors that behavior instead of passing
the unsupported javac `--release` flag. Older or unresolved plugin models fail
closed.

An existing `target/classes` tree, even if labeled “clean” by a caller, is
diagnostic-only and can never authorize a product decision. Ordinary Maven
`compile` in a workspace that already contains output is also not a clean
baseline because removed classes may survive.

Automatic proof currently requires class-set and per-file SHA equality. A SHA
difference is `requires_review`: it neither proves semantic failure nor allows
automatic promotion. The current HotSwap class comparator does not normalize
ordinary method bytecode, so it is not a semantic-equivalence oracle. A future
`semantic_match` must compare all method code, exception tables, annotations,
inner/nest/record/bootstrap metadata, and static initialization semantics.

Measurements use non-overlapping phase buckets for workspace snapshot,
metadata resolution, Processor resolution, model validation, Maven baseline,
baseline class scan, artifact freeze, direct javac/overhead, comparison, and
total wall time. A single fast result without fidelity evidence has no product
value.

## Internal experiment entry

The non-public command is:

```bash
python -m jolink_runtime.experiments.compile \
  --project-path /path/to/maven-project \
  --module app \
  --repeat 2
```

For a guarded Windows/company-project execution procedure, use the Chinese
[Lombok compile experiment runbook](lombok-compile-experiment-runbook.zh-CN.md).
It defines Probe gating, full-run commands, evidence interpretation, and
sensitive-data reporting rules for an executing Agent.

It emits one compact JSON result and retains private artifacts for diagnosis.
The JSON exposes opaque attempt IDs, not absolute paths or raw compiler log
tails. Full logs remain local to the selected attempt directory.
Every result explicitly states:

```text
public_api_changed=false
target_outputs_modified=false
runtime_jdwp_touched=false
subprocess_isolation=supervised_not_security_sandbox
```

`--external-baseline-output` is always labeled `external_unverified`.

### Evidence recorded on this branch

The isolated fixture currently proves, on macOS with real Maven/javac:

```text
JDK 8  : explicit ProcessorPath, implicit classpath, explicit Processor name
JDK 17 : explicit ProcessorPath, implicit classpath, explicit Processor name
result : direct A == direct B == fresh Maven (class set + SHA-256)
```

The fixture includes nested/imported Lombok configuration, the main Lombok
generation annotations, non-ASCII source content, spaces/non-ASCII in project
and attempt paths, a literal `$` Processor name, and a javac argfile over
32 KiB. CI repeats the real experiment on Ubuntu and Windows for JDK 8/17.
JDK 23+ behavior currently has model/unit evidence only and is not claimed as
a verified real-compiler result.

## Company exploration evidence: 2026-08-11

A trusted, single-module Maven project was used to continue the Windows/JDK 8
Probe. The run did not reach `model_resolved`: it stopped with
`MULTIPLE_ANNOTATION_PROCESSORS_UNVERIFIED` after finding Lombok plus Spring
Boot's `ConfigurationMetadataAnnotationProcessor` through compile-classpath
discovery. Maven baseline and direct javac were not executed, the user's Maven
output was not modified by the Probe, and JDWP was not touched.

This run is exploration evidence only. Before reaching the final Processor
rejection, the operator temporarily removed one compiler argument from the
project POM and locally bypassed the dependency-manifest rejection. Those
changes were useful for discovering the next boundary, but no exact-class or
product decision may be based on the resulting state. A trusted rerun must
restore the real POM and remove every local joLink bypass.

The run exposed three distinct compile-model gaps:

| Boundary | What was observed | Current interpretation | Required direction |
| --- | --- | --- | --- |
| javac JVM argument | `-J-Xmx2048m` was parsed as if every `compilerArgs` entry had to be a Processor `-A` option | Small argument-classification gap; the option affects the javac process rather than Processor selection | Separate bounded `javac_jvm_args` from `processor_options`; do not ask the project to delete the real setting |
| Dependency manifest classpath | A compile dependency declares `Class-Path` and private content-addressed relocation can change relative resolution | Real classpath-fidelity boundary; a filename/version bypass is not valid evidence | Resolve the original recursive manifest expansion and preserve or prove equivalent private layout/order |
| Additional Processor | Spring Boot metadata generation is discovered beside Lombok | Not benign/ignored: it writes CLASS_OUTPUT metadata, can report diagnostics, and has ordering/input relationships with Lombok | Model it as a known `METADATA_GENERATOR`, keep it visible in evidence, execute it when Maven does, and reject any remaining unknown Provider |

The current experiment passes the ordered dependency classpath as the implicit
Processor path, so direct javac is already capable of discovering both known
Processors. The missing piece is not merely removing the Lombok-only guard.
The model must retain Provider/artifact/order evidence and distinguish:

```text
Lombok                                      AST_TRANSFORM
Spring ConfigurationMetadataAnnotationProcessor  METADATA_GENERATOR
all other discovered Providers             UNKNOWN → reject
```

The primary experiment conclusion remains exact `.class` equivalence. If a
metadata-generating Processor is accepted, auxiliary evidence should also
compare `META-INF/spring-configuration-metadata.json` (exact first, canonical
JSON only if later justified), report one-sided output, and verify that no
unexpected generated Java source appeared. Class equality and metadata-output
equality must remain separate claims.

The dependency-manifest bypass must not be generalized to JAXB or any named
library. A correct solution works from the manifest graph and the actual
original filesystem resolution. An unresolved original reference and a
resolved recursive dependency are different cases; both must remain equivalent
after freezing. Rewriting signed dependency JARs is not an acceptable shortcut.

These findings show that complete Maven-to-direct-javac equivalence has a much
larger compatibility surface than compiler invocation alone. They do not prove
that direct compilation is impossible, and they do not yet provide performance
or correctness data because the full experiment never ran.

## Candidate architecture: Maven bootstrap plus JDT incremental build

Status: `Phase 1A contract approved / bootstrap implementation in progress`

The Phase 1 evidence requirements and Go/No-Go boundary are defined in
[`jdt-incremental-poc-contract.md`](jdt-incremental-poc-contract.md). Its review
is complete. A dedicated experiment branch may implement only Phase 1A; Phase
1B requires a recorded Phase 1A Go decision.

The alternative under consideration is not “replace javac with ecj.jar”. ECJ
batch compilation still requires joLink to supply source roots, classpath,
Processor path/options, output directories, language level, and resources. The
candidate is instead:

```text
formal Maven bootstrap
→ capture one versioned Build World
→ create a private JDT project
→ JDT full build establishes ECJ output and incremental state
→ launch the target JVM from that private ECJ generation
→ later Java deltas use JDT incremental build
→ schema-compatible output uses standard JDWP HotSwap
→ schema-incompatible output uses restart from a complete private generation
```

Maven remains the Build World constructor; JDT would maintain that world
between invalidations. This changes the primary engineering problem from
reimplementing arbitrary Maven plugins to capturing provenance and deciding
when the captured world is stale. It does not eliminate build semantics.

The candidate requires a versioned Build World containing at least:

```text
project/module identity and source roots
generated-source roots and their producer/input provenance
ordered compile classpath and artifact fingerprints
language/target/encoding/debug/compiler settings
Processor path, Provider order, options, and generated-output locations
resource roots and whether each output is plain-copied, filtered, or generated
Maven/settings/profile/parent/toolchain/plugin input fingerprints
JDT/ECJ and Lombok compatibility identity
```

Any change to a Build World input must cause Maven re-bootstrap plus a new JDT
full build. Ordinary Java changes may use incremental build only while that
generation remains current. POM/parent/settings/profile changes, dependency or
Processor replacement, generated-source inputs, `lombok.config`, resource
filtering inputs, and unknown non-Java changes are initial invalidators.

The JVM should not normally start from Maven/javac classes and then mix in ECJ
classes. javac and ECJ may emit different synthetic/bridge/schema details even
for semantically equivalent source. One runtime generation should therefore use
the same compiler lineage: Maven constructs the model, JDT performs a private
full build, and that ECJ output starts the JVM before subsequent ECJ deltas.

This route has unresolved Go/No-Go risks:

1. True JDT incremental compilation is an Eclipse Java Project Builder backed
   by workspace/core-resources state, not a standalone ECJ incremental API.
   Headless Equinox integration, lifecycle cleanup, distribution size, cold
   start, idle RSS, peak RSS, and cancellation must be measured.
2. The company project uses Lombok 1.18.20. Lombok integrates deeply with ECJ,
   so a compatible JDT/worker runtime must be proven rather than assuming that
   the newest JDT can load an old Lombok agent.
3. JSR-269 incremental behavior must eventually be proven for metadata and
   source-generating Processors. Lombok alone is insufficient evidence.
4. JDT copies ordinary non-Java resources from configured source roots, but it
   does not automatically reproduce Maven filtering, custom resource roots, or
   plugin-generated resources. Resource generation and runtime reload remain
   separate contracts.
5. Standard `RedefineClasses` still rejects field/method/schema/hierarchy
   changes. Fast compilation remains valuable if a complete private generation
   can restart without Maven, but enhanced HotSwap agents are not part of the
   initial candidate.

The approved experiment may create a dedicated branch, worker, and locked
dependency set only for Phase 1A. It may not change an MCP action, production
`update`, or the public Schema. Phase 1B adds the exact Java 8/Lombok 1.18.20
boundary only after Phase 1A passes. Maven Bootstrap, company-project import,
target-JVM launch, HotSwap, and Fast Restart require later contracts.

MapStruct/QueryDSL/Dagger, Maven resource fidelity, JDT LS as a permanent
dependency, enhanced HotSwap, MCP integration, and production promotion remain
outside that first POC. Passing the POC would justify a second design review;
it would not by itself select JDT as joLink's production compiler architecture.

## Verified debts deferred during experimentation

The following findings were reproduced on 2026-08-09. They do not block
experiments against source and Maven configuration already trusted by the
operator, but they block promotion to an untrusted-project or product safety
boundary.

### Maven extensions declared inside profiles

Raw preflight currently inspects only unconditional project build
configuration. A profile can therefore declare either a build extension or a
plugin with `extensions=true` and pass raw preflight. If that profile is active,
even a metadata command such as `help:effective-pom` resolves and loads the
extension before joLink can inspect the resulting effective model. Post-model
validation prevents direct javac from being trusted, but it cannot undo code
loaded during metadata resolution, and `--probe-only` is affected as well.

Before promotion, raw preflight must reject extension-loading declarations in
every declared profile without rejecting unrelated inactive-profile compiler
or lifecycle configuration. Parent/profile sources that Maven can activate
must be included in the same pre-execution safety model, or metadata resolution
must run inside a security boundary that is explicit about executing build
extensions.

### Pre-release Maven Compiler Plugin versions

The current numeric helper treats qualifiers such as `3.13.0-M1`,
`3.13.0-alpha-1`, and `3.13.0-SNAPSHOT` as satisfying the final `3.13.0`
threshold used for JDK 8 `<release>` translation. Until a Maven-compatible
version model is justified, promotion should fail closed for every qualified
version and accept only unqualified final versions at or above the proven
threshold.

### Compiler input snapshot ownership

Compiler-input invariants currently span plan creation, fingerprints,
freshness checks, artifact freezing, semantic comparison, and public change
categories. The experiment keeps these synchronized today, but each new input
has several update sites. After the two compile strategies produce stable real
project evidence, consolidate these fields behind one immutable compiler-input
snapshot with a single diff operation. Do not perform that refactor while the
experimental model is still changing.

## Future Fast Restart generation

This is deliberately not implemented by the Lombok experiment.

### `explicit_sources`

A restartable generation cannot be `staging + target/classes fallback`.
That would resurrect classes deleted by the edit. It must instead:

```text
start from a trusted formal baseline generation
→ identify every old class family belonging to each changed source
→ delete the complete old family, including anonymous/inner classes
→ overlay the newly generated family
→ validate one complete private generation
```

### `module_full_javac`

The private complete class set replaces the selected module's own output entry;
it is not merely prepended before old `target/classes`. Upstream dependency
outputs remain separate classpath entries.

### Resources and generated output

Classes and resources require separate models. A restart generation is not
complete until joLink can reproduce resource copying/filtering, remove stale
resources, preserve `META-INF/services`, and account for generated sources and
Processor resources. If those transformations cannot be frozen, Fast Restart
must refuse and use formal Maven restart.

A verified generation is bound to source/configuration/runtime generation.
Only a newly started JVM that passes readiness can become active. Normal
restart continues to discard runtime-only HotSwap bytes unless a future
explicit contract selects a verified private generation.

## Future Fast Test

Targeted tests are also deferred. The intended ordering is:

```text
verified private main generation
→ modeled test compilation or a proven-current test baseline
→ targeted Surefire/JUnit invocation without silently running compile phases
→ parse fresh reports and prove tests_run > 0
```

Test success cannot compensate for a compiler-model mismatch.

## Rejection ledger and thaw conditions

These are evidence debts, not declarations that support is impossible.

| Current rejection | Risk | Minimum evidence before thawing |
| --- | --- | --- |
| Implicit plugin goal with unresolved descriptor | Hidden default phase or lifecycle fork could enter the compile window | Exact versioned plugin descriptor, artifact fingerprint, phase/fork classifier, and adversarial no-fork/fork fixtures |
| Non-Lombok or multiple Processors | Hidden generated API/method behavior and dependency ordering | Complete transitive Processor resolver, mixed fixtures, fresh Maven exact/semantic parity |
| `proc=none` / `proc=only` | Violating formal policy or incomplete class output | No thaw for Lombok full generation unless product semantics change explicitly |
| JDK 23+ unrequested discovery | Direct javac could run a Processor Maven did not run, or vice versa | Versioned javac/Maven precedence tests and explicit activation model |
| Unresolved Processor artifact/path | Wrong Processor version or missing helper JAR | Same Maven Resolver semantics, ordered path fingerprint, offline/Windows tests |
| Unsupported compiler arguments | Bytecode/API/debug behavior may differ | Parsed argument model plus adversarial fixtures and baseline parity |
| External/env/archive Lombok config | Configuration cannot be frozen safely | Stable snapshot representation and concurrent-change tests |
| Generated/custom source roots | Missing generated types or compiling stale files | Source-root producer model and fresh-generation comparison |
| JPMS, non-jar, cross-module source batch | Module path or output ownership ambiguity | Dedicated graph/module-path contract and real Reactor fixtures |
| Reactor baseline / `-am` upstream modules | Upstream Processor or generator can affect selected output without being modeled | Validate the complete participating closure and prove fresh multi-module parity |
| AspectJ/enhancer/bytecode transform | javac output is not final Maven output | Reproducible post-compile transform pipeline and output parity |
| Maven extensions/toolchains/custom compiler/fork/ECJ | Actual compiler/runtime is not modeled | Resolved toolchain/compiler identity and platform matrix |
| Resource filtering/transforms | Restart generation could carry wrong or stale resources | Complete resource generation/deletion model |
| Input mutation during compile | Evidence mixes generations | Full fingerprint/generation revalidation and cancellation stress tests |
| Non-fresh Maven output | Removed classes may survive and create false equality | Private workspace baseline with no copied build outputs |

Every thaw requires representative fixtures, at least one varied real project,
Windows path/argfile coverage, JDK-version coverage, cancellation cleanup, and
zero false success in adversarial tests.

## Active Phase 2A Maven-to-JDT experiment

The direct-javac line remains frozen. The active experiment now treats Maven
as the bootstrap authority and asks a narrower question:

```text
formal Maven clean compile
-> frozen BuildWorldSnapshot
-> private JDT FULL_BUILD
-> tiered structural comparison
```

The executable contract and company runbook are:

- `docs/jdt-phase2a-real-maven-build-world-contract.md`
- `docs/jdt-phase2a-real-maven-build-world-contract.zh-CN.md`
- `docs/jdt-phase2a-company-runbook.zh-CN.md`

This route shifts the long-term problem from reproducing every Maven plugin to
capturing a build world and invalidating it when Maven-owned generation inputs
change. It still does not make plugin semantics disappear: generated-source
provenance, compile-time Processor refresh, resource transforms, bytecode
enhancers, toolchains, and reactor closure remain explicit invalidation or
fallback boundaries. Phase 2A records those boundaries instead of adding a
new plugin-specific allow-list.

## Retained direct-javac promotion gates

Status: `inactive while the direct-javac research line is frozen`

### Lombok model

- Unknown or mixed Processors are rejected before javac.
- Nested/bubbling/relative-import configs produce the same output as Maven.
- `@Getter`, `@Data`, `@Builder`, `@RequiredArgsConstructor`, `@Value`,
  `@Slf4j`, and `@NonNull` are represented in baseline comparisons.
- Direct A equals direct B and both equal fresh Maven on JDK 8 and 17.
- Windows space, Unicode, `$` Processor names, and long argfiles execute for
  real rather than only passing string assertions.
- CI runs the exact baseline on Ubuntu and Windows with JDK 8 and 17. JDK 23+
  implicit/explicit activation remains a promotion gate rather than a current
  support claim.

### `module_full_javac`

- Own old output is absent from both compile and Processor search paths.
- A deleted or missing source cannot be hidden by stale classes.
- Standard single- and multi-module Reactor fixtures pass.
- Large-project performance is materially better after model cost is separated.

### `explicit_sources`

- Source-to-class-family mapping and deletion are reliable.
- Coverage and upgrade reasons are measured without automatic fallback.
- Changes requiring dependent or downstream recompilation are never reported
  as complete.

### Fast Restart

- One complete class/resource generation exists without old-output fallback.
- Cancellation, rollback, process ownership, and readiness are bounded.
- A failed generation never replaces the current JVM or formal output.

## Open decisions

- Whether a complete semantic class comparator is worth its maintenance cost.
- How a general Processor path should reuse Maven Resolver semantics.
- Whether safely snapshotted external Lombok config should ever be allowed.
- Compile-model caching and precise invalidation after experiments prove value.
- ABI-based downstream Reactor propagation.
- Reproducible Maven resource and generated-source modeling.
- The point at which Fast Test becomes more valuable than another compile
  strategy optimization.
