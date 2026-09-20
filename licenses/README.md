# License materials

See [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) for scope and attribution.

- `common/`: unmodified license texts from the license stewards.
- `eclipse/`: original notices extracted from the exact pinned Eclipse/OSGi/Apache/JNA bundles.
- `jna/`: original JNA dual-license notice and libffi's separate MIT-style notice.
- `openjdk/`: original legal documents from the fixed Temurin source distribution.
- `temurin-build/`: license of the exact build-script revision supplied with the source kit.
- `python/`: original notices from the reviewed Python distributions, including Windows-specific wheels.
- `runtime-sources.json`: versioned binary/source mapping, official URLs, mirror paths and checksums.
- `python-dependencies.json`: a reference installation, not a replacement for the actual user's dependency resolution.
- `materials.json`: origin and checksum of the copied legal documents.

These files are release materials. They do not add startup checks, change the
compiler's cache identity, or require users to download sources during normal
development. Preserve them when redistributing the package or an offline kit.

For a dependency upgrade, collect its original notices and exact source records,
update the matching inventory, build wheel/sdist and inspect the archives. Keep
the original runtime artifacts intact; do not edit an upstream license text.
