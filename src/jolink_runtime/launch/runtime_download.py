"""Shared transport for pinned JDK and Eclipse artifacts."""

from __future__ import annotations

import hashlib
import http.client
import logging
import os
import urllib.request
from pathlib import Path

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
        if path.startswith("jdk/temurin-"):
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
    """Download once per source; callers keep their existing integrity checks."""
    sources = _download_sources(official_url, mirror_path)
    for index, (source, url) in enumerate(sources):
        logger.info(
            "jdt.download.started source=%s artifact=%s", source, destination.name
        )
        request = urllib.request.Request(url, headers={"User-Agent": "joLink-Runtime"})
        try:
            digest = hashlib.sha256()
            total = 0
            with (
                urllib.request.urlopen(request, timeout=30) as response,
                destination.open("wb") as output,
            ):
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise ValueError(
                            "Runtime artifact exceeded the download limit."
                        )
                    digest.update(chunk)
                    output.write(chunk)
            logger.info(
                "jdt.download.finished source=%s artifact=%s bytes=%s",
                source,
                destination.name,
                total,
            )
            return digest.hexdigest()
        except (OSError, http.client.HTTPException) as error:
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
    raise AssertionError("No runtime download source")
