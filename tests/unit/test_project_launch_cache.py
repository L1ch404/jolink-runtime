from __future__ import annotations

from pathlib import Path

from jolink_runtime.launch.contracts import JvmLaunchPlan, LaunchIntent
from jolink_runtime.launch.fast_compile import fast_compile_fingerprint
from jolink_runtime.launch.jdt_compile_session import (
    JdtBuildWorldPlan,
    resource_tree_fingerprint,
)
from jolink_runtime.launch.project_launch_cache import ProjectLaunchCache
from jolink_runtime.launch.toolchain import JavaToolchainCandidate


def test_cache_reuses_persisted_model_without_revalidating_project_files(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = project / "src/main/java/example/App.java"
    resource = project / "src/main/resources/application.yml"
    pom = project / "pom.xml"
    dependency = project / "deps/library.jar"
    java = tmp_path / "jdk/bin/java"
    javac = tmp_path / "jdk/bin/javac"
    for path, content in (
        (source, "class App {}\n"),
        (resource, "name: one\n"),
        (pom, "<project/>\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"jar")
    java.parent.mkdir(parents=True)
    java.write_bytes(b"java")
    javac.write_bytes(b"javac")
    fingerprint = fast_compile_fingerprint(
        configuration_inputs=(pom,),
        configuration_environment_names=(),
        javac_executable=javac,
        compile_classpath=(dependency,),
    )
    plan = JdtBuildWorldPlan(
        project_root=project,
        module_root=project,
        source_roots=(source.parent.parent,),
        dependency_entries=(dependency,),
        processor_entries=(),
        lombok_entries=(),
        target_java_home=tmp_path / "jdk",
        source_encoding="UTF-8",
        source_level=8,
        target_level=8,
        fingerprint=fingerprint,
        configuration_inputs=(pom,),
        configuration_environment_names=(),
        javac_executable=javac,
        freshness_entries=(dependency,),
        resource_roots=(resource.parent,),
        resource_fingerprint=resource_tree_fingerprint((resource.parent,)),
    )
    toolchain = JavaToolchainCandidate(
        home=tmp_path / "jdk",
        java_executable=java,
        javac_executable=javac,
        source="test",
        detected_major_version=8,
        detected_compiler_major_version=8,
    )
    intent = LaunchIntent(
        source="idea",
        launch_name="App",
        launch_type="java_application",
        main_class="example.App",
        working_directory=project,
    )
    jvm_plan = JvmLaunchPlan(
        java_executable=java,
        classpath=(project / "target/classes", dependency),
        main_class="example.App",
        working_directory=project,
    )
    cache = ProjectLaunchCache(tmp_path / "cache")
    cache.save(
        project_root=project,
        intent=intent,
        build_system="maven",
        build_offline=False,
        build_jdk=toolchain,
        runtime_jdk=toolchain,
        module_output=project / "target/classes",
        generation_input_roots=(project / "target/classes",),
        resource_source_roots=(resource.parent,),
        jvm_plan=jvm_plan,
        jdt_plan=plan,
    )

    source.write_text("class App { int value; }\n", encoding="utf-8")
    resource.write_text("name: two\n", encoding="utf-8")
    reused = cache.load(
        project_root=project,
        intent=intent,
        build_system="maven",
        ready_port=8080,
        startup_wait_timeout_seconds=12,
    )

    assert reused is not None
    assert reused.jvm_plan.ready_port == 8080
    assert reused.jdt_plan.resource_fingerprint == plan.resource_fingerprint

    pom.write_text("<project><changed/></project>\n", encoding="utf-8")
    assert cache.load(
        project_root=project,
        intent=intent,
        build_system="maven",
        ready_port=8080,
        startup_wait_timeout_seconds=12,
    ) is not None
