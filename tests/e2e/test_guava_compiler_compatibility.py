"""U2: javac controls, standalone product ECJ, and the real Guava MCP path.

Known failures are not support claims. Only the diagnosed compiler differences
are XFAIL; other failures remain failures. A fixed compiler causes strict XPASS
so the limitation and its mark must be revisited.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import anyio
import pytest
from java_support import open_mcp_session, require_real_mcp_java_e2e, temporary_stderr

from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate,
    discover_java8_system_entries,
)


class KnownCompilerDifference(Exception):
    pass


U2 = pytest.mark.xfail(reason="U2: javac/ECJ generic-type compatibility", strict=True,
                       raises=KnownCompilerDifference)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/compiler_compatibility"


@pytest.fixture
def java8_home() -> Path:
    require_real_mcp_java_e2e()
    if not os.environ.get("JOLINK_TEST_JAVA8_HOME"):
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    return Path(os.environ["JOLINK_TEST_JAVA8_HOME"])


def _run(command: list[str], cwd: Path, log: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, check=False, cwd=cwd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=900)
    log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    return completed


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("name,diagnostic", [
    pytest.param("RecursiveBounds", "Type mismatch: cannot convert", marks=U2),
    pytest.param("CapturedCast", "Cannot cast from Map", marks=U2),
    pytest.param("CapturedInvocation", "The method cast(Class<T>, B)", marks=U2),
    pytest.param("InheritedOverloads", "The method complete(", marks=U2),
    ("ConditionalOverload", None),
])
def test_javac_compatible_generics_with_product_ecj(tmp_path, java8_home, name, diagnostic):
    source = FIXTURES / f"{name}.java"
    javac_output = tmp_path / "javac"
    javac_output.mkdir()
    suffix = ".exe" if os.name == "nt" else ""
    baseline = _run([str(java8_home / f"bin/javac{suffix}"), "-source", "8", "-target", "8",
                     "-proc:none", "-d", str(javac_output), str(source)], tmp_path,
                    tmp_path / "javac.log")
    assert baseline.returncode == 0, baseline.stderr
    assert (javac_output / f"{name}.class").is_file()

    candidate = JdtCandidate.load_product()
    compiler = next((candidate.root / "plugins").glob("org.eclipse.jdt.core.compiler.batch_*.jar"))
    runtime = candidate.select_worker_java()
    ecj_output = tmp_path / "ecj"
    ecj_output.mkdir()
    result = _run([str(runtime.executable), "-cp", str(compiler),
                   "org.eclipse.jdt.internal.compiler.batch.Main", "-8", "-proc:none",
                   "-warn:none", "-bootclasspath",
                   os.pathsep.join(map(str, discover_java8_system_entries(java8_home))),
                   "-d", str(ecj_output), str(source)], tmp_path, tmp_path / "ecj.log")
    messages = result.stdout + result.stderr
    if result.returncode and diagnostic and messages.count(". ERROR in ") == 1 and diagnostic in messages:
        raise KnownCompilerDifference(messages)
    assert result.returncode == 0, messages
    assert (ecj_output / f"{name}.class").is_file()


@pytest.mark.mcp_java_e2e
@U2
def test_guava_native_then_cold_mcp(tmp_path, java8_home, monkeypatch):
    checkout = os.environ.get("JOLINK_TEST_GUAVA")
    if not checkout:
        pytest.skip("set JOLINK_TEST_GUAVA to the pinned Guava 31.1 checkout")
    for name in ("native", "mcp"):
        shutil.copytree(checkout, tmp_path / name,
                        ignore=shutil.ignore_patterns(".git", "target", ".idea"))
    monkeypatch.setenv("JAVA_HOME", str(java8_home))
    selectors = ["com.google.common.base.StringsTest", "com.google.common.collect.ImmutableListTest"]
    maven = shutil.which("mvn")
    assert maven is not None
    settings = os.environ.get("JOLINK_TEST_MAVEN_SETTINGS")
    command = [maven, "-B", *(["-s", settings] if settings else []), "-pl", "guava-tests", "-am",
               "-Dtest=StringsTest,ImmutableListTest", "-DfailIfNoTests=false",
               "-Dsurefire.failIfNoSpecifiedTests=false", "test"]
    native = _run(command, tmp_path / "native", tmp_path / "native.log")
    assert native.returncode == 0, native.stdout + native.stderr
    reports = [ET.parse(file).getroot() for file in
               (tmp_path / "native/guava-tests/target/surefire-reports").glob("TEST-*.xml")]
    count = sum(int(report.attrib["tests"]) for report in reports)
    assert count > 0
    assert all(int(report.attrib["failures"]) + int(report.attrib["errors"]) == 0 for report in reports)
    assert not list((tmp_path / "mcp").rglob("target"))

    async def scenario():
        env = {**os.environ, "XDG_CACHE_HOME": str(tmp_path / "cache")}
        request = {"project_path": str(tmp_path / "mcp"), "tests": selectors,
                   "java_home": str(java8_home), "timeout": 30}
        if settings:
            request["maven_settings"] = settings
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                result = dict((await session.call_tool("java_fast_test", request)).structuredContent)
                with anyio.fail_after(900):
                    while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                        await anyio.sleep(1)
                        result = dict((await session.call_tool("java_fast_test", {
                            "action": "result", "test_run_id": result["test_run_id"]})).structuredContent)
                result = dict((await session.call_tool("java_fast_test", {
                    "action": "result", "test_run_id": result["test_run_id"]})).structuredContent)
        known = {
            "TypeTokenSubtypeTest.java": "Type mismatch: cannot convert",
            "ImmutableClassToInstanceMap.java": "Cannot cast from Map",
            "MutableClassToInstanceMap.java": "The method cast(Class<T>, B)",
            "ClosingFuture.java": "The method whenAllComplete(",
        }
        diagnostics = result.get("diagnostics", [])
        if (result.get("error_code") == "JDT_TEST_FULL_COMPILE_FAILED" and diagnostics
                and not result.get("diagnostics_truncated")
                and all(known.get(Path(d["resource"]).name, "<unknown>") in d["message"] for d in diagnostics)):
            raise KnownCompilerDifference(str(result))
        assert result.get("passed") is True and result["tests"] == count, result

    anyio.run(scenario)
