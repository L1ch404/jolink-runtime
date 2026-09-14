import base64
import hashlib
import io
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import anyio
import pytest

from jolink_runtime.launch import runtime_download, runtime_install, runtime_preparation
from jolink_runtime.launch.jdt_compile_session import JdtCandidate, JdtCompileError
from jolink_runtime.launch.runtime_install import PreparationCancelled, report_progress
from jolink_runtime.launch.runtime_preparation import RuntimePreparation
from jolink_runtime.server.mcp_server import RuntimeMCPBoundary


def test_failed_batch_retains_verified_jars(tmp_path, monkeypatch):
    package = Path(__file__).resolve().parents[2] / "src/jolink_runtime/launch"
    worker = base64.b64decode((package / "jdt-product-worker.jar.b64").read_bytes())
    config = (package / "jdt-product-config.ini").read_bytes()
    lock = {
        "candidate_id": "cache-test",
        "repository_url": "https://download.eclipse.org/test",
        "worker_java_minimum": 17,
        "worker_class_major": 61,
        "artifacts": [
            {"filename": name, "sha256": hashlib.sha256(name.encode()).hexdigest()}
            for name in ("first.jar", "second.jar")
        ],
        "worker_artifact": {
            "filename": "worker.jar",
            "sha256": hashlib.sha256(worker).hexdigest(),
        },
        "equinox": {
            "launcher_filename": "first.jar",
            "configuration_sha256": hashlib.sha256(config).hexdigest(),
        },
    }
    root = tmp_path / "candidate" / "identity"
    requests = []
    fail = True

    def download(url, destination, *, artifact):
        requests.append(artifact)
        if artifact == "second.jar" and fail:
            raise URLError(TimeoutError("network timeout"))
        destination.write_bytes(artifact.encode())
        return hashlib.sha256(artifact.encode()).hexdigest()

    monkeypatch.setattr(
        JdtCandidate, "_download_product_artifact", staticmethod(download)
    )
    with pytest.raises(JdtCompileError):
        JdtCandidate._install_product_candidate(
            lock, product_root=root, legacy_roots=()
        )
    assert not root.exists()
    assert (root.parent / "plugins/first.jar").read_bytes() == b"first.jar"
    assert not list(root.parent.glob("*.tmp"))
    fail = False
    JdtCandidate._install_product_candidate(lock, product_root=root, legacy_roots=())
    assert requests == ["first.jar", "second.jar", "second.jar"]
    assert JdtCandidate._load_root(lock, root).root == root


def test_background_preparation_is_shared_and_ready_is_hidden(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    candidate = SimpleNamespace(select_worker_java=lambda: "jdk")

    def load():
        calls.append(1)
        report_progress(
            phase="download_jdt",
            current_artifact="core.jar",
            completed_files=0,
            total_files=26,
        )
        entered.set()
        assert release.wait(3)
        return candidate

    monkeypatch.setattr(JdtCandidate, "load_product", load)
    monkeypatch.setattr(runtime_preparation, "_active", None)
    preparation = RuntimePreparation()
    try:
        preparation.start()
        assert entered.wait(2)
        assert preparation.snapshot()["current_artifact"] == "core.jar"
        result = []
        waiter = threading.Thread(target=lambda: result.append(preparation.result()))
        waiter.start()
        assert preparation.snapshot()["state"] == "preparing"
        release.set()
        waiter.join(3)
        assert result == [(candidate, "jdk")]
        assert preparation.result() == (candidate, "jdk")
        assert calls == [1]
        assert preparation.snapshot() is None
    finally:
        release.set()
        preparation.close()


def test_failed_warmup_can_retry_on_request(monkeypatch):
    calls = []
    candidate = SimpleNamespace(select_worker_java=lambda: "jdk")

    def load():
        calls.append(1)
        if len(calls) == 1:
            raise URLError(TimeoutError("connect timed out"))
        return candidate

    monkeypatch.setattr(JdtCandidate, "load_product", load)
    monkeypatch.setattr(runtime_preparation, "_active", None)
    preparation = RuntimePreparation()
    try:
        with pytest.raises(URLError):
            preparation.start().result(timeout=2)
        assert preparation.snapshot() is None
        assert "timed out" in preparation.snapshot(requested=True)["error"]["reason"]
        assert preparation.result() == (candidate, "jdk")
        assert preparation.snapshot() is None
    finally:
        preparation.close()


def test_closing_does_not_join_network_thread(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def load():
        entered.set()
        release.wait(3)
        return SimpleNamespace(select_worker_java=lambda: "jdk")

    monkeypatch.setattr(JdtCandidate, "load_product", load)
    monkeypatch.setattr(runtime_preparation, "_active", None)
    preparation = RuntimePreparation()
    try:
        preparation.start()
        assert entered.wait(2)
        before = time.monotonic()
        preparation.close()
        assert time.monotonic() - before < 0.3
        with pytest.raises(PreparationCancelled):
            preparation.result()
    finally:
        release.set()
        preparation._thread.join(3)


def test_local_file_error_is_not_retried(tmp_path, monkeypatch, caplog):
    requests = []
    monkeypatch.setenv("JOLINK_DOWNLOAD_MIRROR", "official")

    def open_url(request, **kwargs):
        requests.append(request.full_url)
        return io.BytesIO(b"content")

    monkeypatch.setattr(runtime_download.urllib.request, "urlopen", open_url)
    target = tmp_path / "cannot-open"
    target.mkdir()
    with pytest.raises(OSError):
        runtime_download.download_file(
            "https://example.invalid/a.jar", target, mirror_path=None
        )
    assert len(requests) == 1
    assert "phase=open_local_file" in caplog.text and "errno" in caplog.text


def test_network_retry_records_reason_and_succeeds(tmp_path, monkeypatch, caplog):
    requests = []
    monkeypatch.setenv("JOLINK_DOWNLOAD_MIRROR", "official")

    def open_url(request, **kwargs):
        requests.append(request.full_url)
        if len(requests) == 1:
            raise URLError(TimeoutError("test connection timed out"))
        return io.BytesIO(b"whole")

    monkeypatch.setattr(runtime_download.urllib.request, "urlopen", open_url)
    actual = runtime_download.download_file(
        "https://example.invalid/a.jar", tmp_path / "a.jar", mirror_path=None
    )
    assert actual == hashlib.sha256(b"whole").hexdigest()
    assert len(requests) == 2
    assert "test connection timed out" in caplog.text


@pytest.mark.parametrize(("system", "mode"), [("posix", 0o700), ("nt", 0o777)])
def test_runtime_directory_passes_platform_mode(tmp_path, monkeypatch, system, mode):
    monkeypatch.setattr(runtime_install, "os", SimpleNamespace(name=system))
    original = Path.mkdir
    observed = []

    def mkdir(path, *args, **kwargs):
        observed.append(kwargs["mode"])
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    runtime_install.create_directory(tmp_path / "runtime")
    assert observed == [mode]


def test_windows_error_details_and_url_credentials():
    error = PermissionError(13, "access denied")
    error.winerror = 5
    wrapped = RuntimeError("installation failed")
    wrapped.__cause__ = error
    details = runtime_install.error_details(wrapped)
    assert details["errno"] == 13 and details["winerror"] == 5
    assert details["cause_type"] == "PermissionError"
    details = runtime_install.error_details(
        URLError("https://account:password@proxy.invalid/file?token=secret")
    )
    assert "password" not in str(details) and "secret" not in str(details)


def test_progress_does_not_replace_project_error_or_ready_reply(tmp_path):
    class Dispatcher:
        def __init__(self):
            self.payload = {
                "ok": False,
                "error_code": "PROJECT_ERROR",
                "suggested_next_step": "Fix project configuration.",
            }

        def dispatch(self, *args, **kwargs):
            return dict(self.payload)

    dispatcher = Dispatcher()
    preparation = RuntimePreparation()
    preparation.update(
        state="preparing", phase="download_jdt", current_artifact="core.jar"
    )
    boundary = RuntimeMCPBoundary(dispatcher, preparation=preparation)

    async def scenario():
        failed = await boundary.call_tool(
            "java_application", {"action": "test", "project_path": str(tmp_path)}
        )
        assert "runtime_preparation" not in failed.structuredContent
        assert (
            failed.structuredContent["suggested_next_step"]
            == "Fix project configuration."
        )
        dispatcher.payload = {"ok": True, "status": "bootstrapping"}
        pending = await boundary.call_tool(
            "java_application", {"action": "test", "project_path": str(tmp_path)}
        )
        assert pending.structuredContent["runtime_preparation"]["state"] == "preparing"
        preparation.update(state="ready")
        ready = await boundary.call_tool("java_status", {"action": "status"})
        assert "runtime_preparation" not in ready.structuredContent

    anyio.run(scenario)
