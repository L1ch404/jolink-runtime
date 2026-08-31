"""Canonical byte rules for text assets covered by product locks."""

from __future__ import annotations


def canonical_lf_bytes(data: bytes) -> bytes:
    """Return platform-independent LF bytes for a locked text asset."""

    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


__all__ = ["canonical_lf_bytes"]
