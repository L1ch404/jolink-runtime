"""Test selection chooses runtime settings without changing the compiled Build World."""

import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET

import anyio
import pytest
from java_support import open_mcp_session, require_real_mcp_java_e2e, temporary_stderr
from test_build_configuration_mcp import project_fixture


def native_tests(root, env, goals, properties):
    command = [
        shutil.which("mvn.cmd" if os.name == "nt" else "mvn"),
        "--offline",
        "-B",
        *properties,
    ]
    if os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
        command += ["-s", os.environ["JOLINK_TEST_MAVEN_SETTINGS"]]
    completed = subprocess.run(
        [*command, *goals],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def environment(tmp_path):
    require_real_mcp_java_e2e()
    java = os.environ.get("JOLINK_TEST_JAVA17_HOME") or os.environ.get("JAVA_HOME")
    if not java:
        pytest.skip("set JAVA_HOME or JOLINK_TEST_JAVA17_HOME")
    env = {
        **os.environ,
        "JAVA_HOME": java,
        "PATH": java + "/bin" + os.pathsep + os.environ["PATH"],
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "MAVEN_ARGS": "--offline",
    }
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")
    return env


async def run(session, project, selectors, **options):
    args = {"project_path": str(project), "tests": selectors, **options}
    if os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
        args["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]
    result = dict((await session.call_tool("java_fast_test", args)).structuredContent)
    with anyio.fail_after(180):
        while result.get("status") in {
            "starting",
            "bootstrapping",
            "compiling",
            "running",
        }:
            await anyio.sleep(0.2)
            result = dict(
                (
                    await session.call_tool(
                        "java_fast_test",
                        {"action": "result", "test_run_id": result["test_run_id"]},
                    )
                ).structuredContent
            )
    return dict(
        (
            await session.call_tool(
                "java_fast_test",
                {"action": "result", "test_run_id": result["test_run_id"]},
            )
        ).structuredContent
    )


@pytest.mark.mcp_java_e2e
def test_surefire_failsafe_environments_and_cache(tmp_path):
    env = environment(tmp_path)
    root, _, pom, _, _ = project_fixture(tmp_path, "maven", False)
    pom.write_text(
        pom.read_text().replace(
            "<version>3.13.0</version>", "<version>3.11.0</version>"
        )
    )
    expected_file = root / "test.properties"
    expected_file.write_text("fromFile=present\noverride=file\nlegacy=file\n")
    xml = ET.parse(pom)
    plugins = xml.getroot().find("./{*}build/{*}plugins")
    surefire = next(
        p for p in plugins if p.findtext("{*}artifactId") == "maven-surefire-plugin"
    )
    surefire.remove(surefire.find("{*}configuration"))
    configs = {}
    for name, plugin, goal in (
        ("unit", surefire, "test"),
        ("it", ET.SubElement(plugins, "plugin"), "integration-test"),
    ):
        if name == "it":
            ET.SubElement(plugin, "artifactId").text = "maven-failsafe-plugin"
            ET.SubElement(plugin, "version").text = "3.2.5"
        settings = ET.SubElement(plugin, "configuration")
        ET.SubElement(
            settings, "argLine"
        ).text = f"-Droute={name} -Dmarker=parent @{{extra.test.args}}"
        variables = ET.SubElement(settings, "systemPropertyVariables")
        ET.SubElement(variables, "override").text = "configured"
        legacy = ET.SubElement(ET.SubElement(settings, "systemProperties"), "property")
        ET.SubElement(legacy, "name").text = "legacy"
        ET.SubElement(legacy, "value").text = "old"
        ET.SubElement(
            settings, "systemPropertiesFile"
        ).text = "${project.basedir}/test.properties"
        ET.SubElement(settings, "workingDirectory").text = (
            "${project.basedir}/" + name + " directory"
        )
        ET.SubElement(
            ET.SubElement(settings, "environmentVariables"), "TEST_ENV"
        ).text = name
        ET.SubElement(settings, "parallel").text = "methods"
        ET.SubElement(settings, "threadCount").text = "8"
        ET.SubElement(settings, "enableAssertions").text = (
            "true" if name == "unit" else "false"
        )
        execution = ET.SubElement(ET.SubElement(plugin, "executions"), "execution")
        ET.SubElement(execution, "id").text = (
            "default-test" if name == "unit" else "integration"
        )
        ET.SubElement(ET.SubElement(execution, "goals"), "goal").text = goal
        override = ET.SubElement(execution, "configuration")
        ET.SubElement(
            override, "argLine"
        ).text = f"-Droute={name} -Dmarker=execution @{{extra.test.args}}"
        configs[name] = settings
    props = xml.getroot().find("{*}properties")
    ET.SubElement(props, "extra.test.args").text = '-Dlate="two words"'
    xml.write(pom, encoding="utf-8")
    (root / "pids").mkdir()
    for cls, name in (
        ("UnitEnvTest", "unit"),
        ("AnotherTest", "unit"),
        ("EnvIT", "it"),
    ):
        (root / f"src/test/java/example/{cls}.java").write_text(
            """package example;
public class CLASS {
 @org.junit.Test public void verify() throws Exception {
  org.junit.Assert.assertEquals("NAME",System.getProperty("route"));
  org.junit.Assert.assertEquals("NAME",System.getenv("TEST_ENV"));
  org.junit.Assert.assertEquals("execution",System.getProperty("marker"));
  org.junit.Assert.assertEquals("two words",System.getProperty("late"));
  org.junit.Assert.assertEquals("present",System.getProperty("fromFile"));
  org.junit.Assert.assertEquals("configured",System.getProperty("override"));
  org.junit.Assert.assertEquals("file",System.getProperty("legacy"));
  org.junit.Assert.assertEquals("NAME directory",new java.io.File(".").getCanonicalFile().getName());
  boolean enabled=false; assert enabled=true;
  org.junit.Assert.assertEquals(EXPECT_ASSERTIONS,enabled);
  java.nio.file.Files.write(java.nio.file.Paths.get(PID),java.lang.management.ManagementFactory.getRuntimeMXBean().getName().getBytes("UTF-8"));
 }
}""".replace("CLASS", cls)
            .replace("NAME", name)
            .replace("EXPECT_ASSERTIONS", str(name == "unit").lower())
            .replace("PID", json.dumps((root / f"pids/{cls}").as_posix()))
        )

    native_tests(
        root,
        env,
        ["test-compile", "surefire:test@default-test"],
        ["-Dtest=example.UnitEnvTest,example.AnotherTest"],
    )
    native_tests(
        root,
        env,
        ["failsafe:integration-test@integration", "failsafe:verify@integration"],
        ["-Dit.test=example.EnvIT"],
    )
    shutil.rmtree(root / "target")

    def stamps():
        return {
            str(p): p.stat().st_mtime_ns for p in (tmp_path / "cache").rglob("*.class")
        }

    async def scenario():
        baseline = None
        for cycle in range(2):
            with temporary_stderr() as log:
                async with open_mcp_session(log, environment=env) as session:
                    for selectors in (
                        ["example.UnitEnvTest#verify"],
                        ["example.EnvIT"],
                        ["example.UnitEnvTest", "example.AnotherTest", "example.EnvIT"],
                    ):
                        result = await run(session, root, selectors)
                        assert result.get("passed") and result["tests"] == len(
                            selectors
                        ), json.dumps(result)
                        assert result["not_applied_test_settings"] == [
                            "parallel",
                            "threadCount",
                        ]
                        if baseline is not None:
                            assert (
                                result["compiled_source_count"] == 0
                                and stamps() == baseline
                            )
                        baseline = stamps()
                    assert (root / "pids/UnitEnvTest").read_text() == (
                        root / "pids/AnotherTest"
                    ).read_text()
                    assert (root / "pids/UnitEnvTest").read_text() != (
                        root / "pids/EnvIT"
                    ).read_text()
                    assert len(result["test_execution_ids"]) == 2
                    if not cycle:
                        # An invalid but unrelated integration-test environment cannot veto a unit test.
                        saved = pom.read_text()
                        (root / "it directory").rename(root / "old-it-directory")
                        (root / "it directory").write_text("not a directory")
                        unit = await run(session, root, ["example.UnitEnvTest"])
                        assert unit.get("passed"), unit
                        failed = await run(session, root, ["example.EnvIT"])
                        assert (
                            failed.get("error_code")
                            == "TEST_WORKING_DIRECTORY_UNAVAILABLE"
                        ), failed
                        (root / "it directory").unlink()
                        assert (await run(session, root, ["example.EnvIT"]))["passed"]
                        assert pom.read_text() == saved and stamps() == baseline
                        # Properties file content is a runtime configuration input, not a Java change.
                        properties_before = expected_file.read_text()
                        expected_file.write_text(
                            properties_before.replace("present", "changed")
                        )
                        failed = await run(session, root, ["example.UnitEnvTest"])
                        assert failed.get("failed_test_count") == 1, failed
                        expected_file.write_text(properties_before)
                        assert (await run(session, root, ["example.UnitEnvTest"]))[
                            "passed"
                        ]
                        assert stamps() == baseline

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("framework", ["junit4", "junit5", "testng"])
def test_framework_filters_are_executed_not_only_exported(tmp_path, framework):
    env = environment(tmp_path)
    root, _, pom, _, _ = project_fixture(tmp_path, "maven", False)
    pom.write_text(
        pom.read_text().replace(
            "<version>3.13.0</version>", "<version>3.11.0</version>"
        )
    )
    xml = ET.parse(pom)
    if framework != "junit4":
        dependency = ET.SubElement(xml.getroot().find("{*}dependencies"), "dependency")
        for k, v in zip(
            ("groupId", "artifactId", "version", "scope"),
            ("org.junit.jupiter", "junit-jupiter", "5.8.2", "test")
            if framework == "junit5"
            else ("org.testng", "testng", "7.5", "test"),
        ):
            ET.SubElement(dependency, k).text = v
    plugin = next(
        p
        for p in xml.getroot().findall("./{*}build/{*}plugins/{*}plugin")
        if p.findtext("{*}artifactId") == "maven-surefire-plugin"
    )
    config = plugin.find("{*}configuration")
    ET.SubElement(config, "groups").text = (
        "example.FilterTest$Fast" if framework == "junit4" else "fast"
    )
    ET.SubElement(config, "excludedGroups").text = (
        "example.FilterTest$Blocked" if framework == "junit4" else "blocked"
    )
    if framework == "junit5":
        ET.SubElement(
            ET.SubElement(config, "includeJUnit5Engines"), "engine"
        ).text = "junit-jupiter"
        ET.SubElement(
            ET.SubElement(config, "properties"), "configurationParameters"
        ).text = "fixture.option = 中文 value"
    xml.write(pom, encoding="utf-8")
    if framework == "junit4":
        annotations = (
            "@org.junit.Test @org.junit.experimental.categories.Category(Fast.class)",
            "@org.junit.Test @org.junit.experimental.categories.Category(Slow.class)",
            "@org.junit.Test @org.junit.experimental.categories.Category({Fast.class,Blocked.class})",
        )
    elif framework == "junit5":
        annotations = (
            '@org.junit.jupiter.api.Test @org.junit.jupiter.api.Tag("fast")',
            '@org.junit.jupiter.api.Test @org.junit.jupiter.api.Tag("slow")',
            '@org.junit.jupiter.api.Test @org.junit.jupiter.api.Tag("fast") @org.junit.jupiter.api.Tag("blocked")',
        )
    else:
        annotations = (
            '@org.testng.annotations.Test(groups="fast")',
            '@org.testng.annotations.Test(groups="slow")',
            '@org.testng.annotations.Test(groups={"fast","blocked"})',
        )
    source = "package example; public class FilterTest {public interface Fast{} public interface Slow{} public interface Blocked{}\n"
    source += (
        annotations[0] + ' public void good(){System.out.println("selected-framework-output");org.junit.Assert.assertTrue(true);}\n'
    )
    source += (
        annotations[1]
        + ' public void slow(){org.junit.Assert.fail("slow must be filtered");}\n'
    )
    source += (
        annotations[2]
        + ' public void blocked(){org.junit.Assert.fail("blocked must be filtered");}\n}'
    )
    if framework == "junit5":
        source = source.replace(
            "public class FilterTest",
            "@org.junit.jupiter.api.extension.ExtendWith(FilterTest.Verify.class) public class FilterTest",
        )
        source = (
            source[:-1]
            + """public static class Verify implements org.junit.jupiter.api.extension.BeforeAllCallback {
 public void beforeAll(org.junit.jupiter.api.extension.ExtensionContext context){org.junit.Assert.assertEquals("中文 value",context.getConfigurationParameter("fixture.option").orElse("missing"));}}
}"""
        )
    (root / "src/test/java/example/FilterTest.java").write_text(source)
    native_tests(root, env, ["test"], ["-Dtest=example.FilterTest"])
    shutil.rmtree(root / "target")

    async def scenario():
        for _ in range(2):
            with temporary_stderr() as log:
                async with open_mcp_session(log, environment=env) as session:
                    result = await run(session, root, ["example.FilterTest"])
                    assert (
                        result.get("passed") is True and result["passed_count"] == 1
                    ), result
                    assert "selected-framework-output" in result["output"], result

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_gradle_platform_filters_use_shared_runner(tmp_path):
    env = environment(tmp_path)
    root, _, config, _, _ = project_fixture(tmp_path, "gradle", False)
    env["GRADLE_ARGS"] = "--offline"
    config.write_text(
        config.read_text()
        + """
repositories { mavenLocal() }
dependencies { testImplementation 'org.junit.jupiter:junit-jupiter:5.8.2' }
test { useJUnitPlatform { includeTags 'fast'; excludeTags 'blocked'; includeEngines 'junit-jupiter' } }
"""
    )
    (root / "src/test/java/example/PlatformTest.java").write_text("""package example;
public class PlatformTest {
 @org.junit.jupiter.api.Test @org.junit.jupiter.api.Tag("fast") void good() {System.out.println("gradle-platform-output");}
 @org.junit.jupiter.api.Test @org.junit.jupiter.api.Tag("slow") void slow() {throw new AssertionError("must exclude slow");}
 @org.junit.jupiter.api.Test @org.junit.jupiter.api.Tag("fast") @org.junit.jupiter.api.Tag("blocked") void blocked() {throw new AssertionError("must exclude blocked");}
}""")
    native = subprocess.run(
        [
            str(root / ("gradlew.bat" if os.name == "nt" else "gradlew")),
            "--offline",
            "--no-daemon",
            "test",
            "--tests",
            "example.PlatformTest",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert native.returncode == 0, native.stdout + native.stderr
    shutil.rmtree(root / "build")

    async def scenario():
        for _ in range(2):
            with temporary_stderr() as log:
                async with open_mcp_session(log, environment=env) as session:
                    result = await run(session, root, ["example.PlatformTest"])
                    assert result.get("passed") and result["tests"] == 1, result
                    assert "gradle-platform-output" in result["output"], result

    anyio.run(scenario)
