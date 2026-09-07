#!/usr/bin/env python3
"""Measure FULL/idle/explicit-GC memory using a cached real Build World.

Creates a disposable Worker, never runs application/test code, and never edits
project sources. The only mutation is a comment in its private source mirror.
This is a diagnostic, not an automatic product GC policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import subprocess

import psutil

from jolink_runtime.launch.fast_test_cache import FastTestCache
from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate,
    PersistentJdtCompileSession,
    discover_target_system_entries,
    lombok_worker_jvm_arguments,
)
from jolink_runtime.launch.jdt_modules import ModuleCompileSession


def measure(
    project: Path, java_home: Path, candidate: JdtCandidate | None = None
) -> dict:
    cached = FastTestCache().load(project.resolve(), "maven")
    if cached is None:
        raise RuntimeError("Run one successful Fast Test for this project first.")
    world, _ = cached
    candidate = candidate or JdtCandidate.load_product()
    release = (java_home / "release").read_text()
    version = next(
        line.split("=", 1)[1].strip('"')
        for line in release.splitlines()
        if line.startswith("JAVA_VERSION=")
    )
    major = int(
        version.split(".")[1]
        if version.startswith("1.")
        else version.split(".")[0].split("-")[0]
    )
    snapshots = []
    peak_rss = 0
    done = threading.Event()

    base = ModuleCompileSession if world.modules else PersistentJdtCompileSession

    class MeasuredSession(base):
        def _build(self, kind, **kwargs):
            if kind == "FULL":
                snapshot(self, "before_full")
            result = super()._build(kind, **kwargs)
            if kind == "FULL":
                snapshot(self, "after_full")
            return result

    def snapshot(compiler, phase):
        client = compiler._client
        metrics = client.command("METRICS")["metrics"]
        snapshots.append(
            {
                "phase": phase,
                "rss_bytes": psutil.Process(client.process.pid).memory_info().rss,
                "metrics": metrics,
            }
        )

    with tempfile.TemporaryDirectory(prefix="jolink-worker-memory-") as temp:
        compiler = MeasuredSession(
            **(
                {"modules": world.modules, "target_module": world.module_root}
                if world.modules
                else {}
            ),
            root=Path(temp) / "worker",
            candidate=candidate,
            worker_java_home=java_home,
            source_roots=world.main_source_roots,
            classpath_entries=(
                *discover_target_system_entries(
                    world.target_java_home, world.source_level
                ),
                *world.main_dependencies,
            ),
            source_encoding=world.source_encoding,
            source_level=world.source_level,
            method_parameters=world.method_parameters,
            test_source_roots=world.test_source_roots,
            test_classpath_entries=world.test_dependencies,
            processor_entries=world.processor_entries,
            java_agents=world.java_agents,
            extra_jvm_arguments=(
                *world.extra_worker_jvm_arguments,
                *lombok_worker_jvm_arguments(
                    major, lombok_enabled=bool(world.java_agents)
                ),
            ),
            min_heap_mb=world.worker_min_heap_mb,
            max_heap_mb=world.worker_max_heap_mb,
        )

        def sample():
            nonlocal peak_rss
            while not done.wait(0.05):
                client = compiler._client
                if client is not None:
                    try:
                        peak_rss = max(
                            peak_rss,
                            psutil.Process(client.process.pid).memory_info().rss,
                        )
                    except psutil.Error:
                        pass

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        try:
            full = compiler.start()
            if not full.compile_ok:
                raise AssertionError(full.diagnostics)
            full_classes = {
                f"{group}/{path.relative_to(directory).as_posix()}": hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for group, directory in (
                    ("main", compiler.output_directory),
                    ("test", compiler.test_output_directory),
                )
                for path in directory.rglob("*.class")
            }
            time.sleep(2)
            snapshot(compiler, "idle_2s")
            started = time.monotonic()
            gc_result = compiler._client.command("GC")
            gc_ms = round((time.monotonic() - started) * 1000, 1)
            snapshot(compiler, "after_gc")
            time.sleep(1)
            snapshot(compiler, "after_gc_1s")
            thread_dump = subprocess.run(
                [
                    str(
                        java_home / "bin" / ("jcmd.exe" if os.name == "nt" else "jcmd")
                    ),
                    str(compiler._client.process.pid),
                    "Thread.print",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            dump = thread_dump.stdout
            indexing_threads = [
                part[:4000]
                for part in dump.split("\n\n")
                if "org.eclipse.jdt.internal.core.search.indexing" in part
            ]
            time.sleep(8)
            snapshot(compiler, "after_gc_10s")
            started = time.monotonic()
            compiler._client.command("GC")
            second_gc_ms = round((time.monotonic() - started) * 1000, 1)
            time.sleep(1)
            snapshot(compiler, "after_second_gc")
            private = next(
                path
                for path in compiler._source_map.values()
                if path.name not in {"package-info.java", "module-info.java"}
            )
            private.write_bytes(
                private.read_bytes() + b"\n// private GC diagnostic edit\n"
            )
            incremental = compiler._build(
                "INCREMENTAL",
                source_changes_pending=False,
                touched_sources=(
                    private.relative_to(compiler.private_project).as_posix(),
                ),
            )
            snapshot(compiler, "after_incremental")
            if not incremental.compile_ok:
                raise AssertionError(incremental.diagnostics)
            return {
                "java_version": version,
                "worker_java_home": str(java_home),
                "worker_pid": compiler._client.process.pid,
                "source_count": len(compiler._source_map),
                "min_heap_mb": compiler.min_heap_mb,
                "max_heap_mb": compiler.max_heap_mb,
                "full_ms": full.elapsed_ms,
                "candidate_identity": candidate.root.name,
                "full_class_count": len(full_classes),
                "full_class_tree_sha256": hashlib.sha256(
                    json.dumps(full_classes, sort_keys=True).encode()
                ).hexdigest(),
                "peak_rss_bytes": peak_rss,
                "explicit_gc_ms": gc_ms,
                "gc_requested": gc_result["status"],
                "second_gc_ms": second_gc_ms,
                "thread_dump_returncode": thread_dump.returncode,
                "indexing_threads": indexing_threads,
                "incremental_after_gc_ms": incremental.elapsed_ms,
                "incremental_after_gc_kind": incremental.actual_build_kind,
                "incremental_after_gc_sources": incremental.compiled_source_count,
                "snapshots": snapshots,
            }
        finally:
            done.set()
            sampler.join(1)
            compiler.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--java-home", type=Path, action="append", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--candidate-ref",
        help="Developer comparison: use an already-installed Worker lock from this git ref.",
    )
    args = parser.parse_args()
    reports = []
    candidate = None
    if args.candidate_ref:
        raw = subprocess.check_output(
            [
                "git",
                "show",
                f"{args.candidate_ref}:src/jolink_runtime/launch/jdt-product-candidate.json",
            ],
            cwd=Path(__file__).resolve().parents[1],
        ).replace(b"\r\n", b"\n")
        lock = json.loads(raw)
        root = (
            Path.home()
            / ".cache/jolink-runtime/jdt-worker/candidates"
            / lock["candidate_id"]
            / hashlib.sha256(raw).hexdigest()
        )
        candidate = JdtCandidate._load_root(lock, root, verify=False)
    for home in args.java_home:
        result = measure(args.project, home, candidate)
        reports.append(result)
        args.report.write_text(json.dumps(reports, indent=2), encoding="utf-8")
        print(json.dumps(result), flush=True)
