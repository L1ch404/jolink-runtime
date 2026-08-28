from __future__ import annotations

import os
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from jolink_runtime.launch import (
    IdeaBuildPreferences,
    JavaToolchainCandidate,
    LaunchErrorCode,
    LaunchIntent,
    MavenBuildSystemAdapter,
    MavenResolutionError,
    MavenToolCandidate,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _pom(
    artifact_id: str,
    *,
    packaging: str = "jar",
    modules: tuple[str, ...] = (),
) -> str:
    module_xml = "".join(f"<module>{name}</module>" for name in modules)
    return f"""
    <project xmlns="http://maven.apache.org/POM/4.0.0">
      <modelVersion>4.0.0</modelVersion>
      <groupId>example</groupId>
      <artifactId>{artifact_id}</artifactId>
      <version>1.0</version>
      <packaging>{packaging}</packaging>
      <modules>{module_xml}</modules>
    </project>
    """


def _intent(
    project: Path,
    *,
    module: str | None = None,
    build_before_run: bool = True,
) -> LaunchIntent:
    return LaunchIntent(
        source="idea",
        launch_name="Application",
        launch_type="spring_boot",
        main_class="com.example.Application",
        working_directory=project,
        ide_module_name=module,
        build_before_run=build_before_run,
    )


def _jdk(
    tmp_path: Path,
    *,
    name: str = "jdk",
    version: str = "1.8.0_402",
) -> JavaToolchainCandidate:
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "release").write_text(
        f'JAVA_VERSION="{version}"\n',
        encoding="utf-8",
    )
    return JavaToolchainCandidate(
        home=home,
        java_executable=home / "bin" / (
            "java.exe" if os.name == "nt" else "java"
        ),
        javac_executable=home / "bin" / (
            "javac.exe" if os.name == "nt" else "javac"
        ),
        source="test",
    )


def _maven(tmp_path: Path) -> MavenToolCandidate:
    return MavenToolCandidate(
        argv_prefix=(str(tmp_path / "maven" / "bin" / "mvn"),),
        source="test",
    )


def _effective_pom(
    artifact_id: str,
    *,
    source: str = "1.8",
    target: str = "1.8",
    source_encoding: str | None = "UTF-8",
    compiler_properties: str = "",
    compiler_configuration: str = "",
    extra_plugins: str = "",
) -> str:
    encoding_property = (
        "<project.build.sourceEncoding>"
        f"{source_encoding}"
        "</project.build.sourceEncoding>"
        if source_encoding is not None
        else ""
    )
    return f"""\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>{artifact_id}</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.source>{source}</maven.compiler.source>
    <maven.compiler.target>{target}</maven.compiler.target>
    {encoding_property}
    {compiler_properties}
  </properties>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <configuration>{compiler_configuration}</configuration>
      </plugin>
      {extra_plugins}
    </plugins>
  </build>
</project>
"""


def _class_header(java_release: int) -> bytes:
    return (
        b"\xca\xfe\xba\xbe"
        + b"\x00\x00"
        + (java_release + 44).to_bytes(2, "big")
    )


def _fast_compile_execution(
    tmp_path: Path,
    *,
    effective_pom: str | None = None,
    compile_entries: tuple[Path, ...] = (),
    build_jdk: JavaToolchainCandidate | None = None,
):
    project = tmp_path / "fast-project"
    _write(project / "pom.xml", _pom("app"))
    _write(
        project / "src/main/java/com/example/Application.java",
        "package com.example; public class Application {}\n",
    )
    adapter = MavenBuildSystemAdapter()
    workspace = adapter.resolve_workspace(project)
    intent = _intent(project)
    module = adapter.select_module(workspace, intent)
    execution = adapter.create_execution_plan(
        workspace=workspace,
        module=module,
        intent=intent,
        maven=_maven(tmp_path),
        build_jdk=build_jdk or _jdk(tmp_path),
        preferences=IdeaBuildPreferences(),
        attempt_directory=tmp_path / "fast-attempt",
    )
    class_file = (
        module.output_directory / "com/example/Application.class"
    )
    class_file.parent.mkdir(parents=True)
    class_file.write_bytes(_class_header(8))
    execution.compile_classpath_file.write_text(
        os.pathsep.join(str(path) for path in compile_entries),
        encoding="utf-8",
    )
    execution.effective_pom_file.write_text(
        effective_pom or _effective_pom("app"),
        encoding="utf-8",
    )
    return adapter, execution


def test_single_module_plan_builds_compile_and_runtime_classpath(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write(project / "pom.xml", _pom("app"))
    adapter = MavenBuildSystemAdapter()
    workspace = adapter.resolve_workspace(project)
    module = adapter.select_module(workspace, _intent(project))
    settings = tmp_path / "settings.xml"
    _write(settings, "<settings />")
    execution = adapter.create_execution_plan(
        workspace=workspace,
        module=module,
        intent=_intent(project),
        maven=_maven(tmp_path),
        build_jdk=_jdk(tmp_path),
        preferences=IdeaBuildPreferences(
            user_settings_file=settings,
            local_repository=tmp_path / "repository",
            active_profiles=("company", "local"),
        ),
        attempt_directory=tmp_path / "attempt",
    )

    operation = adapter.create_build_operation(execution)

    assert operation.operation_name == "maven_compile_and_classpath"
    assert operation.cwd == project
    assert "--batch-mode" in operation.argv
    assert ("-T", "1") == operation.argv[
        operation.argv.index("-T") : operation.argv.index("-T") + 2
    ]
    assert "compile" in operation.argv
    assert any(
        value.endswith(
            "maven-dependency-plugin:3.6.1:build-classpath"
        )
        for value in operation.argv
    )
    assert "-P" in operation.argv
    assert "company,local" in operation.argv
    assert "-pl" not in operation.argv
    assert operation.environment["JAVA_HOME"] == str(_jdk(tmp_path).home)
    output_argument = next(
        value
        for value in operation.argv
        if value.startswith("-Dmdep.outputFile=")
    )
    assert "${project.groupId}-${project.artifactId}" in output_argument
    assert str(execution.classpath_file.parent) in output_argument

    compile_metadata = adapter.create_compile_classpath_operation(execution)
    assert any(
        value.endswith("maven-help-plugin:3.2.0:effective-pom")
        for value in compile_metadata.argv
    )
    assert (
        f"-Doutput={execution.effective_pom_file}"
        in compile_metadata.argv
    )


def test_reactor_selects_unique_main_class_module_and_uses_pl_am(
    tmp_path: Path,
) -> None:
    project = tmp_path / "reactor"
    _write(
        project / "pom.xml",
        _pom("root", packaging="pom", modules=("api", "service")),
    )
    _write(project / "api" / "pom.xml", _pom("api"))
    _write(project / "service" / "pom.xml", _pom("service"))
    _write(
        project
        / "service"
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "Application.java",
        "package com.example; class Application {}",
    )
    adapter = MavenBuildSystemAdapter()
    workspace = adapter.resolve_workspace(project)

    selected = adapter.select_module(workspace, _intent(project))
    execution = adapter.create_execution_plan(
        workspace=workspace,
        module=selected,
        intent=_intent(project),
        maven=_maven(tmp_path),
        build_jdk=_jdk(tmp_path),
        preferences=IdeaBuildPreferences(),
        attempt_directory=tmp_path / "attempt",
    )
    arguments = adapter.create_build_operation(execution).argv

    assert selected.relative_path == "service"
    assert arguments[arguments.index("-pl") + 1] == "service"
    assert "-am" in arguments


def test_reactor_never_guesses_between_multiple_runnable_modules(
    tmp_path: Path,
) -> None:
    project = tmp_path / "reactor"
    _write(
        project / "pom.xml",
        _pom("root", packaging="pom", modules=("one", "two")),
    )
    for name in ("one", "two"):
        _write(project / name / "pom.xml", _pom(name))
    workspace = MavenBuildSystemAdapter().resolve_workspace(project)

    with pytest.raises(MavenResolutionError) as captured:
        MavenBuildSystemAdapter().select_module(
            workspace,
            _intent(project),
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.AMBIGUOUS_BUILD_MODULE
    )
    assert {
        item["relative_path"]
        for item in captured.value.context["candidates"]
    } == {"one", "two"}


def test_single_aggregator_is_not_treated_as_a_runnable_module(
    tmp_path: Path,
) -> None:
    project = tmp_path / "aggregator"
    _write(project / "pom.xml", _pom("root", packaging="pom"))
    adapter = MavenBuildSystemAdapter()
    workspace = adapter.resolve_workspace(project)

    with pytest.raises(MavenResolutionError) as captured:
        adapter.select_module(workspace, _intent(project))

    assert captured.value.error_code is LaunchErrorCode.BUILD_MODULE_NOT_FOUND


def test_profile_controlled_or_escaping_modules_are_rejected(
    tmp_path: Path,
) -> None:
    project = tmp_path / "reactor"
    _write(
        project / "pom.xml",
        """
        <project xmlns="http://maven.apache.org/POM/4.0.0">
          <modelVersion>4.0.0</modelVersion>
          <groupId>example</groupId>
          <artifactId>root</artifactId>
          <version>1</version>
          <profiles>
            <profile>
              <id>dynamic</id>
              <modules><module>service</module></modules>
            </profile>
          </profiles>
        </project>
        """,
    )

    with pytest.raises(MavenResolutionError) as captured:
        MavenBuildSystemAdapter().resolve_workspace(project)

    assert (
        captured.value.error_code
        is LaunchErrorCode.UNSUPPORTED_BUILD_MODEL
    )


def test_classpath_result_is_verified_before_jvm_plan_is_published(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write(project / "pom.xml", _pom("app"))
    adapter = MavenBuildSystemAdapter()
    workspace = adapter.resolve_workspace(project)
    intent = _intent(project)
    module = adapter.select_module(workspace, intent)
    execution = adapter.create_execution_plan(
        workspace=workspace,
        module=module,
        intent=intent,
        maven=_maven(tmp_path),
        build_jdk=_jdk(tmp_path),
        preferences=IdeaBuildPreferences(),
        attempt_directory=tmp_path / "attempt",
    )
    dependency = tmp_path / "repository" / "dependency.jar"
    dependency.parent.mkdir()
    dependency.write_bytes(b"jar")
    main_class = (
        module.output_directory / "com" / "example" / "Application.class"
    )
    main_class.parent.mkdir(parents=True)
    main_class.write_bytes(b"class")
    execution.classpath_file.write_text(
        str(dependency),
        encoding="utf-8",
    )

    plan = adapter.consume_jvm_launch_plan(
        execution=execution,
        intent=intent,
        runtime_jdk=_jdk(tmp_path),
        ready_port=8080,
        startup_wait_timeout_seconds=12,
    )

    assert plan.classpath == (
        module.output_directory.resolve(),
        dependency.resolve(),
    )
    assert plan.main_class == "com.example.Application"
    assert plan.ready_port == 8080
    assert plan.startup_wait_timeout_seconds == 12


def test_missing_classpath_entry_returns_structured_resolution_failure(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write(project / "pom.xml", _pom("app"))
    adapter = MavenBuildSystemAdapter()
    workspace = adapter.resolve_workspace(project)
    intent = _intent(project)
    module = adapter.select_module(workspace, intent)
    execution = adapter.create_execution_plan(
        workspace=workspace,
        module=module,
        intent=intent,
        maven=_maven(tmp_path),
        build_jdk=_jdk(tmp_path),
        preferences=IdeaBuildPreferences(),
        attempt_directory=tmp_path / "attempt",
    )
    module.output_directory.mkdir(parents=True)
    execution.classpath_file.write_text(
        str(tmp_path / "missing.jar"),
        encoding="utf-8",
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_jvm_launch_plan(
            execution=execution,
            intent=intent,
            runtime_jdk=_jdk(tmp_path),
            ready_port=0,
            startup_wait_timeout_seconds=30,
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.RUNTIME_RESOLUTION_FAILED
    )
    assert captured.value.context["missing_path_count"] == 1


def test_fast_compile_plan_does_not_widen_maven_compile_classpath(
    tmp_path: Path,
) -> None:
    project = tmp_path / "reactor-fast"
    _write(
        project / "pom.xml",
        _pom(
            "root",
            packaging="pom",
            modules=("required", "app", "unrelated"),
        ),
    )
    _write(project / "required/pom.xml", _pom("required"))
    _write(project / "app/pom.xml", _pom("app"))
    _write(project / "unrelated/pom.xml", _pom("unrelated"))
    _write(
        project / "app/src/main/java/com/example/Application.java",
        "package com.example; public class Application {}\n",
    )
    adapter = MavenBuildSystemAdapter()
    workspace = adapter.resolve_workspace(project)
    intent = _intent(project, module="app")
    module = adapter.select_module(workspace, intent)
    execution = adapter.create_execution_plan(
        workspace=workspace,
        module=module,
        intent=intent,
        maven=_maven(tmp_path),
        build_jdk=_jdk(tmp_path),
        preferences=IdeaBuildPreferences(),
        attempt_directory=tmp_path / "reactor-attempt",
    )
    outputs = {
        item.artifact_id: item.output_directory
        for item in workspace.modules
        if item.packaging != "pom"
    }
    for output in outputs.values():
        output.mkdir(parents=True)
    app_class = outputs["app"] / "com/example/Application.class"
    app_class.parent.mkdir(parents=True)
    app_class.write_bytes(_class_header(8))
    provided = tmp_path / "provided-classes"
    provided.mkdir()
    stale_required = (
        tmp_path
        / "repository"
        / "example"
        / "required"
        / "1.0"
        / "required-1.0.jar"
    )
    stale_required.parent.mkdir(parents=True)
    stale_required.write_bytes(b"stale")
    execution.compile_classpath_file.write_text(
        os.pathsep.join((str(stale_required), str(provided))),
        encoding="utf-8",
    )
    execution.effective_pom_file.write_text(
        f"""\
<projects>
  {_effective_pom("required")}
  <project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <groupId>example</groupId>
    <artifactId>app</artifactId>
    <version>1.0</version>
    <properties>
      <maven.compiler.source>1.8</maven.compiler.source>
      <maven.compiler.target>1.8</maven.compiler.target>
      <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
    </properties>
    <dependencies>
      <dependency>
        <groupId>example</groupId>
        <artifactId>required</artifactId>
        <version>1.0</version>
      </dependency>
    </dependencies>
  </project>
</projects>
""",
        encoding="utf-8",
    )

    plan = adapter.consume_fast_compile_plan(
        execution=execution,
        runtime_jdk=_jdk(tmp_path),
    )

    assert plan.compile_classpath == (
        outputs["app"].resolve(),
        outputs["required"].resolve(),
        provided.resolve(),
    )
    assert outputs["unrelated"].resolve() not in plan.compile_classpath
    assert stale_required.resolve() not in plan.compile_classpath


def test_fast_compile_rejects_processor_service_on_compile_classpath(
    tmp_path: Path,
) -> None:
    processor_jar = tmp_path / "processor.jar"
    with zipfile.ZipFile(processor_jar, "w") as archive:
        archive.writestr(
            "META-INF/services/javax.annotation.processing.Processor",
            "example.BodyChangingProcessor\n",
        )
    adapter, execution = _fast_compile_execution(
        tmp_path,
        compile_entries=(processor_jar,),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert captured.value.error_code is (
        LaunchErrorCode
        .ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED
    )
    assert captured.value.retryable is False


def test_jdt_build_world_accepts_classpath_processors_and_separates_lombok(
    tmp_path: Path,
) -> None:
    processor_jar = tmp_path / "processor.jar"
    with zipfile.ZipFile(processor_jar, "w") as archive:
        archive.writestr("example/Processor.class", b"class")
        archive.writestr(
            "META-INF/services/javax.annotation.processing.Processor",
            "example.ConfigurationProcessor\n",
        )
    lombok_jar = tmp_path / "lombok.jar"
    with zipfile.ZipFile(lombok_jar, "w") as archive:
        archive.writestr("lombok/Launcher.class", b"class")
        archive.writestr(
            "META-INF/services/javax.annotation.processing.Processor",
            "lombok.launch.AnnotationProcessorHider$AnnotationProcessor\n",
        )
    descriptor = tmp_path / "descriptor.pom"
    descriptor.write_text(
        "<project><modelVersion>4.0.0</modelVersion></project>",
        encoding="utf-8",
    )
    adapter, execution = _fast_compile_execution(
        tmp_path,
        compile_entries=(processor_jar, lombok_jar, descriptor),
    )

    plan = adapter.consume_jdt_build_world_plan(
        execution=execution,
        runtime_jdk=_jdk(tmp_path),
    )

    assert plan.processor_entries == (processor_jar.resolve(),)
    assert plan.lombok_entries == (lombok_jar.resolve(),)
    assert descriptor.resolve() not in plan.dependency_entries
    assert plan.source_level == 8
    assert plan.target_level == 8


def test_reload_models_reject_make_disabled_stale_maven_baseline(
    tmp_path: Path,
) -> None:
    dependency = tmp_path / "dependency.jar"
    with zipfile.ZipFile(dependency, "w") as archive:
        archive.writestr("example/Dependency.class", b"class")
    adapter, execution = _fast_compile_execution(
        tmp_path,
        compile_entries=(dependency,),
    )
    execution = replace(
        execution,
        build_plan=replace(execution.build_plan, compile_required=False),
    )

    for consume in (
        adapter.consume_fast_compile_plan,
        adapter.consume_jdt_build_world_plan,
    ):
        with pytest.raises(MavenResolutionError) as captured:
            consume(execution=execution, runtime_jdk=_jdk(tmp_path))
        assert captured.value.error_code is (
            LaunchErrorCode.JDT_RELOAD_REQUIRES_FRESH_MAVEN_BASELINE
        )


def test_jdt_build_world_rejects_explicit_processor_selection(
    tmp_path: Path,
) -> None:
    effective = _effective_pom(
        "app",
        compiler_configuration=(
            "<annotationProcessors>"
            "<annotationProcessor>example.Processor</annotationProcessor>"
            "</annotationProcessors>"
        ),
    )
    dependency = tmp_path / "dependency.jar"
    with zipfile.ZipFile(dependency, "w") as archive:
        archive.writestr("example/Dependency.class", b"class")
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=effective,
        compile_entries=(dependency,),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_jdt_build_world_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert captured.value.error_code is (
        LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED
    )


def test_jdt_build_world_maps_safe_compiler_process_heap_argument(
    tmp_path: Path,
) -> None:
    dependency = tmp_path / "dependency.jar"
    with zipfile.ZipFile(dependency, "w") as archive:
        archive.writestr("example/Dependency.class", b"class")
    effective = _effective_pom(
        "app",
        compiler_configuration=(
            "<optimize>true</optimize>"
            "<compilerArgs><arg>-J-Xms512m</arg>"
            "<arg>-J-Xmx3g</arg></compilerArgs>"
        ),
    )
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=effective,
        compile_entries=(dependency,),
    )

    plan = adapter.consume_jdt_build_world_plan(
        execution=execution,
        runtime_jdk=_jdk(tmp_path),
    )

    assert plan.worker_min_heap_mb == 512
    assert plan.worker_max_heap_mb == 3072


def test_jdt_freshness_includes_maven_project_and_user_configuration(
    tmp_path: Path,
) -> None:
    dependency = tmp_path / "dependency.jar"
    with zipfile.ZipFile(dependency, "w") as archive:
        archive.writestr("example/Dependency.class", b"class")
    adapter, execution = _fast_compile_execution(
        tmp_path,
        compile_entries=(dependency,),
    )
    settings = tmp_path / "settings.xml"
    settings.write_text("<settings/>", encoding="utf-8")
    execution = replace(
        execution,
        preferences=replace(
            execution.preferences,
            user_settings_file=settings,
        ),
    )

    plan = adapter.consume_jdt_build_world_plan(
        execution=execution,
        runtime_jdk=_jdk(tmp_path),
    )

    expected = {
        execution.workspace.build_root / ".mvn/maven.config",
        execution.workspace.build_root / ".mvn/jvm.config",
        execution.workspace.build_root / ".mvn/extensions.xml",
        settings,
    }
    assert expected.issubset(set(plan.configuration_inputs))
    assert plan.is_fresh() is True
    settings.write_text("<settings><offline>true</offline></settings>", encoding="utf-8")
    assert plan.is_fresh() is False


def test_jdt_build_world_rejects_unmodeled_javac_process_argument(
    tmp_path: Path,
) -> None:
    dependency = tmp_path / "dependency.jar"
    with zipfile.ZipFile(dependency, "w") as archive:
        archive.writestr("example/Dependency.class", b"class")
    effective = _effective_pom(
        "app",
        compiler_configuration=(
            "<compilerArgs><arg>-J-Dcustom.option=true</arg></compilerArgs>"
        ),
    )
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=effective,
        compile_entries=(dependency,),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_jdt_build_world_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert captured.value.error_code is (
        LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED
    )


def test_fast_compile_rejects_explicit_processor_or_transform_plugin(
    tmp_path: Path,
) -> None:
    processor_config = """\
<annotationProcessorPaths>
  <path>
    <groupId>org.projectlombok</groupId>
    <artifactId>lombok</artifactId>
    <version>1.18.36</version>
  </path>
</annotationProcessorPaths>
"""
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            compiler_configuration=processor_config,
        ),
    )
    with pytest.raises(MavenResolutionError) as processor:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )
    assert processor.value.error_code is (
        LaunchErrorCode
        .ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED
    )

    execution.effective_pom_file.write_text(
        _effective_pom(
            "app",
            extra_plugins="""\
<plugin>
  <groupId>org.codehaus.mojo</groupId>
  <artifactId>aspectj-maven-plugin</artifactId>
</plugin>
""",
        ),
        encoding="utf-8",
    )
    with pytest.raises(MavenResolutionError) as transformer:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )
    assert transformer.value.error_code is (
        LaunchErrorCode
        .ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED
    )


def test_fast_compile_rejects_unknown_compile_phase_transformer(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            extra_plugins="""\
<plugin>
  <groupId>com.example.build</groupId>
  <artifactId>acme-class-rewriter</artifactId>
  <executions>
    <execution>
      <phase>compile</phase>
      <goals><goal>apply</goal></goals>
    </execution>
  </executions>
</plugin>
""",
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert captured.value.error_code is (
        LaunchErrorCode
        .ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED
    )


@pytest.mark.parametrize(
    "phase",
    (
        "validate",
        "initialize",
        "generate-sources",
        "process-sources",
        "generate-resources",
        "process-resources",
        "compile",
        "process-classes",
    ),
)
def test_fast_compile_rejects_unmodeled_early_lifecycle_execution(
    tmp_path: Path,
    phase: str,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            extra_plugins=f"""\
<plugin>
  <groupId>com.example.build</groupId>
  <artifactId>acme-source-weaver</artifactId>
  <executions>
    <execution>
      <phase>{phase}</phase>
      <goals><goal>run</goal></goals>
    </execution>
  </executions>
</plugin>
""",
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert captured.value.error_code is (
        LaunchErrorCode
        .ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED
    )


@pytest.mark.parametrize(
    ("group_id", "artifact_id", "phase", "goals"),
    (
        (
            "org.apache.maven.plugins",
            "maven-toolchains-plugin",
            "validate",
            "<goal>toolchain</goal>",
        ),
        (
            "org.apache.maven.plugins",
            "maven-toolchains-plugin",
            "",
            "<goal>toolchain</goal>",
        ),
        (
            "com.evil",
            "maven-enforcer-plugin",
            "validate",
            "<goal>enforce</goal>",
        ),
        (
            "org.apache.maven.plugins",
            "maven-enforcer-plugin",
            "validate",
            "<goal>enforce</goal><goal>mutate</goal>",
        ),
    ),
)
def test_fast_compile_rejects_unmodeled_early_lifecycle_identity(
    tmp_path: Path,
    group_id: str,
    artifact_id: str,
    phase: str,
    goals: str,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            extra_plugins=f"""\
<plugin>
  <groupId>{group_id}</groupId>
  <artifactId>{artifact_id}</artifactId>
  <executions>
    <execution>
      <phase>{phase}</phase>
      <goals>{goals}</goals>
    </execution>
  </executions>
</plugin>
""",
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert captured.value.error_code is (
        LaunchErrorCode
        .ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED
    )


def test_fast_compile_rejects_unresolved_lifecycle_phase(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            extra_plugins="""\
<plugin>
  <groupId>com.example.build</groupId>
  <artifactId>acme-hook</artifactId>
  <executions>
    <execution>
      <phase>${custom.phase}</phase>
      <goals><goal>run</goal></goals>
    </execution>
  </executions>
</plugin>
""",
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert captured.value.error_code is (
        LaunchErrorCode
        .ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED
    )


def test_fast_compile_allows_modeled_early_lifecycle_executions(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            extra_plugins="""\
<plugin>
  <artifactId>maven-enforcer-plugin</artifactId>
  <executions>
    <execution>
      <phase>validate</phase>
      <goals><goal>enforce</goal></goals>
    </execution>
  </executions>
</plugin>
<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-resources-plugin</artifactId>
  <executions>
    <execution>
      <phase>process-resources</phase>
      <goals><goal>resources</goal></goals>
    </execution>
  </executions>
</plugin>
""",
        ),
    )

    plan = adapter.consume_fast_compile_plan(
        execution=execution,
        runtime_jdk=_jdk(tmp_path),
    )

    assert plan.encoding == "UTF-8"


def test_fast_compile_ignores_unknown_execution_after_class_processing(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            extra_plugins="""\
<plugin>
  <groupId>com.example.build</groupId>
  <artifactId>acme-test-reporter</artifactId>
  <executions>
    <execution>
      <phase>test</phase>
      <goals><goal>report</goal></goals>
    </execution>
  </executions>
</plugin>
""",
        ),
    )

    plan = adapter.consume_fast_compile_plan(
        execution=execution,
        runtime_jdk=_jdk(tmp_path),
    )

    assert plan.encoding == "UTF-8"


def test_fast_compile_rejects_unknown_plugin_with_implicit_phase(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            extra_plugins="""\
<plugin>
  <groupId>com.example.build</groupId>
  <artifactId>acme-class-rewriter</artifactId>
  <executions>
    <execution>
      <goals><goal>apply</goal></goals>
    </execution>
  </executions>
</plugin>
""",
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert captured.value.error_code is (
        LaunchErrorCode
        .ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED
    )


def test_fast_compile_allows_source_jar_no_fork_with_implicit_package_phase(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            extra_plugins="""\
<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-source-plugin</artifactId>
  <version>2.2.1</version>
  <executions>
    <execution>
      <id>attach-sources</id>
      <goals><goal>jar-no-fork</goal></goals>
    </execution>
  </executions>
</plugin>
""",
        ),
    )

    plan = adapter.consume_fast_compile_plan(
        execution=execution,
        runtime_jdk=_jdk(tmp_path),
    )

    assert plan.encoding == "UTF-8"


@pytest.mark.parametrize(
    ("goal", "phase_xml"),
    (
        ("jar", ""),
        ("jar-no-fork", "<phase>generate-sources</phase>"),
    ),
)
def test_fast_compile_does_not_overgeneralize_source_plugin_allowance(
    tmp_path: Path,
    goal: str,
    phase_xml: str,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            extra_plugins=f"""\
<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-source-plugin</artifactId>
  <version>2.2.1</version>
  <executions>
    <execution>
      {phase_xml}
      <goals><goal>{goal}</goal></goals>
    </execution>
  </executions>
</plugin>
""",
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert captured.value.error_code is (
        LaunchErrorCode
        .ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED
    )


@pytest.mark.parametrize(
    "phase_xml",
    ("", "<phase>generate-sources</phase>"),
)
def test_fast_compile_rejects_unmodeled_source_root_change(
    tmp_path: Path,
    phase_xml: str,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            extra_plugins="""\
<plugin>
  <groupId>org.codehaus.mojo</groupId>
  <artifactId>build-helper-maven-plugin</artifactId>
  <executions>
    <execution>
      {phase_xml}
      <goals><goal>add-source</goal></goals>
    </execution>
  </executions>
</plugin>
""",
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert captured.value.error_code is (
        LaunchErrorCode
        .ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED
    )


def test_fast_compile_rejects_implicit_property_mutation(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            extra_plugins="""\
<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-dependency-plugin</artifactId>
  <executions>
    <execution>
      <goals><goal>properties</goal></goals>
    </execution>
  </executions>
</plugin>
""",
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert captured.value.error_code is (
        LaunchErrorCode
        .ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED
    )


def test_fast_compile_rejects_unmodeled_known_plugin_goal(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            extra_plugins="""\
<plugin>
  <groupId>org.springframework.boot</groupId>
  <artifactId>spring-boot-maven-plugin</artifactId>
  <executions>
    <execution>
      <goals><goal>process-aot</goal></goals>
    </execution>
  </executions>
</plugin>
""",
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert captured.value.error_code is (
        LaunchErrorCode
        .ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED
    )


def test_fast_compile_rejects_classifier_reactor_dependency(
    tmp_path: Path,
) -> None:
    project = tmp_path / "classifier-reactor"
    _write(
        project / "pom.xml",
        _pom("root", packaging="pom", modules=("shared", "app")),
    )
    _write(project / "shared/pom.xml", _pom("shared"))
    _write(project / "app/pom.xml", _pom("app"))
    _write(
        project / "app/src/main/java/com/example/Application.java",
        "package com.example; public class Application {}\n",
    )
    adapter = MavenBuildSystemAdapter()
    workspace = adapter.resolve_workspace(project)
    module = adapter.select_module(
        workspace,
        _intent(project, module="app"),
    )
    execution = adapter.create_execution_plan(
        workspace=workspace,
        module=module,
        intent=_intent(project, module="app"),
        maven=_maven(tmp_path),
        build_jdk=_jdk(tmp_path),
        preferences=IdeaBuildPreferences(),
        attempt_directory=tmp_path / "classifier-attempt",
    )
    module.output_directory.mkdir(parents=True)
    class_file = (
        module.output_directory / "com/example/Application.class"
    )
    class_file.parent.mkdir(parents=True)
    class_file.write_bytes(_class_header(8))
    classifier_jar = (
        tmp_path
        / "repository/example/shared/1.0/shared-1.0-client.jar"
    )
    classifier_jar.parent.mkdir(parents=True)
    classifier_jar.write_bytes(b"classifier")
    execution.compile_classpath_file.write_text(
        str(classifier_jar),
        encoding="utf-8",
    )
    execution.effective_pom_file.write_text(
        f"""\
<projects>
  {_effective_pom("shared")}
  <project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <groupId>example</groupId>
    <artifactId>app</artifactId>
    <version>1.0</version>
    <properties>
      <maven.compiler.source>1.8</maven.compiler.source>
      <maven.compiler.target>1.8</maven.compiler.target>
      <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
    </properties>
    <dependencies>
      <dependency>
        <groupId>example</groupId>
        <artifactId>shared</artifactId>
        <version>1.0</version>
        <classifier>client</classifier>
      </dependency>
    </dependencies>
  </project>
</projects>
""",
        encoding="utf-8",
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED
    )


@pytest.mark.parametrize(
    "compiler_configuration",
    [
        "<includes><include>**/Application.java</include></includes>",
        "<jdkToolchain><version>17</version></jdkToolchain>",
        "<sourcepath>generated-sources</sourcepath>",
    ],
)
def test_fast_compile_rejects_unmodeled_compiler_configuration(
    tmp_path: Path,
    compiler_configuration: str,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            compiler_configuration=compiler_configuration,
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert captured.value.error_code is (
        LaunchErrorCode
        .ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED
    )


@pytest.mark.parametrize(
    ("property_name", "property_value"),
    (
        ("maven.compiler.compilerId", "eclipse"),
        ("maven.compiler.fork", "true"),
        ("maven.compiler.executable", "/custom/javac"),
        ("maven.compiler.debug", "false"),
        ("maven.compiler.debuglevel", "lines,vars"),
        ("maven.compiler.enablePreview", "true"),
        ("maven.compiler.forceJavacCompilerUse", "true"),
        ("maven.compiler.forceLegacyJavacApi", "true"),
    ),
)
def test_fast_compile_rejects_unreproduced_compiler_user_property(
    tmp_path: Path,
    property_name: str,
    property_value: str,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            compiler_properties=(
                f"<{property_name}>{property_value}</{property_name}>"
            ),
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED
    )


@pytest.mark.parametrize(
    "compiler_configuration",
    (
        "<compilerId>eclipse</compilerId>",
        "<fork>true</fork>",
        "<executable>/custom/javac</executable>",
        "<debug>false</debug>",
        "<debuglevel>lines,vars</debuglevel>",
        "<enablePreview>true</enablePreview>",
        "<forceJavacCompilerUse>true</forceJavacCompilerUse>",
        "<forceLegacyJavacApi>true</forceLegacyJavacApi>",
    ),
)
def test_fast_compile_rejects_unreproduced_compiler_configuration(
    tmp_path: Path,
    compiler_configuration: str,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            compiler_configuration=compiler_configuration,
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED
    )


def test_fast_compile_allows_reproducible_debuglevel(tmp_path: Path) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            compiler_properties=(
                "<maven.compiler.debuglevel>source,lines,vars"
                "</maven.compiler.debuglevel>"
            ),
        ),
    )

    plan = adapter.consume_fast_compile_plan(
        execution=execution,
        runtime_jdk=_jdk(tmp_path),
    )

    assert plan.encoding == "UTF-8"


@pytest.mark.parametrize(
    "extension_xml",
    (
        """\
<plugin>
  <groupId>com.example</groupId>
  <artifactId>custom-extension</artifactId>
  <extensions>true</extensions>
</plugin>
""",
        """\
<plugin>
  <groupId>com.example</groupId>
  <artifactId>custom-extension</artifactId>
  <extensions>${extension.enabled}</extensions>
</plugin>
""",
    ),
)
def test_fast_compile_rejects_plugin_build_extension(
    tmp_path: Path,
    extension_xml: str,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            extra_plugins=extension_xml,
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED
    )


def test_fast_compile_rejects_project_build_extension(tmp_path: Path) -> None:
    effective_pom = _effective_pom("app").replace(
        "<build>",
        """\
<build>
  <extensions>
    <extension>
      <groupId>com.example</groupId>
      <artifactId>custom-extension</artifactId>
      <version>1.0</version>
    </extension>
  </extensions>
""",
        1,
    )
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=effective_pom,
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED
    )


def test_fast_compile_rejects_maven_extensions_xml(tmp_path: Path) -> None:
    adapter, execution = _fast_compile_execution(tmp_path)
    _write(
        execution.workspace.build_root / ".mvn/extensions.xml",
        """\
<extensions>
  <extension>
    <groupId>com.example</groupId>
    <artifactId>custom-extension</artifactId>
    <version>1.0</version>
  </extension>
</extensions>
""",
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED
    )


def test_fast_compile_rejects_effective_core_extension_property(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            compiler_properties=(
                "<maven.ext.class.path>extension.jar"
                "</maven.ext.class.path>"
            ),
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED
    )


@pytest.mark.parametrize(
    ("config_name", "content"),
    (
        ("maven.config", "-Dmaven.compiler.compilerId=eclipse\n"),
        ("maven.config", "-Dmaven.ext.class.path=extension.jar\n"),
        ("jvm.config", "-Dmaven.compiler.enablePreview=true\n"),
        ("jvm.config", "-javaagent:compiler-agent.jar\n"),
    ),
)
def test_fast_compile_rejects_unmodeled_maven_project_argument(
    tmp_path: Path,
    config_name: str,
    content: str,
) -> None:
    adapter, execution = _fast_compile_execution(tmp_path)
    _write(
        execution.workspace.build_root / ".mvn" / config_name,
        content,
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED
    )


def test_maven_project_configuration_creation_makes_plan_stale(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(tmp_path)
    plan = adapter.consume_fast_compile_plan(
        execution=execution,
        runtime_jdk=_jdk(tmp_path),
    )

    assert plan.is_fresh() is True
    _write(
        execution.workspace.build_root / ".mvn/maven.config",
        "-T 2\n",
    )
    assert plan.is_fresh() is False


def test_maven_environment_change_makes_plan_stale(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("MAVEN_ARGS", raising=False)
    monkeypatch.delenv("MAVEN_OPTS", raising=False)
    adapter, execution = _fast_compile_execution(tmp_path)
    plan = adapter.consume_fast_compile_plan(
        execution=execution,
        runtime_jdk=_jdk(tmp_path),
    )

    assert plan.is_fresh() is True
    monkeypatch.setenv("MAVEN_OPTS", "-Xmx2g")
    assert plan.is_fresh() is False


@pytest.mark.parametrize("name", ("MAVEN_ARGS", "MAVEN_OPTS"))
def test_fast_compile_rejects_unmodeled_maven_environment_argument(
    tmp_path: Path,
    monkeypatch,
    name: str,
) -> None:
    monkeypatch.setenv(name, "-Dmaven.compiler.compilerId=eclipse")
    adapter, execution = _fast_compile_execution(tmp_path)

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED
    )


def test_explicit_proc_none_allows_processor_dependency(
    tmp_path: Path,
) -> None:
    processor_jar = tmp_path / "disabled-processor.jar"
    with zipfile.ZipFile(processor_jar, "w") as archive:
        archive.writestr(
            "META-INF/services/javax.annotation.processing.Processor",
            "example.DisabledProcessor\n",
        )
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            compiler_configuration="<proc>none</proc>",
        ),
        compile_entries=(processor_jar,),
    )

    plan = adapter.consume_fast_compile_plan(
        execution=execution,
        runtime_jdk=_jdk(tmp_path),
    )

    assert processor_jar.resolve() in plan.compile_classpath
    assert plan.javac_platform_args == (
        "-source",
        "8",
        "-target",
        "8",
    )


def test_fast_compile_uses_effective_compiler_encoding(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            compiler_configuration="<encoding>GBK</encoding>",
        ),
    )

    plan = adapter.consume_fast_compile_plan(
        execution=execution,
        runtime_jdk=_jdk(tmp_path),
    )

    assert plan.encoding == "GBK"


def test_fast_compile_uses_canonical_encoding_property(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            source_encoding=None,
            compiler_properties="<encoding>GBK</encoding>",
        ),
    )

    plan = adapter.consume_fast_compile_plan(
        execution=execution,
        runtime_jdk=_jdk(tmp_path),
    )

    assert plan.encoding == "GBK"


def test_fast_compile_does_not_treat_noncanonical_encoding_as_override(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            source_encoding="UTF-8",
            compiler_properties=(
                "<maven.compiler.encoding>GBK</maven.compiler.encoding>"
            ),
        ),
    )

    plan = adapter.consume_fast_compile_plan(
        execution=execution,
        runtime_jdk=_jdk(tmp_path),
    )

    assert plan.encoding == "UTF-8"


@pytest.mark.parametrize("source_encoding", (None, "${legacy.encoding}"))
def test_fast_compile_rejects_unverified_source_encoding(
    tmp_path: Path,
    source_encoding: str | None,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            source_encoding=source_encoding,
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED
    )


def test_fast_compile_rejects_unresolved_compiler_encoding_override(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            source_encoding="UTF-8",
            compiler_configuration=(
                "<encoding>${legacy.encoding}</encoding>"
            ),
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED
    )


def test_fast_compile_rejects_host_unsupported_source_encoding(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            source_encoding="jolink-not-a-real-encoding",
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.UNSUPPORTED_BUILD_MODEL
    )


@pytest.mark.parametrize(
    ("compiler_properties", "compiler_configuration"),
    (
        ("", "<failOnWarning>true</failOnWarning>"),
        (
            "<maven.compiler.failOnWarning>true"
            "</maven.compiler.failOnWarning>",
            "",
        ),
        ("", "<failOnWarning>${warnings.fail}</failOnWarning>"),
        (
            "<maven.compiler.failOnWarning>${warnings.fail}"
            "</maven.compiler.failOnWarning>",
            "",
        ),
        (
            "<maven.compiler.failOnWarning>yes"
            "</maven.compiler.failOnWarning>",
            "",
        ),
    ),
)
def test_fast_compile_rejects_unreproduced_fail_on_warning(
    tmp_path: Path,
    compiler_properties: str,
    compiler_configuration: str,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            compiler_properties=compiler_properties,
            compiler_configuration=compiler_configuration,
        ),
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED
    )


def test_fast_compile_allows_explicit_fail_on_warning_false(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(
        tmp_path,
        effective_pom=_effective_pom(
            "app",
            compiler_configuration="<failOnWarning>false</failOnWarning>",
        ),
    )

    plan = adapter.consume_fast_compile_plan(
        execution=execution,
        runtime_jdk=_jdk(tmp_path),
    )

    assert plan.encoding == "UTF-8"


def test_fast_compile_rejects_jpms_module(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(tmp_path)
    _write(
        execution.module.directory / "src/main/java/module-info.java",
        "module example.app {}\n",
    )

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=_jdk(tmp_path),
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.FAST_COMPILE_MODEL_UNVERIFIED
    )


def test_jdk9_plus_fast_compile_uses_release_for_target_api(
    tmp_path: Path,
) -> None:
    build_jdk = _jdk(tmp_path, name="jdk17", version="17.0.12")
    runtime_jdk = _jdk(tmp_path, name="runtime8", version="1.8.0_402")
    adapter, execution = _fast_compile_execution(
        tmp_path,
        build_jdk=build_jdk,
    )

    plan = adapter.consume_fast_compile_plan(
        execution=execution,
        runtime_jdk=runtime_jdk,
    )

    assert plan.build_jdk_major == 17
    assert plan.runtime_jdk_major == 8
    assert plan.target_level == 8
    assert plan.javac_platform_args == ("--release", "8")


def test_jdk8_fast_compile_rejects_an_unmatched_runtime_platform(
    tmp_path: Path,
) -> None:
    adapter, execution = _fast_compile_execution(tmp_path)
    runtime_jdk = _jdk(tmp_path, name="runtime17", version="17.0.12")

    with pytest.raises(MavenResolutionError) as captured:
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=runtime_jdk,
        )

    assert (
        captured.value.error_code
        is LaunchErrorCode.FAST_COMPILE_JDK_INCOMPATIBLE
    )
