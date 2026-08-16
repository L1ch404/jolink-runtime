#!/usr/bin/env python3
"""Run the private Maven-native Build World Probe spike."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from jolink_runtime.experiments.jdt_build_world import (
    BuildWorldError,
    canonical_fingerprint,
    sha256_file,
)
from jolink_runtime.experiments.maven_probe import (
    PROBE_ARTIFACT_ID,
    PROBE_VERSION,
    create_probe_settings,
    resolve_source_settings,
    stage_probe_in_local_repository,
    stage_probe_repository,
)


_PROBE_ID_RESOURCE = "META-INF/jolink/probe-implementation-id.txt"
_MAVEN_VERSION_PATTERN = re.compile(r"^Apache Maven\s+([^\s]+)", re.MULTILINE)
_JAVA_VERSION_PATTERN = re.compile(
    r"^Java version:\s*([^,\r\n]+)(?:,\s*vendor:\s*([^,\r\n]+))?",
    re.MULTILINE,
)
_JAVA_HOME_PATTERN = re.compile(
    r"^(?:Java home|Java version:[^\r\n]*runtime):\s*(.+)$",
    re.MULTILINE,
)


def _run(
    command: list[str],
    *,
    cwd: Path,
    log: Path,
    timeout: float,
    environment: dict[str, str],
    append_log: bool = False,
) -> float:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=environment,
    )
    with log.open("a" if append_log else "w", encoding="utf-8") as stream:
        if append_log:
            stream.write("\n--- joLink Maven Probe build step ---\n")
        stream.write(completed.stdout)
    if completed.returncode != 0:
        raise BuildWorldError(
            "MAVEN_PROBE_EXECUTION_FAILED",
            "The Maven-native Build World Probe operation failed.",
            suggested_next_step="Inspect the private Maven Probe log.",
            retryable=True,
            context={"return_code": completed.returncode},
        )
    return time.monotonic() - started


def _java_environment(java_home: Path | None) -> dict[str, str]:
    environment = {
        **os.environ,
        "MAVEN_OPTS": os.environ.get("MAVEN_OPTS", ""),
    }
    if java_home is None:
        return environment
    home = java_home.expanduser().resolve(strict=True)
    executable = home / "bin" / ("java.exe" if os.name == "nt" else "java")
    if not executable.is_file():
        raise BuildWorldError(
            "JAVA_TOOLCHAIN_UNAVAILABLE",
            "The Maven Probe Java home contains no Java executable.",
            suggested_next_step="Provide the JDK used by the target Maven build.",
            retryable=True,
        )
    environment["JAVA_HOME"] = str(home)
    environment["PATH"] = str(home / "bin") + os.pathsep + environment.get(
        "PATH", ""
    )
    return environment


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _capture_maven_identity(
    maven: Path,
    *,
    cwd: Path,
    timeout: float,
    environment: dict[str, str],
    private_log: Path,
) -> dict[str, object]:
    completed = subprocess.run(
        [str(maven), "--version"],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=min(timeout, 30.0),
        check=False,
        env=environment,
    )
    output = completed.stdout
    private_log.write_text(output, encoding="utf-8")
    maven_match = _MAVEN_VERSION_PATTERN.search(output)
    java_match = _JAVA_VERSION_PATTERN.search(output)
    java_home_match = _JAVA_HOME_PATTERN.search(output)
    if completed.returncode != 0 or maven_match is None or java_match is None:
        raise BuildWorldError(
            "MAVEN_IDENTITY_UNAVAILABLE",
            "The Maven Probe could not verify its Maven and host Java identity.",
            suggested_next_step="Verify the selected Maven/JDK and retry.",
            retryable=True,
        )
    java_home_value = (
        java_home_match.group(1).strip()
        if java_home_match is not None
        else environment.get("JAVA_HOME", "")
    )
    return {
        "maven_version": maven_match.group(1),
        "maven_executable_sha256": sha256_file(maven),
        "version_output_sha256": _hash_text(output),
        "host_java_version": java_match.group(1).strip(),
        "host_java_vendor": (java_match.group(2) or "unknown").strip(),
        "host_java_home_identity_sha256": _hash_text(java_home_value),
        "private": {
            "maven_executable": str(maven),
            "host_java_home": java_home_value,
        },
    }


def _probe_implementation_id(project: Path) -> str:
    inputs = [
        path
        for path in sorted(project.rglob("*"))
        if path.is_file() and "target" not in path.relative_to(project).parts
    ]
    return canonical_fingerprint(
        [(path.relative_to(project).as_posix(), sha256_file(path)) for path in inputs]
    )


def _maven_base(
    executable: Path,
    *,
    root_pom: Path,
    settings: Path | None,
    local_repository: Path | None,
    profiles: tuple[str, ...],
    offline: bool,
) -> list[str]:
    command = [
        str(executable),
        "--batch-mode",
        "--fail-fast",
        "-T",
        "1",
        "-Dstyle.color=never",
        "-f",
        str(root_pom),
    ]
    if settings is not None:
        command.extend(["-s", str(settings)])
    if local_repository is not None:
        command.append(f"-Dmaven.repo.local={local_repository}")
    if profiles:
        command.extend(["-P", ",".join(profiles)])
    if offline:
        command.append("--offline")
    return command


def _build_probe(
    *,
    maven: Path,
    project: Path,
    settings: Path | None,
    local_repository: Path | None,
    timeout: float,
    log: Path,
    environment: dict[str, str],
) -> tuple[Path, Path, float, str]:
    shutil.rmtree(project / "target", ignore_errors=True)
    implementation_id = _probe_implementation_id(project)
    command = _maven_base(
        maven,
        root_pom=project / "pom.xml",
        settings=settings,
        local_repository=local_repository,
        profiles=(),
        offline=False,
    )
    # The Probe has no resources or tests. Invoke exact build goals rather than
    # instead of a Maven lifecycle so old Maven releases do not pull their
    # Super-POM resource/Surefire defaults into the experimental build.
    command.extend(
        [
            "org.apache.maven.plugins:maven-compiler-plugin:3.11.0:compile",
            "org.apache.maven.plugins:maven-plugin-plugin:3.11.0:descriptor",
        ]
    )
    elapsed = _run(
        command,
        cwd=project,
        log=log,
        timeout=timeout,
        environment=environment,
    )
    identity_resource = project / "target" / "classes" / _PROBE_ID_RESOURCE
    identity_resource.parent.mkdir(parents=True, exist_ok=True)
    identity_resource.write_text(implementation_id + "\n", encoding="ascii")
    jar_command = _maven_base(
        maven,
        root_pom=project / "pom.xml",
        settings=settings,
        local_repository=local_repository,
        profiles=(),
        offline=False,
    )
    jar_command.append("org.apache.maven.plugins:maven-jar-plugin:3.3.0:jar")
    elapsed += _run(
        jar_command,
        cwd=project,
        log=log,
        timeout=timeout,
        environment=environment,
        append_log=True,
    )
    jar = project / "target" / f"{PROBE_ARTIFACT_ID}-{PROBE_VERSION}.jar"
    if not jar.is_file():
        raise BuildWorldError(
            "MAVEN_PROBE_ARTIFACT_UNAVAILABLE",
            "The Maven Probe build produced no plugin artifact.",
            suggested_next_step="Inspect the private Maven Probe build log.",
        )
    return jar, project / "pom.xml", elapsed, implementation_id


def _pom_tree_fingerprint(project: Path) -> str:
    return canonical_fingerprint(
        [
            (item.relative_to(project).as_posix(), sha256_file(item))
            for item in sorted(project.rglob("pom.xml"))
        ]
    )


def _load_snapshots(
    directory: Path, *, expected_probe_implementation_id: str
) -> list[dict[str, object]]:
    snapshots: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise BuildWorldError(
                "MAVEN_PROBE_OUTPUT_INVALID",
                "A Maven Probe output is unavailable or malformed.",
                suggested_next_step="Inspect the private Maven Probe output.",
            ) from error
        if payload.get("schema") != "jolink.maven-build-world-probe.v1":
            raise BuildWorldError(
                "MAVEN_PROBE_OUTPUT_INVALID",
                "A Maven Probe output uses an unexpected schema.",
                suggested_next_step="Rebuild the bundled Maven Probe.",
            )
        if payload.get("probeImplementationId") != expected_probe_implementation_id:
            raise BuildWorldError(
                "MAVEN_PROBE_IDENTITY_MISMATCH",
                "Maven executed a different Probe implementation than joLink staged.",
                suggested_next_step=(
                    "Remove the stale io.jolink Probe coordinate from the selected "
                    "Maven local repository and retry."
                ),
            )
        snapshots.append(payload)
    if not snapshots:
        raise BuildWorldError(
            "MAVEN_PROBE_OUTPUT_UNAVAILABLE",
            "Maven produced no Build World Probe snapshots.",
            suggested_next_step="Inspect the private Maven Probe log.",
        )
    return snapshots


def _summary(
    snapshots: list[dict[str, object]], *, project_unchanged: bool
) -> dict[str, object]:
    reactor_references = 0
    source_roots = 0
    classpath_entries = 0
    for snapshot in snapshots:
        roots = snapshot.get("compileSourceRoots", [])
        classpath = snapshot.get("compileClasspathElements", [])
        if not isinstance(roots, list) or not isinstance(classpath, list):
            raise BuildWorldError(
                "MAVEN_PROBE_OUTPUT_INVALID",
                "Maven Probe source/classpath facts have an invalid shape.",
                suggested_next_step="Rebuild the bundled Maven Probe.",
            )
        source_roots += len(roots)
        classpath_entries += len(classpath)
        own_output = str(snapshot.get("outputDirectory") or "")
        reactor_outputs = {
            str(item.get("outputDirectory"))
            for item in snapshot.get("reactorProjects", [])
            if isinstance(item, dict)
            and item.get("outputDirectory")
            and str(item.get("outputDirectory")) != own_output
        }
        reactor_references += sum(
            str(item) in reactor_outputs for item in classpath
        )
    return {
        "snapshot_count": len(snapshots),
        "compile_source_root_count": source_roots,
        "compile_classpath_entry_count": classpath_entries,
        "reactor_output_classpath_reference_count": reactor_references,
        "project_poms_unchanged": project_unchanged,
        "probe_schema_verified": True,
    }


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--maven-executable", type=Path, required=True)
    parser.add_argument("--settings-file", type=Path)
    parser.add_argument("--local-repository", type=Path)
    parser.add_argument(
        "--java-home",
        type=Path,
        help="JDK used for both Maven identity capture and target Probe execution.",
    )
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument(
        "--probe-project", type=Path, default=root / "maven-probe"
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path.home() / ".cache" / "jolink-runtime" / "maven-probe-spike",
    )
    parser.add_argument("--keep-attempt", action="store_true")
    args = parser.parse_args(argv)

    attempt_id = f"maven-probe-{uuid.uuid4().hex[:12]}"
    attempts = args.cache_root / "attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix=f"{attempt_id}-", dir=attempts))
    started = time.monotonic()
    ephemeral_settings: Path | None = None
    try:
        project = args.project_root.expanduser().resolve(strict=True)
        maven = args.maven_executable.expanduser().resolve(strict=True)
        probe_project = args.probe_project.expanduser().resolve(strict=True)
        source_settings, source_settings_kind = resolve_source_settings(
            args.settings_file
        )
        local_repository = (
            args.local_repository.expanduser().resolve(strict=False)
            if args.local_repository is not None
            else None
        )
        environment = _java_environment(args.java_home)
        maven_identity = _capture_maven_identity(
            maven,
            cwd=project,
            timeout=args.timeout,
            environment=environment,
            private_log=attempt / "maven-version.private.log",
        )
        before = _pom_tree_fingerprint(project)
        jar, pom, probe_build_seconds, probe_implementation_id = _build_probe(
            maven=maven,
            project=probe_project,
            # The release artifact will be bundled. During the source-tree
            # experiment, use the selected Maven user settings so a company
            # mirror can resolve the Probe's pinned build plugins. Target
            # project profiles are still not forwarded to the Probe project.
            settings=source_settings,
            local_repository=local_repository,
            timeout=args.timeout,
            log=attempt / "probe-build.log",
            environment=environment,
        )
        repository = stage_probe_repository(
            probe_jar=jar,
            probe_pom=pom,
            repository_root=args.cache_root / "probe-repository",
        )
        settings_facts = create_probe_settings(
            source_settings=source_settings,
            destination=attempt / "settings.private.xml",
            repository=repository,
        )
        ephemeral_settings = Path(settings_facts["settings_path"])
        offline_seed: dict[str, object] | None = None
        if args.offline:
            if local_repository is None:
                raise BuildWorldError(
                    "MAVEN_PROBE_OFFLINE_LOCAL_REPOSITORY_REQUIRED",
                    "Strict offline Probe execution requires an explicit "
                    "Maven local repository.",
                    suggested_next_step=(
                        "Retry with --local-repository set to the repository "
                        "used by the target Maven build."
                    ),
                    retryable=True,
                )
            offline_seed = stage_probe_in_local_repository(
                probe_jar=jar,
                probe_pom=pom,
                local_repository=local_repository,
            )
        output = attempt / "build-world"
        output.mkdir()
        command = _maven_base(
            maven,
            root_pom=project / "pom.xml",
            settings=Path(settings_facts["settings_path"]),
            local_repository=local_repository,
            profiles=tuple(args.profile),
            offline=args.offline,
        )
        command.extend(
            [
                f"-Djolink.probe.outputDirectory={output}",
                "compile",
                repository.goal,
            ]
        )
        try:
            probe_seconds = _run(
                command,
                cwd=project,
                log=attempt / "maven-probe.log",
                timeout=args.timeout,
                environment=environment,
            )
        finally:
            try:
                ephemeral_settings.unlink(missing_ok=True)
            except OSError as error:
                raise BuildWorldError(
                    "MAVEN_PROBE_SETTINGS_CLEANUP_FAILED",
                    "The credential-bearing temporary Maven settings could not "
                    "be removed.",
                    suggested_next_step=(
                        "Delete the private Probe attempt directory before retrying."
                    ),
                ) from error
        if ephemeral_settings.exists():
            raise BuildWorldError(
                "MAVEN_PROBE_SETTINGS_CLEANUP_FAILED",
                "The temporary Maven settings still exists after execution.",
                suggested_next_step="Delete the private Probe attempt directory.",
            )
        snapshots = _load_snapshots(
            output,
            expected_probe_implementation_id=probe_implementation_id,
        )
        after = _pom_tree_fingerprint(project)
        summary = _summary(snapshots, project_unchanged=before == after)
        if not summary["project_poms_unchanged"]:
            raise BuildWorldError(
                "MAVEN_PROBE_MUTATED_PROJECT",
                "The Maven Probe changed a project POM.",
                suggested_next_step="Discard the attempt and inspect the Probe.",
            )
        private_report = {
            "schema": "jolink.maven-probe-spike.private.v1",
            "attempt_id": attempt_id,
            "project_root": str(project),
            "project_pom_fingerprint": before,
            "probe_implementation_id": probe_implementation_id,
            "probe_repository": str(repository.root),
            "invocation": {
                "maven_executable": str(maven),
                "local_repository": (
                    str(local_repository) if local_repository is not None else None
                ),
                "profiles": list(args.profile),
                "offline": args.offline,
            },
            "settings": {
                "source_settings_kind": source_settings_kind,
                "source_settings_sha256": settings_facts[
                    "source_settings_sha256"
                ],
                "probe_repository_id": settings_facts[
                    "probe_repository_id"
                ],
                "wildcard_mirror_adjustment_count": settings_facts[
                    "wildcard_mirror_adjustment_count"
                ],
                "ephemeral_settings_removed": True,
            },
            "offline_probe_seed": {
                key: str(value) if isinstance(value, Path) else value
                for key, value in (offline_seed or {}).items()
            },
            "snapshots": snapshots,
            "maven_identity": maven_identity,
        }
        private_path = attempt / "report.private.json"
        private_path.write_text(
            json.dumps(private_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            private_path.chmod(0o600)
        except OSError:
            pass
        report = {
            "schema": "jolink.maven-probe-spike.v1",
            "status": "maven_native_build_world_exported",
            "attempt_id": attempt_id,
            "probe_artifact_sha256": repository.jar_sha256,
            "probe_pom_sha256": repository.pom_sha256,
            "probe_implementation_id": probe_implementation_id,
            "project_pom_fingerprint": before,
            "maven_version": maven_identity["maven_version"],
            "maven_executable_sha256": maven_identity[
                "maven_executable_sha256"
            ],
            "maven_version_output_sha256": maven_identity[
                "version_output_sha256"
            ],
            "host_java_version": maven_identity["host_java_version"],
            "host_java_vendor": maven_identity["host_java_vendor"],
            "host_java_home_identity_sha256": maven_identity[
                "host_java_home_identity_sha256"
            ],
            "source_settings_kind": source_settings_kind,
            "ephemeral_settings_retained": False,
            "wildcard_mirror_adjustment_count": settings_facts[
                "wildcard_mirror_adjustment_count"
            ],
            "offline_requested": args.offline,
            "offline_probe_seeded": offline_seed is not None,
            "probe_build_ms": round(probe_build_seconds * 1000, 1),
            "maven_probe_ms": round(probe_seconds * 1000, 1),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            **summary,
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
                    "report_path": str(report_path),
                    "private_report_path": str(private_path),
                    "probe_implementation_id": probe_implementation_id,
                    "maven_version": maven_identity["maven_version"],
                    "host_java_version": maven_identity["host_java_version"],
                    "source_settings_kind": source_settings_kind,
                    "ephemeral_settings_retained": False,
                    **summary,
                }
            )
        )
        return 0
    except (
        BuildWorldError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        payload = (
            error.as_dict()
            if isinstance(error, BuildWorldError)
            else {
                "ok": False,
                "error_code": "MAVEN_PROBE_SPIKE_FAILED",
                "error": "The Maven Probe spike could not complete.",
                "error_type": type(error).__name__,
                "retryable": True,
                "suggested_next_step": "Inspect the private Maven Probe log.",
            }
        )
        print(json.dumps(payload), file=os.sys.stderr)
        return 1
    finally:
        if ephemeral_settings is not None and ephemeral_settings.exists():
            try:
                ephemeral_settings.unlink()
            except OSError:
                # A credential copy must not be retained merely to preserve
                # debugging evidence. Best-effort remove the whole attempt.
                shutil.rmtree(attempt, ignore_errors=True)
        if not args.keep_attempt:
            shutil.rmtree(attempt, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
