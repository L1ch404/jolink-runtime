"""Real MCP verifies the 10-round policy independently of diagnostic verbosity."""

from __future__ import annotations

import os
from pathlib import Path
import shutil

import anyio
import pytest

from java_support import open_mcp_session, require_real_mcp_java_e2e, temporary_stderr


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("level", [None, "INFO", "DEBUG", "ERROR", "OFF"])
def test_real_mcp_loop_limit_and_log_levels(tmp_path: Path, level: str | None):
    require_real_mcp_java_e2e()
    home = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not home or shutil.which("mvn") is None:
        pytest.skip("JDK 8 and Maven are required")
    project = tmp_path / "project"
    sources = project / "src/main/java/example"
    tests = project / "src/test/java/example"
    sources.mkdir(parents=True)
    tests.mkdir(parents=True)
    (project / "pom.xml").write_text(
        """<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>build-logging</artifactId><version>1</version>
<properties><maven.compiler.source>1.8</maven.compiler.source><maven.compiler.target>1.8</maven.compiler.target>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
</project>""",
        encoding="utf-8",
    )
    for prefix, length in (("Short", 9), ("Long", 12)):
        for index in range(length):
            expression = "1" if index == 0 else f"{prefix}{index - 1}.VALUE"
            (sources / f"{prefix}{index}.java").write_text(
                f"package example; public class {prefix}{index} {{ public static final int VALUE={expression}; }}",
                encoding="utf-8",
            )
    expected = project / "expected.txt"
    expected.write_text("1,1", encoding="utf-8")
    (tests / "ChainTest.java").write_text(
        """package example;
public class ChainTest {
 @org.junit.Test public void value() throws Exception {
  String[] expected = new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get("expected.txt")), "UTF-8").split(",");
  org.junit.Assert.assertEquals(Integer.parseInt(expected[0]), Short8.VALUE);
  org.junit.Assert.assertEquals(Integer.parseInt(expected[1]), Long11.VALUE);
 }
}""",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    env = {
        **os.environ,
        "XDG_CACHE_HOME": str(cache),
        "JAVA_HOME": home,
        "PATH": str(Path(home) / "bin") + os.pathsep + os.environ.get("PATH", ""),
        "MAVEN_ARGS": "--offline",
    }
    env.pop("JOLINK_LOG_LEVEL", None)
    if level is not None:
        env["JOLINK_LOG_LEVEL"] = level
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(cache)

    async def scenario():
        with temporary_stderr() as stderr:
            async with open_mcp_session(stderr, environment=env) as session:

                async def run_test():
                    result = await session.call_tool(
                        "java_fast_test",
                        {
                            "action": "run",
                            "project_path": str(project),
                            "build_system": "maven",
                            "tests": ["example.ChainTest#value"],
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
                            status = await session.call_tool(
                                "java_status", {"action": "status"}
                            )
                            payload = status.structuredContent["fast_test"]
                    assert payload.get("passed") is True, payload
                    assert payload["tests"] == 1
                    return payload

                await run_test()
                log = cache / "jolink-runtime/logs/mcp.log"
                status = (
                    await session.call_tool("java_status", {"action": "status"})
                ).structuredContent
                assert status["server_diagnostics"]["level"] == (level or "WARNING")
                (sources / "Short0.java").write_text(
                    "package example; public class Short0 { public static final int VALUE=2; }",
                    encoding="utf-8",
                )
                expected.write_text("2,1", encoding="utf-8")
                short = await run_test()
                # The former 5-round policy fell back to FULL for this chain.
                assert short["compiled_source_count"] < 22
                assert not any(
                    "/Long" in name for name in short["compiled_source_units"]
                )

                (sources / "Long0.java").write_text(
                    "package example; public class Long0 { public static final int VALUE=2; }",
                    encoding="utf-8",
                )
                expected.write_text("2,2", encoding="utf-8")
                long = await run_test()
                assert long["compiled_source_count"] == 22
                worker_logs = list(
                    cache.glob(
                        "jolink-runtime/fast-test/*/workspace/*/worker.stderr.log"
                    )
                )
                assert len(worker_logs) == 1
                worker_text = worker_logs[0].read_text(encoding="utf-8")
                assert ("JavaBuilder: Starting build" in worker_text) is (
                    level == "DEBUG"
                )
                text = log.read_text(encoding="utf-8") if log.exists() else ""
                if level in {"INFO", "DEBUG"}:
                    assert "changed_sources=1" in text
                    assert (
                        "requested=INCREMENTAL actual=FULL full_fallback=True" in text
                    )
                    assert "INCREMENTAL_LOOP_LIMIT_EXCEEDED" in text
                    assert '"incremental_loop_limit": 10' in text
                    properties = (worker_logs[0].parent / "modules.properties").read_text()
                    main_project = next(line.split("=", 1)[1].split(",")[0]
                        for line in properties.splitlines() if line.startswith("modules="))
                    assert f'"project": "{main_project}"' in text
                    assert "jdt.workspace.saved" in text
                elif level is None:
                    assert "full_fallback=True" in text
                    assert "jdt.build.started" not in text
                    assert "jdt.workspace.saved" not in text
                    assert '"source": "disabled"' in text
                else:
                    assert "jdt.build.finished" not in text
                    if level == "OFF":
                        assert not log.exists()
                        assert status["server_diagnostics"]["status"] == "disabled"

                (sources / "Long11.java").write_text(
                    "package example; public class Long11 { public static final int VALUE=3; }",
                    encoding="utf-8",
                )
                expected.write_text("2,3", encoding="utf-8")
                normal = await run_test()
                assert normal["compiled_source_count"] < 22
                if level in {"INFO", "DEBUG"}:
                    last = [
                        line
                        for line in log.read_text(encoding="utf-8").splitlines()
                        if "jdt.build.finished" in line
                    ][-1]
                    assert "actual=INCREMENTAL full_fallback=False" in last
                    assert "INCREMENTAL_LOOP_LIMIT_EXCEEDED" not in last
                before = log.read_bytes() if log.exists() else b""
                assert (await run_test())["compiled_source_count"] == 0
                after = log.read_bytes() if log.exists() else b""
                assert after.count(b"jdt.build.started") == before.count(
                    b"jdt.build.started"
                )

    anyio.run(scenario)
