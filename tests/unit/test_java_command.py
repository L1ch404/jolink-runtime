from __future__ import annotations

import os
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from jolink_runtime.launch import (
    JavaCommandMaterializer,
    JvmLaunchPlan,
    ProjectLaunchPipeline,
)


def _plan(
    tmp_path: Path,
    classpath: tuple[Path, ...],
) -> JvmLaunchPlan:
    return JvmLaunchPlan(
        java_executable=tmp_path / "jdk" / "bin" / (
            "java.exe" if os.name == "nt" else "java"
        ),
        classpath=classpath,
        main_class="com.example.Application",
        working_directory=tmp_path,
        jvm_args=("-Xmx256m",),
        program_args=("--server.port=0",),
    )


def _unfold_manifest_header(manifest: bytes, name: str) -> str:
    text = manifest.decode("utf-8")
    unfolded = text.replace("\r\n ", "")
    prefix = f"{name}: "
    return next(
        line[len(prefix) :]
        for line in unfolded.split("\r\n")
        if line.startswith(prefix)
    )


def test_short_classpath_uses_direct_argv(tmp_path: Path) -> None:
    classes = tmp_path / "classes"
    classes.mkdir()
    plan = _plan(tmp_path, (classes,))

    command = JavaCommandMaterializer().materialize(
        plan,
        jdwp_port=5005,
        attempt_directory=tmp_path / "attempt",
        windows=False,
    )

    assert command.materialization == "direct_classpath"
    assert command.retained_files == ()
    assert command.argv[0] == str(plan.java_executable)
    assert "-cp" in command.argv
    assert command.argv[command.argv.index("-cp") + 1] == str(classes)
    assert "suspend=n" in command.argv[1]
    assert command.argv[-1] == "--server.port=0"
    assert not any("spring.devtools.restart.enabled" in arg for arg in command.argv)


@pytest.mark.parametrize("pathing_jar", [False, True])
@pytest.mark.parametrize("jvm_args,expected", [
    ((), ("-Dspring.devtools.restart.enabled=false",)),
    (("-Xmx256m",), ("-Xmx256m", "-Dspring.devtools.restart.enabled=false")),
    (("-Dspring.devtools.restart.enabled.extra=true",),
     ("-Dspring.devtools.restart.enabled.extra=true", "-Dspring.devtools.restart.enabled=false")),
    (("-Dspring.devtools.restart.enabled=true",), ("-Dspring.devtools.restart.enabled=true",)),
    (("-Dspring.devtools.restart.enabled=false",), ("-Dspring.devtools.restart.enabled=false",)),
    (("-Dspring.devtools.restart.enabled=",), ("-Dspring.devtools.restart.enabled=",)),
    (("-Dspring.devtools.restart.enabled",), ("-Dspring.devtools.restart.enabled",)),
])
def test_project_command_defaults_devtools_off_without_overriding_explicit_arguments(
    tmp_path, pathing_jar, jvm_args, expected,
):
    class Commands(JavaCommandMaterializer):
        def materialize(self, *args, **kwargs):
            return super().materialize(*args, **kwargs, force_pathing_jar=pathing_jar)

    pipeline = ProjectLaunchPipeline(command_materializer=Commands())
    original = replace(_plan(tmp_path, (tmp_path,)), jvm_args=jvm_args)
    plan, command = pipeline.materialize_command(
        original, jdwp_port=5005, attempt_directory=tmp_path / "attempt",
    )
    assert original.jvm_args == jvm_args
    assert plan.jvm_args == expected
    assert command.argv[2:command.argv.index("-cp")] == expected
    assert command.materialization == ("pathing_jar" if pathing_jar else "direct_classpath")

    # Re-materializing a cached/prepared plan must not duplicate the property.
    repeated, _ = pipeline.materialize_command(
        plan, jdwp_port=5005, attempt_directory=tmp_path / "repeated",
    )
    assert repeated.jvm_args == expected

    # Restart may replace the entire vm_args list, including with an empty list.
    for replacement in ((), ("-Dfixture.label=restarted",)):
        restarted, command = pipeline.materialize_command(
            replace(plan, jvm_args=replacement), jdwp_port=5005,
            attempt_directory=tmp_path / "restart",
        )
        assert restarted.jvm_args == (*replacement, "-Dspring.devtools.restart.enabled=false")
        assert command.argv[2:command.argv.index("-cp")] == restarted.jvm_args


def test_pathing_jar_is_jdk8_manifest_compatible_and_bounded(
    tmp_path: Path,
) -> None:
    classes = tmp_path / "classes with space" / "中文"
    classes.mkdir(parents=True)
    dependencies = []
    for index in range(12):
        dependency = (
            tmp_path
            / "repository"
            / ("very-long-artifact-name-" + str(index) + ".jar")
        )
        dependency.parent.mkdir(parents=True, exist_ok=True)
        dependency.write_bytes(b"jar")
        dependencies.append(dependency)
    classpath = (classes, *dependencies)

    command = JavaCommandMaterializer().materialize(
        _plan(tmp_path, classpath),
        jdwp_port=5005,
        attempt_directory=tmp_path / "attempt",
        force_pathing_jar=True,
    )

    assert command.materialization == "pathing_jar"
    assert len(command.retained_files) == 1
    pathing_jar = command.retained_files[0]
    assert command.argv[command.argv.index("-cp") + 1] == str(pathing_jar)
    with zipfile.ZipFile(pathing_jar) as archive:
        manifest = archive.read("META-INF/MANIFEST.MF")

    physical_lines = manifest.split(b"\r\n")
    assert all(len(line) <= 72 for line in physical_lines)
    class_path_value = _unfold_manifest_header(manifest, "Class-Path")
    assert class_path_value.split(" ") == [
        JavaCommandMaterializer._classpath_uri(path)
        for path in classpath
    ]
    assert "%20" in class_path_value
    assert "%E4%B8%AD%E6%96%87" in class_path_value
    assert manifest.endswith(b"\r\n\r\n")


def test_windows_length_threshold_selects_pathing_jar(
    tmp_path: Path,
) -> None:
    entries: list[Path] = []
    for index in range(4):
        entry = tmp_path / ("dependency-" + ("x" * 60) + str(index))
        entry.mkdir()
        entries.append(entry)

    command = JavaCommandMaterializer().materialize(
        _plan(tmp_path, tuple(entries)),
        jdwp_port=5005,
        attempt_directory=tmp_path / "attempt",
        windows=True,
        windows_command_limit=80,
    )

    assert command.materialization == "pathing_jar"
    assert command.retained_files[0].is_file()


def test_command_repr_never_exposes_jvm_or_program_arguments(
    tmp_path: Path,
) -> None:
    plan = JvmLaunchPlan(
        java_executable=tmp_path / "java",
        classpath=(tmp_path,),
        main_class="com.example.Application",
        working_directory=tmp_path,
        jvm_args=("-Dpassword=private-secret",),
        program_args=("--token=private-secret",),
    )

    command = JavaCommandMaterializer().materialize(
        plan,
        jdwp_port=5005,
        attempt_directory=tmp_path / "attempt",
        windows=False,
    )

    assert "private-secret" not in repr(command)
