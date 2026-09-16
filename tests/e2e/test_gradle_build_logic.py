"""Configured source roots and build-logic refresh through the product MCP."""

import os
import socket
from pathlib import Path

import anyio
import pytest
from java_support import (
    open_mcp_session,
    require_real_mcp_java_e2e,
    reserve_local_port,
    temporary_stderr,
)


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("kind", ["buildSrc", "included"])
def test_build_logic_and_custom_roots_survive_mcp_reopen(tmp_path, kind):
    require_real_mcp_java_e2e()
    gradle = os.environ.get("JOLINK_FAST_TEST_GRADLE")
    if not gradle:
        pytest.skip("set JOLINK_FAST_TEST_GRADLE")
    project = tmp_path / "custom project"
    project.mkdir()
    wrapper = project / ("gradlew.bat" if os.name == "nt" else "gradlew")
    wrapper.write_text(
        f'@echo off\r\ncall "{gradle}" %*\r\n'
        if os.name == "nt"
        else f'#!/bin/sh\nexec "{gradle}" "$@"\n'
    )
    wrapper.chmod(0o755)
    properties = project / "gradle/wrapper/gradle-wrapper.properties"
    properties.parent.mkdir(parents=True)
    version = os.environ.get("JOLINK_FAST_TEST_GRADLE_VERSION", "8.14")
    properties.write_text(
        f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{version}-bin.zip\n"
    )
    logic = project / ("buildSrc" if kind == "buildSrc" else "convention-tools")
    source = logic / "implementation/fixture/Convention.java"
    source.parent.mkdir(parents=True)
    (project / "settings.gradle").write_text(
        (
            "pluginManagement { includeBuild 'convention-tools' }\n"
            if kind == "included"
            else ""
        )
        + "rootProject.name='application'\n"
    )
    nested = project / "shared-tools"
    if kind == "included":
        nested.mkdir()
        (nested / "settings.gradle").write_text("rootProject.name='layout'\n")
        (nested / "build.gradle").write_text(
            "plugins { id 'java-library' }; group='fixture.logic'; version='1'\nsourceSets.main.java.setSrcDirs(['toolsrc'])\n"
        )
        helper = nested / "toolsrc/fixture/Layout.java"
        helper.parent.mkdir(parents=True)
        helper.write_text(
            'package fixture; public class Layout { public static String main(){return "business-java";} }'
        )
        (logic / "settings.gradle").write_text(
            "rootProject.name='conventions'; includeBuild '../shared-tools'\n"
        )
    else:
        helper = source
    (logic / "build.gradle").write_text(
        """plugins { id 'java-gradle-plugin' }
sourceSets.main.java.setSrcDirs(['implementation'])
gradlePlugin { plugins { layout { id='fixture.layout'; implementationClass='fixture.Convention' } } }
"""
        + (
            "dependencies { implementation 'fixture.logic:layout:1' }\n"
            if kind == "included"
            else ""
        )
    )
    source.write_text(
        """package fixture;
import java.util.Collections;
import org.gradle.api.*;
import org.gradle.api.plugins.JavaPluginExtension;
import org.gradle.api.tasks.SourceSetContainer;
import org.gradle.api.tasks.compile.JavaCompile;
public class Convention implements Plugin<Project> {
 public void apply(Project p) {
  p.getPluginManager().apply("java");
  JavaPluginExtension java=p.getExtensions().getByType(JavaPluginExtension.class);
  java.setSourceCompatibility("8"); java.setTargetCompatibility("8");
  SourceSetContainer sets=p.getExtensions().getByType(SourceSetContainer.class);
  sets.getByName("main").getJava().setSrcDirs(Collections.singleton(MAIN_ROOT));
  sets.getByName("test").getJava().setSrcDirs(Collections.singleton("checks-java"));
  p.getTasks().withType(JavaCompile.class).configureEach(task ->
    task.getOptions().getCompilerArgumentProviders().add(() -> Collections.singleton("-parameters")));
 }
}""".replace(
            "MAIN_ROOT",
            "fixture.Layout.main()" if kind == "included" else '"business-java"',
        )
    )
    junit = Path.home() / ".m2/repository/junit/junit/4.13.2/junit-4.13.2.jar"
    hamcrest = (
        Path.home()
        / ".m2/repository/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar"
    )
    (project / "build.gradle").write_text(f"""plugins {{ id 'fixture.layout' }}
dependencies {{ testImplementation files('{junit.as_posix()}', '{hamcrest.as_posix()}') }}
new File(rootDir, 'configuration-count.txt').append('loaded\\n')
tasks.withType(JavaCompile).configureEach {{ doFirst {{ throw new GradleException('business compile task must not run') }} }}
tasks.withType(Test).configureEach {{ doFirst {{ throw new GradleException('Gradle test task must not run') }} }}
""")
    main = project / "business-java/example/Value.java"
    main.parent.mkdir(parents=True)
    original = "package example; public class Value { public static int get(){return 1;} public static int echo(int named){return named;} }"
    main.write_text(original)
    alternate = project / "alternate-java/example/Value.java"
    alternate.parent.mkdir(parents=True)
    alternate.write_text(original.replace("return 1", "return 2"))
    app_source = """package example; public class App {
 public static void main(String[] args) throws Exception {
  try(java.net.ServerSocket server=new java.net.ServerSocket(Integer.parseInt(args[0]))) {
   while(true) { try(java.net.Socket socket=server.accept()) {
    socket.getOutputStream().write(String.valueOf(Value.get()).getBytes("UTF-8"));
   }}
  }
 }
}"""
    (main.parent / "App.java").write_text(app_source)
    (alternate.parent / "App.java").write_text(app_source)
    test = project / "checks-java/example/AppTest.java"
    test.parent.mkdir(parents=True)
    test.write_text(
        'package example; public class AppTest { @org.junit.Test public void value() throws Exception {org.junit.Assert.assertEquals(1,Value.get()); org.junit.Assert.assertTrue(Value.class.getMethod("echo", int.class).getParameters()[0].isNamePresent());} }'
    )
    port, debug = reserve_local_port(), reserve_local_port()
    (project / ".run").mkdir()
    (project / ".run/App.xml").write_text(
        f'''<component name="ProjectRunConfigurationManager"><configuration name="App" type="Application"><option name="MAIN_CLASS_NAME" value="example.App"/><option name="WORKING_DIRECTORY" value="$PROJECT_DIR$"/><option name="PROGRAM_PARAMETERS" value="{port}"/><method v="2"><option name="Make" enabled="true"/></method></configuration></component>'''
    )
    env = {
        **os.environ,
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "GRADLE_ARGS": "--offline",
    }
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")
    original_logic = helper.read_text()

    def loads():
        return (project / "configuration-count.txt").read_text().count("loaded")

    async def scenario():
        for cycle in range(2):
            with temporary_stderr() as stderr:
                async with open_mcp_session(stderr, environment=env) as session:

                    async def call(tool, args):
                        return dict(
                            (await session.call_tool(tool, args)).structuredContent
                            or {}
                        )

                    async def run_test():
                        state = await call(
                            "java_fast_test",
                            {
                                "action": "run",
                                "build_system": "gradle",
                                "project_path": str(project),
                                "tests": ["example.AppTest"],
                                "timeout": 20,
                            },
                        )
                        with anyio.fail_after(120):
                            while state.get("status") in {
                                "starting",
                                "bootstrapping",
                                "compiling",
                                "running",
                            }:
                                await anyio.sleep(0.05)
                                state = (
                                    await call("java_status", {"action": "status"})
                                )["fast_test"]
                        return state

                    first = await run_test()
                    assert first.get("passed"), first
                    count = loads()
                    assert (await run_test())["passed"]
                    assert loads() == count
                    if cycle == 0:
                        main.write_text(original.replace("return 1", "return 3"))
                        result = await run_test()
                        assert result.get("failed_count") == 1, result
                        assert loads() == count
                        main.write_text(original)
                        assert (await run_test())["passed"]
                        helper.write_text(
                            original_logic.replace("business-java", "alternate-java")
                        )
                        result = await run_test()
                        assert result.get("failed_count") == 1, result
                        assert loads() > count
                        helper.write_text(original_logic)
                        assert (await run_test())["passed"]
                    state = await call(
                        "java_application",
                        {
                            "action": "launch",
                            "project_path": str(project),
                            "launch_name": "App",
                            "ready_port": port,
                            "jdwp_port": debug,
                            "timeout": 10,
                        },
                    )
                    try:
                        with anyio.fail_after(120):
                            while state.get("launch_phase") != "runtime_active":
                                assert (
                                    state.get("ok") is not False
                                    and state.get("launch_phase") != "failed"
                                ), state
                                await anyio.sleep(0.05)
                                state = await call("java_status", {"action": "status"})

                        def value():
                            with socket.create_connection(
                                ("127.0.0.1", port), timeout=10
                            ) as stream:
                                return stream.recv(100).decode()

                        assert await anyio.to_thread.run_sync(value) == "1"
                        if cycle:
                            assert (
                                state["probe_cache_reused"]
                                and state["jdt_bootstrap_reused"]
                            ), state
                    finally:
                        await call("java_application", {"action": "stop"})
        assert not (project / "build/classes/java/main").exists()
        assert not (project / "build/classes/java/test").exists()

    anyio.run(scenario)
