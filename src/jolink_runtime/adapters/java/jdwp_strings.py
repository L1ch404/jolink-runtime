"""Decode JVM strings without discarding valid Java UTF-16 code units."""

from __future__ import annotations

import re


_SURROGATE = re.compile("[\ud800-\udfff]")


def decode_jdwp_string(data: bytes) -> str:
    # HotSpot transmits a lone surrogate as its three-byte UTF-8 encoding.
    # Java permits these String/char values even though Unicode scalar UTF-8
    # does not. Preserve them; the shared JSON boundary renders them explicitly.
    value = data.decode("utf-8", errors="surrogatepass")
    if _SURROGATE.search(value):
        # Also accept a surrogate-pair encoding, preserving ordinary emoji as
        # one Unicode scalar instead of marking a valid pair as a special value.
        return value.encode("utf-16-le", errors="surrogatepass").decode(
            "utf-16-le", errors="surrogatepass",
        )
    return value
