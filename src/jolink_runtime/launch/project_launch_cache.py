"""Local JSON cache for Probe-derived single-module launch facts."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from .contracts import JvmLaunchPlan, LaunchIntent
from .fast_compile import fast_compile_fingerprint
from .jdt_compile_session import (
    JdtBuildWorldPlan,
    resource_tree_fingerprint,
)
from .jdt_workspace_store import jolink_cache_root
from .toolchain import JavaToolchainCandidate


_SCHEMA = "jolink.project-launch-cache.v1"


def _path(value: Any) -> Path:
    return Path(str(value)).expanduser().resolve(strict=False)


def _paths(values: Any) -> tuple[Path, ...]:
    return tuple(_path(value) for value in (values or ()))


def _intent_payload(intent: LaunchIntent) -> dict[str, Any]:
    return {
        "source": intent.source,
        "launch_name": intent.launch_name,
        "launch_type": intent.launch_type,
        "main_class": intent.main_class,
        "working_directory": str(intent.working_directory),
        "ide_module_name": intent.ide_module_name,
        "jvm_args": list(intent.jvm_args),
        "program_args": list(intent.program_args),
        "environment": dict(intent.environment),
        "build_before_run": intent.build_before_run,
        "runtime_jdk_reference": intent.runtime_jdk_reference,
    }


def _toolchain_payload(value: JavaToolchainCandidate) -> dict[str, Any]:
    return {
        "home": str(value.home),
        "java_executable": str(value.java_executable),
        "javac_executable": str(value.javac_executable),
        "source": value.source,
        "detected_major_version": value.detected_major_version,
        "detected_compiler_major_version": (
            value.detected_compiler_major_version
        ),
    }


def _toolchain(value: Mapping[str, Any]) -> JavaToolchainCandidate:
    return JavaToolchainCandidate(
        home=_path(value["home"]),
        java_executable=_path(value["java_executable"]),
        javac_executable=_path(value["javac_executable"]),
        source=str(value.get("source", "launch_cache")),
        detected_major_version=value.get("detected_major_version"),
        detected_compiler_major_version=value.get(
            "detected_compiler_major_version"
        ),
    )


def stabilize_jdt_plan(
    plan: JdtBuildWorldPlan,
    *,
    attempt_directory: Path,
) -> JdtBuildWorldPlan:
    attempt = attempt_directory.resolve(strict=False)
    configuration = tuple(
        path
        for path in plan.configuration_inputs
        if not path.resolve(strict=False).is_relative_to(attempt)
    )
    environment_names = tuple(
        sorted(set((*plan.configuration_environment_names, "JAVA_HOME")))
    )
    freshness = plan.freshness_entries or plan.dependency_entries
    fingerprint = fast_compile_fingerprint(
        configuration_inputs=configuration,
        configuration_environment_names=environment_names,
        javac_executable=plan.javac_executable,
        compile_classpath=freshness,
    )
    return replace(
        plan,
        fingerprint=fingerprint,
        configuration_inputs=configuration,
        configuration_environment_names=environment_names,
    )


@dataclass(frozen=True)
class CachedProjectLaunch:
    build_system: str
    build_offline: bool
    build_jdk: JavaToolchainCandidate
    runtime_jdk: JavaToolchainCandidate
    module_output: Path
    generation_input_roots: tuple[Path, ...]
    resource_source_roots: tuple[Path, ...]
    jvm_plan: JvmLaunchPlan
    jdt_plan: JdtBuildWorldPlan


class ProjectLaunchCache:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (
            root.expanduser().resolve(strict=False)
            if root is not None
            else jolink_cache_root() / "project-launch"
        )

    def _file(self, project_root: Path, launch_name: str) -> Path:
        key = hashlib.sha256(
            (
                os.path.normcase(str(project_root.resolve(strict=False)))
                + "\0"
                + launch_name
            ).encode("utf-8", errors="surrogateescape")
        ).hexdigest()[:24]
        return self.root / key / "build-world.json"

    def load(
        self,
        *,
        project_root: Path,
        intent: LaunchIntent,
        build_system: str,
        ready_port: int,
        startup_wait_timeout_seconds: float,
        build_preferences: Mapping[str, Any] | None = None,
    ) -> CachedProjectLaunch | None:
        path = self._file(project_root, intent.launch_name)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if (
                raw.get("schema") != _SCHEMA
                or raw.get("intent") != _intent_payload(intent)
                or raw.get("build_system") != build_system
                or raw.get("build_preferences", {})
                != dict(build_preferences or {})
            ):
                return None
            plan_raw = raw["jdt_plan"]
            resource_roots = _paths(plan_raw.get("resource_roots"))
            jdt_plan = JdtBuildWorldPlan(
                project_root=_path(plan_raw["project_root"]),
                module_root=_path(plan_raw["module_root"]),
                source_roots=_paths(plan_raw["source_roots"]),
                dependency_entries=_paths(plan_raw["dependency_entries"]),
                processor_entries=_paths(plan_raw["processor_entries"]),
                lombok_entries=_paths(plan_raw["lombok_entries"]),
                target_java_home=_path(plan_raw["target_java_home"]),
                source_encoding=str(plan_raw["source_encoding"]),
                source_level=int(plan_raw["source_level"]),
                target_level=int(plan_raw["target_level"]),
                fingerprint=str(plan_raw["fingerprint"]),
                configuration_inputs=_paths(
                    plan_raw["configuration_inputs"]
                ),
                configuration_environment_names=tuple(
                    str(value)
                    for value in plan_raw[
                        "configuration_environment_names"
                    ]
                ),
                javac_executable=_path(plan_raw["javac_executable"]),
                method_parameters=bool(plan_raw["method_parameters"]),
                freshness_entries=_paths(plan_raw["freshness_entries"]),
                resource_roots=resource_roots,
                resource_fingerprint=resource_tree_fingerprint(
                    resource_roots
                ),
                worker_min_heap_mb=int(plan_raw["worker_min_heap_mb"]),
                worker_max_heap_mb=int(plan_raw["worker_max_heap_mb"]),
            )
            if not jdt_plan.is_fresh():
                return None
            jvm_raw = raw["jvm_plan"]
            runtime_jdk = _toolchain(raw["runtime_jdk"])
            build_jdk = _toolchain(raw["build_jdk"])
            if not runtime_jdk.java_executable.is_file():
                return None
            jvm_plan = JvmLaunchPlan(
                java_executable=runtime_jdk.java_executable,
                classpath=_paths(jvm_raw["classpath"]),
                main_class=str(jvm_raw["main_class"]),
                working_directory=_path(jvm_raw["working_directory"]),
                jvm_args=tuple(str(value) for value in jvm_raw["jvm_args"]),
                program_args=tuple(
                    str(value) for value in jvm_raw["program_args"]
                ),
                environment_overrides={
                    str(key): str(value)
                    for key, value in jvm_raw["environment_overrides"].items()
                },
                ready_port=ready_port,
                startup_wait_timeout_seconds=startup_wait_timeout_seconds,
            )
            return CachedProjectLaunch(
                build_system=build_system,
                build_offline=bool(raw.get("build_offline", False)),
                build_jdk=build_jdk,
                runtime_jdk=runtime_jdk,
                module_output=_path(raw["module_output"]),
                generation_input_roots=_paths(raw["generation_input_roots"]),
                resource_source_roots=_paths(raw["resource_source_roots"]),
                jvm_plan=jvm_plan,
                jdt_plan=jdt_plan,
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def save(
        self,
        *,
        project_root: Path,
        intent: LaunchIntent,
        build_system: str,
        build_offline: bool,
        build_jdk: JavaToolchainCandidate,
        runtime_jdk: JavaToolchainCandidate,
        module_output: Path,
        generation_input_roots: tuple[Path, ...],
        resource_source_roots: tuple[Path, ...],
        jvm_plan: JvmLaunchPlan,
        jdt_plan: JdtBuildWorldPlan,
        build_preferences: Mapping[str, Any] | None = None,
    ) -> None:
        target = self._file(project_root, intent.launch_name)
        value = {
            "schema": _SCHEMA,
            "intent": _intent_payload(intent),
            "build_system": build_system,
            "build_offline": build_offline,
            "build_preferences": dict(build_preferences or {}),
            "build_jdk": _toolchain_payload(build_jdk),
            "runtime_jdk": _toolchain_payload(runtime_jdk),
            "module_output": str(module_output),
            "generation_input_roots": [
                str(path) for path in generation_input_roots
            ],
            "resource_source_roots": [
                str(path) for path in resource_source_roots
            ],
            "jvm_plan": {
                "classpath": [str(path) for path in jvm_plan.classpath],
                "main_class": jvm_plan.main_class,
                "working_directory": str(jvm_plan.working_directory),
                "jvm_args": list(jvm_plan.jvm_args),
                "program_args": list(jvm_plan.program_args),
                "environment_overrides": dict(
                    jvm_plan.environment_overrides
                ),
            },
            "jdt_plan": {
                "project_root": str(jdt_plan.project_root),
                "module_root": str(jdt_plan.module_root),
                "source_roots": [str(path) for path in jdt_plan.source_roots],
                "dependency_entries": [
                    str(path) for path in jdt_plan.dependency_entries
                ],
                "processor_entries": [
                    str(path) for path in jdt_plan.processor_entries
                ],
                "lombok_entries": [
                    str(path) for path in jdt_plan.lombok_entries
                ],
                "target_java_home": str(jdt_plan.target_java_home),
                "source_encoding": jdt_plan.source_encoding,
                "source_level": jdt_plan.source_level,
                "target_level": jdt_plan.target_level,
                "fingerprint": jdt_plan.fingerprint,
                "configuration_inputs": [
                    str(path) for path in jdt_plan.configuration_inputs
                ],
                "configuration_environment_names": list(
                    jdt_plan.configuration_environment_names
                ),
                "javac_executable": str(jdt_plan.javac_executable),
                "method_parameters": jdt_plan.method_parameters,
                "freshness_entries": [
                    str(path) for path in jdt_plan.freshness_entries
                ],
                "resource_roots": [
                    str(path) for path in jdt_plan.resource_roots
                ],
                "worker_min_heap_mb": jdt_plan.worker_min_heap_mb,
                "worker_max_heap_mb": jdt_plan.worker_max_heap_mb,
            },
        }
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=True, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


__all__ = [
    "CachedProjectLaunch",
    "ProjectLaunchCache",
    "stabilize_jdt_plan",
]
