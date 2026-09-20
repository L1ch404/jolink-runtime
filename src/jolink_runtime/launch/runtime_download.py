"""Shared transport for pinned JDK and Eclipse artifacts."""

from __future__ import annotations

import hashlib
import http.client
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

from .runtime_install import (
    check_cancelled,
    error_details,
    progress_context,
    report_progress,
)

logger = logging.getLogger(__name__)
JOLINK_MIRROR = "https://7355608.net/jolink/assets"
TUNA_MIRROR = "https://mirrors.tuna.tsinghua.edu.cn"


def _download_sources(
    official_url: str, mirror_path: str | None
) -> list[tuple[str, str]]:
    mirror = os.environ.get("JOLINK_DOWNLOAD_MIRROR", "official").strip().rstrip("/")
    official = [("official", official_url)]
    if not mirror_path or not mirror or mirror.lower() == "official":
        return official
    path = mirror_path.lstrip("/")
    if mirror.lower() == "cn":
        sources = []
        # The source kit has no architecture/OS directory on the JDK mirror.
        # It is requested only by the offline packaging command.
        if path.startswith("jdk/temurin-") and "-sources_" not in path:
            _, version, filename = path.split("/", 2)
            major = version.removeprefix("temurin-").split(".", 1)[0]
            arch, system = filename.split("_")[1:3]
            sources.append(
                (
                    "tuna",
                    f"{TUNA_MIRROR}/Adoptium/{major}/jdk/{arch}/{system}/{filename}",
                )
            )
        elif path.startswith("eclipse/"):
            sources.append(("tuna", f"{TUNA_MIRROR}/eclipse/{path}"))
        return sources + [("jolink", f"{JOLINK_MIRROR}/{path}")] + official
    return [("mirror", f"{mirror}/{path}")] + official


def download_file(
    official_url: str,
    destination: Path,
    *,
    mirror_path: str | None,
    max_bytes: int | None = None,
) -> str:
    """Retry a transient network failure once; never retry local file errors."""
    sources = _download_sources(official_url, mirror_path)
    for index, (source, url) in enumerate(sources):
        for attempt in (1, 2):
            check_cancelled()
            report_progress(
                current_artifact=destination.name,
                source=source,
                attempt=attempt,
                downloaded_bytes=0,
                total_bytes=None,
                last_error=None,
            )
            logger.info(
                "jdt.download.started source=%s artifact=%s attempt=%s",
                source,
                destination.name,
                attempt,
            )
            request = urllib.request.Request(
                url, headers={"User-Agent": "joLink-Runtime"}
            )
            phase = "connect"
            try:
                digest = hashlib.sha256()
                total = 0
                with urllib.request.urlopen(request, timeout=30) as response:
                    length = getattr(response, "headers", {}).get("Content-Length")
                    report_progress(
                        total_bytes=int(length)
                        if length and str(length).isdigit()
                        else None
                    )
                    phase = "open_local_file"
                    with destination.open("wb") as output:
                        while True:
                            check_cancelled()
                            phase = "read_network"
                            chunk = response.read(64 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            if max_bytes is not None and total > max_bytes:
                                raise ValueError(
                                    "Runtime artifact exceeded the download limit."
                                )
                            phase = "write_local_file"
                            output.write(chunk)
                            digest.update(chunk)
                            report_progress(downloaded_bytes=total)
                        phase = "close_local_file"
                logger.info(
                    "jdt.download.finished source=%s artifact=%s bytes=%s",
                    source,
                    destination.name,
                    total,
                )
                return digest.hexdigest()
            except (OSError, http.client.HTTPException) as error:
                details = error_details(error)
                logger.warning(
                    "jdt.download.failed source=%s artifact=%s phase=%s details=%s",
                    source,
                    destination.name,
                    phase,
                    details,
                )
                report_progress(last_error=details)
                if isinstance(error, urllib.error.HTTPError):
                    error.close()
                if phase not in {"connect", "read_network"}:
                    raise
                transient = not isinstance(
                    getattr(error, "reason", error), ssl.SSLCertVerificationError
                )
                if isinstance(error, urllib.error.HTTPError):
                    transient = error.code in {408, 500, 502, 503, 504}
                if transient and attempt == 1:
                    progress = progress_context.get()
                    if progress is None:
                        time.sleep(1)
                    else:
                        progress.cancelled.wait(1)
                    continue
                if index == len(sources) - 1:
                    raise
                logger.warning(
                    "jdt.download.fallback source=%s artifact=%s error=%s status=%s next=%s",
                    source,
                    destination.name,
                    type(error).__name__,
                    getattr(error, "code", None),
                    sources[index + 1][0],
                )
                break
    raise AssertionError("No runtime download source")
