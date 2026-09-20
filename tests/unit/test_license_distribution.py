"""Release-only license/source checks; no checks enter the runtime build loop."""
import hashlib
import importlib.util
import json
import tarfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("jolink_prepare_worker", ROOT / "scripts/prepare_jdt_worker.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def test_original_notice_bytes_are_preserved():
    materials = json.loads((ROOT / "licenses/materials.json").read_text())
    assert materials["files"]
    for item in materials["files"]:
        path = ROOT / item["path"]
        assert path.is_file(), item
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"], item


def test_runtime_source_index_covers_exact_product_assets():
    product = ROOT / "src/jolink_runtime/launch"
    runtime = json.loads((product / "jdt-product-candidate.json").read_text())
    jdk = json.loads((product / "worker-runtime.json").read_text())
    index = json.loads((ROOT / "licenses/runtime-sources.json").read_text())
    assert {a["filename"]: a["sha256"] for a in index["eclipse_components"]} == {
        a["filename"]: a["sha256"] for a in runtime["artifacts"]}
    sources = {a["filename"]: a for a in index["source_archives"]}
    for component in index["eclipse_components"]:
        assert component["source_archive"] in sources
        assert sources[component["source_archive"]]["component"] == component["symbolic_name"]
        assert component["version"] in component["source_archive"]
        if component["license_identity"] == "EPL-2.0":
            assert "commitId=" in component["source_reference"]
        for notice in component["notices"]:
            assert (ROOT / notice).is_file()
    assert index["jdk"]["version"] == jdk["version"]
    assert {p["platform"]: p["binary_sha256"] for p in index["jdk"]["platforms"]} == {
        platform: item["sha256"] for platform, item in jdk["distributions"].items()}
    assert index["jdk"]["source_archive"] in sources
    assert index["jdk"]["build_source_archive"] in sources


def test_nested_licenses_and_original_attribution_are_present():
    mit = (ROOT / "LICENSE").read_text()
    assert "Nous Research" in mit and "joLink contributors" in mit
    assert "libffi" in (ROOT / "licenses/jna/libffi-LICENSE").read_text()
    assert "Sun Microsystems" in (ROOT / "licenses/eclipse/org.eclipse.jdt.apt.core/mirror-api-license.txt").read_text()
    packages = json.loads((ROOT / "licenses/python-dependencies.json").read_text())["packages"]
    pywin32 = next(p for p in packages if p["name"] == "pywin32")
    adodb = next(p for p in pywin32["notices"] if p.endswith("adodbapi/license.txt"))
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in (ROOT / adodb).read_text()


@pytest.mark.parametrize("path_type", [PurePosixPath, PureWindowsPath])
def test_installed_materials_can_be_found_without_a_checkout(tmp_path, monkeypatch, path_type):
    script = tmp_path / "tools/prepare_jdt_worker.py"
    notice = tmp_path / "site/pkg.dist-info/licenses/THIRD_PARTY_NOTICES.md"
    notice.parent.mkdir(parents=True)
    notice.write_text("notices")
    relative = path_type("pkg.dist-info/licenses/THIRD_PARTY_NOTICES.md")
    monkeypatch.setattr(prepare, "__file__", str(script))
    monkeypatch.setattr(prepare.importlib.metadata, "distribution", lambda _: SimpleNamespace(
        files=[relative], locate_file=lambda p: tmp_path / "site" / p.as_posix()))
    assert prepare.legal_materials_root() == notice.parent


def test_offline_runtime_and_source_kits_keep_legal_files(tmp_path):
    cache = tmp_path / "cache"
    candidate = cache / "jdt-worker/candidates/fixture"
    runtime = cache / "runtimes/fixture/platform"
    for path in [candidate / "worker.jar", runtime / "legal/upstream/LICENSE"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original")
    legal = tmp_path / "legal-input"
    (legal / "licenses").mkdir(parents=True)
    (legal / "LICENSE").write_text("joLink MIT")
    (legal / "THIRD_PARTY_NOTICES.md").write_text("notices")
    source = cache / "license-sources/source.tar.gz"
    source.parent.mkdir()
    source.write_bytes(b"original source archive")
    index = {"source_archives": [{"filename": source.name,
             "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
             "url": "https://unused.invalid/source.tar.gz", "mirror_path": "sources/source.tar.gz"}]}
    (legal / "licenses/runtime-sources.json").write_text(json.dumps(index))
    output = tmp_path / "runtime.tar.gz"
    companion = prepare.create_offline_bundles(output, candidate_root=candidate,
        runtime_root=runtime, cache=cache, legal_root=legal)
    assert companion.name == "runtime-sources.tar.gz"
    for bundle in [output, companion]:
        with tarfile.open(bundle) as archive:
            assert archive.extractfile("legal/LICENSE").read() == b"joLink MIT"
            assert archive.extractfile("legal/THIRD_PARTY_NOTICES.md").read() == b"notices"
            assert "legal/licenses/runtime-sources.json" in archive.getnames()
    with tarfile.open(output) as archive:
        assert archive.extractfile("runtimes/fixture/platform/legal/upstream/LICENSE").read() == b"original"
        assert "sources/source.tar.gz" not in archive.getnames()
    with tarfile.open(companion) as archive:
        assert archive.extractfile("sources/source.tar.gz").read() == source.read_bytes()


@pytest.mark.parametrize("cached", [None, b"damaged cache"])
def test_source_preparation_downloads_missing_or_corrupt_cache(tmp_path, monkeypatch, cached):
    data = b"verified original source"
    path = tmp_path / "source.tar.gz"
    if cached is not None: path.write_bytes(cached)
    item = {"filename": path.name, "url": "https://example.invalid/source.tar.gz",
            "mirror_path": "sources/source.tar.gz", "sha256": hashlib.sha256(data).hexdigest()}
    calls = []
    def download(url, destination, **kwargs):
        calls.append((url, kwargs))
        destination.write_bytes(data)
        return item["sha256"]
    monkeypatch.setattr(prepare, "download_file", download)
    assert prepare.prepare_sources({"source_archives": [item]}, tmp_path) == [path]
    assert prepare.prepare_sources({"source_archives": [item]}, tmp_path) == [path]
    assert len(calls) == 1


def test_source_checksum_error_does_not_publish_offline_kits(tmp_path, monkeypatch):
    legal = tmp_path / "input"
    (legal / "licenses").mkdir(parents=True)
    (legal / "licenses/runtime-sources.json").write_text(json.dumps({"source_archives": [{
        "filename": "bad.tar.gz", "url": "https://example.invalid/bad", "mirror_path": None, "sha256": "expected"}]}))
    def bad_download(url, path, **kwargs):
        path.write_bytes(b"wrong")
        return "wrong"
    monkeypatch.setattr(prepare, "download_file", bad_download)
    output = tmp_path / "out/runtime.tar.gz"
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        prepare.create_offline_bundles(output, candidate_root=tmp_path, runtime_root=tmp_path,
                                      cache=tmp_path / "cache", legal_root=legal)
    assert not output.exists()
    assert not prepare.source_bundle_path(output).exists()
    assert not (tmp_path / "cache/license-sources/bad.tar.gz").exists()


def test_normal_preparation_does_not_request_source_archives(tmp_path, monkeypatch, capsys):
    runtime = SimpleNamespace(home=tmp_path / "jdk", major=21)
    candidate = SimpleNamespace(root=tmp_path / "candidate", select_worker_java=lambda: runtime)
    monkeypatch.setattr(prepare.JdtCandidate, "load_product", lambda: candidate)
    monkeypatch.setattr("sys.argv", ["prepare_jdt_worker.py"])
    def unexpected(*args):
        raise AssertionError("normal preparation must not download source archives")
    monkeypatch.setattr(prepare, "prepare_sources", unexpected)
    prepare.main()
    result = json.loads(capsys.readouterr().out)
    assert result["offline_bundle"] is None and result["sources_bundle"] is None
