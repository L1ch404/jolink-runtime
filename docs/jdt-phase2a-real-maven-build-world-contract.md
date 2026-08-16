# JDT Phase 2A: Real Maven Build World Contract

Status: experiment contract implemented on `experiment/jdt-incremental-worker`.

Phase 2A answers one question only:

```text
Can one representative real Maven module be captured as a frozen Build World
and compiled successfully by the locked private JDT Java Builder?
```

It does not implement product Fast Compile, incremental mutations, HotSwap,
restart, HTTP verification, or a new MCP action.

## Stage boundary

```text
Phase 2A  Maven baseline -> BuildWorldSnapshot -> private JDT FULL
Phase 2B  real mutations -> JDT incremental -> JDT clean-full oracle
Phase 2C  committed changed classes -> HotSwap/restart -> Runtime evidence
```

Phase 2B and Phase 2C remain unauthorized by this contract. A Phase 2A PASS is
evidence for designing Phase 2B; it is not a production claim.

## Authoritative inputs

Maven remains the Build World authority. Phase 2A runs the selected Maven
installation, JDK, settings, local repository, profiles, reactor selection,
and `clean compile` before discovery. The effective POM and compile-scope
classpath are captured by bounded, supervised Maven operations. The raw Maven
log, effective POM, classpath file, absolute paths, source, settings, and
credentials remain in a mode-private attempt directory.

The first P0 supports one representative Java 8 module. It intentionally uses
the Eclipse 2021-03 / JDT 3.25 stack and its frozen Java-8 project model. The
historical anchor lock preserves the earlier Phase 1 evidence; the active
`diagnostics-v2` lock is a distinct Worker/protocol candidate and inherits no
A9, A10, or Phase 1B result until rerun. A non-Java-8 source/target model is
rejected explicitly; the runner must not silently compile it with Java 8
options.

## BuildWorldSnapshot v1

The private snapshot freezes:

- declared main source root;
- discovered generated source roots that contain Java input;
- compile-scope dependency entries and content fingerprints;
- content-verified classification of Maven-discovered classpath entries;
- target JDK system libraries;
- Maven source level, target level, and source encoding;
- effective configuration fingerprint;
- discovered annotation Processor service identities;
- Lombok version and configuration fingerprints when present;
- source/configuration content fingerprints.

The shareable summary contains counts and SHA-256 identities only. It must not
contain workspace paths, repository paths, dependency coordinates, source
contents, Maven log text, compiler diagnostic text, header/token values, or
settings content.

Maven classpath discovery is not treated as proof that every emitted path is
a Java binary input. Before the Worker starts, each entry is classified as:

```text
archive containing Java class files -> include when Maven type does not contradict
known classpath-capable Maven type   -> include (including resource-only JARs)
class directory                     -> include
other Reactor module output         -> include with Reactor provenance
recognized Maven project descriptor -> fingerprint and exclude
unknown file or entry               -> fail closed
```

ZIP compatibility alone does not prove that an artifact is a Java compiler
input; a sources JAR or arbitrary resource ZIP with no supporting Maven type
therefore fails closed.
Recognition of a Maven project descriptor is based on bounded XML content with
the expected model version, not a `.pom` filename suffix. Known non-binary
artifacts remain part of the Build World fingerprint and shareable counts, but
never enter the JDT raw classpath. Phase 2A currently combines the Maven
Dependency Plugin's compile-scope path output with its `dependency:list`
type/scope/path evidence and a bounded allowlist of classpath-capable artifact
types. Adopting Maven's direct `compileClasspathElements`/artifact-handler
projection remains the authoritative discovery improvement rather than a
reason to guess unknown file types.

## No stale self-output invariant

JDT compile classpath must not contain:

- the current module's Maven `target/classes`;
- the current module's `target/test-classes`;
- any entry below the current module's `target` directory;
- the current module's resolved repository JAR when identifiable;
- the private JDT output from this or an earlier generation.

Other reactor modules are dependency inputs and may be present. The report
must state:

```json
{
  "self_output_on_compile_classpath": false,
  "stale_candidate_output_on_classpath": false
}
```

If the invariant cannot be proved, Phase 2A stops before Worker startup.

## Generated-source provenance

Generated roots are classified as:

```text
BOOTSTRAP_GENERATED
COMPILE_TIME_AP_GENERATED
```

`BOOTSTRAP_GENERATED` is a Maven-produced input that JDT may consume after the
baseline. It will require Build World invalidation when its generator inputs
change.

`COMPILE_TIME_AP_GENERATED` can help Phase 2A explore FULL compilation, but it
blocks Phase 2B until refresh semantics are verified. Likewise, an unknown
compile-time annotation Processor may be observed in Phase 2A but sets:

```text
phase2b_incremental_eligible = false
```

Lombok is handled only through the exact mechanism previously validated on the
historical JDT 3.25 candidate: `-javaagent:<lombok>=ECJ` plus the required
Worker JVM module opening. Reusing that mechanism does not transfer the old
Phase 1B evidence to the diagnostics-v2 Worker. It also does not generalize
support to MapStruct, QueryDSL, Dagger, Hibernate enhancement, AspectJ, or
arbitrary Processors.

## Private materialization

All source roots are copied below one attempt-scoped Eclipse workspace. The
frozen Worker currently has one Java source entry, so roots are merged at the
Java package boundary. Different bytes at the same relative source path are a
hard `SOURCE_ROOT_COLLISION`; no implicit root precedence is guessed.

Source links/reparse points, source limits, configuration collisions, imported
Lombok config layouts that cannot be represented faithfully, and multiple
Lombok artifacts fail closed. JDT output remains outside the user project.

The formal Maven baseline is allowed to write the selected module's `target`
tree. After that baseline, the target fingerprint and project input
fingerprint must remain unchanged throughout JDT execution.

## Cross-compiler comparison

Maven/javac byte SHA equality with ECJ/JDT is neither required nor claimed.
Comparison parses class files without loading or initializing application
classes.

Tier 1 is a Phase 2A gate:

- source-declared top-level/member binary-name set;
- public/protected fields and methods;
- descriptors;
- superclass and interfaces;
- generic `Signature` metadata;
- runtime-visible annotations and related API metadata;
- class major version.

Tier 2 is recorded but is not a Phase 2A gate:

- synthetic and bridge members;
- anonymous/lambda/compiler helper classes;
- private compiler-generated members;
- debug metadata and byte layout.

The P0 source-declared classifier is conservative but heuristic. A structural
mismatch results in `REVIEW_REQUIRED`, not a false equivalence claim.

## Results

```text
phase2a_passed
    Maven baseline succeeded
    BuildWorldSnapshot froze successfully
    JDT FULL succeeded without errors
    Tier 1 structural comparison is compatible
    isolation gates passed

phase2a_passed_with_incremental_blockers
    Phase 2A FULL and structural gates passed
    Processor/generated-source refresh semantics still block Phase 2B

phase2a_jdt_full_failed
    Build World gap recorded with redacted diagnostic buckets

phase2a_structural_or_isolation_gap
    JDT compiled, but compatibility or isolation needs review
```

Known experiment outcomes return a report. Infrastructure/model failures use a
structured error and retain the private attempt. Raw diagnostics remain local;
the shareable report carries bucket counts and message fingerprints only.

Worker diagnostics use a bounded error-first projection. The protocol reports
the complete ERROR/WARNING/INFO counts, returned counts, truncation state, and
the `errors_first_then_warnings_then_info` selection policy. Up to 128 errors
are returned before a separate budget of 32 warnings/information markers, so a
warning-heavy project cannot hide the compile errors that explain failure.

Cross-compiler source incompatibility is distinct from a Build World gap. The
versioned `cross-compiler-compatibility` fixture records one Java-8 raw,
double-brace anonymous `ArrayList` expression that javac accepts with unchecked
warnings while the locked ECJ 3.25 compiler rejects its generic inference. Such
a result is source-portability evidence; joLink must not rewrite the source or
misreport it as a missing dependency. The dedicated
`run_cross_compiler_compatibility.py` probe gates that exact expected
divergence independently from normal Phase 2A PASS fixtures.

## Maven-native Probe migration experiment

The standalone Maven-native Probe spike is specified in
`maven-build-world-probe-contract.md`. It proves that a bundled normal Mojo can
export source roots, compile classpath, output identity, and live Reactor
outputs in the target Maven session without editing project POMs. It has also
passed Maven 3.3.9/JDK 8, `mirrorOf=*`, and explicit strict-offline injection.

Phase 2A now has an explicit hybrid entry: with a private Probe report, Probe is
authoritative for source roots, compile classpath, and reactor outputs, while
compiler/Processor configuration and artifact type still come from effective
POM/dependency metadata. Reports identify every provider and must not describe
this hybrid as a complete Maven compiler invocation. The legacy path remains
for regression/private differential evidence and must not silently become a
trusted fallback when Probe evidence is absent or conflicting.

## Phase 2A Go gate

Phase 2A can recommend Phase 2B design only when:

- the exact Maven baseline succeeds;
- the snapshot is frozen and sensitive-data gates pass;
- self and stale candidate outputs are absent from the JDT classpath;
- target-system libraries and Java 8 compiler model are verified;
- private JDT FULL succeeds;
- Tier 1 structural comparison is compatible;
- Maven target and project inputs remain unchanged after JDT;
- the Worker exits without owned-process residue.

A Processor blocker may still make Phase 2B ineligible even when the Phase 2A
FULL result is useful. No output from Phase 2A may be published to a target JVM.
