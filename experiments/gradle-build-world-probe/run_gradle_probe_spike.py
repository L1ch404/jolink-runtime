#!/usr/bin/env python3
"""Run the isolated Gradle Task-native Build World and cancellation spike."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from jolink_runtime.launch.contracts import BuildOperationSpec
from jolink_runtime.launch.process_supervisor import (
    AttemptToken,
    ProcessSupervisor,
)


ROOT = Path(__file__).resolve().parent
INIT_TEMPLATE = ROOT / "init.gradle.template"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest(project: Path) -> str:
    digest = hashlib.sha256()
    for root in (project / "src/main/java", project / "src/test/java"):
        if not root.is_dir():
            continue
        for source in sorted(root.rglob("*.java")):
            digest.update(source.relative_to(project).as_posix().encode())
            digest.update(hashlib.sha256(source.read_bytes()).digest())
    return digest.hexdigest()


def run(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}):\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return completed


def create_fixture(root: Path, *, kotlin_dsl: bool) -> Path:
    project = root / ("kotlin-dsl" if kotlin_dsl else "groovy-dsl")
    main = project / "src/main/java/example/TextService.java"
    test = project / "src/test/java/example/TextServiceTest.java"
    main.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    (project / ("settings.gradle.kts" if kotlin_dsl else "settings.gradle")).write_text(
        'rootProject.name = "jolink-gradle-fixture"\n'
        if kotlin_dsl
        else "rootProject.name = 'jolink-gradle-fixture'\n",
        encoding="utf-8",
    )
    if kotlin_dsl:
        build = """plugins { java }

java {
    sourceCompatibility = JavaVersion.VERSION_11
    targetCompatibility = JavaVersion.VERSION_11
}

tasks.test {
    useJUnitPlatform()
    workingDir = layout.buildDirectory.dir("test-working").get().asFile
    systemProperty("jolink.fixture.token", "sensitive-system-value")
    environment("JOLINK_FIXTURE_ENV", "sensitive-environment-value")
    jvmArgs("-Xmx128m")
}
"""
    else:
        build = """plugins { id 'java' }

java {
    sourceCompatibility = JavaVersion.VERSION_11
    targetCompatibility = JavaVersion.VERSION_11
}

test {
    useJUnitPlatform()
    workingDir = layout.buildDirectory.dir('test-working').get().asFile
    systemProperty 'jolink.fixture.token', 'sensitive-system-value'
    environment 'JOLINK_FIXTURE_ENV', 'sensitive-environment-value'
    jvmArgs '-Xmx128m'
}
"""
    (project / ("build.gradle.kts" if kotlin_dsl else "build.gradle")).write_text(
        build, encoding="utf-8"
    )
    main.write_text(
        "package example; public class TextService { "
        "public String strip(String value) { return value.strip(); } }\n",
        encoding="utf-8",
    )
    test.write_text(
        "package example; public class TextServiceTest { "
        "public void placeholder() { new TextService().strip(\" x \" ); } }\n",
        encoding="utf-8",
    )
    return project


def create_wrapper(
    project: Path,
    *,
    gradle: Path,
    version: str,
    distribution_zip: Path,
    environment: dict[str, str],
) -> None:
    run(
        (
            str(gradle),
            "--offline",
            "--no-daemon",
            "wrapper",
            "--gradle-version",
            version,
            "--distribution-type",
            "bin",
        ),
        cwd=project,
        environment=environment,
    )
    wrapper = project / "gradlew"
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    properties = project / "gradle/wrapper/gradle-wrapper.properties"
    rendered = properties.read_text(encoding="utf-8")
    rendered = "\n".join(
        (
            "distributionUrl=" + distribution_zip.as_uri()
            if line.startswith("distributionUrl=")
            else line
        )
        for line in rendered.splitlines()
    ) + "\n"
    properties.write_text(rendered, encoding="utf-8")


def private_distribution_zip(
    root: Path,
    *,
    gradle: Path,
    version: str,
) -> Path:
    distribution = gradle.parent.parent.resolve(strict=True)
    output = root / "private-distributions" / f"gradle-{version}-bin.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_file():
        return output
    archive = shutil.make_archive(
        str(output.with_suffix("")),
        "zip",
        root_dir=distribution.parent,
        base_dir=distribution.name,
    )
    return Path(archive).resolve(strict=True)


def probe_command(
    project: Path,
    *,
    probe_jar: Path,
    init_script: Path,
    output: Path,
    slow_millis: int = 0,
) -> tuple[str, ...]:
    return (
        str(project / "gradlew"),
        "--offline",
        "--stacktrace",
        "-I",
        str(init_script),
        f"-Djolink.gradle.probeJar={probe_jar}",
        f"-Djolink.gradle.output={output}",
        "-Djolink.gradle.targetProject=:",
        f"-Djolink.gradle.slowMillis={slow_millis}",
        "jolinkExportBuildWorld",
    )


def validate_private_model(
    model: dict,
    *,
    project: Path,
    version: str,
) -> dict:
    assert model["schema"] == "jolink.gradle-build-world-probe.v1"
    assert model["gradleVersion"] == version
    assert model["projectPath"] == ":"
    assert model["compileJava"]["sourceCompatibility"] == "11"
    assert model["compileJava"]["targetCompatibility"] == "11"
    assert model["compileTestJava"]["sourceCompatibility"] == "11"
    assert model["testRuntime"]["framework"] == "junit_platform"
    assert "jolink.fixture.token" in model["testRuntime"][
        "systemPropertyNames"
    ]
    assert "JOLINK_FIXTURE_ENV" in model["testRuntime"][
        "environmentOverrideNames"
    ]
    assert model["testRuntime"]["systemPropertiesPrivate"][
        "jolink.fixture.token"
    ] == "sensitive-system-value"
    assert model["testRuntime"]["environmentOverridesPrivate"][
        "JOLINK_FIXTURE_ENV"
    ] == "sensitive-environment-value"
    main_sources = model["main"]["javaSourceDirectories"]
    test_sources = model["test"]["javaSourceDirectories"]
    assert str((project / "src/main/java").resolve()) in main_sources
    assert str((project / "src/test/java").resolve()) in test_sources
    return {
        "source_level": 11,
        "target_level": 11,
        "main_source_root_count": len(main_sources),
        "test_source_root_count": len(test_sources),
        "test_framework": model["testRuntime"]["framework"],
        "system_property_override_count": len(
            model["testRuntime"]["systemPropertyNames"]
        ),
        "environment_override_count": len(
            model["testRuntime"]["environmentOverrideNames"]
        ),
        "system_properties_identity": model["testRuntime"][
            "systemPropertiesIdentity"
        ],
        "environment_identity": model["testRuntime"][
            "environmentOverridesIdentity"
        ],
    }


def daemon_pids(java_home: Path, environment: dict[str, str]) -> set[int]:
    completed = subprocess.run(
        (str(java_home / "bin/jps"), "-l"),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    result: set[int] = set()
    for line in completed.stdout.splitlines():
        pid, _, name = line.partition(" ")
        if "org.gradle.launcher.daemon.bootstrap.GradleDaemon" in name:
            result.add(int(pid))
    return result


def cancellation_gate(
    project: Path,
    *,
    probe_jar: Path,
    init_script: Path,
    java_home: Path,
    environment: dict[str, str],
) -> dict:
    output = project.parent / "cancelled-private-model.json"
    marker = output.with_name(output.name + ".started")
    log = project.parent / "cancelled-gradle.log"
    output.unlink(missing_ok=True)
    marker.unlink(missing_ok=True)
    before = daemon_pids(java_home, environment)
    supervisor = ProcessSupervisor()
    owner = AttemptToken("gradle_probe_cancel", 1)
    result_box: list[object] = []
    thread = threading.Thread(
        target=lambda: result_box.append(
            supervisor.run(
                BuildOperationSpec(
                    argv=probe_command(
                        project,
                        probe_jar=probe_jar,
                        init_script=init_script,
                        output=output,
                        slow_millis=12_000,
                    ),
                    cwd=project,
                    environment=environment,
                    timeout_seconds=60,
                    output_capture=log,
                    operation_name="gradle_probe_cancel",
                ),
                owner=owner,
            )
        ),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 30
    while not marker.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not marker.is_file():
        raise TimeoutError("Gradle slow export task did not start")
    cancellation = supervisor.cancel(
        owner, deadline=time.monotonic() + 10
    )
    thread.join(12)
    if thread.is_alive() or not cancellation.settled:
        raise RuntimeError("Gradle client cancellation did not settle")
    # The daemon-side action must not continue to publish after its client was
    # cancelled. Wait beyond the requested slow task duration.
    time.sleep(12.5)
    if output.exists():
        raise AssertionError("Cancelled Gradle task published a late model")
    after = daemon_pids(java_home, environment)
    surviving = before & after
    if before and not surviving:
        raise AssertionError("Cancelling the client killed the shared daemon")

    recovery_output = project.parent / "recovery-private-model.json"
    run(
        probe_command(
            project,
            probe_jar=probe_jar,
            init_script=init_script,
            output=recovery_output,
        ),
        cwd=project,
        environment=environment,
        timeout=30,
    )
    return {
        "cancel_settled": True,
        "late_publish_blocked": True,
        "shared_daemon_survived": bool(surviving or not before),
        "post_cancel_build_passed": recovery_output.is_file(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-jar", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--java-home", type=Path, required=True)
    parser.add_argument(
        "--gradle",
        action="append",
        required=True,
        help="VERSION=/absolute/path/to/gradle",
    )
    args = parser.parse_args()
    probe_jar = args.probe_jar.expanduser().resolve(strict=True)
    lock = json.loads(
        args.lock.expanduser().resolve(strict=True).read_text(encoding="utf-8")
    )
    java_home = args.java_home.expanduser().resolve(strict=True)
    distributions: list[tuple[str, Path]] = []
    for raw in args.gradle:
        version, separator, executable = raw.partition("=")
        if not separator:
            raise SystemExit("--gradle must be VERSION=/path/to/gradle")
        distributions.append(
            (version, Path(executable).expanduser().resolve(strict=True))
        )
    environment = {
        **os.environ,
        "JAVA_HOME": str(java_home),
        "PATH": str(java_home / "bin") + os.pathsep + os.environ.get("PATH", ""),
    }
    probe_identity = sha256(probe_jar)
    if probe_identity != lock.get("sha256"):
        raise SystemExit("The Gradle Probe JAR does not match its lock.")
    report: dict[str, object] = {
        "ok": True,
        "probe_sha256": probe_identity,
        "probe_class_major": None,
        "matrix": [],
    }
    with __import__("zipfile").ZipFile(probe_jar) as archive:
        majors = {
            int.from_bytes(archive.read(name)[6:8], "big")
            for name in archive.namelist()
            if name.endswith(".class")
        }
    if majors != {int(lock["class_major"])}:
        raise AssertionError(majors)
    report["probe_class_major"] = int(lock["class_major"])

    with tempfile.TemporaryDirectory(prefix="jolink-gradle-probe-") as raw:
        root = Path(raw)
        init_script = root / "private-init.gradle"
        init_script.write_bytes(INIT_TEMPLATE.read_bytes())
        init_script.chmod(0o600)
        for version, gradle in distributions:
            distribution_zip = private_distribution_zip(
                root, gradle=gradle, version=version
            )
            for kotlin_dsl in (False, True):
                fixture_root = root / f"gradle-{version}"
                fixture_root.mkdir(exist_ok=True)
                project = create_fixture(fixture_root, kotlin_dsl=kotlin_dsl)
                create_wrapper(
                    project,
                    gradle=gradle,
                    version=version,
                    distribution_zip=distribution_zip,
                    environment=environment,
                )
                before = source_manifest(project)
                output = fixture_root / (
                    ("kotlin" if kotlin_dsl else "groovy")
                    + "-private-model.json"
                )
                completed = run(
                    probe_command(
                        project,
                        probe_jar=probe_jar,
                        init_script=init_script,
                        output=output,
                    ),
                    cwd=project,
                    environment=environment,
                )
                after = source_manifest(project)
                if before != after:
                    raise AssertionError("source changed during Gradle Bootstrap")
                if "sensitive-system-value" in (
                    completed.stdout + completed.stderr
                ) or "sensitive-environment-value" in (
                    completed.stdout + completed.stderr
                ):
                    raise AssertionError("Gradle logs leaked Test task secrets")
                model = json.loads(output.read_text(encoding="utf-8"))
                public = validate_private_model(
                    model, project=project, version=version
                )
                public.update(
                    {
                        "gradle_version": version,
                        "dsl": "kotlin" if kotlin_dsl else "groovy",
                        "source_manifest_stable": True,
                        "private_model_mode": oct(
                            stat.S_IMODE(output.stat().st_mode)
                        ),
                        "sensitive_values_not_logged": True,
                    }
                )
                report["matrix"].append(public)

        cancellation_version, cancellation_gradle = distributions[-1]
        cancellation_distribution = private_distribution_zip(
            root,
            gradle=cancellation_gradle,
            version=cancellation_version,
        )
        cancellation_root = root / "cancellation"
        project = create_fixture(cancellation_root, kotlin_dsl=False)
        create_wrapper(
            project,
            gradle=cancellation_gradle,
            version=cancellation_version,
            distribution_zip=cancellation_distribution,
            environment=environment,
        )
        warm_output = cancellation_root / "warm-private-model.json"
        run(
            probe_command(
                project,
                probe_jar=probe_jar,
                init_script=init_script,
                output=warm_output,
            ),
            cwd=project,
            environment=environment,
        )
        report["cancellation"] = cancellation_gate(
            project,
            probe_jar=probe_jar,
            init_script=init_script,
            java_home=java_home,
            environment=environment,
        )

    print(json.dumps(report, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
