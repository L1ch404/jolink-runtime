#!/usr/bin/env python3
"""Build the modern product Worker; keep Probe/Runner artifacts on Java 8."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def _run(command: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java-home", type=Path, required=True)
    parser.add_argument("--worker-java-home", type=Path)
    parser.add_argument("--maven", type=Path)
    parser.add_argument("--gradle", type=Path)
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path.home() / ".cache/jolink-runtime/jdt-poc",
    )
    args = parser.parse_args()
    from jolink_runtime.launch.worker_runtime import managed_worker_java_home

    worker_java_home = args.worker_java_home or managed_worker_java_home()
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required to build the product wheel")
    maven = args.maven or (
        Path(shutil.which("mvn.cmd" if sys.platform == "win32" else "mvn"))
        if shutil.which("mvn.cmd" if sys.platform == "win32" else "mvn")
        else None
    )
    if maven is None:
        raise SystemExit("Maven is required to build the bundled Fast Test Probe")
    gradle = args.gradle or (
        Path(shutil.which("gradle")) if shutil.which("gradle") else None
    )
    if gradle is None:
        raise SystemExit("Gradle is required to build the Gradle Fast Test Probe")
    build_script = (
        repository / "experiments/jdt-incremental-worker/build_worker.py"
    )
    lock = (
        repository
        / "experiments/jdt-incremental-worker/locks/"
        "eclipse-4.40-product.json"
    )
    product_lock = (
        repository / "src/jolink_runtime/launch/jdt-product-candidate.json"
    )
    product_base64 = (
        repository / "src/jolink_runtime/launch/jdt-product-worker.jar.b64"
    )
    _run(
        [
            sys.executable,
            str(build_script),
            "--lock",
            str(lock),
            "--cache-root",
            str(args.cache_root),
            "--java-home",
            str(worker_java_home),
            "--product-lock",
            str(product_lock),
            "--product-worker-base64",
            str(product_base64),
        ],
        cwd=repository,
    )
    _run(
        [
            sys.executable,
            str(repository / "scripts/build_gradle_probe_assets.py"),
            "--java8-home",
            str(args.java_home),
            "--gradle",
            str(gradle),
        ],
        cwd=repository,
    )
    _run(
        [
            sys.executable,
            str(repository / "scripts/build_fast_test_assets.py"),
            "--java-home",
            str(args.java_home),
            "--maven",
            str(maven),
        ],
        cwd=repository,
    )
    _run([uv, "build", "--no-sources"], cwd=repository)
    product = json.loads(product_lock.read_text(encoding="utf-8"))
    with (repository / "pyproject.toml").open("rb") as stream:
        version = str(tomllib.load(stream)["project"]["version"])
    wheel = repository / f"dist/jolink_runtime-{version}-py3-none-any.whl"
    if not wheel.is_file():
        raise SystemExit(f"Expected wheel is unavailable: {wheel}")
    validation = (
        "from pathlib import Path; "
        "from jolink_runtime.launch.jdt_compile_session import JdtCandidate; "
        "from jolink_runtime.launch.fast_test import FastTestAssets; "
        "from jolink_runtime.launch.maven_probe import ProductMavenProbe; "
        "from jolink_runtime.launch.gradle_probe import ProductGradleProbe; "
        "c=JdtCandidate.load_product(); "
        "w=c.select_worker_java(); "
        "assert c.worker_class_major==61 and c.worker_java_minimum==17; "
        "assert w.major>=17 and w.data_model==64; "
        "assert FastTestAssets.load().java_minimum==8; "
        "assert ProductMavenProbe.load().schema.endswith('.v2'); "
        "assert ProductGradleProbe.load().supported_versions==('8.10','8.14')"
    )
    _run(
        [
            uv,
            "run",
            "--isolated",
            "--with",
            str(wheel),
            "python",
            "-c",
            validation,
        ],
        cwd=repository,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "worker_sha256": product["worker_artifact"]["sha256"],
                "worker_class_major": product["worker_class_major"],
                "worker_java_minimum": product["worker_java_minimum"],
                "wheel": str(wheel),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
