# Java Compile Strategy Roadmap

Status: `experimental / non-public`

This document is a design and evidence ledger. It is not an MCP contract.
The production `update` action continues to reject annotation processors, the
MCP Schema is unchanged, and no experiment described here may publish into the
user's Maven output or attach/redefine through JDWP.

## Purpose

joLink is evaluating two ways to avoid paying the full Maven lifecycle for
every edit:

```text
explicit_sources
    compile only 1..N source files explicitly selected by the caller

module_full_javac
    compile every main Java source in one selected Maven module
```

The experiments must answer, with evidence rather than assumptions:

1. Can joLink faithfully reproduce the Maven compiler and Processor model?
2. How often is `explicit_sources` sufficient for real edits?
3. Which edits require a complete module generation?
4. Is direct module compilation materially faster on large real projects?
5. Can a verified private generation later support HotSwap, Fast Restart, or
   targeted tests without reviving stale build output?

The prerequisite branch is `experiment/lombok-processor-model`. It validates
Lombok before either source-selection strategy becomes a product feature.

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
- Dependency and Processor artifacts are copied into the private generation;
  direct javac never keeps using a mutable repository path after validation.
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

## Shared compile model

Future strategies must share one build model. They may differ only in source
selection and the output-generation rules that follow from it.

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
- honor `config.stopBubbling`;
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
invalidates the evidence. Javac argfiles are written as UTF-8 and javac is
started with `-J-Dfile.encoding=UTF-8`; this keeps Windows/POSIX parsing tied
to the frozen compiler model instead of Python's locale.

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

Measurements record workspace snapshot, Maven baseline, metadata/model,
Processor resolution, source scan, javac, class scan, and total durations. A
single fast result without fidelity evidence has no product value.

## Internal experiment entry

The non-public command is:

```bash
python -m jolink_runtime.experiments.compile \
  --project-path /path/to/maven-project \
  --module app \
  --repeat 2
```

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

## Promotion gates

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
