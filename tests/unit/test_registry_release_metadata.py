"""Exercise the same metadata preparation command used by release CI."""

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def release_tree(tmp_path):
    root = tmp_path / "release"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/prepare_registry_metadata.py", root / "scripts")
    shutil.copy2(ROOT / "server.json", root)
    server = json.loads((root / "server.json").read_text(encoding="utf-8"))
    (root / "README.md").write_text(
        f"# joLink 发布\n<!-- mcp-name: {server['name']} -->\n", encoding="utf-8"
    )
    return root


def write_project(root, version):
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "jolink-runtime"\nversion = "{version}"\nreadme = "README.md"\n',
        encoding="utf-8",
    )


def prepare(root):
    return subprocess.run(
        [sys.executable, str(root / "scripts/prepare_registry_metadata.py")],
        cwd=root.parent, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


def test_versions_follow_project_across_releases_without_manual_registry_edit(release_tree):
    root = release_tree
    path = root / "server.json"
    original = json.loads(path.read_text(encoding="utf-8"))
    expected = copy.deepcopy(original)
    for version in ("0.2.0a1", "0.2.0"):
        write_project(root, version)
        project_before = (root / "pyproject.toml").read_bytes()
        result = prepare(root)
        assert result.returncode == 0, result.stderr
        expected["version"] = expected["packages"][0]["version"] = version
        # Only the two version fields change; identity, transport and env survive.
        assert json.loads(path.read_text(encoding="utf-8")) == expected
        assert (root / "pyproject.toml").read_bytes() == project_before
        generated = path.read_bytes()
        assert prepare(root).returncode == 0
        assert path.read_bytes() == generated


@pytest.mark.parametrize("problem", ["package_name", "ownership_marker"])
def test_existing_identity_checks_precede_writing_metadata(release_tree, problem):
    root = release_tree
    write_project(root, "0.2.0")
    if problem == "package_name":
        project = root / "pyproject.toml"
        project.write_text(project.read_text().replace("jolink-runtime", "other-package"))
    else:
        (root / "README.md").write_text("# joLink\n", encoding="utf-8")
    before = (root / "server.json").read_bytes()
    result = prepare(root)
    assert result.returncode != 0
    assert "Registry package name" in result.stderr or "ownership marker" in result.stderr
    assert (root / "server.json").read_bytes() == before
