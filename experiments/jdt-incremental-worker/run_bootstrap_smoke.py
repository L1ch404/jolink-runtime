#!/usr/bin/env python3
"""Run a bounded non-evidence smoke test of the locked Headless JDT worker."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import queue
import re
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


def canonical_json_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def git_identity(repository: Path) -> dict[str, Any]:
    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            raise SmokeError("Unable to capture git evidence identity.")
        return completed.stdout

    revision = git("rev-parse", "HEAD").strip()
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    diff = subprocess.run(
        ["git", "diff", "--binary", "--no-ext-diff", "HEAD", "--"],
        cwd=repository,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    if diff.returncode != 0:
        raise SmokeError("Unable to fingerprint tracked worktree changes.")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z")
    worktree_content = hashlib.sha256()
    worktree_content.update(diff.stdout)
    for relative in sorted(item for item in untracked.split("\0") if item):
        path = repository / relative
        if not path.is_file():
            raise SmokeError("Unable to fingerprint an untracked worktree entry.")
        worktree_content.update(relative.encode("utf-8"))
        worktree_content.update(b"\0")
        worktree_content.update(path.read_bytes())
        worktree_content.update(b"\0")
    return {
        "revision": revision,
        "dirty_worktree": bool(status),
        "worktree_status_sha256": hashlib.sha256(
            status.encode("utf-8")
        ).hexdigest(),
        "worktree_content_sha256": worktree_content.hexdigest(),
    }


def worker_java_identity(java_home: Path) -> dict[str, Any]:
    java = java_tool(java_home, "java")
    completed = subprocess.run(
        [str(java), "-XshowSettings:properties", "-version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise SmokeError("Unable to capture Worker JDK identity.")
    properties: dict[str, str] = {}
    for line in (completed.stdout + "\n" + completed.stderr).splitlines():
        match = re.match(r"\s*([A-Za-z0-9_.]+)\s*=\s*(.*)$", line)
        if match:
            properties[match.group(1)] = match.group(2).strip()
    required = ("java.vendor", "java.version", "java.vm.name", "os.arch")
    if any(not properties.get(key) for key in required):
        raise SmokeError("Worker JDK identity is incomplete.")
    return {
        "vendor": properties["java.vendor"],
        "version": properties["java.version"],
        "vm_name": properties["java.vm.name"],
        "architecture": properties["os.arch"],
        "java_binary_sha256": sha256_file(java),
    }


def candidate_report_identity(lock: dict[str, Any], lock_path: Path) -> dict[str, Any]:
    artifacts = [*lock["artifacts"], lock["worker_artifact"]]
    return {
        "candidate_id": lock["candidate_id"],
        "lock_schema_version": lock["schema_version"],
        "lock_file_sha256": sha256_file(lock_path),
        "content_metadata_sha256": lock["repository"]["content_metadata"][
            "sha256"
        ],
        "artifacts": [
            {
                "symbolic_name": artifact["symbolic_name"],
                "version": artifact["version"],
                "sha256": artifact["sha256"],
            }
            for artifact in artifacts
        ],
    }


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
    if values.get("format") != "jolink-target-system-libraries-v2":
        raise SmokeError("Unexpected target-system helper format.")

    def decode(key: str) -> str:
        try:
            return base64.b64decode(values[f"{key}.base64"]).decode("utf-8")
        except (KeyError, ValueError, UnicodeError) as exc:
            raise SmokeError("Incomplete target-system helper output.") from exc

    def entries(prefix: str) -> list[dict[str, Any]]:
        try:
            count = int(values[f"{prefix}.count"])
        except (KeyError, ValueError) as exc:
            raise SmokeError("Incomplete target-system helper output.") from exc
        result: list[dict[str, Any]] = []
        for index in range(count):
            state = values.get(f"{prefix}.{index}.present")
            if state not in {"true", "false"}:
                raise SmokeError("Invalid target-system entry state.")
            result.append(
                {
                    "path": Path(decode(f"{prefix}.{index}")),
                    "present": state == "true",
                }
            )
        return result

    try:
        vendor = decode("java.vendor")
        version = decode("java.version")
        java_home = decode("java.home")
        method = values["discovery.method"]
    except KeyError as exc:
        raise SmokeError("Incomplete target-system helper output.") from exc
    groups = {
        "bootstrap_advertised": entries("bootstrap.advertised"),
        "extension_directories": entries("extension.directory"),
        "endorsed_directories": entries("endorsed.directory"),
        "compiler_platform": entries("compiler.platform"),
        "runtime_extension_urls": entries("runtime.extension.url"),
    }
    if not groups["bootstrap_advertised"] or not groups["compiler_platform"]:
        raise SmokeError("Target-system helper returned no platform entries.")
    if any(
        item["present"] != item["path"].exists()
        for group in groups.values()
        for item in group
    ):
        raise SmokeError("Target-system entry changed after helper capture.")
    return {
        "vendor": vendor,
        "version": version,
        "java_home": java_home,
        "method": method,
        **groups,
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
    def within(entry: Path, directories: list[dict[str, Any]]) -> bool:
        for item in directories:
            directory = item["path"]
            try:
                entry.relative_to(directory)
                return True
            except ValueError:
                continue
        return False

    def describe(
        group: str, index: int, item: dict[str, Any]
    ) -> tuple[dict[str, Any], bytes]:
        entry = item["path"]
        path_identity = hashlib.sha256(str(entry).encode("utf-8")).hexdigest()
        if item["present"]:
            entry_type = "archive" if entry.is_file() else "class_directory"
            content = sha256_file(entry) if entry.is_file() else tree_fingerprint(entry)
            state = "PRESENT"
        else:
            entry_type = "absent_placeholder"
            content = None
            state = "ABSENT"
        material = hashlib.sha256()
        material.update(group.encode("ascii"))
        material.update(b"\0")
        material.update(str(index).encode("ascii"))
        material.update(b"\0")
        material.update(bytes.fromhex(path_identity))
        material.update(b"\0")
        material.update(state.encode("ascii"))
        if content is not None:
            material.update(b"\0")
            material.update(bytes.fromhex(content))
        return (
            {
                "index": index,
                "state": state,
                "entry_type": entry_type,
                "path_identity_sha256": path_identity,
                "content_sha256": content,
            },
            material.digest(),
        )

    groups: dict[str, list[dict[str, Any]]] = {}
    overall = hashlib.sha256()
    for group in (
        "bootstrap_advertised",
        "extension_directories",
        "endorsed_directories",
        "compiler_platform",
        "runtime_extension_urls",
    ):
        described: list[dict[str, Any]] = []
        for index, item in enumerate(snapshot[group]):
            entry, material = describe(group, index, item)
            described.append(entry)
            overall.update(material)
        groups[group] = described

    compiler_entries = [
        item["path"]
        for item in snapshot["compiler_platform"]
        if item["present"]
    ]
    absent_advertised_boot = {
        item["path"]
        for item in snapshot["bootstrap_advertised"]
        if not item["present"]
    }
    absent_compiler_entries = {
        item["path"]
        for item in snapshot["compiler_platform"]
        if not item["present"]
    }
    unexpected_absent_compiler_entries = (
        absent_compiler_entries - absent_advertised_boot
    )
    if unexpected_absent_compiler_entries:
        raise SmokeError(
            "javac reported an absent platform entry that is not an advertised "
            "boot placeholder."
        )

    effective_endorsed = [
        entry
        for entry in compiler_entries
        if within(entry, snapshot["endorsed_directories"])
    ]
    if effective_endorsed:
        raise SmokeError(
            "Target JDK uses endorsed platform entries that Phase 1A does not model."
        )

    compiler_extensions = [
        entry
        for entry in compiler_entries
        if within(entry, snapshot["extension_directories"])
    ]
    runtime_extensions = [
        item["path"]
        for item in snapshot["runtime_extension_urls"]
        if item["present"]
    ]
    extension_validation = {
        "available": bool(snapshot["runtime_extension_urls"]),
        "status": (
            "exact_match"
            if compiler_extensions == runtime_extensions
            else "different_views_recorded"
        ),
        "compiler_extension_count": len(compiler_extensions),
        "runtime_extension_url_count": len(runtime_extensions),
    }

    worker_input = attempt / "target-system-library-paths.txt"
    worker_input.write_text(
        "".join(f"{entry.resolve()}\n" for entry in compiler_entries),
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
        "snapshot_format": "jolink-target-system-libraries-v2",
        "groups": groups,
        "compiler_platform_present_entry_count": len(compiler_entries),
        "compiler_platform_advertised_entry_count": len(
            snapshot["compiler_platform"]
        ),
        "compiler_platform_absent_placeholder_count": len(
            absent_compiler_entries
        ),
        "extension_validation": extension_validation,
        "tolerated_absent_entry_count": sum(
            1
            for group in (
                snapshot["bootstrap_advertised"],
                snapshot["extension_directories"],
                snapshot["endorsed_directories"],
            )
            for item in group
            if not item["present"]
        ),
        "effective_endorsed_entry_count": len(effective_endorsed),
        "admissible_for_phase_1a": True,
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

    def close(self) -> dict[str, Any]:
        cooperative_acknowledged = False
        forced = False
        try:
            if self.process.poll() is None:
                try:
                    response = self.command("STOP")
                    cooperative_acknowledged = (
                        response.get("ok") is True
                        and response.get("status") == "stopped"
                    )
                except (BrokenPipeError, SmokeError):
                    pass
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    forced = True
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
        finally:
            self.stderr_stream.close()
        return {
            "status": (
                "settled" if self.process.poll() is not None else "unsettled"
            ),
            "cooperative_stop_acknowledged": cooperative_acknowledged,
            "forced": forced,
            "exit_code": self.process.poll(),
            "direct_process_exited": self.process.poll() is not None,
        }


def load_lock(path: Path) -> dict[str, Any]:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SmokeError("Unable to read candidate lock.") from exc
    if "worker_artifact" not in lock:
        raise SmokeError("Build the locked worker before running the smoke test.")
    return lock


def verify_candidate(lock: dict[str, Any], candidate_root: Path) -> None:
    execution_environment = lock.get("execution_environment")
    if not isinstance(execution_environment, dict) or execution_environment.get(
        "status"
    ) != "satisfied":
        raise SmokeError("Candidate has no satisfied osgi.ee evidence.")
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


def revalidate_frozen_inputs(
    *,
    lock_path: Path,
    starting_lock: dict[str, Any],
    candidate_root: Path,
    target_java_home: Path,
    attempt: Path,
    helper_source: Path,
    starting_snapshot: dict[str, Any],
    fixture_roots: dict[str, Path],
    starting_fixture_fingerprints: dict[str, str],
    repository_root: Path,
    starting_git_identity: dict[str, Any],
) -> dict[str, Any]:
    ending_lock = load_lock(lock_path)
    if canonical_json_fingerprint(ending_lock) != canonical_json_fingerprint(
        starting_lock
    ):
        raise SmokeError("Candidate lock changed during the evidence run.")
    verify_candidate(ending_lock, candidate_root)

    post_attempt = attempt / "post-run-system-library-revalidation"
    post_attempt.mkdir()
    ending_snapshot = snapshot_target_system_libraries(
        target_java_home=target_java_home,
        attempt=post_attempt,
        helper_source=helper_source,
    )
    if ending_snapshot["system_library_fingerprint"] != starting_snapshot[
        "system_library_fingerprint"
    ]:
        raise SmokeError("Target system library changed during the evidence run.")

    ending_fixture_fingerprints = {
        name: tree_fingerprint(path) for name, path in fixture_roots.items()
    }
    if ending_fixture_fingerprints != starting_fixture_fingerprints:
        raise SmokeError("Fixture checkout changed during the evidence run.")

    ending_git_identity = git_identity(repository_root)
    if ending_git_identity != starting_git_identity:
        raise SmokeError("Git worktree identity changed during the evidence run.")

    return {
        "status": "passed",
        "candidate_lock_unchanged": True,
        "candidate_artifacts_unchanged": True,
        "target_system_library_unchanged": True,
        "fixture_inputs_unchanged": True,
        "git_worktree_identity_unchanged": True,
        "post_snapshot_fingerprint": ending_snapshot[
            "system_library_fingerprint"
        ],
    }


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


def require_exact_observed_build(
    frame: dict[str, Any],
    *,
    label: str,
    actual_build_kind: str | None,
    build_outcome: str,
    compiled_source_units: list[str],
    changed_classes: list[str],
    deleted_classes: list[str] | None = None,
    expected_callbacks_seen: bool = True,
    expected_observer_build_finished: bool = True,
) -> None:
    if frame.get("ok") is not True:
        raise SmokeError(f"{label} failed: {frame}")
    if frame.get("actual_build_kind") != actual_build_kind:
        raise SmokeError(
            f"{label} was observed as {frame.get('actual_build_kind')}, "
            f"not {actual_build_kind}."
        )
    observation = frame.get("compilation_observation")
    if not isinstance(observation, dict) or observation.get("status") != "enabled":
        raise SmokeError(f"{label} has no enabled compilation observation.")
    if frame.get("build_outcome") != build_outcome:
        raise SmokeError(
            f"{label} had build outcome {frame.get('build_outcome')}, "
            f"not {build_outcome}."
        )
    if frame.get("project_build_returned") is not True:
        raise SmokeError(f"{label} project.build did not return normally.")
    if observation.get("callbacks_seen") is not expected_callbacks_seen:
        raise SmokeError(f"{label} compilation callback evidence is inconsistent.")
    if observation.get("build_finished") is not expected_observer_build_finished:
        raise SmokeError(f"{label} buildFinished observation is inconsistent.")
    if observation.get("compiled_source_units") != compiled_source_units:
        raise SmokeError(f"{label} compiled an unexpected source-unit set.")
    if frame.get("compiled_source_units") != compiled_source_units:
        raise SmokeError(f"{label} top-level source-unit evidence is inconsistent.")
    if frame.get("changed_classes") != changed_classes:
        raise SmokeError(f"{label} changed an unexpected class set.")
    if frame.get("deleted_classes") != (deleted_classes or []):
        raise SmokeError(f"{label} deleted an unexpected class set.")
    expected_batch = actual_build_kind == "FULL"
    expected_incremental = actual_build_kind == "INCREMENTAL"
    if observation.get("batch_seen") is not expected_batch:
        raise SmokeError(f"{label} batch observation is inconsistent.")
    if observation.get("incremental_compile_seen") is not expected_incremental:
        raise SmokeError(f"{label} incremental observation is inconsistent.")
    if int(frame.get("error_count", -1)) != 0:
        raise SmokeError(f"{label} produced ERROR markers.")


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    repository_root = root.parents[1]
    helper_source = (
        root
        / "target-system-helper"
        / "src"
        / "net"
        / "jolink"
        / "runtime"
        / "jdt"
        / "helper"
        / "TargetSystemLibraries.java"
    )
    fixture_roots = {
        "plain_java": root / "fixtures" / "plain-java" / "src",
        "java9_api_negative": root
        / "fixtures"
        / "java9-api-negative"
        / "src",
    }
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
    generation_ids = {
        "primary": f"generation-{uuid.uuid4().hex[:12]}",
        "instrumentation_off": f"generation-{uuid.uuid4().hex[:12]}",
        "clean_full_oracle": f"generation-{uuid.uuid4().hex[:12]}",
        "java9_api_negative": f"generation-{uuid.uuid4().hex[:12]}",
    }
    attempts_root = args.cache_root / "attempts"
    attempts_root.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix=f"{attempt_id}-", dir=attempts_root))
    client: WorkerClient | None = None
    shutdown_reports: dict[str, dict[str, Any]] = {}
    owned_worker_pids: list[int] = []
    started = time.monotonic()
    try:
        starting_git_identity = git_identity(repository_root)
        starting_fixture_fingerprints = {
            name: tree_fingerprint(path) for name, path in fixture_roots.items()
        }
        combined_fixture_fingerprint = canonical_json_fingerprint(
            starting_fixture_fingerprints
        )
        lock = load_lock(args.lock)
        starting_lock_fingerprint = canonical_json_fingerprint(lock)
        candidate_root = args.cache_root / "candidates" / lock["candidate_id"]
        verify_candidate(lock, candidate_root)
        candidate_identity = candidate_report_identity(lock, args.lock)
        locked_worker_java_identity = worker_java_identity(args.worker_java_home)
        expected_worker_java = lock.get("worker_build", {}).get(
            "java_home_identity", {}
        ).get("java_binary_sha256")
        if not expected_worker_java or sha256_file(
            java_tool(args.worker_java_home, "java")
        ) != expected_worker_java:
            raise SmokeError(
                "Worker Java does not match the locked Worker build identity."
            )
        snapshot = snapshot_target_system_libraries(
            target_java_home=args.target_java_home,
            attempt=attempt,
            helper_source=helper_source,
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
        owned_worker_pids.append(client.process.pid)
        ready_frame = dict(client.ready)

        project = attempt / "workspace" / "plain-fixture"
        source = project / "src"
        shutil.copytree(
            root / "fixtures" / "plain-java" / "src",
            source,
            dirs_exist_ok=True,
        )
        full = client.command("BUILD\tFULL")
        require_exact_observed_build(
            full,
            label="A1 full build",
            actual_build_kind="FULL",
            build_outcome="COMPILED",
            compiled_source_units=[
                "src/example/Api.java",
                "src/example/Application.java",
                "src/example/Service.java",
            ],
            changed_classes=[
                "example/Api.class",
                "example/Application.class",
                "example/Service.class",
            ],
        )

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
        require_exact_observed_build(
            incremental,
            label="A3 leaf incremental build",
            actual_build_kind="INCREMENTAL",
            build_outcome="COMPILED",
            compiled_source_units=["src/example/Application.java"],
            changed_classes=["example/Application.class"],
        )
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
        require_exact_observed_build(
            no_op,
            label="A2 no-op incremental build",
            actual_build_kind=None,
            build_outcome="NO_COMPILE",
            compiled_source_units=[],
            changed_classes=[],
            expected_callbacks_seen=False,
            expected_observer_build_finished=False,
        )
        if output_hashes(classes) != incremental_hashes:
            raise SmokeError("No-op build changed class outputs.")

        shutdown_reports["primary"] = client.close()
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
        owned_worker_pids.append(off_client.process.pid)
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
            shutdown_reports["instrumentation_off"] = off_client.close()

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
        owned_worker_pids.append(oracle_client.process.pid)
        try:
            oracle_source = (
                oracle_attempt / "workspace" / "plain-fixture" / "src"
            )
            shutil.copytree(source, oracle_source, dirs_exist_ok=True)
            oracle_full = oracle_client.command("BUILD\tFULL")
            require_exact_observed_build(
                oracle_full,
                label="A3 clean-full oracle",
                actual_build_kind="FULL",
                build_outcome="COMPILED",
                compiled_source_units=[
                    "src/example/Api.java",
                    "src/example/Application.java",
                    "src/example/Service.java",
                ],
                changed_classes=[
                    "example/Api.class",
                    "example/Application.class",
                    "example/Service.class",
                ],
            )
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
            shutdown_reports["clean_full_oracle"] = oracle_client.close()

        negative_attempt = attempt / "java9-api-negative"
        negative_attempt.mkdir()
        negative_client = start_worker(
            lock=lock,
            candidate_root=candidate_root,
            worker_java_home=args.worker_java_home,
            attempt=negative_attempt,
            system_libraries_file=snapshot["worker_input"],
            instrumentation="enabled",
            timeout=args.timeout,
        )
        owned_worker_pids.append(negative_client.process.pid)
        try:
            negative_source = (
                negative_attempt / "workspace" / "plain-fixture" / "src"
            )
            shutil.copytree(
                root / "fixtures" / "java9-api-negative" / "src",
                negative_source,
                dirs_exist_ok=True,
            )
            java9_api_negative = negative_client.command("BUILD\tFULL")
            negative_hashes = output_hashes(
                negative_attempt / "workspace" / "plain-fixture" / "bin"
            )
            if java9_api_negative.get("ok") is not False:
                raise SmokeError("Java 9 platform API unexpectedly compiled for Java 8.")
            if int(java9_api_negative.get("error_count", 0)) < 1:
                raise SmokeError("Java 9 API negative case produced no ERROR marker.")
            negative_observation = java9_api_negative.get(
                "compilation_observation"
            )
            if (
                java9_api_negative.get("actual_build_kind") != "FULL"
                or java9_api_negative.get("build_outcome") != "COMPILED"
                or java9_api_negative.get("project_build_returned") is not True
                or not isinstance(negative_observation, dict)
                or negative_observation.get("status") != "enabled"
                or negative_observation.get("callbacks_seen") is not True
                or negative_observation.get("batch_seen") is not True
                or negative_observation.get("build_finished") is not True
                or negative_observation.get("compiled_source_units")
                != ["src/example/Java9ApiLeak.java"]
            ):
                raise SmokeError(
                    "Java 9 API negative case has incomplete build evidence."
                )
            java9_api_negative["generated_class_count"] = len(negative_hashes)
            java9_api_negative["generation_publishable"] = False
        finally:
            shutdown_reports["java9_api_negative"] = negative_client.close()

        if set(shutdown_reports) != set(generation_ids):
            raise SmokeError("Worker shutdown evidence is incomplete.")
        if any(
            report.get("status") != "settled"
            or report.get("direct_process_exited") is not True
            or report.get("cooperative_stop_acknowledged") is not True
            or report.get("forced") is not False
            for report in shutdown_reports.values()
        ):
            raise SmokeError("A Worker did not settle through cooperative shutdown.")

        input_revalidation = revalidate_frozen_inputs(
            lock_path=args.lock,
            starting_lock=lock,
            candidate_root=candidate_root,
            target_java_home=args.target_java_home,
            attempt=attempt,
            helper_source=helper_source,
            starting_snapshot=snapshot,
            fixture_roots=fixture_roots,
            starting_fixture_fingerprints=starting_fixture_fingerprints,
            repository_root=repository_root,
            starting_git_identity=starting_git_identity,
        )
        if canonical_json_fingerprint(load_lock(args.lock)) != starting_lock_fingerprint:
            raise SmokeError("Candidate lock fingerprint changed after revalidation.")

        edited_fixture_fingerprint = tree_fingerprint(source)
        all_direct_workers_exited = all(
            report["direct_process_exited"] for report in shutdown_reports.values()
        )
        report = {
            "schema_version": 3,
            "attempt_id": attempt_id,
            "generation_id": generation_ids["primary"],
            "generation_ids": generation_ids,
            "status": "phase_1a_a1_a2_a3_evidence_passed",
            "evidence_status": "partial_phase_1a_evidence_a1_a2_a3",
            "candidate_id": lock["candidate_id"],
            "candidate_execution_environment": lock["execution_environment"],
            "provenance": {
                "git": starting_git_identity,
                "platform": {
                    "operating_system": platform.system(),
                    "release": platform.release(),
                    "architecture": platform.machine(),
                    "python_version": platform.python_version(),
                },
                "candidate": candidate_identity,
                "worker_jdk": locked_worker_java_identity,
                "compiler_configuration": {
                    "source_compliance": "1.8",
                    "class_target": "1.8",
                    "lombok": "absent",
                    "worker_jvm_arguments": ["-Xms64m", "-Xmx512m"],
                    "instrumentation_identity": lock["worker_build"][
                        "instrumentation"
                    ],
                },
                "fixture_inputs": {
                    "combined_sha256": combined_fixture_fingerprint,
                    "source_roots": starting_fixture_fingerprints,
                    "edited_primary_generation_sha256": (
                        edited_fixture_fingerprint
                    ),
                },
            },
            "worker_ready": True,
            "worker": {
                "jdt_bundle_version": ready_frame["jdt_bundle_version"],
                "ready_frame": ready_frame,
            },
            "target_system_library": {
                key: value
                for key, value in snapshot.items()
                if key != "worker_input"
            },
            "cases": {
                "full": full,
                "leaf_incremental": incremental,
                "no_op_incremental": no_op,
                "instrumentation_off_full": instrumentation_off_full,
                "clean_full_oracle": oracle_full,
                "java9_api_negative": java9_api_negative,
            },
            "output": {
                "full_class_count": len(full_hashes),
                "incremental_class_count": len(incremental_hashes),
                "class_major": 52,
                "full_class_sha256": full_hashes,
                "incremental_class_sha256": incremental_hashes,
                "clean_full_oracle_class_sha256": oracle_hashes,
                "instrumentation_off_class_sha256": off_hashes,
                "java9_api_rejected": True,
                "instrumentation_off_on_parity": instrumentation_parity,
                "incremental_equals_clean_full_oracle": oracle_equal,
            },
            "input_revalidation": input_revalidation,
            "lifecycle": {
                "shutdown": shutdown_reports,
                "owned_processes": {
                    "status": (
                        "settled" if all_direct_workers_exited else "unsettled"
                    ),
                    "direct_worker_count": len(owned_worker_pids),
                    "direct_workers_exited": all_direct_workers_exited,
                    "owned_process_tree_verification": "not_implemented_a9",
                },
                "cancellation": {
                    "status": "not_run",
                    "reason": "outside_A1_A3_scope",
                },
            },
            "measurements": {
                "timing": {
                    "status": "partial",
                    "case_elapsed_ms_are_in_case_frames": True,
                    "attempt_elapsed_ms": round(
                        (time.monotonic() - started) * 1000, 1
                    ),
                },
                "resource_usage": {
                    "status": "unavailable",
                    "reason": "A9_resource_measurement_not_run",
                },
                "resource_delta": {
                    "status": "unavailable",
                    "reason": "resource_delta_instrumentation_not_implemented",
                },
                "workspace_restart": {
                    "status": "not_run",
                    "reason": "A8_not_run",
                },
            },
            "phase_1a_case_status": {
                "A1": "passed",
                "A2": "passed",
                "A3": "passed",
                "A4_A10": "not_run",
            },
            "limitations": [
                "only A1/A2/A3 ran; A4 through A10 are not implemented",
                "this partial evidence does not satisfy the complete Phase 1A Go gate",
                "resource delta observation is unavailable until its instrumentation is implemented",
                "owned process-tree verification remains an A9 lifecycle item",
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
                    "evidence_status": report["evidence_status"],
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
