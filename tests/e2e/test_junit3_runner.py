"""Exercise the actual JUnit 3.8 runtime, not JUnit 4's compatibility classes."""
import os
from pathlib import Path
import shutil
import subprocess
import anyio
import pytest
from java_support import call_workflow, open_mcp_session, temporary_stderr
from test_maven_compiler_diagnostics import environment, make_project


@pytest.mark.mcp_java_e2e
def test_legacy_junit_suite_method_and_failure_counts(tmp_path):
    env, java = environment(tmp_path)
    root, _, test = make_project(tmp_path)
    pom = root / "pom.xml"
    pom.write_text(pom.read_text().replace("4.13.1", "3.8.1").replace("3.0.0-M7", "2.7.2"))
    test.write_text("""package repro;
public class SmokeTest extends junit.framework.TestCase {
 public void testPass(){assertEquals(4, 2+2);}
 public void testFailure(){fail("expected assertion");}
 public void testError(){throw new IllegalStateException("expected error");}
 public void helper(){} }""")
    (test.parent / "SuiteTest.java").write_text("""package repro;
public class SuiteTest extends junit.framework.TestCase { public static junit.framework.Test suite() {
 return junit.framework.TestSuite.createTest(SmokeTest.class,"testPass"); }}""")
    native = tmp_path / "native"
    shutil.copytree(root, native)
    settings = os.environ.get("JOLINK_TEST_MAVEN_SETTINGS")
    p = subprocess.run(["mvn", "-B", "--offline", *(["-s",settings] if settings else []),
                        "-Dtest=repro.SuiteTest","test"], cwd=native,env=env,capture_output=True,
                       text=True,errors="replace",timeout=120)
    assert p.returncode == 0 and "Tests run: 1" in p.stdout, p.stdout+p.stderr

    async def check():
        with temporary_stderr() as log:
            async with open_mcp_session(log,environment=env) as session:
                async def run(*selectors):
                    args = dict(project_path=str(root), tests=list(selectors), java_home=str(java),
                                build_java_home=str(java), timeout=30)
                    if settings: args["maven_settings"]=settings
                    result = await call_workflow(session,"java_fast_test",args)
                    assert result.get("test_run_id"),result
                    return dict((await session.call_tool("java_fast_test",dict(action="result", test_run_id=result["test_run_id"]))).structuredContent)
                for sel in ("repro.SuiteTest", "repro.SmokeTest#testPass"):
                    result = await run(sel)
                    assert result.get("passed") and result["framework"]=="junit3" and result["tests"]==1,result
                red = await run("repro.SmokeTest")
                assert not red.get("passed") and (red["tests"],red["passed_count"],red["failed_count"])==(3,1,2), red
                assert {f["exception"] for f in red["failed_tests"]}=={"junit.framework.AssertionFailedError","java.lang.IllegalStateException"},red
                assert not (await run("repro.SmokeTest#helper")).get("passed")
    anyio.run(check)
