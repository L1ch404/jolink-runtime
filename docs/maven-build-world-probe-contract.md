# Maven-native Build World Probe Contract

Status: an experimental spike exists on `experiment/jdt-incremental-worker`,
including an explicit private-Probe-report to Phase 2A JDT FULL hybrid path.
It is not a public Runtime/MCP action.

## Question answered by the spike

The Probe tests a narrower architecture than the legacy Python-side Maven
reconstruction:

```text
the project's own Maven invocation
        ↓
normal joLink Maven Mojo in the same reactor/session
        ↓
MavenProject / MavenSession facts
        ↓
private, versioned Build World input
```

The objective is to obtain Maven-native facts instead of teaching joLink every
possible POM shape or plugin convention. Maven remains responsible for model
building, inheritance, profiles, artifact handlers, dependency resolution,
reactor ordering, and lifecycle execution. The Probe only exports bounded facts
that Maven has already resolved.

This spike does **not** claim that all Maven compile semantics are captured yet.
In particular, the current schema is deliberately small and is not yet a full
replacement for `BuildWorldSnapshot`.

## Injection contract

The Probe is a normal Maven Plugin/Mojo, not a Core Extension. It has Maven
coordinates and is invoked after the selected lifecycle operation:

```text
mvn compile io.jolink:jolink-maven-probe:<version>:export-build-world
```

The release design bundles the prebuilt Probe artifact with joLink. The current
spike builds it from source only to validate the artifact. That source build
uses the selected Maven user settings so company mirrors can resolve pinned
Probe build plugins, but does not forward target command-line profiles. Logs
and settings remain private evidence.

The default injection path is:

```text
bundled Probe JAR + POM
        ↓
content-addressed Maven2-layout file repository under joLink cache
        ↓
attempt-private settings.xml adds one pluginRepository
        ↓
exact fully-qualified Probe goal
```

The target project is never edited:

- no POM modification;
- no `.mvn` modification;
- no source modification;
- no modification of the user's settings file;
- the POM tree is fingerprinted before and after the invocation.

When a source settings file uses a wildcard `mirrorOf=*`, the private copy adds
an exclusion for the content-addressed Probe repository ID:

```text
*,!jolink-local-probe-<jar-sha-prefix>
```

This preserves the user's mirror for all other repositories while allowing the
Probe to resolve from joLink's local `file://` repository. The source settings
bytes remain unchanged. The temporary copy is private evidence and must never
be included in a shareable report. Without an explicit settings path, Maven's
default `~/.m2/settings.xml` semantics are preserved. The credential-bearing
copy is deleted immediately after Maven returns, including with
`--keep-attempt`.

### Strict offline behavior

Maven `--offline` does not consult a `file://` remote/plugin repository when the
artifact has not previously been cached. Therefore strict offline execution
uses an explicit fallback:

```text
bundled Probe artifact
        ↓
verify content hashes and coordinate collision
        ↓
seed io/jolink/jolink-maven-probe into the explicitly selected localRepository
        ↓
run Maven --offline
```

This is a bounded Maven-cache write, not a zero-footprint operation. It must be
reported as `offline_probe_seeded=true`. The spike requires an explicit
`--local-repository` for strict offline mode so it never guesses which user
repository may be modified. A coordinate collision fails closed.

Online/default file-repository resolution may also cause Maven itself to cache
the Probe in its selected local repository, exactly as it caches any plugin.
Product documentation must not promise that the injection leaves the Maven
local repository byte-for-byte unchanged.

`-Dmaven.ext.class.path` is out of scope: loading joLink as a Maven Core
Extension would increase classloader/API compatibility risk without providing
evidence needed by this Probe.

## Probe dependency boundary

The runtime Probe artifact intentionally has no bundled third-party library.
It uses Maven-provided APIs with `provided` scope and a small internal JSON
writer. The initial compatibility floor is Maven 3.3.9 and Java 8 bytecode.

The source plugin build uses explicit compiler, descriptor, and JAR goals rather
than the full `package` lifecycle. This avoids old Maven Super-POM resource and
Surefire defaults during the development-only Probe build. `--offline` is now
also propagated to this source bootstrap, so an offline target invocation does
not silently perform an online Probe build.

## Exported schema v1

One private JSON document is emitted per reactor project:

```text
schema / probe version / Probe implementation identity
project coordinates, packaging, and base directory
requested Maven goals
compile source roots
compile classpath elements
formal output directory
reactor project identities and output directories
annotation-processing discovery mode
Processor-service artifact paths discovered on the implicit compile classpath
Processor provider names
`-A` options and explicitly selected Processor names
explicit annotationProcessorPaths declaration count
```

The initial Processor-aware implementation used historical `spike2`; the
current reproducible boundary with effective factory-path and legacy-option
guards uses the separate `0.1.0-spike6` coordinate. The
currently verified path reports
`IMPLICIT_COMPILE_CLASSPATH`, exact provider-bearing artifact paths, providers,
and options. An effective compiler configuration with explicit
`annotationProcessorPaths` is reported as `EXPLICIT_DECLARED_UNRESOLVED` rather
than being misrepresented as a resolved path.

`processorProviderArtifactPaths` proves where Processor providers are declared;
it is not a claim that their complete runtime dependency closure has been
resolved. Non-empty options, explicit Processor names, execution-level
Processor configuration, `proc=only`, and directory providers currently fail
closed at the APT runner boundary. Plugin-level legacy
`<compilerArguments><A...>` options, `maven.compiler.proc` properties, and raw
processor-control compiler arguments are detected separately and rejected.
Provider artifact ordering preserves Maven's compile-classpath order.

These are private facts. Absolute paths, coordinates, and the settings copy do
not enter the shareable report. Every snapshot must echo an implementation
identity derived from Probe source/POM, preventing a fixed GAV from silently
executing stale locally cached plugin bytes. The shareable report contains only
counts, artifact/implementation/JDK/Maven fingerprints, timing,
mirror-adjustment count, offline seed state, and project-mutation gates.

The current Probe still needs later schema work before becoming the sole
BuildWorld authority, including complete compiler options, explicit Processor
artifact resolution, artifact-handler provenance, generated-source provenance,
resources, toolchains, and exact configuration fingerprints.

## Authority and fallback rules

During the current migration step:

- Maven-native Probe output is experimental evidence.
- With an explicit private Probe report, Phase 2A treats source roots, compile
  classpath, and reactor outputs as Probe-authoritative.
- Compiler/Processor configuration and artifact-type provenance still come
  from effective-POM/dependency metadata, and reports mark the hybrid model.
- The no-Probe Phase 2A path remains only for regression/differential evidence.
- Legacy discovery may be used later as comparison evidence, but must not be a
  silent trusted fallback when Probe evidence is missing or contradictory.
- A malformed/missing Probe document, repository collision, unsupported
  settings document, or Maven failure is a structured failure.
- Probe success does not authorize JDT publication, HotSwap, or Phase 2B.

The migration gate for a future integration is:

```text
same selected Maven/JDK/settings/profile/local-repository inputs
        ↓
Probe facts and legacy facts compared privately
        ↓
all differences classified
        ↓
only then replace individual legacy facts with Probe authority
```

## Evidence obtained by the spike

The following real executions passed on 2026-08-16:

```text
Maven 3.9.11 + modern host JDK
  single module                       PASS
  two-module reactor                  PASS
  settings with mirrorOf=*            PASS
  first strict-offline invocation     PASS after explicit local-repo seed

Maven 3.3.9 + JDK 8u332
  single module                       PASS
  two-module reactor                  PASS
```

The reactor fixture intentionally keeps the upstream SNAPSHOT uninstalled. The
app Probe document contains the sibling module's current `target/classes`,
proving that the exported compile classpath reflects the live reactor rather
than requiring a stale local-repository JAR.

All executions preserved the project POM fingerprint. The initial schema was
consumed successfully from a normal Mojo running in the target Maven session.

## Remaining experiment gates

Before product integration, test at least:

- Windows paths with spaces and non-ASCII characters;
- the company Maven 3.3.9/JDK 8/settings/local-repository environment;
- authenticated mirrors, proxies, encrypted credentials, and multiple
  `mirrorOf` expressions;
- selected profiles and partial reactor commands (`-pl/-am`);
- Maven toolchains and Maven Wrapper selection;
- Maven 4 behavior;
- cancellation/process-tree cleanup and bounded output on a slow build;
- schema expansion followed by private differential comparison against current
  Phase 2A discovery.

No new public action, automatic fallback, or product claim is approved by this
contract.
