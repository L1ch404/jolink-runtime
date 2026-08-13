from __future__ import annotations

import base64
import importlib.util
import io
import json
import queue
import sys
import zipfile
from pathlib import Path

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
a9 = _load_module(
    "jolink_jdt_a9_experiment",
    EXPERIMENT / "run_a9_experiment.py",
)
phase1b = _load_module(
    "jolink_jdt_phase1b_lombok_experiment",
    EXPERIMENT / "run_lombok_experiment.py",
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
