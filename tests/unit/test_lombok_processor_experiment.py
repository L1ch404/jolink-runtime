from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from jolink_runtime.experiments import compile as experiment_cli
from jolink_runtime.experiments import lombok_processor as experiment
from jolink_runtime.launch import (
    IdeaBuildPreferences,
    JavaToolchainCandidate,
    LaunchIntent,
    MavenBuildSystemAdapter,
    MavenResolutionError,
    MavenToolCandidate,
)
from jolink_runtime.launch.process_supervisor import OperationResult


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _lombok_jar(
    path: Path,
    *,
    providers: tuple[str, ...] = tuple(experiment._LOMBOK_PROCESSORS),
    version: str = "1.18.36",
    manifest_extra: str = "",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            experiment._PROCESSOR_SERVICE,
            "\n".join(providers) + "\n",
        )
        for provider in providers:
            if provider in experiment._LOMBOK_PROCESSORS:
                archive.writestr(
                    f"{provider.replace('.', '/')}.class",
                    b"processor-bytecode",
                )
        archive.writestr("lombok/Generated.class", b"lombok-bytecode")
        archive.writestr(
            "META-INF/MANIFEST.MF",
            (
                f"Manifest-Version: 1.0\nLombok-Version: {version}\n"
                f"{manifest_extra}"
            ),
        )
    return path


def _effective_pom(
    *,
    compiler_configuration: str = "",
    compiler_properties: str = "",
) -> str:
    return f"""\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>app</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.source>1.8</maven.compiler.source>
    <maven.compiler.target>1.8</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
    {compiler_properties}
  </properties>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.13.0</version>
        <configuration>{compiler_configuration}</configuration>
      </plugin>
    </plugins>
  </build>
</project>
"""


def _execution(
    tmp_path: Path,
    *,
    effective_pom: str,
    compile_entries: tuple[Path, ...],
    jdk_major: int = 8,
):
    project = tmp_path / "project"
    _write(
        project / "pom.xml",
        """\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId><artifactId>app</artifactId><version>1.0</version>
</project>
""",
    )
    _write(
        project / "src/main/java/example/App.java",
        "package example; public class App {}\n",
    )
    adapter = MavenBuildSystemAdapter()
    workspace = adapter.resolve_workspace(project)
    module = workspace.modules[0]
    jdk = tmp_path / f"jdk-{jdk_major}"
    _write(
        jdk / "release",
        f'JAVA_VERSION="{jdk_major}.0.1"\n',
    )
    candidate = JavaToolchainCandidate(
        home=jdk,
        java_executable=jdk / "bin/java",
        javac_executable=jdk / "bin/javac",
        source="test",
        detected_major_version=jdk_major,
        detected_compiler_major_version=jdk_major,
    )
    intent = LaunchIntent(
        source="experiment",
        launch_name="app",
        launch_type="java_application",
        main_class="example.App",
        working_directory=project,
        build_before_run=False,
    )
    execution = adapter.create_execution_plan(
        workspace=workspace,
        module=module,
        intent=intent,
        maven=MavenToolCandidate(argv_prefix=("mvn",), source="test"),
        build_jdk=candidate,
        preferences=IdeaBuildPreferences(),
        attempt_directory=tmp_path / "metadata",
    )
    execution.compile_classpath_file.write_text(
        os.pathsep.join(str(path) for path in compile_entries),
        encoding="utf-8",
    )
    execution.effective_pom_file.write_text(
        effective_pom,
        encoding="utf-8",
    )
    return adapter, execution


def _explicit_configuration(*, extra: str = "") -> str:
    return f"""\
<annotationProcessorPaths>
  <path>
    <groupId>org.projectlombok</groupId>
    <artifactId>lombok</artifactId>
    <version>1.18.36</version>
  </path>
</annotationProcessorPaths>
{extra}
"""


def test_fidelity_model_preserves_maven_source_target_on_jdk17(
    tmp_path: Path,
) -> None:
    lombok = _lombok_jar(tmp_path / "lombok.jar")
    adapter, execution = _execution(
        tmp_path,
        effective_pom=_effective_pom(),
        compile_entries=(lombok,),
        jdk_major=17,
    )

    plan = experiment.LombokExperimentPlanner(adapter).create_plan(execution)

    assert plan.compiler_model.platform_args == (
        "-source",
        "8",
        "-target",
        "8",
    )
    assert plan.compiler_model.release_level is None


def test_fidelity_model_uses_release_only_when_maven_declares_release(
    tmp_path: Path,
) -> None:
    lombok = _lombok_jar(tmp_path / "lombok.jar")
    adapter, execution = _execution(
        tmp_path,
        effective_pom=_effective_pom(
            compiler_configuration="<release>17</release>"
        ),
        compile_entries=(lombok,),
        jdk_major=17,
    )

    plan = experiment.LombokExperimentPlanner(adapter).create_plan(execution)

    assert plan.compiler_model.platform_args == ("--release", "17")
    assert plan.compiler_model.release_level == 17


def test_explicit_lombok_processor_is_resolved_outside_compile_classpath(
    tmp_path: Path,
) -> None:
    processor_directory = tmp_path / "resolved-processor"
    lombok = _lombok_jar(processor_directory / "lombok-1.18.36.jar")
    adapter, execution = _execution(
        tmp_path,
        effective_pom=_effective_pom(
            compiler_configuration=_explicit_configuration(
                extra=(
                    "<compilerArgs>"
                    "<arg>-Asecret.option=private-value</arg>"
                    "</compilerArgs>"
                )
            )
        ),
        compile_entries=(),
    )
    planner = experiment.LombokExperimentPlanner(adapter)

    assert planner.explicit_processor_coordinate(execution) == (
        "org.projectlombok",
        "lombok",
        "1.18.36",
    )
    plan = planner.create_plan(
        execution,
        explicit_processor_directory=processor_directory,
    )

    assert plan.dependency_classpath == ()
    assert plan.processor_model.processor_path == (lombok.resolve(),)
    summary = plan.redacted_summary()
    assert "private-value" not in repr(summary)
    processing = summary["annotation_processing"]
    assert isinstance(processing, dict)
    assert processing["processor_option_names"] == ["secret.option"]


def test_production_fast_update_still_rejects_lombok_configuration(
    tmp_path: Path,
) -> None:
    adapter, execution = _execution(
        tmp_path,
        effective_pom=_effective_pom(
            compiler_configuration=_explicit_configuration()
        ),
        compile_entries=(),
    )
    output = execution.module.output_directory / "example/App.class"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"\xca\xfe\xba\xbe\x00\x00\x00\x34")

    with pytest.raises(MavenResolutionError):
        adapter.consume_fast_compile_plan(
            execution=execution,
            runtime_jdk=execution.build_jdk,
        )


def test_implicit_processor_path_excludes_own_formal_output(
    tmp_path: Path,
) -> None:
    lombok = _lombok_jar(tmp_path / "lombok.jar")
    adapter, execution = _execution(
        tmp_path,
        effective_pom=_effective_pom(),
        compile_entries=(),
    )
    execution.compile_classpath_file.write_text(
        os.pathsep.join(
            (str(execution.module.output_directory), str(lombok))
        ),
        encoding="utf-8",
    )

    plan = experiment.LombokExperimentPlanner(adapter).create_plan(execution)

    assert execution.module.output_directory not in plan.dependency_classpath
    assert execution.module.output_directory not in (
        plan.processor_model.processor_path
    )
    assert plan.processor_model.processor_path == (lombok.resolve(),)


def test_lombok_main_and_claiming_provider_are_one_supported_distribution(
    tmp_path: Path,
) -> None:
    lombok = _lombok_jar(tmp_path / "lombok.jar")
    adapter, execution = _execution(
        tmp_path,
        effective_pom=_effective_pom(),
        compile_entries=(lombok,),
    )

    plan = experiment.LombokExperimentPlanner(adapter).create_plan(execution)

    assert set(plan.processor_model.discovered_processors) == set(
        experiment._LOMBOK_PROCESSORS
    )


def test_unknown_processor_mixed_with_lombok_is_rejected(
    tmp_path: Path,
) -> None:
    mixed = _lombok_jar(
        tmp_path / "mixed.jar",
        providers=(
            *experiment._LOMBOK_PROCESSORS,
            "example.UnverifiedProcessor",
        ),
    )
    adapter, execution = _execution(
        tmp_path,
        effective_pom=_effective_pom(),
        compile_entries=(mixed,),
    )

    with pytest.raises(experiment.LombokExperimentError) as captured:
        experiment.LombokExperimentPlanner(adapter).create_plan(execution)

    assert captured.value.error_code == (
        "MULTIPLE_ANNOTATION_PROCESSORS_UNVERIFIED"
    )


def test_lombok_processor_class_shadow_is_rejected(tmp_path: Path) -> None:
    shadow = tmp_path / "00-shadow.jar"
    with zipfile.ZipFile(shadow, "w") as archive:
        archive.writestr(
            experiment._LOMBOK_PROCESSOR_CLASS_ENTRIES[
                experiment._LOMBOK_MAIN_PROCESSOR
            ],
            b"shadow-bytecode",
        )
    lombok = _lombok_jar(tmp_path / "10-lombok.jar")
    adapter, execution = _execution(
        tmp_path,
        effective_pom=_effective_pom(),
        compile_entries=(shadow, lombok),
    )

    with pytest.raises(experiment.LombokExperimentError) as captured:
        experiment.LombokExperimentPlanner(adapter).create_plan(execution)

    assert captured.value.error_code == "PROCESSOR_PATH_UNRESOLVED"


def test_processor_manifest_class_path_is_rejected(tmp_path: Path) -> None:
    lombok = _lombok_jar(
        tmp_path / "lombok.jar",
        manifest_extra="Class-Path: hidden-helper.jar\n",
    )
    adapter, execution = _execution(
        tmp_path,
        effective_pom=_effective_pom(),
        compile_entries=(lombok,),
    )

    with pytest.raises(experiment.LombokExperimentError) as captured:
        experiment.LombokExperimentPlanner(adapter).create_plan(execution)

    assert captured.value.error_code == "PROCESSOR_PATH_UNRESOLVED"


def test_jdk23_default_discovery_rejected_but_explicit_name_supported(
    tmp_path: Path,
) -> None:
    lombok = _lombok_jar(tmp_path / "lombok.jar")
    adapter, implicit = _execution(
        tmp_path / "implicit",
        effective_pom=_effective_pom(),
        compile_entries=(lombok,),
        jdk_major=23,
    )
    with pytest.raises(experiment.LombokExperimentError) as captured:
        experiment.LombokExperimentPlanner(adapter).create_plan(implicit)
    assert captured.value.error_code == "ANNOTATION_PROCESSING_UNVERIFIED"

    explicit_root = tmp_path / "explicit"
    explicit_lombok = _lombok_jar(explicit_root / "lombok.jar")
    explicit_config = (
        "<annotationProcessors><annotationProcessor>"
        f"{experiment._LOMBOK_MAIN_PROCESSOR}"
        "</annotationProcessor></annotationProcessors>"
    )
    adapter, named = _execution(
        explicit_root,
        effective_pom=_effective_pom(
            compiler_configuration=explicit_config
        ),
        compile_entries=(explicit_lombok,),
        jdk_major=23,
    )

    plan = experiment.LombokExperimentPlanner(adapter).create_plan(named)
    assert plan.processor_model.explicit_processor_names == (
        experiment._LOMBOK_MAIN_PROCESSOR,
    )


def test_jdk23_proc_full_explicitly_enables_classpath_processing(
    tmp_path: Path,
) -> None:
    lombok = _lombok_jar(tmp_path / "lombok.jar")
    adapter, execution = _execution(
        tmp_path,
        effective_pom=_effective_pom(
            compiler_configuration="<proc>full</proc>"
        ),
        compile_entries=(lombok,),
        jdk_major=23,
    )

    plan = experiment.LombokExperimentPlanner(adapter).create_plan(execution)

    assert plan.processor_model.activation_args == ("-proc:full",)


def test_proc_none_is_never_overridden(tmp_path: Path) -> None:
    lombok = _lombok_jar(tmp_path / "lombok.jar")
    adapter, execution = _execution(
        tmp_path,
        effective_pom=_effective_pom(
            compiler_configuration="<proc>none</proc>"
        ),
        compile_entries=(lombok,),
    )

    with pytest.raises(experiment.LombokExperimentError) as captured:
        experiment.LombokExperimentPlanner(adapter).create_plan(execution)

    assert captured.value.error_code == "ANNOTATION_PROCESSING_DISABLED"


def test_lombok_config_relative_import_and_absence_are_fingerprinted(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = project / "src/main/java/example/App.java"
    _write(source, "package example; class App {}\n")
    _write(
        project / "lombok.config",
        "import config/shared.config\nconfig.stopBubbling = true\n",
    )
    imported = project / "config/shared.config"
    _write(imported, "lombok.log.fieldName = audit\n")

    first = experiment.discover_lombok_configuration(project, (source,))
    assert {item.relative_path for item in first.inputs} == {
        "config/shared.config",
        "lombok.config",
    }
    assert "src/main/java/example/lombok.config" in (
        first.absent_candidates
    )

    _write(imported, "lombok.log.fieldName = changed\n")
    second = experiment.discover_lombok_configuration(project, (source,))
    assert first.fingerprint != second.fingerprint


def test_lombok_imported_stop_bubbling_uses_last_value(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    root = project / "lombok.config"
    _write(root, "import config/first\nimport config/second\n")
    _write(
        project / "config/first",
        "config.stopBubbling = true\n",
    )
    _write(
        project / "config/second",
        "config.stopBubbling = false\n",
    )

    assert experiment._config_stops_bubbling(
        root,
        project=project.resolve(),
        active=[],
        depth=0,
    ) is False


@pytest.mark.parametrize(
    "import_value",
    (
        "/tmp/external.config",
        "~/external.config",
        "<HOME>/external.config",
        "config/archive.zip!lombok.config",
        "../outside.config",
    ),
)
def test_unfrozen_lombok_config_import_is_rejected(
    tmp_path: Path,
    import_value: str,
) -> None:
    project = tmp_path / "project"
    source = project / "src/main/java/example/App.java"
    _write(source, "package example; class App {}\n")
    _write(project / "lombok.config", f"import {import_value}\n")
    _write(tmp_path / "outside.config", "config.stopBubbling = true\n")

    with pytest.raises(experiment.LombokExperimentError) as captured:
        experiment.discover_lombok_configuration(project, (source,))

    assert captured.value.error_code == "LOMBOK_CONFIG_IMPORT_UNVERIFIED"


def test_new_source_invalidates_frozen_plan(tmp_path: Path) -> None:
    lombok = _lombok_jar(tmp_path / "lombok.jar")
    adapter, execution = _execution(
        tmp_path,
        effective_pom=_effective_pom(),
        compile_entries=(lombok,),
    )
    plan = experiment.LombokExperimentPlanner(adapter).create_plan(execution)
    _write(
        execution.module.directory / "src/main/java/example/NewType.java",
        "package example; class NewType {}\n",
    )

    with pytest.raises(experiment.LombokExperimentError) as captured:
        experiment._assert_plan_fresh(plan)

    assert captured.value.error_code == "COMPILE_MODEL_STALE"


def test_class_comparison_requires_exact_method_bytes() -> None:
    compared = experiment.compare_class_outputs(
        {"example/App.class": "old"},
        {"example/App.class": "new"},
    )

    assert compared["class_set_match"] is True
    assert compared["exact_match"] is False
    assert compared["verification_state"] == "requires_review"


def test_dependency_and_processor_artifacts_are_frozen_privately(
    tmp_path: Path,
) -> None:
    lombok = _lombok_jar(tmp_path / "repository/lombok.jar")
    adapter, execution = _execution(
        tmp_path,
        effective_pom=_effective_pom(),
        compile_entries=(lombok,),
    )
    plan = experiment.LombokExperimentPlanner(adapter).create_plan(execution)

    frozen = experiment.freeze_plan_artifacts(
        plan,
        destination=tmp_path / "private-inputs",
    )

    assert frozen.dependency_classpath != plan.dependency_classpath
    assert all(
        (tmp_path / "private-inputs") in path.parents
        for path in frozen.dependency_classpath
    )
    assert frozen.processor_model.processor_path == frozen.dependency_classpath
    lombok.write_bytes(b"changed-after-freeze")
    with pytest.raises(experiment.LombokExperimentError) as captured:
        experiment._assert_plan_fresh(frozen)
    assert captured.value.error_code == "COMPILE_MODEL_STALE"


def test_javac_argfile_rejects_line_breaks_and_nul(tmp_path: Path) -> None:
    for value in ("first\nsecond", "first\rsecond", "first\x00second"):
        with pytest.raises(experiment.LombokExperimentError) as captured:
            experiment._write_javac_argfile(tmp_path / "args", (value,))
        assert captured.value.error_code == "INVALID_COMPILER_ARGUMENT"


def test_private_workspace_snapshot_excludes_only_exact_build_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write(source / "pom.xml", "<project />")
    _write(source / "src/main/java/App.java", "class App {}\n")
    _write(
        source / "src/main/java/example/target/Keep.java",
        "package example.target; class Keep {}\n",
    )
    _write(
        source / "src/main/java/example/out/AlsoKeep.java",
        "package example.out; class AlsoKeep {}\n",
    )
    _write(source / "target/classes/App.class", "stale")
    destination = tmp_path / "snapshot"

    experiment_cli._snapshot_workspace(
        source,
        destination,
        excluded_directories=(source / "target",),
    )

    assert (destination / "src/main/java/App.java").is_file()
    assert (
        destination / "src/main/java/example/target/Keep.java"
    ).is_file()
    assert (
        destination / "src/main/java/example/out/AlsoKeep.java"
    ).is_file()
    assert not (destination / "target").exists()


def test_private_workspace_snapshot_rejects_symlinks(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write(source / "pom.xml", "<project />")

    symlink_source = tmp_path / "symlink-source"
    symlink_source.mkdir()
    (symlink_source / "linked").symlink_to(source / "pom.xml")
    with pytest.raises(experiment.LombokExperimentError):
        experiment_cli._snapshot_workspace(
            symlink_source,
            tmp_path / "symlink-snapshot",
        )


def test_workspace_mutation_during_snapshot_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    pom = source / "pom.xml"
    _write(pom, "<project />")
    original = experiment_cli._copy_workspace_directory

    def copy_then_mutate(*args, **kwargs):
        original(*args, **kwargs)
        if Path(args[0]) == source:
            pom.write_text("<project><changed /></project>", encoding="utf-8")

    monkeypatch.setattr(
        experiment_cli,
        "_copy_workspace_directory",
        copy_then_mutate,
    )

    with pytest.raises(experiment.LombokExperimentError) as captured:
        experiment_cli._snapshot_workspace(
            source,
            tmp_path / "snapshot",
        )

    assert captured.value.error_code == "WORKSPACE_CHANGED_DURING_SNAPSHOT"


def test_private_workspace_root_guard_handles_project_without_config(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    source = original / "src/main/java/example/App.java"
    _write(source, "package example; class App {}\n")
    snapshot = tmp_path / "attempt" / "workspace-snapshot"
    experiment_cli._snapshot_workspace(original, snapshot)

    experiment_cli._install_snapshot_lombok_guard(snapshot)
    copied_source = snapshot / source.relative_to(original)
    config = experiment.discover_lombok_configuration(
        snapshot,
        (copied_source,),
    )

    assert (snapshot / "lombok.config").read_text(encoding="utf-8") == (
        "config.stopBubbling = true\n"
    )
    assert {item.relative_path for item in config.inputs} == {
        "lombok.config"
    }


def test_attempt_root_must_be_outside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(experiment.LombokExperimentError) as captured:
        experiment_cli._validate_attempt_root_location(
            project / ".experiment",
            project,
        )

    assert captured.value.error_code == "INVALID_ARGUMENT"


def test_external_maven_arguments_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAVEN_ARGS", "verify")

    with pytest.raises(experiment.LombokExperimentError) as captured:
        experiment.validate_compiler_environment()

    assert captured.value.error_code == "COMPILER_ENVIRONMENT_UNVERIFIED"


def test_static_preflight_rejects_compile_phase_project_plugin(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write(
        project / "pom.xml",
        """\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId><artifactId>app</artifactId><version>1</version>
  <build><plugins><plugin>
    <groupId>org.codehaus.mojo</groupId><artifactId>exec-maven-plugin</artifactId>
    <executions><execution><phase>compile</phase>
      <goals><goal>exec</goal></goals>
    </execution></executions>
  </plugin></plugins></build>
</project>
""",
    )
    adapter = MavenBuildSystemAdapter()
    workspace = adapter.resolve_workspace(project)

    with pytest.raises(experiment.LombokExperimentError) as captured:
        experiment_cli._static_maven_preflight(adapter, workspace)

    assert captured.value.error_code == "COMPILE_MODEL_UNAVAILABLE"


def test_p0_rejects_reactor_workspace(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write(
        project / "pom.xml",
        """\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId><artifactId>parent</artifactId><version>1</version>
  <packaging>pom</packaging><modules><module>app</module></modules>
</project>
""",
    )
    _write(
        project / "app/pom.xml",
        """\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent><groupId>example</groupId><artifactId>parent</artifactId>
    <version>1</version></parent>
  <artifactId>app</artifactId>
</project>
""",
    )
    workspace = MavenBuildSystemAdapter().resolve_workspace(project)

    with pytest.raises(experiment.LombokExperimentError) as captured:
        experiment_cli._validate_p0_workspace(workspace)

    assert captured.value.error_code == "COMPILE_EXPERIMENT_UNSUPPORTED"


class _GeneratingSupervisor:
    def __init__(self) -> None:
        self.cwd: Path | None = None
        self.argv: tuple[str, ...] = ()
        self.argfile_text = ""

    def run(self, spec, *, owner):
        self.cwd = spec.cwd
        self.argv = spec.argv
        argfile_argument = next(
            value for value in spec.argv if value.startswith("@")
        )
        argfile = Path(argfile_argument[1:])
        self.argfile_text = argfile.read_text(
            encoding=os.device_encoding(0) or "utf-8"
        )
        classes = argfile.parent / "classes" / "example"
        classes.mkdir(parents=True)
        (classes / "App.class").write_bytes(b"compiled")
        return OperationResult(
            operation_name=spec.operation_name,
            return_code=0,
            cancelled=False,
            timed_out=False,
            started_at=1.0,
            finished_at=1.2,
            output_capture=spec.output_capture,
        )

    def release_owner(self, owner) -> bool:
        return True


class _FailingSupervisor:
    def run(self, spec, *, owner):
        assert spec.output_capture is not None
        spec.output_capture.write_text(
            "processor echoed secret-value and /private/project/path\n",
            encoding="utf-8",
        )
        return OperationResult(
            operation_name=spec.operation_name,
            return_code=1,
            cancelled=False,
            timed_out=False,
            started_at=1.0,
            finished_at=1.2,
            output_capture=spec.output_capture,
        )

    def release_owner(self, owner) -> bool:
        return True


def test_runner_uses_mirrored_module_and_never_old_self_output(
    tmp_path: Path,
) -> None:
    lombok = _lombok_jar(tmp_path / "lombok.jar")
    adapter, execution = _execution(
        tmp_path,
        effective_pom=_effective_pom(),
        compile_entries=(lombok,),
    )
    plan = experiment.LombokExperimentPlanner(adapter).create_plan(execution)
    supervisor = _GeneratingSupervisor()

    attempt = experiment.LombokExperimentRunner(supervisor).compile(
        plan,
        root_directory=tmp_path / "attempts",
        timeout_seconds=5,
    )

    assert attempt.class_hashes.keys() == {"example/App.class"}
    assert supervisor.cwd == (
        attempt.attempt_directory
        / "workspace"
        / execution.module.directory.relative_to(
            execution.workspace.project_root
        )
    )
    assert str(execution.module.output_directory) not in (
        supervisor.argfile_text
    )
    assert experiment._JAVAC_ARGFILE_JVM_ARGUMENT in supervisor.argv


def test_javac_failure_result_does_not_echo_private_log(
    tmp_path: Path,
) -> None:
    lombok = _lombok_jar(tmp_path / "lombok.jar")
    adapter, execution = _execution(
        tmp_path,
        effective_pom=_effective_pom(),
        compile_entries=(lombok,),
    )
    plan = experiment.LombokExperimentPlanner(adapter).create_plan(execution)

    with pytest.raises(experiment.LombokExperimentError) as captured:
        experiment.LombokExperimentRunner(_FailingSupervisor()).compile(
            plan,
            root_directory=tmp_path / "attempts",
            timeout_seconds=5,
        )

    payload = captured.value.as_dict()
    assert "secret-value" not in repr(payload)
    assert "/private/project/path" not in repr(payload)
    assert payload["private_compile_log_available"] is True


def test_fresh_baseline_operation_never_invokes_clean(tmp_path: Path) -> None:
    adapter, execution = _execution(
        tmp_path,
        effective_pom=_effective_pom(),
        compile_entries=(),
    )

    operation = experiment_cli._fresh_maven_baseline_operation(
        adapter,
        execution,
        timeout_seconds=20,
    )

    assert "compile" in operation.argv
    assert "clean" not in operation.argv
