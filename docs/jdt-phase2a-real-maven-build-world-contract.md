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
the already validated Eclipse 2021-03 / JDT 3.25 candidate and its frozen
Java-8 project model. A non-Java-8 source/target model is rejected explicitly;
the runner must not silently compile it with Java 8 options.

## BuildWorldSnapshot v1

The private snapshot freezes:

- declared main source root;
- discovered generated source roots that contain Java input;
- compile-scope dependency entries and content fingerprints;
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

Lombok is handled only through the exact Phase 1B mechanism already validated
for the locked JDT 3.25 candidate: `-javaagent:<lombok>=ECJ` plus the required
Worker JVM module opening. This does not generalize support to MapStruct,
QueryDSL, Dagger, Hibernate enhancement, AspectJ, or arbitrary Processors.

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
