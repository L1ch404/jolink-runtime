import hashlib
import http.client
import io
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

import pytest

from jolink_runtime.launch import runtime_download
from jolink_runtime.launch.jdt_compile_session import JdtCandidate


@contextmanager
def download_server(mirror_status=200, *, tuna_status=200, jolink_status=200):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            statuses = {
                "/mirror/": mirror_status,
                "/tuna/": tuna_status,
                "/jolink/": jolink_status,
            }
            status = next(
                (
                    value
                    for prefix, value in statuses.items()
                    if self.path.startswith(prefix)
                ),
                200,
            )
            payload = b"pinned official bytes"
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


@pytest.mark.parametrize("mirror_status", [200, 403, 404, 503])
def test_real_http_mirror_then_official(tmp_path, monkeypatch, mirror_status):
    with download_server(mirror_status) as (base, requests):
        monkeypatch.setenv("JOLINK_DOWNLOAD_MIRROR", base + "/mirror/")
        path = tmp_path / "jdk.zip"
        sha = runtime_download.download_file(
            base + "/official/jdk.zip", path, mirror_path="jdk/21/jdk.zip"
        )
        assert path.read_bytes() == b"pinned official bytes"
        assert sha == hashlib.sha256(path.read_bytes()).hexdigest()
        expected = ["/mirror/jdk/21/jdk.zip"]
        if mirror_status != 200:
            expected.append("/official/jdk.zip")
        assert requests == expected


@pytest.mark.parametrize("mirror_path", [None, "jdk/21/jdk.zip"])
@pytest.mark.parametrize("setting", [None, "", "official"])
def test_explicit_official_only(tmp_path, monkeypatch, mirror_path, setting):
    with download_server() as (base, requests):
        if setting is None:
            monkeypatch.delenv("JOLINK_DOWNLOAD_MIRROR", raising=False)
        else:
            monkeypatch.setenv("JOLINK_DOWNLOAD_MIRROR", setting)
        runtime_download.download_file(
            base + "/official", tmp_path / "jdk.zip", mirror_path=mirror_path
        )
        assert requests == ["/official"]


def test_partial_mirror_is_replaced_not_appended(tmp_path, monkeypatch):
    class Interrupted(io.BytesIO):
        def read(self, size):
            if self.tell():
                raise http.client.IncompleteRead(b"partial")
            return super().read(size)

    monkeypatch.setenv("JOLINK_DOWNLOAD_MIRROR", "https://mirror.invalid")
    requests = []

    def open_url(request, **kwargs):
        requests.append(request.full_url)
        return Interrupted(b"partial") if len(requests) == 1 else io.BytesIO(b"whole")

    monkeypatch.setattr(runtime_download.urllib.request, "urlopen", open_url)
    path = tmp_path / "jdk.zip"
    sha = runtime_download.download_file(
        "https://official.invalid/file", path, mirror_path="jdk/file"
    )
    assert path.read_bytes() == b"whole"
    assert sha == hashlib.sha256(b"whole").hexdigest()
    assert len(requests) == 2


def test_both_sources_fail_without_hidden_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("JOLINK_DOWNLOAD_MIRROR", "https://mirror.invalid")
    requests = []

    def reject(request, **kwargs):
        requests.append(request.full_url)
        raise HTTPError(request.full_url, 404, "missing", {}, None)

    monkeypatch.setattr(runtime_download.urllib.request, "urlopen", reject)
    with pytest.raises(HTTPError):
        runtime_download.download_file(
            "https://official.invalid/file",
            tmp_path / "jdk.zip",
            mirror_path="jdk/file",
        )
    assert len(requests) == 2


def test_eclipse_mirror_keeps_full_repository_path(tmp_path, monkeypatch):
    monkeypatch.setenv("JOLINK_DOWNLOAD_MIRROR", runtime_download.JOLINK_MIRROR)
    requests = []

    def open_url(request, **kwargs):
        requests.append(request.full_url)
        return io.BytesIO(b"bundle")

    monkeypatch.setattr(runtime_download.urllib.request, "urlopen", open_url)
    suffix = "eclipse/updates/4.40/R-4.40-202606010713/plugins/core.jar"
    sha = JdtCandidate._download_product_artifact(
        "https://download.eclipse.org/" + suffix,
        tmp_path / "core.jar",
        artifact="core.jar",
    )
    assert sha == hashlib.sha256(b"bundle").hexdigest()
    assert requests == [runtime_download.JOLINK_MIRROR + "/" + suffix]


@pytest.mark.parametrize(
    ("tuna_status", "jolink_status"), [(200, 200), (403, 200), (404, 503)]
)
@pytest.mark.parametrize("kind", ["jdk", "eclipse"])
def test_real_http_cn_source_order(
    tmp_path, monkeypatch, caplog, tuna_status, jolink_status, kind
):
    with download_server(tuna_status=tuna_status, jolink_status=jolink_status) as (
        base,
        requests,
    ):
        monkeypatch.setenv("JOLINK_DOWNLOAD_MIRROR", "cn")
        monkeypatch.setattr(runtime_download, "TUNA_MIRROR", base + "/tuna")
        monkeypatch.setattr(runtime_download, "JOLINK_MIRROR", base + "/jolink")
        if kind == "jdk":
            name = "OpenJDK21U-jdk_x64_windows_hotspot_21.0.12.1_1.zip"
            relative = "jdk/temurin-21.0.12.1+1/" + name
            tuna = "/tuna/Adoptium/21/jdk/x64/windows/" + name
        else:
            relative = "eclipse/updates/4.40/R-4.40-202606010713/plugins/core.jar"
            tuna = "/tuna/eclipse/" + relative
        destination = tmp_path / "artifact"
        sha = runtime_download.download_file(
            base + "/official", destination, mirror_path=relative
        )
        expected = [tuna]
        if tuna_status != 200:
            expected.append("/jolink/" + relative)
            assert "source=tuna" in caplog.text and "next=jolink" in caplog.text
            if jolink_status != 200:
                expected.append("/official")
                assert "source=jolink" in caplog.text and "next=official" in caplog.text
        assert requests == expected
        assert sha == hashlib.sha256(b"pinned official bytes").hexdigest()


@pytest.mark.parametrize(
    ("platform", "arch", "system"),
    [
        ("Darwin-x86_64", "x64", "mac"),
        ("Darwin-arm64", "aarch64", "mac"),
        ("Linux-x86_64", "x64", "linux"),
        ("Linux-arm64", "aarch64", "linux"),
        ("Windows-x86_64", "x64", "windows"),
        ("Windows-arm64", "aarch64", "windows"),
    ],
)
def test_cn_jdk_paths_keep_pinned_filename(monkeypatch, platform, arch, system):
    lock = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "src/jolink_runtime/launch/worker-runtime.json"
        ).read_text()
    )
    name = lock["distributions"][platform]["filename"]
    official = lock["release_url"] + "/" + name
    relative = f"jdk/temurin-{lock['version']}/{name}"
    monkeypatch.setenv("JOLINK_DOWNLOAD_MIRROR", "cn")
    sources = runtime_download._download_sources(official, relative)
    assert sources == [
        (
            "tuna",
            f"{runtime_download.TUNA_MIRROR}/Adoptium/{lock['version'].split('.')[0]}/jdk/{arch}/{system}/{name}",
        ),
        ("jolink", runtime_download.JOLINK_MIRROR + "/" + relative),
        ("official", official),
    ]


def test_cn_all_sources_fail_once(tmp_path, monkeypatch):
    monkeypatch.setenv("JOLINK_DOWNLOAD_MIRROR", "cn")
    requests = []

    def reject(request, **kwargs):
        requests.append(request.full_url)
        raise HTTPError(request.full_url, 404, "missing", {}, None)

    monkeypatch.setattr(runtime_download.urllib.request, "urlopen", reject)
    with pytest.raises(HTTPError):
        runtime_download.download_file(
            "https://official.invalid/core.jar",
            tmp_path / "core.jar",
            mirror_path="eclipse/updates/core.jar",
        )
    assert len(requests) == 3 and len(set(requests)) == 3


def test_cn_does_not_redirect_unmapped_repository(monkeypatch):
    monkeypatch.setenv("JOLINK_DOWNLOAD_MIRROR", "cn")
    assert runtime_download._download_sources("https://custom.invalid/file", None) == [
        ("official", "https://custom.invalid/file")
    ]


def test_existing_size_limit_still_applies(tmp_path, monkeypatch):
    monkeypatch.setenv("JOLINK_DOWNLOAD_MIRROR", "")
    monkeypatch.setattr(
        runtime_download.urllib.request,
        "urlopen",
        lambda *a, **kw: io.BytesIO(b"too large"),
    )
    with pytest.raises(ValueError, match="download limit"):
        runtime_download.download_file(
            "https://official.invalid/file",
            tmp_path / "bundle.jar",
            mirror_path=None,
            max_bytes=2,
        )
