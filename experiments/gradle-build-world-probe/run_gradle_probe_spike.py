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
import zipfile
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


def build_local_processor(root: Path, java8_home: Path) -> Path:
    source = root / "processor-src/fixture/MarkerProcessor.java"
    classes = root / "processor-classes"
    source.parent.mkdir(parents=True)
    classes.mkdir()
    source.write_text(
        "package fixture; "
        "@javax.annotation.processing.SupportedAnnotationTypes(\"*\") "
        "@javax.annotation.processing.SupportedSourceVersion("
        "javax.lang.model.SourceVersion.RELEASE_8) "
        "public class MarkerProcessor extends javax.annotation.processing.AbstractProcessor { "
        "private boolean written; public boolean process("
        "java.util.Set<? extends javax.lang.model.element.TypeElement> annotations, "
        "javax.annotation.processing.RoundEnvironment round) { "
        "if (!written) { try { javax.tools.FileObject file = processingEnv.getFiler()"
        ".createResource(javax.tools.StandardLocation.CLASS_OUTPUT, \"\", "
        "\"META-INF/jolink-processor.txt\"); "
        "try (java.io.Writer out = file.openWriter()) { out.write(\"processed\"); } "
        "written = true; } catch (Exception error) { throw new RuntimeException(error); } } "
        "return false; } }\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        (
            str(java8_home / "bin/javac"),
            "-source",
            "8",
            "-target",
            "8",
            "-d",
            str(classes),
            str(source),
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stdout + completed.stderr)
    jar = root / "marker-processor.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        for class_file in sorted(classes.rglob("*.class")):
            archive.write(class_file, class_file.relative_to(classes).as_posix())
        archive.writestr(
            "META-INF/services/javax.annotation.processing.Processor",
            "fixture.MarkerProcessor\n",
        )
    return jar


def create_fixture(
    root: Path,
    *,
    kotlin_dsl: bool,
    processor_jar: Path | None = None,
    junit_jars: tuple[Path, ...] = (),
    custom_test_runtime: bool = True,
) -> Path:
    project = root / ("kotlin-dsl" if kotlin_dsl else "groovy-dsl")
    main = project / "src/main/java/example/TextService.java"
    test = project / "src/test/java/example/TextServiceTest.java"
    main.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    libraries = project / "libs"
    libraries.mkdir()
    for name, value in (("dependency-a.jar", "A"), ("dependency-b.jar", "B")):
        with zipfile.ZipFile(libraries / name, "w") as archive:
            archive.writestr("META-INF/jolink-order.txt", value)
    if processor_jar is None:
        with zipfile.ZipFile(libraries / "processor.jar", "w") as archive:
            archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n\n")
    else:
        shutil.copyfile(processor_jar, libraries / "processor.jar")
    junit_directory = libraries / "junit"
    junit_directory.mkdir()
    for jar in junit_jars:
        shutil.copyfile(jar, junit_directory / jar.name)
    (project / ("settings.gradle.kts" if kotlin_dsl else "settings.gradle")).write_text(
        'rootProject.name = "jolink-gradle-fixture"\n'
        if kotlin_dsl
        else "rootProject.name = 'jolink-gradle-fixture'\n",
        encoding="utf-8",
    )
    (project / "gradle.properties").write_text(
        "org.gradle.configuration-cache=true\n",
        encoding="utf-8",
    )
    if kotlin_dsl:
        build = """plugins { java }

java {
    sourceCompatibility = JavaVersion.VERSION_11
    targetCompatibility = JavaVersion.VERSION_11
    toolchain { languageVersion.set(JavaLanguageVersion.of(11)) }
}

dependencies {
    implementation(files("libs/dependency-a.jar", "libs/dependency-b.jar"))
    annotationProcessor(files("libs/processor.jar"))
    testAnnotationProcessor(files("libs/processor.jar"))
    testImplementation(fileTree("libs/junit") { include("*.jar") })
}

tasks.withType<JavaCompile>().configureEach {
    options.encoding = "UTF-8"
}

tasks.test {
    useJUnitPlatform()
%s
}
""" % (
            """    workingDir = layout.buildDirectory.dir("test-working").get().asFile
    systemProperty("jolink.fixture.token", "sensitive-system-value")
    environment("JOLINK_FIXTURE_ENV", "sensitive-environment-value")
    jvmArgs("-Xmx128m")"""
            if custom_test_runtime
            else ""
        )
    else:
        build = """plugins { id 'java' }

java {
    sourceCompatibility = JavaVersion.VERSION_11
    targetCompatibility = JavaVersion.VERSION_11
    toolchain { languageVersion = JavaLanguageVersion.of(11) }
}

dependencies {
    implementation files('libs/dependency-a.jar', 'libs/dependency-b.jar')
    annotationProcessor files('libs/processor.jar')
    testAnnotationProcessor files('libs/processor.jar')
    testImplementation fileTree(dir: 'libs/junit', include: ['*.jar'])
}

tasks.withType(JavaCompile).configureEach {
    options.encoding = 'UTF-8'
}

test {
    useJUnitPlatform()
%s
}
""" % (
            """    workingDir = layout.buildDirectory.dir('test-working').get().asFile
    systemProperty 'jolink.fixture.token', 'sensitive-system-value'
    environment 'JOLINK_FIXTURE_ENV', 'sensitive-environment-value'
    jvmArgs '-Xmx128m'"""
            if custom_test_runtime
            else ""
        )
    (project / ("build.gradle.kts" if kotlin_dsl else "build.gradle")).write_text(
        build, encoding="utf-8"
    )
    main.write_text(
        "package example; public class TextService { "
        "public String strip(String value) { return value.strip(); } "
        "public static void main(String[] args) throws Exception { "
        "try (java.io.InputStream in = TextService.class.getClassLoader()"
        ".getResourceAsStream(\"META-INF/jolink-order.txt\")) { "
        "System.out.print((char) in.read()); } } }\n",
        encoding="utf-8",
    )
    test.write_text(
        (
            "package example; import org.junit.jupiter.api.Test; "
            "import static org.junit.jupiter.api.Assertions.assertEquals; "
            "public class TextServiceTest { @Test public void works() { "
            "assertEquals(\"x\", new TextService().strip(\" x \")); } }\n"
            if junit_jars
            else "package example; public class TextServiceTest { "
            "public void placeholder() { new TextService().strip(\" x \" ); } }\n"
        ),
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
    probe_sha256: str,
    slow_compile_millis: int = 0,
) -> tuple[str, ...]:
    request_id = request_identity(output)
    return (
        str(project / "gradlew"),
        "--offline",
        "--no-configuration-cache",
        "--stacktrace",
        "-I",
        str(init_script),
        f"-Djolink.gradle.probeJar={probe_jar}",
        f"-Djolink.gradle.output={output}",
        "-Djolink.gradle.targetProject=:",
        f"-Djolink.gradle.requestId={request_id}",
        f"-Djolink.gradle.probeSha256={probe_sha256}",
        f"-Djolink.gradle.slowCompileMillis={slow_compile_millis}",
        f"jolinkExportBuildWorld_{probe_sha256[:12]}",
    )


def request_identity(output: Path) -> str:
    return "req_" + hashlib.sha256(
        str(output.resolve(strict=False)).encode("utf-8")
    ).hexdigest()[:24]


def validate_private_model(
    model: dict,
    *,
    project: Path,
    version: str,
    output: Path,
    probe_sha256: str,
    expected_daemon_java: int = 17,
) -> dict:
    assert model["ok"] is True
    assert model["schema"] == "jolink.gradle-build-world-probe.v1"
    assert model["gradleVersion"] == version
    assert model["projectPath"] == ":"
    daemon_version = str(model["gradleDaemonJavaVersion"])
    assert daemon_version == (
        "1.8" if expected_daemon_java == 8 else str(expected_daemon_java)
    )
    assert model["targetProjectPath"] == ":"
    assert model["probeSha256"] == probe_sha256
    assert model["requestId"] == request_identity(output)
    assert model["exportTaskName"] == (
        "jolinkExportBuildWorld_" + probe_sha256[:12]
    )
    assert model["compileJava"]["sourceCompatibility"] == "11"
    assert model["compileJava"]["targetCompatibility"] == "11"
    assert model["compileTestJava"]["sourceCompatibility"] == "11"
    assert model["testRuntime"]["framework"] == "junit_platform"
    assert model["compileJava"]["compilerJavaVersion"] == 11
    assert model["compileTestJava"]["compilerJavaVersion"] == 11
    assert model["testRuntime"]["javaVersion"] == 11
    assert model["testRuntime"]["javaSelectionSource"] == (
        "resolved_java_launcher"
    )
    assert model["testRuntime"]["jvmArgumentProviderCount"] == 0
    assert model["testRuntime"]["jvmArgumentProvidersUnmodeled"] is False
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
    dependency_a = str((project / "libs/dependency-a.jar").resolve())
    dependency_b = str((project / "libs/dependency-b.jar").resolve())
    processor = str((project / "libs/processor.jar").resolve())
    compile_classpath = model["compileJava"]["classpath"]
    runtime_classpath = model["main"]["runtimeClasspath"]
    assert compile_classpath.index(dependency_a) < compile_classpath.index(
        dependency_b
    )
    assert runtime_classpath.index(dependency_a) < runtime_classpath.index(
        dependency_b
    )
    assert model["compileJava"]["annotationProcessorPath"] == [processor]
    assert model["compileTestJava"]["annotationProcessorPath"] == [processor]
    junit_runtime = [
        path
        for path in model["test"]["runtimeClasspath"]
        if "/libs/junit/" in path.replace("\\", "/")
    ]
    if not junit_runtime or not any(
        "junit-jupiter-api" in path for path in junit_runtime
    ):
        raise AssertionError(junit_runtime)
    assert (
        project / "build/classes/java/main/META-INF/jolink-processor.txt"
    ).read_text(encoding="utf-8") == "processed"
    assert (
        project / "build/classes/java/test/META-INF/jolink-processor.txt"
    ).read_text(encoding="utf-8") == "processed"
    return {
        "source_level": 11,
        "target_level": 11,
        "gradle_daemon_java_version": expected_daemon_java,
        "main_source_root_count": len(main_sources),
        "test_source_root_count": len(test_sources),
        "test_framework": model["testRuntime"]["framework"],
        "compile_java_version": model["compileJava"]["compilerJavaVersion"],
        "test_java_version": model["testRuntime"]["javaVersion"],
        "test_java_selection_source": model["testRuntime"][
            "javaSelectionSource"
        ],
        "classpath_order_preserved": True,
        "annotation_processor_path_observed": True,
        "annotation_processor_executed": True,
        "junit_runtime_jar_count": len(junit_runtime),
        "system_property_override_count": len(
            model["testRuntime"]["systemPropertyNames"]
        ),
        "environment_override_count": len(
            model["testRuntime"]["environmentOverrideNames"]
        ),
        "system_property_names": model["testRuntime"][
            "systemPropertyNames"
        ],
        "environment_override_names": model["testRuntime"][
            "environmentOverrideNames"
        ],
    }


def validate_java_resource_order(
    model: dict,
    *,
    project: Path,
    java_home: Path,
    environment: dict[str, str],
) -> None:
    completed = run(
        (
            str(java_home / "bin/java"),
            "-cp",
            os.pathsep.join(model["main"]["runtimeClasspath"]),
            "example.TextService",
        ),
        cwd=project,
        environment=environment,
        timeout=30,
    )
    if completed.stdout != "A":
        raise AssertionError(completed.stdout)


def run_boundary_failure(
    project: Path,
    *,
    probe_jar: Path,
    probe_sha256: str,
    init_script: Path,
    output: Path,
    environment: dict[str, str],
    expected_code: str,
    structured: bool = True,
) -> None:
    completed = subprocess.run(
        probe_command(
            project,
            probe_jar=probe_jar,
            init_script=init_script,
            output=output,
            probe_sha256=probe_sha256,
        ),
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if completed.returncode == 0:
        raise AssertionError(f"Boundary {expected_code} was accepted")
    combined = completed.stdout + completed.stderr
    if structured:
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["ok"] is False
        assert payload["errorCode"] == expected_code
        assert payload["probeSha256"] == probe_sha256
        assert payload["requestId"] == request_identity(output)
    elif expected_code not in combined:
        raise AssertionError(combined)


def boundary_gates(
    root: Path,
    *,
    gradle: Path,
    version: str,
    distribution_zip: Path,
    probe_jar: Path,
    probe_sha256: str,
    init_script: Path,
    environment: dict[str, str],
) -> dict:
    cases: list[tuple[str, str, bool]] = []

    multi_root = root / "multi"
    multi = create_fixture(multi_root, kotlin_dsl=False)
    with (multi / "settings.gradle").open("a", encoding="utf-8") as stream:
        stream.write("include 'child'\n")
    child = multi / "child"
    child.mkdir()
    (child / "build.gradle").write_text(
        "plugins { id 'java' }\n", encoding="utf-8"
    )
    create_wrapper(
        multi,
        gradle=gradle,
        version=version,
        distribution_zip=distribution_zip,
        environment=environment,
    )
    run_boundary_failure(
        multi,
        probe_jar=probe_jar,
        probe_sha256=probe_sha256,
        init_script=init_script,
        output=multi_root / "multi.json",
        environment=environment,
        expected_code="GRADLE_MULTI_PROJECT_UNSUPPORTED",
    )
    cases.append(("multi_project", "GRADLE_MULTI_PROJECT_UNSUPPORTED", True))

    source_root = root / "source-set"
    source_project = create_fixture(source_root, kotlin_dsl=False)
    with (source_project / "build.gradle").open("a", encoding="utf-8") as stream:
        stream.write("\nsourceSets { integrationTest }\n")
    create_wrapper(
        source_project,
        gradle=gradle,
        version=version,
        distribution_zip=distribution_zip,
        environment=environment,
    )
    run_boundary_failure(
        source_project,
        probe_jar=probe_jar,
        probe_sha256=probe_sha256,
        init_script=init_script,
        output=source_root / "source-set.json",
        environment=environment,
        expected_code="GRADLE_SOURCE_SET_UNSUPPORTED",
    )
    cases.append(("extra_source_set", "GRADLE_SOURCE_SET_UNSUPPORTED", True))

    task_root = root / "test-task"
    task_project = create_fixture(task_root, kotlin_dsl=False)
    with (task_project / "build.gradle").open("a", encoding="utf-8") as stream:
        stream.write("\ntasks.register('integrationTest', Test)\n")
    create_wrapper(
        task_project,
        gradle=gradle,
        version=version,
        distribution_zip=distribution_zip,
        environment=environment,
    )
    run_boundary_failure(
        task_project,
        probe_jar=probe_jar,
        probe_sha256=probe_sha256,
        init_script=init_script,
        output=task_root / "test-task.json",
        environment=environment,
        expected_code="GRADLE_TEST_TASK_UNSUPPORTED",
    )
    cases.append(("extra_test_task", "GRADLE_TEST_TASK_UNSUPPORTED", True))

    conflict_root = root / "task-conflict"
    conflict = create_fixture(conflict_root, kotlin_dsl=False)
    build_file = conflict / "build.gradle"
    task_name = "jolinkExportBuildWorld_" + probe_sha256[:12]
    rendered = build_file.read_text(encoding="utf-8").replace(
        "plugins { id 'java' }",
        f"tasks.register('{task_name}')\napply plugin: 'java'",
    )
    build_file.write_text(rendered, encoding="utf-8")
    create_wrapper(
        conflict,
        gradle=gradle,
        version=version,
        distribution_zip=distribution_zip,
        environment=environment,
    )
    run_boundary_failure(
        conflict,
        probe_jar=probe_jar,
        probe_sha256=probe_sha256,
        init_script=init_script,
        output=conflict_root / "conflict.json",
        environment=environment,
        expected_code="GRADLE_PROBE_TASK_CONFLICT",
        structured=False,
    )
    cases.append(("task_conflict", "GRADLE_PROBE_TASK_CONFLICT", False))

    return {
        "passed": True,
        "cases": [
            {"name": name, "error_code": code, "structured": structured}
            for name, code, structured in cases
        ],
    }


def private_daemon_pids(
    gradle_user_home: Path,
    *,
    version: str,
) -> set[int]:
    result: set[int] = set()
    for log in (gradle_user_home / "daemon" / version).glob(
        "daemon-*.out.log"
    ):
        try:
            pid = int(log.stem.split("-", 1)[1].split(".", 1)[0])
            os.kill(pid, 0)
        except (ValueError, OSError):
            continue
        result.add(pid)
    return result


def cancellation_gate(
    project: Path,
    *,
    probe_jar: Path,
    init_script: Path,
    java_home: Path,
    environment: dict[str, str],
    probe_sha256: str,
    gradle_version: str,
    gradle_user_home: Path,
) -> dict:
    output = project.parent / "cancelled-private-model.json"
    marker = output.with_name(output.name + ".started")
    log = project.parent / "cancelled-gradle.log"
    output.unlink(missing_ok=True)
    marker.unlink(missing_ok=True)
    before = private_daemon_pids(
        gradle_user_home, version=gradle_version
    )
    if len(before) != 1:
        raise AssertionError(
            f"Expected one private Gradle daemon, found {sorted(before)}"
        )
    class_output = project / "build/classes/java/main/example/TextService.class"
    class_output.unlink(missing_ok=True)
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
                        probe_sha256=probe_sha256,
                        slow_compile_millis=12_000,
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
    if thread.is_alive() or not cancellation.requested or not cancellation.settled:
        raise RuntimeError("Gradle client cancellation did not settle")
    if len(result_box) != 1:
        raise AssertionError("Gradle operation result is unavailable")
    operation = result_box[0]
    if not operation.cancelled or operation.timed_out:
        raise AssertionError(operation)
    # The daemon-side action must not continue to publish after its client was
    # cancelled. Wait beyond the requested slow task duration.
    time.sleep(12.5)
    if output.exists():
        raise AssertionError("Cancelled Gradle task published a late model")
    if class_output.exists():
        raise AssertionError("Cancelled compile chain produced a late class")
    after = private_daemon_pids(
        gradle_user_home, version=gradle_version
    )
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
            probe_sha256=probe_sha256,
        ),
        cwd=project,
        environment=environment,
        timeout=30,
    )
    final = private_daemon_pids(
        gradle_user_home, version=gradle_version
    )
    if not before <= final or not class_output.is_file():
        raise AssertionError("Recovery did not reuse the private daemon/build")
    return {
        "cancel_settled": True,
        "late_publish_blocked": True,
        "shared_daemon_survived": bool(surviving or not before),
        "post_cancel_build_passed": recovery_output.is_file(),
        "operation_cancelled": operation.cancelled,
        "operation_timed_out": operation.timed_out,
        "exact_daemon_pid": next(iter(before)),
        "compile_output_blocked": True,
        "recovery_reused_same_daemon": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-jar", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--java-home", type=Path, required=True)
    parser.add_argument("--java8-home", type=Path, required=True)
    parser.add_argument(
        "--junit-jar", type=Path, action="append", required=True
    )
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
    java8_home = args.java8_home.expanduser().resolve(strict=True)
    junit_jars = tuple(
        path.expanduser().resolve(strict=True) for path in args.junit_jar
    )
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
        gradle_user_home = root / "gradle-user-home"
        environment = {
            **environment,
            "GRADLE_USER_HOME": str(gradle_user_home),
        }
        init_script = root / "private-init.gradle"
        init_script.write_bytes(INIT_TEMPLATE.read_bytes())
        init_script.chmod(0o600)
        processor_jar = build_local_processor(root, java8_home)
        for version, gradle in distributions:
            distribution_zip = private_distribution_zip(
                root, gradle=gradle, version=version
            )
            for kotlin_dsl in (False, True):
                fixture_root = root / f"gradle-{version}"
                fixture_root.mkdir(exist_ok=True)
                project = create_fixture(
                    fixture_root,
                    kotlin_dsl=kotlin_dsl,
                    processor_jar=processor_jar,
                    junit_jars=junit_jars,
                )
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
                        probe_sha256=probe_identity,
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
                    model,
                    project=project,
                    version=version,
                    output=output,
                    probe_sha256=probe_identity,
                    expected_daemon_java=17,
                )
                validate_java_resource_order(
                    model,
                    project=project,
                    java_home=java_home,
                    environment=environment,
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
                        "runtime_resource_order_verified": True,
                        "configuration_cache_forced_off": True,
                    }
                )
                report["matrix"].append(public)

        java8_environment = {
            **environment,
            "JAVA_HOME": str(java8_home),
            "PATH": str(java8_home / "bin")
            + os.pathsep
            + os.environ.get("PATH", ""),
            "GRADLE_USER_HOME": str(root / "gradle-user-home-jdk8"),
        }
        java8_version, java8_gradle = distributions[0]
        java8_distribution = private_distribution_zip(
            root, gradle=java8_gradle, version=java8_version
        )
        java8_root = root / "jdk8-daemon"
        java8_project = create_fixture(
            java8_root,
            kotlin_dsl=False,
            processor_jar=processor_jar,
            junit_jars=junit_jars,
        )
        create_wrapper(
            java8_project,
            gradle=java8_gradle,
            version=java8_version,
            distribution_zip=java8_distribution,
            environment=java8_environment,
        )
        java8_output = java8_root / "private-model.json"
        run(
            probe_command(
                java8_project,
                probe_jar=probe_jar,
                init_script=init_script,
                output=java8_output,
                probe_sha256=probe_identity,
            ),
            cwd=java8_project,
            environment=java8_environment,
        )
        java8_model = json.loads(java8_output.read_text(encoding="utf-8"))
        java8_public = validate_private_model(
            java8_model,
            project=java8_project,
            version=java8_version,
            output=java8_output,
            probe_sha256=probe_identity,
            expected_daemon_java=8,
        )
        validate_java_resource_order(
            java8_model,
            project=java8_project,
            java_home=java_home,
            environment=environment,
        )
        java8_public.update(
            {
                "gradle_version": java8_version,
                "dsl": "groovy",
                "source_manifest_stable": True,
                "private_model_mode": oct(
                    stat.S_IMODE(java8_output.stat().st_mode)
                ),
                "sensitive_values_not_logged": True,
                "runtime_resource_order_verified": True,
                "configuration_cache_forced_off": True,
                "probe_loaded_on_java8_daemon": True,
            }
        )
        report["matrix"].append(java8_public)

        cancellation_version, cancellation_gradle = distributions[-1]
        cancellation_distribution = private_distribution_zip(
            root,
            gradle=cancellation_gradle,
            version=cancellation_version,
        )
        report["boundary_gates"] = boundary_gates(
            root / "boundaries",
            gradle=cancellation_gradle,
            version=cancellation_version,
            distribution_zip=cancellation_distribution,
            probe_jar=probe_jar,
            probe_sha256=probe_identity,
            init_script=init_script,
            environment=environment,
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
                probe_sha256=probe_identity,
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
            probe_sha256=probe_identity,
            gradle_version=cancellation_version,
            gradle_user_home=gradle_user_home,
        )

    print(json.dumps(report, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
