from __future__ import annotations

import os
from pathlib import Path

import pytest

from jolink_runtime.launch import (
    IdeaBuildPreferences,
    JavaToolchainCandidate,
    JavaToolchainResolver,
    MavenToolResolver,
)


def _make_jdk(home: Path) -> None:
    suffix = ".exe" if os.name == "nt" else ""
    for name in ("java", "javac"):
        executable = home / "bin" / f"{name}{suffix}"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"")


def test_java_toolchain_reads_static_release_major(tmp_path: Path) -> None:
    jdk8 = tmp_path / "jdk8"
    jdk17 = tmp_path / "jdk17"
    unknown = tmp_path / "unknown"
    for home in (jdk8, jdk17, unknown):
        _make_jdk(home)
    (jdk8 / "release").write_text(
        'JAVA_VERSION="1.8.0_402"\n',
        encoding="utf-8",
    )
    (jdk17 / "release").write_text(
        'JAVA_VERSION="17.0.12"\n',
        encoding="utf-8",
    )

    def candidate(home: Path) -> JavaToolchainCandidate:
        suffix = ".exe" if os.name == "nt" else ""
        return JavaToolchainCandidate(
            home=home,
            java_executable=home / "bin" / f"java{suffix}",
            javac_executable=home / "bin" / f"javac{suffix}",
            source="test",
        )

    assert candidate(jdk8).major_version == 8
    assert candidate(jdk17).major_version == 17
    assert candidate(unknown).major_version is None


def test_java_candidates_keep_build_and_runtime_jdks_distinct(
    tmp_path: Path,
    monkeypatch,
) -> None:
    build = tmp_path / "build-jdk"
    runtime = tmp_path / "runtime-jdk"
    fallback = tmp_path / "fallback-jdk"
    for home in (build, runtime, fallback):
        _make_jdk(home)
    monkeypatch.setenv("JAVA_HOME", str(fallback))
    monkeypatch.setattr(
        "jolink_runtime.launch.toolchain.shutil.which",
        lambda _name: None,
    )
    preferences = IdeaBuildPreferences(
        project_jdk_name="runtime",
        maven_runner_jdk_name="build",
        jdk_homes_by_name={
            "build": (build,),
            "runtime": (runtime,),
        },
    )
    resolver = JavaToolchainResolver()

    build_candidates = resolver.candidates(
        preferences=preferences,
        explicit_reference=None,
        for_build=True,
    )
    runtime_candidates = resolver.candidates(
        preferences=preferences,
        explicit_reference=None,
        for_build=False,
    )

    assert [candidate.home for candidate in build_candidates] == [build]
    assert [candidate.home for candidate in runtime_candidates] == [runtime]


def test_explicit_runtime_jdk_path_has_highest_priority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    explicit = tmp_path / "jdk8"
    fallback = tmp_path / "jdk17"
    _make_jdk(explicit)
    _make_jdk(fallback)
    monkeypatch.setenv("JAVA_HOME", str(fallback))
    monkeypatch.setattr(
        "jolink_runtime.launch.toolchain.shutil.which",
        lambda _name: None,
    )

    candidates = JavaToolchainResolver().candidates(
        preferences=IdeaBuildPreferences(),
        explicit_reference=str(explicit),
        for_build=False,
    )

    assert candidates[0].home == explicit
    assert candidates[0].source == "idea_explicit_jdk"


def test_missing_named_jdk_never_silently_falls_back(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fallback = tmp_path / "jdk17"
    _make_jdk(fallback)
    monkeypatch.setenv("JAVA_HOME", str(fallback))
    monkeypatch.setattr(
        "jolink_runtime.launch.toolchain.shutil.which",
        lambda _name: str(fallback / "bin" / "java"),
    )

    candidates = JavaToolchainResolver().candidates(
        preferences=IdeaBuildPreferences(
            project_jdk_name="company-jdk8",
            jdk_homes_by_name={},
        ),
        explicit_reference=None,
        for_build=False,
    )

    assert candidates == ()


def test_path_java_does_not_publish_a_guessed_java_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "system-prefix"
    candidate = JavaToolchainCandidate(
        home=home,
        java_executable=home / "bin" / "java",
        javac_executable=home / "bin" / "javac",
        source="PATH",
    )

    environment = JavaToolchainResolver.maven_environment(candidate)

    assert "JAVA_HOME" not in environment
    assert environment["PATH"].split(os.pathsep)[0] == str(home / "bin")


def test_maven_candidates_prefer_idea_then_wrapper_then_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    idea_home = tmp_path / "idea-maven"
    env_home = tmp_path / "env-maven"
    suffix = "mvn.cmd" if os.name == "nt" else "mvn"
    idea_executable = idea_home / "bin" / suffix
    env_executable = env_home / "bin" / suffix
    wrapper = project / ("mvnw.cmd" if os.name == "nt" else "mvnw")
    for executable in (idea_executable, env_executable, wrapper):
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"")
        executable.chmod(0o755)
    monkeypatch.setenv("MAVEN_HOME", str(env_home))
    monkeypatch.delenv("M2_HOME", raising=False)
    monkeypatch.setattr(
        "jolink_runtime.launch.toolchain.shutil.which",
        lambda _name: None,
    )

    candidates = MavenToolResolver().candidates(
        project_root=project,
        preferences=IdeaBuildPreferences(custom_maven_home=idea_home),
    )

    assert [candidate.source for candidate in candidates] == [
        "idea_custom_maven",
        "project_wrapper",
        "MAVEN_HOME",
    ]


def test_toolchain_probe_specs_do_not_expose_environment_values(
    tmp_path: Path,
) -> None:
    home = tmp_path / "jdk"
    _make_jdk(home)
    candidate = JavaToolchainCandidate(
        home=home,
        java_executable=home / "bin" / (
            "java.exe" if os.name == "nt" else "java"
        ),
        javac_executable=home / "bin" / (
            "javac.exe" if os.name == "nt" else "javac"
        ),
        source="test",
    )

    spec = JavaToolchainResolver.probe_spec(
        candidate,
        cwd=tmp_path,
        output_capture=tmp_path / "probe.log",
        operation_name="java_probe",
    )

    assert spec.argv[-1] == "-version"
    assert spec.timeout_seconds == 15
    assert str(home) not in repr(spec)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ('openjdk version "17.0.14" 2025-01-21\n', 17),
        ('java version "1.8.0_432"\n', 8),
        ("openjdk 21.0.6 2025-01-21\n", 21),
        ("not a Java version response\n", None),
    ],
)
def test_java_version_probe_output_reports_major(
    output: str,
    expected: int | None,
) -> None:
    assert (
        JavaToolchainCandidate.parse_major_version_output(output)
        == expected
    )


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("javac 17.0.14\n", 17),
        ("javac 1.8.0_432\n", 8),
        ("not a compiler response\n", None),
    ],
)
def test_javac_version_probe_output_reports_major(
    output: str,
    expected: int | None,
) -> None:
    assert (
        JavaToolchainCandidate.parse_compiler_major_version_output(
            output
        )
        == expected
    )


def test_supervised_probe_versions_override_static_release_metadata(
    tmp_path: Path,
) -> None:
    home = tmp_path / "shim-jdk"
    home.mkdir()
    (home / "release").write_text(
        'JAVA_VERSION="17.0.14"\n',
        encoding="utf-8",
    )
    candidate = JavaToolchainCandidate(
        home=home,
        java_executable=home / "bin/java",
        javac_executable=home / "bin/javac",
        source="test",
        detected_major_version=8,
        detected_compiler_major_version=11,
    )

    assert candidate.major_version == 8
    assert candidate.compiler_major_version == 11
