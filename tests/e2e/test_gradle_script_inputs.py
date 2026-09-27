"""Actual Gradle-applied scripts, not guessed apply syntax, invalidate cached worlds."""

import json
import os
from pathlib import Path

import anyio
import pytest
from java_support import (
    open_mcp_session,
    require_real_mcp_java_e2e,
    reserve_local_port,
    temporary_stderr,
)
from test_build_configuration_mcp import project_fixture
from test_review_model_cache import response


def script_body(value, kotlin=False):
    if kotlin:
        arguments = 'listOf("-parameters")' if value == "B" else "emptyList()"
        return f'''import org.gradle.api.tasks.SourceSetContainer
import org.gradle.api.tasks.compile.JavaCompile
import org.gradle.api.tasks.testing.Test
extensions.configure<SourceSetContainer> {{ named("main") {{ resources.setSrcDirs(listOf("resources{value}")) }} }}
tasks.withType<JavaCompile>().configureEach {{ options.compilerArgs = {arguments} }}
tasks.withType<Test>().configureEach {{ systemProperty("expected", "{value}") }}
'''
    return f'''sourceSets.main.resources.setSrcDirs(['resources{value}'])
tasks.withType(JavaCompile).configureEach {{ options.compilerArgs = {"['-parameters']" if value == "B" else "[]"} }}
tasks.withType(Test).configureEach {{ systemProperty 'expected', '{value}' }}
'''


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("kind", ["nested", "external", "empty", "kotlin", "switch"])
def test_applied_scripts_refresh_launch_restart_test_and_reopen(tmp_path, kind):
    require_real_mcp_java_e2e()
    root, app, config, source, port = project_fixture(tmp_path, "gradle", False)
    counter = root / "configuration-count.txt"
    kotlin = kind == "kotlin"
    if kotlin:
        config.unlink()
        config = root / "build.gradle.kts"
        junit = (Path.home() / ".m2/repository/junit/junit/4.13.2/junit-4.13.2.jar").as_posix()
        hamcrest = (Path.home() / ".m2/repository/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar").as_posix()
        config.write_text(f'''plugins {{ java }}
java {{ sourceCompatibility = JavaVersion.VERSION_1_8; targetCompatibility = JavaVersion.VERSION_1_8 }}
tasks.withType<JavaCompile>().configureEach {{ options.encoding = "UTF-8" }}
dependencies {{ testImplementation(files("{junit}", "{hamcrest}")) }}
''')
        # Use the actual Kotlin apply(from=...) form; applied script sets the model.
        relative = "config/脚本 input.gradle.kts"
        config.write_text(config.read_text() + f'val input = "{relative}"\napply(from = input)\n'
                          + 'file("configuration-count.txt").appendText("loaded\\n")\n')
    else:
        config.write_text(config.read_text() + "new File(rootDir, 'configuration-count.txt').append('loaded\\n')\n")
        relative = "../shared scripts/配置.inc" if kind == "external" else "config/behavior.inc"
        entry = root / "config/entry.gradle"
        entry.parent.mkdir()
        entry.write_text("def path = " + json.dumps(relative) + "\napply from: rootProject.file(path)\n")
        config.write_text(config.read_text() + "def parts = ['config', 'entry.gradle']\napply from: file(parts.join(File.separator))\n")
    script = root / relative
    script.parent.mkdir(parents=True, exist_ok=True)
    original = "" if kind == "empty" else script_body("A", kotlin)
    script.write_text(original, encoding="utf-8")
    unrelated = root / "unused/unrelated.gradle"
    unrelated.parent.mkdir()
    unrelated.write_text("throw new GradleException('not applied')\n")
    source.write_text(source.read_text()
        .replace('byte[] b=value().getBytes("UTF-8")',
                 'byte[] b=(value()+"|"+App.class.getMethod("echo",String.class).getParameters()[0].isNamePresent()).getBytes("UTF-8")')
        .replace(" public static void main", " public static String echo(String arg) {return arg;}\n public static void main"))
    oracle = root / "expected.txt"
    oracle.write_text("A")
    test = app / "src/test/java/example/ReadyTest.java"
    test.write_text('''package example;
public class ReadyTest {@org.junit.Test public void resource() throws Exception {
 String expected=new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(ORACLE)),"UTF-8");
 org.junit.Assert.assertEquals(expected, System.getProperty("expected"));
 org.junit.Assert.assertEquals(expected, App.value());
 org.junit.Assert.assertEquals("B".equals(expected), App.class.getMethod("echo",String.class).getParameters()[0].isNamePresent());
}}'''.replace("ORACLE", json.dumps(oracle.as_posix())))
    unchanged = {p: p.read_bytes() for p in (config, source, test)}
    cache = tmp_path / "cache"
    env = {**os.environ, "GRADLE_ARGS": "--offline", "XDG_CACHE_HOME": str(cache)}

    def loads():
        return counter.read_text().count("loaded")

    def edit(value):
        script.write_text(original if value == "A" else script_body(value, kotlin), encoding="utf-8")
        oracle.write_text(value)
        assert all(p.read_bytes() == data for p, data in unchanged.items())

    async def scenario():
        nonlocal script
        for cycle in range(2):
            if cycle:
                edit("B")
            with temporary_stderr() as log:
                async with open_mcp_session(log, environment=env) as session:
                    async def call(tool, **args):
                        return dict((await session.call_tool(tool, args)).structuredContent)

                    async def run_test(passed=True):
                        result = await call("java_fast_test", project_path=str(root), tests=["example.ReadyTest"])
                        with anyio.fail_after(180):
                            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                                await anyio.sleep(.2)
                                result = await call("java_fast_test", action="result", test_run_id=result["test_run_id"])
                        assert result.get("passed") is passed, result
                        return result

                    async def run_app(value, restart=False):
                        result = await call("java_application", **({"action": "restart"} if restart else {
                            "action": "launch", "project_path": str(root), "launch_name": "Preparation",
                            "ready_port": port, "jdwp_port": reserve_local_port(),
                        }))
                        with anyio.fail_after(180):
                            while result.get("startup_state") != "ready" and result.get("ok"):
                                await anyio.sleep(.2)
                                result = await call("java_status", action="status", details=True)
                        assert result.get("startup_state") == "ready", result
                        assert response(port) == value + "|" + str(value == "B").lower()
                        return result

                    try:
                        value = "B" if cycle else "A"
                        await run_app(value)
                        await run_test()
                        if not cycle:
                            count = loads()
                            unrelated.write_text("// still not applied\n")
                            assert (await run_test())["compiled_source_count"] == 0
                            assert loads() == count, "unused scripts must not invalidate the build world"
                            edit("B")
                            await run_test()
                            await run_app("B", restart=True)
                            edit("A")
                            await run_app("A", restart=True)
                            await run_test()
                            # A missing applied file must reach Gradle, not silently run stale tests.
                            script.unlink()
                            missing = await run_test(passed=False)
                            assert missing["ok"] is False and "error_code" in missing, missing
                            edit("A")
                            await run_test()
                            if kind == "switch":
                                previous = script
                                script = root / "more config/replacement.script"
                                script.parent.mkdir()
                                script.write_text(original)
                                entry.write_text("apply from: rootProject.file('more config/replacement.script')\n")
                                await run_test()
                                await run_app("A", restart=True)
                                previous.unlink()
                                count = loads()
                                await run_test()
                                assert loads() == count, "a removed script dependency must stop invalidating the model"
                        count = loads()
                        await call("java_application", action="stop")
                        assert (await run_app(value))["probe_cache_reused"]
                        await run_test()
                        assert loads() == count
                    finally:
                        await call("java_application", action="stop")
        for file in (cache / "jolink-runtime").glob("*/**/build-world.json"):
            data = json.loads(file.read_text())
            inputs = data.get("configuration_files", data.get("jdt_plan", {}).get("configuration_inputs", []))
            assert str(script.resolve()) in inputs, inputs
            assert str(unrelated.resolve()) not in inputs
            assert not any("gradle-init.private.gradle" in p for p in inputs)

    anyio.run(scenario)
