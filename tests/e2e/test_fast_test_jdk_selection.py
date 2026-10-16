"""Real MCP: separate build/test JVMs, persistent reuse and per-run precedence."""

import json
import os
import shutil
from pathlib import Path

import anyio
import pytest
from java_support import open_mcp_session, temporary_stderr
from test_application_java_home import setup
from test_build_configuration_mcp import project_fixture
from test_test_execution_config import run


def fixture(tmp_path, system, multi, configured_java):
    root, app, config, source, _ = project_fixture(
        tmp_path, system, multi if system == "maven" else False
    )
    shutil.rmtree(root / ".idea")
    if system == "gradle" and multi:
        app = root / "app"
        app.mkdir()
        for name in ("src", "resourcesA", "resourcesB", "build.gradle"):
            (root / name).rename(app / name)
        config = app / "build.gradle"
        source = app / "src/main/java/example/App.java"
        (root / "settings.gradle").write_text(
            "rootProject.name='jdk-test'; include 'app', 'lib'\n"
        )
        lib = root / "lib"
        lib.mkdir()
        (lib / "build.gradle").write_text(
            "plugins {id 'java-library'}; sourceCompatibility='8'; targetCompatibility='8'\n"
        )
        library = lib / "src/main/java/example/Library.java"
        library.parent.mkdir(parents=True)
        library.write_text(
            "package example; public class Library {public static final int VALUE=42;}"
        )
        config.write_text(
            config.read_text() + "dependencies {implementation project(':lib')}\n"
        )
    source.write_text(
        "package example; public class App {public static int value(){return "
        + ("Library.VALUE" if multi else "42")
        + ";}}"
    )
    if system == "maven":
        config.write_text(
            config.read_text()
            .replace("<version>3.13.0</version>", "<version>3.11.0</version>")
            .replace(
                "<directory>${resource.folder}</directory>",
                "<directory>${resource.folder}</directory><filtering>true</filtering>",
            )
            .replace(
                "<configuration><systemPropertyVariables>",
                (
                    f"<configuration><jvm>{configured_java}</jvm><systemPropertyVariables>"
                    if configured_java
                    else "<configuration><systemPropertyVariables>"
                ),
            )
        )
        (app / "resourcesA/build-jdk.txt").write_text("${java.home}")
    else:
        (app / "resourcesA/build-jdk.txt").write_text("replace")
        config.write_text(
            config.read_text()
            + "\nprocessResources { inputs.property('buildJdk', System.getProperty('java.home')); filesMatching('build-jdk.txt') { filter { line -> System.getProperty('java.home') } } }\n"
            + (
                f"test {{ executable={json.dumps(configured_java)} }}\n"
                if configured_java
                else ""
            )
            + "tasks.withType(JavaCompile).configureEach {doFirst {throw new GradleException('native compile must not run')}}\n"
            + "test.doFirst {throw new GradleException('native test must not run')}\n"
        )
    oracle = root / "expected.json-lines"
    test = app / "src/test/java/example/ReadyTest.java"
    test.write_text(
        """package example;
public class ReadyTest {
 static String home(String value) throws Exception {
  java.io.File f=new java.io.File(value).getCanonicalFile();
  return (f.getName().equals("jre")?f.getParentFile():f).getPath();
 }
 @org.junit.Test public void selectedJdks() throws Exception {
  java.util.List<String> expected=java.nio.file.Files.readAllLines(java.nio.file.Paths.get(ORACLE),java.nio.charset.StandardCharsets.UTF_8);
  org.junit.Assert.assertEquals(home(expected.get(0)),home(System.getProperty("java.home")));
  try(java.util.Scanner scanner=new java.util.Scanner(App.class.getResourceAsStream("/build-jdk.txt"),"UTF-8")) {
   org.junit.Assert.assertEquals(home(expected.get(1)),home(scanner.useDelimiter("\\\\A").next().trim()));
  }
  org.junit.Assert.assertEquals(Integer.parseInt(expected.get(2)), App.value());
  System.out.println("test-jdk="+System.getProperty("java.specification.version"));
 }
}""".replace("ORACLE", json.dumps(oracle.as_posix())),
        encoding="utf-8",
    )
    return root, source, oracle


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize(
    "system,multi",
    [("maven", False), ("maven", True), ("gradle", False), ("gradle", True)],
)
@pytest.mark.parametrize("explicit_project_jvm", [False, True])
def test_separate_build_and_test_jdks_reuse_and_reopen(
    tmp_path, system, multi, explicit_project_jvm
):
    homes, env = setup(tmp_path)
    suffix = ".exe" if os.name == "nt" else ""
    native_java = (
        str(Path(homes[17]) / f"bin/java{suffix}") if explicit_project_jvm else ""
    )
    root, source, oracle = fixture(tmp_path, system, multi, native_java)
    cache = tmp_path / "cache"
    evidence = []

    def classes():
        return {str(p): p.stat().st_mtime_ns for p in cache.rglob("*.class")}

    def world():
        files = list(cache.rglob("build-world.json"))
        assert len(files) == 1, files
        return files[0]

    async def verify(session, build, test=None, *, value=42, legacy=False):
        expected_test = test or (17 if explicit_project_jvm else build)
        oracle.write_text(
            f"{homes[expected_test]}\n{homes[build]}\n{value}\n", encoding="utf-8"
        )
        args = {} if legacy else {"build_java_home": homes[build]}
        if test:
            args["java_home"] = homes[test]
        result = await run(session, root, ["example.ReadyTest"], **args)
        evidence.append(result)
        assert result.get("passed") and result["tests"] == 1, result
        assert "ignored_arguments" not in result
        expected_version = "1.8" if expected_test == 8 else str(expected_test)
        assert "test-jdk=" + expected_version in result["output"], result
        assert (
            Path(json.loads(world().read_text())["build_jdk"]["home"]).resolve()
            == Path(homes[build]).resolve()
        )
        return result

    async def scenario():
        with temporary_stderr() as log:
            try:
                async with open_mcp_session(log, environment=env) as session:
                    await verify(session, 17, 8)
                    stamps = classes()
                    model = world().read_bytes()
                    assert stamps
                    for version in [17, *([21] if 21 in homes else []), None, 8]:
                        result = await verify(session, 17, version)
                        assert (
                            result["compiled_source_count"] == 0
                            and "freshness_ms" in result
                        ), result
                        assert classes() == stamps and world().read_bytes() == model
                async with open_mcp_session(log, environment=env) as session:
                    # Change only the Runner JDK across MCP process lifetimes.
                    result = await verify(session, 17, 17)
                    assert (
                        result["compiled_source_count"] == 0 and classes() == stamps
                    ), result
                    result = await verify(session, 17)
                    assert result["compiled_source_count"] == 0 and classes() == stamps
                    source.write_text(
                        source.read_text().replace("return ", "return 1+")
                    )
                    result = await verify(session, 17, 8, value=43)
                    assert result["compiled_source_count"] > 0, result
                    # Change the actual build JDK: re-export resources/model, no stale reuse.
                    await verify(session, 8, 8, value=43)
                    await verify(session, 8, value=43)
                    # Legacy one-parameter calls still choose the preparation JDK too.
                    result = await verify(session, 8, 8, value=43, legacy=True)
                    assert (
                        result["compiled_source_count"] == 0
                        and "freshness_ms" in result
                    )
                    # Neither explicit bad path may silently fall back to another JDK.
                    for key in ("java_home", "build_java_home"):
                        args = {
                            "java_home": homes[8],
                            "build_java_home": homes[8],
                            key: str(tmp_path / "missing JDK"),
                        }
                        bad = await run(session, root, ["example.ReadyTest"], **args)
                        evidence.append(bad)
                        assert bad.get("ok") is False and bad.get("passed") is False, (
                            bad
                        )
                        await verify(session, 8, 8, value=43)
                    # Both omitted: no sticky values from previous calls.
                    await verify(session, 17, value=43, legacy=True)
            finally:
                log.seek(0)
                (tmp_path / "mcp.log").write_text(log.read(), encoding="utf-8")
                (tmp_path / "results.json").write_text(
                    json.dumps(evidence, indent=2), encoding="utf-8"
                )

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("system", ["maven", "gradle"])
def test_incompatible_test_jvm_fails_then_reuses_compiled_outputs(tmp_path, system):
    homes, env = setup(tmp_path)
    root, _, config, _, _ = project_fixture(tmp_path, system, False)
    shutil.rmtree(root / ".idea")
    if system == "maven":
        config.write_text(
            config.read_text()
            .replace("<version>3.13.0</version>", "<version>3.11.0</version>")
            .replace("<maven.compiler.source>8<", "<maven.compiler.source>17<")
            .replace("<maven.compiler.target>8<", "<maven.compiler.target>17<")
        )
    else:
        config.write_text(
            config.read_text().replace(
                "sourceCompatibility='8'; targetCompatibility='8'",
                "sourceCompatibility='17'; targetCompatibility='17'",
            )
        )

    async def scenario():
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                failed = await run(
                    session,
                    root,
                    ["example.ReadyTest"],
                    build_java_home=homes[17],
                    java_home=homes[8],
                )
                assert failed.get("ok") is False and failed.get("passed") is False, (
                    failed
                )
                assert "UnsupportedClassVersionError" in json.dumps(failed), failed
                before = {
                    str(p): p.stat().st_mtime_ns
                    for p in (tmp_path / "cache").rglob("*.class")
                }
                assert before
                recovered = await run(
                    session,
                    root,
                    ["example.ReadyTest"],
                    build_java_home=homes[17],
                    java_home=homes[17],
                )
                assert (
                    recovered.get("passed") and recovered["compiled_source_count"] == 0
                ), recovered
                assert before == {
                    str(p): p.stat().st_mtime_ns
                    for p in (tmp_path / "cache").rglob("*.class")
                }

    anyio.run(scenario)
