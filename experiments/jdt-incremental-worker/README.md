# Headless JDT Incremental Worker Experiment

Status: `Phase 1A bootstrap implemented; evidence gate not yet open`

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
3. fail closed if a selected unit has an unsupported mandatory capability;
4. download and hash only the resolved bundles;
5. derive bundle license identity from signed bundle content;
6. compile a small joLink Equinox application without Maven;
7. launch it in a private configuration/workspace with JDT Core
   `3.46.0.v20260520-1003`;
8. configure exactly one real `org.eclipse.jdt.core.javabuilder`;
9. use an ordered system-library view captured by a helper running under the
   target JDK 8;
10. run full, leaf incremental, and no-op incremental builds;
11. observe actual batch/incremental behavior and compiled source units through
    a read-only `CompilationParticipant`;
12. prove instrumentation OFF/ON output parity;
13. compare the leaf incremental output with a separate clean-full oracle; and
14. stop all owned Equinox workers.

The current closure is 23 Eclipse/OSGi bundles plus the joLink worker bundle.
The locked bundle bytes total 16,096,227 bytes (about 15.4 MiB). It does not
include JDT LS, M2E, Buildship, SWT, JDT UI, Eclipse UI, or debug bundles.

## Why this is not Phase 1A evidence yet

The local Zulu JDK 8 used for the first macOS smoke advertises three absent
entries in `sun.boot.class.path`. The contract says missing system-library
entries invalidate an evidence generation. The smoke records and skips those
absent placeholders so the Worker architecture can be exercised, but it
explicitly reports:

```text
evidence_status = not_phase_1a_evidence
```

A4 through A10, resource/RSS measurement, cancellation pressure, workspace
restart, and the Windows run are also still outstanding.

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
    Java 8 helper that captures the active sun.boot.class.path in JVM order

fixtures/plain-java/
    tiny Maven-free Phase 1A fixture

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

## First observed smoke result

On macOS a real run produced:

```text
JDT Core                  3.46.0.v20260520-1003
Java Builder count         1
full build                 3 source units / 3 changed classes
leaf incremental           Application.java only / Application.class only
no-op incremental          0 source units / 0 changed classes
instrumentation parity     exact
incremental vs clean-full  exact
class major                52
owned worker left behind   no
overall elapsed            about 3.2 seconds
```

Those are bootstrap facts, not a Phase 1A Go decision.

## Next implementation boundary

The next work should remain inside this experiment and add, in order:

1. an admissible exact JDK 8 `TargetSystemLibrarySnapshot`;
2. A4 upstream body and A5 dependency/constant propagation;
3. A6 delete/rename and stale class-family cleanup;
4. A7 diagnostics/recovery;
5. A8 workspace restart;
6. A9 repeated-build/resource stability;
7. A10 Windows, spaces, and non-ASCII paths.

Phase 1B Lombok work must not begin until the same exact evidence candidate
passes the complete Phase 1A Go gate.
