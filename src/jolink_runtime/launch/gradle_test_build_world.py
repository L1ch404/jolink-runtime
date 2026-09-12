"""Validate a private Gradle model into the Fast Test authority contract."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path
from typing import Any, Sequence

from .test_build_world import JavaTestBuildWorld


class GradleBuildWorldError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _paths(values: Sequence[str]) -> tuple[Path, ...]:
    return tuple(Path(value).expanduser().resolve(strict=False) for value in values)


def _existing(
    values: Sequence[str],
    *,
    optional_missing: set[Path],
    field: str,
) -> tuple[Path, ...]:
    result: list[Path] = []
    for path in _paths(values):
        if path.exists():
            result.append(path)
        elif path not in optional_missing:
            raise GradleBuildWorldError(
                "GRADLE_CLASSPATH_ENTRY_UNAVAILABLE",
                f"Gradle classpath entry is unavailable: {field}.",
            )
    return tuple(result)


def _is_lombok(path: Path) -> bool:
    if not path.is_file() or path.suffix.casefold() != ".jar":
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return False
    return bool(
        {"lombok/launch/Agent.class", "lombok/core/AnnotationProcessor.class"}
        & names
    )


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise GradleBuildWorldError(code, message)


def javac_executable(java_home: Path, *, windows: bool | None = None) -> Path:
    if windows is None:
        windows = os.name == "nt"
    name = "javac.exe" if windows else "javac"
    return (java_home / "bin" / name).resolve(strict=True)


def create_gradle_test_build_world(
    *,
    model: dict[str, Any],
    project_root: Path,
    configuration_inputs: Sequence[Path],
    runner_environment: dict[str, str],
    configuration_environment_names: Sequence[str],
) -> JavaTestBuildWorld:
    if model.get("modules"):
        return _module_test_world(model, project_root, configuration_inputs, runner_environment, configuration_environment_names)
    project = project_root.resolve(strict=True)
    standard_main = (project / "src/main/java").resolve(strict=False)
    standard_test = (project / "src/test/java").resolve(strict=False)
    standard_main_resources = (project / "src/main/resources").resolve(
        strict=False
    )
    standard_test_resources = (project / "src/test/resources").resolve(
        strict=False
    )
    _require(
        _paths(model["main"]["javaSourceDirectories"]) == (standard_main,)
        and _paths(model["test"]["javaSourceDirectories"]) == (standard_test,),
        "GRADLE_SOURCE_LAYOUT_UNSUPPORTED",
        "Gradle Fast Test requires standard main/test Java source roots.",
    )
    _require(
        _paths(model["main"]["resourceDirectories"])
        == (standard_main_resources,)
        and _paths(model["test"]["resourceDirectories"])
        == (standard_test_resources,),
        "GRADLE_RESOURCE_LAYOUT_UNSUPPORTED",
        "Gradle Fast Test requires standard main/test resource roots.",
    )
    for source_set in (model["main"], model["test"]):
        _require(
            not any(
                source_set[field]
                for field in (
                    "javaIncludes",
                    "javaExcludes",
                    "resourceIncludes",
                    "resourceExcludes",
                )
            ),
            "GRADLE_SOURCE_PATTERN_UNMODELED",
            "Gradle Fast Test does not model SourceSet include/exclude patterns.",
        )
    # Resource-bearing Gradle outputs are separate directories. The first
    # product slice rejects them until ordered overlay semantics are modeled.
    _require(
        not any(
            root.is_dir() and any(path.is_file() for path in root.rglob("*"))
            for root in (standard_main_resources, standard_test_resources)
        ),
        "GRADLE_RESOURCES_UNMODELED",
        "Gradle Fast Test v0.1 requires empty main/test resource roots.",
    )
    expected_main_sources = {
        path.resolve(strict=True)
        for path in standard_main.rglob("*.java")
        if path.is_file()
    }
    expected_test_sources = {
        path.resolve(strict=True)
        for path in standard_test.rglob("*.java")
        if path.is_file()
    }
    _require(
        set(_paths(model["compileJava"]["sourceFiles"]))
        == expected_main_sources
        and set(_paths(model["compileTestJava"]["sourceFiles"]))
        == expected_test_sources,
        "GRADLE_COMPILE_SOURCE_SET_UNMODELED",
        "Gradle JavaCompile source files differ from standard source roots.",
    )
    _require(
        not any(
            path.name == "module-info.java"
            for path in expected_main_sources | expected_test_sources
        ),
        "GRADLE_JPMS_UNSUPPORTED",
        "Gradle JPMS/module-path compilation is not supported.",
    )

    main_compile = model["compileJava"]
    test_compile = model["compileTestJava"]
    main_home = Path(main_compile["compilerJavaHome"]).resolve(strict=True)
    test_home = Path(test_compile["compilerJavaHome"]).resolve(strict=True)
    _require(
        main_home == test_home,
        "GRADLE_COMPILE_TOOLCHAIN_UNMODELED",
        "Main and test compilation use different Java toolchains.",
    )
    _require(
        main_compile["sourceCompatibility"]
        == test_compile["sourceCompatibility"]
        == main_compile["targetCompatibility"]
        == test_compile["targetCompatibility"]
        and main_compile["sourceCompatibility"] in {"1.8", "8", "11"},
        "GRADLE_COMPILE_LEVEL_UNMODELED",
        "Gradle main/test Java levels cannot share one JDT project.",
    )
    level = 8 if main_compile["sourceCompatibility"] in {"1.8", "8"} else 11
    _require(
        bool(main_compile["encoding"])
        and main_compile["encoding"] == test_compile["encoding"],
        "GRADLE_COMPILE_ENCODING_UNMODELED",
        "Gradle main/test source encodings differ.",
    )
    for task in (main_compile, test_compile):
        _require(
            task["release"] is None
            and not task["compilerArgsPrivate"]
            and not task["fork"]
            and not task["compilerArgumentProvidersUnmodeled"]
            and task["debug"]
            and task["incremental"],
            "GRADLE_COMPILE_CONFIGURATION_UNMODELED",
            "Gradle JavaCompile has unsupported configuration.",
        )

    main_output = Path(main_compile["destinationDirectory"]).resolve(strict=False)
    test_output = Path(test_compile["destinationDirectory"]).resolve(strict=False)
    _require(
        _paths(model["main"]["classesDirectories"]) == (main_output,),
        "GRADLE_MAIN_OUTPUT_UNMODELED",
        "Gradle main SourceSet has multiple or mismatched class outputs.",
    )
    _require(
        _paths(model["test"]["classesDirectories"]) == (test_output,),
        "GRADLE_TEST_OUTPUT_UNMODELED",
        "Gradle test SourceSet has multiple or mismatched class outputs.",
    )
    optional_missing = {
        Path(value).resolve(strict=False)
        for value in (
            model["main"].get("resourcesDirectory"),
            model["test"].get("resourcesDirectory"),
        )
        if value and not Path(value).resolve(strict=False).exists()
    }
    optional_missing.update((main_output, test_output))
    main_dependencies = _existing(
        main_compile["classpath"],
        optional_missing=optional_missing,
        field="compileJava.classpath",
    )
    raw_test = _existing(
        test_compile["classpath"],
        optional_missing=optional_missing,
        field="compileTestJava.classpath",
    )
    without_outputs = tuple(
        path for path in raw_test if path not in {main_output, test_output}
    )
    _require(
        without_outputs[: len(main_dependencies)] == main_dependencies,
        "GRADLE_TEST_CLASSPATH_ORDER_UNMODELED",
        "Gradle test classpath does not preserve the main dependency prefix.",
    )
    test_dependencies = without_outputs[len(main_dependencies) :]

    main_processors = _existing(
        main_compile["annotationProcessorPath"],
        optional_missing=set(),
        field="compileJava.annotationProcessorPath",
    )
    test_processors = _existing(
        test_compile["annotationProcessorPath"],
        optional_missing=set(),
        field="compileTestJava.annotationProcessorPath",
    )
    _require(
        main_processors == test_processors,
        "GRADLE_PROCESSOR_PATH_UNMODELED",
        "Gradle main/test Processor paths differ.",
    )
    _require(
        not any(_is_lombok(path) for path in main_processors),
        "GRADLE_LOMBOK_UNMODELED",
        "Gradle Lombok requires a dedicated ECJ javaagent path.",
    )
    for task in (main_compile, test_compile):
        generated = task.get("generatedSourceOutputDirectory")
        if generated:
            generated_root = Path(generated).resolve(strict=False)
            _require(
                not generated_root.is_dir()
                or not any(generated_root.rglob("*.java")),
                "GRADLE_SOURCE_GENERATING_PROCESSOR_UNSUPPORTED",
                "Gradle source-generating Processors are not supported.",
            )

    runtime = model["testRuntime"]
    _require(
        runtime["framework"] == "junit_platform",
        "GRADLE_TEST_FRAMEWORK_UNSUPPORTED",
        "Gradle Test uses an unsupported framework.",
    )
    _require(
        runtime["enableAssertions"] is True
        and runtime["debug"] is False
        and runtime["failFast"] is False
        and runtime["dryRun"] is False
        and runtime["scanForTestClasses"] is True
        and runtime["minHeapSize"] is None
        and runtime["maxHeapSize"] is None
        and not runtime["jvmArgsPrivate"]
        and not runtime["jvmArgumentProvidersUnmodeled"]
        and not runtime["systemPropertiesPrivate"]
        and not runtime["environmentOverridesPrivate"]
        and not runtime["bootstrapClasspath"]
        and runtime["maxParallelForks"] == 1
        and runtime["forkEvery"] == 0
        and not any(
            runtime[field]
            for field in (
                "includePatterns",
                "excludePatterns",
                "includeEngines",
                "excludeEngines",
                "includeTags",
                "excludeTags",
            )
        ),
        "GRADLE_TEST_CONFIGURATION_UNMODELED",
        "Gradle Test has unsupported runtime configuration.",
    )
    runtime_paths = _paths(runtime["classpath"])
    _require(
        _paths(runtime["testClassesDirectories"]) == (test_output,),
        "GRADLE_TEST_CLASSES_UNMODELED",
        "Gradle Test classes directories differ from compileTestJava output.",
    )
    test_working_directory = Path(runtime["workingDirectory"]).resolve(
        strict=False
    )
    _require(
        test_working_directory.is_dir(),
        "GRADLE_TEST_WORKING_DIRECTORY_UNAVAILABLE",
        "Gradle Test working directory is unavailable.",
    )
    _require(
        runtime_paths.count(test_output) == 1
        and runtime_paths.count(main_output) == 1,
        "GRADLE_TEST_RUNTIME_OUTPUT_UNMODELED",
        "Gradle Test classpath omits or duplicates formal class outputs.",
    )
    runtime_dependencies = _existing(
        (
            str(path)
            for path in runtime_paths
            if path not in {test_output, main_output}
        ),
        optional_missing=optional_missing,
        field="test.classpath",
    )

    source_roots = (standard_main, standard_test)
    resource_roots = (standard_main_resources, standard_test_resources)
    configuration = tuple(
        path.resolve(strict=False) for path in configuration_inputs
    )
    return JavaTestBuildWorld(
        build_system="gradle",
        project_root=project,
        module_root=project,
        main_source_roots=(standard_main,),
        test_source_roots=(standard_test,),
        main_output=main_output,
        test_output=test_output,
        main_dependencies=main_dependencies,
        test_dependencies=test_dependencies,
        test_runtime_classpath=runtime_dependencies,
        resource_roots=resource_roots,
        target_java_home=main_home,
        source_encoding=main_compile["encoding"],
        source_level=level,
        method_parameters=False,
        processor_entries=main_processors,
        java_agents=(),
        extra_worker_jvm_arguments=(),
        test_java_executable=Path(runtime["javaExecutable"]).resolve(strict=True),
        test_framework="junit5",
        test_working_directory=test_working_directory,
        test_classes_directories=(test_output,),
        runner_environment=dict(runner_environment),
        javac_executable=javac_executable(main_home),
        configuration_inputs=configuration,
        configuration_environment_names=tuple(
            sorted(set(configuration_environment_names))
        ),
        runner_support_provenance={
            "build_system": "gradle",
            "gradle_version": model["gradleVersion"],
            "test_framework": runtime["framework"],
        },
        native_resource_oracle_required=bool(main_processors),
    )


def _module_test_world(model, project_root, inputs, environment, environment_names):
    from .gradle_module_world import module_world

    modules, target, classpath = module_world(model)
    runtime = model["testRuntime"]
    frameworks = {"junit_platform": "junit5", "junit4": "junit4", "testng": "testng"}
    _require(
        runtime["framework"] in frameworks,
        "GRADLE_TEST_FRAMEWORK_UNSUPPORTED",
        "Unsupported Gradle test framework.",
    )
    _require(
        not any(
            runtime[key]
            for key in (
                "includePatterns",
                "excludePatterns",
                "includeEngines",
                "excludeEngines",
                "includeTags",
                "excludeTags",
            )
        ),
        "GRADLE_TEST_FILTER_UNMODELED",
        "Gradle test filters are not mapped to the standalone Runner.",
    )
    args = list(runtime["jvmArgsPrivate"])
    if runtime["enableAssertions"]:
        args.append("-ea")
    if runtime["minHeapSize"]:
        args.append("-Xms" + runtime["minHeapSize"])
    if runtime["maxHeapSize"]:
        args.append("-Xmx" + runtime["maxHeapSize"])
    args.extend(
        "-D" + key + "=" + str(value)
        for key, value in runtime["systemPropertiesPrivate"].items()
    )
    runner_environment = dict(environment)
    for key, value in runtime["environmentOverridesPrivate"].items():
        if value is None:
            runner_environment.pop(key, None)
        else:
            runner_environment[key] = value
    home = Path(target["target_java_home"])
    outputs = {Path(target["output_directory"]), Path(target["test_output_directory"])}
    configuration = tuple(
        dict.fromkeys(
            (*inputs, *(Path(p) for p in model.get("configurationFiles", ())),
             *(Path(p) for p in model.get("configurationDirectories", ())))
        )
    )
    return JavaTestBuildWorld(
        build_system="gradle",
        project_root=project_root,
        module_root=Path(target["module_root"]),
        main_source_roots=tuple(Path(p) for m in modules for p in m["source_roots"]),
        test_source_roots=tuple(Path(p) for p in target["test_source_roots"]),
        main_output=Path(target["output_directory"]),
        test_output=Path(target["test_output_directory"]),
        main_dependencies=tuple(Path(p) for p in target["classpath"]),
        test_dependencies=tuple(Path(p) for p in target["test_classpath"]),
        test_runtime_classpath=tuple(p for p in classpath if p not in outputs),
        resource_roots=tuple(
            Path(p)
            for sources in (model["test"], model["main"])
            for p in sources["resourceDirectories"]
        ),
        target_java_home=home,
        source_encoding=target["source_encoding"],
        source_level=target["source_level"],
        method_parameters=target["method_parameters"],
        processor_entries=tuple(Path(p) for p in target["processor_entries"]),
        java_agents=tuple(
            dict.fromkeys(p + "=ECJ" for m in modules
                for p in (*m["lombok_entries"], *m.get("test_compiler", {}).get("lombok_entries", ())))
        ),
        extra_worker_jvm_arguments=(),
        test_java_executable=Path(runtime["javaExecutable"]),
        test_framework=frameworks[runtime["framework"]],
        test_working_directory=Path(runtime["workingDirectory"]),
        test_classes_directories=(Path(target["test_output_directory"]),),
        runner_environment=runner_environment,
        javac_executable=javac_executable(home),
        configuration_inputs=configuration,
        configuration_environment_names=tuple(
            dict.fromkeys((*environment_names, *runtime["environmentOverrideNames"]))
        ),
        test_jvm_arguments=tuple(args),
        modules=modules,
        runner_support_provenance={
            "build_system": "gradle",
            "gradle_version": model["gradleVersion"],
            "test_framework": runtime["framework"],
        },
    )


__all__ = [
    "GradleBuildWorldError",
    "create_gradle_test_build_world",
    "javac_executable",
]
