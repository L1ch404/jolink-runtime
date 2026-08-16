# Headless JDT Incremental Worker Experiment

Status: `diagnostics-v2 candidates have local functional A1-A8 and Phase 2A evidence; clean-worktree canonical evidence plus A9/A10/Phase 1B remain pending, while prior results stay attached to the previous exact Worker artifacts`

Contract:

- [English](../../docs/jdt-incremental-poc-contract.md)
- [中文](../../docs/jdt-incremental-poc-contract.zh-CN.md)

This directory is isolated experimental code. Production Runtime modules do
not import it, and ordinary package builds/tests do not download or launch
Eclipse.

## What the first vertical slice proves

The current bootstrap can:

1. read the official Eclipse 4.40 p2 metadata;
2. resolve mandatory `osgi.bundle`, `java.package`, and
   `osgi.extender` capabilities from a fixed root set;
3. parse, satisfy, and lock every selected bundle's `osgi.ee` requirement
   against the pinned Worker Java 17 capability;
4. fail closed if a selected unit has an unsupported mandatory capability;
5. download and hash only the resolved bundles;
6. derive bundle license identity from signed bundle content;
7. compile a small joLink Equinox application without Maven;
8. launch it in a private configuration/workspace with JDT Core
   `3.46.0.v20260520-1003`;
9. configure exactly one real `org.eclipse.jdt.core.javabuilder`;
10. capture target javac's complete platform view, including bootstrap
    placeholders and extension archives, from the exact target JDK 8;
11. run full, leaf incremental, no-op incremental, upstream method-body, public
    API propagation, compile-time constant propagation, source deletion, and
    source/type rename, same-Worker error/recovery, and saved-workspace restart
    builds;
12. reject a source that references Java 9's `List.of`;
13. observe actual batch/incremental behavior and compiled source units through
    a read-only `CompilationParticipant`;
14. prove instrumentation OFF/ON output parity;
15. compare the leaf incremental output with a separate clean-full oracle; and
16. enforce exact FULL/INCREMENTAL build-kind or NO_COMPILE outcome gates,
    including source-unit and class-output sets, rather than inferring the
    build kind from stable SHA values;
17. emit a contract-shaped v3 report with provenance, unavailable/not-run
    measurements, lifecycle settlement, and complete class SHA maps;
18. revalidate the candidate lock/artifacts, target-system snapshot, fixture
    checkout, and Git worktree identity after the run; and
19. stop all owned Equinox workers;
20. execute the frozen A9-S workload in one Worker: one 11-operation warm-up
    epoch plus ten measured epochs (121/121 requests total), with immutable
    build-generation identities and attempt-scoped clean-full oracles;
21. measure Worker heap/class metadata and fixed-cadence identity-bound process
    tree RSS, including per-build sampled peaks and a 30-second idle value; and
22. prove explicit/deadline cancellation, both STOP race outcomes, atomic
    CLEAN/FULL recovery, clean-marker reopen with an offline source delta, and
    abnormal-exit state invalidation in separate workspace lineages.

The current closure is 23 Eclipse/OSGi bundles plus the joLink worker bundle.
The locked bundle bytes total 16,096,227 bytes (about 15.4 MiB). It does not
include JDT LS, M2E, Buildship, SWT, JDT UI, Eclipse UI, or debug bundles.

## Current evidence boundary

The local Zulu JDK 8 used for the first macOS smoke advertises three absent
entries in `sun.boot.class.path`. Snapshot v2 now preserves those entries as
stable `ABSENT` placeholders while materializing only the 17 present entries
from target javac's 20-entry `PLATFORM_CLASS_PATH`. It also captures 11
extension archives; javac's extension view exactly matched the runtime
Extension ClassLoader cross-check. No effective endorsed archive was present.

The latest real macOS run therefore reports:

```text
status          = a9_evidence_passed
evidence_status = partial_phase_1a_evidence_a1_through_a9
```

The previously recorded A1-A10, A9-S/M/L, and Phase 1B results remain evidence
only for the exact Worker artifacts frozen in the original
`eclipse-4.40-current` and `eclipse-2021-03-lombok-anchor` locks. The bounded
error-first diagnostics protocol changed the Worker artifact, so active
development now uses separate `*-diagnostics-v2` candidate identities and
locks. No historical A9, A10, or Phase 1B conclusion is inherited by those new
candidates. Both diagnostics-v2 stacks have passed a local A1-A8 functional
run, and the anchor has passed the 16-source Spring Boot Phase 2A zero gate;
those dirty-worktree development runs are not canonical evidence. A clean-
worktree canonical rerun is required before each stronger claim is renewed.

## Files

```text
candidate-bootstrap.json / candidate-bootstrap-eclipse-2021-03.json
    preserved bootstrap identities for the historical Worker protocol

candidate-bootstrap-diagnostics-v2.json /
candidate-bootstrap-eclipse-2021-03-diagnostics-v2.json
    active bootstrap identities for the bounded error-first diagnostics protocol

bootstrap_candidate.py
    p2 metadata parser, capability resolver, artifact downloader, lock writer

locks/eclipse-4.40-current.json /
locks/eclipse-2021-03-lombok-anchor.json
    preserved exact locks for historical evidence; current runners do not
    mutate or silently reuse them

locks/eclipse-4.40-current-diagnostics-v2.json /
locks/eclipse-2021-03-lombok-anchor-diagnostics-v2.json
    active exact bundle and Worker identities; currently only newly rerun
    evidence may be attributed to them

build_worker.py
    verifies the lock, compiles the Worker with javac 17, builds the OSGi
    bundle, and writes the deterministic config template

worker/
    Equinox IApplication and read-only CompilationParticipant

target-system-helper/
    Java 8 helper that captures target javac PLATFORM_CLASS_PATH plus
    bootstrap/extension/endorsed provenance

fixtures/plain-java/
    tiny Maven-free Phase 1A fixture

fixtures/dependency-java/
    A5 public-API and compile-time-constant propagation fixture

fixtures/class-family-java/
    A6 delete/rename fixture with member, anonymous, local, and bridge outputs

fixtures/recovery-java/
    A7 deterministic compile-error and same-Worker recovery fixture

fixtures/java9-api-negative/
    companion source proving the Java 8 platform rejects List.of

run_bootstrap_smoke.py
    private target-library capture, Worker launch/restart,
    full/incremental/no-op, instrumentation parity, clean-full oracle, and
    bounded shutdown

run_a9_experiment.py
    frozen A9-S/M/L workload, attempt-scoped Oracle catalog, fixed-cadence
    process-tree sampling, lifecycle races, recovery, and Runner-owned lineage
    manifest/clean-marker evidence

run_lombok_experiment.py
    exploratory Phase 1B Lombok 1.18.20 full/incremental compatibility,
    clean-full oracles, generated-member propagation/recovery, and repeated
    mixed edit/no-op stability; the full run also records fixed-cadence
    process-tree sampling and an A9-M resource decision. Its compatibility-only
    mode compares bounded lombok.config and @Builder(toBuilder=true) behavior
    across candidates

run_real_maven_build_world.py
    private Phase 2A experiment: runs the real Maven clean-compile baseline,
    freezes one representative Java 8 module as BuildWorldSnapshot v1,
    classifies Maven-discovered binary/non-binary classpath entries, excludes
    the module's old output from the JDT classpath, executes a private JDT FULL
    build, and records tiered cross-compiler structural evidence. Supplying a
    private Maven Probe report makes Maven-native source/classpath/reactor facts
    authoritative while compiler/Processor metadata remains an explicitly
    reported legacy effective-POM layer

fixtures/cross-compiler-compatibility
    versioned Java 8 source-portability fixture: javac accepts the raw
    double-brace anonymous collection with unchecked warnings, while the
    locked ECJ 3.25 compiler rejects its generic inference. This is not a
    Build World dependency-gap fixture.

run_cross_compiler_compatibility.py
    expected-divergence probe: target JDK 8 javac must accept the versioned raw
    collection fixture with an unchecked warning, while locked ECJ 3.25 must
    reject it with the known type-mismatch family

maven-probe/ and run_maven_probe_spike.py
    experimental Maven-native Build World exporter. A dependency-light normal
    Mojo is staged in a content-addressed local file repository, injected via
    an attempt-private settings file, and executed in the target reactor/session
    without changing project POMs. Strict offline mode explicitly seeds the
    selected Maven local repository. Its private report can feed the explicit
    Phase 2A hybrid path; this does not authorize Phase 2B or Runtime publication.
```

## Reproduce on macOS/POSIX

The commands below are explicit experiment commands. The first command needs
network access; later commands use the locked local cache.

```bash
uv run python experiments/jdt-incremental-worker/bootstrap_candidate.py

uv run python experiments/jdt-incremental-worker/build_worker.py \
  --java-home /path/to/jdk-17

uv run python experiments/jdt-incremental-worker/run_bootstrap_smoke.py \
  --worker-java-home /path/to/jdk-17 \
  --target-java-home /path/to/jdk-8 \
  --keep-attempt

uv run python experiments/jdt-incremental-worker/run_a9_experiment.py \
  --worker-java-home /path/to/jdk-17 \
  --target-java-home /path/to/jdk-8 \
  --keep-attempt

uv run python experiments/jdt-incremental-worker/run_bootstrap_smoke.py \
  --worker-java-home /path/to/jdk-17 \
  --target-java-home /path/to/jdk-8 \
  --a10-path-boundary \
  --keep-attempt

uv run python experiments/jdt-incremental-worker/run_lombok_experiment.py \
  --worker-java-home /path/to/jdk-17 \
  --target-java-home /path/to/jdk-8 \
  --keep-attempt

# Expected javac-8 / locked ECJ-3.25 portability divergence:
uv run python experiments/jdt-incremental-worker/run_cross_compiler_compatibility.py \
  --worker-java-home /path/to/jdk-17 \
  --target-java-home /path/to/jdk-8

# Bounded current/anchor compatibility probe (select either lock):
uv run python experiments/jdt-incremental-worker/run_lombok_experiment.py \
  --lock experiments/jdt-incremental-worker/locks/eclipse-2021-03-lombok-anchor-diagnostics-v2.json \
  --worker-java-home /path/to/jdk-17 \
  --target-java-home /path/to/jdk-8 \
  --compatibility-probes-only \
  --keep-attempt

# Maven-native Probe spike (online/default file-repository injection):
uv run python experiments/jdt-incremental-worker/run_maven_probe_spike.py \
  --project-root /path/to/maven-project \
  --maven-executable /path/to/mvn \
  --settings-file /path/to/settings.xml \
  --local-repository /path/to/maven-local-repository \
  --java-home /path/to/jdk-8 \
  --keep-attempt

# Phase 2A real Maven module. Pass private_report_path from the Probe stdout;
# use the same Maven/JDK/settings/repository/profiles in both commands:
uv run python experiments/jdt-incremental-worker/run_real_maven_build_world.py \
  --project-path /path/to/maven-project \
  --maven-executable /path/to/mvn \
  --settings-file /path/to/settings.xml \
  --local-repository /path/to/maven-local-repository \
  --maven-probe-private-report /path/to/probe/report.private.json \
  --build-java-home /path/to/jdk-8 \
  --target-java-home /path/to/jdk-8 \
  --worker-java-home /path/to/locked-jdk-17 \
  --keep-attempt

# Strict offline requires the exact local repository Maven will use. joLink
# seeds only its own verified Probe coordinate before invoking Maven offline:
uv run python experiments/jdt-incremental-worker/run_maven_probe_spike.py \
  --project-root /path/to/maven-project \
  --maven-executable /path/to/mvn \
  --local-repository /path/to/maven-local-repository \
  --offline \
  --keep-attempt
```

Use the same Python entry points on Windows and pass JDK home directories
without appending `bin`. The A10 mode creates both attempt and source paths
with spaces and non-ASCII characters, then reports only boolean path facts so
absolute user paths are not published. Run it once on Windows and once on
POSIX; either run alone is only `passed_for_current_platform`. No Maven or
target application JVM is involved.

Artifacts and attempts stay outside the repository by default:

```text
~/.cache/jolink-runtime/jdt-poc/
    candidates/
    attempts/
    reports/
```

The candidate lock is committed; downloaded Eclipse JARs and attempt workspaces
are not.

## Historical A1-A8 evidence (previous Worker artifact)

On macOS a real run produced:

```text
JDT Core                  3.46.0.v20260520-1003
Java Builder count         1
full build                 3 source units / 3 changed classes
leaf incremental           Application.java only / Application.class only
no-op incremental          requested INCREMENTAL / outcome NO_COMPILE
no-op actual build kind    unavailable (no compilation callback observed)
no-op participant callbacks none; project.build returned and output unchanged
upstream method body       Api.java only / Api.class only
A4 vs clean-full oracle    exact
public API propagation     Api/Application/Service incrementally compiled
constant propagation       Api/Application/Service incrementally compiled
A5 vs clean-full oracles   exact for both independent edits
source deletion            complete six-file Legacy family removed
source/type rename         old family removed / complete new family emitted
A6 vs clean-full oracles   exact for both independent edits
broken generation          structured ERROR / non-publishable
same-Worker recovery       incremental / diagnostics cleared / publishable
A7 vs clean-full oracle    exact
workspace restart          saved / stopped / reopened in a new Worker
post-restart build         actual INCREMENTAL / Application.java only
A8 vs clean-full oracle    exact
Java 9 API negative        List.of rejected with an ERROR marker
target javac platform      20 advertised / 17 present / 3 absent placeholders
extension libraries        11 / runtime cross-check exact
osgi.ee requirements       23 / all satisfied and locked
instrumentation parity     exact
incremental vs clean-full  exact
class major                52
owned worker left behind   no
post-run input revalidation exact
report schema              v3 / no absolute user path
overall elapsed            about 4.8 seconds
```

Those are partial Phase 1A facts, not a complete Phase 1A Go decision.

## Historical A9 canonical evidence (previous Worker artifact)

On macOS, a clean committed worktree and its locked Worker artifact completed
the frozen A9 workload. The canonical report records the exact Git revision,
`dirty_worktree=false`, and successful post-run frozen-input revalidation.

```text
A9-S same Worker requests       121 / 121
measured requests               110 / 110
unique build generations        121 / 121
attempt-scoped oracle states    6 / all exact
process-tree sample coverage    100%
every build RSS sample count    at least 2
observed process-tree RSS peak  about 193 MiB
30-second idle RSS sum          about 54 MiB
A9-M decision                   PASS
explicit/deadline cancellation  BUILD_CANCELLED / recovered
STOP races                      completion-win and cancel-win passed
clean reopen + offline delta    actual INCREMENTAL / oracle exact
abnormal exit                   Runner BUILD_ABORTED / fresh lineage FULL exact
owned Worker residue            none
```

These are tiny-fixture implementation facts. They do not claim company-project,
Lombok, HotSwap, or production publication performance.

## Historical Phase 1B result (previous Worker artifact)

The Phase 1B runner is intentionally labelled `exploratory_non_canonical`
until the complete Phase 1A Go gate is recorded. On the current macOS
candidate it proves:

```text
Lombok                         1.18.20, exact locked SHA
integration                    -javaagent:lombok.jar=ECJ
Worker JVM opening             java.base/java.lang
target class major             52
@Data/@Builder/@NonNull/@Slf4j active
downstream generated members   compiled
method/field/annotation/consumer incrementals exact vs clean full
generated-member failure       rejected; recovery exact vs clean full
warm-up operations             10 / all oracle exact
measured mixed operations      100 (50 edits + 50 no-ops) / all oracle exact
owned Worker residue           none
```

This is not Phase 1B PASS. On JDT 3.46, bounded `lombok.config` lookup still
falls back to the default log field and `@Builder(toBuilder=true)` exposes a
Lombok 1.18.20 versus current ECJ internal-API incompatibility. READY evidence
shows that the Eclipse source URI and private filesystem source directory are
identical, so this is no longer described as a wrong physical-path mapping.
Windows evidence is also missing. The report preserves these findings instead
of weakening the successful core-transform evidence.

A compatibility-only dual probe now locks Eclipse 2021-03 / JDT Core
`3.25.0.v20210223-0522`. With the same JDK 17 binary, identical Worker JAR,
exact Lombok 1.18.20, target JDK 8 snapshot, and fixture, both
`lombok.config` and `@Builder(toBuilder=true)` pass on that anchor. This makes
the version trade-off concrete; it does not make the old anchor a product
choice. Its separate Phase 1A, resource/lifecycle, platform, maintenance, and
security gates remain open.

The promoted anchor has since completed one full macOS/POSIX candidate run.
On that exact artifact lineage A1-A8, A9-S/M/L, and the current-platform A10
spaces/non-ASCII path boundary passed. The complete Phase 1B workload also
passed with no blockers: `lombok.config` generated the configured field;
`@Builder(toBuilder=true)` compiled a downstream `toBuilder()` consumer with
the exact descriptor `()Lexample/LombokModel$LombokModelBuilder;`; every
successful state matched an independent same-stack clean-full class tree and
diagnostics oracle; and all 110 mixed warm-up/measured operations were
oracle-exact. Fixed-cadence process-tree sampling was complete and the Lombok
Worker resource decision was `PASS`, with a tiny-fixture peak below 256 MiB.

This promotes Eclipse 2021-03 from a dual-probe-only anchor to a successful
POSIX compatibility candidate. It is still not a product selection or final
Phase 1 Go: Windows A1-A10/Phase 1B evidence and the explicit
maintenance/security review remain external gates.

## Next implementation boundary

The diagnostics-v2 candidates must first receive fresh A1-A8 and Phase 2A
evidence. Their A9, A10, and Phase 1B gates remain pending and must not be
inferred from the historical sections above. The experiment may then proceed
with A10 Windows, spaces, and non-ASCII path evidence while the forced-shutdown
settlement P1 remains explicitly recorded.
Before a complete Phase 1A decision, deterministic lifecycle tests must prove
that a live-Worker EOF and a shutdown deadline expiry force-settle the exact
identity-bound process tree under one five-second budget and publish exactly
one Runner-owned `BUILD_ABORTED` terminal.

Phase 1B compatibility probes may continue only as non-canonical exploration.
Canonical Phase 1B evidence still requires the same exact evidence candidate
to pass the complete Phase 1A Go gate first.
