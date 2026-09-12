"""Real MCP: runner ordering and independent, persistent main/test builds."""

import os
from pathlib import Path

import anyio
import pytest
from java_support import open_mcp_session, require_real_mcp_java_e2e, temporary_stderr


def environment(tmp_path, *, java11=False):
    require_real_mcp_java_e2e()
    home = os.environ.get(
        "JOLINK_FAST_TEST_JAVA11_HOME" if java11 else "JOLINK_TEST_JAVA8_HOME"
    )
    if not home:
        pytest.skip("set the JDK8/11 test homes")
    return {
        **os.environ,
        "JAVA_HOME": home,
        "PATH": str(Path(home) / "bin") + os.pathsep + os.environ.get("PATH", ""),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "MAVEN_ARGS": "--offline",
    }


async def run(session, project, tests):
    result = dict(
        (
            await session.call_tool(
                "java_application",
                {
                    "action": "test",
                    "project_path": str(project),
                    "tests": tests,
                    "timeout": 30,
                },
            )
        ).structuredContent
        or {}
    )
    with anyio.fail_after(150):
        while result.get("status") in {
            "starting",
            "bootstrapping",
            "compiling",
            "running",
        }:
            await anyio.sleep(0.05)
            result = dict(
                (
                    await session.call_tool("java_status", {"action": "status"})
                ).structuredContent["fast_test"]
            )
    return result


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("framework", ["junit4", "junit5", "testng"])
@pytest.mark.parametrize("order", ["alphabetical", "reversealphabetical"])
def test_mcp_runs_classes_in_configured_order(tmp_path, framework, order):
    env = environment(tmp_path, java11=framework == "testng")
    project = tmp_path / "ordered"
    source = project / "src/test/java/example"
    source.mkdir(parents=True)
    dependency, annotation, assertion = {
        "junit4": (("junit", "junit", "4.13.2"), "org.junit.Test", "org.junit.Assert"),
        "junit5": (
            ("org.junit.jupiter", "junit-jupiter-engine", "5.7.2"),
            "org.junit.jupiter.api.Test",
            "org.junit.jupiter.api.Assertions",
        ),
        "testng": (
            ("org.testng", "testng", "7.11.0"),
            "org.testng.annotations.Test",
            "org.testng.Assert",
        ),
    }[framework]
    group, artifact, version = dependency
    (project / "pom.xml").write_text(f"""<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>ordered</artifactId><version>1</version>
<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>{group}</groupId><artifactId>{artifact}</artifactId><version>{version}</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version><configuration><runOrder>{order}</runOrder></configuration></plugin></plugins></build></project>""")
    expected = ["A", "Z"] if order == "alphabetical" else ["Z", "A"]
    (source / "SuiteLifecycle.java").write_text(
        """package example; public class SuiteLifecycle {
@org.testng.annotations.BeforeSuite public void beforeSuite() {
 org.testng.Assert.assertNull(System.getProperty("suite.started"));
 System.setProperty("suite.started", "yes");
}
@org.testng.annotations.AfterSuite public void afterSuite() {
 org.testng.Assert.assertEquals(System.getProperty("order"), "LAST");
}
}""".replace("LAST", expected[-1])
    ) if framework == "testng" else None
    for name in ("A", "Z"):
        prior = "" if name == expected[0] else expected[0]
        superclass = " extends SuiteLifecycle" if framework == "testng" else ""
        (
            source / f"{name}Test.java"
        ).write_text(f"""package example; public class {name}Test{superclass} {{
@{annotation} public void ordered() {{
 {assertion}.assertTrue("{prior}".equals(System.getProperty("order", "")));
 System.setProperty("order", "{name}");
}} }}""")

    async def scenario():
        for _ in range(2):
            with temporary_stderr() as stderr:
                async with open_mcp_session(stderr, environment=env) as session:
                    # Deliberately submit the opposite order; assertions observe execution, not discovery.
                    result = await run(
                        session,
                        project,
                        ["example." + n + "Test" for n in reversed(expected)],
                    )
                    assert result.get("passed") and result["tests"] == 2, result

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("build_system", ["maven", "gradle"])
def test_main_test_compiler_scopes_incremental_and_reopen(tmp_path, build_system):
    env = environment(tmp_path, java11=True)
    project = tmp_path / "scoped"
    main = project / "src/main/java/example/Main.java"
    test = project / "src/test/java/example/ScopeTest.java"
    main.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    main_original = "package example; public class Main { public static int value(int input){return 1;} }"
    main.write_text(main_original)
    test.write_text("""package example; public class ScopeTest {
@org.junit.Test public void scope() throws Exception {
 org.junit.Assert.assertTrue(" ".isBlank());
 org.junit.Assert.assertFalse(Main.class.getMethod("value",int.class).getParameters()[0].isNamePresent());
 org.junit.Assert.assertTrue(getClass().getMethod("echo",int.class).getParameters()[0].isNamePresent());
 org.junit.Assert.assertEquals(1, Main.value(0));
 org.junit.Assert.assertEquals(52, major(Main.class));
 org.junit.Assert.assertEquals(55, major(getClass()));
}
public int echo(int value){ return value; }
static int major(Class<?> type) throws Exception {
 try(java.io.DataInputStream in=new java.io.DataInputStream(type.getResourceAsStream(type.getSimpleName()+".class"))) {
  in.readInt(); in.readUnsignedShort(); return in.readUnsignedShort();
 }
}
}""")
    if build_system == "maven":
        (project / "pom.xml").write_text("""<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>scoped</artifactId><version>1</version>
<properties><maven.compiler.release>8</maven.compiler.release><maven.compiler.testRelease>11</maven.compiler.testRelease><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version>
<configuration><parameters>false</parameters></configuration>
<executions><execution><id>default-testCompile</id><goals><goal>testCompile</goal></goals><configuration><parameters>true</parameters></configuration></execution></executions>
</plugin></plugins></build></project>""")
    else:
        gradle = os.environ.get("JOLINK_FAST_TEST_GRADLE")
        if not gradle:
            pytest.skip("set JOLINK_FAST_TEST_GRADLE")
        (project / "settings.gradle").write_text("rootProject.name='scoped'")
        wrapper = project / ("gradlew.bat" if os.name == "nt" else "gradlew")
        wrapper.write_text(
            f'@echo off\r\ncall "{gradle}" %*\r\n'
            if os.name == "nt"
            else f'#!/bin/sh\nexec "{gradle}" "$@"\n'
        )
        wrapper.chmod(0o755)
        junit = Path.home() / ".m2/repository/junit/junit/4.13.2/junit-4.13.2.jar"
        hamcrest = (
            Path.home()
            / ".m2/repository/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar"
        )
        (project / "build.gradle").write_text(f"""plugins {{ id 'java' }}
dependencies {{ testImplementation files('{junit.as_posix()}', '{hamcrest.as_posix()}') }}
compileJava.options.release=8
compileTestJava.options.release=11
compileTestJava.options.compilerArgs.add('-parameters')
tasks.withType(JavaCompile).configureEach {{ options.encoding='UTF-8'; doFirst {{ throw new GradleException('business compile must not run') }} }}
test {{ doFirst {{ throw new GradleException('native test must not run') }} }}
""")

    async def scenario():
        for cycle in range(2):
            with temporary_stderr() as stderr:
                async with open_mcp_session(stderr, environment=env) as session:

                    async def check():
                        return await run(session, project, ["example.ScopeTest"])

                    result = await check()
                    assert result.get("passed"), result
                    if cycle:
                        assert result["compiled_source_count"] == 0, result
                        continue
                    main.write_text(main_original.replace("return 1", "return 2"))
                    result = await check()
                    assert (
                        result.get("failed_count") == 1
                        and result["compiled_source_count"] >= 1
                    ), result
                    main.write_text(
                        main_original.replace("return 1", 'return " ".isBlank()?1:0')
                    )
                    result = await check()
                    assert (
                        result.get("error_count", 0) > 0 and result.get("ok") is False
                    ), result
                    main.write_text(main_original)
                    assert (await check())["passed"]
                    original_test = test.read_text()
                    test.write_text(
                        original_test.replace("return value;", "return missing;")
                    )
                    result = await check()
                    assert result.get("ok") is False, result
                    test.write_text(original_test)
                    assert (await check())["passed"]
        assert not (project / "target").exists()

    anyio.run(scenario)
