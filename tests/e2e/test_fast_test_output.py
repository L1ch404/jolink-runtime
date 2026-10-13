"""Real console output through MCP: encoding, errors, live reads and execution groups."""

import json
import os
import time
from pathlib import Path

import anyio
import pytest
from java_support import open_mcp_session, temporary_stderr
from test_mcp_runtime_preparation import make_test_project
from test_test_execution_config import environment, run

from jolink_runtime.launch.fast_test_manager import FastTestManager

SOURCE = """package example;
public class OutputTest {
 @org.junit.Test public void prints() {
  System.out.println("开始验证：40 + 2");
  System.err.println("stderr：这是一条测试提示");
  org.junit.Assert.assertEquals(42, 40 + 2);
  System.out.println("验证完成：结果为 42");
 }
 @org.junit.Test public void failed() {
  System.out.println("断言之前的业务输出");
  org.junit.Assert.assertEquals("expected", "actual");
 }
 @org.junit.Test public void exits() {
  System.err.println("before System.exit"); System.err.flush(); System.exit(7);
 }
 @org.junit.Test public void manyLines() {
  for(int i=0;i<100;i++) System.out.println("line-" + i);
 }
 @org.junit.Test public void longLine() {
  for(int i=0;i<30000;i++) System.out.print('x');
  System.out.println(); System.out.println("after-long-line");
 }
 @org.junit.Test public void slow() throws Exception {
  System.out.println("live-start"); System.out.flush();
  while(true) Thread.sleep(100);
 }
 @org.junit.Test public void quiet() {}
 @org.junit.Test public void rawValues() {
  System.out.println("password=demo-only,not-a-real-credential");
  System.out.println("Authorization=Bearer example-only");
 }
 @org.junit.Test public void tooLoud() {
  String line = new String(new char[1024]).replace('\\0', 'x');
  for(int i=0;i<6000;i++) System.out.println(line);
 }
}"""


def fixture(tmp_path, charset):
    root = make_test_project(tmp_path)
    source = root / "src/test/java/example/OutputTest.java"
    source.write_text(SOURCE, encoding="utf-8")
    pom = root / "pom.xml"
    pom.write_text(
        pom.read_text()
        .replace("<version>3.13.0</version>", "<version>3.11.0</version>")
        .replace(
            "</plugins>",
            "<plugin><artifactId>maven-surefire-plugin</artifactId>"
            "<version>3.2.5</version><configuration><argLine>"
            + (f"-Dfile.encoding={charset}" if charset else "")
            + "</argLine></configuration></plugin></plugins>",
        ),
        encoding="utf-8",
    )
    return root, source


def assert_console_file(path, text):
    # file.encoding does not select PrintStream encoding on every JDK.
    # Check the actual unmodified bytes, independently of the MCP decoder.
    raw = path.read_bytes()
    assert raw in {
        value.encode(encoding)
        for value in (text, text.replace("\n", "\r\n"))
        for encoding in ("utf-8", "gbk")
    }


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize(
    "jdk_variable,charset",
    [
        ("JOLINK_TEST_JAVA8_HOME", "UTF-8"),
        ("JOLINK_TEST_JAVA8_HOME", "GBK"),
        ("JOLINK_TEST_JAVA17_HOME", "GBK"),
        ("JOLINK_TEST_JAVA21_HOME", ""),
    ],
)
def test_result_console_output(tmp_path, jdk_variable, charset):
    env = environment(tmp_path)
    java = os.environ.get(jdk_variable)
    if not java:
        pytest.skip(f"set {jdk_variable}")
    root, source = fixture(tmp_path, charset)

    async def scenario():
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:

                async def selected(method):
                    return await run(
                        session, root, ["example.OutputTest#" + method], java_home=java
                    )

                result = await selected("prints")
                expected = (
                    "开始验证：40 + 2\nstderr：这是一条测试提示\n验证完成：结果为 42\n"
                )
                assert result["passed"] is True and result["tests"] == 1, result
                assert result["output"].replace("\r\n", "\n") == expected
                assert result["output_truncated"] is False
                print(json.dumps({"demo_result": result}, ensure_ascii=False))
                run_id = result["test_run_id"]
                args = {"action": "result", "test_run_id": run_id}
                assert (
                    dict(
                        (
                            await session.call_tool("java_fast_test", args)
                        ).structuredContent
                    )
                    == result
                )
                assert len(result["log_files"]) == 1
                log_file = Path(result["log_files"][0])
                assert log_file.is_absolute() and log_file.is_file()
                assert_console_file(log_file, expected)
                status = dict(
                    (
                        await session.call_tool("java_status", {"action": "status"})
                    ).structuredContent
                )
                assert not {"output", "log_files"} & status["fast_test"].keys()
                request = {
                    "project_path": str(root),
                    "tests": ["example.OutputTest#prints"],
                    "java_home": java,
                }
                if os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
                    request["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]
                summary = dict(
                    (
                        await session.call_tool("java_fast_test", request)
                    ).structuredContent
                )
                assert "output" not in summary and summary["passed"] is True, summary
                assert "log_files" not in summary

                raw = await selected("rawValues")
                assert "password=demo-only,not-a-real-credential" in raw["output"]
                assert "Authorization=Bearer example-only" in raw["output"]
                assert "<redacted" not in raw["output"]

                failed = await selected("failed")
                assert (
                    failed["failed_test_count"] == 1
                    and "断言之前的业务输出" in failed["output"]
                )
                assert failed["failed_tests"]
                exited = await selected("exits")
                assert (
                    exited["ok"] is False and "before System.exit" in exited["output"]
                ), exited
                lines = await selected("manyLines")
                assert lines["output"].splitlines() == [
                    f"line-{i}" for i in range(50, 100)
                ]
                assert lines["output_truncated"] is True
                all_lines = Path(lines["log_files"][0]).read_text(encoding="ascii")
                assert all_lines.splitlines() == [f"line-{i}" for i in range(100)]
                long = await selected("longLine")
                assert (
                    long["output"].endswith("after-long-line\n")
                    and long["output_truncated"]
                )
                assert len(long["output"].encode()) <= 16 * 1024
                quiet = await selected("quiet")
                assert (
                    quiet["passed"]
                    and quiet["output"] == ""
                    and not quiet["output_truncated"]
                )

                pending = dict(
                    (
                        await session.call_tool(
                            "java_fast_test",
                            {
                                **request,
                                "tests": ["example.OutputTest#slow"],
                                "timeout": 0,
                            },
                        )
                    ).structuredContent
                )
                live_args = {"action": "result", "test_run_id": pending["test_run_id"]}
                with anyio.fail_after(30):
                    while True:
                        live = dict(
                            (
                                await session.call_tool("java_fast_test", live_args)
                            ).structuredContent
                        )
                        if "live-start" in live.get("output", ""):
                            break
                        await anyio.sleep(0.1)
                assert live["status"] == "running"
                await session.call_tool(
                    "java_fast_test", {**live_args, "action": "cancel"}
                )
                with anyio.fail_after(15):
                    while True:
                        cancelled = dict(
                            (
                                await session.call_tool("java_fast_test", live_args)
                            ).structuredContent
                        )
                        if cancelled["status"] == "cancelled":
                            break
                        await anyio.sleep(0.1)
                assert (
                    cancelled["error_code"] == "TEST_CANCELLED"
                    and "live-start" in cancelled["output"]
                )

                source.write_text(SOURCE + "\ninvalid java\n", encoding="utf-8")
                try:
                    compile_error = await selected("prints")
                    assert (
                        compile_error["diagnostics"] and "output" not in compile_error
                    )
                finally:
                    source.write_text(SOURCE, encoding="utf-8")
                assert (await selected("prints"))["output"].replace(
                    "\r\n", "\n"
                ) == expected

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_output_from_mixed_and_failed_execution_groups(tmp_path):
    env = environment(tmp_path)
    root, _ = fixture(tmp_path, "UTF-8")
    pom = root / "pom.xml"
    pom.write_text(
        pom.read_text().replace(
            "</plugins>",
            """<plugin>
<artifactId>maven-failsafe-plugin</artifactId><version>3.2.5</version>
<configuration><argLine>-Dfile.encoding=GBK</argLine></configuration>
<executions><execution><id>integration</id><goals><goal>integration-test</goal></goals></execution></executions>
</plugin></plugins>""",
        ),
        encoding="utf-8",
    )
    (root / "src/test/java/example/OutputIT.java").write_text(
        """package example;
public class OutputIT {
 @org.junit.Test public void works(){System.err.println("集成测试的输出");}
 @org.junit.Test public void exits(){System.err.println("第二组异常退出");System.err.flush();System.exit(7);}
}""",
        encoding="utf-8",
    )

    async def scenario():
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                for method in ("works", "exits"):
                    result = await run(
                        session,
                        root,
                        ["example.OutputTest#prints", "example.OutputIT#" + method],
                    )
                    assert "output" in result, json.dumps(result, ensure_ascii=False)
                    text = result["output"]
                    assert "[maven-surefire-plugin:default-test]" in text
                    assert "[maven-failsafe-plugin:integration]" in text
                    assert text.index("验证完成") < text.index(
                        "[maven-failsafe-plugin:integration]"
                    )
                    if method == "works":
                        assert result["passed"] and "集成测试的输出" in text
                    else:
                        assert not result["ok"] and "第二组异常退出" in text
                    assert len(result["log_files"]) == 2
                    first, second = map(Path, result["log_files"])
                    assert (
                        first.is_absolute() and second.is_absolute() and first != second
                    )
                    assert_console_file(
                        first,
                        "开始验证：40 + 2\nstderr：这是一条测试提示\n验证完成：结果为 42\n",
                    )
                    assert_console_file(
                        second,
                        ("集成测试的输出" if method == "works" else "第二组异常退出")
                        + "\n",
                    )

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_timeout_and_output_limit_keep_console_tail(tmp_path, monkeypatch):
    env = environment(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", env["XDG_CACHE_HOME"])
    root, _ = fixture(tmp_path, "UTF-8")
    manager = FastTestManager()
    try:
        # Real JDT + real Runner; use a short internal execution deadline, not MCP's wait timeout.
        for method, timeout, code in (
            ("slow", 2, "TEST_TIMEOUT"),
            ("tooLoud", 30, "TEST_OUTPUT_LIMIT_EXCEEDED"),
        ):
            started = manager.start(
                project_path=root,
                tests=("example.OutputTest#" + method,),
                source_files=(),
                java_home=env["JAVA_HOME"],
                maven_settings=os.environ.get("JOLINK_TEST_MAVEN_SETTINGS", ""),
                timeout_seconds=timeout,
                short_wait_seconds=0,
            )
            deadline = time.monotonic() + 120
            while manager.result(started["test_run_id"])["status"] in {
                "starting",
                "bootstrapping",
                "compiling",
                "running",
            }:
                assert time.monotonic() < deadline
                time.sleep(0.1)
            result = manager.result(started["test_run_id"])
            assert result["error_code"] == code, result
            assert result["output"]
            assert len(result["output"].encode()) <= 16 * 1024
            if method == "slow":
                assert "live-start" in result["output"]
            else:
                assert result["output_truncated"] is True
    finally:
        manager.close()
