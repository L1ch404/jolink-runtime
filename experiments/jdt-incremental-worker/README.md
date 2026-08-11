# Headless JDT Incremental Worker Experiment

Status: `Phase 1A A1-A6 partial evidence passed; complete gate remains open`

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
    source/type rename builds;
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
19. stop all owned Equinox workers.

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
status          = phase_1a_a1_a2_a3_a4_a5_a6_evidence_passed
evidence_status = partial_phase_1a_evidence_a1_a2_a3_a4_a5_a6
```

This is real evidence for A1-A6, not a Phase 1A Go decision. A7 through A10,
resource/RSS measurement, cancellation pressure, workspace restart, and
the Windows run are still outstanding.

## Files

```text
candidate-bootstrap.json
    fixed repository, roots, and bootstrap identity

bootstrap_candidate.py
    p2 metadata parser, capability resolver, artifact downloader, lock writer

locks/eclipse-4.40-current.json
    exact bundle URLs, versions, SHA-256, licenses, sizes, start policy,
    Worker artifact/config identity

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

fixtures/java9-api-negative/
    companion source proving the Java 8 platform rejects List.of

run_bootstrap_smoke.py
    private target-library capture, Worker launch, full/incremental/no-op,
    instrumentation parity, clean-full oracle, and bounded shutdown
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
```

Use the same Python entry points on Windows and pass JDK home directories
without appending `bin`. No Maven or target application JVM is involved.

Artifacts and attempts stay outside the repository by default:

```text
~/.cache/jolink-runtime/jdt-poc/
    candidates/
    attempts/
    reports/
```

The candidate lock is committed; downloaded Eclipse JARs and attempt workspaces
are not.

## Latest observed A1-A6 evidence

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

## Next implementation boundary

The next work should remain inside this experiment and add, in order:

1. A7 diagnostics/recovery;
2. A8 workspace restart;
3. A9 repeated-build/resource stability;
4. A10 Windows, spaces, and non-ASCII paths.

Phase 1B Lombok work must not begin until the same exact evidence candidate
passes the complete Phase 1A Go gate.
