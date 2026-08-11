from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = REPO_ROOT / "experiments" / "jdt-incremental-worker"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bootstrap = _load_module(
    "jolink_jdt_candidate_bootstrap",
    EXPERIMENT / "bootstrap_candidate.py",
)
worker_build = _load_module(
    "jolink_jdt_worker_build",
    EXPERIMENT / "build_worker.py",
)


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
