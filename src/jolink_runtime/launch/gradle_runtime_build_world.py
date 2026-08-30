"""Convert Maven-native Gradle Probe facts into one Runtime Build World."""

from __future__ import annotations

import os
import hashlib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .fast_compile import fast_compile_fingerprint
from .jdt_compile_session import JdtBuildWorldPlan, resource_tree_fingerprint


_PROCESSOR_SERVICE = "META-INF/services/javax.annotation.processing.Processor"
_VERIFIED_RUNTIME_PLUGIN_CLASSES = frozenset(
    {
        "org.gradle.api.distribution.plugins.DistributionBasePlugin_Decorated",
        "org.gradle.api.distribution.plugins.DistributionPlugin_Decorated",
        "org.gradle.api.plugins.ApplicationPlugin_Decorated",
        "org.gradle.api.plugins.BasePlugin_Decorated",
        "org.gradle.api.plugins.HelpTasksPlugin_Decorated",
        "org.gradle.api.plugins.JavaBasePlugin_Decorated",
        "org.gradle.api.plugins.JavaPlugin_Decorated",
        "org.gradle.api.plugins.JvmEcosystemPlugin_Decorated",
        "org.gradle.api.plugins.JvmTestSuitePlugin_Decorated",
        "org.gradle.api.plugins.JvmToolchainsPlugin_Decorated",
        "org.gradle.api.plugins.ReportingBasePlugin_Decorated",
        "org.gradle.api.plugins.SoftwareReportingTasksPlugin_Decorated",
        "org.gradle.buildinit.plugins.BuildInitPlugin_Decorated",
        "org.gradle.buildinit.plugins.WrapperPlugin_Decorated",
        "org.gradle.language.base.plugins.LifecycleBasePlugin_Decorated",
        "org.gradle.testing.base.plugins.TestSuiteBasePlugin_Decorated",
    }
)


class GradleRuntimeBuildWorldError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class GradleRuntimeBuildWorld:
    project_root: Path
    module_output: Path
    generation_input_roots: tuple[Path, ...]
    generation_input_manifest: dict[str, str]
    resource_source_roots: tuple[Path, ...]
    formal_resource_roots: tuple[Path, ...]
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


def _merged_runtime_manifest(roots: Sequence[Path]) -> dict[str, str]:
    result: dict[str, str] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise GradleRuntimeBuildWorldError(
                    "GRADLE_RUNTIME_OUTPUT_LINK_UNSUPPORTED",
                    "Gradle Runtime outputs may not contain links.",
                )
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            parents = Path(relative).parents
            if (
                relative in result
                or any(parent.as_posix() in result for parent in parents)
                or any(key.startswith(f"{relative}/") for key in result)
            ):
                raise GradleRuntimeBuildWorldError(
                    "GRADLE_RUNTIME_OUTPUT_COLLISION",
                    "Gradle classes/resources outputs contain a path collision.",
                )
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


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
    runtime_execution = model.get("runtimeExecution")
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
        isinstance(runtime_execution, dict),
        "GRADLE_RUNTIME_TASK_GRAPH_UNAVAILABLE",
        "The Gradle Probe did not export the Runtime task graph.",
    )
    applied_plugins = set(model.get("appliedPluginClassNames", ()))
    _require(
        bool(applied_plugins)
        and applied_plugins <= _VERIFIED_RUNTIME_PLUGIN_CLASSES,
        "GRADLE_BYTECODE_TRANSFORM_UNMODELED",
        "Gradle Runtime Bootstrap applies an unverified plugin.",
    )
    expected_tasks = {
        runtime_execution.get("compileJavaTaskPath"),
        runtime_execution.get("processResourcesTaskPath"),
        runtime_execution.get("classesTaskPath"),
        runtime_execution.get("exportTaskPath"),
    }
    _require(
        None not in expected_tasks
        and set(runtime_execution.get("executedTaskPaths", ()))
        == expected_tasks,
        "GRADLE_BYTECODE_TRANSFORM_UNMODELED",
        "Gradle Runtime Bootstrap executed an unmodeled task.",
    )
    _require(
        set(runtime_execution.get("classOutputOverlappingTaskPaths", ()))
        <= {runtime_execution.get("compileJavaTaskPath")},
        "GRADLE_BYTECODE_TRANSFORM_UNMODELED",
        "A Gradle task other than compileJava owns the main class output.",
    )
    _require(
        runtime_execution.get("compileJavaActionCount") == 1
        and runtime_execution.get("processResourcesActionCount") == 1
        and runtime_execution.get("classesActionCount") == 0,
        "GRADLE_BYTECODE_TRANSFORM_UNMODELED",
        "A Gradle lifecycle task has an unverified action chain.",
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
    raw_resource_output = main.get("resourcesDirectory")
    resource_output = (
        Path(raw_resource_output).resolve(strict=False)
        if raw_resource_output
        else None
    )
    formal_resource_roots = (
        (resource_output,)
        if resource_output is not None and resource_output.is_dir()
        else ()
    )
    resource_source_has_files = resource_root.is_dir() and any(
        path.is_file() for path in resource_root.rglob("*")
    )
    _require(
        not resource_source_has_files or bool(formal_resource_roots),
        "GRADLE_RESOURCE_OUTPUT_UNAVAILABLE",
        "Gradle did not produce the main resource output.",
    )
    formal_owned = {module_output, *formal_resource_roots}
    generation_input_roots = tuple(
        path for path in runtime_classpath if path in formal_owned
    )
    _require(
        len(generation_input_roots) == len(formal_owned)
        and len(set(generation_input_roots)) == len(formal_owned),
        "GRADLE_RUNTIME_OUTPUT_UNMODELED",
        "Gradle runtime classpath omits or duplicates a formal output root.",
    )
    owned_positions = tuple(
        runtime_classpath.index(path) for path in generation_input_roots
    )
    _require(
        owned_positions
        == tuple(range(owned_positions[0], owned_positions[0] + len(owned_positions))),
        "GRADLE_RUNTIME_OUTPUT_ORDER_UNMODELED",
        "Gradle classes/resources outputs must be a consecutive classpath segment.",
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
        generation_input_roots=generation_input_roots,
        generation_input_manifest=_merged_runtime_manifest(
            generation_input_roots
        ),
        resource_source_roots=(resource_root,),
        formal_resource_roots=formal_resource_roots,
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
            resource_roots=(resource_root, *formal_resource_roots),
            resource_fingerprint=resource_tree_fingerprint(
                (resource_root, *formal_resource_roots)
            ),
        ),
    )


__all__ = [
    "GradleRuntimeBuildWorld",
    "GradleRuntimeBuildWorldError",
    "create_gradle_runtime_build_world",
]
