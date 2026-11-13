"""Exercise the shipped native JDT state codec with real Java objects, offline."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from jolink_runtime.launch import jdt_compile_session


def _java_tools():
    suffix = ".exe" if os.name == "nt" else ""
    homes = [os.environ.get(name) for name in (
        "JOLINK_TEST_JAVA21_HOME", "JOLINK_TEST_JAVA17_HOME", "JOLINK_WORKER_JAVA_HOME", "JAVA_HOME",
    )]
    pairs = [(str(Path(home) / "bin" / ("java" + suffix)),
              str(Path(home) / "bin" / ("javac" + suffix))) for home in homes if home]
    pairs.append((shutil.which("java"), shutil.which("javac")))
    for java, javac in pairs:
        if not java or not javac or not Path(java).is_file() or not Path(javac).is_file():
            continue
        versions = []
        for tool in (java, javac):
            result = subprocess.run([tool, "-version"], text=True, capture_output=True, timeout=10)
            match = re.search(r'(?:version\s+"|javac\s+)(\d+)', result.stdout + result.stderr)
            versions.append(int(match.group(1)) if match and result.returncode == 0 else 0)
        if min(versions) >= 17:
            return java, javac
    pytest.skip("Java and javac 17+ are required for the native JDT State codec regression")


def test_native_state_round_trip_preserves_module_configuration(tmp_path):
    java, javac = _java_tools()
    asset_root = Path(jdt_compile_session.__file__).parent
    lock = json.loads((asset_root / "jdt-product-candidate.json").read_text())
    cache = Path.home() / ".cache/jolink-runtime/jdt-worker/candidates" / lock["candidate_id"]
    # Reuse already downloaded jars; ordinary unit tests must never install JDT.
    roots = [cache / "plugins", *(p / "plugins" for p in cache.iterdir() if p.is_dir())] if cache.is_dir() else []
    plugins = next((root for root in roots
                    if all((root / item["filename"]).is_file() for item in lock["artifacts"])), None)
    if plugins is None:
        pytest.skip("locked Eclipse dependencies must already be cached for this Java regression")
    jars = [plugins / item["filename"] for item in lock["artifacts"]]
    core = next(path for path in jars if path.name.startswith("org.eclipse.jdt.core_"))
    classes = tmp_path / "classes"
    classes.mkdir()
    # The upstream JAR is signed. Put its classes and the exact shipped State
    # bytecode in a private test directory, not in the signed/global candidate.
    with zipfile.ZipFile(core) as archive:
        for entry in archive.infolist():
            if entry.filename.endswith(".class"):
                archive.extract(entry, classes)
    worker = base64.b64decode((asset_root / "jdt-product-worker.jar.b64").read_bytes())
    with zipfile.ZipFile(io.BytesIO(worker)) as archive:
        resource = "META-INF/jolink/patches/org/eclipse/jdt/internal/core/builder/State.class"
        patched = archive.read(resource)
    (classes / "org/eclipse/jdt/internal/core/builder/State.class").write_bytes(patched)
    classpath = os.pathsep.join(map(str, [classes, *(path for path in jars if path != core)]))
    source = Path(__file__).parents[1] / "java/StateRoundTripHarness.java"
    compile_result = subprocess.run(
        [javac, "-encoding", "UTF-8", "-cp", classpath, "-d", str(classes), str(source)],
        text=True, capture_output=True, timeout=60,
    )
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    result = subprocess.run(
        [java, "-cp", classpath, "org.eclipse.jdt.internal.core.builder.StateRoundTripHarness"],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STATE_ROUNDTRIP_OK" in result.stdout
