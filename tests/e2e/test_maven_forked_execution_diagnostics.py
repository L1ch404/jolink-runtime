"""Only the compiler actually executing a goal can decide which warnings are valid."""
import os
from pathlib import Path
import shutil
import subprocess
from xml.sax.saxutils import escape

import anyio
import pytest

from java_support import call_workflow, open_mcp_session, require_real_mcp_java_e2e, reserve_local_port


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("goal", ["compile", "testCompile"])
def test_maven_host_lint_not_recognized_by_forked_javac8(tmp_path, goal):
    require_real_mcp_java_e2e()
    host = os.getenv("JOLINK_TEST_JAVA17_HOME") or os.getenv("JOLINK_TEST_JAVA11_HOME")
    fork = os.getenv("JOLINK_TEST_JAVA8_HOME")
    mvn = os.getenv("JOLINK_TEST_MAVEN") or shutil.which("mvn")
    if not (host and fork and mvn):
        pytest.skip("requires Maven, a JDK 11/17 Maven host and JOLINK_TEST_JAVA8_HOME")
    javac = Path(fork) / "bin" / ("javac.exe" if os.name == "nt" else "javac")
    project = tmp_path / "project"
    for name, scope, body in [("App", "main", "public static void main(String[] args) {}"),
                              ("AppTest", "test", "@org.junit.Test public void ok() {}")]:
        source = project / f"src/{scope}/java/example/{name}.java"
        source.parent.mkdir(parents=True)
        source.write_text(f"package example; public class {name} {{ {body} }}", encoding="utf-8")
    (project / "pom.xml").write_text(f"""<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>forked-diagnostics</artifactId><version>1</version>
<properties><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId>
<version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.10.1</version>
<configuration><source>8</source><target>8</target><fork>true</fork>
<executable>{escape(str(javac))}</executable></configuration>
<executions><execution><id>default-{goal}</id><configuration>
<compilerArgs><arg>-Xlint:module</arg></compilerArgs></configuration></execution>
<execution><id>again</id><goals><goal>{goal}</goal></goals></execution></executions>
</plugin><plugin><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version></plugin>
</plugins></build></project>""", encoding="utf-8")
    native = tmp_path / "native"
    shutil.copytree(project, native)
    env = {**os.environ, "JAVA_HOME": host, "PATH": str(Path(host) / "bin") + os.pathsep + os.environ["PATH"],
           "XDG_CACHE_HOME": str(tmp_path / "cache"), "MAVEN_ARGS": "--offline"}
    if os.name == "nt":
        env["LOCALAPPDATA"] = env["XDG_CACHE_HOME"]
    settings = os.getenv("JOLINK_TEST_MAVEN_SETTINGS")
    outcome = subprocess.run([mvn, "--offline", "-B", *(["-s", settings] if settings else []), "test-compile"],
                             cwd=native, env=env, capture_output=True, text=True, errors="replace", timeout=120)
    log = outcome.stdout + outcome.stderr
    (tmp_path / "native.log").write_text(log, encoding="utf-8")
    assert outcome.returncode != 0 and any(
        "javac:" in line and "-Xlint:module" in line for line in log.splitlines()
    ), log

    async def verify():
        common = dict(project_path=str(project), build_java_home=host, java_home=host, timeout=0)
        if settings:
            common["maven_settings"] = settings
        with (tmp_path / "mcp.log").open("w", encoding="utf-8") as err:
            async with open_mcp_session(err, environment=env) as session:
                result = await call_workflow(session, "java_fast_test", dict(common, tests=["example.AppTest"]))
                details = dict((await session.call_tool("java_fast_test", {
                    "action": "result", "test_run_id": result["test_run_id"]})).structuredContent)
                def ambiguous(error):
                    assert error.get("error_code") == "MAVEN_COMPILER_EXECUTION_AMBIGUOUS", error
                    assert set(error["execution_ids"]) == {f"default-{goal}", "again"}
                    assert error["scope"] == ("main" if goal == "compile" else "test")
                ambiguous(details)
                if goal == "compile":
                    launched = await call_workflow(session, "java_application", dict(
                        common, action="launch", main_class="example.App", jdwp_port=reserve_local_port()))
                    ambiguous(launched.get("launch_error", launched))
                    assert not launched.get("pid")
    anyio.run(verify)
