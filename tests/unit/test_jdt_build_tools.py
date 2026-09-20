"""Regression tests for the product Worker build and dependency lock tooling."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bootstrap = _load_module("jolink_jdt_dependency_resolver", REPO_ROOT / "scripts/resolve_jdt_dependencies.py")
worker_build = _load_module("jolink_jdt_worker_build", REPO_ROOT / "scripts/build_jdt_worker.py")


def test_product_worker_source_fingerprint_matches_bundled_build():
    lock = json.loads((REPO_ROOT / "src/jolink_runtime/launch/jdt-product-candidate.json").read_text())
    assert worker_build._source_fingerprint(REPO_ROOT / "java/jdt-worker") == lock["worker_build_provenance"]["source_fingerprint"]


def test_product_probe_and_runner_build_inputs_exist_outside_retired_tree():
    fast = _load_module("jolink_fast_assets_build", REPO_ROOT / "scripts/build_fast_test_assets.py")
    gradle = _load_module("jolink_gradle_assets_build", REPO_ROOT / "scripts/build_gradle_probe_assets.py")
    assert fast.RUNNER_SOURCE.is_file()
    assert (fast.PROBE_ROOT / "pom.xml").is_file()
    assert (gradle.PROJECT / "build.gradle").is_file()
    assert (gradle.PROJECT / "init.gradle.template").read_bytes() == (REPO_ROOT / "src/jolink_runtime/launch/gradle-init.gradle").read_bytes()
    assets = json.loads((REPO_ROOT / "src/jolink_runtime/launch/fast-test-assets.json").read_text())
    assert fast.probe_implementation_id() == assets["maven_probe"]["implementation_id"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("17", "17.0.0"),
        ("3.46.0", "3.46.0"),
        ("3.46.0.v20260520-1003", "3.46.0.v20260520-1003"),
    ],
)
def test_osgi_version_round_trip(raw: str, expected: str) -> None:
    assert bootstrap.OSGiVersion.parse(raw).text() == expected


def test_osgi_version_range_boundaries() -> None:
    version_range = bootstrap.VersionRange.parse("[1.2.0,2.0.0)")
    assert version_range.contains(bootstrap.OSGiVersion.parse("1.2.0"))
    assert version_range.contains(bootstrap.OSGiVersion.parse("1.9.9"))
    assert not version_range.contains(bootstrap.OSGiVersion.parse("2.0.0"))


def test_resolver_uses_bundle_and_worker_system_capabilities(
    tmp_path: Path,
) -> None:
    content = tmp_path / "content.xml"
    content.write_text(
        """<?xml version='1.0'?>
<repository>
  <units>
    <unit id='a.jre.javase' version='17.0.0'>
      <provides>
        <provided namespace='java.package' name='javax.xml.parsers' version='0.0.0'/>
      </provides>
    </unit>
    <unit id='root' version='1.0.0'>
      <provides>
        <provided namespace='osgi.bundle' name='root' version='1.0.0'/>
      </provides>
      <requires>
        <required namespace='osgi.bundle' name='dependency' range='[1.0.0,2.0.0)'/>
        <required namespace='java.package' name='javax.xml.parsers' range='0.0.0'/>
      </requires>
      <artifacts><artifact classifier='osgi.bundle' id='root' version='1.0.0'/></artifacts>
    </unit>
    <unit id='dependency' version='1.5.0'>
      <provides>
        <provided namespace='osgi.bundle' name='dependency' version='1.5.0'/>
      </provides>
      <artifacts><artifact classifier='osgi.bundle' id='dependency' version='1.5.0'/></artifacts>
    </unit>
  </units>
</repository>
""",
        encoding="utf-8",
    )
    units = bootstrap.parse_units(content)
    system = bootstrap.parse_system_capabilities(content, java_major=17)
    resolved = bootstrap.resolve_units(
        units, ["root"], system_capabilities=system
    )
    assert [unit.unit_id for unit in resolved] == ["dependency", "root"]


def test_resolver_validates_and_records_osgi_execution_environment(
    tmp_path: Path,
) -> None:
    content = tmp_path / "content.xml"
    content.write_text(
        """<?xml version='1.0'?>
<repository>
  <units>
    <unit id='a.jre.javase' version='17.0.0'>
      <provides>
        <provided namespace='osgi.ee' name='JavaSE' version='17.0.0'/>
        <provided namespace='osgi.ee' name='JavaSE/compact1' version='1.8.0'/>
      </provides>
    </unit>
    <unit id='root' version='1.0.0'>
      <provides>
        <provided namespace='osgi.bundle' name='root' version='1.0.0'/>
      </provides>
      <requires>
        <requiredProperties namespace='osgi.ee'
          match='(|(&amp;(osgi.ee=JavaSE)(version=17))(&amp;(osgi.ee=JavaSE/compact1)(version=1.8)))'/>
      </requires>
      <artifacts><artifact classifier='osgi.bundle' id='root' version='1.0.0'/></artifacts>
    </unit>
  </units>
</repository>
""",
        encoding="utf-8",
    )
    units = bootstrap.parse_units(content)
    system = bootstrap.parse_system_capabilities(content, java_major=17)
    resolved = bootstrap.resolve_units(
        units, ["root"], system_capabilities=system
    )
    evidence = bootstrap.execution_environment_evidence(
        resolved, system_capabilities=system, worker_java_major=17
    )

    assert evidence["status"] == "satisfied"
    assert evidence["worker_java_major"] == 17
    assert evidence["p2_capability_unit_java_major"] == 17
    assert evidence["worker_java_satisfies_p2_profile"] is True
    assert evidence["requirements"] == [
        {
            "bundle": "root",
            "bundle_version": "1.0.0",
            "filter": "(|(&(osgi.ee=JavaSE)(version=17))(&(osgi.ee=JavaSE/compact1)(version=1.8)))",
            "status": "satisfied",
            "matched_capability": {"name": "JavaSE", "version": "17.0.0"},
        }
    ]


def test_execution_environment_can_use_older_p2_profile_for_newer_worker(
    tmp_path: Path,
) -> None:
    content = tmp_path / "content.xml"
    content.write_text(
        """<?xml version='1.0'?>
<repository>
  <units>
    <unit id='a.jre.javase' version='11.0.0'>
      <provides>
        <provided namespace='osgi.ee' name='JavaSE' version='11.0.0'/>
      </provides>
    </unit>
    <unit id='root' version='1.0.0'>
      <provides>
        <provided namespace='osgi.bundle' name='root' version='1.0.0'/>
      </provides>
      <requires>
        <requiredProperties namespace='osgi.ee'
          match='(&amp;(osgi.ee=JavaSE)(version=11))'/>
      </requires>
      <artifacts><artifact classifier='osgi.bundle' id='root' version='1.0.0'/></artifacts>
    </unit>
  </units>
</repository>
""",
        encoding="utf-8",
    )
    units = bootstrap.parse_units(content)
    system = bootstrap.parse_system_capabilities(content, java_major=11)
    resolved = bootstrap.resolve_units(
        units, ["root"], system_capabilities=system
    )

    evidence = bootstrap.execution_environment_evidence(
        resolved,
        system_capabilities=system,
        worker_java_major=17,
        p2_capability_unit_java_major=11,
    )

    assert evidence["worker_java_major"] == 17
    assert evidence["p2_capability_unit_java_major"] == 11
    assert evidence["worker_java_satisfies_p2_profile"] is True


def test_execution_environment_rejects_worker_older_than_p2_profile() -> None:
    with pytest.raises(
        bootstrap.DiscoveryError,
        match="older than the p2 execution-environment profile",
    ):
        bootstrap.execution_environment_evidence(
            [],
            system_capabilities=(),
            worker_java_major=8,
            p2_capability_unit_java_major=11,
        )


def test_resolver_rejects_unsatisfied_osgi_execution_environment(
    tmp_path: Path,
) -> None:
    content = tmp_path / "content.xml"
    content.write_text(
        """<?xml version='1.0'?>
<repository>
  <units>
    <unit id='a.jre.javase' version='17.0.0'>
      <provides><provided namespace='osgi.ee' name='JavaSE' version='17.0.0'/></provides>
    </unit>
    <unit id='root' version='1.0.0'>
      <provides><provided namespace='osgi.bundle' name='root' version='1.0.0'/></provides>
      <requires><requiredProperties namespace='osgi.ee' match='(&amp;(osgi.ee=JavaSE)(version=21))'/></requires>
      <artifacts><artifact classifier='osgi.bundle' id='root' version='1.0.0'/></artifacts>
    </unit>
  </units>
</repository>
""",
        encoding="utf-8",
    )
    units = bootstrap.parse_units(content)
    system = bootstrap.parse_system_capabilities(content, java_major=17)

    with pytest.raises(
        bootstrap.DiscoveryError,
        match="does not satisfy root",
    ):
        bootstrap.resolve_units(
            units, ["root"], system_capabilities=system
        )


def test_selected_unknown_mandatory_capability_fails_closed(
    tmp_path: Path,
) -> None:
    content = tmp_path / "content.xml"
    content.write_text(
        """<?xml version='1.0'?>
<repository>
  <units>
    <unit id='root' version='1.0.0'>
      <provides>
        <provided namespace='osgi.bundle' name='root' version='1.0.0'/>
      </provides>
      <requires>
        <requiredProperties namespace='unknown.capability' match='(x=y)'/>
      </requires>
      <artifacts><artifact classifier='osgi.bundle' id='root' version='1.0.0'/></artifacts>
    </unit>
  </units>
</repository>
""",
        encoding="utf-8",
    )
    units = bootstrap.parse_units(content)
    with pytest.raises(
        bootstrap.DiscoveryError,
        match="unsupported mandatory requirements",
    ):
        bootstrap.resolve_units(units, ["root"])


@pytest.mark.parametrize(
    ("license_payload", "expected"),
    [
        (
            "SPDX-License-Identifier: Apache-2.0 OR LGPL-2.1-or-later\n",
            "Apache-2.0 OR LGPL-2.1-or-later",
        ),
        (
            "<p>Eclipse Public License Version 2.0</p>",
            "EPL-2.0",
        ),
    ],
)
def test_license_identity_fallback(
    tmp_path: Path, license_payload: str, expected: str
) -> None:
    jar = tmp_path / "bundle.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        if expected == "EPL-2.0":
            archive.writestr("about.html", license_payload)
        else:
            archive.writestr("META-INF/LICENSE", license_payload)
    assert bootstrap._license_identity(jar, {}) == expected


def test_generated_config_excludes_launcher_and_framework() -> None:
    lock = {
        "artifacts": [
            {
                "symbolic_name": "org.eclipse.osgi",
                "filename": "org.eclipse.osgi.jar",
            },
            {
                "symbolic_name": "org.eclipse.equinox.launcher",
                "filename": "launcher.jar",
            },
            {
                "symbolic_name": "org.eclipse.jdt.core",
                "filename": "jdt.jar",
            },
        ]
    }
    config = worker_build._config_ini(lock, worker_filename="worker.jar")
    assert "osgi.framework=file:plugins/org.eclipse.osgi.jar" in config
    assert "launcher.jar@4:start" not in config
    assert "jdt.jar@4:start" in config
    assert "worker.jar@4:start" in config


def test_worker_jar_is_reproducible(tmp_path: Path) -> None:
    worker = tmp_path / "worker"
    (worker / "META-INF").mkdir(parents=True)
    (worker / "META-INF" / "MANIFEST.MF").write_text(
        "Manifest-Version: 1.0\n\n", encoding="utf-8"
    )
    (worker / "plugin.xml").write_text("<plugin/>\n", encoding="utf-8")
    classes = tmp_path / "classes"
    (classes / "example").mkdir(parents=True)
    (classes / "example" / "Fixture.class").write_bytes(b"class-bytes")

    first = tmp_path / "first.jar"
    second = tmp_path / "second.jar"
    worker_build._create_worker_jar(worker, classes, first)
    worker_build._create_worker_jar(worker, classes, second)

    assert worker_build.sha256_file(first) == worker_build.sha256_file(second)


def test_product_worker_matches_its_declared_runtime() -> None:
    launch = REPO_ROOT / "src/jolink_runtime/launch"
    lock = json.loads(
        (launch / "jdt-product-candidate.json").read_text(encoding="utf-8")
    )
    raw = base64.b64decode(
        "".join(
            (launch / "jdt-product-worker.jar.b64")
            .read_text(encoding="ascii")
            .split()
        ),
        validate=True,
    )
    jar = io.BytesIO(raw)
    majors: set[int] = set()
    with zipfile.ZipFile(jar) as archive:
        manifest = archive.read("META-INF/MANIFEST.MF").decode("utf-8")
        for name in archive.namelist():
            if name.endswith(".class"):
                payload = archive.read(name)
                majors.add(int.from_bytes(payload[6:8], "big"))

    assert lock["worker_java_minimum"] == 17
    assert lock["worker_class_major"] == 44 + lock["worker_java_minimum"]
    assert majors == {lock["worker_class_major"]}
    assert "Bundle-RequiredExecutionEnvironment: JavaSE-17" in manifest
    assert hashlib.sha256(raw).hexdigest() == lock["worker_artifact"]["sha256"]

    assert lock["worker_runtime"] == "temurin-21"


def test_p2_metadata_cache_is_partitioned_by_repository_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads: list[tuple[str, Path]] = []

    def fake_download(url: str, destination: Path) -> None:
        downloads.append((url, destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("content.xml", "<repository />")

    monkeypatch.setattr(bootstrap, "_download", fake_download)

    first_xml, first_identity = bootstrap._content_xml(
        "https://example.invalid/eclipse/current", tmp_path
    )
    second_xml, second_identity = bootstrap._content_xml(
        "https://example.invalid/eclipse/anchor", tmp_path
    )

    assert first_xml != second_xml
    assert first_identity["url"] != second_identity["url"]
    assert len(downloads) == 2
    assert downloads[0][1].parent != downloads[1][1].parent


def test_bundle_download_retries_a_truncated_cached_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = bootstrap.Unit(
        unit_id="example.bundle",
        version=bootstrap.OSGiVersion.parse("1.0.0"),
        capabilities=(),
        requirements=(),
        execution_environment_requirements=(),
        unsupported_mandatory_requirements=(),
        artifact_classifier="osgi.bundle",
        artifact_id="example.bundle",
        artifact_version="1.0.0",
    )
    destination = (
        tmp_path
        / "candidates/candidate/plugins/example.bundle_1.0.0.jar"
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"truncated")
    download_count = 0

    def fake_download(url: str, path: Path) -> None:
        nonlocal download_count
        download_count += 1
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "META-INF/MANIFEST.MF",
                "Manifest-Version: 1.0\n"
                "Bundle-SymbolicName: example.bundle\n"
                "Bundle-Version: 1.0.0\n\n",
            )
            archive.writestr(
                "about.html",
                "Eclipse Public License Version 2.0",
            )

    monkeypatch.setattr(bootstrap, "_download", fake_download)

    lock = bootstrap.download_and_lock(
        repository_url="https://example.invalid/repository",
        units=[unit],
        cache_root=tmp_path,
        candidate_id="candidate",
        bootstrap_config={
            "worker_java_minimum": 17,
            "root_installable_units": ["example.bundle"],
        },
        metadata={"sha256": "metadata"},
        execution_environment={"status": "satisfied"},
        lock_path=tmp_path / "lock.json",
    )

    assert download_count == 1
    assert lock["artifacts"][0]["symbolic_name"] == "example.bundle"


def test_download_retries_transient_transport_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    class Response(io.BytesIO):
        def __init__(self) -> None:
            super().__init__(b"payload")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()
            return False

    def fake_urlopen(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("transient transport failure")
        return Response()

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _: None)
    destination = tmp_path / "artifact.jar"

    bootstrap._download("https://example.invalid/artifact.jar", destination)

    assert attempts == 3
    assert destination.read_bytes() == b"payload"
