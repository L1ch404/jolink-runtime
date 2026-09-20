#!/usr/bin/env python3
"""Prepare the Worker; optionally create runtime and corresponding-source kits."""

import argparse
import hashlib
import importlib.metadata
import json
import tarfile
from pathlib import Path

from jolink_runtime.launch.jdt_compile_session import JdtCandidate
from jolink_runtime.launch.runtime_download import download_file


def legal_materials_root() -> Path:
    checkout = Path(__file__).resolve().parents[1]
    if (checkout / "THIRD_PARTY_NOTICES.md").is_file():
        return checkout
    distribution = importlib.metadata.distribution("jolink-runtime")
    for file in distribution.files or ():
        if str(file).endswith(".dist-info/licenses/THIRD_PARTY_NOTICES.md"):
            return Path(distribution.locate_file(file)).parent
    raise RuntimeError("Installed joLink distribution is missing third-party notices.")


def add_legal_materials(archive: tarfile.TarFile, root: Path) -> None:
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "licenses"):
        archive.add(root / name, arcname="legal/" + name)


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare_sources(index: dict, cache: Path) -> list[Path]:
    """Release-time downloads only; never called by application launch/test."""
    cache.mkdir(parents=True, exist_ok=True)
    files = []
    for source in index["source_archives"]:
        path = cache / source["filename"]
        if not path.is_file() or file_sha256(path) != source["sha256"]:
            digest = download_file(source["url"], path, mirror_path=source["mirror_path"])
            if digest != source["sha256"]:
                path.unlink(missing_ok=True)
                raise RuntimeError(f"Source archive checksum mismatch: {path.name}")
        files.append(path)
    return files


def source_bundle_path(runtime_bundle: Path) -> Path:
    name = runtime_bundle.name
    for suffix in (".tar.gz", ".tgz"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return runtime_bundle.with_name(name + "-sources.tar.gz")


def create_offline_bundles(destination: Path, *, candidate_root: Path,
                           runtime_root: Path, cache: Path, legal_root: Path) -> Path:
    index = json.loads((legal_root / "licenses/runtime-sources.json").read_text())
    sources = prepare_sources(index, cache / "license-sources")
    companion = source_bundle_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Prepare the corresponding sources before publishing a runtime kit.
    with tarfile.open(companion, "w:gz") as archive:
        add_legal_materials(archive, legal_root)
        for source in sources:
            archive.add(source, arcname="sources/" + source.name)
    with tarfile.open(destination, "w:gz") as archive:
        for root in (candidate_root, runtime_root):
            archive.add(root, arcname=root.relative_to(cache).as_posix())
        add_legal_materials(archive, legal_root)
    return companion


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-bundle", type=Path,
                        help="Write the runtime kit and a sibling *-sources.tar.gz kit.")
    args = parser.parse_args()
    candidate = JdtCandidate.load_product()
    runtime = candidate.select_worker_java()
    cache = Path.home() / ".cache/jolink-runtime"
    sources_bundle = None
    if args.offline_bundle:
        args.offline_bundle.parent.mkdir(parents=True, exist_ok=True)
        if not runtime.home.is_relative_to(cache / "runtimes"):
            raise SystemExit(
                "Unset JOLINK_WORKER_JAVA_HOME to package the official private runtime."
            )
        runtime_root = next(
            p for p in runtime.home.parents if p.parent.parent == cache / "runtimes"
        )
        sources_bundle = create_offline_bundles(
            args.offline_bundle, candidate_root=candidate.root,
            runtime_root=runtime_root, cache=cache, legal_root=legal_materials_root(),
        )
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
                "sources_bundle": str(sources_bundle) if sources_bundle else None,
            }
        )
    )


if __name__ == "__main__":
    main()
