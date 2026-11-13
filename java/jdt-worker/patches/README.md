# JDT saved-state patch

This directory contains a modified Eclipse JDT `State.java`, licensed under
EPL-2.0. The original copyright and license header remain in the source file.
joLink changes are dated 2026-11-13.

## Exact upstream source

- JDT Core: `3.46.0.v20260520-1003` (Eclipse 4.40).
- Source archive: `org.eclipse.jdt.core.source_3.46.0.v20260520-1003.jar`.
- Source archive SHA256:
  `e586425cbc8846de25ad3389c8298674e09ca4114c7aa8541e6c469855ffd1c4`.
- Original entry: `org/eclipse/jdt/internal/core/builder/State.java`.
- Repository: <https://download.eclipse.org/eclipse/updates/4.40/R-4.40-202606010713>.

## Changes

1. Distinguish null module limits (`-1`) from an empty no-limit set (`0`),
   restoring `ClasspathJrt.NO_LIMIT_MODULES` for the latter. JDT uses that
   singleton when selecting all system modules.
2. Read every serialized `AddReads` entry, preserving order and duplicates.
   The upstream writer already writes all entries, but its reader consumes one.
3. Advance `State.VERSION` from `0x27` to `0x28`. Existing state is rejected by
   JDT's normal version check and rebuilt once, never guessed or partly read.

The rest of the upstream codec is unchanged; this is not a replacement builder
or a claim that every possible JDT state/configuration combination is covered.

## Packaging and activation

`scripts/build_jdt_worker.py` compiles this class separately against the locked
JDT bundles. The Worker ships its bytecode, this source, this README and the EPL
license under `META-INF/jolink/patches/`. The class is **not** on the Worker's
normal classpath.

`StatePatchActivator` installs a single-target OSGi weaving hook, loads State
through the JDT bundle, confirms replacement, then unregisters the hook.
The original downloaded Eclipse JAR and its signature are not modified.
The replacement class belongs to the original JDT classloader/package, so
package-private builder access is preserved. There is no per-build hook cost.

When upgrading the locked JDT version, compare this source with upstream and
remove the patch if both fixes are present. Re-run the codec regression and
real Worker/MCP cold, saved-state reopen, incremental and recovery tests.
