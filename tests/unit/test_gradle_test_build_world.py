from __future__ import annotations

import copy
import zipfile
from pathlib import Path

import pytest

from jolink_runtime.launch.fast_test_manager import (
    FastTestManager,
    FastTestManagerError,
)
from jolink_runtime.launch.gradle_test_build_world import (
    GradleBuildWorldError,
    create_gradle_test_build_world,
    javac_executable,
)
from jolink_runtime.launch.gradle_runtime_build_world import (
    GradleRuntimeBuildWorldError,
    create_gradle_runtime_build_world,
)


def _model(tmp_path: Path) -> tuple[Path, dict]:
    project = tmp_path / "project"
    main_source = project / "src/main/java/example/App.java"
    test_source = project / "src/test/java/example/AppTest.java"
    main_source.parent.mkdir(parents=True)
    test_source.parent.mkdir(parents=True)
    main_source.write_text("package example; class App {}\n", encoding="utf-8")
    test_source.write_text(
        "package example; class AppTest {}\n", encoding="utf-8"
    )
    main_output = project / "build/classes/java/main"
    test_output = project / "build/classes/java/test"
    dependency = project / "deps/main"
    test_dependency = project / "deps/test"
    runtime_dependency = project / "deps/runtime"
    generated_main = project / "build/generated/sources/main"
    generated_test = project / "build/generated/sources/test"
    java_home = project / "jdk"
    for path in (
        main_output,
        test_output,
        dependency,
        test_dependency,
        runtime_dependency,
        generated_main,
        generated_test,
        java_home / "bin",
    ):
        path.mkdir(parents=True)
    for name in ("java", "javac", "java.exe", "javac.exe"):
        (java_home / "bin" / name).write_bytes(b"")

    def source_set(name: str, output: Path) -> dict:
        return {
            "name": name,
            "javaSourceDirectories": [
                str(project / f"src/{name}/java")
            ],
            "javaIncludes": [],
            "javaExcludes": [],
            "resourceDirectories": [
                str(project / f"src/{name}/resources")
            ],
            "resourceIncludes": [],
            "resourceExcludes": [],
            "classesDirectories": [str(output)],
            "resourcesDirectory": str(project / f"build/resources/{name}"),
        }

    def compile_task(
        source: Path,
        output: Path,
        classpath: list[Path],
        generated: Path,
    ) -> dict:
        return {
            "sourceCompatibility": "11",
            "targetCompatibility": "11",
            "sourceFiles": [str(source)],
            "classpath": [str(path) for path in classpath],
            "destinationDirectory": str(output),
            "encoding": "UTF-8",
            "debug": True,
            "fork": False,
            "incremental": True,
            "compilerArgumentProvidersUnmodeled": False,
            "compilerArgsPrivate": [],
            "annotationProcessorPath": [],
            "generatedSourceOutputDirectory": str(generated),
            "release": None,
            "compilerJavaHome": str(java_home),
        }

    model = {
        "gradleVersion": "8.10",
        "main": source_set("main", main_output),
        "test": source_set("test", test_output),
        "compileJava": compile_task(
            main_source, main_output, [dependency], generated_main
        ),
        "compileTestJava": compile_task(
            test_source,
            test_output,
            [main_output, dependency, test_dependency],
            generated_test,
        ),
        "testRuntime": {
            "framework": "junit_platform",
            "testClassesDirectories": [str(test_output)],
            "classpath": [
                str(test_output),
                str(main_output),
                str(dependency),
                str(test_dependency),
                str(runtime_dependency),
            ],
            "workingDirectory": str(project),
            "enableAssertions": True,
            "debug": False,
            "failFast": False,
            "dryRun": False,
            "scanForTestClasses": True,
            "javaExecutable": str(java_home / "bin/java"),
            "minHeapSize": None,
            "maxHeapSize": None,
            "jvmArgsPrivate": [],
            "jvmArgumentProvidersUnmodeled": False,
            "systemPropertiesPrivate": {},
            "environmentOverridesPrivate": {},
            "bootstrapClasspath": [],
            "maxParallelForks": 1,
            "forkEvery": 0,
            "includePatterns": [],
            "excludePatterns": [],
            "includeEngines": [],
            "excludeEngines": [],
            "includeTags": [],
            "excludeTags": [],
        },
    }
    return project, model


def _convert(tmp_path: Path, model: dict):
    project = tmp_path / "project"
    return create_gradle_test_build_world(
        model=model,
        project_root=project,
        configuration_inputs=(project / "build.gradle",),
        runner_environment={"JAVA_HOME": str(project / "jdk")},
        configuration_environment_names=("GRADLE_USER_HOME",),
    )


def test_product_gradle_authority_preserves_test_runtime(tmp_path: Path) -> None:
    project, model = _model(tmp_path)
    working = project / "test-work"
    working.mkdir()
    model["testRuntime"]["workingDirectory"] = str(working)

    world = _convert(tmp_path, model)

    assert world.test_framework == "junit5"
    assert world.test_working_directory == working.resolve()
    assert world.test_classes_directories == (world.test_output,)
    assert world.runner_environment["JAVA_HOME"] == str(project / "jdk")
    assert world.test_runtime_classpath[-1].name == "runtime"
    assert world.configuration_inputs == (
        (project / "build.gradle").resolve(strict=False),
    )
    assert world.configuration_environment_names == ("GRADLE_USER_HOME",)


@pytest.mark.parametrize(
    ("code", "mutation"),
    (
        (
            "GRADLE_COMPILE_SOURCE_SET_UNMODELED",
            lambda model, root: model["compileJava"].__setitem__(
                "sourceFiles", []
            ),
        ),
        (
            "GRADLE_COMPILE_ENCODING_UNMODELED",
            lambda model, root: model["compileJava"].__setitem__(
                "encoding", None
            ),
        ),
        (
            "GRADLE_TEST_CLASSES_UNMODELED",
            lambda model, root: model["testRuntime"][
                "testClassesDirectories"
            ].append(str(root / "extra")),
        ),
        (
            "GRADLE_TEST_CONFIGURATION_UNMODELED",
            lambda model, root: model["testRuntime"].__setitem__(
                "enableAssertions", False
            ),
        ),
        (
            "GRADLE_TEST_FRAMEWORK_UNSUPPORTED",
            lambda model, root: model["testRuntime"].__setitem__(
                "framework", "junit"
            ),
        ),
        (
            "GRADLE_COMPILE_CONFIGURATION_UNMODELED",
            lambda model, root: model["compileJava"].__setitem__(
                "release", 11
            ),
        ),
        (
            "GRADLE_SOURCE_LAYOUT_UNSUPPORTED",
            lambda model, root: model["main"][
                "javaSourceDirectories"
            ].append(str(root / "src/custom/java")),
        ),
    ),
)
def test_product_gradle_authority_rejects_unmodeled_semantics(
    tmp_path: Path, code: str, mutation
) -> None:
    project, model = _model(tmp_path)
    mutation(model, project)

    with pytest.raises(GradleBuildWorldError) as captured:
        _convert(tmp_path, model)

    assert captured.value.error_code == code


def test_product_gradle_authority_rejects_jpms(tmp_path: Path) -> None:
    project, model = _model(tmp_path)
    module_info = project / "src/main/java/module-info.java"
    module_info.write_text("module example {}\n", encoding="utf-8")
    model["compileJava"]["sourceFiles"].append(str(module_info))

    with pytest.raises(GradleBuildWorldError) as captured:
        _convert(tmp_path, model)

    assert captured.value.error_code == "GRADLE_JPMS_UNSUPPORTED"


def test_product_gradle_authority_rejects_generated_sources(
    tmp_path: Path,
) -> None:
    _project, model = _model(tmp_path)
    generated = Path(
        model["compileJava"]["generatedSourceOutputDirectory"]
    ) / "example/Generated.java"
    generated.parent.mkdir(parents=True)
    generated.write_text("package example; class Generated {}\n", encoding="utf-8")

    with pytest.raises(GradleBuildWorldError) as captured:
        _convert(tmp_path, model)

    assert (
        captured.value.error_code
        == "GRADLE_SOURCE_GENERATING_PROCESSOR_UNSUPPORTED"
    )


def test_gradle_javac_uses_windows_executable(tmp_path: Path) -> None:
    home = tmp_path / "jdk"
    (home / "bin").mkdir(parents=True)
    expected = home / "bin/javac.exe"
    expected.write_bytes(b"")

    assert javac_executable(home, windows=True) == expected.resolve()


def test_build_system_provider_dispatch_rejects_ambiguous_project(
    tmp_path: Path,
) -> None:
    (tmp_path / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    (tmp_path / "build.gradle").write_text("plugins {}\n", encoding="utf-8")
    (tmp_path / "gradlew").write_bytes(b"")
    manager = FastTestManager()
    attempt = type("Attempt", (), {"project_path": tmp_path})()
    try:
        with pytest.raises(FastTestManagerError) as captured:
            manager._bootstrap(attempt)
        assert captured.value.error_code == "BUILD_SYSTEM_AMBIGUOUS"
    finally:
        manager.close()


def _prepare_runtime_model(tmp_path: Path, model: dict) -> dict:
    project = tmp_path / "project"
    model["exportScope"] = "runtime"
    main_output = model["compileJava"]["destinationDirectory"]
    model["main"]["runtimeClasspath"] = [
        main_output,
        *(
            [model["main"]["resourcesDirectory"]]
            if Path(model["main"]["resourcesDirectory"]).is_dir()
            else []
        ),
        *model["compileJava"]["classpath"],
        str(project / "deps/runtime"),
    ]
    model["runtimeExecution"] = {
        "compileJavaTaskPath": ":compileJava",
        "processResourcesTaskPath": ":processResources",
        "classesTaskPath": ":classes",
        "exportTaskPath": ":jolinkExportBuildWorld_fixture",
        "executedTaskPaths": [
            ":classes",
            ":compileJava",
            ":jolinkExportBuildWorld_fixture",
            ":processResources",
        ],
        "classOutputOverlappingTaskPaths": [":compileJava"],
        "compileJavaActionCount": 1,
        "processResourcesActionCount": 1,
        "classesActionCount": 0,
    }
    model["appliedPluginClassNames"] = [
        "org.gradle.api.plugins.JavaPlugin_Decorated"
    ]
    return model


def _runtime_world(tmp_path: Path, model: dict):
    model = _prepare_runtime_model(tmp_path, model)
    project = tmp_path / "project"
    return create_gradle_runtime_build_world(
        model=model,
        project_root=project,
        configuration_inputs=(project / "build.gradle",),
        configuration_environment_names=("GRADLE_USER_HOME",),
    )


def test_product_gradle_runtime_world_creates_reload_authority(
    tmp_path: Path,
) -> None:
    project, model = _model(tmp_path)

    world = _runtime_world(tmp_path, model)

    assert world.project_root == project.resolve()
    assert world.module_output == Path(
        model["compileJava"]["destinationDirectory"]
    ).resolve()
    assert world.runtime_classpath[0] == world.module_output
    assert world.jdt_plan.source_level == 11
    assert world.jdt_plan.target_level == 11
    assert world.jdt_plan.source_encoding == "UTF-8"
    assert world.jdt_plan.configuration_environment_names == (
        "GRADLE_USER_HOME",
    )


def test_product_gradle_runtime_freshness_covers_resources_and_runtime_only_deps(
    tmp_path: Path,
) -> None:
    project, model = _model(tmp_path)
    world = _runtime_world(tmp_path, model)
    assert world.jdt_plan.is_fresh() is True

    resource = project / "src/main/resources/application.properties"
    resource.parent.mkdir(parents=True)
    resource.write_text("feature=true\n", encoding="utf-8")
    assert world.jdt_plan.is_fresh() is False

    resource.unlink()
    world = _runtime_world(tmp_path, model)
    assert world.jdt_plan.is_fresh() is True
    runtime_class = project / "deps/runtime/example/RuntimeOnly.class"
    runtime_class.parent.mkdir(parents=True)
    runtime_class.write_bytes(b"changed")
    assert world.jdt_plan.is_fresh() is False


def test_product_gradle_runtime_seals_and_tracks_formal_resources(
    tmp_path: Path,
) -> None:
    project, model = _model(tmp_path)
    source = project / "src/main/resources/application.properties"
    formal = project / "build/resources/main/application.properties"
    source.parent.mkdir(parents=True)
    formal.parent.mkdir(parents=True)
    source.write_text("feature=v1\n", encoding="utf-8")
    formal.write_text("feature=v1\n", encoding="utf-8")

    world = _runtime_world(tmp_path, model)

    assert world.generation_input_roots == (
        world.module_output,
        formal.parent.resolve(),
    )
    assert world.generation_input_manifest["application.properties"]
    assert world.jdt_plan.is_fresh() is True
    formal.write_text("feature=v2\n", encoding="utf-8")
    assert world.jdt_plan.is_fresh() is False


def test_product_gradle_runtime_rejects_class_resource_collision(
    tmp_path: Path,
) -> None:
    project, model = _model(tmp_path)
    class_file = project / "build/classes/java/main/example/App.class"
    resource_file = project / "build/resources/main/example/App.class"
    class_file.parent.mkdir(parents=True, exist_ok=True)
    resource_file.parent.mkdir(parents=True, exist_ok=True)
    class_file.write_bytes(b"class")
    resource_file.write_bytes(b"resource")

    with pytest.raises(GradleRuntimeBuildWorldError) as captured:
        _runtime_world(tmp_path, model)

    assert captured.value.error_code == "GRADLE_RUNTIME_OUTPUT_COLLISION"


def test_product_gradle_runtime_rejects_transform_task(
    tmp_path: Path,
) -> None:
    _project, model = _model(tmp_path)
    _prepare_runtime_model(tmp_path, model)
    model["runtimeExecution"]["executedTaskPaths"].append(
        ":enhanceClasses"
    )
    model["runtimeExecution"]["classOutputOverlappingTaskPaths"].append(
        ":enhanceClasses"
    )

    with pytest.raises(GradleRuntimeBuildWorldError) as captured:
        create_gradle_runtime_build_world(
            model=model,
            project_root=tmp_path / "project",
            configuration_inputs=(),
            configuration_environment_names=(),
        )

    assert captured.value.error_code == "GRADLE_BYTECODE_TRANSFORM_UNMODELED"


def test_product_gradle_runtime_rejects_unverified_plugin(
    tmp_path: Path,
) -> None:
    _project, model = _model(tmp_path)
    _prepare_runtime_model(tmp_path, model)
    model["appliedPluginClassNames"].append("example.EnhancingPlugin")

    with pytest.raises(GradleRuntimeBuildWorldError) as captured:
        create_gradle_runtime_build_world(
            model=model,
            project_root=tmp_path / "project",
            configuration_inputs=(),
            configuration_environment_names=(),
        )

    assert captured.value.error_code == "GRADLE_BYTECODE_TRANSFORM_UNMODELED"


def test_product_gradle_runtime_rejects_compile_action_injection(
    tmp_path: Path,
) -> None:
    _project, model = _model(tmp_path)
    _prepare_runtime_model(tmp_path, model)
    model["runtimeExecution"]["compileJavaActionCount"] = 2

    with pytest.raises(GradleRuntimeBuildWorldError) as captured:
        create_gradle_runtime_build_world(
            model=model,
            project_root=tmp_path / "project",
            configuration_inputs=(),
            configuration_environment_names=(),
        )

    assert captured.value.error_code == "GRADLE_BYTECODE_TRANSFORM_UNMODELED"

def test_product_gradle_runtime_world_rejects_wrong_probe_scope(
    tmp_path: Path,
) -> None:
    _project, model = _model(tmp_path)
    model["exportScope"] = "test"
    model["main"]["runtimeClasspath"] = [
        model["compileJava"]["destinationDirectory"]
    ]

    with pytest.raises(GradleRuntimeBuildWorldError) as captured:
        create_gradle_runtime_build_world(
            model=model,
            project_root=tmp_path / "project",
            configuration_inputs=(),
            configuration_environment_names=(),
        )

    assert captured.value.error_code == "GRADLE_RUNTIME_PROBE_SCOPE_MISMATCH"


def test_product_gradle_runtime_world_rejects_non_lombok_processor(
    tmp_path: Path,
) -> None:
    project, model = _model(tmp_path)
    processor = project / "deps/processor.jar"
    with zipfile.ZipFile(processor, "w") as archive:
        archive.writestr(
            "META-INF/services/javax.annotation.processing.Processor",
            "example.Processor\n",
        )
    model["compileJava"]["annotationProcessorPath"] = [str(processor)]

    with pytest.raises(GradleRuntimeBuildWorldError) as captured:
        _runtime_world(tmp_path, model)

    assert captured.value.error_code == "GRADLE_RUNTIME_PROCESSOR_UNMODELED"
