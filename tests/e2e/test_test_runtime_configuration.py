"""Public MCP runtime configuration, independent of Java compilation state."""

import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET

import anyio
import pytest
from java_support import (
    open_mcp_session,
    require_real_mcp_java_e2e,
    reserve_local_port,
    temporary_stderr,
)
from test_build_configuration_mcp import project_fixture
from test_fast_test_environment_resources import finished
from test_review_model_cache import response


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("system,multi,framework,placement", [
    ("maven", False, "junit4", "execution"), ("maven", True, "junit4", "execution"),
    ("gradle", False, "junit4", "direct"), ("maven", False, "junit5", "direct"),
    ("maven", False, "testng", "execution"), ("maven", True, "junit4", "profile"),
])
def test_test_process_configuration_and_reopen(tmp_path, system, multi, framework, placement):
    require_real_mcp_java_e2e()
    root, app, config, _, _ = project_fixture(tmp_path, system, multi)
    original = config.read_text().replace("<version>3.13.0</version>", "<version>3.11.0</version>")
    if framework in {"junit5", "testng"}:
        group, artifact, version = ("org.junit.jupiter", "junit-jupiter", "5.8.2") if framework == "junit5" else ("org.testng", "testng", "7.5")
        original = original.replace("</dependencies>", f"<dependency><groupId>{group}</groupId><artifactId>{artifact}</artifactId><version>{version}</version><scope>test</scope></dependency></dependencies>")
    oracle = app / "runtime-expected.properties"
    test = app / "src/test/java/example/ReadyTest.java"
    test.write_text('''package example;
public class ReadyTest {
 static java.util.Properties expected() throws Exception {
  java.util.Properties p=new java.util.Properties();
  try(java.io.Reader r=java.nio.file.Files.newBufferedReader(java.nio.file.Paths.get(ORACLE),java.nio.charset.StandardCharsets.UTF_8)){p.load(r);}return p;
 }
 @org.junit.Test public void environment() throws Exception {
  java.util.Properties p=expected();
  org.junit.Assert.assertEquals(p.getProperty("value"),System.getenv("JOLINK_TEST_VALUE"));
  org.junit.Assert.assertEquals("",System.getenv("JOLINK_TEST_EMPTY"));
  org.junit.Assert.assertNull(System.getenv("JOLINK_TEST_REMOVE"));
  org.junit.Assert.assertEquals("configured",System.getenv("JOLINK_TEST_BOTH"));
  org.junit.Assert.assertEquals(p.getProperty("inherited"),System.getenv("JOLINK_TEST_INHERITED"));
  org.junit.Assert.assertEquals(p.getProperty("reference"),System.getenv("JOLINK_TEST_REFERENCE"));
 }
 @org.junit.Test public void directory() throws Exception {
  org.junit.Assert.assertEquals(new java.io.File(expected().getProperty("directory")).getCanonicalPath(),new java.io.File(".").getCanonicalPath());
  java.nio.file.Files.write(java.nio.file.Paths.get("runner-created.txt"),new byte[]{1});
 }
 @org.junit.Test public void assertions() throws Exception {
  boolean enabled=false; assert enabled=true;
  org.junit.Assert.assertEquals(Boolean.parseBoolean(expected().getProperty("assertions")),enabled);
 }
}'''.replace("ORACLE", json.dumps(oracle.as_posix())), encoding="utf-8")
    if framework != "junit4":
        annotation = "org.junit.jupiter.api.Test" if framework == "junit5" else "org.testng.annotations.Test"
        test.write_text(test.read_text().replace("@org.junit.Test", "@" + annotation), encoding="utf-8")

    def configure(enabled):
        directory = app / ("运行 B" if enabled else "运行 A")
        value = "second 中文 value" if enabled else "first 中文 value"
        if system == "maven":
            xml = ET.fromstring(original)
            plugin = next(p for p in xml.findall("./{*}build/{*}plugins/{*}plugin") if p.findtext("{*}artifactId") == "maven-surefire-plugin")
            if placement == "profile":
                profile = ET.SubElement(ET.SubElement(xml, "profiles"), "profile")
                ET.SubElement(profile, "id").text = "test-runtime"
                ET.SubElement(ET.SubElement(profile, "activation"), "activeByDefault").text = "true"
                plugin = ET.SubElement(ET.SubElement(ET.SubElement(profile, "build"), "plugins"), "plugin")
                ET.SubElement(plugin, "artifactId").text = "maven-surefire-plugin"
                settings = ET.SubElement(plugin, "configuration")
            elif placement == "execution":
                # Execution settings inherit the parent/plugin configuration.
                execution = ET.SubElement(ET.SubElement(plugin, "executions"), "execution")
                ET.SubElement(execution, "id").text = "default-test"
                ET.SubElement(ET.SubElement(execution, "goals"), "goal").text = "test"
                settings = ET.SubElement(execution, "configuration")
            else:
                settings = plugin.find("{*}configuration")
            ET.SubElement(settings, "workingDirectory").text = directory.name if placement == "direct" else "${project.basedir}/" + directory.name
            ET.SubElement(settings, "enableAssertions").text = str(enabled).lower()
            if framework == "testng":
                ET.SubElement(settings, "argLine").text = "-da"
            variables = ET.SubElement(settings, "environmentVariables")
            for key, item in {"JOLINK_TEST_VALUE": value, "JOLINK_TEST_EMPTY": "", "JOLINK_TEST_BOTH": "configured", "JOLINK_TEST_REFERENCE": "${env.JOLINK_TEST_CONFIG_INPUT}"}.items():
                ET.SubElement(variables, key).text = item
            excluded = ET.SubElement(settings, "excludedEnvironmentVariables")
            for name in ("JOLINK_TEST_REMOVE", "JOLINK_TEST_BOTH"):
                ET.SubElement(excluded, "excludedEnvironmentVariable").text = name
            config.write_text(ET.tostring(xml, encoding="unicode"), encoding="utf-8")
        else:
            config.write_text(original + f'''
test {{
 workingDir=file({json.dumps(directory.name, ensure_ascii=False)})
 enableAssertions={str(enabled).lower()}
 environment.remove('JOLINK_TEST_REMOVE')
 environment 'JOLINK_TEST_VALUE', {json.dumps(value, ensure_ascii=False)}
 environment 'JOLINK_TEST_EMPTY', ''
 environment 'JOLINK_TEST_BOTH', 'configured'
 environment 'JOLINK_TEST_REFERENCE', project.property('runtimeRef')
}}
''', encoding="utf-8")
        return directory, value

    directory, _ = configure(False)
    assert not directory.exists()
    cache = tmp_path / "cache"
    base_env = {**os.environ, "XDG_CACHE_HOME": str(cache), "MAVEN_ARGS": "--offline", "GRADLE_ARGS": "--offline",
                "JOLINK_TEST_REMOVE": "inherited-remove", "JOLINK_TEST_BOTH": "inherited-both", "JOLINK_LOG_LEVEL": "INFO"}
    if os.name == "nt":
        base_env["LOCALAPPDATA"] = str(cache)
    jdk = os.environ.get("JOLINK_TEST_JAVA8_HOME" if system == "maven" else "JOLINK_TEST_JAVA17_HOME")
    if jdk:
        base_env.update(JAVA_HOME=jdk, PATH=jdk + "/bin" + os.pathsep + base_env.get("PATH", ""))
    trace = root / "build-invocations.txt"
    if system == "maven":
        wrapper = root / ("mvnw.cmd" if os.name == "nt" else "mvnw")
        executable = shutil.which("mvn.cmd" if os.name == "nt" else "mvn")
        wrapper.write_text(f'@echo off\r\ncall "{executable}" %*\r\n' if os.name == "nt" else f'#!/bin/sh\nexec "{executable}" "$@"\n')
    else:
        wrapper = root / ("gradlew.bat" if os.name == "nt" else "gradlew")
    lines = wrapper.read_text().splitlines(keepends=True)
    lines.insert(1, f'echo invoked>>"{trace}"\n')
    wrapper.write_text("".join(lines))
    wrapper.chmod(0o755)

    def expected(enabled, inherited, reference):
        path, text = app / ("运行 B" if enabled else "运行 A"), "second 中文 value" if enabled else "first 中文 value"
        assertions = enabled and framework != "testng"  # Explicit argLine -da overrides the default.
        oracle.write_text(f"assertions={str(assertions).lower()}\ndirectory={path.as_posix()}\nvalue={text}\ninherited={inherited}\nreference={reference}\n", encoding="utf-8")

    def class_stamps():
        return {str(p): p.stat().st_mtime_ns for p in cache.rglob("*.class")}

    # Same independent oracle under the native build, before any joLink cache.
    expected(False, "parent-0", "reference-A")
    native_env = {**base_env, "JOLINK_TEST_CONFIG_INPUT": "reference-A", "ORG_GRADLE_PROJECT_runtimeRef": "reference-A", "JOLINK_TEST_INHERITED": "parent-0"}
    options = {}
    settings_file = os.environ.get("JOLINK_TEST_MAVEN_SETTINGS")
    if system == "maven" and settings_file:
        options["maven_settings"] = settings_file
    command = (["mvn", "--offline", "-B", "-Dtest=example.ReadyTest", "-Dsurefire.failIfNoSpecifiedTests=false", "test"]
               if system == "maven" else [str(root / ("gradlew.bat" if os.name == "nt" else "gradlew")), "--offline", "--no-daemon", "test", "--tests", "example.ReadyTest"])
    if system == "maven" and settings_file:
        command.extend(["-s", settings_file])
    if system == "gradle":
        directory.mkdir(parents=True)  # Native Gradle Test expects an existing directory.
    native = subprocess.run(command, cwd=root, env=native_env, capture_output=True, text=True, check=False, timeout=180)
    assert native.returncode == 0, native.stdout + native.stderr
    assert (directory / "runner-created.txt").is_file()
    shutil.rmtree(directory)  # joLink must create its missing Runner directory too.

    async def scenario():
        stamps = None
        for cycle in range(3):
            reference = "reference-A" if not cycle else "reference-B"
            inherited = "parent-" + str(cycle)
            env = {**base_env, "JOLINK_TEST_CONFIG_INPUT": reference, "ORG_GRADLE_PROJECT_runtimeRef": reference, "JOLINK_TEST_INHERITED": inherited}
            expected(bool(cycle), inherited, reference)
            invocation_count = trace.read_text().count("invoked") if trace.exists() else 0
            with temporary_stderr() as log:
                async with open_mcp_session(log, environment=env) as session:
                    async def run(expect_pass=True):
                        result = dict((await session.call_tool("java_fast_test", {"project_path": str(root), "tests": ["example.ReadyTest"], **options})).structuredContent)
                        with anyio.fail_after(180):
                            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                                await anyio.sleep(.2)
                                result = dict((await session.call_tool("java_fast_test", {"action": "result", "test_run_id": result["test_run_id"]})).structuredContent)
                        details = dict((await session.call_tool("java_fast_test", {"action": "result", "test_run_id": result["test_run_id"]})).structuredContent)
                        if expect_pass:
                            assert details.get("passed") is True and details["tests"] == 3, json.dumps(details)
                        else:
                            assert details.get("ok") is False and details.get("error_code") == "TEST_WORKING_DIRECTORY_UNAVAILABLE", json.dumps(details)
                        return result
                    await run()
                    if cycle == 2:
                        assert trace.read_text().count("invoked") == invocation_count, "Inherited-only env change must not invoke the build tool"
                    if stamps is not None:
                        assert class_stamps() == stamps, "Runtime configuration must not invalidate compiled Java"
                    else:
                        stamps = class_stamps()
                        assert stamps
                    warm = await run()
                    assert "bootstrap_ms" not in warm and warm["compiled_source_count"] == 0, warm
                    if not cycle:
                        configure(True)
                        expected(True, inherited, reference)
                        await run()
                        assert class_stamps() == stamps
                    elif cycle == 2 and framework == "junit4" and not multi:
                        saved = config.read_text()
                        (app / "blocked-path").write_text("not a directory")
                        config.write_text(saved.replace("运行 B", "blocked-path"), encoding="utf-8")
                        await run(expect_pass=False)
                        config.write_text(saved, encoding="utf-8")
                        await run()
                        assert class_stamps() == stamps
        assert (app / "运行 A/runner-created.txt").is_file()
        assert (app / "运行 B/runner-created.txt").is_file()
    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_unresolved_working_directory_reports_configuration_error(tmp_path):
    require_real_mcp_java_e2e()
    root, app, pom, _, port = project_fixture(tmp_path, "maven", False)
    pom.write_text(pom.read_text().replace("<version>3.13.0</version>", "<version>3.11.0</version>"))
    xml = ET.parse(pom)
    plugin = next(p for p in xml.getroot().findall("./{*}build/{*}plugins/{*}plugin") if p.findtext("{*}artifactId") == "maven-surefire-plugin")
    ET.SubElement(plugin.find("{*}configuration"), "workingDirectory").text = "${unsetTestDirectory}"
    xml.write(pom, encoding="utf-8")
    test = app / "src/test/java/example/ReadyTest.java"
    test.write_text('''package example; public class ReadyTest {
 @org.junit.Test public void defaults() throws Exception {
  org.junit.Assert.assertEquals(new java.io.File(EXPECTED).getCanonicalPath(),new java.io.File(".").getCanonicalPath());
  boolean enabled=false; assert enabled=true; org.junit.Assert.assertTrue(enabled);
 }
}'''.replace("EXPECTED", json.dumps(app.as_posix())), encoding="utf-8")
    env = {**os.environ, "MAVEN_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache")}
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")
    native = subprocess.run(["mvn", "--offline", "-B", "-Dtest=example.ReadyTest", "test"], cwd=root, env=env,
                            capture_output=True, text=True, check=False, timeout=120)
    assert native.returncode != 0 and "workingDirectory cannot be null" in native.stdout, native.stdout + native.stderr
    async def scenario():
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                result = await finished(session, root, expect_pass=False)
                assert result["error_code"] == "TEST_WORKING_DIRECTORY_UNAVAILABLE", result
                assert not (root / "null").exists()
                try:
                    launched = dict((await session.call_tool("java_application", {
                        "action": "launch", "project_path": str(root), "main_class": "example.App",
                        "ready_port": port, "jdwp_port": reserve_local_port(),
                    })).structuredContent)
                    with anyio.fail_after(120):
                        while launched.get("launch_phase") not in {"runtime_active", "failed"} and launched.get("ok", True):
                            await anyio.sleep(.2)
                            launched = dict((await session.call_tool("java_status", {"action": "status", "details": True})).structuredContent)
                    assert launched.get("startup_state") == "ready", launched
                    assert response(port) == "A", "Unused test configuration must not prevent application launch"
                finally:
                    await session.call_tool("java_application", {"action": "stop"})
    anyio.run(scenario)
