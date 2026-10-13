import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jolink_runtime.core.dispatcher import Dispatcher
from jolink_runtime.launch.fast_test_manager import TestAttempt as Attempt
from jolink_runtime.launch.process_supervisor import AttemptToken
from jolink_runtime.launch.test_execution import run_test_executions
from jolink_runtime.launch.test_output import TestOutputLog as OutputLog
from jolink_runtime.launch.test_output import read_test_output


def output_log(root, name, text, *, encoding="utf-8"):
    path = root / name / "test.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))
    return OutputLog(path, name, encoding)


def test_only_result_reads_output(tmp_path):
    attempt = Attempt(
        "test-output", 1, AttemptToken("test-output", 1), tmp_path, (), ("Test",), 30
    )
    log = output_log(tmp_path, "unit", "one\n中文 stderr\nlast\n")
    attempt.output_logs = (log,)
    attempt.state = "completed"
    attempt.result = {"ok": True, "passed": True, "tests": 1}
    dispatcher = Dispatcher()
    manager = dispatcher.sessions.get_runtime()._fast_tests
    manager._last = attempt
    try:
        assert "output" not in manager.status()
        assert "output" not in attempt.run_response()
        assert "output" not in attempt.snapshot()
        public = dispatcher.dispatch(
            "java_fast_test",
            {"action": "result", "test_run_id": attempt.test_run_id},
        )
        assert public["output"] == "one\n中文 stderr\nlast\n"
        assert public["log_files"] == [str(log.path)]
        assert "log_files" not in manager.status()
        assert "log_files" not in attempt.run_response()
        full = manager.result(attempt.test_run_id)
        assert full["output"] == "one\n中文 stderr\nlast\n"
        assert full["output_truncated"] is False
        assert manager.result(attempt.test_run_id) == full
        with log.path.open("a", encoding="utf-8") as stream:
            stream.write("later\n")
        assert manager.result(attempt.test_run_id)["output"].endswith("later\n")
    finally:
        dispatcher.close_all_sessions()


def test_log_not_registered_before_test_execution(tmp_path):
    assert read_test_output(()) == {}
    absent = OutputLog(tmp_path / "test.log")
    expected = {
        "output": "",
        "output_truncated": False,
        "log_files": [str(absent.path)],
    }
    assert read_test_output((absent,)) == expected
    absent.path.touch()
    assert read_test_output((absent,)) == expected


def test_log_file_paths_are_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = output_log(Path("."), "relative", "text\n")
    result = read_test_output((log,))
    assert result["log_files"] == [str(tmp_path / "relative/test.log")]
    assert Path(result["log_files"][0]).read_text() == "text\n"


def test_multiple_groups_keep_order_and_share_tail(tmp_path):
    first = output_log(tmp_path, "unit", "unit1\nunit2\n")
    last = output_log(tmp_path, "integration", "it1\nit2\n")
    full = read_test_output((first, last))
    assert full == {
        "output": "[unit]\nunit1\nunit2\n[integration]\nit1\nit2\n",
        "output_truncated": False,
        "log_files": [str(first.path), str(last.path)],
    }
    first.path.write_text("".join(f"unit-{i}\n" for i in range(20)))
    last.path.write_text("".join(f"it-{i}\n" for i in range(40)))
    short = read_test_output((first, last))
    assert short == {
        "output": "[unit]\n"
        + "".join(f"unit-{i}\n" for i in range(10, 20))
        + "[integration]\n"
        + "".join(f"it-{i}\n" for i in range(40)),
        "output_truncated": True,
        "log_files": [str(first.path), str(last.path)],
    }
    last.path.write_text("".join(f"it-{i}\n" for i in range(60)))
    newest = read_test_output((first, last))
    assert newest == {
        "output": "[integration]\n" + "".join(f"it-{i}\n" for i in range(10, 60)),
        "output_truncated": True,
        "log_files": [str(first.path), str(last.path)],
    }


@pytest.mark.parametrize("encoding", ["utf-8", "gbk", "cp1252"])
def test_reported_encoding_is_used(tmp_path, encoding):
    text = "中文输出\r\n" if encoding != "cp1252" else "café\r\n"
    result = read_test_output(
        (output_log(tmp_path, "encoded", text, encoding=encoding),)
    )
    assert result["output"] == text


def test_output_is_not_redacted_and_only_console_colors_are_removed(tmp_path):
    log = output_log(
        tmp_path,
        "unit",
        "\x1b[31mnormal\x1b[0m\nAuthorization=Bearer abc def\n"
        "COOKIE=a=1; b=2\npassword=abc,def\nhttp://alice:abc@example.test/\nlast\n",
    )
    result = read_test_output((log,))
    assert result["output"].startswith("normal\n") and result["output"].endswith(
        "last\n"
    )
    original = log.path.read_text()
    assert result["output"] == original.replace("\x1b[31m", "").replace("\x1b[0m", "")
    assert "<redacted" not in result["output"]
    assert Path(result["log_files"][0]).read_text() == original


@pytest.mark.parametrize("size", [20000, 600000])
def test_long_line_returns_a_bounded_suffix_without_replacing_values(tmp_path, size):
    log = output_log(tmp_path, "unit", "password=" + "value" * size)
    result = read_test_output((log,))
    assert result["output_truncated"] is True
    assert result["output"].endswith("value")
    assert "omitted>" not in result["output"]
    assert len(result["output"].encode()) <= 16 * 1024
    assert Path(result["log_files"][0]).read_text() == "password=" + "value" * size


def test_all_groups_share_byte_budget_and_keep_newest_output(tmp_path):
    logs = tuple(
        output_log(tmp_path, str(i), ("中文" * 100 + "\n") * 40 + f"end-{i}\n")
        for i in range(3)
    )
    result = read_test_output(logs)
    assert len(result["output"].encode()) <= 16 * 1024
    assert result["output_truncated"] is True
    assert result["output"].endswith("end-2\n")
    assert "end-0" not in result["output"]
    assert result["log_files"] == [str(log.path) for log in logs]


def test_read_error_does_not_replace_test_result(tmp_path, monkeypatch):
    log = output_log(tmp_path, "unit", "text\n")

    def unreadable(*_args, **_kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(
        "jolink_runtime.adapters.java.log_manager.read_log_tail_snapshot", unreadable
    )
    assert read_test_output((log,))["output_unavailable"] is True


def test_execution_registers_log_before_runner_failure(tmp_path):
    attempt = SimpleNamespace(
        tests=("x.Test",),
        timeout_seconds=30,
        owner=object(),
        output_logs=(),
        require_not_cancelled=lambda: None,
    )
    project = SimpleNamespace(
        test_executions=(),
        test_java_executable=Path("java"),
        test_framework=None,
        test_run_order="",
        test_working_directory=tmp_path,
        runner_environment={},
        runner_jvm_arguments=(),
        runtime_classpath=(),
        compiler=SimpleNamespace(
            test_output_directory=tmp_path, output_directory=tmp_path
        ),
    )

    def crash(**options):
        assert attempt.output_logs == (options["output_log"],)
        assert attempt.output_logs[0].path == options["attempt_directory"] / "test.log"
        attempt.output_logs[0].path.parent.mkdir()
        attempt.output_logs[0].path.write_text("before crash\n")
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError, match="crash"):
        run_test_executions(
            SimpleNamespace(run=crash), project, attempt, tmp_path / "run"
        )
    assert read_test_output(attempt.output_logs)["output"] == "before crash\n"


@pytest.mark.parametrize(
    "module", ["fast_test_manager", "test_output", "test_execution"]
)
def test_modules_import_without_adapter_first(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import jolink_runtime.launch.{module}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
