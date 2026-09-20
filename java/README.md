# Product Java components

These are the sources of the Java artifacts shipped with joLink, not experimental
implementations. They are included in the source distribution. Runtime Python
code loads the bundled artifacts from `src/jolink_runtime/launch/`.

| Source | Bundled asset | Build entry point |
| --- | --- | --- |
| `jdt-worker/` | `jdt-product-worker.jar.b64` | `scripts/build_jdt_worker.py` |
| `maven-probe/` | `maven-build-world-probe.jar.b64` and `.pom` | `scripts/build_fast_test_assets.py` |
| `gradle-probe/` | `gradle-build-world-probe.jar.b64` and `gradle-init.gradle` | `scripts/build_gradle_probe_assets.py` |
| `test-runner/` | `fast-test-runner.jar.b64` | `scripts/build_fast_test_assets.py` |

Run build scripts from the repository/source-distribution root. To rebuild all
artifacts and the wheel:

```sh
uv run python scripts/build_jdt_worker_release.py \
  --java-home /path/to/jdk8 \
  --maven /path/to/mvn \
  --gradle /path/to/gradle
```

To package the already bundled artifacts, `uv build --no-sources --clear` needs
no Java rebuild. The source rebuild above expects the locked Eclipse bundles
already prepared under `--cache-root` (default `~/.cache/jolink-runtime/jdt-poc`);
it fails explicitly if those build inputs are missing. That is a maintainer
build cache, not the end-user installation flow.

The product Worker runs with the managed Temurin 21 JDK; pass
`--worker-java-home` to select an existing compatible build JDK. Probe and Runner
artifacts retain Java 8 bytecode so they can run with the user's build/test JDK.
The Worker build uses the frozen bundle closure in
`jdt-worker/locks/eclipse-4.40-product.json`; it does not resolve new dependencies
or upgrade JDT implicitly. Existing bundle caches and workspace locations are
unchanged by the source relocation.

For an intentional dependency upgrade, `scripts/resolve_jdt_dependencies.py`
accepts an explicit `--bootstrap` discovery JSON and `--lock` output path.
The input specifies `candidate_id`, `repository_url`, `worker_java_minimum`,
and `root_installable_units` (the current roots are in the product lock).
Resolve a new closure separately, review it, and only then rebuild the product.
This maintenance tool is not called during an application launch.

`scripts/verify_distribution.py` runs against an **installed** wheel or sdist,
without importing the source checkout. It verifies all four public MCP tools,
owned/external JVM debugging, JDT project launch, HTTP-observed HotSwap/restart,
and selected tests before and after a source change. Java, javac and Maven must
be available; the first run may download normal Maven/JDT/JDK dependencies.

See [current documentation](../docs/README.md) and
[historical research records](../docs/archive/README.md).
