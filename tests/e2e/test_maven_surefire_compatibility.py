"""Explicit Maven test selection and extra runtime paths, through real MCP."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import anyio
import pytest
from java_support import open_mcp_session, require_real_mcp_java_e2e, temporary_stderr


@pytest.mark.mcp_java_e2e
def test_explicit_classes_with_unused_execution_and_extra_classpath(tmp_path: Path):
    require_real_mcp_java_e2e()
    home = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    maven = shutil.which("mvn")
    if not home or not maven:
        pytest.skip("JDK 8 and Maven required")
    project = tmp_path / "product"
    tests = project / "src/test/java/example"
    main = project / "src/main/java/example/Value.java"
    tests.mkdir(parents=True)
    main.parent.mkdir(parents=True)
    main.write_text(
        "package example; public class Value { public static int get(){return 1;} }"
    )
    data = project / "extra-runtime"
    data.mkdir()
    (data / "fixture.txt").write_text("runtime-only")
    (project / "pom.xml").write_text("""<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>surefire-compat</artifactId><version>1</version>
<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version>
<configuration><includes><include>**/Other.java</include></includes><excludes><exclude>**/Selected*.java</exclude></excludes>
<additionalClasspathElements><additionalClasspathElement>extra-runtime</additionalClasspathElement></additionalClasspathElements></configuration>
<executions><execution><id>unused</id><configuration><runOrder>random</runOrder><argLine>-Dunused=must-not-apply</argLine></configuration></execution></executions>
</plugin><plugin><artifactId>maven-failsafe-plugin</artifactId><configuration>
<includes><include>**/Integration*.java</include></includes></configuration>
</plugin></plugins></build></project>""")
    for name in ("SelectedOne", "SelectedTwo"):
        (tests / f"{name}.java").write_text(f"""package example; public class {name} {{
@org.junit.Test public void selected() throws Exception {{
 org.junit.Assert.assertEquals(1,Value.get());
 org.junit.Assert.assertNull(System.getProperty("unused"));
 try(java.io.InputStream input=getClass().getClassLoader().getResourceAsStream("fixture.txt")) {{
  org.junit.Assert.assertNotNull(input); org.junit.Assert.assertEquals('r',input.read());
 }}
}} }}""")
    (tests / "Other.java").write_text(
        "package example; public class Other { @org.junit.Test public void notSelected(){org.junit.Assert.fail();} }"
    )
    baseline = tmp_path / "native"
    shutil.copytree(project, baseline)
    env = {
        **os.environ,
        "JAVA_HOME": home,
        "PATH": str(Path(home) / "bin") + os.pathsep + os.environ.get("PATH", ""),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "MAVEN_ARGS": "--offline",
    }
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")
    native = subprocess.run(
        [maven, "--offline", "-B", "-Dtest=SelectedOne,SelectedTwo", "test"],
        cwd=baseline,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert native.returncode == 0, native.stdout + native.stderr

    async def scenario():
        async def run(session):
            result = dict(
                (
                    await session.call_tool(
                        "java_application",
                        {
                            "action": "test",
                            "project_path": str(project),
                            "tests": ["example.SelectedOne", "example.SelectedTwo"],
                            "timeout": 20,
                        },
                    )
                ).structuredContent
                or {}
            )
            with anyio.fail_after(90):
                while result.get("status") in {
                    "starting",
                    "bootstrapping",
                    "compiling",
                    "running",
                }:
                    await anyio.sleep(0.05)
                    status = await session.call_tool(
                        "java_status", {"action": "status"}
                    )
                    result = dict(status.structuredContent["fast_test"])
            return result

        with temporary_stderr() as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                cold = await run(session)
                assert cold.get("passed") is True and cold["tests"] == 2, cold
                assert (await run(session))["passed"]
                original = main.read_text()
                main.write_text(original.replace("return 1", "return 2"))
                failed = await run(session)
                assert failed["failed_count"] == 2, failed
                main.write_text(original)
                assert (await run(session))["passed"]
        with temporary_stderr() as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                assert (await run(session))["passed"]
        assert not (project / "target").exists()

    anyio.run(scenario)
