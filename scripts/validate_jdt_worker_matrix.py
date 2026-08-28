#!/usr/bin/env python3
"""Run one product Worker JAR across JDK8+ FULL/Incremental/recovery."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate,
    PersistentJdtCompileSession,
    discover_java8_system_entries,
)


def _source(value: str) -> str:
    return (
        "package example; public class App { "
        f"public int value() {{ return {value}; }} }}"
    )


def _run_one(
    *,
    candidate: JdtCandidate,
    worker_java_home: Path,
    target_java_home: Path,
) -> dict[str, object]:
    worker = candidate.verify_worker_java(worker_java_home)
    with tempfile.TemporaryDirectory(prefix=f"jolink-worker-{worker.major}-") as raw:
        root = Path(raw)
        source_root = root / "source"
        source = source_root / "example/App.java"
        source.parent.mkdir(parents=True)
        source.write_text(_source("1"), encoding="utf-8")
        session = PersistentJdtCompileSession(
            root=root / "session",
            candidate=candidate,
            worker_java_home=worker.home,
            source_roots=(source_root,),
            classpath_entries=discover_java8_system_entries(target_java_home),
            source_encoding="UTF-8",
            min_heap_mb=64,
            max_heap_mb=512,
        )
        try:
            started = time.monotonic()
            full = session.start()
            full_ms = round((time.monotonic() - started) * 1000, 1)
            if not full.compile_ok:
                raise RuntimeError(f"JDK {worker.major} FULL failed: {full.diagnostics}")
            session.accept_baseline()

            source.write_text(_source("2"), encoding="utf-8")
            incremental = session.compile((source,))
            if not incremental.compile_ok or incremental.candidate_changed_classes != (
                "example/App.class",
            ):
                raise RuntimeError(
                    f"JDK {worker.major} incremental failed: {incremental}"
                )

            source.write_text(_source("missingSymbol"), encoding="utf-8")
            failed = session.compile((source,))
            if failed.compile_ok or failed.error_count < 1:
                raise RuntimeError(
                    f"JDK {worker.major} compile error was missed: {failed}"
                )

            source.write_text(_source("3"), encoding="utf-8")
            recovered = session.compile((source,))
            if not recovered.compile_ok or recovered.candidate_changed_classes != (
                "example/App.class",
            ):
                raise RuntimeError(f"JDK {worker.major} recovery failed: {recovered}")
            return {
                "worker_java_major": worker.major,
                "worker_data_model": worker.data_model,
                "full_ms": full_ms,
                "incremental_ms": incremental.elapsed_ms,
                "recovery_ms": recovered.elapsed_ms,
                "compile_error_count": failed.error_count,
                "stopped": session.close(),
            }
        finally:
            if session.root.exists():
                session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-java-home", type=Path, required=True)
    parser.add_argument(
        "--worker-java-home",
        type=Path,
        action="append",
        required=True,
    )
    args = parser.parse_args()
    candidate = JdtCandidate.load_product()
    results = [
        _run_one(
            candidate=candidate,
            worker_java_home=home,
            target_java_home=args.target_java_home,
        )
        for home in args.worker_java_home
    ]
    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
