# Third-party notices and corresponding sources

joLink's own code is licensed under the [MIT License](LICENSE), with the existing
Nous Research and joLink contributors copyright notices retained. That license
does not replace the separate licenses of the third-party components below.
This inventory was reviewed on 2026-09-21.

## What joLink distributes

The Python package contains joLink's JDT Worker, Maven Probe, Gradle Probe and
Test Runner JARs. Those JARs contain joLink classes; they do not embed the JDT,
Maven, Gradle or JUnit implementations. Their source is in `java/` in the source
distribution and the [joLink repository](https://github.com/L1ch404/jolink-runtime).
The repository's Runtime lineage records the MIT-licensed Hermes origin at
commit `cc726310c7d9d7981ef3f0bf9e2d27513d0c9515`.

The Eclipse bundles and Temurin JDK are separate, unmodified upstream runtime
distributions, downloaded when needed. Their original `about.html`, `NOTICE`,
`LICENSE` and JDK `legal/` content is retained. Additional copies of applicable
notices and license texts accompany this package in [licenses/](licenses/README.md).

| Component | Applicable terms and notices |
| --- | --- |
| Eclipse JDT/ECJ, APT, Equinox and Platform components | [EPL-2.0](licenses/common/EPL-2.0.txt), with the additional original notices in `licenses/eclipse/` |
| Apache Felix SCR, Apache Ant and the OSGi API bundles | [Apache-2.0](licenses/common/Apache-2.0.txt) and their original LICENSE/NOTICE files |
| JNA 5.18.1 | Upstream offers [Apache-2.0 OR LGPL-2.1-or-later](licenses/jna/LICENSE). joLink uses the Apache-2.0 option; the original dual-license notice is retained. |
| libffi used by JNA's native dispatch libraries | Its separate [MIT-style license and copyright](licenses/jna/libffi-LICENSE) remain applicable. |
| Java Mirror API included in Eclipse APT | The original Sun Microsystems BSD-style [mirror-api-license.txt](licenses/eclipse/org.eclipse.jdt.apt.core/mirror-api-license.txt) remains applicable. |
| Apache/OSGi material within Eclipse bundles | See the original `about.html` and `about_files/` notices for the individual bundle; EPL is not a replacement for these terms. |
| Eclipse Temurin 21.0.12.1+1 / OpenJDK | [GPLv2 with the Classpath Exception](licenses/openjdk/LICENSE), [additional licensing information](licenses/openjdk/ADDITIONAL_LICENSE_INFO), [Assembly Exception](licenses/openjdk/ASSEMBLY_EXCEPTION), and the other original JDK `legal/` notices |
| Temurin build scripts supplied with the corresponding source bundle | [Apache-2.0](licenses/temurin-build/LICENSE) |

## Corresponding source code

The source of each EPL-covered Eclipse component is available under EPL-2.0,
with the component-specific third-party terms retained. OpenJDK source is
available under its original GPLv2/Classpath Exception and other applicable
terms. These rights are not restricted by joLink's MIT license.

[runtime-sources.json](licenses/runtime-sources.json) maps every pinned runtime
binary to its exact source archive, version, checksum and upstream source
reference. The same index records the JDK release metadata and pinned Temurin
build-script commit. It is a distribution record, not an input to application
compilation or runtime cache selection.

Sources can be obtained from the official URLs in that index. Copies published
on the [joLink mirror](https://7355608.net/jolink/assets/README.txt) are listed in
its manifest; mirror paths in the index describe the layout, not a substitute
for checking that a release has actually been deployed. The complete
Temurin source archive is
[OpenJDK21U-jdk-sources_21.0.12.1_1.tar.gz](https://7355608.net/jolink/assets/jdk/temurin-21.0.12.1+1/OpenJDK21U-jdk-sources_21.0.12.1_1.tar.gz),
not just the partial `lib/src.zip` contained in a JDK installation.

`scripts/prepare_jdt_worker.py --offline-bundle ...` produces a runtime archive
and a companion source archive. Make both available together when distributing
an offline kit, particularly when transferring it without network access.
The sources are not required to run joLink and are not downloaded during a
normal application launch, test or restart.

## Python dependencies

Python dependencies are installed as their own upstream packages, not relicensed
or combined into joLink's JARs. The direct runtime dependencies are:

| Package | License |
| --- | --- |
| anyio, mcp, jsonschema | MIT |
| httpx, psutil | BSD-3-Clause |

The reviewed installation also contains dependencies under other terms,
including certifi (MPL-2.0), cryptography (Apache-2.0 OR BSD-3-Clause), cffi
(MIT-0) and typing_extensions (PSF-2.0). The [reference inventory](licenses/python-dependencies.json)
lists the reviewed versions and original notices copied into `licenses/python/`.
It is not a promise that every future `uvx` resolution has identical transitive
versions. The installed upstream distributions' own notices remain authoritative.

Windows also uses colorama and pywin32. **pywin32 is a multi-license distribution:**
among other notices, its `adodbapi` component carries LGPL-2.1. Do not summarize
the whole package as BSD or remove its component-specific license files.
The reviewed Windows wheel's original notices are included under `licenses/python/pywin32/`.

If redistributing a complete Python environment or wheelhouse, preserve the
actual selected wheels and their legal materials and source availability;
this reference inventory does not substitute for identifying that environment's
actual components. The joLink runtime resource archive is not a complete Python
offline installer.

## Project-provided tools and libraries

The user's Maven/Gradle installation, JUnit/TestNG engines, Lombok and other
annotation processors are loaded from the user's selected project/toolchain.
They are not embedded in joLink's four JARs. Their licenses remain applicable
to their own use and distribution; joLink does not acquire or grant rights to
the user's project code or generated outputs.

## Preservation and attribution

Copies under `licenses/` preserve the upstream texts and copyright notices.
[materials.json](licenses/materials.json) records their origin and checksums.
Do not replace these with generic license templates that omit the original
copyright holders. Third-party names identify their software; no endorsement
by those projects or their contributors is implied.

If distributing a joLink JAR separately from its Python/source distribution,
include joLink's MIT License and applicable third-party notices with that copy.
