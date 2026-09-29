"""Real MCP: build choices survive HotSwap, restart, refresh and reopen."""

import json
import os
import shutil
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


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("system,multi", [("maven", False), ("maven", True), ("gradle", False), ("gradle", True)])
def test_launch_build_choices_are_shared_and_inherited(tmp_path, system, multi):
    require_real_mcp_java_e2e()
    java8 = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    java17 = os.environ.get("JOLINK_TEST_JAVA17_HOME")
    if not java8 or not java17:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME and JOLINK_TEST_JAVA17_HOME")
    root, app, config, source, port = project_fixture(tmp_path, system, multi if system == "maven" else False)
    shutil.rmtree(root / ".idea")
    if system == "gradle" and multi:
        app = root / "app"
        app.mkdir()
        for name in ("src", "resourcesA", "resourcesB", "build.gradle"):
            (root / name).rename(app / name)
        config = app / "build.gradle"
        source = app / "src/main/java/example/App.java"
        (root / "settings.gradle").write_text("rootProject.name='build-environment'; include 'app', 'lib'\n")
        lib = root / "lib"
        (lib / "src/main/java/example").mkdir(parents=True)
        (lib / "src/main/java/example/Library.java").write_text("package example; public class Library {}")
        (lib / "build.gradle").write_text("plugins {id 'java-library'}; sourceCompatibility='8'; targetCompatibility='8'\n")
        config.write_text(config.read_text() + "dependencies { implementation project(':lib') }\n")

    version = app / "src/main/java/example/Version.java"
    version.write_text('package example; public class Version {public static String value(){return "v1";}}')
    source.write_text(source.read_text().replace(
        'byte[] b=value().getBytes("UTF-8")',
        'byte[] b=(environment()+"|"+System.getProperty("java.specification.version")+"|"+Version.value()).getBytes("UTF-8")',
    ).replace(' public static void main', ''' public static String environment() throws Exception {
  try(java.io.InputStream s=App.class.getResourceAsStream("/environment.txt")) {
   java.io.ByteArrayOutputStream b=new java.io.ByteArrayOutputStream(); int c; while((c=s.read())!=-1)b.write(c); return b.toString("UTF-8");
  }
 }
 public static void main'''))
    resource = app / "resourcesA/environment.txt"
    cache = tmp_path / "cache"
    settings = root / "chosen settings.xml"
    wrapper_marker = root / "wrong-wrapper-used"
    if system == "maven":
        resource.write_text("@java.specification.version@|@launch.value@")
        config.write_text(config.read_text().replace("<directory>${resource.folder}</directory>",
                                                    "<directory>${resource.folder}</directory><filtering>true</filtering>"))
        def select(value):
            settings.write_text(f"<settings><profiles><profile><id>chosen</id><properties><launch.value>{value}</launch.value></properties></profile></profiles><activeProfiles><activeProfile>chosen</activeProfile></activeProfiles></settings>")
        wrapper = root / ("mvnw.cmd" if os.name == "nt" else "mvnw")
        wrapper.write_text('@echo wrong>wrong-wrapper-used\r\n@exit /b 99\r\n' if os.name == "nt" else '#!/bin/sh\ntouch wrong-wrapper-used\nexit 99\n')
        wrapper.chmod(0o755)
        maven = shutil.which("mvn.cmd" if os.name == "nt" else "mvn")
        if not maven:
            pytest.skip("a real Maven installation is needed")
        extra = {"maven_home": str(Path(maven).resolve().parent.parent), "maven_settings": str(settings)}
    else:
        resource.write_text("${buildJava}|${launchValue}")
        config.write_text(config.read_text() + """
processResources {
 inputs.property('launchValue', project.property('launchValue'))
 inputs.property('buildJava', System.getProperty('java.specification.version'))
 filesMatching('environment.txt') {
  expand(buildJava: System.getProperty('java.specification.version'), launchValue: project.property('launchValue'))
 }
}
""")
        def select(value):
            (root / "gradle.properties").write_text(f"launchValue={value}\n")
        extra = {}
    select("A")
    env = {**os.environ, "JAVA_HOME": java8, "PATH": str(Path(java8) / "bin") + os.pathsep + os.environ["PATH"],
           "MAVEN_ARGS": "--offline", "GRADLE_ARGS": "--offline", "XDG_CACHE_HOME": str(cache)}

    def model_mtime():
        return next((cache / "jolink-runtime/project-launch").glob("*/build-world.json")).stat().st_mtime_ns

    async def scenario():
        for reopened in (False, True):
            with temporary_stderr() as log:
                async with open_mcp_session(log, environment=env) as session:
                    async def call(tool, **args):
                        return dict((await session.call_tool(tool, args)).structuredContent)

                    async def status():
                        return await call("java_status", action="status", details=True)

                    async def ready(result):
                        with anyio.fail_after(180):
                            while result.get("launch_phase") not in {"failed", "runtime_active"} and result.get("ok", True):
                                await anyio.sleep(.2)
                                result = await status()
                        assert result.get("startup_state") == "ready", json.dumps(result)
                        return await status()

                    async def launch(runtime_home=java8, build_home=java17):
                        return await ready(await call("java_application", action="launch", project_path=str(root),
                            main_class="example.App", build_system=system, build_java_home=build_home, java_home=runtime_home,
                            ready_port=port, jdwp_port=reserve_local_port(), **extra))

                    async def restart(hotswap=True):
                        result = await call("java_application", action="restart", hotswap=hotswap)
                        with anyio.fail_after(180):
                            while result.get("applied") is None and result.get("ok", True):
                                observed = await status()
                                if result.get("reload_id"):
                                    result = observed.get("last_reload") or result
                                elif observed.get("launch_phase") in {"runtime_active", "failed"}:
                                    await ready(observed)
                                    return observed
                                await anyio.sleep(.2)
                        assert result.get("applied") is True, json.dumps(result)
                        await ready(await status())
                        return result

                    try:
                        first = await launch()
                        assert first["probe_cache_reused"] is reopened, first
                        assert response(port) == ("17|B|1.8|v2" if reopened else "17|A|1.8|v1")
                        assert not wrapper_marker.exists()
                        if not reopened:
                            before = model_mtime()
                            version.write_text(version.read_text().replace('"v1"', '"v2"'))
                            changed = await restart()
                            assert changed["apply_method"] == "hotswap", changed
                            assert response(port) == "17|A|1.8|v2"
                            await restart(hotswap=False)
                            assert response(port) == "17|A|1.8|v2"
                            assert model_mtime() == before, "restart must not lose explicit choices and re-Probe"
                            await call("java_application", action="stop")
                            assert (await launch())["probe_cache_reused"]
                            select("B")
                            await restart(hotswap=False)
                            assert response(port) == "17|B|1.8|v2"
                            assert model_mtime() != before, "changed selected config must refresh the model"
                        else:
                            await call("java_application", action="stop")
                            # Changing only the application JVM must not change the build selection.
                            assert (await launch(runtime_home=java17))["probe_cache_reused"]
                            assert response(port) == "17|B|17|v2"
                            await call("java_application", action="stop")
                            # Unlike an application-JDK-only edit, changing the
                            # build JDK changes the model and filtered resources.
                            assert not (await launch(runtime_home=java17, build_home=java8))["probe_cache_reused"]
                            assert response(port) == "1.8|B|17|v2"
                    finally:
                        await call("java_application", action="stop")
    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_public_petclinic_launch_restart_with_explicit_environment(tmp_path):
    require_real_mcp_java_e2e()
    checkout = os.environ.get("JOLINK_TEST_PETCLINIC")
    java8 = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    java17 = os.environ.get("JOLINK_TEST_JAVA17_HOME")
    if not checkout or not java8 or not java17:
        pytest.skip("set the Petclinic checkout and JDK paths")
    project = tmp_path / "petclinic"
    shutil.copytree(checkout, project, ignore=shutil.ignore_patterns(".git", "target", "build", ".gradle", ".idea"))
    port = reserve_local_port()
    options = {"action": "launch", "project_path": str(project), "build_system": "maven",
               "main_class": "org.springframework.samples.petclinic.PetClinicApplication",
               "java_home": java17, "build_java_home": java17, "ready_port": port,
               "jdwp_port": reserve_local_port(), "app_args": [f"--server.port={port}"]}
    for key in ("maven_home", "maven_settings"):
        if value := os.environ.get("JOLINK_TEST_" + key.upper()):
            options[key] = value
    env = {**os.environ, "JAVA_HOME": java8, "PATH": str(Path(java8) / "bin") + os.pathsep + os.environ["PATH"],
           "XDG_CACHE_HOME": str(tmp_path / "cache")}

    async def scenario():
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                async def call(tool, **args):
                    return dict((await session.call_tool(tool, args)).structuredContent)

                async def ready(result):
                    with anyio.fail_after(180):
                        while result.get("startup_state") != "ready" and result.get("ok", True) and result.get("launch_phase") != "failed":
                            await anyio.sleep(.5)
                            result = await call("java_status", action="status", details=True)
                    assert result.get("startup_state") == "ready", json.dumps(result)
                    assert "PetClinic" in await anyio.to_thread.run_sync(response, port)
                    return result

                try:
                    first = await ready(await call("java_application", **options))
                    restarted = await call("java_application", action="restart", hotswap=False)
                    with anyio.fail_after(180):
                        while restarted.get("applied") is None and restarted.get("ok", True):
                            await anyio.sleep(.5)
                            details = await call("java_status", action="status", details=True)
                            restarted = details.get("last_reload") or restarted
                    assert restarted.get("applied") is True, restarted
                    second = await ready(await call("java_status", action="status", details=True))
                    assert second["pid"] != first["pid"]
                    await call("java_application", action="stop")
                    third = await ready(await call("java_application", **options))
                    assert third["probe_cache_reused"] and third["jdt_bootstrap_reused"], third
                finally:
                    await call("java_application", action="stop")
    anyio.run(scenario)
