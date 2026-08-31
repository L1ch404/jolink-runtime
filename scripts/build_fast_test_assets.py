#!/usr/bin/env python3
"""Build reproducible Java 8 Fast Test Runner and Maven Probe resources."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER_SOURCE = (
    ROOT
    / "java/test-runner/src/net/jolink/runtime/test/TestRunner.java"
)
PROBE_ROOT = ROOT / "experiments/jdt-incremental-worker/maven-probe"
PRODUCT_ROOT = ROOT / "src/jolink_runtime/launch"
FIXED_TIME = (2026, 8, 29, 0, 0, 0)


def canonical_lf_bytes(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def class_majors(jar: bytes) -> set[int]:
    with zipfile.ZipFile(__import__("io").BytesIO(jar)) as archive:
        return {
            int.from_bytes(archive.read(name)[6:8], "big")
            for name in archive.namelist()
            if name.endswith(".class")
        }


def build_runner(java_home: Path) -> bytes:
    javac = java_home / "bin" / ("javac.exe" if os.name == "nt" else "javac")
    with tempfile.TemporaryDirectory(prefix="jolink-test-runner-build-") as raw:
        classes = Path(raw) / "classes"
        classes.mkdir()
        subprocess.run(
            [
                str(javac),
                "-encoding",
                "UTF-8",
                "-source",
                "8",
                "-target",
                "8",
                "-d",
                str(classes),
                str(RUNNER_SOURCE),
            ],
            cwd=ROOT,
            check=True,
        )
        destination = Path(raw) / "jolink-test-runner.jar"
        with zipfile.ZipFile(
            destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            manifest = (
                "Manifest-Version: 1.0\r\n"
                "Main-Class: net.jolink.runtime.test.TestRunner\r\n"
                "Build-Jdk-Spec: 1.8\r\n\r\n"
            ).encode("ascii")
            info = zipfile.ZipInfo("META-INF/MANIFEST.MF", FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, manifest)
            for source in sorted(classes.rglob("*.class")):
                info = zipfile.ZipInfo(
                    source.relative_to(classes).as_posix(), FIXED_TIME
                )
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, source.read_bytes())
        result = destination.read_bytes()
    if class_majors(result) != {52}:
        raise RuntimeError("Fast Test Runner is not pure Java 8 bytecode")
    return result


def probe_implementation_id() -> str:
    digest = hashlib.sha256()
    for source in sorted(PROBE_ROOT.rglob("*")):
        if not source.is_file() or "target" in source.relative_to(PROBE_ROOT).parts:
            continue
        relative = source.relative_to(PROBE_ROOT).as_posix().encode("utf-8")
        data = source.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def build_probe(java_home: Path, maven: Path) -> tuple[bytes, bytes, str]:
    target = PROBE_ROOT / "target"
    shutil.rmtree(target, ignore_errors=True)
    environment = {
        **os.environ,
        "JAVA_HOME": str(java_home),
        "PATH": str(java_home / "bin") + os.pathsep + os.environ.get("PATH", ""),
    }
    base = [
        str(maven),
        "--batch-mode",
        "--fail-fast",
        "-Dstyle.color=never",
        "-f",
        str(PROBE_ROOT / "pom.xml"),
    ]
    subprocess.run(
        [
            *base,
            "org.apache.maven.plugins:maven-compiler-plugin:3.11.0:compile",
            "org.apache.maven.plugins:maven-plugin-plugin:3.11.0:descriptor",
        ],
        cwd=PROBE_ROOT,
        env=environment,
        check=True,
    )
    implementation_id = probe_implementation_id()
    identity = target / "classes/META-INF/jolink/probe-implementation-id.txt"
    identity.parent.mkdir(parents=True, exist_ok=True)
    identity.write_text(implementation_id + "\n", encoding="ascii")
    subprocess.run(
        [*base, "org.apache.maven.plugins:maven-jar-plugin:3.3.0:jar"],
        cwd=PROBE_ROOT,
        env=environment,
        check=True,
    )
    jar = target / "jolink-maven-probe-0.1.0-fasttest10.jar"
    jar_bytes = jar.read_bytes()
    if class_majors(jar_bytes) != {52}:
        raise RuntimeError("Maven Probe is not pure Java 8 bytecode")
    return (
        jar_bytes,
        canonical_lf_bytes((PROBE_ROOT / "pom.xml").read_bytes()),
        implementation_id,
    )


def write_base64(path: Path, data: bytes) -> None:
    encoded = base64.b64encode(data).decode("ascii")
    path.write_text(
        "\n".join(encoded[index : index + 76] for index in range(0, len(encoded), 76))
        + "\n",
        encoding="ascii",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java-home", type=Path, required=True)
    parser.add_argument("--maven", type=Path, required=True)
    args = parser.parse_args()
    java_home = args.java_home.expanduser().resolve(strict=True)
    runner = build_runner(java_home)
    probe, probe_pom, implementation_id = build_probe(
        java_home, args.maven.expanduser().resolve(strict=True)
    )
    write_base64(PRODUCT_ROOT / "fast-test-runner.jar.b64", runner)
    write_base64(PRODUCT_ROOT / "maven-build-world-probe.jar.b64", probe)
    (PRODUCT_ROOT / "maven-build-world-probe.pom").write_bytes(probe_pom)
    lock = {
        "schema_version": 1,
        "java_minimum": 8,
        "class_major": 52,
        "test_runner": {
            "sha256": sha256(runner),
            "main_class": "net.jolink.runtime.test.TestRunner",
        },
        "maven_probe": {
            "schema": "jolink.maven-build-world-probe.v2",
            "group_id": "io.jolink",
            "artifact_id": "jolink-maven-probe",
            "version": "0.1.0-fasttest10",
            "sha256": sha256(probe),
            "pom_sha256": sha256(probe_pom),
            "implementation_id": implementation_id,
        },
    }
    (PRODUCT_ROOT / "fast-test-assets.json").write_text(
        json.dumps(lock, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, **lock}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
