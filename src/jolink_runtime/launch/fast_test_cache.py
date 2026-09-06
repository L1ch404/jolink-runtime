"""Small local cache for Probe-exported Fast Test Build Worlds."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid
import xml.etree.ElementTree as ET

from .jdt_workspace_store import JdtWorkspaceStore, jolink_cache_root
from .test_build_world import JavaTestBuildWorld
from .toolchain import JavaToolchainCandidate
from .gradle_probe import environment_input_stamps as _environment_inputs


def _paths(values) -> tuple[Path, ...]:
    return tuple(Path(value) for value in values)


def _input_files(project: Path, build_system: str) -> tuple[Path, ...]:
    if build_system == "gradle":
        names = (
            "build.gradle", "build.gradle.kts", "settings.gradle",
            "settings.gradle.kts", "gradle.properties",
            "gradle/wrapper/gradle-wrapper.properties",
        )
        return tuple(project / name for name in names if (project / name).is_file())
    result: list[Path] = []
    pending = [project / "pom.xml"]
    seen: set[Path] = set()
    while pending:
        pom = pending.pop().resolve(strict=False)
        if not pom.is_file() or pom in seen:
            continue
        seen.add(pom)
        result.append(pom)
        try:
            root = ET.parse(pom).getroot()
            pending.extend(pom.parent / str(item.text).strip() / "pom.xml"
                for item in root.findall("./{*}modules/{*}module") if item.text)
            parent = root.find("{*}parent")
            if parent is not None:
                relative = parent.find("{*}relativePath")
                if relative is None or (relative.text or "").strip():
                    pending.append(pom.parent / ((relative.text or "").strip() if relative is not None else "../pom.xml"))
        except (ET.ParseError, OSError):
            continue
    for name in (".mvn/maven.config", ".mvn/jvm.config"):
        path = project / name
        if path.is_file():
            result.append(path)
    return tuple(result)


def _inputs(project: Path, build_system: str, additional=()) -> dict[str, str]:
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
        for path in dict.fromkeys((*_input_files(project, build_system), *(Path(p) for p in additional)))
    }


class FastTestCache:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or jolink_cache_root() / "fast-test"

    def _directory(self, project: Path, build_system: str) -> Path:
        key = hashlib.sha256(
            (os.path.normcase(str(project.resolve(strict=False))) + "\0" + build_system)
            .encode("utf-8", errors="surrogateescape")
        ).hexdigest()[:24]
        return self.root / key

    def workspace_store(self, project: Path, build_system: str) -> JdtWorkspaceStore:
        return JdtWorkspaceStore(self._directory(project, build_system) / "workspace")

    def is_current(self, project: Path, build_system: str) -> bool:
        path = self._directory(project, build_system) / "build-world.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if build_system == "gradle" and raw.get("environment_inputs") != _environment_inputs(raw.get("environment_inputs", ())):
                return False
            return raw.get("inputs") == _inputs(project, build_system, raw.get("configuration_files", ()))
        except (OSError, json.JSONDecodeError):
            return False

    def load(self, project: Path, build_system: str):
        path = self._directory(project, build_system) / "build-world.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("schema") != "jolink.fast-test-world.v1":
                return None
            if build_system == "gradle" and raw.get("environment_inputs") != _environment_inputs(raw.get("environment_inputs", ())):
                return None
            if raw.get("inputs") != _inputs(project, build_system, raw.get("configuration_files", ())):
                return None
            world = raw["world"]
            toolchain = raw["build_jdk"]
            return (
                JavaTestBuildWorld(
                    build_system=build_system,
                    project_root=Path(world["project_root"]),
                    module_root=Path(world["module_root"]),
                    main_source_roots=_paths(world["main_source_roots"]),
                    test_source_roots=_paths(world["test_source_roots"]),
                    main_output=Path(world["main_output"]),
                    test_output=Path(world["test_output"]),
                    main_dependencies=_paths(world["main_dependencies"]),
                    test_dependencies=_paths(world["test_dependencies"]),
                    test_runtime_classpath=_paths(world["test_runtime_classpath"]),
                    resource_roots=_paths(world["resource_roots"]),
                    target_java_home=Path(world["target_java_home"]),
                    source_encoding=world["source_encoding"],
                    source_level=int(world["source_level"]),
                    method_parameters=bool(world["method_parameters"]),
                    processor_entries=_paths(world["processor_entries"]),
                    java_agents=tuple(world["java_agents"]),
                    extra_worker_jvm_arguments=tuple(world["extra_worker_jvm_arguments"]),
                    test_java_executable=Path(world["test_java_executable"]),
                    test_framework=world.get("test_framework"),
                    test_working_directory=Path(world["test_working_directory"]),
                    test_classes_directories=_paths(world["test_classes_directories"]),
                    runner_environment=dict(world["runner_environment"]),
                    javac_executable=Path(world["javac_executable"]),
                    configuration_inputs=_paths(raw.get("configuration_files", ())),
                    configuration_environment_names=tuple(dict.fromkeys((*raw.get("environment_inputs", ()), *world["runner_environment"]))),
                    upstream_source_roots=_paths(world.get("upstream_source_roots", ())),
                    worker_min_heap_mb=int(world["worker_min_heap_mb"]),
                    worker_max_heap_mb=int(world["worker_max_heap_mb"]),
                    test_jvm_arguments=tuple(world["test_jvm_arguments"]),
                    runner_support_provenance=dict(world["runner_support_provenance"]),
                    modules=tuple(world.get("modules", ())),
                ),
                JavaToolchainCandidate(
                    home=Path(toolchain["home"]),
                    java_executable=Path(toolchain["java_executable"]),
                    javac_executable=Path(toolchain["javac_executable"]),
                    source=toolchain["source"],
                    detected_major_version=toolchain.get("major_version"),
                    detected_compiler_major_version=toolchain.get("compiler_major_version"),
                ),
            )
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            return None

    def save(self, world: JavaTestBuildWorld, build_jdk: JavaToolchainCandidate) -> None:
        def values(paths): return [str(path) for path in paths]
        payload = {
            "schema": "jolink.fast-test-world.v1",
            "inputs": _inputs(world.project_root, world.build_system, world.configuration_inputs if world.build_system == "gradle" else ()),
            "configuration_files": values(world.configuration_inputs) if world.build_system == "gradle" else [],
            "environment_inputs": _environment_inputs(world.configuration_environment_names) if world.build_system == "gradle" else {},
            "build_jdk": {
                "home": str(build_jdk.home), "java_executable": str(build_jdk.java_executable),
                "javac_executable": str(build_jdk.javac_executable), "source": build_jdk.source,
                "major_version": build_jdk.major_version,
                "compiler_major_version": build_jdk.compiler_major_version,
            },
            "world": {
                "project_root": str(world.project_root), "module_root": str(world.module_root),
                "main_source_roots": values(world.main_source_roots),
                "test_source_roots": values(world.test_source_roots),
                "main_output": str(world.main_output), "test_output": str(world.test_output),
                "main_dependencies": values(world.main_dependencies),
                "test_dependencies": values(world.test_dependencies),
                "test_runtime_classpath": values(world.test_runtime_classpath),
                "resource_roots": values(world.resource_roots),
                "target_java_home": str(world.target_java_home),
                "source_encoding": world.source_encoding, "source_level": world.source_level,
                "method_parameters": world.method_parameters,
                "processor_entries": values(world.processor_entries), "java_agents": list(world.java_agents),
                "extra_worker_jvm_arguments": list(world.extra_worker_jvm_arguments),
                "test_java_executable": str(world.test_java_executable),
                "test_framework": world.test_framework,
                "test_working_directory": str(world.test_working_directory),
                "test_classes_directories": values(world.test_classes_directories),
                "runner_environment": {
                    name: world.runner_environment[name]
                    for name in world.configuration_environment_names
                    if name in world.runner_environment
                },
                "javac_executable": str(world.javac_executable),
                "upstream_source_roots": values(world.upstream_source_roots),
                "worker_min_heap_mb": world.worker_min_heap_mb,
                "worker_max_heap_mb": world.worker_max_heap_mb,
                "test_jvm_arguments": list(world.test_jvm_arguments),
                "runner_support_provenance": world.runner_support_provenance,
                "modules": world.modules,
            },
        }
        path = self._directory(world.project_root, world.build_system) / "build-world.json"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(path)


__all__ = ["FastTestCache"]
