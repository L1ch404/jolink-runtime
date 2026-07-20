from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

from jolink_runtime.adapters.java import process_discovery


def _completed(*, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr)


def test_find_jps_prefers_java_home(monkeypatch, tmp_path: Path) -> None:
    jps = tmp_path / "bin" / "jps"
    jps.parent.mkdir()
    jps.touch()
    monkeypatch.setenv("JAVA_HOME", str(tmp_path))

    assert process_discovery._find_jps() == str(jps)


def test_run_jps_preserves_basic_and_full_result_fields(monkeypatch) -> None:
    monkeypatch.setattr(process_discovery, "_find_jps", lambda: "jps")
    monkeypatch.setattr(process_discovery, "_get_runtime", lambda: "Temurin 17.0.9")

    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return _completed(
            stdout=(
                "101 com.example.Application -Xmx512m -Dspring.profiles.active=test\n"
                "202 app.jar -Dfile.encoding=UTF-8\n"
            )
        )

    monkeypatch.setattr(process_discovery.subprocess, "run", fake_run)

    basic = process_discovery._run_jps(full=False)
    full = process_discovery._run_jps(full=True)

    assert calls == [["jps", "-l"], ["jps", "-lv"]]
    assert basic == [
        {"pid": 101, "main_class": "com.example.Application", "runtime": "Temurin 17.0.9"},
        {"pid": 202, "main_class": "app.jar", "runtime": "Temurin 17.0.9"},
    ]
    assert full[0]["jvm_args"] == "-Xmx512m -Dspring.profiles.active=test"
    assert full[1]["jvm_args"] == "-Dfile.encoding=UTF-8"


def test_discovery_falls_back_to_ps_when_jps_is_missing(monkeypatch) -> None:
    def missing_jps(*, full: bool):
        assert full is True
        raise FileNotFoundError("jps")

    expected = [
        {"pid": 73, "main_class": "java -jar service.jar", "runtime": "OpenJDK 8"}
    ]
    monkeypatch.setattr(process_discovery, "_run_jps", missing_jps)
    monkeypatch.setattr(process_discovery, "_run_ps", lambda: expected)

    result = process_discovery.discover_java_processes(full=True)

    assert result == {
        "message": "Found 1 Java process(es)",
        "processes": expected,
        "count": 1,
    }


def test_discovery_filters_by_class_case_insensitively_or_exact_pid(monkeypatch) -> None:
    processes = [
        {"pid": 101, "main_class": "com.example.SpringApplication", "runtime": "Java"},
        {"pid": 202, "main_class": "worker.jar", "runtime": "Java"},
    ]
    monkeypatch.setattr(
        process_discovery,
        "_run_jps",
        lambda *, full: processes,
    )

    by_class = process_discovery.discover_java_processes("spring")
    by_pid = process_discovery.discover_java_processes("202")

    assert by_class["processes"] == [processes[0]]
    assert by_class["message"] == "Found 1 Java process(es) matching 'spring'"
    assert by_pid["processes"] == [processes[1]]
    assert by_pid["message"] == "Found 1 Java process(es) matching '202'"


def test_empty_discovery_preserves_message_and_writes_no_stdout(
    monkeypatch,
    caplog,
    capsys,
) -> None:
    monkeypatch.setattr(process_discovery, "_run_jps", lambda *, full: [])
    caplog.set_level(logging.INFO, logger=process_discovery.__name__)

    result = process_discovery.discover_java_processes("missing", full=False)

    assert result == {
        "message": "No Java processes found matching 'missing'. Is a JVM running?",
        "processes": [],
        "count": 0,
    }
    assert capsys.readouterr().out == ""
    assert "java_processes.discovery.start filtered=True full=False" in caplog.text
    assert "java_processes.discovery.finish source=jps count=0" in caplog.text


def test_run_ps_preserves_command_as_main_class(monkeypatch) -> None:
    monkeypatch.setattr(process_discovery, "_get_runtime", lambda: "Zulu 11.0.27")
    monkeypatch.setattr(
        process_discovery.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed(
            stdout=(
                "alice 314 0.0 0.1 100 200 ?? S 10:00AM 0:01.00 java -jar app.jar\n"
                "alice 315 0.0 0.1 100 200 ?? S 10:00AM 0:01.00 grep java\n"
            )
        ),
    )

    assert process_discovery._run_ps() == [
        {"pid": 314, "main_class": "java -jar app.jar", "runtime": "Zulu 11.0.27"}
    ]


def test_detect_runtime_preserves_distribution_and_version(monkeypatch) -> None:
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(process_discovery, "_resolve_macos_java_home", lambda: None)
    monkeypatch.setattr(
        process_discovery.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed(
            stderr=(
                'openjdk version "17.0.9" 2023-10-17\n'
                "OpenJDK Runtime Environment Temurin-17.0.9+9 (build 17.0.9+9)\n"
            )
        ),
    )

    assert process_discovery._detect_runtime() == "Temurin 17.0.9"


def test_run_jps_timeout_is_logged_and_returns_empty(monkeypatch, caplog) -> None:
    monkeypatch.setattr(process_discovery, "_find_jps", lambda: "jps")
    monkeypatch.setattr(process_discovery, "_get_runtime", lambda: "Java")
    monkeypatch.setattr(
        process_discovery.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("jps", 10)
        ),
    )
    caplog.set_level(logging.DEBUG, logger=process_discovery.__name__)

    assert process_discovery._run_jps(full=False) == []
    assert "jps discovery failed" in caplog.text
