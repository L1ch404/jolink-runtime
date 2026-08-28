#!/usr/bin/env python3
"""Build the isolated OSGi worker from an already locked bootstrap candidate."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


class WorkerBuildError(RuntimeError):
    pass


_PRODUCT_WORKER_CLASS_MAJOR = 52


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load_lock(path: Path) -> dict[str, object]:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerBuildError("Unable to read candidate lock.") from exc
    artifacts = lock.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise WorkerBuildError("Candidate lock has no bundle artifacts.")
    return lock


def _java_tool(java_home: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    tool = java_home / "bin" / f"{name}{suffix}"
    if not tool.is_file():
        raise WorkerBuildError(f"Worker JDK tool is unavailable: {name}")
    return tool


def _java8_identity(java: Path, javac: Path) -> dict[str, str]:
    completed = subprocess.run(
        [str(javac), "-version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != 0 or re.search(r"\b1\.8(?:\.|\s|$)", output) is None:
        raise WorkerBuildError(
            "The product Worker must be compiled by a real JDK 8 javac."
        )
    runtime = subprocess.run(
        [str(java), "-version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )
    if runtime.returncode != 0:
        raise WorkerBuildError("Unable to record the Worker build JDK.")
    return {
        "java_version_output": (runtime.stderr or runtime.stdout).splitlines()[0],
        "javac_version_output": output.splitlines()[0],
        "java_binary_sha256": sha256_file(java),
        "javac_binary_sha256": sha256_file(javac),
    }


def _source_files(worker_root: Path) -> list[Path]:
    files = sorted((worker_root / "src").rglob("*.java"))
    if not files:
        raise WorkerBuildError("Worker Java source is unavailable.")
    return files


def _verify_bundles(
    lock: dict[str, object], *, plugins: Path
) -> tuple[list[Path], str]:
    jars: list[Path] = []
    launcher = ""
    for artifact in lock["artifacts"]:
        if not isinstance(artifact, dict):
            raise WorkerBuildError("Invalid artifact entry in candidate lock.")
        filename = str(artifact["filename"])
        path = plugins / filename
        if not path.is_file() or sha256_file(path) != artifact["sha256"]:
            raise WorkerBuildError(f"Locked bundle is missing or changed: {filename}")
        jars.append(path)
        if artifact["symbolic_name"] == "org.eclipse.equinox.launcher":
            launcher = filename
    if not launcher:
        raise WorkerBuildError("Candidate lock has no Equinox launcher.")
    return jars, launcher


def _source_fingerprint(worker_root: Path) -> str:
    digest = hashlib.sha256()
    inputs = [
        worker_root / "META-INF" / "MANIFEST.MF",
        worker_root / "plugin.xml",
        *_source_files(worker_root),
    ]
    for path in sorted(inputs):
        digest.update(path.relative_to(worker_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _create_worker_jar(worker_root: Path, classes: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        def write_entry(name: str, payload: bytes) -> None:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payload, compresslevel=9)

        write_entry(
            "META-INF/MANIFEST.MF",
            (worker_root / "META-INF" / "MANIFEST.MF").read_bytes(),
        )
        write_entry("plugin.xml", (worker_root / "plugin.xml").read_bytes())
        for path in sorted(classes.rglob("*")):
            if path.is_file():
                write_entry(path.relative_to(classes).as_posix(), path.read_bytes())


def _verify_java8_classes(classes: Path) -> None:
    class_files = sorted(classes.rglob("*.class"))
    if not class_files:
        raise WorkerBuildError("Worker compilation produced no class files.")
    for path in class_files:
        raw = path.read_bytes()
        if len(raw) < 8 or raw[:4] != b"\xca\xfe\xba\xbe":
            raise WorkerBuildError("Worker output contains an invalid class file.")
        major = int.from_bytes(raw[6:8], "big")
        if major != _PRODUCT_WORKER_CLASS_MAJOR:
            raise WorkerBuildError(
                f"Worker class major must be 52, got {major}: {path.name}"
            )


def _update_product_artifacts(
    *,
    product_lock: Path,
    product_base64: Path,
    worker_jar: Path,
    source_fingerprint: str,
    build_identity: dict[str, str],
) -> None:
    try:
        lock = json.loads(product_lock.read_text(encoding="utf-8"))
        worker = lock["worker_artifact"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise WorkerBuildError("Unable to read the product Worker lock.") from exc
    worker_sha = sha256_file(worker_jar)
    worker["sha256"] = worker_sha
    lock["worker_java_minimum"] = 8
    lock["worker_class_major"] = _PRODUCT_WORKER_CLASS_MAJOR
    lock["worker_build_provenance"] = {
        "source_fingerprint": source_fingerprint,
        **build_identity,
    }
    encoded = base64.b64encode(worker_jar.read_bytes()).decode("ascii")
    wrapped = "\n".join(
        encoded[index:index + 76]
        for index in range(0, len(encoded), 76)
    ) + "\n"
    _atomic_write(
        product_lock,
        (json.dumps(lock, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        ),
    )
    _atomic_write(product_base64, wrapped.encode("ascii"))


def _config_ini(
    lock: dict[str, object], *, worker_filename: str
) -> str:
    bundle_entries: list[str] = []
    framework = ""
    for artifact in lock["artifacts"]:
        symbolic_name = artifact["symbolic_name"]
        filename = artifact["filename"]
        if symbolic_name == "org.eclipse.osgi":
            framework = f"file:plugins/{filename}"
            continue
        if symbolic_name == "org.eclipse.equinox.launcher":
            continue
        bundle_entries.append(
            f"reference:file:plugins/{filename}@4:start"
        )
    bundle_entries.append(
        f"reference:file:plugins/{worker_filename}@4:start"
    )
    if not framework:
        raise WorkerBuildError("Candidate lock has no Equinox framework.")
    return "\n".join(
        [
            f"osgi.framework={framework}",
            "osgi.bundles=" + ",".join(bundle_entries),
            "osgi.bundles.defaultStartLevel=4",
            "osgi.configuration.cascaded=false",
            "osgi.noShutdown=false",
            "eclipse.application=net.jolink.runtime.jdt.worker",
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    experiment_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=(
            experiment_root
            / "locks"
            / "eclipse-2021-03-apt-spike.json"
        ),
    )
    parser.add_argument("--product-lock", type=Path)
    parser.add_argument("--product-worker-base64", type=Path)
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path.home() / ".cache" / "jolink-runtime" / "jdt-poc",
    )
    parser.add_argument(
        "--java-home",
        type=Path,
        default=Path(os.environ.get("JAVA_HOME", "")),
    )
    args = parser.parse_args(argv)

    try:
        if not str(args.java_home):
            raise WorkerBuildError("Set JAVA_HOME or pass --java-home.")
        lock = _load_lock(args.lock)
        candidate_id = str(lock["candidate_id"])
        candidate_root = args.cache_root / "candidates" / candidate_id
        plugins = candidate_root / "plugins"
        jars, launcher = _verify_bundles(lock, plugins=plugins)
        javac = _java_tool(args.java_home, "javac")
        java = _java_tool(args.java_home, "java")
        build_identity = _java8_identity(java, javac)
        worker_root = experiment_root / "worker"
        manifest = (worker_root / "META-INF" / "MANIFEST.MF").read_text(
            encoding="utf-8"
        )
        if "Bundle-RequiredExecutionEnvironment: JavaSE-1.8" not in manifest:
            raise WorkerBuildError("Worker manifest must require JavaSE-1.8.")

        with tempfile.TemporaryDirectory(prefix="jolink-jdt-worker-") as temporary:
            classes = Path(temporary) / "classes"
            classes.mkdir()
            command = [
                str(javac),
                "-encoding",
                "UTF-8",
                "-source",
                "8",
                "-target",
                "8",
                "-classpath",
                os.pathsep.join(str(path) for path in jars),
                "-d",
                str(classes),
                *(str(path) for path in _source_files(worker_root)),
            ]
            completed = subprocess.run(
                command,
                cwd=experiment_root,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
            if completed.returncode != 0:
                sys.stderr.write(completed.stderr[-8000:])
                raise WorkerBuildError("Worker javac failed.")
            _verify_java8_classes(classes)
            worker_filename = "net.jolink.runtime.jdt.worker_0.1.0.jar"
            worker_jar = plugins / worker_filename
            _create_worker_jar(worker_root, classes, worker_jar)

        configuration = candidate_root / "configuration"
        configuration.mkdir(parents=True, exist_ok=True)
        config_ini = _config_ini(lock, worker_filename=worker_filename)
        config_path = configuration / "config.ini"
        config_path.write_text(config_ini, encoding="utf-8")

        worker_artifact = {
            "symbolic_name": "net.jolink.runtime.jdt.worker",
            "version": "0.1.0",
            "origin_url": "workspace://experiments/jdt-incremental-worker/worker",
            "sha256": sha256_file(worker_jar),
            "license_identity": "MIT",
            "compressed_bytes": worker_jar.stat().st_size,
            "installed_bytes": worker_jar.stat().st_size,
            "bundle_start_level": 4,
            "activation_policy": "lazy",
            "filename": worker_filename,
        }
        lock["worker_artifact"] = worker_artifact
        lock["worker_java_minimum"] = 8
        lock["worker_class_major"] = _PRODUCT_WORKER_CLASS_MAJOR
        lock["worker_build"] = {
            "source_fingerprint": _source_fingerprint(worker_root),
            "java_home_identity": {
                "java_binary_sha256": build_identity["java_binary_sha256"],
                "javac_binary_sha256": build_identity["javac_binary_sha256"],
            },
            "source_level": "8",
            "target_level": "8",
            "instrumentation": {
                "extension_id": "net.jolink.runtime.jdt.compilationObserver",
                "modifies_environment": False,
                "creates_problems": False,
                "annotation_processor": False,
                "post_processor": False,
                "evidence_mode": "enabled_with_required_off_on_parity",
            },
        }
        lock["equinox"]["configuration_sha256"] = sha256_file(config_path)
        lock["equinox"]["launcher_filename"] = launcher
        lock["equinox"]["configuration_materialization"] = (
            "replace file:plugins/ with the verified candidate plugins file URI"
        )
        lock["evidence_status"] = (
            "locked_phase_1a_candidate_pending_case_evidence"
        )
        _atomic_write(
            args.lock,
            (json.dumps(lock, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )
        if (args.product_lock is None) != (args.product_worker_base64 is None):
            raise WorkerBuildError(
                "Pass both --product-lock and --product-worker-base64."
            )
        if args.product_lock is not None:
            _update_product_artifacts(
                product_lock=args.product_lock,
                product_base64=args.product_worker_base64,
                worker_jar=worker_jar,
                source_fingerprint=lock["worker_build"]["source_fingerprint"],
                build_identity=build_identity,
            )

        print(
            json.dumps(
                {
                    "ok": True,
                    "candidate_id": candidate_id,
                    "worker_jar": str(worker_jar),
                    "worker_sha256": worker_artifact["sha256"],
                    "configuration_sha256": lock["equinox"][
                        "configuration_sha256"
                    ],
                    "evidence_status": lock["evidence_status"],
                }
            )
        )
        return 0
    except (OSError, subprocess.SubprocessError, WorkerBuildError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "JDT_WORKER_BUILD_FAILED",
                    "message": str(exc),
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
