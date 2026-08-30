#!/usr/bin/env python3
"""Build and validate the single Java 8 product JDT Worker release."""

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
    parser.add_argument("--maven", type=Path)
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path.home() / ".cache/jolink-runtime/jdt-poc",
    )
    args = parser.parse_args()
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
    build_script = (
        repository / "experiments/jdt-incremental-worker/build_worker.py"
    )
    lock = (
        repository
        / "experiments/jdt-incremental-worker/locks/"
        "eclipse-2021-03-apt-spike.json"
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
            str(args.java_home),
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
        "c=JdtCandidate.load_product(); "
        f"w=c.verify_worker_java(Path({str(args.java_home)!r})); "
        "assert c.worker_class_major==52 and c.worker_java_minimum==8; "
        "assert w.major==8 and w.data_model==64; "
        "assert FastTestAssets.load().java_minimum==8; "
        "assert ProductMavenProbe.load().schema.endswith('.v2')"
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
