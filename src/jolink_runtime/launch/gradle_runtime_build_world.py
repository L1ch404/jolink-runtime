"""Convert Maven-native Gradle Probe facts into one Runtime Build World."""

from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .fast_compile import fast_compile_fingerprint
from .jdt_compile_session import JdtBuildWorldPlan, resource_tree_fingerprint


_PROCESSOR_SERVICE = "META-INF/services/javax.annotation.processing.Processor"


class GradleRuntimeBuildWorldError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class GradleRuntimeBuildWorld:
    project_root: Path
    module_output: Path
    runtime_classpath: tuple[Path, ...]
    jdt_plan: JdtBuildWorldPlan
    configuration_inputs: tuple[Path, ...]


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise GradleRuntimeBuildWorldError(code, message)


def _paths(values: Sequence[str]) -> tuple[Path, ...]:
    return tuple(Path(value).expanduser().resolve(strict=False) for value in values)


def _existing_paths(
    values: Sequence[str],
    *,
    optional_missing: set[Path] = frozenset(),
) -> tuple[Path, ...]:
    result: list[Path] = []
    for path in _paths(values):
        if path.exists():
            result.append(path)
        elif path not in optional_missing:
            raise GradleRuntimeBuildWorldError(
                "GRADLE_CLASSPATH_ENTRY_UNAVAILABLE",
                "A Gradle Runtime classpath entry is unavailable.",
            )
    return tuple(result)


def _processor_facts(path: Path) -> tuple[tuple[str, ...], bool]:
    service: bytes | None = None
    lombok = False
    if path.is_dir():
        candidate = path / _PROCESSOR_SERVICE
        if candidate.is_file():
            service = candidate.read_bytes()
        lombok = (path / "lombok").is_dir()
    elif path.is_file() and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if _PROCESSOR_SERVICE in names:
                service = archive.read(_PROCESSOR_SERVICE)
            lombok = bool({"lombok/launch/Agent.class", "lombok/Lombok.class"} & names)
    else:
        raise GradleRuntimeBuildWorldError(
            "GRADLE_COMPILE_CLASSPATH_UNMODELED",
            "Gradle compile classpath contains a non-binary entry.",
        )
    providers: list[str] = []
    if service is not None:
        for raw in service.decode("utf-8", errors="strict").splitlines():
            value = raw.partition("#")[0].strip()
            if value and value not in providers:
                providers.append(value)
    return tuple(providers), lombok


def _javac(java_home: Path) -> Path:
    name = "javac.exe" if os.name == "nt" else "javac"
    return (java_home / "bin" / name).resolve(strict=True)


def create_gradle_runtime_build_world(
    *,
    model: dict[str, Any],
    project_root: Path,
    configuration_inputs: Sequence[Path],
    configuration_environment_names: Sequence[str],
) -> GradleRuntimeBuildWorld:
    project = project_root.expanduser().resolve(strict=True)
    source_root = (project / "src/main/java").resolve(strict=False)
    resource_root = (project / "src/main/resources").resolve(strict=False)
    main = model["main"]
    compile_task = model["compileJava"]
    _require(
        source_root.is_dir(),
        "GRADLE_SOURCE_LAYOUT_UNSUPPORTED",
        "Gradle Runtime launch requires a main Java source root.",
    )
    _require(
        model.get("exportScope") == "runtime",
        "GRADLE_RUNTIME_PROBE_SCOPE_MISMATCH",
        "The Gradle Probe did not export a Runtime Build World.",
    )
    _require(
        _paths(main["javaSourceDirectories"]) == (source_root,),
        "GRADLE_SOURCE_LAYOUT_UNSUPPORTED",
        "Gradle Runtime launch requires the standard main Java source root.",
    )
    _require(
        _paths(main["resourceDirectories"]) == (resource_root,),
        "GRADLE_RESOURCE_LAYOUT_UNSUPPORTED",
        "Gradle Runtime launch requires the standard main resource root.",
    )
    _require(
        not any(
            main[field]
            for field in (
                "javaIncludes",
                "javaExcludes",
                "resourceIncludes",
                "resourceExcludes",
            )
        ),
        "GRADLE_SOURCE_PATTERN_UNMODELED",
        "Gradle Runtime launch does not model SourceSet patterns.",
    )
    expected_sources = {
        path.resolve(strict=True)
        for path in source_root.rglob("*.java")
        if path.is_file()
    }
    _require(
        set(_paths(compile_task["sourceFiles"])) == expected_sources,
        "GRADLE_COMPILE_SOURCE_SET_UNMODELED",
        "Gradle compileJava sources differ from the Runtime source root.",
    )
    _require(
        not any(path.name == "module-info.java" for path in expected_sources),
        "GRADLE_JPMS_UNSUPPORTED",
        "Gradle Runtime reload does not support JPMS/module-path compilation.",
    )
    source = str(compile_task["sourceCompatibility"])
    target = str(compile_task["targetCompatibility"])
    _require(
        source == target and source in {"1.8", "8", "11"},
        "GRADLE_COMPILE_LEVEL_UNMODELED",
        "Gradle Runtime reload requires equal Java 8 or 11 source/target.",
    )
    source_level = 8 if source in {"1.8", "8"} else 11
    encoding = compile_task.get("encoding")
    _require(
        isinstance(encoding, str) and bool(encoding.strip()),
        "GRADLE_COMPILE_ENCODING_UNMODELED",
        "Gradle compileJava must declare its source encoding.",
    )
    compiler_args = tuple(compile_task["compilerArgsPrivate"])
    _require(
        set(compiler_args).issubset({"-parameters"})
        and compile_task["release"] is None
        and not compile_task["fork"]
        and not compile_task["compilerArgumentProvidersUnmodeled"]
        and compile_task["debug"]
        and compile_task["incremental"],
        "GRADLE_COMPILE_CONFIGURATION_UNMODELED",
        "Gradle compileJava has unsupported Runtime reload configuration.",
    )
    module_output = Path(compile_task["destinationDirectory"]).resolve(strict=True)
    _require(
        _paths(main["classesDirectories"]) == (module_output,),
        "GRADLE_MAIN_OUTPUT_UNMODELED",
        "Gradle main SourceSet has multiple or mismatched class outputs.",
    )
    optional_missing = {
        Path(value).resolve(strict=False)
        for value in (main.get("resourcesDirectory"),)
        if value and not Path(value).resolve(strict=False).exists()
    }
    dependencies = _existing_paths(
        compile_task["classpath"], optional_missing=optional_missing
    )
    runtime_classpath = _existing_paths(
        main["runtimeClasspath"], optional_missing=optional_missing
    )
    _require(
        runtime_classpath.count(module_output) == 1,
        "GRADLE_RUNTIME_OUTPUT_UNMODELED",
        "Gradle main runtime classpath must contain its class output once.",
    )
    processors = _existing_paths(compile_task["annotationProcessorPath"])
    processor_entries: list[Path] = []
    lombok_entries: list[Path] = []
    for entry in processors:
        providers, lombok = _processor_facts(entry)
        if lombok or any(name.startswith("lombok.") for name in providers):
            lombok_entries.append(entry)
        if any(not name.startswith("lombok.") for name in providers):
            processor_entries.append(entry)
    _require(
        not processor_entries,
        "GRADLE_RUNTIME_PROCESSOR_UNMODELED",
        "G4 Runtime reload currently supports Lombok but no other Processor.",
    )
    target_java_home = Path(compile_task["compilerJavaHome"]).resolve(strict=True)
    configuration = tuple(
        dict.fromkeys(
            path.expanduser().resolve(strict=False) for path in configuration_inputs
        )
    )
    java_compiler = _javac(target_java_home)
    fingerprint = fast_compile_fingerprint(
        configuration_inputs=configuration,
        configuration_environment_names=configuration_environment_names,
        javac_executable=java_compiler,
        compile_classpath=tuple(dict.fromkeys((*dependencies, *runtime_classpath))),
    )
    return GradleRuntimeBuildWorld(
        project_root=project,
        module_output=module_output,
        runtime_classpath=runtime_classpath,
        configuration_inputs=configuration,
        jdt_plan=JdtBuildWorldPlan(
            project_root=project,
            module_root=project,
            source_roots=(source_root,),
            dependency_entries=dependencies,
            processor_entries=(),
            lombok_entries=tuple(lombok_entries),
            target_java_home=target_java_home,
            source_encoding=encoding,
            source_level=source_level,
            target_level=source_level,
            fingerprint=fingerprint,
            configuration_inputs=configuration,
            configuration_environment_names=tuple(
                sorted(set(configuration_environment_names))
            ),
            javac_executable=java_compiler,
            method_parameters="-parameters" in compiler_args,
            freshness_entries=tuple(dict.fromkeys((*dependencies, *runtime_classpath))),
            resource_roots=(resource_root,),
            resource_fingerprint=resource_tree_fingerprint((resource_root,)),
        ),
    )


__all__ = [
    "GradleRuntimeBuildWorld",
    "GradleRuntimeBuildWorldError",
    "create_gradle_runtime_build_world",
]
