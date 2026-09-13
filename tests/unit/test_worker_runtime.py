import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from jolink_runtime.launch import runtime_download, worker_runtime
from jolink_runtime.launch.jdt_compile_session import JdtCandidate
from jolink_runtime.launch.worker_runtime import (
    WorkerRuntimeError,
    managed_worker_java_home,
)


@pytest.mark.parametrize(
    ("system", "machine", "key"),
    [
        ("Darwin", "arm64", "Darwin-arm64"),
        ("Linux", "aarch64", "Linux-arm64"),
        ("Windows", "AMD64", "Windows-x86_64"),
    ],
)
def test_runtime_install_once_and_offline_reuse(
    tmp_path, monkeypatch, system, machine, key
):
    monkeypatch.delenv("JOLINK_WORKER_JAVA_HOME", raising=False)
    monkeypatch.delenv("JOLINK_DOWNLOAD_MIRROR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(worker_runtime.platform, "system", lambda: system)
    monkeypatch.setattr(worker_runtime.platform, "machine", lambda: machine)
    relative = "jdk-21.0.12.1+1/" + ("Contents/Home/" if system == "Darwin" else "")
    executable = "java.exe" if system == "Windows" else "java"
    payload = io.BytesIO()
    files = {
        relative + "bin/" + executable: b"java",
        relative + "legal/LICENSE": b"licenses",
    }
    if system == "Windows":
        with zipfile.ZipFile(payload, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
    else:
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                member.mode = 0o755
                archive.addfile(member, io.BytesIO(data))
    data = payload.getvalue()
    lock = {
        "version": "21.0.12.1+1",
        "release_url": "https://example.invalid",
        "distributions": {
            key: {
                "filename": "jdk.zip" if system == "Windows" else "jdk.tar.gz",
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        },
    }
    original_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda p, *a, **kw: (
            json.dumps(lock)
            if p.name == "worker-runtime.json"
            else original_read(p, *a, **kw)
        ),
    )
    downloads = []

    def download(request, **kwargs):
        downloads.append(request.full_url)
        return io.BytesIO(data)

    monkeypatch.setattr(runtime_download.urllib.request, "urlopen", download)
    home = managed_worker_java_home()
    assert (home / "bin" / executable).read_bytes() == b"java"
    assert (home / "legal/LICENSE").is_file()

    def no_network(*args, **kwargs):
        raise AssertionError("reused runtime downloaded again")

    monkeypatch.setattr(runtime_download.urllib.request, "urlopen", no_network)
    monkeypatch.setattr(runtime_download.hashlib, "sha256", no_network)
    for source in ("official", "cn", "https://custom.invalid/mirror"):
        monkeypatch.setenv("JOLINK_DOWNLOAD_MIRROR", source)
        assert managed_worker_java_home() == home
    assert len(downloads) == 1


def test_product_uses_managed_runtime_without_reprobing(tmp_path, monkeypatch):
    monkeypatch.delenv("JOLINK_WORKER_JAVA_HOME", raising=False)
    home = tmp_path / "managed-jdk"
    monkeypatch.setattr(worker_runtime, "managed_worker_java_home", lambda: home)

    def no_probe(*args):
        raise AssertionError("cached official runtime was probed again")

    monkeypatch.setattr(JdtCandidate, "verify_worker_java", no_probe)
    candidate = JdtCandidate(
        "test", tmp_path, tmp_path / "launcher", None, {"worker_runtime": "temurin-21"}
    )
    selected = candidate.select_worker_java((tmp_path / "project-jdk8",))
    assert selected.home == home and selected.major == 21 and selected.data_model == 64


def test_enterprise_worker_jdk_override_does_not_download(tmp_path, monkeypatch):
    monkeypatch.setenv("JOLINK_WORKER_JAVA_HOME", str(tmp_path))
    monkeypatch.setenv("JAVA_HOME", "project-build-jdk")
    assert managed_worker_java_home() == tmp_path
    assert worker_runtime.os.environ["JAVA_HOME"] == "project-build-jdk"


def test_bad_archive_is_not_published(tmp_path, monkeypatch):
    monkeypatch.delenv("JOLINK_WORKER_JAVA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(worker_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(worker_runtime.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        runtime_download.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"bad")
    )
    with pytest.raises(WorkerRuntimeError, match="checksum"):
        managed_worker_java_home()
    assert not list(tmp_path.rglob(".install-*"))
    assert not list(tmp_path.rglob("java"))
