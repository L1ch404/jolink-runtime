from __future__ import annotations

import os
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


def _jdk(tmp_path: Path) -> JavaToolchainCandidate:
    home = tmp_path / "jdk"
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
