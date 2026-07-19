from __future__ import annotations

import logging
import shutil
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest


from jolink_runtime_debugger.core.models import RuntimeAction, Variable
from jolink_runtime_debugger.adapters.java.jdwp_client import (
    Cmd,
    EventKind,
    IDSizes,
    JDWPClient,
    SuspendPolicy,
    Tag,
)
from jolink_runtime_debugger.adapters.java.log_manager import LogManager
from jolink_runtime_debugger.adapters.java import process_manager as process_module
from jolink_runtime_debugger.adapters.java.process_manager import (
    ProcessInfo,
    ProcessManager,
)
from jolink_runtime_debugger.adapters.java.jdwp_adapter import (
    BreakpointClassCandidate,
    BreakpointLocation,
    JavaRuntime,
    SuspensionSnapshot,
)
def _recv_exact(sock: socket.socket, size: int) -> bytes:
    result = b""
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise RuntimeError("socket closed")
        result += chunk
    return result


def test_external_process_liveness_uses_psutil(monkeypatch) -> None:
    checked = []
    monkeypatch.setattr(
        process_module.psutil,
        "pid_exists",
        lambda pid: checked.append(pid) or True,
    )

    info = ProcessInfo(None, 5005, "Attached", pid=3488, owned=False)

    assert info.is_alive() is True
    assert checked == [3488]


def test_process_manager_releases_expected_target_without_clearing_replacement(
    monkeypatch,
) -> None:
    class FakeProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        @staticmethod
        def poll():
            return None

    stopped: list[int] = []
    monkeypatch.setattr(
        ProcessManager,
        "_stop_posix",
        staticmethod(lambda proc: stopped.append(proc.pid)),
    )
    monkeypatch.setattr(
        ProcessManager,
        "_stop_windows",
        staticmethod(lambda proc: stopped.append(proc.pid)),
    )

    manager = ProcessManager()
    first = manager._publish(ProcessInfo(FakeProc(101), 5005, "First"))
    second = manager._publish(ProcessInfo(FakeProc(202), 5006, "Second"))

    assert first.generation == 1
    assert second.generation == 2
    released = manager.stop_target(first)

    assert released == {"status": "stopped", "pid": 101}
    assert stopped == [101]
    assert manager.current is second


def test_process_manager_detaches_expected_target_without_clearing_replacement() -> None:
    manager = ProcessManager()
    first = manager._publish(ProcessInfo(
        None,
        5005,
        "First",
        pid=101,
        owned=False,
    ))
    second = manager._publish(ProcessInfo(
        None,
        5006,
        "Second",
        pid=202,
        owned=False,
    ))

    released = manager.detach_target(first)

    assert released == {"status": "already_detached", "pid": 101}
    assert manager.current is second


def test_force_close_sees_owned_process_while_start_waits_for_jdwp(
    monkeypatch,
) -> None:
    probe_started = threading.Event()
    release_probe = threading.Event()
    stopped: list[int] = []
    start_errors: list[Exception] = []

    class FakeProc:
        pid = 303
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    proc = FakeProc()
    monkeypatch.setattr(process_module, "_IS_WINDOWS", False)
    monkeypatch.setattr(
        process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: proc,
    )

    def blocked_probe(*args, **kwargs) -> bool:
        probe_started.set()
        assert release_probe.wait(timeout=2)
        return False

    monkeypatch.setattr(ProcessManager, "_check_jdwp_port", blocked_probe)

    def stop_posix(target) -> None:
        stopped.append(target.pid)
        target.returncode = -15

    monkeypatch.setattr(ProcessManager, "_stop_posix", staticmethod(stop_posix))

    runtime = JavaRuntime()

    def start_process() -> None:
        try:
            runtime._proc.start(
                classpath=".",
                main_class="StartingFixture",
                jdwp_port=5005,
                startup_timeout=2,
            )
        except Exception as error:
            start_errors.append(error)

    starter = threading.Thread(target=start_process)
    starter.start()
    assert probe_started.wait(timeout=2)

    in_flight = runtime._proc.current
    assert in_flight is not None
    assert in_flight.pid == 303

    runtime.force_close()
    release_probe.set()
    starter.join(timeout=3)

    assert not starter.is_alive()
    assert stopped == [303]
    assert runtime._proc.current is None
    assert len(start_errors) == 1
    assert "Process exited with code -15" in str(start_errors[0])


def test_failed_start_forgets_early_published_target(monkeypatch) -> None:
    class ExitedProc:
        pid = 304
        returncode = 7

        def poll(self):
            return self.returncode

    manager = ProcessManager()
    monkeypatch.setattr(
        process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: ExitedProc(),
    )

    with pytest.raises(RuntimeError, match="Process exited with code 7"):
        manager.start(
            classpath=".",
            main_class="FailingFixture",
            jdwp_port=5005,
            startup_timeout=1,
        )

    assert manager.current is None


def test_shutdown_gate_prevents_late_spawn_and_attach(monkeypatch) -> None:
    manager = ProcessManager()
    popen_called = False

    def unexpected_popen(*args, **kwargs):
        nonlocal popen_called
        popen_called = True
        raise AssertionError("shutdown gate must reject before Popen")

    monkeypatch.setattr(process_module.subprocess, "Popen", unexpected_popen)
    monkeypatch.setattr(process_module.psutil, "pid_exists", lambda pid: True)
    manager.prevent_new_targets()

    with pytest.raises(RuntimeError, match="shutting down"):
        manager.start(
            classpath=".",
            main_class="TooLate",
            jdwp_port=5005,
        )
    with pytest.raises(RuntimeError, match="shutting down"):
        manager.attach(999, 5005, "TooLate")

    assert popen_called is False
    assert manager.current is None


def test_attach_uses_non_destructive_pid_probe(monkeypatch) -> None:
    manager = ProcessManager()
    monkeypatch.setattr(process_module.psutil, "pid_exists", lambda pid: pid == 3488)
    monkeypatch.setattr(
        manager,
        "_check_jdwp_port",
        lambda *args, **kwargs: pytest.fail("attach must not consume a JDWP handshake"),
    )

    info = manager.attach(3488, 5005, "SpringApplication")

    assert info.pid == 3488
    assert info.owned is False
    assert info.launch_mode == "attached"


def test_runtime_attach_connects_once_and_rolls_back_on_failure(monkeypatch) -> None:
    runtime = JavaRuntime()
    monkeypatch.setattr(process_module.psutil, "pid_exists", lambda pid: pid == 3488)
    connect_calls = []

    def fail_connect():
        connect_calls.append(True)
        raise ConnectionRefusedError("JDWP is unavailable")

    monkeypatch.setattr(runtime, "_connect", fail_connect)

    result = runtime.attach(RuntimeAction(
        action="attach",
        pid=3488,
        jdwp_port=5005,
        main_class="SpringApplication",
    ))

    assert connect_calls == [True]
    assert result.ok is False
    assert result.error == "JDWP is unavailable"
    assert runtime._proc.current is None
    assert runtime._jdwp is None


def test_runtime_attach_rejects_ipv6_loopback_before_process_tracking() -> None:
    runtime = JavaRuntime()

    result = runtime.attach(RuntimeAction(
        action="attach",
        pid=3488,
        host="::1",
        jdwp_port=5005,
    ))

    assert result.ok is False
    assert result.error == "Remote attach is not supported yet; use a local JDWP endpoint"
    assert runtime._proc.current is None


def test_jar_launch_builds_java_jar_command_and_windows_flags(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = {}

    class FakeProcess:
        pid = 4321
        returncode = None

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(process_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(process_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ProcessManager, "_check_jdwp_port", lambda *args, **kwargs: True)
    monkeypatch.setattr(process_module.time, "sleep", lambda seconds: None)
    log_file = tmp_path / "application.log"

    info = ProcessManager().start(
        classpath="ignored",
        main_class="",
        jar_path=r"C:\apps\demo.jar",
        vm_args=["-Xmx512m"],
        app_args=["--spring.profiles.active=dogfood"],
        jdwp_port=5005,
        log_file=str(log_file),
    )

    assert captured["command"] == [
        "java",
        "-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=127.0.0.1:5005",
        "-Xmx512m",
        "-jar",
        r"C:\apps\demo.jar",
        "--spring.profiles.active=dogfood",
    ]
    assert "-cp" not in captured["command"]
    assert captured["kwargs"]["creationflags"] == process_module._CREATE_NEW_PROCESS_GROUP
    assert "start_new_session" not in captured["kwargs"]
    assert captured["kwargs"]["stdout"].mode == "wb"
    assert info.launch_mode == "jar"
    assert info.jar_path == r"C:\apps\demo.jar"


def test_run_rejects_ambiguous_or_missing_launch_target() -> None:
    runtime = JavaRuntime()

    ambiguous = runtime.run(RuntimeAction(
        action="run",
        main_class="Demo",
        jar_path="demo.jar",
    ))
    missing = runtime.run(RuntimeAction(action="run"))

    assert ambiguous.error == "Provide either jar_path or main_class, not both"
    assert missing.error == "run requires either jar_path or main_class"
    assert runtime._proc.current is None
    assert runtime._log.path is None


def test_windows_stop_escalates_to_force_tree_kill(monkeypatch) -> None:
    commands = []

    class FakeResult:
        returncode = 0

    class FakeProcess:
        pid = 4321
        returncode = None

        def __init__(self):
            self.wait_count = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            self.wait_count += 1
            if self.wait_count == 1:
                raise subprocess.TimeoutExpired("java", timeout)
            self.returncode = 1
            return self.returncode

        def kill(self):
            raise AssertionError("taskkill force path should finish the process")

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        return FakeResult()

    proc = FakeProcess()
    manager = ProcessManager()
    manager._process = ProcessInfo(proc, 5005, "Demo")
    monkeypatch.setattr(process_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(process_module.subprocess, "run", fake_run)

    result = manager.stop()

    assert result == {"status": "stopped", "pid": 4321}
    assert commands[0][0] == ["taskkill", "/PID", "4321", "/T"]
    assert commands[1][0] == ["taskkill", "/PID", "4321", "/T", "/F"]
    assert commands[0][1]["encoding"] == "utf-8"


def test_log_reading_is_utf8_and_replaces_invalid_bytes(tmp_path: Path) -> None:
    log_file = tmp_path / "java.log"
    log_file.write_bytes("启动成功\n".encode("utf-8") + b"bad:\xff\n")
    manager = LogManager(str(tmp_path))
    manager._current_file = str(log_file)

    result = manager.tail(10)

    assert result["lines"] == ["启动成功\n", "bad:\ufffd\n"]
    assert "启动成功" in ProcessManager._read_log_tail(str(log_file))


def test_log_read_failure_is_returned_instead_of_silently_swallowed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "unreadable.log"
    manager = LogManager(str(tmp_path))
    manager._current_file = str(log_file)

    def deny_open(*args, **kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr("builtins.open", deny_open)

    result = manager.tail(10)
    startup_tail = ProcessManager._read_log_tail(str(log_file))

    assert "PermissionError: access denied" in result["error"]
    assert startup_tail == "[Unable to read log file: PermissionError: access denied]"


def test_command_queues_interleaved_breakpoint_event(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    client_sock, vm_sock = socket.socketpair()
    client = JDWPClient()
    client._sock = client_sock
    client.ids = IDSizes(8, 8, 8, 8, 8)
    event_data = struct.pack(">BI", 2, 1)
    event_data += struct.pack(">BI", EventKind.BREAKPOINT, 73)
    event_data += (101).to_bytes(8, "big")
    event_data += struct.pack(">B", 1)
    event_data += (202).to_bytes(8, "big")
    event_data += (303).to_bytes(8, "big")
    event_data += struct.pack(">Q", 404)
    event_packet = (
        struct.pack(">IIBBB", 11 + len(event_data), 900, 0, 64, 100)
        + event_data
    )

    def vm() -> None:
        command_header = _recv_exact(vm_sock, 11)
        command_length, command_id, _ = struct.unpack(">IIB", command_header[:9])
        _recv_exact(vm_sock, command_length - 11)
        vm_sock.sendall(event_packet)
        vm_sock.sendall(struct.pack(">IIBH", 11, command_id, 0x80, 0))

    thread = threading.Thread(target=vm)
    thread.start()
    try:
        error, data = client.command(Cmd.VM, 1)
        assert error == 0
        assert data == b""

        composite = client.wait_for_event(0.1)
        assert composite is not None
        assert composite["suspend_policy"] == 2
        assert composite["events"] == [{
            "kind": EventKind.BREAKPOINT,
            "request_id": 73,
            "thread_id": 101,
            "location": {
                "type_tag": 1,
                "class_id": 202,
                "method_id": 303,
                "index": 404,
            },
        }]
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "java_runtime.jdwp.command.send" in messages
        assert "java_runtime.jdwp.event.queued" in messages
        assert "java_runtime.jdwp.command.reply" in messages
        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        client.close()
        vm_sock.close()


def test_jdwp_parser_handles_exception_event() -> None:
    client = JDWPClient()
    client.ids = IDSizes(8, 8, 8, 8, 8)
    event_data = struct.pack(">BI", 2, 1)
    event_data += struct.pack(">BI", EventKind.EXCEPTION, 73)
    event_data += (101).to_bytes(8, "big")
    event_data += struct.pack(">B", 1)
    event_data += (202).to_bytes(8, "big")
    event_data += (303).to_bytes(8, "big")
    event_data += struct.pack(">Q", 404)
    event_data += bytes([Tag.OBJECT])
    event_data += (505).to_bytes(8, "big")
    event_data += struct.pack(">B", 1)
    event_data += (606).to_bytes(8, "big")
    event_data += (707).to_bytes(8, "big")
    event_data += struct.pack(">Q", 808)

    composite = client._parse_composite_event(event_data)

    assert composite == {
        "suspend_policy": 2,
        "events": [{
            "kind": EventKind.EXCEPTION,
            "request_id": 73,
            "thread_id": 101,
            "location": {
                "type_tag": 1,
                "class_id": 202,
                "method_id": 303,
                "index": 404,
            },
            "exception": {
                "tag": Tag.OBJECT,
                "object_id": 505,
            },
            "catch_location": {
                "type_tag": 1,
                "class_id": 606,
                "method_id": 707,
                "index": 808,
            },
        }],
    }


def test_id_sizes_pack_each_jdwp_id_kind() -> None:
    ids = IDSizes(
        field_id_size=2,
        method_id_size=4,
        object_id_size=8,
        reference_type_id_size=6,
        frame_id_size=3,
    )

    assert ids.pack_field(0x1234) == bytes.fromhex("1234")
    assert ids.pack_method(0x12345678) == bytes.fromhex("12345678")
    assert ids.pack_obj(0x0102030405060708) == bytes.fromhex("0102030405060708")
    assert ids.pack_ref(0x010203040506) == bytes.fromhex("010203040506")
    assert ids.pack_frame(0x010203) == bytes.fromhex("010203")


def test_breakpoint_remove_disarmed_definition_uses_no_protocol_call() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        def __init__(self):
            self.calls = []

        def command(self, command_set, command, data=b""):
            self.calls.append((command_set, command, data))
            return 0, b""

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._breakpoints = {
        "bp_001": {"breakpoint_id": "bp_001", "line": 10}
    }
    client = FakeClient()
    runtime._connect = lambda: client

    result = runtime.breakpoint(RuntimeAction(action="breakpoint", bp_action="remove"))

    assert result.error == ""
    assert client.calls == []


def test_breakpoint_remove_rejects_raw_jdwp_request_id() -> None:
    class FakeProcessManager:
        is_running = True

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._breakpoints = {
        "bp_001": {
            "breakpoint_id": "bp_001",
            "last_jdwp_request_id": 77,
            "line": 10,
        }
    }

    result = runtime.breakpoint(RuntimeAction(
        action="breakpoint",
        bp_action="remove",
        request_id=77,
    ))

    assert result.ok is False
    assert result.data["error_code"] == "invalid_argument"
    assert result.data["argument"] == "request_id"
    assert "breakpoint_id" in result.data["suggested_next_step"]
    assert set(runtime._breakpoints) == {"bp_001"}


def test_breakpoint_set_validates_required_arguments_without_connecting() -> None:
    class FakeProcessManager:
        is_running = True

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._connect = lambda: (_ for _ in ()).throw(AssertionError("should not connect"))

    missing_class = runtime.breakpoint(RuntimeAction(
        action="breakpoint",
        bp_action="set",
        line=25,
    ))
    missing_line = runtime.breakpoint(RuntimeAction(
        action="breakpoint",
        bp_action="set",
        class_pattern="com/example/Foo;",
    ))

    assert missing_class.error == "class_pattern is required for breakpoint set"
    assert missing_class.data == {
        "error_code": "invalid_argument",
        "argument": "class_pattern",
        "bp_action": "set",
    }
    assert missing_line.error == "line is required for breakpoint set"
    assert missing_line.data == {
        "error_code": "invalid_argument",
        "argument": "line",
        "bp_action": "set",
    }


def test_breakpoint_set_skips_proxy_by_default_and_can_opt_in() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def __init__(self):
            self.calls = []

        def command(self, command_set, command, data=b""):
            self.calls.append((command_set, command, data))
            if (command_set, command) == (Cmd.VM, 3):
                return 0, _pack_all_classes(
                    (1, 42, "Lcom/example/UserServiceImpl$$SpringCGLIB$$0;", 7),
                )
            if (command_set, command) == (Cmd.REF_TYPE, 7):
                source = b"UserServiceImpl.java"
                return 0, struct.pack(">I", len(source)) + source
            if (command_set, command) == (Cmd.REF_TYPE, 5):
                name = b"handle"
                signature = b"()V"
                return 0, (
                    struct.pack(">I", 1)
                    + (7).to_bytes(8, "big")
                    + struct.pack(">I", len(name)) + name
                    + struct.pack(">I", len(signature)) + signature
                    + struct.pack(">I", 0)
                )
            if (command_set, command) == (Cmd.METHOD, 1):
                return 0, (
                    struct.pack(">QQI", 0, 20, 1)
                    + struct.pack(">QI", 9, 25)
                )
            if (command_set, command) == (Cmd.EVENT, 1):
                return 0, struct.pack(">I", 99)
            raise AssertionError((command_set, command, data))

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    default_client = FakeClient()
    runtime._connect = lambda: default_client

    default_result = runtime.breakpoint(RuntimeAction(
        action="breakpoint",
        bp_action="set",
        class_pattern="UserServiceImpl",
        line=25,
    ))

    assert default_result.error == "ONLY_PROXY_CLASSES_MATCHED"
    assert default_result.data["error_code"] == "ONLY_PROXY_CLASSES_MATCHED"
    assert default_result.data["matched_proxy_classes"] == [{
        "class": "com.example.UserServiceImpl$$SpringCGLIB$$0",
        "signature": "Lcom/example/UserServiceImpl$$SpringCGLIB$$0;",
        "source_file": "UserServiceImpl.java",
        "proxy_type": "spring_cglib",
        "match_type": "simple_name_exact",
    }]
    assert [call[:2] for call in default_client.calls] == [
        (Cmd.VM, 3),
        (Cmd.REF_TYPE, 7),
    ]

    opt_in_client = FakeClient()
    runtime._connect = lambda: opt_in_client
    opt_in_result = runtime.breakpoint(RuntimeAction(
        action="breakpoint",
        bp_action="set",
        class_pattern="UserServiceImpl",
        include_proxy=True,
        line=25,
    ))

    assert opt_in_result.error == ""
    assert opt_in_result.data["breakpoint_id"] == "bp_001"
    assert opt_in_result.data["matched_class"] == "com.example.UserServiceImpl$$SpringCGLIB$$0"
    assert opt_in_result.data["source_file"] == "UserServiceImpl.java"
    assert opt_in_result.data["is_proxy"] is True
    assert opt_in_result.data["proxy_type"] == "spring_cglib"
    assert opt_in_result.data["line"] == 25
    assert opt_in_result.data["method"] == "handle"
    assert opt_in_result.data["suspend_policy"] == "EVENT_THREAD"
    assert opt_in_result.data["warnings"] == []
    assert opt_in_result.data["jdwp"] == {
        "armed": False,
        "suspend_policy": "EVENT_THREAD",
    }
    assert (Cmd.EVENT, 1) not in [call[:2] for call in opt_in_client.calls]
    assert opt_in_result.data["class"] == "Lcom/example/UserServiceImpl$$SpringCGLIB$$0;"


def test_breakpoint_set_returns_matched_application_location_summary() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def command(self, command_set, command, data=b""):
            if (command_set, command) == (Cmd.VM, 3):
                return 0, _pack_all_classes(
                    (1, 42, "Lcom/example/HgPortraitServiceImpl;", 7),
                )
            if (command_set, command) == (Cmd.REF_TYPE, 7):
                source = b"HgPortraitServiceImpl.java"
                return 0, struct.pack(">I", len(source)) + source
            if (command_set, command) == (Cmd.REF_TYPE, 5):
                name = b"queryPortrait"
                signature = b"(Ljava/util/List;)Ljava/util/List;"
                return 0, (
                    struct.pack(">I", 1)
                    + (7).to_bytes(8, "big")
                    + struct.pack(">I", len(name)) + name
                    + struct.pack(">I", len(signature)) + signature
                    + struct.pack(">I", 0)
                )
            if (command_set, command) == (Cmd.METHOD, 1):
                return 0, (
                    struct.pack(">QQI", 0, 200, 1)
                    + struct.pack(">QI", 128, 716)
                )
            if (command_set, command) == (Cmd.EVENT, 1):
                return 0, struct.pack(">I", 17)
            raise AssertionError((command_set, command, data))

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._connect = lambda: FakeClient()

    result = runtime.breakpoint(RuntimeAction(
        action="breakpoint",
        bp_action="set",
        class_pattern="HgPortraitServiceImpl",
        line=716,
    ))

    assert result.error == ""
    assert result.data["breakpoint_id"] == "bp_001"
    assert result.data["matched_class"] == "com.example.HgPortraitServiceImpl"
    assert result.data["source_file"] == "HgPortraitServiceImpl.java"
    assert result.data["is_proxy"] is False
    assert result.data["line"] == 716
    assert result.data["method"] == "queryPortrait"
    assert result.data["suspend_policy"] == "EVENT_THREAD"
    assert result.data["warnings"] == []
    assert "request_id" not in result.data
    assert result.data["jdwp"] == {
        "armed": False,
        "suspend_policy": "EVENT_THREAD",
    }
    assert runtime._breakpoints["bp_001"]["breakpoint_id"] == "bp_001"


def test_wait_event_owns_breakpoint_request_and_resumes_late_timeout_event() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def __init__(self) -> None:
            self.calls: list[tuple[int, int, bytes]] = []
            self.pending: list[dict[str, Any]] = []

        def command(self, command_set, command, data=b""):
            self.calls.append((command_set, command, data))
            if (command_set, command) == (Cmd.VM, 3):
                return 0, _pack_all_classes((1, 42, "LExample;", 7))
            if (command_set, command) == (Cmd.REF_TYPE, 7):
                source = b"Example.java"
                return 0, struct.pack(">I", len(source)) + source
            if (command_set, command) == (Cmd.REF_TYPE, 5):
                name = b"run"
                signature = b"()V"
                return 0, (
                    struct.pack(">I", 1)
                    + (7).to_bytes(8, "big")
                    + struct.pack(">I", len(name)) + name
                    + struct.pack(">I", len(signature)) + signature
                    + struct.pack(">I", 0)
                )
            if (command_set, command) == (Cmd.METHOD, 1):
                return 0, (
                    struct.pack(">QQI", 0, 20, 1)
                    + struct.pack(">QI", 9, 25)
                )
            if (command_set, command) == (Cmd.EVENT, 1):
                return 0, struct.pack(">I", 77)
            if (command_set, command) == (Cmd.EVENT, 2):
                self.pending.append({
                    "suspend_policy": SuspendPolicy.EVENT_THREAD,
                    "events": [{
                        "kind": EventKind.BREAKPOINT,
                        "request_id": 77,
                        "thread_id": 10,
                        "location": {"class_id": 42, "method_id": 7, "index": 9},
                    }],
                })
                return 0, b""
            if (command_set, command) in {(Cmd.VM, 1), (Cmd.THREAD, 3)}:
                return 0, b""
            raise AssertionError((command_set, command, data))

        def drain_events(self):
            pending = list(self.pending)
            self.pending.clear()
            return pending

        def wait_for_event(self, timeout):
            return None

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    client = FakeClient()
    runtime._connect = lambda: client

    configured = runtime.breakpoint(RuntimeAction(
        action="breakpoint",
        bp_action="set",
        class_pattern="Example",
        line=25,
    ))
    assert configured.ok is True
    assert (Cmd.EVENT, 1) not in [call[:2] for call in client.calls]

    result = runtime.wait_event(RuntimeAction(action="wait_event", timeout=0.01))

    assert result.data["status"] == "timeout"
    assert set(runtime._breakpoints) == {"bp_001"}
    assert runtime._armed_breakpoint_requests == {}
    assert runtime._active_suspension is None
    event_calls = [call for call in client.calls if call[0] == Cmd.EVENT]
    assert event_calls[0][1] == 1
    assert event_calls[0][2][:6] == struct.pack(
        ">BBI", EventKind.BREAKPOINT, SuspendPolicy.EVENT_THREAD, 1
    )
    assert event_calls[1] == (
        Cmd.EVENT,
        2,
        struct.pack(">BI", EventKind.BREAKPOINT, 77),
    )
    assert (Cmd.THREAD, 3, (10).to_bytes(8, "big")) in client.calls


def test_partial_arm_failure_rolls_back_first_remote_request() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def __init__(self) -> None:
            self.calls: list[tuple[int, int, bytes]] = []
            self.set_count = 0

        def command(self, command_set, command, data=b""):
            self.calls.append((command_set, command, data))
            if (command_set, command) == (Cmd.EVENT, 1):
                self.set_count += 1
                if self.set_count == 1:
                    return 0, struct.pack(">I", 71)
                return 99, b""
            if (command_set, command) in {(Cmd.EVENT, 2), (Cmd.VM, 1)}:
                return 0, b""
            raise AssertionError((command_set, command, data))

        @staticmethod
        def drain_events():
            return []

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._breakpoints = {
        "bp_001": {
            "breakpoint_id": "bp_001",
            "class": "LExample;",
            "matched_class": "Example",
            "method_signature": "()V",
            "code_index": 9,
            "line": 25,
        },
        "bp_002": {
            "breakpoint_id": "bp_002",
            "class": "LExample;",
            "matched_class": "Example",
            "method_signature": "()V",
            "code_index": 9,
            "line": 25,
        },
    }
    candidate = BreakpointClassCandidate(
        type_tag=1,
        class_id=42,
        signature="LExample;",
        name="Example",
        simple_name="Example",
        source_file="Example.java",
        is_proxy=False,
        proxy_type=None,
        is_generated=False,
        match_type="fully_qualified_exact",
        match_rank=10,
        warnings=[],
    )
    location = BreakpointLocation(
        method_id=7,
        method="run",
        method_signature="()V",
        line=25,
        code_index=9,
    )
    runtime._resolve_breakpoint_class = lambda jdwp, action: (candidate, None, [])
    runtime._line_locations_for_class = (
        lambda jdwp, resolved, line: ([location], [], None)
    )
    client = FakeClient()
    runtime._connect = lambda: client

    result = runtime.wait_event(RuntimeAction(action="wait_event", timeout=1))

    assert result.data["error_code"] == "ARM_BREAKPOINT_FAILED"
    assert set(runtime._breakpoints) == {"bp_001", "bp_002"}
    assert runtime._armed_breakpoint_requests == {}
    assert (Cmd.EVENT, 2, struct.pack(">BI", EventKind.BREAKPOINT, 71)) in client.calls


def test_malformed_arm_reply_disconnects_instead_of_leaving_unknown_request() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def __init__(self) -> None:
            self.close_count = 0

        @staticmethod
        def command(command_set, command, data=b""):
            if (command_set, command) == (Cmd.EVENT, 1):
                return 0, b""
            raise AssertionError((command_set, command, data))

        @staticmethod
        def drain_events():
            return []

        def close(self) -> None:
            self.close_count += 1

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._breakpoints = {
        "bp_001": {
            "breakpoint_id": "bp_001",
            "class": "LExample;",
            "matched_class": "Example",
            "method_signature": "()V",
            "code_index": 9,
            "line": 25,
        },
    }
    candidate = BreakpointClassCandidate(
        type_tag=1,
        class_id=42,
        signature="LExample;",
        name="Example",
        simple_name="Example",
        source_file="Example.java",
        is_proxy=False,
        proxy_type=None,
        is_generated=False,
        match_type="fully_qualified_exact",
        match_rank=10,
        warnings=[],
    )
    location = BreakpointLocation(
        method_id=7,
        method="run",
        method_signature="()V",
        line=25,
        code_index=9,
    )
    runtime._resolve_breakpoint_class = lambda jdwp, action: (candidate, None, [])
    runtime._line_locations_for_class = (
        lambda jdwp, resolved, line: ([location], [], None)
    )
    client = FakeClient()
    runtime._jdwp = client
    runtime._connect = lambda: client

    result = runtime.wait_event(RuntimeAction(action="wait_event", timeout=1))

    assert result.data["error_code"] == "ARM_BREAKPOINT_FAILED"
    assert client.close_count == 1
    assert runtime._jdwp is None
    assert set(runtime._breakpoints) == {"bp_001"}
    assert runtime._armed_breakpoint_requests == {}
    assert runtime._debug_connection_dirty is True


def test_breakpoint_set_returns_ambiguous_class_match() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def command(self, command_set, command, data=b""):
            if (command_set, command) == (Cmd.VM, 3):
                return 0, _pack_all_classes(
                    (1, 42, "Lcom/example/a/DuplicateService;", 7),
                    (1, 43, "Lcom/example/b/DuplicateService;", 7),
                )
            if (command_set, command) == (Cmd.REF_TYPE, 7):
                ref_id = int.from_bytes(data, "big")
                source = (b"DuplicateServiceA.java" if ref_id == 42 else b"DuplicateServiceB.java")
                return 0, struct.pack(">I", len(source)) + source
            raise AssertionError((command_set, command, data))

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._connect = lambda: FakeClient()

    result = runtime.breakpoint(RuntimeAction(
        action="breakpoint",
        bp_action="set",
        class_pattern="DuplicateService",
        line=10,
    ))

    assert result.ok is False
    assert result.error == "AMBIGUOUS_CLASS_MATCH"
    assert result.data["error_code"] == "AMBIGUOUS_CLASS_MATCH"
    assert [item["class"] for item in result.data["candidates"]] == [
        "com.example.a.DuplicateService",
        "com.example.b.DuplicateService",
    ]
    assert "fully qualified" in result.data["suggested_next_step"]


def test_breakpoint_set_returns_no_executable_location_error() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def command(self, command_set, command, data=b""):
            if (command_set, command) == (Cmd.VM, 3):
                return 0, _pack_all_classes(
                    (1, 42, "Lcom/example/HgPortraitServiceImpl;", 7),
                )
            if (command_set, command) == (Cmd.REF_TYPE, 7):
                source = b"HgPortraitServiceImpl.java"
                return 0, struct.pack(">I", len(source)) + source
            if (command_set, command) == (Cmd.REF_TYPE, 5):
                name = b"queryPortrait"
                signature = b"(Ljava/util/List;)Ljava/util/List;"
                return 0, (
                    struct.pack(">I", 1)
                    + (7).to_bytes(8, "big")
                    + struct.pack(">I", len(name)) + name
                    + struct.pack(">I", len(signature)) + signature
                    + struct.pack(">I", 0)
                )
            if (command_set, command) == (Cmd.METHOD, 1):
                return 0, (
                    struct.pack(">QQI", 0, 200, 2)
                    + struct.pack(">QI", 120, 714)
                    + struct.pack(">QI", 128, 717)
                )
            raise AssertionError((command_set, command, data))

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._connect = lambda: FakeClient()

    result = runtime.breakpoint(RuntimeAction(
        action="breakpoint",
        bp_action="set",
        class_pattern="HgPortraitServiceImpl",
        line=716,
    ))

    assert result.ok is False
    assert result.error == "NO_EXECUTABLE_LOCATION_AT_LINE"
    assert result.data["error_code"] == "NO_EXECUTABLE_LOCATION_AT_LINE"
    assert result.data["matched_class"] == "com.example.HgPortraitServiceImpl"
    assert result.data["source_file"] == "HgPortraitServiceImpl.java"
    assert result.data["line"] == 716
    assert result.data["possible_causes"] == [
        "line is not executable",
        "source does not match running bytecode",
    ]
    assert "nearby executable line" in result.data["suggested_next_step"]
    assert result.data["nearby_locations"] == [
        {"line": 717, "method": "queryPortrait", "code_index": 128},
        {"line": 714, "method": "queryPortrait", "code_index": 120},
    ]


def test_generated_class_match_requires_explicit_opt_in() -> None:
    runtime = JavaRuntime()

    assert runtime._class_match_skip_reason(
        "Lcom/example/Foo$$Lambda$123;",
        RuntimeAction(action="breakpoint"),
    ) == "generated_class_excluded"
    assert runtime._class_match_skip_reason(
        "Lcom/example/Foo$$Lambda$123;",
        RuntimeAction(action="breakpoint", include_generated=True),
    ) == ""


def test_breakpoint_list_returns_stable_ids_without_protocol_call() -> None:
    class FakeProcessManager:
        is_running = True

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._breakpoints = {
        "bp_001": {
            "breakpoint_id": "bp_001",
            "class": "Lcom/example/Foo;",
            "matched_class": "com.example.Foo",
            "source_file": "Foo.java",
            "is_proxy": False,
            "method": "run",
            "line": 10,
        },
        "bp_002": {
            "breakpoint_id": "bp_002",
            "class": "Lcom/example/Bar;",
            "matched_class": "com.example.Bar",
            "source_file": "Bar.java",
            "is_proxy": False,
            "method": "handle",
            "line": 20,
        },
    }
    runtime._connect = lambda: (_ for _ in ()).throw(AssertionError("list should not connect"))

    result = runtime.breakpoint(RuntimeAction(action="breakpoint", bp_action="list"))

    assert result.error == ""
    assert result.data["bp_action"] == "list"
    assert result.data["count"] == 2
    assert result.data["breakpoints"] == [
        {
            "breakpoint_id": "bp_001",
            "class": "Lcom/example/Foo;",
            "matched_class": "com.example.Foo",
            "source_file": "Foo.java",
            "is_proxy": False,
            "method": "run",
            "line": 10,
            "jdwp": {"armed": False, "suspend_policy": "EVENT_THREAD"},
        },
        {
            "breakpoint_id": "bp_002",
            "class": "Lcom/example/Bar;",
            "matched_class": "com.example.Bar",
            "source_file": "Bar.java",
            "is_proxy": False,
            "method": "handle",
            "line": 20,
            "jdwp": {"armed": False, "suspend_policy": "EVENT_THREAD"},
        },
    ]


def test_breakpoint_remove_by_breakpoint_id_removes_only_that_definition() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        def __init__(self):
            self.calls = []

        def command(self, command_set, command, data=b""):
            self.calls.append((command_set, command, data))
            return 0, b""

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._breakpoints = {
        "bp_001": {"breakpoint_id": "bp_001", "class": "Lcom/example/Foo;", "method": "run", "line": 10},
        "bp_002": {"breakpoint_id": "bp_002", "class": "Lcom/example/Bar;", "method": "handle", "line": 20},
    }
    client = FakeClient()
    runtime._connect = lambda: client

    result = runtime.breakpoint(RuntimeAction(
        action="breakpoint",
        bp_action="remove",
        breakpoint_id="bp_002",
    ))

    assert result.error == ""
    assert result.data["cleared_ids"] == ["bp_002"]
    assert result.data["cleared_breakpoint_ids"] == ["bp_002"]
    assert result.data["jdwp"]["cleared_request_ids"] == []
    assert result.data["remaining"] == 1
    assert list(runtime._breakpoints) == ["bp_001"]
    assert client.calls == []


def test_breakpoint_remove_by_class_and_line_filters_existing_breakpoints() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        def __init__(self):
            self.calls = []

        def command(self, command_set, command, data=b""):
            self.calls.append((command_set, command, data))
            return 0, b""

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._breakpoints = {
        "bp_001": {"breakpoint_id": "bp_001", "class": "Lcom/example/Foo;", "method": "run", "line": 10},
        "bp_002": {"breakpoint_id": "bp_002", "class": "Lcom/example/Bar;", "method": "handle", "line": 20},
        "bp_003": {"breakpoint_id": "bp_003", "class": "Lcom/example/Bar;", "method": "other", "line": 21},
    }
    client = FakeClient()
    runtime._connect = lambda: client

    result = runtime.breakpoint(RuntimeAction(
        action="breakpoint",
        bp_action="remove",
        class_pattern="example/Bar",
        line=20,
    ))

    assert result.error == ""
    assert result.data["cleared_ids"] == ["bp_002"]
    assert result.data["cleared_breakpoint_ids"] == ["bp_002"]
    assert set(runtime._breakpoints) == {"bp_001", "bp_003"}
    assert client.calls == []


def _pack_all_classes(*classes: tuple[int, int, str, int]) -> bytes:
    payload = struct.pack(">I", len(classes))
    for type_tag, class_id, signature, status in classes:
        signature_bytes = signature.encode("utf-8")
        payload += bytes([type_tag])
        payload += class_id.to_bytes(8, "big")
        payload += struct.pack(">I", len(signature_bytes))
        payload += signature_bytes
        payload += struct.pack(">I", status)
    return payload


def test_exception_class_names_are_normalized() -> None:
    runtime = JavaRuntime()

    for value in (
        "java.lang.NullPointerException",
        "java/lang/NullPointerException",
        "Ljava/lang/NullPointerException;",
        "Ljava.lang.NullPointerException;",
        "NullPointerException",
    ):
        normalized, error = runtime._normalize_exception_signature(value)
        assert error == ""
        assert normalized == "Ljava/lang/NullPointerException;"

    for value in (
        "NumberFormatException",
        "UnsupportedOperationException",
        "NegativeArraySizeException",
        "SecurityException",
        "StringIndexOutOfBoundsException",
    ):
        normalized, error = runtime._normalize_exception_signature(value)
        assert error == ""
        assert normalized == f"Ljava/lang/{value};"


def test_broad_caught_exception_watch_is_rejected_without_connecting() -> None:
    class FakeProcessManager:
        is_running = True

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._connect = lambda: (_ for _ in ()).throw(AssertionError("should not connect"))

    result = runtime.exception(RuntimeAction(
        action="exception",
        exception_class="java.lang.Exception",
    ))

    assert "Refusing broad caught exception watch" in result.error
    assert "allow_broad_caught=true" in result.error


def test_exception_set_creates_stable_definition_without_protocol_request() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def __init__(self):
            self.calls = []

        def command(self, command_set, command, data=b""):
            self.calls.append((command_set, command, data))
            if (command_set, command) == (Cmd.VM, 3):
                return 0, _pack_all_classes(
                    (1, 42, "Ljava/lang/NullPointerException;", 7),
                )
            raise AssertionError((command_set, command, data))

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    client = FakeClient()
    runtime._connect = lambda: client

    result = runtime.exception(RuntimeAction(
        action="exception",
        exception_class="java.lang.NullPointerException",
    ))

    assert result.error == ""
    assert result.data["request_id"] == 1
    assert result.data["exception_class"] == "Ljava/lang/NullPointerException;"
    assert result.data["caught"] is True
    assert result.data["uncaught"] is True
    assert result.data["suspend_policy"] == "EVENT_THREAD"
    assert client.calls == [(Cmd.VM, 3, b"")]
    assert runtime._exceptions[1]["exception_class"] == "Ljava/lang/NullPointerException;"


def test_exception_logical_request_id_survives_rearming() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def __init__(self) -> None:
            self.calls: list[tuple[int, int, bytes]] = []
            self.remote_ids = iter((91, 92))
            self.current_remote_id = 0

        def command(self, command_set, command, data=b""):
            self.calls.append((command_set, command, data))
            if (command_set, command) == (Cmd.VM, 3):
                return 0, _pack_all_classes(
                    (1, 42, "Ljava/lang/NullPointerException;", 7),
                )
            if (command_set, command) == (Cmd.EVENT, 1):
                self.current_remote_id = next(self.remote_ids)
                return 0, struct.pack(">I", self.current_remote_id)
            if (command_set, command) in {
                (Cmd.EVENT, 2),
                (Cmd.VM, 1),
                (Cmd.THREAD, 3),
            }:
                return 0, b""
            raise AssertionError((command_set, command, data))

        def drain_events(self):
            return []

        def wait_for_event(self, timeout):
            return {
                "suspend_policy": SuspendPolicy.EVENT_THREAD,
                "events": [{
                    "kind": EventKind.EXCEPTION,
                    "request_id": self.current_remote_id,
                    "thread_id": 10,
                    "location": {"class_id": 20, "method_id": 30, "index": 40},
                    "exception": {"tag": Tag.OBJECT, "object_id": 50},
                    "catch_location": {},
                }],
            }

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    client = FakeClient()
    runtime._connect = lambda: client
    runtime._describe_location = lambda jdwp, location: {
        "class": "LExample;",
        "method": "run()V",
        "line": 123,
    }
    runtime._thread_name = lambda jdwp, thread_id: "main"
    runtime._object_class_signature = (
        lambda jdwp, obj_id: "Ljava/lang/NullPointerException;"
    )

    configured = runtime.exception(RuntimeAction(
        action="exception",
        exception_class="java.lang.NullPointerException",
    ))
    logical_request_id = configured.data["request_id"]
    assert logical_request_id == 1

    first = runtime.wait_event(RuntimeAction(action="wait_event", timeout=1))
    assert first.data["request_id"] == logical_request_id
    assert first.data["jdwp"]["request_id"] == 91
    assert not any(
        (command_set, command) == (Cmd.THREAD, 3)
        for command_set, command, _ in client.calls
    )
    runtime.resume(RuntimeAction(
        action="resume",
        suspension_id=first.data["suspension_id"],
    ))

    second = runtime.wait_event(RuntimeAction(action="wait_event", timeout=1))
    assert second.data["request_id"] == logical_request_id
    assert second.data["jdwp"]["request_id"] == 92
    assert runtime._armed_exception_requests == {}


def test_exception_set_returns_structured_not_loaded_error() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def command(self, command_set, command, data=b""):
            if (command_set, command) == (Cmd.VM, 3):
                return 0, _pack_all_classes()
            raise AssertionError((command_set, command, data))

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._connect = lambda: FakeClient()

    result = runtime.exception(RuntimeAction(
        action="exception",
        exception_class="java.lang.NumberFormatException",
    ))

    assert result.error == (
        "Exception class 'Ljava/lang/NumberFormatException;' is not loaded in the target VM"
    )
    assert result.data["error_code"] == "exception_class_not_loaded"
    assert result.data["exception_class"] == "Ljava/lang/NumberFormatException;"
    assert result.data["signature"] == "Ljava/lang/NumberFormatException;"
    assert result.data["retryable"] is True
    assert result.data["next_action"] == "trigger_code_path_then_retry_exception_set"
    assert "Trigger the code path once" in result.data["suggestions"][0]


def test_exception_list_and_remove_by_request_id() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        def __init__(self):
            self.calls = []

        def command(self, command_set, command, data=b""):
            self.calls.append((command_set, command, data))
            return 0, b""

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._exceptions = {
        91: {
            "exception_class": "Ljava/lang/NullPointerException;",
            "caught": True,
            "uncaught": True,
        },
        92: {
            "exception_class": "Ljava/lang/IllegalStateException;",
            "caught": False,
            "uncaught": True,
        },
    }
    runtime._connect = lambda: (_ for _ in ()).throw(AssertionError("list should not connect"))

    listed = runtime.exception(RuntimeAction(action="exception", exception_action="list"))

    assert listed.error == ""
    assert listed.data["exceptions"] == [
        {
            "request_id": 91,
            "exception_class": "Ljava/lang/NullPointerException;",
            "caught": True,
            "uncaught": True,
        },
        {
            "request_id": 92,
            "exception_class": "Ljava/lang/IllegalStateException;",
            "caught": False,
            "uncaught": True,
        },
    ]

    client = FakeClient()
    runtime._connect = lambda: client
    removed = runtime.exception(RuntimeAction(
        action="exception",
        exception_action="remove",
        request_id=91,
    ))

    assert removed.error == ""
    assert removed.data["cleared_ids"] == [91]
    assert set(runtime._exceptions) == {92}
    assert client.calls == []


def test_wait_event_returns_exception_suspension() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def __init__(self):
            self.commands = []

        def drain_events(self):
            return []

        def wait_for_event(self, timeout):
            return {
                "suspend_policy": SuspendPolicy.EVENT_THREAD,
                "events": [{
                    "kind": EventKind.EXCEPTION,
                    "request_id": 501,
                    "thread_id": 10,
                    "location": {"class_id": 20, "method_id": 30, "index": 40},
                    "exception": {"tag": Tag.OBJECT, "object_id": 50},
                    "catch_location": {"class_id": 21, "method_id": 31, "index": 41},
                }],
            }

        def command(self, command_set, command, data=b""):
            self.commands.append((command_set, command, data))
            if (command_set, command) in {(Cmd.EVENT, 2), (Cmd.VM, 1)}:
                return 0, b""
            raise AssertionError((command_set, command, data))

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._exceptions = {
        91: {
            "exception_class": "Ljava/lang/NullPointerException;",
            "caught": True,
            "uncaught": True,
        }
    }
    client = FakeClient()
    runtime._connect = lambda: client
    runtime._arm_debug_requests = lambda jdwp, kinds, control: (
        runtime._armed_exception_requests.update({501: 91}) or None
    )
    runtime._describe_location = lambda jdwp, location: {
        "class": "LExample;",
        "method": "run()V",
        "line": 123,
    }
    runtime._thread_name = lambda jdwp, thread_id: "main"
    runtime._object_class_signature = lambda jdwp, obj_id: "Ljava/lang/NullPointerException;"

    result = runtime.wait_event(RuntimeAction(action="wait_event", timeout=1))

    assert result.error == ""
    assert result.data["status"] == "exception_hit"
    assert result.data["event_type"] == "exception"
    assert result.data["event_kind"] == "exception"
    assert result.data["exception"] == {
        "request_id": 91,
        "exception_class": "Ljava/lang/NullPointerException;",
        "signature": "Ljava/lang/NullPointerException;",
        "thrown_class": "Ljava/lang/NullPointerException;",
        "value": {"_ref": "0x32", "_kind": "object"},
        "caught": True,
        "request_caught": True,
        "request_uncaught": True,
    }
    assert result.data["throw_location"]["line"] == 123
    assert result.data["location"]["line"] == 123
    assert result.data["catch_location"]["line"] == 123
    assert "throw_location may be inside JDK or framework code" in result.data["hint"]
    assert runtime._active_suspension is not None
    assert runtime._active_suspension.event_kind == "exception"
    assert runtime._active_suspension.suspend_policy == SuspendPolicy.EVENT_THREAD
    assert result.data["suspend_policy"] == SuspendPolicy.EVENT_THREAD
    assert result.data["suspend_policy_name"] == "EVENT_THREAD"
    assert result.data["resumed"] is False
    assert result.data["request_id"] == 91
    assert result.data["jdwp"]["request_id"] == 501
    assert runtime._armed_exception_requests == {}
    assert (Cmd.EVENT, 2, struct.pack(">BI", EventKind.EXCEPTION, 501)) in client.commands


def test_wait_event_requires_resuming_active_suspension_first() -> None:
    class FakeProcessManager:
        is_running = True

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._breakpoints = {
        "bp_001": {"breakpoint_id": "bp_001", "class": "LExample;", "method": "run()V", "line": 123}
    }
    runtime._active_suspension = SuspensionSnapshot(
        suspension_id="susp_active",
        generation=1,
        request_id=42,
        thread_id=10,
        location={},
        observed_at="2026-07-04T00:00:00+00:00",
        created_at="2026-07-04T00:00:00+00:00",
        suspend_policy=SuspendPolicy.EVENT_THREAD,
    )
    runtime._connect = lambda: (_ for _ in ()).throw(AssertionError("should not connect"))

    result = runtime.wait_event(RuntimeAction(action="wait_event", timeout=1))

    assert result.ok is False
    assert result.error == "ACTIVE_SUSPENSION_EXISTS"
    assert result.data["error_code"] == "active_suspension_exists"
    assert result.data["suspension_id"] == "susp_active"
    assert result.data["suspend_policy_name"] == "EVENT_THREAD"
    assert "Call resume" in result.data["suggested_next_step"]


def test_wait_event_resumes_ignored_stale_suspending_event() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def __init__(self):
            self.waits = 0
            self.commands = []

        def drain_events(self):
            return []

        def wait_for_event(self, timeout):
            self.waits += 1
            if self.waits == 1:
                return {
                    "suspend_policy": SuspendPolicy.EVENT_THREAD,
                    "events": [{
                        "kind": EventKind.BREAKPOINT,
                        "request_id": 41,
                        "thread_id": 10,
                        "location": {"class_id": 20, "method_id": 30, "index": 40},
                    }],
                }
            return {
                "suspend_policy": SuspendPolicy.EVENT_THREAD,
                "events": [{
                    "kind": EventKind.BREAKPOINT,
                    "request_id": 42,
                    "thread_id": 10,
                    "location": {"class_id": 20, "method_id": 30, "index": 40},
                }],
            }

        def command(self, command_set, command, data=b""):
            self.commands.append((command_set, command, data))
            if (command_set, command) == (Cmd.THREAD, 3):
                return 0, b""
            if (command_set, command) in {(Cmd.EVENT, 2), (Cmd.VM, 1)}:
                return 0, b""
            raise AssertionError((command_set, command, data))

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._breakpoints = {
        "bp_001": {"breakpoint_id": "bp_001", "class": "LExample;", "method": "run()V", "line": 123}
    }
    client = FakeClient()
    runtime._connect = lambda: client
    runtime._describe_location = lambda jdwp, location: {
        "class": "LExample;",
        "method": "run()V",
        "line": 123,
    }
    runtime._thread_name = lambda jdwp, thread_id: "main"
    runtime._arm_debug_requests = lambda jdwp, kinds, control: (
        runtime._armed_breakpoint_requests.update({42: "bp_001"}) or None
    )

    result = runtime.wait_event(RuntimeAction(action="wait_event", timeout=1))

    assert result.error == ""
    assert result.data["status"] == "breakpoint_hit"
    assert result.data["breakpoint"]["line"] == 123
    assert client.waits == 2
    assert client.commands == [
        (Cmd.THREAD, 3, (10).to_bytes(8, "big")),
        (Cmd.EVENT, 2, struct.pack(">BI", EventKind.BREAKPOINT, 42)),
        (Cmd.VM, 1, b""),
    ]


def test_resume_uses_thread_resume_for_event_thread_suspension() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def __init__(self):
            self.commands = []

        def command(self, command_set, command, data=b""):
            self.commands.append((command_set, command, data))
            return 0, b""

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._active_suspension = SuspensionSnapshot(
        suspension_id="susp_thread",
        generation=1,
        request_id=42,
        thread_id=10,
        location={},
        observed_at="2026-07-04T00:00:00+00:00",
        suspend_policy=SuspendPolicy.EVENT_THREAD,
    )
    client = FakeClient()
    runtime._connect = lambda: client

    result = runtime.resume(RuntimeAction(action="resume", suspension_id="susp_thread"))

    assert result.error == ""
    assert result.data["resume_scope"] == "event_thread"
    assert result.data["suspend_policy_name"] == "EVENT_THREAD"
    assert result.data["debug_state"] == "attached"
    assert runtime._active_suspension is None
    assert client.commands == [(Cmd.THREAD, 3, (10).to_bytes(8, "big"))]


def test_resume_uses_vm_resume_for_suspend_all_suspension() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def __init__(self):
            self.commands = []

        def command(self, command_set, command, data=b""):
            self.commands.append((command_set, command, data))
            return 0, b""

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._active_suspension = SuspensionSnapshot(
        suspension_id="susp_vm",
        generation=1,
        request_id=42,
        thread_id=10,
        location={},
        observed_at="2026-07-04T00:00:00+00:00",
        suspend_policy=SuspendPolicy.ALL,
    )
    client = FakeClient()
    runtime._connect = lambda: client

    result = runtime.resume(RuntimeAction(action="resume", suspension_id="susp_vm"))

    assert result.error == ""
    assert result.data["resume_scope"] == "vm"
    assert result.data["suspend_policy_name"] == "SUSPEND_ALL"
    assert client.commands == [(Cmd.VM, 9, b"")]


def _jdwp_version_payload() -> bytes:
    description = b""
    vm_version = b"17"
    vm_name = b"OpenJDK"
    return (
        struct.pack(">I", len(description)) + description
        + struct.pack(">II", 17, 0)
        + struct.pack(">I", len(vm_version)) + vm_version
        + struct.pack(">I", len(vm_name)) + vm_name
    )


def test_status_auto_resumes_pending_event_without_creating_suspension() -> None:
    class FakeManagedProcess:
        pid = 4321
        jdwp_port = 5005
        launch_mode = "class"
        owned = True
        main_class = "Example"
        jar_path = ""

        def is_alive(self):
            return True

    class FakeProcessManager:
        current = FakeManagedProcess()
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def __init__(self):
            self.drained = False
            self.commands = []

        def drain_events(self):
            if self.drained:
                return []
            self.drained = True
            return [{
                "suspend_policy": SuspendPolicy.EVENT_THREAD,
                "events": [{
                    "kind": EventKind.BREAKPOINT,
                    "request_id": 42,
                    "thread_id": 10,
                    "location": {"class_id": 20, "method_id": 30, "index": 40},
                }],
            }]

        def command(self, command_set, command, data=b""):
            self.commands.append((command_set, command, data))
            if (command_set, command) == (Cmd.THREAD, 3):
                return 0, b""
            if (command_set, command) == (Cmd.VM, 1):
                return 0, _jdwp_version_payload()
            raise AssertionError((command_set, command, data))

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._breakpoints = {
        "bp_001": {"breakpoint_id": "bp_001", "class": "LExample;", "method": "run()V", "line": 123}
    }
    client = FakeClient()
    runtime._connect = lambda: client
    runtime._describe_location = lambda jdwp, location: {
        "class": "LExample;",
        "method": "run()V",
        "line": 123,
    }
    runtime._thread_name = lambda jdwp, thread_id: "main"

    result = runtime.status(RuntimeAction(action="status"))

    assert result.error == ""
    assert result.data["debug_state"] == "attached"
    assert result.data["orphan_events_resumed"] == 1
    assert result.data["suspension_id"] is None
    assert runtime._active_suspension is None
    assert client.commands[0] == (Cmd.THREAD, 3, (10).to_bytes(8, "big"))
    assert "Start wait_event" in result.data["suggested_next_step"]


def test_cleanup_debug_state_drains_resumes_and_clears_debug_requests() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def __init__(self):
            self.commands = []

        def drain_events(self):
            return [{
                "suspend_policy": SuspendPolicy.EVENT_THREAD,
                "events": [{
                    "kind": EventKind.BREAKPOINT,
                    "request_id": 41,
                    "thread_id": 11,
                    "location": {"class_id": 20, "method_id": 30, "index": 40},
                }],
            }]

        def command(self, command_set, command, data=b""):
            self.commands.append((command_set, command, data))
            return 0, b""

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._breakpoints = {
        "bp_001": {"breakpoint_id": "bp_001", "class": "LExample;", "method": "run()V", "line": 123}
    }
    runtime._exceptions = {
        91: {
            "exception_class": "Ljava/lang/NullPointerException;",
            "caught": True,
            "uncaught": True,
        }
    }
    runtime._active_suspension = SuspensionSnapshot(
        suspension_id="susp_active",
        generation=1,
        request_id=42,
        thread_id=10,
        location={},
        observed_at="2026-07-04T00:00:00+00:00",
        suspend_policy=SuspendPolicy.EVENT_THREAD,
    )
    client = FakeClient()
    runtime._connect = lambda: client
    runtime._armed_breakpoint_requests = {42: "bp_001"}
    runtime._armed_exception_requests = {91: 91}

    result = runtime.cleanup_debug_state(RuntimeAction(action="cleanup_debug_state"))

    assert result.error == ""
    assert result.data["status"] == "debug_state_cleaned"
    assert result.data["drained_events"] == 1
    assert result.data["resumed_active_suspension"] is True
    assert result.data["emergency_vm_resume"] is True
    assert result.data["cleared_breakpoint_ids"] == ["bp_001"]
    assert result.data["cleared_exception_ids"] == [91]
    assert runtime._breakpoints == {}
    assert runtime._exceptions == {}
    assert runtime._active_suspension is None
    assert client.commands == [
        (Cmd.THREAD, 3, (11).to_bytes(8, "big")),
        (Cmd.EVENT, 2, struct.pack(">BI", EventKind.BREAKPOINT, 42)),
        (Cmd.EVENT, 2, struct.pack(">BI", EventKind.EXCEPTION, 91)),
        (Cmd.THREAD, 3, (10).to_bytes(8, "big")),
        (Cmd.VM, 9, b""),
    ]
    assert "Call status" in result.data["suggested_next_step"]


def _runtime_with_fake_variable_response(stack_error: int, stack_data: bytes):
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def command(self, command_set, command, data=b""):
            if (command_set, command) == (Cmd.METHOD, 2):
                return 0, b"variable-table"
            if (command_set, command) == (Cmd.STACK, 1):
                return stack_error, stack_data
            raise AssertionError((command_set, command, data))

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._active_suspension = SuspensionSnapshot(
        suspension_id="susp_test",
        generation=1,
        request_id=1,
        thread_id=10,
        location={},
        observed_at="2026-07-04T00:00:00+00:00",
    )
    runtime._connect = lambda: FakeClient()
    runtime._resolve_thread_id = lambda jdwp, snapshot, name: 10
    runtime._read_frames = lambda jdwp, thread_id, count, start_index=0: [{
        "index": 0,
        "frame_id": 20,
        "class_id": 30,
        "method_id": 40,
        "location_index": 50,
        "class": "LFixture;",
        "method": "run()V",
        "line": 10,
        "is_native": False,
    }]
    runtime._visible_variables_for_location = lambda data, location: [
        Variable(name="actualNull", type_name="Ljava/lang/String;", slot=1),
        Variable(name="notReturned", type_name="Ljava/lang/String;", slot=2),
    ]
    runtime._thread_name = lambda jdwp, thread_id: "main"
    return runtime


def _runtime_with_fake_receiver_and_body_variables():
    captured: dict[str, list[int]] = {
        "requested_slots": [],
        "value_depths": [],
    }

    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def command(self, command_set, command, data=b""):
            if (command_set, command) == (Cmd.METHOD, 2):
                return 0, b"variable-table"
            if (command_set, command) == (Cmd.STACK, 1):
                requested_count = struct.unpack_from(">I", data, 16)[0]
                offset = 20
                slots = []
                for _ in range(requested_count):
                    slots.append(struct.unpack_from(">I", data, offset)[0])
                    offset += 5
                captured["requested_slots"] = slots
                values = struct.pack(">I", requested_count)
                for index in range(requested_count):
                    values += bytes([Tag.OBJECT]) + (100 + index).to_bytes(8, "big")
                return 0, values
            raise AssertionError((command_set, command, data))

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._active_suspension = SuspensionSnapshot(
        suspension_id="susp_test",
        generation=1,
        request_id=1,
        thread_id=10,
        location={},
        observed_at="2026-07-04T00:00:00+00:00",
    )
    runtime._connect = lambda: FakeClient()
    runtime._resolve_thread_id = lambda jdwp, snapshot, name: 10
    runtime._read_frames = lambda jdwp, thread_id, count, start_index=0: [{
        "index": 0,
        "frame_id": 20,
        "class_id": 30,
        "method_id": 40,
        "location_index": 50,
        "class": "LFixture;",
        "method": "run()V",
        "line": 10,
        "is_native": False,
    }]
    runtime._visible_variables_for_location = lambda data, location: [
        Variable(name="this", type_name="Lcom/example/WorkflowServiceImpl;", slot=0),
        Variable(name="body", type_name="Lcom/example/RequestBody;", slot=2),
    ]
    runtime._thread_name = lambda jdwp, thread_id: "main"

    def fake_read_value(
        jdwp,
        ids,
        tag,
        data,
        offset,
        depth=3,
        visited=None,
        **_kwargs,
    ):
        captured["value_depths"].append(depth)
        return {"value_index": len(captured["value_depths"]), "depth": depth}, offset + 8

    runtime._read_value = fake_read_value
    return runtime, captured


def test_getvalues_error_marks_every_variable_unavailable() -> None:
    runtime = _runtime_with_fake_variable_response(35, b"")

    result = runtime.variables(RuntimeAction(
        action="variables", suspension_id="susp_test"
    ))

    assert result.error == ""
    assert result.data["complete"] is False
    assert result.data["partial"] is True
    for variable in result.data["variables"]:
        assert variable["value_state"] == "unavailable"
        assert "value" not in variable
        assert "GetValues failed" in variable["error"]


def test_real_null_is_observed_but_missing_batch_value_is_unavailable() -> None:
    one_null_value = (
        struct.pack(">I", 1)
        + bytes([Tag.STRING])
        + (0).to_bytes(8, "big")
    )
    runtime = _runtime_with_fake_variable_response(0, one_null_value)

    result = runtime.variables(RuntimeAction(
        action="variables", suspension_id="susp_test"
    ))

    actual_null, not_returned = result.data["variables"]
    assert actual_null["value_state"] == "observed"
    assert actual_null["value"] is None
    assert "error" not in actual_null
    assert not_returned["value_state"] == "unavailable"
    assert "value" not in not_returned
    assert "JVM returned no value" in not_returned["error"]


def test_variables_skip_this_by_default_and_use_shallow_depth() -> None:
    runtime, captured = _runtime_with_fake_receiver_and_body_variables()

    result = runtime.variables(RuntimeAction(
        action="variables",
        suspension_id="susp_test",
    ))

    assert result.error == ""
    assert captured["requested_slots"] == [2]
    assert captured["value_depths"] == [1]
    assert result.data["include_this"] is False
    assert result.data["max_value_depth"] == 1
    assert result.data["variable_count"] == 1
    assert result.data["skipped_variable_count"] == 1
    assert result.data["variables"][0]["name"] == "body"
    assert result.data["skipped_variables"] == [{
        "name": "this",
        "type": "Lcom/example/WorkflowServiceImpl;",
        "slot": 0,
        "reason": "excluded_by_default",
        "hint": "Pass include_this=true to inspect the receiver object.",
    }]


def test_variables_can_include_this_and_increase_value_depth() -> None:
    runtime, captured = _runtime_with_fake_receiver_and_body_variables()

    result = runtime.variables(RuntimeAction(
        action="variables",
        suspension_id="susp_test",
        include_this=True,
        max_value_depth=4,
    ))

    assert result.error == ""
    assert captured["requested_slots"] == [0, 2]
    assert captured["value_depths"] == [4, 4]
    assert result.data["include_this"] is True
    assert result.data["max_value_depth"] == 4
    assert result.data["variable_count"] == 2
    assert result.data["skipped_variable_count"] == 0
    assert [item["name"] for item in result.data["variables"]] == ["this", "body"]
    assert result.data["skipped_variables"] == []


def test_variables_use_method_and_frame_id_sizes_when_building_payloads() -> None:
    captured = {}

    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(
            field_id_size=2,
            method_id_size=4,
            object_id_size=8,
            reference_type_id_size=6,
            frame_id_size=3,
        )

        def command(self, command_set, command, data=b""):
            if (command_set, command) == (Cmd.METHOD, 2):
                captured["variable_table_payload"] = data
                return 0, b"variable-table"
            if (command_set, command) == (Cmd.STACK, 1):
                captured["get_values_payload"] = data
                return 0, struct.pack(">I", 1) + bytes([Tag.INT]) + struct.pack(">i", 123)
            raise AssertionError((command_set, command, data))

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._active_suspension = SuspensionSnapshot(
        suspension_id="susp_test",
        generation=1,
        request_id=1,
        thread_id=0x0102030405060708,
        location={},
        observed_at="2026-07-04T00:00:00+00:00",
    )
    runtime._connect = lambda: FakeClient()
    runtime._resolve_thread_id = lambda jdwp, snapshot, name: snapshot.thread_id
    runtime._read_frames = lambda jdwp, thread_id, count, start_index=0: [{
        "index": 0,
        "frame_id": 0x010203,
        "class_id": 0x010203040506,
        "method_id": 0x11223344,
        "location_index": 50,
        "class": "LFixture;",
        "method": "run()V",
        "line": 10,
        "is_native": False,
    }]
    runtime._visible_variables_for_location = lambda data, location: [
        Variable(name="answer", type_name="I", slot=7),
    ]
    runtime._thread_name = lambda jdwp, thread_id: "main"

    result = runtime.variables(RuntimeAction(
        action="variables",
        suspension_id="susp_test",
    ))

    assert result.error == ""
    assert result.data["variables"][0]["value"] == 123
    assert captured["variable_table_payload"] == (
        bytes.fromhex("010203040506") + bytes.fromhex("11223344")
    )
    assert captured["get_values_payload"] == (
        bytes.fromhex("0102030405060708")
        + bytes.fromhex("010203")
        + struct.pack(">I", 1)
        + struct.pack(">I", 7)
        + bytes([Tag.INT])
    )


def _raw_value_bytes(tag: int, value, ids: IDSizes) -> bytes:
    if tag == Tag.INT:
        return struct.pack(">i", value)
    if tag == Tag.BOOLEAN:
        return bytes([1 if value else 0])
    if tag == Tag.LONG:
        return struct.pack(">q", value)
    if tag in {
        Tag.ARRAY,
        Tag.OBJECT,
        Tag.STRING,
        Tag.THREAD,
        Tag.THREAD_GROUP,
        Tag.CLASS_LOADER,
        Tag.CLASS_OBJECT,
    }:
        return ids.pack_obj(int(value or 0))
    raise AssertionError(f"Unsupported fake tag {tag}")


def _pack_tagged_value(tag: int, value, ids: IDSizes) -> bytes:
    return bytes([tag]) + _raw_value_bytes(tag, value, ids)


def _pack_fake_fields(fields: list[tuple[int, str, str]]) -> bytes:
    payload = struct.pack(">I", len(fields))
    for field_id, name, signature in fields:
        name_bytes = name.encode("utf-8")
        signature_bytes = signature.encode("utf-8")
        payload += field_id.to_bytes(8, "big")
        payload += struct.pack(">I", len(name_bytes)) + name_bytes
        payload += struct.pack(">I", len(signature_bytes)) + signature_bytes
        payload += struct.pack(">I", 0)
    return payload


class SemanticCollectionsFakeClient:
    ids = IDSizes(8, 8, 8, 8, 8)

    def __init__(self):
        self.object_types: dict[int, tuple[int, int]] = {}
        self.signatures: dict[int, str] = {}
        self.fields: dict[int, list[tuple[int, str, str]]] = {}
        self.superclasses: dict[int, int] = {}
        self.object_values: dict[int, dict[int, tuple[int, object]]] = {}
        self.arrays: dict[int, tuple[int, int, list[tuple[int, object]]]] = {}
        self.strings: dict[int, str] = {}

    def add_type(
        self,
        ref_type_id: int,
        signature: str,
        *,
        fields: list[tuple[int, str, str]] | None = None,
        superclass: int = 0,
    ) -> None:
        self.signatures[ref_type_id] = signature
        self.fields[ref_type_id] = fields or []
        self.superclasses[ref_type_id] = superclass

    def add_object(
        self,
        obj_id: int,
        ref_type_id: int,
        field_values: dict[int, tuple[int, object]] | None = None,
        *,
        type_tag: int = 1,
    ) -> None:
        self.object_types[obj_id] = (type_tag, ref_type_id)
        self.object_values[obj_id] = field_values or {}

    def add_array(
        self,
        obj_id: int,
        ref_type_id: int,
        signature: str,
        values: list[tuple[int, object]],
        *,
        element_tag: int = Tag.OBJECT,
    ) -> None:
        self.add_type(ref_type_id, signature)
        self.add_object(obj_id, ref_type_id, type_tag=3)
        self.arrays[obj_id] = (element_tag, ref_type_id, values)

    def command(self, command_set, command, data=b""):
        ids = self.ids
        if (command_set, command) == (Cmd.OBJ_REF, 1):
            obj_id = int.from_bytes(data[:ids.object_id_size], "big")
            type_tag, ref_type_id = self.object_types[obj_id]
            return 0, bytes([type_tag]) + ids.pack_ref(ref_type_id)
        if (command_set, command) == (Cmd.REF_TYPE, 1):
            ref_type_id = int.from_bytes(data[:ids.reference_type_id_size], "big")
            signature = self.signatures[ref_type_id].encode("utf-8")
            return 0, struct.pack(">I", len(signature)) + signature
        if (command_set, command) == (Cmd.REF_TYPE, 4):
            ref_type_id = int.from_bytes(data[:ids.reference_type_id_size], "big")
            return 0, _pack_fake_fields(self.fields.get(ref_type_id, []))
        if (command_set, command) == (Cmd.CLASS_TYPE, 1):
            ref_type_id = int.from_bytes(data[:ids.reference_type_id_size], "big")
            return 0, ids.pack_ref(self.superclasses.get(ref_type_id, 0))
        if (command_set, command) == (Cmd.OBJ_REF, 2):
            obj_id = int.from_bytes(data[:ids.object_id_size], "big")
            count = struct.unpack_from(">I", data, ids.object_id_size)[0]
            offset = ids.object_id_size + 4
            payload = struct.pack(">I", count)
            for _ in range(count):
                field_id = int.from_bytes(data[offset:offset + ids.field_id_size], "big")
                offset += ids.field_id_size
                tag, value = self.object_values[obj_id][field_id]
                payload += _pack_tagged_value(tag, value, ids)
            return 0, payload
        if (command_set, command) == (Cmd.ARRAY, 1):
            arr_id = int.from_bytes(data[:ids.object_id_size], "big")
            return 0, struct.pack(">I", len(self.arrays[arr_id][2]))
        if (command_set, command) == (Cmd.ARRAY, 2):
            arr_id = int.from_bytes(data[:ids.object_id_size], "big")
            first, length = struct.unpack_from(">II", data, ids.object_id_size)
            element_tag, _ref_type_id, values = self.arrays[arr_id]
            selected = values[first:first + length]
            payload = bytes([element_tag]) + struct.pack(">I", len(selected))
            for tag, value in selected:
                if JavaRuntime()._array_elements_are_tagged(element_tag):
                    payload += _pack_tagged_value(tag, value, ids)
                else:
                    payload += _raw_value_bytes(tag, value, ids)
            return 0, payload
        if (command_set, command) == (Cmd.STRING_REF, 1):
            obj_id = int.from_bytes(data[:ids.object_id_size], "big")
            value = self.strings[obj_id].encode("utf-8")
            return 0, struct.pack(">I", len(value)) + value
        raise AssertionError((command_set, command, data))


def _read_fake_object(
    client: SemanticCollectionsFakeClient,
    obj_id: int,
    *,
    depth: int = 2,
    semantic_collections: bool = True,
    item_limit: int = 16,
    map_entry_limit: int = 16,
):
    runtime = JavaRuntime()
    value, offset = runtime._read_value(
        client,
        client.ids,
        Tag.OBJECT,
        client.ids.pack_obj(obj_id),
        0,
        depth=depth,
        visited=set(),
        semantic_collections=semantic_collections,
        item_limit=item_limit,
        map_entry_limit=map_entry_limit,
    )
    assert offset == client.ids.object_id_size
    return value


def _read_fake_array(
    client: SemanticCollectionsFakeClient,
    arr_id: int,
    *,
    depth: int = 2,
    semantic_collections: bool = True,
    item_limit: int = 16,
):
    runtime = JavaRuntime()
    value, offset = runtime._read_value(
        client,
        client.ids,
        Tag.ARRAY,
        client.ids.pack_obj(arr_id),
        0,
        depth=depth,
        visited=set(),
        semantic_collections=semantic_collections,
        item_limit=item_limit,
        map_entry_limit=16,
    )
    assert offset == client.ids.object_id_size
    return value


def test_semantic_array_and_arraylist_output_use_logical_items() -> None:
    client = SemanticCollectionsFakeClient()
    client.strings = {301: "Alice", 302: "Bob", 303: "Carol"}
    client.add_array(
        200,
        2000,
        "[Ljava/lang/String;",
        [(Tag.STRING, 301), (Tag.STRING, 302), (Tag.STRING, 303)],
    )
    client.add_type(
        1000,
        "Ljava/util/ArrayList;",
        fields=[
            (1, "size", "I"),
            (2, "elementData", "[Ljava/lang/Object;"),
        ],
    )
    client.add_object(100, 1000, {
        1: (Tag.INT, 2),
        2: (Tag.ARRAY, 200),
    })

    array_value = _read_fake_array(client, 200, item_limit=2)
    list_value = _read_fake_object(client, 100)

    assert array_value == {
        "_ref": "0xc8",
        "_kind": "array",
        "_class": "[Ljava.lang.String;",
        "length": 3,
        "items": ["Alice", "Bob"],
        "truncated": True,
        "item_limit": 2,
    }
    assert list_value == {
        "_ref": "0x64",
        "_kind": "list",
        "_class": "java.util.ArrayList",
        "size": 2,
        "items": ["Alice", "Bob"],
        "truncated": False,
        "item_limit": 16,
    }


def test_semantic_collections_false_keeps_raw_arraylist_fields() -> None:
    client = SemanticCollectionsFakeClient()
    client.add_array(200, 2000, "[Ljava/lang/Object;", [])
    client.add_type(
        1000,
        "Ljava/util/ArrayList;",
        fields=[
            (1, "size", "I"),
            (2, "elementData", "[Ljava/lang/Object;"),
        ],
    )
    client.add_object(100, 1000, {
        1: (Tag.INT, 0),
        2: (Tag.ARRAY, 200),
    })

    value = _read_fake_object(client, 100, semantic_collections=False)

    assert value["_kind"] == "object"
    assert value["_class"] == "Ljava/util/ArrayList;"
    assert value["size"] == 0
    assert value["elementData"]["_kind"] == "array"
    assert value["elementData"]["_length"] == 0
    assert "items" not in value["elementData"]


def test_semantic_collection_items_respect_visited_refs() -> None:
    client = SemanticCollectionsFakeClient()
    client.add_array(
        200,
        2000,
        "[Ljava/lang/Object;",
        [(Tag.OBJECT, 100)],
    )
    client.add_type(
        1000,
        "Ljava/util/ArrayList;",
        fields=[
            (1, "size", "I"),
            (2, "elementData", "[Ljava/lang/Object;"),
        ],
    )
    client.add_object(100, 1000, {
        1: (Tag.INT, 1),
        2: (Tag.ARRAY, 200),
    })

    value = _read_fake_object(client, 100, depth=3)

    assert value["_kind"] == "list"
    assert value["items"] == [{"_ref": "0x64", "_kind": "object"}]
    assert "value_state" not in value


def test_semantic_linked_list_traverses_nodes_without_exposing_node_fields() -> None:
    client = SemanticCollectionsFakeClient()
    client.strings = {301: "first", 302: "second"}
    client.add_type(
        1100,
        "Ljava/util/LinkedList;",
        fields=[(1, "size", "I"), (2, "first", "Ljava/util/LinkedList$Node;")],
    )
    client.add_type(
        1200,
        "Ljava/util/LinkedList$Node;",
        fields=[
            (3, "item", "Ljava/lang/Object;"),
            (4, "next", "Ljava/util/LinkedList$Node;"),
        ],
    )
    client.add_object(100, 1100, {1: (Tag.INT, 2), 2: (Tag.OBJECT, 201)})
    client.add_object(201, 1200, {3: (Tag.STRING, 301), 4: (Tag.OBJECT, 202)})
    client.add_object(202, 1200, {3: (Tag.STRING, 302), 4: (Tag.OBJECT, 0)})

    value = _read_fake_object(client, 100)

    assert value["_kind"] == "list"
    assert value["_class"] == "java.util.LinkedList"
    assert value["size"] == 2
    assert value["items"] == ["first", "second"]
    assert "value_state" not in value


def test_semantic_linked_hash_map_reads_inherited_hashmap_table() -> None:
    client = SemanticCollectionsFakeClient()
    client.strings = {301: "id", 302: "42"}
    client.add_type(
        1000,
        "Ljava/util/HashMap;",
        fields=[(1, "size", "I"), (2, "table", "[Ljava/util/HashMap$Node;")],
    )
    client.add_type(1001, "Ljava/util/LinkedHashMap;", superclass=1000)
    client.add_type(
        1200,
        "Ljava/util/HashMap$Node;",
        fields=[
            (3, "key", "Ljava/lang/Object;"),
            (4, "value", "Ljava/lang/Object;"),
            (5, "next", "Ljava/util/HashMap$Node;"),
        ],
    )
    client.add_array(
        200,
        2000,
        "[Ljava/util/HashMap$Node;",
        [(Tag.OBJECT, 0), (Tag.OBJECT, 201)],
    )
    client.add_object(100, 1001, {1: (Tag.INT, 1), 2: (Tag.ARRAY, 200)})
    client.add_object(201, 1200, {
        3: (Tag.STRING, 301),
        4: (Tag.STRING, 302),
        5: (Tag.OBJECT, 0),
    })

    value = _read_fake_object(client, 100)

    assert value == {
        "_ref": "0x64",
        "_kind": "map",
        "_class": "java.util.LinkedHashMap",
        "size": 1,
        "entries": [{"key": "id", "value": "42"}],
        "truncated": False,
        "entry_limit": 16,
    }


def test_semantic_linked_hash_set_reads_internal_map_keys() -> None:
    client = SemanticCollectionsFakeClient()
    client.strings = {301: "red", 302: "blue"}
    client.add_type(
        1000,
        "Ljava/util/HashSet;",
        fields=[(1, "map", "Ljava/util/HashMap;")],
    )
    client.add_type(1001, "Ljava/util/LinkedHashSet;", superclass=1000)
    client.add_type(
        1100,
        "Ljava/util/HashMap;",
        fields=[(2, "size", "I"), (3, "table", "[Ljava/util/HashMap$Node;")],
    )
    client.add_type(
        1200,
        "Ljava/util/HashMap$Node;",
        fields=[
            (4, "key", "Ljava/lang/Object;"),
            (5, "value", "Ljava/lang/Object;"),
            (6, "next", "Ljava/util/HashMap$Node;"),
        ],
    )
    client.add_array(
        300,
        2000,
        "[Ljava/util/HashMap$Node;",
        [(Tag.OBJECT, 201), (Tag.OBJECT, 202)],
    )
    client.add_object(100, 1001, {1: (Tag.OBJECT, 150)})
    client.add_object(150, 1100, {2: (Tag.INT, 2), 3: (Tag.ARRAY, 300)})
    client.add_object(201, 1200, {
        4: (Tag.STRING, 301),
        5: (Tag.OBJECT, 999),
        6: (Tag.OBJECT, 0),
    })
    client.add_object(202, 1200, {
        4: (Tag.STRING, 302),
        5: (Tag.OBJECT, 999),
        6: (Tag.OBJECT, 0),
    })

    value = _read_fake_object(client, 100)

    assert value["_kind"] == "set"
    assert value["_class"] == "java.util.LinkedHashSet"
    assert value["size"] == 2
    assert value["items"] == ["red", "blue"]
    assert value["truncated"] is False


def test_semantic_optional_present_and_empty() -> None:
    client = SemanticCollectionsFakeClient()
    client.strings = {301: "present"}
    client.add_type(
        1000,
        "Ljava/util/Optional;",
        fields=[(1, "value", "Ljava/lang/Object;")],
    )
    client.add_object(100, 1000, {1: (Tag.STRING, 301)})
    client.add_object(101, 1000, {1: (Tag.OBJECT, 0)})

    present = _read_fake_object(client, 100)
    empty = _read_fake_object(client, 101)

    assert present == {
        "_ref": "0x64",
        "_kind": "optional",
        "_class": "java.util.Optional",
        "present": True,
        "value": "present",
    }
    assert empty == {
        "_ref": "0x65",
        "_kind": "optional",
        "_class": "java.util.Optional",
        "present": False,
        "value": None,
    }


def test_semantic_collection_failure_is_unavailable_not_empty() -> None:
    client = SemanticCollectionsFakeClient()
    client.add_type(
        1000,
        "Ljava/util/ArrayList;",
        fields=[(1, "size", "I")],
    )
    client.add_object(100, 1000, {1: (Tag.INT, 2)})

    value = _read_fake_object(client, 100)
    variable = Variable(
        name="names",
        type_name="Ljava/util/ArrayList;",
        slot=1,
        value=value,
        value_observed=True,
    )

    observed = JavaRuntime()._variable_observation(variable)

    assert value["size"] == 2
    assert value["items"] == []
    assert value["value_state"] == "unavailable"
    assert "elementData" in value["error"]
    assert observed["value_state"] == "unavailable"
    assert observed["value"] == value


def test_object_field_reads_use_field_id_size() -> None:
    captured = {}

    class FakeClient:
        ids = IDSizes(
            field_id_size=2,
            method_id_size=4,
            object_id_size=8,
            reference_type_id_size=6,
            frame_id_size=3,
        )

        def command(self, command_set, command, data=b""):
            if (command_set, command) == (Cmd.OBJ_REF, 1):
                return 0, bytes([1]) + bytes.fromhex("010203040506")
            if (command_set, command) == (Cmd.REF_TYPE, 1):
                signature = b"LFixture;"
                return 0, struct.pack(">I", len(signature)) + signature
            if (command_set, command) == (Cmd.REF_TYPE, 4):
                name = b"age"
                signature = b"I"
                return 0, (
                    struct.pack(">I", 1)
                    + bytes.fromhex("1234")
                    + struct.pack(">I", len(name))
                    + name
                    + struct.pack(">I", len(signature))
                    + signature
                    + struct.pack(">I", 0)
                )
            if (command_set, command) == (Cmd.OBJ_REF, 2):
                captured["get_values_payload"] = data
                return 0, struct.pack(">I", 1) + bytes([Tag.INT]) + struct.pack(">i", 20)
            raise AssertionError((command_set, command, data))

    runtime = JavaRuntime()

    value = runtime._read_object(
        FakeClient(),
        FakeClient.ids,
        obj_id=0x0102030405060708,
        depth=1,
        visited=set(),
    )

    assert value["age"] == 20
    assert captured["get_values_payload"] == (
        bytes.fromhex("0102030405060708")
        + struct.pack(">I", 1)
        + bytes.fromhex("1234")
    )


@pytest.mark.skipif(
    shutil.which("java") is None or shutil.which("javac") is None,
    reason="JDK is required for the real JDWP integration test",
)
def test_real_jvm_breakpoint_snapshot_variables_and_resume(tmp_path: Path) -> None:
    source = """\
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

public class DebugFixture {
    public static void main(String[] args) throws Exception {
        Path trigger = Paths.get(args[0]);
        while (!Files.exists(trigger)) {
            Thread.sleep(20);
        }
        String sex = "男";
        String missing = null;
        int answer = 42;
        System.out.println(sex + answer);
        Thread.sleep(30000);
    }
}
"""
    source_path = tmp_path / "DebugFixture.java"
    source_path.write_text(source, encoding="utf-8")
    subprocess.run(
        ["javac", "-encoding", "UTF-8", "-g", str(source_path)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    breakpoint_line = next(
        index for index, line in enumerate(source.splitlines(), start=1)
        if "System.out.println" in line
    )
    trigger = tmp_path / "trigger"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    runtime = JavaRuntime()
    try:
        started = runtime.run(RuntimeAction(
            action="run",
            classpath=str(tmp_path),
            main_class="DebugFixture",
            app_args=[str(trigger)],
            jdwp_port=port,
        ))
        assert started.error == ""

        breakpoint = runtime.breakpoint(RuntimeAction(
            action="breakpoint",
            bp_action="set",
            class_pattern="DebugFixture",
            line=breakpoint_line,
        ))
        assert breakpoint.error == ""

        trigger_timer = threading.Timer(
            0.2,
            lambda: trigger.write_text("go", encoding="utf-8"),
        )
        trigger_timer.start()
        try:
            hit = runtime.wait_breakpoint(RuntimeAction(
                action="wait_breakpoint",
                timeout=10,
            ))
        finally:
            trigger_timer.join(timeout=2)
        assert hit.error == ""
        assert hit.data["status"] == "breakpoint_hit"
        suspension_id = hit.data["suspension_id"]
        assert hit.data["location"]["line"] == breakpoint_line

        status = runtime.status(RuntimeAction(action="status"))
        assert status.data["debug_state"] == "suspended"
        assert status.data["suspension_id"] == suspension_id

        threads = runtime.threads(RuntimeAction(
            action="threads",
            suspension_id=suspension_id,
        ))
        assert threads.error == ""
        assert any(
            thread["name"] == "main" and thread["is_breakpoint_thread"]
            for thread in threads.data["threads"]
        )

        stack = runtime.stack(RuntimeAction(
            action="stack",
            suspension_id=suspension_id,
        ))
        assert stack.error == ""
        assert stack.data["frames"][0]["class"] == "LDebugFixture;"

        variables = runtime.variables(RuntimeAction(
            action="variables",
            suspension_id=suspension_id,
            frame_index=0,
        ))
        assert variables.error == ""
        values = {item["name"]: item["value"] for item in variables.data["variables"]}
        assert values["sex"] == "男"
        assert values["missing"] is None
        assert values["answer"] == 42
        missing = next(
            item for item in variables.data["variables"] if item["name"] == "missing"
        )
        assert missing["value_state"] == "observed"
        assert "error" not in missing
        assert "\\u7537" not in variables.to_json()

        resumed = runtime.resume(RuntimeAction(
            action="resume",
            suspension_id=suspension_id,
        ))
        assert resumed.error == ""
        assert resumed.data["invalidated_suspension_id"] == suspension_id

        stale = runtime.variables(RuntimeAction(
            action="variables",
            suspension_id=suspension_id,
        ))
        assert "No active debug suspension" in stale.error
    finally:
        runtime.stop(RuntimeAction(action="stop"))


@pytest.mark.skipif(
    shutil.which("java") is None or shutil.which("javac") is None,
    reason="JDK is required for the real JDWP integration test",
)
def test_real_jvm_caught_exception_event_suspends_at_throw_line(tmp_path: Path) -> None:
    source = """\
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

public class ExceptionFixture {
    public static void main(String[] args) throws Exception {
        Class.forName("java.lang.NullPointerException");
        Path trigger = Paths.get(args[0]);
        while (!Files.exists(trigger)) {
            Thread.sleep(20);
        }
        try {
            String value = null;
            value.equals("boom");
        } catch (NullPointerException e) {
            System.out.println("caught npe");
            Thread.sleep(30000);
        }
    }
}
"""
    source_path = tmp_path / "ExceptionFixture.java"
    source_path.write_text(source, encoding="utf-8")
    subprocess.run(
        ["javac", "-encoding", "UTF-8", "-g", str(source_path)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    throw_line = next(
        index for index, line in enumerate(source.splitlines(), start=1)
        if 'value.equals("boom")' in line
    )
    trigger = tmp_path / "trigger"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    runtime = JavaRuntime()
    try:
        started = runtime.run(RuntimeAction(
            action="run",
            classpath=str(tmp_path),
            main_class="ExceptionFixture",
            app_args=[str(trigger)],
            jdwp_port=port,
        ))
        assert started.error == ""

        exception = runtime.exception(RuntimeAction(
            action="exception",
            exception_class="java.lang.NullPointerException",
        ))
        assert exception.error == ""
        assert exception.data["exception_class"] == "Ljava/lang/NullPointerException;"
        assert exception.data["caught"] is True
        assert exception.data["uncaught"] is True

        trigger_timer = threading.Timer(
            0.2,
            lambda: trigger.write_text("go", encoding="utf-8"),
        )
        trigger_timer.start()
        try:
            hit = runtime.wait_event(RuntimeAction(
                action="wait_event",
                timeout=10,
            ))
        finally:
            trigger_timer.join(timeout=2)

        assert hit.error == ""
        assert hit.data["status"] == "exception_hit"
        assert hit.data["event_kind"] == "exception"
        assert hit.data["exception"]["exception_class"] == "Ljava/lang/NullPointerException;"
        assert hit.data["exception"]["thrown_class"] == "Ljava/lang/NullPointerException;"
        assert hit.data["exception"]["caught"] is True
        assert hit.data["location"]["class"] == "LExceptionFixture;"
        assert hit.data["location"]["line"] == throw_line
        assert hit.data["catch_location"] is not None

        resumed = runtime.resume(RuntimeAction(
            action="resume",
            suspension_id=hit.data["suspension_id"],
        ))
        assert resumed.error == ""
    finally:
        runtime.stop(RuntimeAction(action="stop"))


@pytest.mark.skipif(
    any(shutil.which(command) is None for command in ("java", "javac", "jar")),
    reason="JDK java, javac, and jar commands are required",
)
def test_real_executable_jar_launch_and_utf8_logs(tmp_path: Path) -> None:
    source_path = tmp_path / "JarFixture.java"
    source_path.write_text(
        """\
import java.nio.file.Files;
import java.nio.file.Paths;

public class JarFixture {
    public static void main(String[] args) throws Exception {
        System.out.println("Spring 启动成功");
        while (!Files.exists(Paths.get(args[0]))) {
            Thread.sleep(20);
        }
    }
}
""",
        encoding="utf-8",
    )
    subprocess.run(
        ["javac", "-encoding", "UTF-8", "-g", str(source_path)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    jar_path = tmp_path / "fixture-app.jar"
    subprocess.run(
        [
            "jar", "cfe", str(jar_path), "JarFixture",
            "-C", str(tmp_path), "JarFixture.class",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    stop_file = tmp_path / "stop"
    runtime = JavaRuntime()
    try:
        started = runtime.run(RuntimeAction(
            action="run",
            jar_path=str(jar_path),
            app_args=[str(stop_file)],
            jdwp_port=port,
            vm_args=["-Dfile.encoding=UTF-8"],
        ))

        assert started.error == ""
        assert started.data["launch_mode"] == "jar"
        assert started.data["jar_path"] == str(jar_path)
        assert "main_class" not in started.data

        status = runtime.status(RuntimeAction(action="status"))
        assert status.data["running"] is True
        assert status.data["launch_mode"] == "jar"
        assert status.data["jar_path"] == str(jar_path)

        logs = runtime.logs(RuntimeAction(action="logs", tail=10))
        assert logs.error == ""
        assert any("Spring 启动成功" in line for line in logs.data["lines"])
    finally:
        stop_file.write_text("stop", encoding="utf-8")
        runtime.stop(RuntimeAction(action="stop"))


@pytest.mark.skipif(
    shutil.which("java") is None or shutil.which("javac") is None,
    reason="JDK is required for the real JDWP integration test",
)
def test_attach_and_detach_leave_external_jvm_running(tmp_path: Path) -> None:
    source_path = tmp_path / "AttachFixture.java"
    source_path.write_text(
        """\
import java.nio.file.Files;
import java.nio.file.Paths;

public class AttachFixture {
    public static void main(String[] args) throws Exception {
        while (!Files.exists(Paths.get(args[0]))) {
            Thread.sleep(20);
        }
    }
}
""",
        encoding="utf-8",
    )
    subprocess.run(
        ["javac", "-encoding", "UTF-8", "-g", str(source_path)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    stop_file = tmp_path / "stop"
    process = subprocess.Popen(
        [
            "java",
            f"-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,"
            f"address=127.0.0.1:{port}",
            "-cp",
            str(tmp_path),
            "AttachFixture",
            str(stop_file),
        ],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    runtime = JavaRuntime()
    try:
        deadline = time.monotonic() + 10
        while True:
            attached = runtime.attach(RuntimeAction(
                action="attach",
                pid=process.pid,
                jdwp_port=port,
                main_class="AttachFixture",
            ))
            if not attached.error:
                break
            if time.monotonic() >= deadline:
                pytest.fail(attached.error)
            time.sleep(0.1)

        status = runtime.status(RuntimeAction(action="status"))
        assert status.data["ownership"] == "attached"

        detached = runtime.detach(RuntimeAction(action="detach"))
        assert detached.error == ""
        assert detached.data == {"status": "detached", "pid": process.pid}
        assert process.poll() is None
    finally:
        stop_file.write_text("stop", encoding="utf-8")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
