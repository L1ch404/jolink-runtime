from __future__ import annotations

import os
import json
from pathlib import Path
import socket

import anyio
import pytest

from java_support import (
    open_mcp_session,
    temporary_stderr,
    require_real_mcp_java_e2e,
    reserve_local_port,
)


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("dsl", ["groovy", "kotlin"])
def test_gradle_modules_mcp_launch_reload_test_and_reopen(tmp_path: Path, dsl: str):
    require_real_mcp_java_e2e()
    gradle = os.environ.get("JOLINK_FAST_TEST_GRADLE")
    if not gradle:
        pytest.skip("set JOLINK_FAST_TEST_GRADLE")
    junit = Path.home() / ".m2/repository/junit/junit/4.13.2/junit-4.13.2.jar"
    hamcrest = (
        Path.home()
        / ".m2/repository/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar"
    )
    if not junit.is_file() or not hamcrest.is_file():
        pytest.skip("JUnit4/Hamcrest fixture dependencies are required")
    project = tmp_path / "Gradle 模块 workspace"
    project.mkdir()
    launcher = project / ("gradlew.bat" if os.name == "nt" else "gradlew")
    launcher.write_text(
        f'@call "{gradle}" %*\r\n'
        if os.name == "nt"
        else f'#!/bin/sh\nexec "{gradle}" "$@"\n'
    )
    launcher.chmod(0o755)
    wrapper = project / "gradle/wrapper/gradle-wrapper.properties"
    wrapper.parent.mkdir(parents=True)
    version = os.environ.get("JOLINK_FAST_TEST_GRADLE_VERSION", "8.14")
    wrapper.write_text(
        f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{version}-bin.zip\n"
    )
    kotlin = dsl == "kotlin"
    (project / ("settings.gradle.kts" if kotlin else "settings.gradle")).write_text(
        'rootProject.name = "modules"\ninclude("base", "core", "app", "runtime", "unused")\n'
    )
    suffix = ".gradle.kts" if kotlin else ".gradle"
    (project / ("build" + suffix)).write_text(
        'subprojects { apply(plugin = "java-library")\n extensions.configure<JavaPluginExtension> { sourceCompatibility = JavaVersion.VERSION_1_8; targetCompatibility = JavaVersion.VERSION_1_8 }\n}\n'
        if kotlin
        else "subprojects { apply plugin: 'java-library'; java { sourceCompatibility = '8'; targetCompatibility = '8' } }\n"
    )
    for name in ("base", "core", "app", "runtime", "unused"):
        (project / name / "src/main/java/example").mkdir(parents=True)
        (project / name / ("build" + suffix)).write_text("")
    (project / "core" / ("build" + suffix)).write_text(
        'dependencies { "api"(project(":base")) }\n'
        if kotlin
        else "dependencies { api project(':base') }\n"
    )
    app_build = project / "app" / ("build" + suffix)
    app_build.write_text(
        f'dependencies {{ "implementation"(project(":core")); "runtimeOnly"(project(":runtime")); "testImplementation"(files("{junit.as_posix()}", "{hamcrest.as_posix()}")) }}\n'
        if kotlin
        else f"dependencies {{ implementation project(':core'); runtimeOnly project(':runtime'); testImplementation files('{junit.as_posix()}', '{hamcrest.as_posix()}') }}\n"
    )
    base = project / "base/src/main/java/example/Base.java"
    # An unselected retry task must neither block the default test task nor run.
    app_build.write_text(app_build.read_text() + (
        '\ntasks.register<Test>("retryTest") { doFirst { error("retryTest must not run") } }\n'
        if kotlin else
        "\ntasks.register('retryTest', Test) { doFirst { throw new GradleException('retryTest must not run') } }\n"
    ))
    original = "package example; public class Base { public static final int NUMBER=40; public static int value(){return NUMBER;} }"
    base.write_text(original)
    (project / "core/src/main/java/example/Core.java").write_text(
        "package example; public class Core { public static int value(){ return Base.value()+Base.NUMBER-40+2; } }"
    )
    if not kotlin:
        lombok = (
            Path.home()
            / ".m2/repository/org/projectlombok/lombok/1.18.20/lombok-1.18.20.jar"
        )
        if not lombok.is_file():
            pytest.skip("Lombok 1.18.20 fixture dependency is required")
        (project / "base/build.gradle").write_text(
            f"dependencies {{ compileOnly files('{lombok.as_posix()}'); annotationProcessor files('{lombok.as_posix()}') }}\n"
        )
        original = original.replace(
            "public class Base {",
            "@lombok.Getter public class Base { private final int extra = 0;",
        )
        base.write_text(original)
        core = project / "core/src/main/java/example/Core.java"
        core.write_text(
            core.read_text().replace(
                "return Base.value()", "return new Base().getExtra()+Base.value()"
            )
        )
    (project / "unused/src/main/java/example/Broken.java").write_text("not valid java")
    (project / "runtime/src/main/java/example/RuntimeOnly.java").write_text(
        "package example; public class RuntimeOnly {}"
    )
    app = project / "app/src/main/java/example/App.java"
    app.write_text("""package example; public class App {
public static void main(String[] args) throws Exception {
 Class.forName("example.RuntimeOnly");
 try(java.net.ServerSocket server=new java.net.ServerSocket(Integer.parseInt(args[0]))) {
  while(true) { try(java.net.Socket socket=server.accept()) { socket.getOutputStream().write(String.valueOf(Core.value()).getBytes("UTF-8")); } }
 }
}}""")
    resource = project / "base/src/main/resources/base.txt"
    resource.parent.mkdir(parents=True)
    resource.write_text("upstream-resource")
    test = project / "app/src/test/java/example/AppTest.java"
    test.parent.mkdir(parents=True)
    test.write_text("""package example; public class AppTest {
@org.junit.Test public void value() throws Exception {
 org.junit.Assert.assertEquals(42,Core.value());
 org.junit.Assert.assertNotNull(Class.forName("example.RuntimeOnly"));
 org.junit.Assert.assertNotNull(AppTest.class.getResourceAsStream("/base.txt"));
 org.junit.Assert.assertEquals("from-gradle", System.getProperty("fixture.property"));
 org.junit.Assert.assertEquals("persisted", System.getenv("FIXTURE_VALUE"));
}}""")
    with app_build.open("a") as stream:
        stream.write(
            'tasks.withType<Test> { systemProperty("fixture.property", "from-gradle"); environment("FIXTURE_VALUE", "persisted") }\n'
            if kotlin
            else "test { systemProperty 'fixture.property', 'from-gradle'; environment 'FIXTURE_VALUE', 'persisted' }\n"
        )
    port, debug = reserve_local_port(), reserve_local_port()
    (project / ".run").mkdir()
    (project / ".run/App.xml").write_text(
        f'''<component name="ProjectRunConfigurationManager"><configuration name="App" type="Application"><module name="app"/><option name="MAIN_CLASS_NAME" value="example.App"/><option name="WORKING_DIRECTORY" value="$PROJECT_DIR$"/><option name="PROGRAM_PARAMETERS" value="{port}"/><method v="2"><option name="Make" enabled="true"/></method></configuration></component>'''
    )
    env = {
        **os.environ,
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "GRADLE_ARGS": "--offline",
    }
    alias = tmp_path / "project alias"
    try:
        alias.symlink_to(project, target_is_directory=True)
    except OSError:
        alias = project

    async def scenario():
        for cycle in range(3):
            with temporary_stderr() as stderr:
                async with open_mcp_session(stderr, environment=env) as session:

                    async def call(tool, args):
                        return dict(
                            (await session.call_tool(tool, args)).structuredContent
                            or {}
                        )

                    async def poll(predicate):
                        with anyio.fail_after(180):
                            while True:
                                state = await call("java_status", {"action": "status"})
                                assert state.get("launch_phase") != "failed", state
                                if predicate(state):
                                    return state
                                await anyio.sleep(0.05)

                    async def run_test():
                        value = await call(
                            "java_fast_test",
                            {
                                "action": "run",
                                "project_path": str(project),
                                "tests": ["example.AppTest#value"],
                                "timeout": 20,
                            },
                        )
                        if value.get("status") in {
                            "starting",
                            "bootstrapping",
                            "compiling",
                            "running",
                        }:
                            state = await poll(
                                lambda s: (
                                    s.get("fast_test", {}).get("status")
                                    not in {
                                        "starting",
                                        "bootstrapping",
                                        "compiling",
                                        "running",
                                    }
                                )
                            )
                            value = state["fast_test"]
                        return value

                    baseline = await run_test()
                    assert baseline.get("passed") is True, json.dumps(baseline)
                    warm = await run_test()
                    assert warm.get("passed") is True, warm
                    assert warm["compiled_source_count"] == 0, warm
                    if cycle == 0:
                        for bad in (
                            original.replace("NUMBER=40", "NUMBER=39"),
                            "package example; broken",
                        ):
                            base.write_text(bad)
                            failed = await run_test()
                            assert failed.get("passed") is not True, failed
                            base.write_text(original)
                            recovered = await run_test()
                            assert recovered.get("passed") is True, recovered
                        # api is exported by Gradle; implementation must not be visible to app.
                        core_build = project / "core" / ("build" + suffix)
                        old = core_build.read_text()
                        core_build.write_text(old.replace("api", "implementation"))
                        app_original = app.read_text()
                        app.write_text(
                            app_original.replace(
                                "public class App {",
                                "public class App { int illegal = Base.NUMBER;",
                            )
                        )
                        invisible = await run_test()
                        assert invisible.get("ok") is False, invisible
                        app.write_text(app_original)
                        core_build.write_text(old)
                        recovered = await run_test()
                        assert recovered.get("passed") is True, recovered
                        waiting_test = test.with_name("CancelProbeTest.java")
                        waiting_test.write_text(
                            "package example; public class CancelProbeTest { @org.junit.Test public void waitForCancel() throws Exception { Thread.sleep(30000); } }"
                        )
                        active_test = await call(
                            "java_fast_test",
                            {
                                "action": "run",
                                "project_path": str(project),
                                "tests": ["example.CancelProbeTest"],
                                "timeout": 60,
                            },
                        )
                        assert active_test["status"] == "running", active_test
                        await call(
                            "java_fast_test",
                            {
                                "action": "cancel",
                                "test_run_id": active_test["test_run_id"],
                            },
                        )
                        cancelled = await poll(
                            lambda s: (
                                s.get("fast_test", {}).get("status") == "cancelled"
                            )
                        )
                        assert (
                            cancelled["fast_test"]["error_code"] == "TEST_CANCELLED"
                        ), cancelled
                        waiting_test.unlink()
                        recovered = await run_test()
                        assert recovered.get("passed") is True, recovered
                        app.write_text(
                            app_original.replace(
                                "public class App {",
                                "public class App { RuntimeOnly illegal;",
                            )
                        )
                        invisible = await run_test()
                        assert invisible.get("ok") is False, invisible
                        app.write_text(app_original)
                        recovered = await run_test()
                        assert recovered.get("passed") is True, recovered
                    accepted = await call(
                        "java_application",
                        {
                            "action": "launch",
                            "project_path": str(alias if cycle == 0 else project),
                            "launch_name": "App",
                            "jdwp_port": debug,
                            "ready_port": port,
                            "timeout": 10,
                        },
                    )
                    assert accepted["ok"], accepted
                    active = await poll(
                        lambda s: s.get("launch_phase") == "runtime_active"
                    )
                    assert active["jdt_bootstrap_reused"] is bool(cycle), active
                    if cycle:
                        assert active["jdt_bootstrap_build_kind"] is None

                    def value():
                        with socket.create_connection(
                            ("127.0.0.1", port), timeout=3
                        ) as s:
                            return s.recv(64).decode()

                    assert await anyio.to_thread.run_sync(value) == "42"
                    for replacement, expected in (
                        (original.replace("NUMBER=40", "NUMBER=39"), "40"),
                        (original, "42"),
                    ):
                        base.write_text(replacement)
                        reload = await call(
                            "java_application",
                            {
                                "action": "reload",
                                "source_files": [
                                    "base/src/main/java/example/Base.java"
                                ],
                            },
                        )
                        assert reload["status"] == "reload_started", reload
                        complete = await poll(
                            lambda s: (
                                s.get("last_reload", {}).get("reload_id")
                                == reload["reload_id"]
                                if s.get("last_reload")
                                else False
                            )
                        )
                        assert complete["last_reload"]["applied"] is True, complete
                        assert await anyio.to_thread.run_sync(value) == expected
                    assert (await call("java_application", {"action": "stop"}))["ok"]
            if cycle == 0:
                # Simulate the pre-fix persisted alias, then reopen it in a new MCP.
                for cached in (tmp_path / "cache/jolink-runtime/project-launch").glob(
                    "*/build-world.json"
                ):
                    raw = json.loads(cached.read_text())
                    raw["jdt_plan"]["project_root"] = str(alias)
                    raw["jdt_plan"]["module_root"] = str(alias / "app")
                    cached.write_text(json.dumps(raw))
        for name in ("base", "core", "app", "runtime", "unused"):
            assert not (project / name / "build").exists()

    anyio.run(scenario)
