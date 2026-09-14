"""One private, version-pinned JDK shared by this user's product Workers."""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import tarfile
import threading
import uuid
import zipfile
from pathlib import Path

from .runtime_download import download_file
from .runtime_install import (
    check_cancelled,
    create_directory,
    error_details,
    report_progress,
)

logger = logging.getLogger(__name__)
_install_lock = threading.Lock()


class WorkerRuntimeError(RuntimeError):
    pass


def managed_worker_java_home() -> Path:
    override = os.environ.get("JOLINK_WORKER_JAVA_HOME")
    if override:
        return Path(override).expanduser().resolve()
    lock = json.loads(Path(__file__).with_name("worker-runtime.json").read_text())
    system = platform.system()
    arch = {"AMD64": "x86_64", "aarch64": "arm64", "ARM64": "arm64"}.get(
        platform.machine(), platform.machine()
    )
    key = f"{system}-{arch}"
    distribution = lock["distributions"].get(key)
    if distribution is None:
        raise WorkerRuntimeError(
            f"No bundled Worker JDK for {key}; set JOLINK_WORKER_JAVA_HOME."
        )
    root = (
        Path.home()
        / ".cache/jolink-runtime/runtimes"
        / ("temurin-" + lock["version"])
        / key
    )
    relative = Path("jdk-" + lock["version"])
    if system == "Darwin":
        relative /= "Contents/Home"
    home = root / relative
    executable = Path("bin/java.exe" if system == "Windows" else "bin/java")
    if (home / executable).is_file():
        return home
    with _install_lock:
        if (home / executable).is_file():
            return home
        create_directory(root.parent, parents=True, exist_ok=True)
        staging = root.parent / (".install-" + uuid.uuid4().hex)
        create_directory(staging)
        try:
            check_cancelled()
            archive = staging / distribution["filename"]
            report_progress(
                phase="download_jdk",
                completed_files=0,
                total_files=1,
                current_artifact=archive.name,
                downloaded_bytes=0,
                total_bytes=None,
            )
            logger.info(
                "jdt.worker.runtime.install version=%s platform=%s",
                lock["version"],
                key,
            )
            actual_sha = download_file(
                lock["release_url"] + "/" + archive.name,
                archive,
                mirror_path=f"jdk/temurin-{lock['version']}/{archive.name}",
            )
            if actual_sha != distribution["sha256"]:
                raise WorkerRuntimeError("Worker JDK archive checksum mismatch.")
            check_cancelled()
            report_progress(phase="install_jdk", completed_files=1)
            unpacked = staging / "runtime"
            create_directory(unpacked)
            if archive.suffix == ".zip":
                with zipfile.ZipFile(archive) as bundle:
                    bundle.extractall(unpacked)
            else:
                with tarfile.open(archive) as bundle:
                    bundle.extractall(unpacked, filter="data")
            if not (unpacked / relative / executable).is_file():
                raise WorkerRuntimeError(
                    "Worker JDK archive contains no Java executable."
                )
            check_cancelled()
            try:
                unpacked.rename(root)
            except OSError:
                if not (home / executable).is_file():
                    raise
            return home
        except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
            details = error_details(error)
            logger.exception("jdt.worker.runtime.install_failed details=%s", details)
            raise WorkerRuntimeError(
                "Unable to install the private Worker JDK. Prepare the Worker offline bundle "
                "or set JOLINK_WORKER_JAVA_HOME to an installed JDK."
            ) from error
        finally:
            shutil.rmtree(staging, ignore_errors=True)
