from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import queue
import shutil
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = REPO_ROOT / "experiments" / "jdt-incremental-worker"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bootstrap = _load_module(
    "jolink_jdt_candidate_bootstrap",
    EXPERIMENT / "bootstrap_candidate.py",
)
worker_build = _load_module(
    "jolink_jdt_worker_build",
    EXPERIMENT / "build_worker.py",
)
smoke = _load_module(
    "jolink_jdt_bootstrap_smoke",
    EXPERIMENT / "run_bootstrap_smoke.py",
)
sys.modules["run_bootstrap_smoke"] = smoke
cross_compiler = _load_module(
    "jolink_jdt_cross_compiler_compatibility",
    EXPERIMENT / "run_cross_compiler_compatibility.py",
)
a9 = _load_module(
    "jolink_jdt_a9_experiment",
    EXPERIMENT / "run_a9_experiment.py",
)
phase1b = _load_module(
    "jolink_jdt_phase1b_lombok_experiment",
    EXPERIMENT / "run_lombok_experiment.py",
)
phase2a = _load_module(
    "jolink_jdt_phase2a_real_maven_build_world",
    EXPERIMENT / "run_real_maven_build_world.py",
)
apt_spike = _load_module(
    "jolink_jdt_apt_spike",
    EXPERIMENT / "run_apt_spike.py",
)
maven_probe_spike = _load_module(
    "jolink_jdt_maven_probe_spike",
    EXPERIMENT / "run_maven_probe_spike.py",
)


from jolink_runtime.experiments import jdt_build_world as build_world
from jolink_runtime.experiments import maven_probe
from jolink_runtime.launch.maven import MavenBuildSystemAdapter


def test_apt_spike_candidate_is_independent_and_excludes_ui() -> None:
    bootstrap = json.loads(
        (
            EXPERIMENT
            / "candidate-bootstrap-eclipse-2021-03-apt-spike.json"
        ).read_text(encoding="utf-8")
    )
    apt_lock = json.loads(
        (EXPERIMENT / "locks/eclipse-2021-03-apt-spike.json").read_text(
            encoding="utf-8"
        )
    )
    baseline_lock = json.loads(
        (EXPERIMENT / "locks/eclipse-2021-03-no-apt-spike.json").read_text(
            encoding="utf-8"
        )
    )

    assert bootstrap["candidate_id"] == "eclipse-2021-03-apt-spike"
    assert {
        "org.eclipse.jdt.apt.core",
        "org.eclipse.jdt.apt.pluggable.core",
        "org.eclipse.jdt.compiler.apt",
        "org.eclipse.jdt.compiler.tool",
    }.issubset(bootstrap["root_installable_units"])
    names = {item["symbolic_name"] for item in apt_lock["artifacts"]}
    assert len(apt_lock["artifacts"]) - len(baseline_lock["artifacts"]) == 5
    assert not any(
        token in name.casefold()
        for name in names
        for token in ("apt.ui", "jdt.ui", "eclipse.ui", "swt", "m2e")
    )


def test_apt_spike_fixture_lock_has_one_resource_processor() -> None:
    payload = json.loads(
        (EXPERIMENT / "apt-spring-config-java8-lock.json").read_text(
            encoding="utf-8"
        )
    )

    assert payload["processor"] == {
        "provider": (
            "org.springframework.boot.configurationprocessor."
            "ConfigurationMetadataAnnotationProcessor"
        ),
        "execution_model": "STANDARD_JSR269",
        "output_kind": "RESOURCE",
        "output_path": "META-INF/spring-configuration-metadata.json",
    }
    assert sum(
        item["processor_path"] is True for item in payload["artifacts"]
    ) == 1


def test_apt_spike_reads_metadata_semantically(tmp_path: Path) -> None:
    metadata = tmp_path / "bin/META-INF/spring-configuration-metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps(
            {
                "properties": [
                    {"name": "demo.timeout", "type": "java.lang.Integer"},
                    {"name": "demo.name", "type": "java.lang.String"},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert apt_spike.metadata_properties(tmp_path) == [
        ("demo.name", "java.lang.String"),
        ("demo.timeout", "java.lang.Integer"),
    ]


def test_apt_spike_resource_manifest_tracks_change_and_delete() -> None:
    assert apt_spike.output_changes(
        {"metadata.json": "old", "stale.json": "stale"},
        {"metadata.json": "new", "created.json": "created"},
    ) == {
        "changed": ["created.json", "metadata.json"],
        "deleted": ["stale.json"],
    }


def test_apt_spike_accepts_probe_processor_artifacts(tmp_path: Path) -> None:
    processor = tmp_path / "processor.jar"
    processor.write_bytes(b"processor")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema": "jolink.maven-build-world-probe.v1",
                "annotationProcessing": {
                    "processingMode": "DEFAULT",
                    "discoveryMode": "IMPLICIT_COMPILE_CLASSPATH",
                    "compileClasspathDiscovery": True,
                    "processorProviderArtifactPaths": [str(processor)],
                    "providers": [
                        "org.springframework.boot.configurationprocessor."
                        "ConfigurationMetadataAnnotationProcessor"
                    ],
                    "options": [],
                    "explicitProcessorNames": [],
                    "executionProcessorConfigurationDetected": False,
                    "legacyProcessorOptionsDetected": False,
                    "legacyProcessorOptionCount": 0,
                    "procPropertyDetected": False,
                    "procPropertySourceCount": 0,
                    "unmodeledProcessorCompilerArgsDetected": False,
                    "unmodeledProcessorCompilerArgCount": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    assert apt_spike.load_probe_processor_provider_path(snapshot) == [
        processor.resolve()
    ]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"options": ["-Afoo=bar"]}, "options are not materialized"),
        (
            {"explicitProcessorNames": ["example.Processor"]},
            "Explicit Processor selection",
        ),
        ({"processingMode": "ONLY"}, "path mode is unsupported"),
        (
            {"executionProcessorConfigurationDetected": True},
            "path mode is unsupported",
        ),
        (
            {
                "legacyProcessorOptionsDetected": True,
                "legacyProcessorOptionCount": 1,
            },
            "path mode is unsupported",
        ),
        (
            {"procPropertyDetected": True, "procPropertySourceCount": 1},
            "path mode is unsupported",
        ),
        (
            {
                "unmodeledProcessorCompilerArgsDetected": True,
                "unmodeledProcessorCompilerArgCount": 1,
            },
            "path mode is unsupported",
        ),
    ],
)
def test_apt_spike_rejects_unmaterialized_probe_processor_semantics(
    tmp_path: Path,
    override: dict[str, object],
    message: str,
) -> None:
    processor = tmp_path / "processor.jar"
    processor.write_bytes(b"processor")
    facts = {
        "processingMode": "DEFAULT",
        "discoveryMode": "IMPLICIT_COMPILE_CLASSPATH",
        "compileClasspathDiscovery": True,
        "processorProviderArtifactPaths": [str(processor)],
        "providers": [
            "org.springframework.boot.configurationprocessor."
            "ConfigurationMetadataAnnotationProcessor"
        ],
        "options": [],
        "explicitProcessorNames": [],
        "executionProcessorConfigurationDetected": False,
        "legacyProcessorOptionsDetected": False,
        "legacyProcessorOptionCount": 0,
        "procPropertyDetected": False,
        "procPropertySourceCount": 0,
        "unmodeledProcessorCompilerArgsDetected": False,
        "unmodeledProcessorCompilerArgCount": 0,
    }
    facts.update(override)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema": "jolink.maven-build-world-probe.v1",
                "annotationProcessing": facts,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(apt_spike.AptSpikeError, match=message):
        apt_spike.load_probe_processor_provider_path(snapshot)


def test_apt_spike_rejects_processor_directory(tmp_path: Path) -> None:
    processor = tmp_path / "processor-classes"
    processor.mkdir()
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema": "jolink.maven-build-world-probe.v1",
                "annotationProcessing": {
                    "processingMode": "DEFAULT",
                    "discoveryMode": "IMPLICIT_COMPILE_CLASSPATH",
                    "compileClasspathDiscovery": True,
                    "processorProviderArtifactPaths": [str(processor)],
                    "providers": [
                        "org.springframework.boot.configurationprocessor."
                        "ConfigurationMetadataAnnotationProcessor"
                    ],
                    "options": [],
                    "explicitProcessorNames": [],
                    "executionProcessorConfigurationDetected": False,
                    "legacyProcessorOptionsDetected": False,
                    "legacyProcessorOptionCount": 0,
                    "procPropertyDetected": False,
                    "procPropertySourceCount": 0,
                    "unmodeledProcessorCompilerArgsDetected": False,
                    "unmodeledProcessorCompilerArgCount": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(apt_spike.AptSpikeError, match="directory"):
        apt_spike.load_probe_processor_provider_path(snapshot)


def test_phase2a_unverified_apt_preserves_probe_provider_order(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.jar"
    second = tmp_path / "second.jar"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    snapshot = {
        "annotationProcessing": {
            "processingMode": "DEFAULT",
            "discoveryMode": "IMPLICIT_COMPILE_CLASSPATH",
            "compileClasspathDiscovery": True,
            "processorProviderArtifactPaths": [str(second), str(first)],
            "providers": ["example.Second", "example.First"],
            "options": [],
            "explicitProcessorNames": [],
            "explicitPathDeclarationCount": 0,
            "executionProcessorConfigurationDetected": False,
            "legacyProcessorOptionsDetected": False,
            "legacyProcessorOptionCount": 0,
            "procPropertyDetected": False,
            "procPropertySourceCount": 0,
            "unmodeledProcessorCompilerArgsDetected": False,
            "unmodeledProcessorCompilerArgCount": 0,
        }
    }

    paths, providers = phase2a._experimental_unverified_apt_provider_paths(
        snapshot
    )

    assert paths == (second.resolve(), first.resolve())
    assert providers == ("example.Second", "example.First")


def test_phase2a_unverified_apt_rejects_maven_control_semantics(
    tmp_path: Path,
) -> None:
    processor = tmp_path / "processor.jar"
    processor.write_bytes(b"processor")
    facts = {
        "processingMode": "DEFAULT",
        "discoveryMode": "IMPLICIT_COMPILE_CLASSPATH",
        "compileClasspathDiscovery": True,
        "processorProviderArtifactPaths": [str(processor)],
        "providers": ["example.Processor"],
        "options": [],
        "explicitProcessorNames": [],
        "explicitPathDeclarationCount": 0,
        "executionProcessorConfigurationDetected": False,
        "legacyProcessorOptionsDetected": False,
        "legacyProcessorOptionCount": 0,
        "procPropertyDetected": True,
        "procPropertySourceCount": 1,
        "unmodeledProcessorCompilerArgsDetected": False,
        "unmodeledProcessorCompilerArgCount": 0,
    }

    with pytest.raises(build_world.BuildWorldError) as raised:
        phase2a._experimental_unverified_apt_provider_paths(
            {"annotationProcessing": facts}
        )

    assert raised.value.error_code == "EXPERIMENTAL_APT_MODEL_UNSAFE"


def test_phase2a_unverified_apt_uses_independent_candidate() -> None:
    assert phase2a._lock_for_phase2a(
        EXPERIMENT,
        None,
        experimental_apt=True,
    ) == EXPERIMENT / "locks/eclipse-2021-03-apt-spike.json"


def test_cross_compiler_fixture_is_wired_to_expected_divergence_probe() -> None:
    import subprocess

    result = cross_compiler.require_expected_divergence(
        subprocess.CompletedProcess(
            args=["javac"],
            returncode=0,
            stdout="",
            stderr="warning: unchecked conversion",
        ),
        subprocess.CompletedProcess(
            args=["ecj"],
            returncode=1,
            stdout="Type mismatch: cannot convert from ArrayList to List<T>",
            stderr="",
        ),
    )

    assert result == {
        "javac_8_accepted": True,
        "javac_unchecked_warning_observed": True,
        "ecj_3_25_rejected": True,
        "ecj_type_mismatch_observed": True,
    }


def test_cross_compiler_probe_rejects_missing_expected_divergence() -> None:
    import subprocess

    with pytest.raises(
        cross_compiler.CompatibilityProbeError,
        match="unexpectedly accepted",
    ):
        cross_compiler.require_expected_divergence(
            subprocess.CompletedProcess(
                args=["javac"],
                returncode=0,
                stdout="warning: unchecked conversion",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["ecj"], returncode=0, stdout="", stderr=""
            ),
        )


def test_cross_compiler_probe_requires_an_actual_jdk8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    java_home = tmp_path / "jdk"
    bin_dir = java_home / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("java", "javac"):
        (bin_dir / name).write_bytes(name.encode("ascii"))
    monkeypatch.setattr(
        cross_compiler.common,
        "worker_java_identity",
        lambda _home: {
            "vendor": "fixture",
            "version": "17.0.10",
            "vm_name": "fixture",
            "architecture": "fixture",
            "java_binary_sha256": "java-sha",
        },
    )
    monkeypatch.setattr(
        cross_compiler,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="javac 17.0.10",
            stderr="",
        ),
    )

    with pytest.raises(
        cross_compiler.CompatibilityProbeError,
        match="complete JDK 8",
    ):
        cross_compiler.target_jdk8_identity(java_home)


def _compile_java_tree(root: Path, *, value: str = "one") -> Path:
    source = root / "src" / "example" / "Api.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        "package example; public class Api { "
        f"public String value() {{ return \"{value}\"; }} "
        "protected int size() { return 1; } }\n",
        encoding="utf-8",
    )
    output = root / "classes"
    output.mkdir()
    import subprocess

    completed = subprocess.run(
        ["javac", "-source", "8", "-target", "8", "-d", str(output), str(source)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("A local javac is unavailable for class-structure tests")
    return output


def test_phase2a_filters_current_module_outputs(tmp_path: Path) -> None:
    module = tmp_path / "app"
    output = module / "target" / "classes"
    tests_output = module / "target" / "test-classes"
    repository_jar = (
        tmp_path
        / "repository"
        / "example"
        / "app"
        / "1.0"
        / "app-1.0.jar"
    )
    dependency = tmp_path / "repository" / "example" / "dep" / "1.0" / "dep-1.0.jar"
    private_output = tmp_path / "attempt" / "bin"
    for path in (output, tests_output, private_output):
        path.mkdir(parents=True)
    for path in (repository_jar, dependency):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jar")

    accepted, facts = build_world.filter_self_outputs(
        [output, tests_output, repository_jar, dependency, private_output],
        module_root=module,
        maven_output=output,
        private_candidate_output=private_output,
        current_module_coordinate=("example", "app", "1.0"),
    )

    assert accepted == (dependency.resolve(),)
    assert facts == {
        "excluded_self_output_entry_count": 4,
        "self_output_on_compile_classpath": False,
        "stale_candidate_output_on_classpath": False,
    }


def test_phase2a_excludes_content_verified_maven_descriptor_from_jdt(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    module = workspace / "app"
    source = module / "src" / "main" / "java"
    source.mkdir(parents=True)
    (source / "Api.java").write_text("class Api {}\n", encoding="utf-8")
    output = module / "target" / "classes"
    output.mkdir(parents=True)
    descriptor = tmp_path / "repository" / "artifact-without-pom-suffix"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text(
        "<project xmlns=\"http://maven.apache.org/POM/4.0.0\">"
        "<modelVersion>4.0.0</modelVersion>"
        "<groupId>example</groupId><artifactId>descriptor</artifactId>"
        "<version>1</version></project>\n",
        encoding="utf-8",
    )

    snapshot = build_world.create_snapshot(
        workspace_root=workspace,
        module_root=module,
        maven_output=output,
        source_roots=(
            build_world.describe_source_root(
                source, "DECLARED_SOURCE", workspace_root=workspace
            ),
        ),
        compile_classpath=(descriptor,),
        private_candidate_output=tmp_path / "private-bin",
        source_level=8,
        target_level=8,
        encoding="UTF-8",
        configuration_fingerprint="config",
    )

    assert snapshot.dependencies == ()
    assert len(snapshot.excluded_classpath_inputs) == 1
    excluded = snapshot.excluded_classpath_inputs[0]
    assert excluded.entry_type == "maven_project_descriptor"
    summary = snapshot.redacted_summary()
    assert summary["compile_classpath_entry_count"] == 0
    assert summary["excluded_non_binary_classpath_entry_count"] == 1
    assert summary["excluded_non_binary_classpath_entry_types"] == [
        "maven_project_descriptor"
    ]
    assert str(descriptor) not in json.dumps(summary)


def test_phase2a_redaction_guard_includes_excluded_classpath_paths(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    descriptor = tmp_path / "private-repository" / "descriptor.pom"
    excluded = build_world.ExcludedClasspathInput(
        path=descriptor,
        content_sha256="descriptor-sha",
        entry_type="maven_project_descriptor",
    )
    snapshot = SimpleNamespace(
        dependencies=(),
        excluded_classpath_inputs=(excluded,),
    )
    private_values = phase2a._shareable_report_private_values(
        workspace=SimpleNamespace(project_root=tmp_path / "workspace"),
        module=SimpleNamespace(
            directory=tmp_path / "module",
            output_directory=tmp_path / "module" / "target" / "classes",
        ),
        preferences=SimpleNamespace(
            user_settings_file=None,
            local_repository=tmp_path / "private-repository",
        ),
        snapshot=snapshot,
        diagnostics=(),
    )

    assert str(descriptor) in private_values
    with pytest.raises(build_world.BuildWorldError) as raised:
        phase2a._assert_shareable_report(
            {"excluded_path": str(descriptor)},
            private_values=private_values,
        )
    assert raised.value.error_code == "REPORT_REDACTION_FAILED"


def test_phase2a_unknown_non_binary_classpath_entry_fails_closed(
    tmp_path: Path,
) -> None:
    unknown = tmp_path / "repository" / "opaque-artifact"
    unknown.parent.mkdir(parents=True)
    unknown.write_text("not a Java archive or Maven descriptor\n", encoding="utf-8")

    with pytest.raises(build_world.BuildWorldError) as raised:
        build_world.inspect_dependency(unknown)

    assert raised.value.error_code == "COMPILE_CLASSPATH_ENTRY_UNSUPPORTED"
    assert str(unknown) not in json.dumps(raised.value.as_dict())


def test_phase2a_zip_without_classes_fails_closed(tmp_path: Path) -> None:
    sources = tmp_path / "repository" / "example-sources.jar"
    sources.parent.mkdir(parents=True)
    with zipfile.ZipFile(sources, "w") as archive:
        archive.writestr("example/Api.java", "package example; class Api {}\n")

    with pytest.raises(build_world.BuildWorldError) as raised:
        build_world.inspect_dependency(sources)

    assert raised.value.error_code == "COMPILE_CLASSPATH_ENTRY_UNSUPPORTED"
    assert str(sources) not in json.dumps(raised.value.as_dict())


def test_phase2a_zip_with_class_is_a_java_binary_archive(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "repository" / "example.jar"
    binary.parent.mkdir(parents=True)
    with zipfile.ZipFile(binary, "w") as archive:
        archive.writestr("example/Api.class", b"fixture")

    dependency = build_world.inspect_dependency(binary)

    assert isinstance(dependency, build_world.DependencyInput)
    assert dependency.entry_type == "java_binary_archive"


def test_phase2a_maven_jar_evidence_accepts_resource_only_archive(
    tmp_path: Path,
) -> None:
    resource_jar = tmp_path / "repository" / "starter.jar"
    resource_jar.parent.mkdir(parents=True)
    with zipfile.ZipFile(resource_jar, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        archive.writestr("META-INF/NOTICE.txt", "fixture\n")

    dependency = build_world.inspect_dependency(
        resource_jar,
        maven_artifact_paths=(resource_jar,),
        maven_binary_archive_paths=(resource_jar,),
        maven_resource_archive_paths=(resource_jar,),
    )

    assert isinstance(dependency, build_world.DependencyInput)
    assert dependency.entry_type == "java_binary_archive"


def test_phase2a_unknown_maven_archive_type_fails_closed_even_with_classes(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "repository" / "opaque.zip"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("example/Api.class", b"fixture")

    with pytest.raises(build_world.BuildWorldError) as raised:
        build_world.inspect_dependency(
            archive_path,
            maven_artifact_paths=(archive_path,),
            maven_binary_archive_paths=(),
        )

    assert raised.value.error_code == "COMPILE_CLASSPATH_ENTRY_UNSUPPORTED"


def test_phase2a_parses_maven_artifact_types_without_exposing_coordinates(
    tmp_path: Path,
) -> None:
    jar = tmp_path / "repository" / "starter.jar"
    pom = tmp_path / "repository" / "descriptor.pom"
    manifest = tmp_path / "compile-artifacts.txt"
    manifest.write_text(
        "The following files have been resolved:\n"
        f"   example:starter:jar:1.0:compile:{jar}\n"
        f"   example:descriptor:pom:1.0:compile:{pom}\n",
        encoding="utf-8",
    )

    evidence = phase2a._parse_maven_artifact_manifest(manifest)

    assert [(item.artifact_type, item.scope) for item in evidence] == [
        ("jar", "compile"),
        ("pom", "compile"),
    ]
    assert phase2a._maven_binary_archive_paths(evidence) == (jar.resolve(),)
    assert phase2a._maven_resource_archive_paths(evidence) == (jar.resolve(),)


def test_phase2a_sources_classifier_cannot_authorize_resource_archive(
    tmp_path: Path,
) -> None:
    sources = tmp_path / "repository" / "example-sources.jar"
    sources.parent.mkdir(parents=True)
    with zipfile.ZipFile(sources, "w") as archive:
        archive.writestr("example/Api.java", "package example; class Api {}\n")
    evidence = (
        phase2a.MavenArtifactClasspathEvidence(
            path=sources,
            artifact_type="jar",
            classifier="sources",
            scope="compile",
        ),
    )

    assert phase2a._maven_binary_archive_paths(evidence) == ()
    assert phase2a._maven_resource_archive_paths(evidence) == ()
    with pytest.raises(build_world.BuildWorldError) as raised:
        build_world.inspect_dependency(
            sources,
            maven_artifact_paths=(sources,),
            maven_binary_archive_paths=(),
            maven_resource_archive_paths=(),
        )
    assert raised.value.error_code == "COMPILE_CLASSPATH_ENTRY_UNSUPPORTED"


def test_phase2a_maven_classpath_custom_classifier_can_be_resource_only(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "repository" / "custom.jar"
    archive_path.parent.mkdir(parents=True)
    evidence = (
        phase2a.MavenArtifactClasspathEvidence(
            path=archive_path,
            artifact_type="jar",
            classifier="custom",
            scope="provided",
        ),
    )
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("META-INF/NOTICE", "fixture")

    assert phase2a._maven_binary_archive_paths(evidence) == (
        archive_path.resolve(),
    )
    assert phase2a._maven_resource_archive_paths(evidence) == (
        archive_path.resolve(),
    )
    dependency = build_world.inspect_dependency(
        archive_path,
        maven_artifact_paths=(archive_path,),
        maven_binary_archive_paths=phase2a._maven_binary_archive_paths(evidence),
        maven_resource_archive_paths=phase2a._maven_resource_archive_paths(
            evidence
        ),
    )
    assert isinstance(dependency, build_world.DependencyInput)


def test_phase2a_accepts_confirmed_empty_artifact_manifest(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "compile-artifacts.txt"
    manifest.write_text(
        "The following files have been resolved:\nnone\n",
        encoding="utf-8",
    )

    evidence = phase2a._parse_maven_artifact_manifest(manifest)
    phase2a._require_maven_artifact_coverage((), evidence)

    assert evidence == ()


def test_phase2a_empty_artifact_manifest_rejects_uncovered_external_file(
    tmp_path: Path,
) -> None:
    dependency = tmp_path / "repository" / "dependency.jar"
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"fixture")

    with pytest.raises(build_world.BuildWorldError) as raised:
        phase2a._require_maven_artifact_coverage((dependency,), ())

    assert raised.value.error_code == "MAVEN_ARTIFACT_METADATA_UNAVAILABLE"
    assert raised.value.context == {"uncovered_file_count": 1}


def test_phase2a_private_snapshot_records_classification_provenance(
    tmp_path: Path,
) -> None:
    dependency_path = tmp_path / "repository" / "dependency.jar"
    dependency = build_world.DependencyInput(
        path=dependency_path,
        content_sha256="dependency-sha",
        entry_type="java_binary_archive",
    )
    snapshot = SimpleNamespace(
        workspace_root=tmp_path / "workspace",
        module_root=tmp_path / "workspace" / "app",
        maven_output=tmp_path / "workspace" / "app" / "target" / "classes",
        source_roots=(),
        dependencies=(dependency,),
        excluded_classpath_inputs=(),
        fingerprint="snapshot-sha",
    )
    evidence = (
        phase2a.MavenArtifactClasspathEvidence(
            path=dependency_path,
            artifact_type="jar",
            classifier=None,
            scope="compile",
        ),
    )

    payload = phase2a._private_snapshot_payload(
        snapshot,
        artifact_evidence=evidence,
    )

    assert payload["compile_classpath"] == [
        {
            "path": str(dependency_path),
            "content_sha256": "dependency-sha",
            "entry_type": "java_binary_archive",
            "classification_source": "maven_metadata_and_content",
            "maven_evidence": {
                "artifact_type": "jar",
                "classifier": None,
                "scope": "compile",
            },
        }
    ]


def _probe_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    jar = tmp_path / "probe.jar"
    pom = tmp_path / "probe.pom"
    jar.write_bytes(b"probe-jar")
    pom.write_text("<project/>\n", encoding="utf-8")
    return jar, pom


def test_maven_probe_stages_content_addressed_file_repository(
    tmp_path: Path,
) -> None:
    jar, pom = _probe_artifacts(tmp_path)

    staged = maven_probe.stage_probe_repository(
        probe_jar=jar,
        probe_pom=pom,
        repository_root=tmp_path / "repo",
    )

    artifact = (
        staged.root
        / "io/jolink/jolink-maven-probe"
        / maven_probe.PROBE_VERSION
    )
    staged_jar = artifact / (
        f"jolink-maven-probe-{maven_probe.PROBE_VERSION}.jar"
    )
    staged_pom = artifact / (
        f"jolink-maven-probe-{maven_probe.PROBE_VERSION}.pom"
    )
    assert staged_jar.read_bytes() == b"probe-jar"
    assert staged_pom.read_text(encoding="utf-8") == "<project/>\n"
    assert staged_jar.with_suffix(".jar.sha1").is_file()
    assert staged.jar_sha256[:16] in staged.root.parts
    assert staged.goal.endswith(":export-build-world")


def test_maven_probe_can_seed_explicit_local_repository_for_offline(
    tmp_path: Path,
) -> None:
    jar, pom = _probe_artifacts(tmp_path)
    local_repository = tmp_path / "offline-local-repository"

    facts = maven_probe.stage_probe_in_local_repository(
        probe_jar=jar,
        probe_pom=pom,
        local_repository=local_repository,
    )

    artifact = (
        local_repository
        / "io/jolink/jolink-maven-probe"
        / maven_probe.PROBE_VERSION
    )
    staged_jar = artifact / (
        f"jolink-maven-probe-{maven_probe.PROBE_VERSION}.jar"
    )
    assert staged_jar.read_bytes() == b"probe-jar"
    assert facts["coordinate"] == (
        f"io.jolink:jolink-maven-probe:{maven_probe.PROBE_VERSION}"
    )


def test_maven_probe_settings_preserve_user_values_and_bypass_star_mirror(
    tmp_path: Path,
) -> None:
    jar, pom = _probe_artifacts(tmp_path)
    staged = maven_probe.stage_probe_repository(
        probe_jar=jar,
        probe_pom=pom,
        repository_root=tmp_path / "repo with spaces",
    )
    original = tmp_path / "settings.xml"
    original.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <localRepository>/private/company-repository</localRepository>
  <servers><server><id>company</id><password>{encrypted}</password></server></servers>
  <mirrors><mirror><id>company</id><url>https://repo.invalid</url><mirrorOf>*</mirrorOf></mirror></mirrors>
  <profiles><profile><id>existing</id></profile></profiles>
  <activeProfiles><activeProfile>existing</activeProfile></activeProfiles>
</settings>
""",
        encoding="utf-8",
    )
    original_sha = build_world.sha256_file(original)
    destination = tmp_path / "attempt" / "settings.private.xml"

    facts = maven_probe.create_probe_settings(
        source_settings=original,
        destination=destination,
        repository=staged,
    )

    assert build_world.sha256_file(original) == original_sha
    assert b"ns0:" not in destination.read_bytes()
    root = ET.parse(destination).getroot()
    namespace = {"m": "http://maven.apache.org/SETTINGS/1.0.0"}
    assert root.findtext("m:localRepository", namespaces=namespace) == (
        "/private/company-repository"
    )
    assert root.findtext(
        "m:servers/m:server/m:password", namespaces=namespace
    ) == "{encrypted}"
    mirror_of = root.findtext(
        "m:mirrors/m:mirror/m:mirrorOf", namespaces=namespace
    )
    assert mirror_of == f"*,!{staged.repository_id}"
    repositories = root.findall(
        "m:profiles/m:profile/m:pluginRepositories/m:pluginRepository",
        namespace,
    )
    assert len(repositories) == 1
    assert repositories[0].findtext("m:url", namespaces=namespace) == (
        staged.root.as_uri()
    )
    active = [
        item.text
        for item in root.findall(
            "m:activeProfiles/m:activeProfile", namespace
        )
    ]
    assert active == ["existing", staged.repository_id]
    assert facts["wildcard_mirror_adjustment_count"] == 1


def test_maven_probe_resolves_default_user_settings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    default = home / ".m2" / "settings.xml"
    default.parent.mkdir(parents=True)
    default.write_text("<settings><mirrors/></settings>\n", encoding="utf-8")

    selected, kind = maven_probe.resolve_source_settings(None, user_home=home)

    assert selected == default.resolve()
    assert kind == "maven_user_default"


def test_maven_probe_explicit_settings_win_over_user_default(tmp_path: Path) -> None:
    home = tmp_path / "home"
    default = home / ".m2" / "settings.xml"
    default.parent.mkdir(parents=True)
    default.write_text("<settings/>\n", encoding="utf-8")
    explicit = tmp_path / "company-settings.xml"
    explicit.write_text("<settings><servers/></settings>\n", encoding="utf-8")

    selected, kind = maven_probe.resolve_source_settings(
        explicit, user_home=home
    )

    assert selected == explicit.resolve()
    assert kind == "explicit"


def test_maven_probe_rejects_stale_cached_plugin_identity(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "snapshot.json").write_text(
        json.dumps(
            {
                "schema": "jolink.maven-build-world-probe.v1",
                "probeImplementationId": "b" * 64,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(build_world.BuildWorldError) as raised:
        maven_probe_spike._load_snapshots(
            output, expected_probe_implementation_id="a" * 64
        )

    assert raised.value.error_code == "MAVEN_PROBE_IDENTITY_MISMATCH"


def test_maven_probe_summary_recognizes_reactor_output_reference() -> None:
    snapshots = [
        {
            "schema": "jolink.maven-build-world-probe.v1",
            "outputDirectory": "/workspace/app/target/classes",
            "compileSourceRoots": ["/workspace/app/src/main/java"],
            "compileClasspathElements": [
                "/workspace/app/target/classes",
                "/workspace/common/target/classes",
            ],
            "annotationProcessing": {
                "processingMode": "DEFAULT",
                "discoveryMode": "IMPLICIT_COMPILE_CLASSPATH",
                "compileClasspathDiscovery": True,
                "processorProviderArtifactPaths": [
                    "/workspace/processor.jar"
                ],
                "providers": ["example.Processor"],
                "options": ["-Aexample=true"],
                "explicitProcessorNames": [],
                "executionProcessorConfigurationDetected": False,
                "legacyProcessorOptionsDetected": False,
                "legacyProcessorOptionCount": 0,
                "procPropertyDetected": False,
                "procPropertySourceCount": 0,
                "unmodeledProcessorCompilerArgsDetected": False,
                "unmodeledProcessorCompilerArgCount": 0,
            },
            "reactorProjects": [
                {"outputDirectory": "/workspace/common/target/classes"},
                {"outputDirectory": "/workspace/app/target/classes"},
            ],
        }
    ]

    summary = maven_probe_spike._summary(
        snapshots, project_unchanged=True
    )

    assert summary["reactor_output_classpath_reference_count"] == 1
    assert summary["annotation_processing_modes"] == [
        "IMPLICIT_COMPILE_CLASSPATH"
    ]
    assert summary["annotation_processing_execution_modes"] == ["DEFAULT"]
    assert summary["processor_provider_artifact_count"] == 1
    assert summary["processor_provider_count"] == 1
    assert summary["processor_option_count"] == 1
    assert summary["execution_processor_configuration_count"] == 0
    assert summary["legacy_processor_option_count"] == 0
    assert summary["proc_property_source_count"] == 0
    assert summary["unmodeled_processor_compiler_arg_count"] == 0
    assert summary["project_poms_unchanged"] is True


def test_maven_probe_spike_does_not_echo_private_path_on_os_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private_missing_path = tmp_path / "company-secret-project"

    exit_code = maven_probe_spike.main(
        [
            "--project-root",
            str(private_missing_path),
            "--maven-executable",
            str(tmp_path / "missing-maven"),
            "--cache-root",
            str(tmp_path / "cache"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "MAVEN_PROBE_SPIKE_FAILED" in captured.err
    assert str(private_missing_path) not in captured.err


def test_maven_probe_keep_attempt_removes_credential_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    probe_project = tmp_path / "probe-project"
    probe_project.mkdir()
    probe_pom = probe_project / "pom.xml"
    probe_pom.write_text("<project/>\n", encoding="utf-8")
    probe_jar = probe_project / "probe.jar"
    probe_jar.write_bytes(b"probe")
    maven = tmp_path / "mvn"
    maven.write_text("fixture\n", encoding="utf-8")
    settings = tmp_path / "settings.xml"
    settings.write_text(
        "<settings><servers><server><password>company-secret</password>"
        "</server></servers></settings>\n",
        encoding="utf-8",
    )
    implementation_id = "a" * 64
    monkeypatch.setattr(
        maven_probe_spike,
        "_capture_maven_identity",
        lambda *_args, **_kwargs: {
            "maven_version": "3.3.9",
            "maven_executable_sha256": "maven-sha",
            "version_output_sha256": "version-sha",
            "host_java_version": "1.8.0_451",
            "host_java_vendor": "fixture",
            "host_java_home_identity_sha256": "java-home-sha",
            "private": {},
        },
    )
    monkeypatch.setattr(
        maven_probe_spike,
        "_build_probe",
        lambda **_kwargs: (probe_jar, probe_pom, 0.01, implementation_id),
    )

    def fake_run(command, **_kwargs):
        output_arg = next(
            item
            for item in command
            if item.startswith("-Djolink.probe.outputDirectory=")
        )
        output = Path(output_arg.partition("=")[2])
        output.mkdir(parents=True, exist_ok=True)
        (output / "snapshot.json").write_text(
            json.dumps(
                {
                    "schema": "jolink.maven-build-world-probe.v1",
                    "probeImplementationId": implementation_id,
                    "compileSourceRoots": [],
                    "compileClasspathElements": [],
                    "annotationProcessing": {
                        "processingMode": "DEFAULT",
                        "discoveryMode": "IMPLICIT_COMPILE_CLASSPATH",
                        "compileClasspathDiscovery": True,
                        "processorProviderArtifactPaths": [],
                        "providers": [],
                        "options": [],
                        "explicitProcessorNames": [],
                        "executionProcessorConfigurationDetected": False,
                        "legacyProcessorOptionsDetected": False,
                        "legacyProcessorOptionCount": 0,
                        "procPropertyDetected": False,
                        "procPropertySourceCount": 0,
                        "unmodeledProcessorCompilerArgsDetected": False,
                        "unmodeledProcessorCompilerArgCount": 0,
                    },
                    "outputDirectory": str(project / "target" / "classes"),
                    "reactorProjects": [],
                }
            ),
            encoding="utf-8",
        )
        return 0.01

    monkeypatch.setattr(maven_probe_spike, "_run", fake_run)
    cache = tmp_path / "cache"

    exit_code = maven_probe_spike.main(
        [
            "--project-root",
            str(project),
            "--maven-executable",
            str(maven),
            "--settings-file",
            str(settings),
            "--probe-project",
            str(probe_project),
            "--cache-root",
            str(cache),
            "--keep-attempt",
        ]
    )

    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    private_report = Path(payload["private_report_path"])
    assert exit_code == 0
    assert private_report.is_file()
    assert not (private_report.parent / "settings.private.xml").exists()
    assert "company-secret" not in private_report.read_text(encoding="utf-8")


def test_phase2a_binds_private_probe_report_to_exact_invocation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    module_root = workspace / "app"
    source = module_root / "src" / "main" / "java"
    output = module_root / "target" / "classes"
    reactor_output = workspace / "shared" / "target" / "classes"
    dependency = tmp_path / "repository" / "dependency.jar"
    for directory in (source, output, reactor_output):
        directory.mkdir(parents=True)
    (source / "App.java").write_text("class App {}\n", encoding="utf-8")
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"not-inspected-by-loader")
    (workspace / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    (module_root / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    settings = tmp_path / "settings.xml"
    settings.write_text("<settings/>\n", encoding="utf-8")
    maven = tmp_path / "mvn"
    maven.write_text("fixture\n", encoding="utf-8")
    module = SimpleNamespace(
        directory=module_root,
        output_directory=output,
    )
    implementation_id = "a" * 64
    snapshot = {
        "schema": "jolink.maven-build-world-probe.v1",
        "probeImplementationId": implementation_id,
        "project": {"baseDirectory": str(module_root)},
        "requestedGoals": [
            "compile",
            (
                "io.jolink:jolink-maven-probe:"
                f"{maven_probe.PROBE_VERSION}:export-build-world"
            ),
        ],
        "compileSourceRoots": [str(source)],
        "compileClasspathElements": [str(output), str(reactor_output), str(dependency)],
        "outputDirectory": str(output),
        "reactorProjects": [
            {"outputDirectory": str(output)},
            {"outputDirectory": str(reactor_output)},
        ],
    }
    report = {
        "schema": "jolink.maven-probe-spike.private.v1",
        "project_root": str(workspace),
        "project_pom_fingerprint": phase2a._pom_tree_fingerprint(workspace),
        "probe_implementation_id": implementation_id,
        "invocation": {
            "maven_executable": str(maven.resolve()),
            "local_repository": None,
            "profiles": ["company"],
            "offline": False,
        },
        "settings": {
            "source_settings_sha256": build_world.sha256_file(settings),
        },
        "snapshots": [snapshot],
    }
    report_path = tmp_path / "probe.private.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    preferences = SimpleNamespace(
        user_settings_file=settings,
        local_repository=None,
        active_profiles=("company",),
    )

    selected, loaded_report = phase2a._load_maven_probe_snapshot(
        report_path,
        workspace_root=workspace,
        module=module,
        preferences=preferences,
        maven_executable=maven,
    )
    roots = phase2a._probe_source_roots(
        selected,
        declared_source=source,
        module=module,
        workspace_root=workspace,
    )
    reactors = phase2a._probe_reactor_outputs(selected, module)

    assert selected is snapshot or selected == snapshot
    assert loaded_report["probe_implementation_id"] == implementation_id
    assert [item.provenance for item in roots] == ["DECLARED_SOURCE"]
    assert reactors == (reactor_output.resolve(),)


def test_phase2a_rejects_probe_report_after_pom_change(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    module_root = workspace / "app"
    output = module_root / "target" / "classes"
    output.mkdir(parents=True)
    pom = workspace / "pom.xml"
    pom.write_text("<project/>\n", encoding="utf-8")
    maven = tmp_path / "mvn"
    maven.write_text("fixture\n", encoding="utf-8")
    report_path = tmp_path / "probe.private.json"
    report_path.write_text(
        json.dumps(
            {
                "schema": "jolink.maven-probe-spike.private.v1",
                "project_root": str(workspace),
                "project_pom_fingerprint": phase2a._pom_tree_fingerprint(workspace),
                "probe_implementation_id": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    pom.write_text("<project><name>changed</name></project>\n", encoding="utf-8")

    with pytest.raises(build_world.BuildWorldError) as raised:
        phase2a._load_maven_probe_snapshot(
            report_path,
            workspace_root=workspace,
            module=SimpleNamespace(directory=module_root, output_directory=output),
            preferences=SimpleNamespace(
                user_settings_file=None,
                local_repository=None,
                active_profiles=(),
            ),
            maven_executable=maven,
        )

    assert raised.value.error_code == "MAVEN_PROBE_PROJECT_CHANGED"


@pytest.mark.skipif(os.name != "nt", reason="Windows artifact-path parsing")
def test_phase2a_parses_classifier_and_windows_artifact_path(tmp_path: Path) -> None:
    manifest = tmp_path / "compile-artifacts.txt"
    manifest.write_text(
        "   example:fixture:test-jar:tests:1.0:provided:"
        "C:\\repo\\fixture-1.0-tests.jar\n",
        encoding="utf-8",
    )

    evidence = phase2a._parse_maven_artifact_manifest(manifest)

    assert len(evidence) == 1
    assert evidence[0].artifact_type == "test-jar"
    assert evidence[0].classifier == "tests"
    assert evidence[0].scope == "provided"


def test_phase2a_rejects_pom_like_xml_with_invalid_model_version(
    tmp_path: Path,
) -> None:
    descriptor = tmp_path / "repository" / "pom-like.xml"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text(
        "<project><modelVersion>banana</modelVersion></project>\n",
        encoding="utf-8",
    )

    with pytest.raises(build_world.BuildWorldError) as raised:
        build_world.inspect_dependency(descriptor)

    assert raised.value.error_code == "COMPILE_CLASSPATH_ENTRY_UNSUPPORTED"


def test_phase2a_classifies_other_workspace_directory_as_reactor_output(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    module = workspace / "app"
    reactor_output = workspace / "shared" / "target" / "classes"
    reactor_output.mkdir(parents=True)
    (reactor_output / "Shared.class").write_bytes(b"fixture")

    dependency = build_world.inspect_dependency(
        reactor_output,
        reactor_output_directories=(reactor_output,),
    )

    assert isinstance(dependency, build_world.DependencyInput)
    assert dependency.entry_type == "reactor_output"


def test_phase2a_materialization_rejects_conflicting_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    module = workspace / "app"
    declared = module / "src" / "main" / "java"
    generated = module / "target" / "generated-sources" / "annotations"
    for root, text in ((declared, "class A {}"), (generated, "class B {}")):
        source = root / "example" / "Same.java"
        source.parent.mkdir(parents=True)
        source.write_text(text, encoding="utf-8")
    output = module / "target" / "classes"
    output.mkdir(parents=True)
    snapshot = build_world.create_snapshot(
        workspace_root=workspace,
        module_root=module,
        maven_output=output,
        source_roots=(
            build_world.describe_source_root(
                declared, "DECLARED_SOURCE", workspace_root=workspace
            ),
            build_world.describe_source_root(
                generated, "COMPILE_TIME_AP_GENERATED", workspace_root=workspace
            ),
        ),
        compile_classpath=(),
        private_candidate_output=tmp_path / "private-bin",
        source_level=8,
        target_level=8,
        encoding="UTF-8",
        configuration_fingerprint="config",
    )

    with pytest.raises(build_world.BuildWorldError) as raised:
        build_world.materialize_private_sources(
            snapshot, destination=tmp_path / "private-src"
        )
    assert raised.value.error_code == "SOURCE_ROOT_COLLISION"


def test_phase2a_summary_never_contains_private_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "company-secret-workspace"
    module = workspace / "secret-module"
    source = module / "src" / "main" / "java"
    (source / "example").mkdir(parents=True)
    (source / "example" / "Api.java").write_text(
        "package example; public class Api {}\n", encoding="utf-8"
    )
    output = module / "target" / "classes"
    output.mkdir(parents=True)
    snapshot = build_world.create_snapshot(
        workspace_root=workspace,
        module_root=module,
        maven_output=output,
        source_roots=(
            build_world.describe_source_root(
                source, "DECLARED_SOURCE", workspace_root=workspace
            ),
        ),
        compile_classpath=(),
        private_candidate_output=tmp_path / "private-bin",
        source_level=8,
        target_level=8,
        encoding="UTF-8",
        configuration_fingerprint="config",
    )

    rendered = json.dumps(snapshot.redacted_summary(), ensure_ascii=False)
    assert "company-secret-workspace" not in rendered
    assert "secret-module" not in rendered
    assert str(tmp_path) not in rendered
    assert snapshot.redacted_summary()["self_output_on_compile_classpath"] is False


def test_phase2a_structural_comparison_ignores_method_body_bytes(
    tmp_path: Path,
) -> None:
    maven = _compile_java_tree(tmp_path / "maven", value="maven")
    jdt = _compile_java_tree(tmp_path / "jdt", value="jdt")

    comparison = build_world.compare_class_outputs(
        maven_output=maven, jdt_output=jdt
    )

    assert comparison["class_loading_or_initialization_used"] is False
    assert comparison["tier1"]["status"] == "compatible"
    assert comparison["tier1"]["api_mismatch_count"] == 0


def test_phase2a_api_metadata_comparison_ignores_attribute_order() -> None:
    first = (
        ("RuntimeVisibleAnnotations", ["Lexample/First;"]),
        ("Signature", "Ljava/util/List<Ljava/lang/String;>;"),
        ("SourceFile", "Api.java"),
    )
    second = tuple(reversed(first))

    first_subset = build_world._metadata_subset(first)  # noqa: SLF001
    second_subset = build_world._metadata_subset(second)  # noqa: SLF001

    assert first_subset == second_subset


def test_worker_ready_requires_build_world_source_encoding() -> None:
    ready = {
        "source_encoding_requested": "utf8",
        "source_encoding_requested_canonical": "UTF-8",
        "source_encoding_effective": "UTF-8",
        "source_encoding_verified": True,
    }

    assert smoke.require_ready_source_encoding(
        ready, requested="utf8"
    ) == "UTF-8"

    with pytest.raises(smoke.SmokeError, match="does not match Build World"):
        smoke.require_ready_source_encoding(
            {
                "source_encoding_requested": "UTF-8",
                "source_encoding_requested_canonical": "UTF-8",
                "source_encoding_effective": "GBK",
                "source_encoding_verified": True,
            },
            requested="UTF-8",
        )

    with pytest.raises(smoke.SmokeError, match="did not report"):
        smoke.require_ready_source_encoding({}, requested="UTF-8")


def test_worker_ready_requires_effective_apt_state() -> None:
    identity = "a" * 64
    ready = {
        "apt_enabled": True,
        "apt_factory_path_requested_count": 1,
        "apt_factory_path_effective_count": 1,
        "apt_factory_path_requested_identity": identity,
        "apt_factory_path_effective_identity": identity,
        "apt_factory_path_verified": True,
        "apt_unexpected_enabled_container_count": 0,
        "apt_unexpected_enabled_container_identity": identity,
        "apt_generated_source_requested": ".apt_generated",
        "apt_generated_source_effective": ".apt_generated",
        "apt_generated_source_verified": True,
    }

    smoke.require_ready_apt_state(ready)

    with pytest.raises(smoke.SmokeError, match="did not verify"):
        smoke.require_ready_apt_state(
            {**ready, "apt_factory_path_effective_count": 0}
        )

    with pytest.raises(smoke.SmokeError, match="did not verify"):
        smoke.require_ready_apt_state(
            {**ready, "apt_unexpected_enabled_container_count": 1}
        )


def test_phase2a_defers_encoding_authority_to_java_charset() -> None:
    project = ET.fromstring(
        """
        <project xmlns="http://maven.apache.org/POM/4.0.0">
          <properties>
            <project.build.sourceEncoding>IBM01140</project.build.sourceEncoding>
          </properties>
        </project>
        """
    )

    assert MavenBuildSystemAdapter()._source_encoding(  # noqa: SLF001
        project,
        require_host_codec=False,
    ) == "IBM01140"


def test_phase2a_diagnostic_summary_contains_no_raw_messages() -> None:
    secret = "C:/company/SecretService.java:42:error: TokenValue cannot be resolved"
    summary = build_world.classify_diagnostics([secret])
    rendered = json.dumps(summary)

    assert summary["diagnostic_count"] == 1
    assert summary["raw_diagnostics_in_report"] is False
    assert "SecretService" not in rendered
    assert "TokenValue" not in rendered


def test_phase2a_diagnostic_classifier_does_not_treat_unused_import_as_gap() -> None:
    summary = build_world.classify_diagnostics(
        [
            "src/example/Api.java:8:1:The import example.Value is never used",
            "src/example/Service.java:9:2:The import example.Missing cannot be resolved",
            "src/example/Other.java:10:2:MissingType cannot be resolved to a type",
        ]
    )

    assert summary["buckets"] == {
        "missing_dependency": 2,
        "missing_generated_source": 0,
        "processor_or_generated_api_mismatch": 0,
        "language_or_compiler_incompatibility": 0,
        "other": 1,
    }


def test_worker_diagnostic_contract_accepts_error_first_projection() -> None:
    smoke.require_diagnostics_contract(
        {
            "error_count": 260,
            "warning_count": 64,
            "info_count": 0,
            "returned_error_count": 128,
            "returned_warning_count": 32,
            "returned_info_count": 0,
            "diagnostic_selection_policy": (
                "errors_first_then_warnings_then_info"
            ),
            "diagnostics": ["diagnostic"] * 160,
            "diagnostic_details": (
                [{"severity_name": "ERROR"}] * 128
                + [{"severity_name": "WARNING"}] * 32
            ),
            "diagnostics_truncated": True,
        }
    )


def test_worker_diagnostic_contract_rejects_hidden_errors() -> None:
    with pytest.raises(smoke.SmokeError, match="prioritize ERROR"):
        smoke.require_diagnostics_contract(
            {
                "error_count": 1,
                "warning_count": 64,
                "info_count": 0,
                "returned_error_count": 0,
                "returned_warning_count": 32,
                "returned_info_count": 0,
                "diagnostic_selection_policy": (
                    "errors_first_then_warnings_then_info"
                ),
                "diagnostics": ["warning"] * 32,
                "diagnostic_details": [
                    {"severity_name": "WARNING"}
                ] * 32,
                "diagnostics_truncated": True,
            }
        )


def test_worker_diagnostic_contract_rejects_mislabelled_projection() -> None:
    with pytest.raises(smoke.SmokeError, match="error-first severity ordering"):
        smoke.require_diagnostics_contract(
            {
                "error_count": 1,
                "warning_count": 1,
                "info_count": 0,
                "returned_error_count": 1,
                "returned_warning_count": 1,
                "returned_info_count": 0,
                "diagnostic_selection_policy": (
                    "errors_first_then_warnings_then_info"
                ),
                "diagnostics": ["error", "warning"],
                "diagnostic_details": [
                    {"severity_name": "WARNING"},
                    {"severity_name": "ERROR"},
                ],
                "diagnostics_truncated": False,
            }
        )


def test_phase2a_unknown_processor_blocks_incremental_only(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    module = workspace / "app"
    source = module / "src" / "main" / "java"
    source.mkdir(parents=True)
    (source / "Api.java").write_text("class Api {}\n", encoding="utf-8")
    output = module / "target" / "classes"
    output.mkdir(parents=True)

    snapshot = build_world.create_snapshot(
        workspace_root=workspace,
        module_root=module,
        maven_output=output,
        source_roots=(
            build_world.describe_source_root(
                source, "DECLARED_SOURCE", workspace_root=workspace
            ),
        ),
        compile_classpath=(),
        private_candidate_output=tmp_path / "private-bin",
        source_level=8,
        target_level=8,
        encoding="UTF-8",
        configuration_fingerprint="config",
        declared_processor_identities=("processor-sha",),
        declared_processor_kinds=("unknown",),
    )

    assert snapshot.phase2b_incremental_eligible is False
    assert snapshot.phase2b_blockers == (
        "unknown_declared_annotation_processor",
    )


def test_phase2a_maven_native_extra_source_root_blocks_incremental_only(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    module = workspace / "app"
    declared = module / "src" / "main" / "java"
    extra = module / "target" / "custom-generated"
    output = module / "target" / "classes"
    for directory in (declared, extra, output):
        directory.mkdir(parents=True)
    (declared / "App.java").write_text("class App {}\n", encoding="utf-8")
    (extra / "Generated.java").write_text(
        "class Generated {}\n", encoding="utf-8"
    )

    snapshot = build_world.create_snapshot(
        workspace_root=workspace,
        module_root=module,
        maven_output=output,
        source_roots=(
            build_world.describe_source_root(
                declared, "DECLARED_SOURCE", workspace_root=workspace
            ),
            build_world.describe_source_root(
                extra, "MAVEN_NATIVE_SOURCE_ROOT", workspace_root=workspace
            ),
        ),
        compile_classpath=(),
        private_candidate_output=tmp_path / "private-bin",
        source_level=8,
        target_level=8,
        encoding="UTF-8",
        configuration_fingerprint="config",
    )

    assert snapshot.phase2b_incremental_eligible is False
    assert snapshot.phase2b_blockers == (
        "maven_native_source_root_refresh_unverified",
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("17", "17.0.0"),
        ("3.46.0", "3.46.0"),
        ("3.46.0.v20260520-1003", "3.46.0.v20260520-1003"),
    ],
)
def test_osgi_version_round_trip(raw: str, expected: str) -> None:
    assert bootstrap.OSGiVersion.parse(raw).text() == expected


def test_osgi_version_range_boundaries() -> None:
    version_range = bootstrap.VersionRange.parse("[1.2.0,2.0.0)")
    assert version_range.contains(bootstrap.OSGiVersion.parse("1.2.0"))
    assert version_range.contains(bootstrap.OSGiVersion.parse("1.9.9"))
    assert not version_range.contains(bootstrap.OSGiVersion.parse("2.0.0"))


def test_resolver_uses_bundle_and_worker_system_capabilities(
    tmp_path: Path,
) -> None:
    content = tmp_path / "content.xml"
    content.write_text(
        """<?xml version='1.0'?>
<repository>
  <units>
    <unit id='a.jre.javase' version='17.0.0'>
      <provides>
        <provided namespace='java.package' name='javax.xml.parsers' version='0.0.0'/>
      </provides>
    </unit>
    <unit id='root' version='1.0.0'>
      <provides>
        <provided namespace='osgi.bundle' name='root' version='1.0.0'/>
      </provides>
      <requires>
        <required namespace='osgi.bundle' name='dependency' range='[1.0.0,2.0.0)'/>
        <required namespace='java.package' name='javax.xml.parsers' range='0.0.0'/>
      </requires>
      <artifacts><artifact classifier='osgi.bundle' id='root' version='1.0.0'/></artifacts>
    </unit>
    <unit id='dependency' version='1.5.0'>
      <provides>
        <provided namespace='osgi.bundle' name='dependency' version='1.5.0'/>
      </provides>
      <artifacts><artifact classifier='osgi.bundle' id='dependency' version='1.5.0'/></artifacts>
    </unit>
  </units>
</repository>
""",
        encoding="utf-8",
    )
    units = bootstrap.parse_units(content)
    system = bootstrap.parse_system_capabilities(content, java_major=17)
    resolved = bootstrap.resolve_units(
        units, ["root"], system_capabilities=system
    )
    assert [unit.unit_id for unit in resolved] == ["dependency", "root"]


def test_resolver_validates_and_records_osgi_execution_environment(
    tmp_path: Path,
) -> None:
    content = tmp_path / "content.xml"
    content.write_text(
        """<?xml version='1.0'?>
<repository>
  <units>
    <unit id='a.jre.javase' version='17.0.0'>
      <provides>
        <provided namespace='osgi.ee' name='JavaSE' version='17.0.0'/>
        <provided namespace='osgi.ee' name='JavaSE/compact1' version='1.8.0'/>
      </provides>
    </unit>
    <unit id='root' version='1.0.0'>
      <provides>
        <provided namespace='osgi.bundle' name='root' version='1.0.0'/>
      </provides>
      <requires>
        <requiredProperties namespace='osgi.ee'
          match='(|(&amp;(osgi.ee=JavaSE)(version=17))(&amp;(osgi.ee=JavaSE/compact1)(version=1.8)))'/>
      </requires>
      <artifacts><artifact classifier='osgi.bundle' id='root' version='1.0.0'/></artifacts>
    </unit>
  </units>
</repository>
""",
        encoding="utf-8",
    )
    units = bootstrap.parse_units(content)
    system = bootstrap.parse_system_capabilities(content, java_major=17)
    resolved = bootstrap.resolve_units(
        units, ["root"], system_capabilities=system
    )
    evidence = bootstrap.execution_environment_evidence(
        resolved, system_capabilities=system, worker_java_major=17
    )

    assert evidence["status"] == "satisfied"
    assert evidence["worker_java_major"] == 17
    assert evidence["p2_capability_unit_java_major"] == 17
    assert evidence["worker_java_satisfies_p2_profile"] is True
    assert evidence["requirements"] == [
        {
            "bundle": "root",
            "bundle_version": "1.0.0",
            "filter": "(|(&(osgi.ee=JavaSE)(version=17))(&(osgi.ee=JavaSE/compact1)(version=1.8)))",
            "status": "satisfied",
            "matched_capability": {"name": "JavaSE", "version": "17.0.0"},
        }
    ]


def test_execution_environment_can_use_older_p2_profile_for_newer_worker(
    tmp_path: Path,
) -> None:
    content = tmp_path / "content.xml"
    content.write_text(
        """<?xml version='1.0'?>
<repository>
  <units>
    <unit id='a.jre.javase' version='11.0.0'>
      <provides>
        <provided namespace='osgi.ee' name='JavaSE' version='11.0.0'/>
      </provides>
    </unit>
    <unit id='root' version='1.0.0'>
      <provides>
        <provided namespace='osgi.bundle' name='root' version='1.0.0'/>
      </provides>
      <requires>
        <requiredProperties namespace='osgi.ee'
          match='(&amp;(osgi.ee=JavaSE)(version=11))'/>
      </requires>
      <artifacts><artifact classifier='osgi.bundle' id='root' version='1.0.0'/></artifacts>
    </unit>
  </units>
</repository>
""",
        encoding="utf-8",
    )
    units = bootstrap.parse_units(content)
    system = bootstrap.parse_system_capabilities(content, java_major=11)
    resolved = bootstrap.resolve_units(
        units, ["root"], system_capabilities=system
    )

    evidence = bootstrap.execution_environment_evidence(
        resolved,
        system_capabilities=system,
        worker_java_major=17,
        p2_capability_unit_java_major=11,
    )

    assert evidence["worker_java_major"] == 17
    assert evidence["p2_capability_unit_java_major"] == 11
    assert evidence["worker_java_satisfies_p2_profile"] is True


def test_execution_environment_rejects_worker_older_than_p2_profile() -> None:
    with pytest.raises(
        bootstrap.DiscoveryError,
        match="older than the p2 execution-environment profile",
    ):
        bootstrap.execution_environment_evidence(
            [],
            system_capabilities=(),
            worker_java_major=8,
            p2_capability_unit_java_major=11,
        )


def test_phase1b_source_path_evidence_is_shareable_and_redacted(
    tmp_path: Path,
) -> None:
    source = tmp_path / "private-user-name" / "workspace" / "src"

    class Worker:
        ready = {
            "source_resource_full_path": "/plain-fixture/src",
            "source_location_uri": source.resolve().as_uri(),
        }

    evidence = phase1b.worker_source_path_evidence(
        Worker(), expected_source=source
    )
    serialized = json.dumps(evidence)

    assert evidence["location_uri_scheme"] == "file"
    assert evidence["physical_path_matches_expected"] is True
    assert evidence["expected_path_identity_sha256"] == evidence[
        "observed_path_identity_sha256"
    ]
    assert "private-user-name" not in serialized
    assert str(tmp_path) not in serialized
    assert "location_uri" not in evidence
    assert "expected_physical_path" not in evidence
    assert "observed_physical_path" not in evidence


def test_resolver_rejects_unsatisfied_osgi_execution_environment(
    tmp_path: Path,
) -> None:
    content = tmp_path / "content.xml"
    content.write_text(
        """<?xml version='1.0'?>
<repository>
  <units>
    <unit id='a.jre.javase' version='17.0.0'>
      <provides><provided namespace='osgi.ee' name='JavaSE' version='17.0.0'/></provides>
    </unit>
    <unit id='root' version='1.0.0'>
      <provides><provided namespace='osgi.bundle' name='root' version='1.0.0'/></provides>
      <requires><requiredProperties namespace='osgi.ee' match='(&amp;(osgi.ee=JavaSE)(version=21))'/></requires>
      <artifacts><artifact classifier='osgi.bundle' id='root' version='1.0.0'/></artifacts>
    </unit>
  </units>
</repository>
""",
        encoding="utf-8",
    )
    units = bootstrap.parse_units(content)
    system = bootstrap.parse_system_capabilities(content, java_major=17)

    with pytest.raises(
        bootstrap.DiscoveryError,
        match="does not satisfy root",
    ):
        bootstrap.resolve_units(
            units, ["root"], system_capabilities=system
        )


def test_target_system_v2_parser_preserves_absent_placeholders(
    tmp_path: Path,
) -> None:
    boot = tmp_path / "rt.jar"
    boot.write_bytes(b"boot")
    missing = tmp_path / "legacy-placeholder.jar"
    extension_directory = tmp_path / "ext"
    extension_directory.mkdir()
    extension = extension_directory / "vendor.jar"
    extension.write_bytes(b"extension")

    def encoded(key: str, value: Path | str) -> str:
        payload = base64.b64encode(str(value).encode("utf-8")).decode("ascii")
        return f"{key}.base64={payload}"

    output = tmp_path / "snapshot.properties"
    output.write_text(
        "\n".join(
            [
                "format=jolink-target-system-libraries-v2",
                encoded("java.vendor", "fixture-vendor"),
                encoded("java.version", "1.8.0_fixture"),
                encoded("java.home", tmp_path),
                "discovery.method=fixture",
                "bootstrap.advertised.count=2",
                encoded("bootstrap.advertised.0", boot),
                "bootstrap.advertised.0.present=true",
                encoded("bootstrap.advertised.1", missing),
                "bootstrap.advertised.1.present=false",
                "extension.directory.count=1",
                encoded("extension.directory.0", extension_directory),
                "extension.directory.0.present=true",
                "endorsed.directory.count=0",
                "compiler.platform.count=2",
                encoded("compiler.platform.0", boot),
                "compiler.platform.0.present=true",
                encoded("compiler.platform.1", extension),
                "compiler.platform.1.present=true",
                "runtime.extension.url.count=1",
                encoded("runtime.extension.url.0", extension),
                "runtime.extension.url.0.present=true",
                "",
            ]
        ),
        encoding="utf-8",
    )

    parsed = smoke.parse_helper_output(output)

    assert parsed["bootstrap_advertised"][1] == {
        "path": missing,
        "present": False,
    }
    assert parsed["compiler_platform"][1]["path"] == extension


def test_exact_build_gate_accepts_observed_leaf_incremental() -> None:
    frame = {
        "ok": True,
        "actual_build_kind": "INCREMENTAL",
        "build_outcome": "COMPILED",
        "project_build_returned": True,
        "compilation_observation": {
            "status": "enabled",
            "callbacks_seen": True,
            "batch_seen": False,
            "incremental_compile_seen": True,
            "build_finished": True,
            "compiled_source_units": ["src/example/Application.java"],
        },
        "compiled_source_units": ["src/example/Application.java"],
        "changed_classes": ["example/Application.class"],
        "compiler_output_eligible": True,
        "generation_publishable": False,
        "publishable_changed_classes": [],
        "deleted_classes": [],
        "error_count": 0,
    }

    smoke.require_exact_observed_build(
        frame,
        label="fixture",
        actual_build_kind="INCREMENTAL",
        build_outcome="COMPILED",
        compiled_source_units=["src/example/Application.java"],
        changed_classes=["example/Application.class"],
    )


def test_exact_build_gate_rejects_full_build_masquerading_as_incremental() -> None:
    frame = {
        "ok": True,
        "actual_build_kind": "FULL",
        "build_outcome": "COMPILED",
        "project_build_returned": True,
        "compilation_observation": {
            "status": "enabled",
            "callbacks_seen": True,
            "batch_seen": True,
            "incremental_compile_seen": False,
            "build_finished": True,
            "compiled_source_units": [
                "src/example/Api.java",
                "src/example/Application.java",
                "src/example/Service.java",
            ],
        },
        "compiled_source_units": [
            "src/example/Api.java",
            "src/example/Application.java",
            "src/example/Service.java",
        ],
        "changed_classes": ["example/Application.class"],
        "compiler_output_eligible": True,
        "generation_publishable": False,
        "publishable_changed_classes": [],
        "deleted_classes": [],
        "error_count": 0,
    }

    with pytest.raises(smoke.SmokeError, match="not INCREMENTAL"):
        smoke.require_exact_observed_build(
            frame,
            label="fixture",
            actual_build_kind="INCREMENTAL",
            build_outcome="COMPILED",
            compiled_source_units=["src/example/Application.java"],
            changed_classes=["example/Application.class"],
        )


def test_exact_no_compile_gate_accepts_absent_compilation_callbacks() -> None:
    frame = {
        "ok": True,
        "actual_build_kind": None,
        "build_outcome": "NO_COMPILE",
        "project_build_returned": True,
        "compilation_observation": {
            "status": "enabled",
            "callbacks_seen": False,
            "batch_seen": False,
            "incremental_compile_seen": False,
            "build_finished": False,
            "compiled_source_units": [],
        },
        "compiled_source_units": [],
        "changed_classes": [],
        "compiler_output_eligible": True,
        "generation_publishable": False,
        "publishable_changed_classes": [],
        "deleted_classes": [],
        "error_count": 0,
    }

    smoke.require_exact_observed_build(
        frame,
        label="fixture",
        actual_build_kind=None,
        build_outcome="NO_COMPILE",
        compiled_source_units=[],
        changed_classes=[],
        expected_callbacks_seen=False,
        expected_observer_build_finished=False,
    )


def test_exact_build_gate_requires_project_build_to_return() -> None:
    frame = {
        "ok": True,
        "actual_build_kind": None,
        "build_outcome": "NO_COMPILE",
        "project_build_returned": False,
        "compilation_observation": {
            "status": "enabled",
            "callbacks_seen": False,
            "batch_seen": False,
            "incremental_compile_seen": False,
            "build_finished": False,
            "compiled_source_units": [],
        },
        "compiled_source_units": [],
        "changed_classes": [],
        "compiler_output_eligible": True,
        "generation_publishable": False,
        "publishable_changed_classes": [],
        "deleted_classes": [],
        "error_count": 0,
    }

    with pytest.raises(smoke.SmokeError, match="project.build"):
        smoke.require_exact_observed_build(
            frame,
            label="fixture",
            actual_build_kind=None,
            build_outcome="NO_COMPILE",
            compiled_source_units=[],
            changed_classes=[],
            expected_callbacks_seen=False,
            expected_observer_build_finished=False,
        )


def test_exact_build_gate_rejects_worker_publication_before_runner_commit() -> None:
    frame = {
        "ok": True,
        "actual_build_kind": "INCREMENTAL",
        "build_outcome": "COMPILED",
        "project_build_returned": True,
        "compilation_observation": {
            "status": "enabled",
            "callbacks_seen": True,
            "batch_seen": False,
            "incremental_compile_seen": True,
            "build_finished": True,
            "compiled_source_units": ["src/example/Application.java"],
        },
        "compiled_source_units": ["src/example/Application.java"],
        "changed_classes": ["example/Application.class"],
        "compiler_output_eligible": True,
        "generation_publishable": True,
        "publishable_changed_classes": ["example/Application.class"],
        "deleted_classes": [],
        "error_count": 0,
    }

    with pytest.raises(smoke.SmokeError, match="publication gate"):
        smoke.require_exact_observed_build(
            frame,
            label="fixture",
            actual_build_kind="INCREMENTAL",
            build_outcome="COMPILED",
            compiled_source_units=["src/example/Application.java"],
            changed_classes=["example/Application.class"],
        )


def test_class_family_uses_exact_binary_name_boundary(tmp_path: Path) -> None:
    output = tmp_path / "bin"
    package = output / "example"
    package.mkdir(parents=True)
    for relative in (
        "Legacy.class",
        "Legacy$Inner.class",
        "Legacy$1.class",
        "LegacyExtra.class",
        "Unrelated.class",
    ):
        (package / relative).write_bytes(b"fixture")

    assert smoke.class_family(output, "example/Legacy") == [
        "example/Legacy$1.class",
        "example/Legacy$Inner.class",
        "example/Legacy.class",
    ]


@pytest.mark.parametrize(
    ("actual_build_kind", "expected_outcome", "expected_units"),
    [
        (
            "INCREMENTAL",
            "incremental_state_restored",
            ["src/example/Application.java"],
        ),
        (
            "FULL",
            "explicit_full_rebuild_required",
            [
                "src/example/Api.java",
                "src/example/Application.java",
                "src/example/Service.java",
            ],
        ),
    ],
)
def test_restart_build_expectations_are_explicit(
    actual_build_kind: str,
    expected_outcome: str,
    expected_units: list[str],
) -> None:
    assert smoke.restart_build_expectations(actual_build_kind) == (
        expected_outcome,
        expected_units,
    )


def test_restart_build_expectations_reject_unobserved_kind() -> None:
    with pytest.raises(smoke.SmokeError, match="incremental or full"):
        smoke.restart_build_expectations(None)


def test_selected_unknown_mandatory_capability_fails_closed(
    tmp_path: Path,
) -> None:
    content = tmp_path / "content.xml"
    content.write_text(
        """<?xml version='1.0'?>
<repository>
  <units>
    <unit id='root' version='1.0.0'>
      <provides>
        <provided namespace='osgi.bundle' name='root' version='1.0.0'/>
      </provides>
      <requires>
        <requiredProperties namespace='unknown.capability' match='(x=y)'/>
      </requires>
      <artifacts><artifact classifier='osgi.bundle' id='root' version='1.0.0'/></artifacts>
    </unit>
  </units>
</repository>
""",
        encoding="utf-8",
    )
    units = bootstrap.parse_units(content)
    with pytest.raises(
        bootstrap.DiscoveryError,
        match="unsupported mandatory requirements",
    ):
        bootstrap.resolve_units(units, ["root"])


@pytest.mark.parametrize(
    ("license_payload", "expected"),
    [
        (
            "SPDX-License-Identifier: Apache-2.0 OR LGPL-2.1-or-later\n",
            "Apache-2.0 OR LGPL-2.1-or-later",
        ),
        (
            "<p>Eclipse Public License Version 2.0</p>",
            "EPL-2.0",
        ),
    ],
)
def test_license_identity_fallback(
    tmp_path: Path, license_payload: str, expected: str
) -> None:
    jar = tmp_path / "bundle.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        if expected == "EPL-2.0":
            archive.writestr("about.html", license_payload)
        else:
            archive.writestr("META-INF/LICENSE", license_payload)
    assert bootstrap._license_identity(jar, {}) == expected


def test_generated_config_excludes_launcher_and_framework() -> None:
    lock = {
        "artifacts": [
            {
                "symbolic_name": "org.eclipse.osgi",
                "filename": "org.eclipse.osgi.jar",
            },
            {
                "symbolic_name": "org.eclipse.equinox.launcher",
                "filename": "launcher.jar",
            },
            {
                "symbolic_name": "org.eclipse.jdt.core",
                "filename": "jdt.jar",
            },
        ]
    }
    config = worker_build._config_ini(lock, worker_filename="worker.jar")
    assert "osgi.framework=file:plugins/org.eclipse.osgi.jar" in config
    assert "launcher.jar@4:start" not in config
    assert "jdt.jar@4:start" in config
    assert "worker.jar@4:start" in config


def test_worker_jar_is_reproducible(tmp_path: Path) -> None:
    worker = tmp_path / "worker"
    (worker / "META-INF").mkdir(parents=True)
    (worker / "META-INF" / "MANIFEST.MF").write_text(
        "Manifest-Version: 1.0\n\n", encoding="utf-8"
    )
    (worker / "plugin.xml").write_text("<plugin/>\n", encoding="utf-8")
    classes = tmp_path / "classes"
    (classes / "example").mkdir(parents=True)
    (classes / "example" / "Fixture.class").write_bytes(b"class-bytes")

    first = tmp_path / "first.jar"
    second = tmp_path / "second.jar"
    worker_build._create_worker_jar(worker, classes, first)
    worker_build._create_worker_jar(worker, classes, second)

    assert worker_build.sha256_file(first) == worker_build.sha256_file(second)


def test_diagnostic_identity_ignores_marker_enumeration_order() -> None:
    assert smoke.diagnostics_identity(
        {"diagnostics": ["Service.java:7:error", "Service.java:4:warning"]}
    ) == ["Service.java:4:warning", "Service.java:7:error"]


@pytest.mark.parametrize(
    ("operation_kind", "compile_ok", "terminal_status"),
    [
        ("CLEAN", None, "SUCCEEDED"),
        ("FULL", True, "SUCCEEDED"),
        ("INCREMENTAL", False, "FAILED_COMPILE"),
    ],
)
def test_a9_build_operation_contract(
    operation_kind: str,
    compile_ok: bool | None,
    terminal_status: str,
) -> None:
    smoke.require_build_operation_contract(
        {
            "operation_kind": operation_kind,
            "operation_ok": True,
            "compile_ok": compile_ok,
            "terminal_status": terminal_status,
            "error_count": 0,
            "warning_count": 0,
            "info_count": 0,
            "returned_error_count": 0,
            "returned_warning_count": 0,
            "returned_info_count": 0,
            "diagnostic_selection_policy": (
                "errors_first_then_warnings_then_info"
            ),
            "diagnostics": [],
            "diagnostic_details": [],
            "diagnostics_truncated": False,
        },
        operation_kind=operation_kind,
    )


def test_a9_resource_decision_requires_complete_sampling() -> None:
    checkpoints = []
    for index in range(6):
        checkpoints.append(
            {
                "after": {
                    "heap_used_bytes": 10_000_000 + index,
                    "class_metadata_used_bytes": 20_000_000 + index,
                    "thread_count": 12,
                    "loaded_class_count": 1000,
                },
                "process_tree_after": {
                    "process_tree_rss_sum_bytes": 100_000_000 + index
                },
            }
        )
    decision = smoke.a9_resource_decision(
        checkpoints=checkpoints,
        sampler={
            "coverage_status": "incomplete",
            "samples": [
                {"process_tree_rss_sum_bytes": 100_000_000}
            ],
        },
    )
    assert decision["status"] == "DIAGNOSTIC_RERUN_REQUIRED"
    assert "process_tree_sampling_incomplete" in decision["reasons"]


def _complete_a9_checkpoint(rss: int = 100_000_000) -> dict[str, object]:
    metrics = {
        "heap_used_bytes": 10_000_000,
        "class_metadata_used_bytes": 20_000_000,
        "thread_count": 12,
        "loaded_class_count": 1000,
        "memory_pools": [
            {
                "name": "Metaspace",
                "used_bytes": 20_000_000,
                "committed_bytes": 24_000_000,
                "max_bytes": -1,
                "peak_used_bytes": 20_000_000,
            }
        ],
        "garbage_collectors": [
            {
                "name": "Fixture GC",
                "collection_count": 1,
                "collection_time_ms": 1,
            }
        ],
    }
    process_tree = {"process_tree_rss_sum_bytes": rss}
    return {
        "gc_request_sent": True,
        "before": dict(metrics),
        "after": dict(metrics),
        "process_tree_before": dict(process_tree),
        "process_tree_after": dict(process_tree),
    }


def test_a9_resource_decision_rejects_gib_peak() -> None:
    decision = smoke.a9_resource_decision(
        checkpoints=[_complete_a9_checkpoint() for _ in range(6)],
        sampler={
            "coverage_status": "complete",
            "samples": [
                {"process_tree_rss_sum_bytes": 100_000_000},
                {"process_tree_rss_sum_bytes": 1024**3 + 1},
                {"process_tree_rss_sum_bytes": 100_000_000},
            ],
        },
    )
    assert decision["status"] == "NO_GO"
    assert "full_build_peak_rss_no_go_band" in decision["reasons"]


def test_runner_ledger_synthesizes_exactly_one_abort() -> None:
    ledger = smoke.RunnerBuildLedger()
    accepted = {
        "ok": True,
        "status": "BUILD_ACCEPTED",
        "request_id": "request-1",
        "build_generation_id": "build-1",
        "operation_kind": "INCREMENTAL",
        "protocol_sequence": 1,
    }
    ledger.observe(accepted)
    ledger.accept(accepted)

    terminal = ledger.abort("build-1", error_type="WorkerEOF")
    duplicate = ledger.abort("build-1", error_type="ForcedTermination")

    assert terminal == duplicate
    assert terminal["status"] == "BUILD_ABORTED"
    assert terminal["terminal_record_source"] == "runner"
    assert terminal["protocol_sequence"] == 2
    assert terminal["generation_publishable"] is False


def test_runner_ledger_rejects_late_worker_terminal() -> None:
    ledger = smoke.RunnerBuildLedger()
    accepted = {
        "ok": True,
        "status": "BUILD_ACCEPTED",
        "request_id": "request-1",
        "build_generation_id": "build-1",
        "operation_kind": "FULL",
        "protocol_sequence": 1,
    }
    ledger.observe(accepted)
    ledger.accept(accepted)
    ledger.abort("build-1", error_type="WorkerEOF")

    with pytest.raises(smoke.SmokeError, match="late or unknown"):
        ledger.terminalize_worker(
            {
                "status": "BUILD_COMPLETED",
                "request_id": "request-1",
                "build_generation_id": "build-1",
                "protocol_sequence": 2,
            }
        )


def test_live_worker_terminal_timeout_does_not_abort_active_build() -> None:
    client = object.__new__(smoke.WorkerClient)
    client.timeout = 0.001
    client.frames = queue.Queue()
    client.received_frames = []
    client.build_ledger = smoke.RunnerBuildLedger()
    client.process = type(
        "LiveProcess",
        (),
        {"poll": lambda self: None},
    )()
    accepted = {
        "ok": True,
        "status": "BUILD_ACCEPTED",
        "request_id": "request-1",
        "build_generation_id": "build-1",
        "operation_kind": "INCREMENTAL",
        "protocol_sequence": 1,
    }
    client.build_ledger.observe(accepted)
    client.build_ledger.accept(accepted)

    with pytest.raises(smoke.WorkerResponseTimeout):
        client.receive_terminal(
            build_generation_id="build-1",
            timeout=0.001,
        )

    assert client.build_ledger.terminal("build-1") is None


def test_worker_eof_synthesizes_runner_abort() -> None:
    client = object.__new__(smoke.WorkerClient)
    client.timeout = 0.001
    client.frames = queue.Queue()
    client.frames.put(None)
    client.received_frames = []
    client.build_ledger = smoke.RunnerBuildLedger()
    client.process = type(
        "ExitedProcess",
        (),
        {"poll": lambda self: -9},
    )()
    accepted = {
        "ok": True,
        "status": "BUILD_ACCEPTED",
        "request_id": "request-1",
        "build_generation_id": "build-1",
        "operation_kind": "INCREMENTAL",
        "protocol_sequence": 1,
    }
    client.build_ledger.observe(accepted)
    client.build_ledger.accept(accepted)

    terminal = client.receive_terminal(
        build_generation_id="build-1",
        timeout=0.001,
    )

    assert terminal["status"] == "BUILD_ABORTED"
    assert terminal["terminal_record_source"] == "runner"
    assert terminal["error_type"] == "WorkerEOF"


def test_a9_sampler_cadence_is_stricter_than_contract_limit() -> None:
    defaults = smoke.ProcessTreeSampler.__init__.__defaults__
    assert defaults is not None
    assert defaults[-1] == 0.05
    assert defaults[-1] <= 0.1


def test_a10_path_boundary_facts_do_not_expose_the_path(tmp_path: Path) -> None:
    boundary = tmp_path / "A10 path 路径边界" / "source 目录"
    boundary.mkdir(parents=True)

    facts = smoke.path_boundary_facts(boundary)

    assert facts == {
        "contains_space": True,
        "contains_non_ascii": True,
    }
    assert str(boundary) not in repr(facts)


def test_phase1b_lock_rejects_changed_lombok_artifact(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    artifact = repository / "org/projectlombok/lombok/1.18.20/lombok.jar"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"changed")
    support = repository / "org/slf4j/slf4j-api/1.7.30/slf4j.jar"
    support.parent.mkdir(parents=True)
    support.write_bytes(b"support")
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps(
            {
                "lombok": {
                    "relative_maven_path": str(artifact.relative_to(repository)),
                    "sha256": "0" * 64,
                    "bytes": len(b"changed"),
                },
                "slf4j_api": {
                    "relative_maven_path": str(support.relative_to(repository)),
                    "sha256": smoke.sha256_file(support),
                    "bytes": len(b"support"),
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(smoke.SmokeError, match="lombok"):
        phase1b.load_lombok_lock(lock, maven_repository=repository)


def test_phase1b_activation_gate_requires_every_transform() -> None:
    with pytest.raises(smoke.SmokeError, match="activation evidence"):
        phase1b.assert_activation_shape(
            {
                "activation": {
                    "builder_generated": True,
                    "getter_generated": True,
                    "setter_generated": True,
                    "nonnull_guard_literal_present": True,
                    "slf4j_field_generated": False,
                }
            }
        )


def test_phase1b_lock_matches_exact_lombok_1_18_20_identity() -> None:
    lock = json.loads(
        (EXPERIMENT / "lombok-1.18.20-lock.json").read_text(encoding="utf-8")
    )

    assert lock["lombok"]["version"] == "1.18.20"
    assert lock["lombok"]["sha256"] == (
        "ce947be6c2fbe759fbbe8ef3b42b6825f814c98c8853f1013f2d9630cedf74b0"
    )
    assert lock["integration"]["java_agent_argument"] == "=ECJ"
    assert lock["integration"]["worker_jvm_arguments"] == [
        "--add-opens=java.base/java.lang=ALL-UNNAMED"
    ]


def test_phase1b_compile_failure_accepts_worker_failure_semantics() -> None:
    phase1b.require_compile_failure(
        {
            "ok": False,
            "operation_kind": "INCREMENTAL",
            "operation_ok": True,
            "compile_ok": False,
            "terminal_status": "FAILED_COMPILE",
            "error_count": 1,
            "warning_count": 0,
            "info_count": 0,
            "returned_error_count": 1,
            "returned_warning_count": 0,
            "returned_info_count": 0,
            "diagnostic_selection_policy": (
                "errors_first_then_warnings_then_info"
            ),
            "diagnostics": ["src/example/LombokConsumer.java:1:2:error"],
            "diagnostic_details": [{"severity_name": "ERROR"}],
            "diagnostics_truncated": False,
            "generation_publishable": False,
            "build_outcome": "COMPILED",
            "compiled_source_units": ["src/example/LombokConsumer.java"],
        },
        label="generated-member failure",
    )


def test_phase1b_oracle_gate_compares_diagnostics_and_complete_tree(
    tmp_path: Path,
) -> None:
    output = tmp_path / "bin"
    target = output / "example/Model.class"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"class-bytes")
    frame = {"diagnostics": []}
    hashes = smoke.output_hashes(output)

    evidence = phase1b.require_oracle_equal(
        label="fixture",
        output=output,
        frame=frame,
        oracle_hashes=hashes,
        oracle_diagnostics=[],
    )

    assert evidence["clean_full_oracle_equal"] is True
    target.write_bytes(b"changed")
    with pytest.raises(smoke.SmokeError, match="clean-full oracle"):
        phase1b.require_oracle_equal(
            label="fixture",
            output=output,
            frame=frame,
            oracle_hashes=hashes,
            oracle_diagnostics=[],
        )


def test_phase1b_to_builder_consumer_is_real_downstream_usage(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src"
    source.mkdir()
    fixture = EXPERIMENT / "fixtures/lombok-java/src"
    shutil.copytree(fixture, source, dirs_exist_ok=True)
    phase1b.add_to_builder_consumer(source)

    consumer = (source / "example/LombokConsumer.java").read_text(
        encoding="utf-8"
    )
    assert "LombokModel copy = model.toBuilder()" in consumer
    assert ".count(3)" in consumer
    assert "return copy.getName()" in consumer


def test_phase1b_sampled_peak_is_bounded_to_operation_window() -> None:
    evidence = phase1b.sampled_process_tree_peak(
        {
            "samples": [
                {"monotonic_seconds": 1.0, "process_tree_rss_sum_bytes": 10},
                {"monotonic_seconds": 2.0, "process_tree_rss_sum_bytes": 20},
                {"monotonic_seconds": 3.0, "process_tree_rss_sum_bytes": 30},
            ]
        },
        started=1.5,
        ended=2.5,
    )

    assert evidence == {
        "sample_count": 1,
        "process_tree_rss_sum_bytes": 20,
    }


def test_p2_metadata_cache_is_partitioned_by_repository_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads: list[tuple[str, Path]] = []

    def fake_download(url: str, destination: Path) -> None:
        downloads.append((url, destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("content.xml", "<repository />")

    monkeypatch.setattr(bootstrap, "_download", fake_download)

    first_xml, first_identity = bootstrap._content_xml(
        "https://example.invalid/eclipse/current", tmp_path
    )
    second_xml, second_identity = bootstrap._content_xml(
        "https://example.invalid/eclipse/anchor", tmp_path
    )

    assert first_xml != second_xml
    assert first_identity["url"] != second_identity["url"]
    assert len(downloads) == 2
    assert downloads[0][1].parent != downloads[1][1].parent


def test_bundle_download_retries_a_truncated_cached_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = bootstrap.Unit(
        unit_id="example.bundle",
        version=bootstrap.OSGiVersion.parse("1.0.0"),
        capabilities=(),
        requirements=(),
        execution_environment_requirements=(),
        unsupported_mandatory_requirements=(),
        artifact_classifier="osgi.bundle",
        artifact_id="example.bundle",
        artifact_version="1.0.0",
    )
    destination = (
        tmp_path
        / "candidates/candidate/plugins/example.bundle_1.0.0.jar"
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"truncated")
    download_count = 0

    def fake_download(url: str, path: Path) -> None:
        nonlocal download_count
        download_count += 1
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "META-INF/MANIFEST.MF",
                "Manifest-Version: 1.0\n"
                "Bundle-SymbolicName: example.bundle\n"
                "Bundle-Version: 1.0.0\n\n",
            )
            archive.writestr(
                "about.html",
                "Eclipse Public License Version 2.0",
            )

    monkeypatch.setattr(bootstrap, "_download", fake_download)

    lock = bootstrap.download_and_lock(
        repository_url="https://example.invalid/repository",
        units=[unit],
        cache_root=tmp_path,
        candidate_id="candidate",
        bootstrap_config={
            "worker_java_minimum": 17,
            "root_installable_units": ["example.bundle"],
        },
        metadata={"sha256": "metadata"},
        execution_environment={"status": "satisfied"},
        lock_path=tmp_path / "lock.json",
    )

    assert download_count == 1
    assert lock["artifacts"][0]["symbolic_name"] == "example.bundle"


def test_download_retries_transient_transport_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    class Response(io.BytesIO):
        def __init__(self) -> None:
            super().__init__(b"payload")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()
            return False

    def fake_urlopen(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("transient transport failure")
        return Response()

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _: None)
    destination = tmp_path / "artifact.jar"

    bootstrap._download("https://example.invalid/artifact.jar", destination)

    assert attempts == 3
    assert destination.read_bytes() == b"payload"


def test_workspace_lineage_marker_is_consumed_and_reports_offline_delta(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "Fixture.java").write_text("class Fixture {}\n", encoding="utf-8")
    lineage = a9.WorkspaceLineage(
        root=tmp_path / "lineage",
        workspace_lineage_id="workspace-1",
        candidate_id="candidate-1",
        candidate_lock_fingerprint="lock-sha",
        bundle_set_fingerprint="bundle-sha",
        worker_sha256="worker-sha",
        target_system_library_fingerprint="system-sha",
        project_model_fingerprint="model-sha",
    )
    manifest = lineage.write_manifest(
        last_completed_build_generation_id="build-1",
        source=source,
    )
    lineage.publish_clean_marker(manifest=manifest)
    (source / "Fixture.java").write_text("class Fixture { int x; }\n", encoding="utf-8")

    reuse = lineage.consume_for_reopen(source=source)

    assert reuse["reusable"] is True
    assert reuse["offline_source_delta"] is True
    assert not lineage.marker_path.exists()
    assert lineage.claimed_marker_path.exists()


def test_workspace_lineage_without_runner_marker_is_not_reusable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "Fixture.java").write_text("class Fixture {}\n", encoding="utf-8")
    lineage = a9.WorkspaceLineage(
        root=tmp_path / "lineage",
        workspace_lineage_id="workspace-1",
        candidate_id="candidate-1",
        candidate_lock_fingerprint="lock-sha",
        bundle_set_fingerprint="bundle-sha",
        worker_sha256="worker-sha",
        target_system_library_fingerprint="system-sha",
        project_model_fingerprint="model-sha",
    )
    lineage.write_manifest(
        last_completed_build_generation_id="build-1",
        source=source,
    )

    assert lineage.consume_for_reopen(source=source) == {
        "reusable": False,
        "reason": "missing_manifest_or_clean_shutdown_marker",
    }
