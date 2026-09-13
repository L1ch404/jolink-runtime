#!/usr/bin/env python3
"""Prepare the product Worker once; optionally pack its offline dependencies."""

import argparse
import json
import tarfile
from pathlib import Path

from jolink_runtime.launch.jdt_compile_session import JdtCandidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-bundle", type=Path)
    args = parser.parse_args()
    candidate = JdtCandidate.load_product()
    runtime = candidate.select_worker_java()
    cache = Path.home() / ".cache/jolink-runtime"
    if args.offline_bundle:
        args.offline_bundle.parent.mkdir(parents=True, exist_ok=True)
        if not runtime.home.is_relative_to(cache / "runtimes"):
            raise SystemExit(
                "Unset JOLINK_WORKER_JAVA_HOME to package the official private runtime."
            )
        runtime_root = next(
            p for p in runtime.home.parents if p.parent.parent == cache / "runtimes"
        )
        with tarfile.open(args.offline_bundle, "w:gz") as archive:
            for root in (candidate.root, runtime_root):
                archive.add(root, arcname=root.relative_to(cache).as_posix())
    print(
        json.dumps(
            {
                "java_home": str(runtime.home),
                "java_major": runtime.major,
                "candidate": str(candidate.root),
                "cache_root": str(cache),
                "offline_bundle": str(args.offline_bundle)
                if args.offline_bundle
                else None,
            }
        )
    )


if __name__ == "__main__":
    main()
