#!/usr/bin/env python3
"""Probe Java 11 JRT system libraries with the locked product JDT Worker."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate,
    PersistentJdtCompileSession,
)


def major(path: Path) -> int:
    raw = path.read_bytes()
    return int.from_bytes(raw[6:8], "big")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java11-home", type=Path, required=True)
    args = parser.parse_args()
    java11 = args.java11_home.expanduser().resolve(strict=True)
    jrt = java11 / "lib/jrt-fs.jar"
    if not jrt.is_file():
        raise SystemExit("JDK 11 jrt-fs.jar is unavailable")
    with tempfile.TemporaryDirectory(prefix="jolink-jdt11-") as raw:
        root = Path(raw)
        source_root = root / "project/src/main/java"
        source = source_root / "example/Java11Api.java"
        source.parent.mkdir(parents=True)
        source.write_text(
            "package example; public class Java11Api { "
            "public boolean blank(String value){ return value.isBlank(); } }\n",
            encoding="utf-8",
        )
        session = PersistentJdtCompileSession(
            root=root / "session",
            candidate=JdtCandidate.load_product(),
            worker_java_home=java11,
            source_roots=(source_root,),
            classpath_entries=(jrt,),
            source_encoding="UTF-8",
            source_level=11,
        )
        try:
            full = session.start()
            if not full.compile_ok:
                print(json.dumps({"ok": False, "diagnostics": full.diagnostics}))
                return 2
            output = session.output_directory / "example/Java11Api.class"
            if major(output) != 55:
                raise AssertionError("Java 11 output is not class major 55")
            session.accept_baseline()
            source.write_text(
                "package example; public class Java11Api { "
                "public boolean blank(String value){ return value.strip().isEmpty(); } }\n",
                encoding="utf-8",
            )
            incremental = session.compile((source,))
            if not incremental.compile_ok or major(output) != 55:
                raise AssertionError(incremental)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "full_ms": full.elapsed_ms,
                        "incremental_ms": incremental.elapsed_ms,
                        "class_major": major(output),
                    },
                    separators=(",", ":"),
                )
            )
        finally:
            session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
