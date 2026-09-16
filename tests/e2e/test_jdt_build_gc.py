"""Real MCP verifies optional GC after FULL, incremental and failed compilation."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import anyio
import pytest
from java_support import open_mcp_session, require_real_mcp_java_e2e, temporary_stderr


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("enabled", [False, True], ids=["gc-off", "gc-on"])
def test_real_mcp_gc_after_build(tmp_path: Path, enabled: bool):
    require_real_mcp_java_e2e()
    home = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not home or shutil.which("mvn") is None:
        pytest.skip("JDK 8 and Maven are required")
    project = tmp_path / "project"
    source = project / "src/main/java/example/Value.java"
    test = project / "src/test/java/example/ValueTest.java"
    source.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    (project / "pom.xml").write_text(
        """<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>build-gc</artifactId><version>1</version>
<properties><maven.compiler.source>1.8</maven.compiler.source><maven.compiler.target>1.8</maven.compiler.target>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
</project>""",
        encoding="utf-8",
    )
    original = (
        "package example; public class Value { public static int get(){return 1;} }"
    )
    source.write_text(original, encoding="utf-8")
    test.write_text(
        "package example; public class ValueTest { @org.junit.Test public void value(){"
        "org.junit.Assert.assertEquals(1,Value.get());} }",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    env = {
        **os.environ,
        "XDG_CACHE_HOME": str(cache),
        "JAVA_HOME": home,
        "PATH": str(Path(home) / "bin") + os.pathsep + os.environ.get("PATH", ""),
        "MAVEN_ARGS": "--offline",
        "JOLINK_LOG_LEVEL": "INFO",
        "JOLINK_JDT_GC_AFTER_BUILD": "1" if enabled else "0",
    }
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(cache)
    log = cache / "jolink-runtime/logs/mcp.log"

    def verify_requests(count):
        text = log.read_text(encoding="utf-8")
        builds = re.findall(r"jdt\.build\.finished build_id=(\w+)", text)
        gcs = re.findall(r"jdt\.gc\.requested build_id=(\w+)", text)
        assert len(builds) == count, text
        assert gcs == (builds if enabled else []), text
        if enabled:
            assert text.count("status=gc_requested") == count
            assert (
                len(re.findall(r"heap_used_bytes=\d+ heap_committed_bytes=\d+", text))
                == count
            )

    async def run_test(session):
        result = await session.call_tool(
            "java_fast_test",
            {
                "action": "run",
                "project_path": str(project),
                "build_system": "maven",
                "tests": ["example.ValueTest#value"],
                "timeout": 30,
            },
        )
        payload = dict(result.structuredContent or {})
        with anyio.fail_after(90):
            while payload.get("status") in {
                "starting",
                "bootstrapping",
                "compiling",
                "running",
            }:
                await anyio.sleep(0.05)
                status = await session.call_tool("java_status", {"action": "status"})
                payload = dict(status.structuredContent["fast_test"])
        return payload

    async def scenario():
        with temporary_stderr() as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                assert (await run_test(session))["passed"]
                verify_requests(1)
                assert (await run_test(session))["passed"]
                verify_requests(1)
                source.write_text(
                    original.replace("return 1", "return 2"), encoding="utf-8"
                )
                failure = await run_test(session)
                assert failure["failed_count"] == 1, failure
                verify_requests(2)
                source.write_text("package example; public class {", encoding="utf-8")
                error = await run_test(session)
                assert error["ok"] is False, error
                verify_requests(3)
                assert (await run_test(session))["ok"] is False
                verify_requests(3)
                source.write_text(original, encoding="utf-8")
                assert (await run_test(session))["passed"]
                verify_requests(4)

        # Saved state survives GC and Worker exit. No-op reopen adds no request.
        with temporary_stderr() as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                assert (await run_test(session))["passed"]
                verify_requests(4)
                source.write_text(original + "\n// after reopen\n", encoding="utf-8")
                assert (await run_test(session))["passed"]
                verify_requests(5)
        lines = [
            line
            for line in log.read_text(encoding="utf-8").splitlines()
            if "jdt.build.finished" in line
        ]
        assert all("actual=INCREMENTAL" in line for line in lines[1:]), lines
        assert not (project / "target").exists()

    anyio.run(scenario)
