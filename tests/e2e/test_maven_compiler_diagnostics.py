"""Real MCP: standard diagnostics and development debug metadata are consistent."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import anyio
import pytest

from java_support import open_mcp_session, require_real_mcp_java_e2e


CALCULATOR = """package repro;
public class Calculator {
    public int add(int left, int right) {
        int sum = left + right;
        int deprecated = new java.util.Date(100, 0, 1).getDate();
        return sum;
    }
}
"""
TEST = """package repro;
public class SmokeTest {
    @org.junit.Test public void adds() {
        int left = 2;
        int actual = new Calculator().add(left, 3);
        org.junit.Assert.assertEquals(5, actual);
        int deprecated = new java.util.Date(100, 0, 1).getDate();
    }
}
"""
DISABLED = "<debug>false</debug><showWarnings>true</showWarnings><showDeprecation>true</showDeprecation>"
ENABLED = "<debug>true</debug><debuglevel>lines</debuglevel><showWarnings>false</showWarnings><showDeprecation>false</showDeprecation>"


def make_project(tmp_path, configuration="", test_configuration="", *, inherited=False):
    project = tmp_path / "project"
    main = project / "src/main/java/repro/Calculator.java"
    test = project / "src/test/java/repro/SmokeTest.java"
    main.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    main.write_text(CALCULATOR, encoding="utf-8")
    test.write_text(TEST, encoding="utf-8")
    if inherited:
        (tmp_path / "parent.xml").write_text(f"""<project><modelVersion>4.0.0</modelVersion>
<groupId>repro</groupId><artifactId>parent</artifactId><version>1</version><packaging>pom</packaging>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.10.1</version>
<configuration>{configuration}</configuration></plugin></plugins></build></project>""", encoding="utf-8")
        parent = "<parent><groupId>repro</groupId><artifactId>parent</artifactId><version>1</version><relativePath>../parent.xml</relativePath></parent>"
        configuration = ""
    else:
        parent = ""
    execution = ("<executions><execution><id>default-testCompile</id><goals><goal>testCompile</goal></goals>"
                 f"<configuration>{test_configuration}</configuration></execution></executions>"
                 if test_configuration else "")
    (project / "pom.xml").write_text(f"""<project><modelVersion>4.0.0</modelVersion>{parent}
<groupId>repro</groupId><artifactId>diagnostic-debug-policy</artifactId><version>1</version>
<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.1</version><scope>test</scope></dependency></dependencies>
<build><plugins>
<plugin><artifactId>maven-resources-plugin</artifactId><version>3.3.0</version></plugin>
<plugin><artifactId>maven-compiler-plugin</artifactId><version>3.10.1</version><configuration>{configuration}</configuration>{execution}</plugin>
<plugin><artifactId>maven-surefire-plugin</artifactId><version>3.0.0-M7</version></plugin>
</plugins></build></project>""", encoding="utf-8")
    return project, main, test


def environment(tmp_path):
    require_real_mcp_java_e2e()
    home = os.environ.get("JOLINK_TEST_JAVA17_HOME") or os.environ.get("JAVA_HOME")
    if not home or not shutil.which("mvn"):
        pytest.skip("Maven and JOLINK_TEST_JAVA17_HOME/JAVA_HOME are required")
    env = {**os.environ, "JAVA_HOME": home, "MAVEN_ARGS": "--offline", "JOLINK_LOG_LEVEL": "INFO",
           "PATH": str(Path(home) / "bin") + os.pathsep + os.environ.get("PATH", ""),
           "XDG_CACHE_HOME": str(tmp_path / "cache")}
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")
    return env, Path(home)


async def run(session, project, java, transcript, *, passed=True):
    async def call(arguments):
        result = dict((await session.call_tool("java_fast_test", arguments)).structuredContent)
        with transcript.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"arguments": arguments, "result": result}, ensure_ascii=False) + "\n")
        return result

    result = await call(dict(project_path=str(project), tests=["repro.SmokeTest"],
                             java_home=str(java), build_java_home=str(java), timeout=30))
    with anyio.fail_after(180):
        while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
            await anyio.sleep(.25)
            result = await call(dict(action="result", test_run_id=result["test_run_id"]))
    if result.get("passed") is not passed:
        result = await call(dict(action="result", test_run_id=result["test_run_id"]))
    assert result.get("passed") is passed, result
    if passed:
        assert result["ok"] is True and result["tests"] == result["passed_count"] == 1, result
        assert not result.get("diagnostics"), result
    return result


def debug_outputs(tmp_path, java):
    """Inspect class bytes, not just the compiler settings or a successful test."""
    javap = java / "bin" / ("javap.exe" if os.name == "nt" else "javap")
    outputs = {}
    preferences = set()
    for filename, local in (("Calculator.class", "sum"), ("SmokeTest.class", "actual")):
        files = list((tmp_path / "cache").rglob(filename))
        assert len(files) == 1, files
        path = files[0]
        result = subprocess.run([str(javap), "-v", str(path)], capture_output=True,
                                text=True, encoding="utf-8", check=True, timeout=20)
        for attribute in ("SourceFile", "LineNumberTable", "LocalVariableTable"):
            assert re.search(r"^\s*" + attribute + r":", result.stdout, re.MULTILINE), result.stdout
        assert re.search(r"\d+\s+\d+\s+\d+\s+" + local + r"\s+I\b", result.stdout), result.stdout
        outputs[str(path)] = (path.read_bytes(), path.stat().st_mtime_ns)
        preferences.add(path.parents[2] / ".settings/org.eclipse.jdt.core.prefs")
    # The same compiler policy is installed independently in main/test projects.
    assert len(preferences) == 2, preferences
    for path in preferences:
        options = dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
                       if "=" in line and not line.startswith("#"))
        assert all(value not in {"warning", "info"} for key, value in options.items()
                   if key.startswith("org.eclipse.jdt.core.compiler.problem.")), options
    return outputs


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("configuration,test_configuration,inherited", [
    pytest.param("", "", False, id="omitted"),
    pytest.param(DISABLED, "", False, id="debug-false-diagnostics-true"),
    pytest.param(ENABLED, "", False, id="debug-lines-diagnostics-false"),
    pytest.param(DISABLED, ENABLED, False, id="test-execution-override"),
    pytest.param(DISABLED, ENABLED, True, id="parent-and-test-execution-override"),
    pytest.param('<compilerArgs><arg>-g:none</arg><arg>-deprecation</arg><arg>-nowarn</arg></compilerArgs>',
                 "", False, id="equivalent-compiler-arguments"),
])
def test_standard_options_keep_development_debug_information(tmp_path, configuration, test_configuration, inherited):
    env, java = environment(tmp_path)
    project, _, _ = make_project(tmp_path, configuration, test_configuration, inherited=inherited)

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                first = await run(session, project, java, tmp_path / "calls.jsonl")
                assert first["compiled_source_count"] > 0, first
                outputs = debug_outputs(tmp_path, java)
                second = await run(session, project, java, tmp_path / "calls.jsonl")
                assert second["compiled_source_count"] == 0 and "bootstrap_ms" not in second, second
                assert debug_outputs(tmp_path, java) == outputs
    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_debug_diagnostic_policy_survives_config_change_error_and_reopen(tmp_path):
    env, java = environment(tmp_path)
    project, main, test = make_project(tmp_path, DISABLED)
    pom = project / "pom.xml"
    transcript = tmp_path / "calls.jsonl"

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                await run(session, project, java, transcript)
                outputs = debug_outputs(tmp_path, java)
                pom.write_text(pom.read_text(encoding="utf-8").replace(DISABLED, ENABLED), encoding="utf-8")
                refreshed = await run(session, project, java, transcript)
                assert refreshed["compiled_source_count"] == 0, refreshed
                assert debug_outputs(tmp_path, java) == outputs
                for source in (main, test):
                    original = source.read_text(encoding="utf-8")
                    source.write_text(original.replace("int deprecated =", "int deprecated = missingName +"), encoding="utf-8")
                    try:
                        broken = await run(session, project, java, transcript, passed=False)
                        assert broken["ok"] is False and broken["error_count"] > 0, broken
                        assert broken["error_code"] == "JDT_TEST_COMPILE_FAILED", broken
                        assert any("missingName" in entry["message"] for entry in broken["diagnostics"]), broken
                        assert all(entry["severity_name"] == "ERROR" for entry in broken["diagnostics"]), broken
                    finally:
                        source.write_text(original, encoding="utf-8")
                    recovered = await run(session, project, java, transcript)
                    assert recovered["compiled_source_count"] > 0, recovered
                    debug_outputs(tmp_path, java)
        with (tmp_path / "reopen-stderr.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                reopened = await run(session, project, java, transcript)
                assert reopened["compiled_source_count"] == 0, reopened
                debug_outputs(tmp_path, java)
    anyio.run(scenario)
