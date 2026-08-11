#!/usr/bin/env python3
"""Run a bounded non-evidence smoke test of the locked Headless JDT worker."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class SmokeError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def tree_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(child.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(child)))
    return digest.hexdigest()


def java_tool(java_home: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    path = java_home / "bin" / f"{name}{suffix}"
    if not path.is_file():
        raise SmokeError(f"JDK tool is unavailable: {name}")
    return path


def run_checked(
    command: list[str], *, cwd: Path, timeout: float, stderr_path: Path
) -> None:
    with stderr_path.open("ab") as stderr:
        completed = subprocess.run(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            timeout=timeout,
            check=False,
        )
    if completed.returncode != 0:
        raise SmokeError(f"Command failed with exit code {completed.returncode}.")


def parse_helper_output(path: Path) -> dict[str, Any]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in values:
            raise SmokeError("Invalid target-system helper output.")
        values[key] = value
    if values.get("format") != "jolink-target-system-libraries-v1":
        raise SmokeError("Unexpected target-system helper format.")
    try:
        count = int(values["entry.count"])
        entries = [
            {
                "path": Path(
                    base64.b64decode(values[f"entry.{index}.base64"]).decode(
                        "utf-8"
                    )
                ),
                "present": values[f"entry.{index}.present"] == "true",
            }
            for index in range(count)
        ]
        vendor = base64.b64decode(values["java.vendor.base64"]).decode("utf-8")
        version = base64.b64decode(values["java.version.base64"]).decode("utf-8")
        java_home = base64.b64decode(values["java.home.base64"]).decode("utf-8")
    except (KeyError, ValueError, UnicodeError) as exc:
        raise SmokeError("Incomplete target-system helper output.") from exc
    if not entries:
        raise SmokeError("Target-system helper returned no entries.")
    if any(item["present"] != item["path"].exists() for item in entries):
        raise SmokeError("Target-system entry changed after helper capture.")
    return {
        "vendor": vendor,
        "version": version,
        "java_home": java_home,
        "method": values["discovery.method"],
        "entries": entries,
    }


def snapshot_target_system_libraries(
    *, target_java_home: Path, attempt: Path, helper_source: Path
) -> dict[str, Any]:
    classes = attempt / "target-system-helper-classes"
    classes.mkdir(parents=True)
    output = attempt / "target-system-libraries.properties"
    stderr = attempt / "target-system-helper.stderr.log"
    run_checked(
        [
            str(java_tool(target_java_home, "javac")),
            "-encoding",
            "UTF-8",
            "-source",
            "1.8",
            "-target",
            "1.8",
            "-d",
            str(classes),
            str(helper_source),
        ],
        cwd=attempt,
        timeout=30,
        stderr_path=stderr,
    )
    run_checked(
        [
            str(java_tool(target_java_home, "java")),
            "-cp",
            str(classes),
            "net.jolink.runtime.jdt.helper.TargetSystemLibraries",
            str(output),
        ],
        cwd=attempt,
        timeout=30,
        stderr_path=stderr,
    )
    snapshot = parse_helper_output(output)
    ordered: list[dict[str, Any]] = []
    overall = hashlib.sha256()
    available_entries: list[Path] = []
    for index, item in enumerate(snapshot["entries"]):
        entry = item["path"]
        if item["present"]:
            entry_type = "archive" if entry.is_file() else "class_directory"
            fingerprint = (
                sha256_file(entry) if entry.is_file() else tree_fingerprint(entry)
            )
            available_entries.append(entry)
        else:
            entry_type = "missing"
            fingerprint = hashlib.sha256(str(entry).encode("utf-8")).hexdigest()
        overall.update(str(index).encode("ascii"))
        overall.update(b"\0")
        overall.update(entry_type.encode("ascii"))
        overall.update(b"\0")
        overall.update(bytes.fromhex(fingerprint))
        ordered.append(
            {
                "index": index,
                "entry_type": entry_type,
                "sha256": fingerprint,
            }
        )

    worker_input = attempt / "target-system-library-paths.txt"
    worker_input.write_text(
        "".join(f"{entry.resolve()}\n" for entry in available_entries),
        encoding="utf-8",
    )
    return {
        "vendor": snapshot["vendor"],
        "version": snapshot["version"],
        "java_home_identity_sha256": hashlib.sha256(
            snapshot["java_home"].encode("utf-8")
        ).hexdigest(),
        "system_library_discovery_method": snapshot["method"],
        "system_library_fingerprint": overall.hexdigest(),
        "entries": ordered,
        "missing_entry_count": sum(
            1 for item in snapshot["entries"] if not item["present"]
        ),
        "worker_input": worker_input,
    }


class WorkerClient:
    def __init__(
        self,
        *,
        process: subprocess.Popen[str],
        stderr_stream: Any,
        timeout: float,
    ) -> None:
        self.process = process
        self.stderr_stream = stderr_stream
        self.timeout = timeout
        self.frames: queue.Queue[str | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self.frames.put(line.rstrip("\r\n"))
        finally:
            self.frames.put(None)

    def receive(self) -> dict[str, Any]:
        try:
            line = self.frames.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise SmokeError("Timed out waiting for worker protocol frame.") from exc
        if line is None:
            raise SmokeError(
                f"Worker exited before a protocol frame (code={self.process.poll()})."
            )
        try:
            frame = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SmokeError(
                f"Worker stdout contained a non-JSON frame: {line[:200]!r}"
            ) from exc
        if not isinstance(frame, dict):
            raise SmokeError("Worker protocol frame must be an object.")
        return frame

    def command(self, value: str) -> dict[str, Any]:
        if self.process.stdin is None:
            raise SmokeError("Worker stdin is unavailable.")
        self.process.stdin.write(value + "\n")
        self.process.stdin.flush()
        return self.receive()

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                try:
                    self.command("STOP")
                except (BrokenPipeError, SmokeError):
                    pass
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
        finally:
            self.stderr_stream.close()


def load_lock(path: Path) -> dict[str, Any]:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SmokeError("Unable to read candidate lock.") from exc
    if "worker_artifact" not in lock:
        raise SmokeError("Build the locked worker before running the smoke test.")
    return lock


def verify_candidate(lock: dict[str, Any], candidate_root: Path) -> None:
    artifacts = [*lock["artifacts"], lock["worker_artifact"]]
    for artifact in artifacts:
        path = candidate_root / "plugins" / artifact["filename"]
        if not path.is_file() or sha256_file(path) != artifact["sha256"]:
            raise SmokeError(f"Locked candidate artifact changed: {artifact['filename']}")
    config = candidate_root / "configuration" / "config.ini"
    if not config.is_file() or sha256_file(config) != lock["equinox"][
        "configuration_sha256"
    ]:
        raise SmokeError("Locked Equinox configuration changed.")


def start_worker(
    *,
    lock: dict[str, Any],
    candidate_root: Path,
    worker_java_home: Path,
    attempt: Path,
    system_libraries_file: Path,
    instrumentation: str,
    timeout: float,
) -> WorkerClient:
    configuration = attempt / "configuration"
    configuration.mkdir(parents=True)
    template = (candidate_root / "configuration" / "config.ini").read_text(
        encoding="utf-8"
    )
    plugin_uri_prefix = (candidate_root / "plugins").resolve().as_uri() + "/"
    materialized = template.replace("file:plugins/", plugin_uri_prefix)
    (configuration / "config.ini").write_text(materialized, encoding="utf-8")
    workspace = attempt / "workspace"
    workspace.mkdir()
    stderr_path = attempt / "worker.stderr.log"
    stderr_stream = stderr_path.open("w", encoding="utf-8")
    launcher = candidate_root / "plugins" / lock["equinox"]["launcher_filename"]
    command = [
        str(java_tool(worker_java_home, "java")),
        "-Xms64m",
        "-Xmx512m",
        "-jar",
        str(launcher),
        "-clean",
        "-nosplash",
        "-install",
        str(candidate_root),
        "-configuration",
        str(configuration),
        "-data",
        str(workspace),
        "-application",
        "net.jolink.runtime.jdt.worker",
        "--system-libraries",
        str(system_libraries_file),
        "--instrumentation",
        instrumentation,
    ]
    process = subprocess.Popen(
        command,
        cwd=candidate_root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr_stream,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    client = WorkerClient(process=process, stderr_stream=stderr_stream, timeout=timeout)
    ready = client.receive()
    if ready.get("ok") is not True or ready.get("status") != "ready":
        client.close()
        raise SmokeError(f"Worker did not become ready: {ready}")
    if ready.get("java_builder_count") != 1:
        client.close()
        raise SmokeError("Worker project does not have exactly one Java Builder.")
    client.ready = ready
    return client


def output_hashes(output: Path) -> dict[str, str]:
    if not output.is_dir():
        return {}
    return {
        path.relative_to(output).as_posix(): sha256_file(path)
        for path in sorted(output.rglob("*.class"))
    }


def class_major(path: Path) -> int:
    payload = path.read_bytes()[:8]
    if len(payload) != 8 or payload[:4] != b"\xca\xfe\xba\xbe":
        raise SmokeError("Invalid class output.")
    return int.from_bytes(payload[6:8], "big")


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=root / "locks" / "eclipse-4.40-current.json",
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path.home() / ".cache" / "jolink-runtime" / "jdt-poc",
    )
    parser.add_argument("--worker-java-home", type=Path, required=True)
    parser.add_argument("--target-java-home", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--keep-attempt", action="store_true")
    args = parser.parse_args(argv)

    attempt_id = f"bootstrap-{uuid.uuid4().hex[:12]}"
    attempts_root = args.cache_root / "attempts"
    attempts_root.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix=f"{attempt_id}-", dir=attempts_root))
    client: WorkerClient | None = None
    started = time.monotonic()
    try:
        lock = load_lock(args.lock)
        candidate_root = args.cache_root / "candidates" / lock["candidate_id"]
        verify_candidate(lock, candidate_root)
        snapshot = snapshot_target_system_libraries(
            target_java_home=args.target_java_home,
            attempt=attempt,
            helper_source=root
            / "target-system-helper"
            / "src"
            / "net"
            / "jolink"
            / "runtime"
            / "jdt"
            / "helper"
            / "TargetSystemLibraries.java",
        )
        client = start_worker(
            lock=lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            attempt=attempt,
            system_libraries_file=snapshot["worker_input"],
            instrumentation="enabled",
            timeout=args.timeout,
        )
        ready_frame = dict(client.ready)

        project = attempt / "workspace" / "plain-fixture"
        source = project / "src"
        shutil.copytree(
            root / "fixtures" / "plain-java" / "src",
            source,
            dirs_exist_ok=True,
        )
        full = client.command("BUILD\tFULL")
        if full.get("ok") is not True:
            raise SmokeError(f"Full build failed: {full}")

        classes = project / "bin"
        full_hashes = output_hashes(classes)
        if set(full_hashes) != {
            "example/Api.class",
            "example/Application.class",
            "example/Service.class",
        }:
            raise SmokeError("Full build output set is incomplete.")
        if any(class_major(classes / relative) != 52 for relative in full_hashes):
            raise SmokeError("Full build did not produce Java 8 class files.")

        application = source / "example" / "Application.java"
        original = application.read_text(encoding="utf-8")
        application.write_text(
            original.replace("service.calculate(20)", "service.calculate(21)"),
            encoding="utf-8",
        )
        incremental = client.command("BUILD\tINCREMENTAL")
        if incremental.get("ok") is not True:
            raise SmokeError(f"Incremental build failed: {incremental}")
        incremental_hashes = output_hashes(classes)
        if incremental_hashes.get("example/Application.class") == full_hashes.get(
            "example/Application.class"
        ):
            raise SmokeError("Edited class output did not change.")
        if incremental_hashes.get("example/Api.class") != full_hashes.get(
            "example/Api.class"
        ):
            raise SmokeError("Unchanged upstream class output changed.")

        no_op = client.command("BUILD\tINCREMENTAL")
        if no_op.get("ok") is not True:
            raise SmokeError(f"No-op build failed: {no_op}")
        if output_hashes(classes) != incremental_hashes:
            raise SmokeError("No-op build changed class outputs.")

        stop = client.command("STOP")
        if stop.get("ok") is not True:
            raise SmokeError("Worker did not acknowledge shutdown.")
        client.process.wait(timeout=5)
        client.stderr_stream.close()
        client = None

        off_attempt = attempt / "instrumentation-off"
        off_attempt.mkdir()
        off_client = start_worker(
            lock=lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            attempt=off_attempt,
            system_libraries_file=snapshot["worker_input"],
            instrumentation="disabled",
            timeout=args.timeout,
        )
        try:
            off_source = (
                off_attempt / "workspace" / "plain-fixture" / "src"
            )
            shutil.copytree(
                root / "fixtures" / "plain-java" / "src",
                off_source,
                dirs_exist_ok=True,
            )
            instrumentation_off_full = off_client.command("BUILD\tFULL")
            off_hashes = output_hashes(
                off_attempt / "workspace" / "plain-fixture" / "bin"
            )
            instrumentation_parity = (
                instrumentation_off_full.get("ok") is True
                and off_hashes == full_hashes
                and instrumentation_off_full.get("diagnostics")
                == full.get("diagnostics")
            )
            if not instrumentation_parity:
                raise SmokeError("Instrumentation OFF/ON full-build parity failed.")
        finally:
            off_client.close()

        oracle_attempt = attempt / "clean-full-oracle"
        oracle_attempt.mkdir()
        oracle_client = start_worker(
            lock=lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            attempt=oracle_attempt,
            system_libraries_file=snapshot["worker_input"],
            instrumentation="enabled",
            timeout=args.timeout,
        )
        try:
            oracle_source = (
                oracle_attempt / "workspace" / "plain-fixture" / "src"
            )
            shutil.copytree(source, oracle_source, dirs_exist_ok=True)
            oracle_full = oracle_client.command("BUILD\tFULL")
            oracle_hashes = output_hashes(
                oracle_attempt / "workspace" / "plain-fixture" / "bin"
            )
            oracle_equal = (
                oracle_full.get("ok") is True
                and oracle_hashes == incremental_hashes
                and oracle_full.get("diagnostics")
                == incremental.get("diagnostics")
            )
            if not oracle_equal:
                raise SmokeError("Incremental output differs from clean-full oracle.")
        finally:
            oracle_client.close()

        report = {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "status": "bootstrap_smoke_passed",
            "evidence_status": "not_phase_1a_evidence",
            "candidate_id": lock["candidate_id"],
            "worker_ready": True,
            "worker": {
                "jdt_bundle_version": ready_frame["jdt_bundle_version"],
                "ready_frame": ready_frame,
            },
            "target_system_library": {
                key: value
                for key, value in snapshot.items()
                if key not in {"entries", "worker_input"}
            }
            | {"entries": snapshot["entries"]},
            "cases": {
                "full": full,
                "leaf_incremental": incremental,
                "no_op_incremental": no_op,
                "instrumentation_off_full": instrumentation_off_full,
                "clean_full_oracle": oracle_full,
            },
            "output": {
                "full_class_count": len(full_hashes),
                "incremental_class_count": len(incremental_hashes),
                "class_major": 52,
                "instrumentation_off_on_parity": instrumentation_parity,
                "incremental_equals_clean_full_oracle": oracle_equal,
            },
            "limitations": [
                "the local JDK 8 advertises missing boot-class-path entries, so this generation is not admissible Phase 1A evidence",
                "only the bootstrap A1/A2/A3 subset ran; A4 through A10 are not implemented",
                "this run cannot satisfy a Phase 1A gate",
            ],
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        }
        reports = args.cache_root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        report_path = reports / f"{attempt_id}.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": report["status"],
                    "attempt_id": attempt_id,
                    "candidate_id": lock["candidate_id"],
                    "report_path": str(report_path),
                    "limitations": report["limitations"],
                }
            )
        )
        if not args.keep_attempt:
            shutil.rmtree(attempt)
        return 0
    except (
        OSError,
        SmokeError,
        subprocess.SubprocessError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "JDT_BOOTSTRAP_SMOKE_FAILED",
                    "message": str(exc),
                    "attempt_path": str(attempt),
                }
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
