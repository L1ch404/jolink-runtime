#!/usr/bin/env python3
"""Run Gradle Build World -> JDT FULL/Tier1/Incremental -> JUnit G2."""

from __future__ import annotations

import argparse
import atexit
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from types import ModuleType

from jolink_runtime.adapters.java.classfile import compare_class_output_tier1
from jolink_runtime.launch.fast_test import FastTestRunner
from jolink_runtime.launch.gradle_test_build_world import (
    GradleBuildWorldError,
    create_gradle_test_build_world,
)
from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate,
    PersistentJdtCompileSession,
    discover_target_system_entries,
)
from jolink_runtime.launch.process_supervisor import (
    AttemptToken,
    ProcessSupervisor,
)


ROOT = Path(__file__).resolve().parent


def load_g1() -> ModuleType:
    source = ROOT / "run_gradle_probe_spike.py"
    spec = importlib.util.spec_from_file_location("jolink_gradle_g1", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load G1 helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_authority(
    model: dict,
    *,
    project: Path,
    expected_compile_java_home: Path,
) -> dict:
    configuration_inputs = tuple(
        (project / name).resolve(strict=False)
        for name in (
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            "settings.gradle.kts",
            "gradle.properties",
            "gradle/libs.versions.toml",
            "gradle/wrapper/gradle-wrapper.properties",
            "gradle/wrapper/gradle-wrapper.jar",
        )
    )
    try:
        world = create_gradle_test_build_world(
            model=model,
            project_root=project,
            configuration_inputs=configuration_inputs,
            runner_environment={},
            configuration_environment_names=(),
        )
    except GradleBuildWorldError as error:
        raise AssertionError(error.error_code) from error
    if world.target_java_home != expected_compile_java_home.resolve(
        strict=True
    ):
        raise AssertionError("GRADLE_COMPILE_TOOLCHAIN_UNMODELED")
    return {
        "main_roots": world.main_source_roots,
        "test_roots": world.test_source_roots,
        "resource_roots": world.resource_roots,
        "main_output": world.main_output,
        "test_output": world.test_output,
        "main_dependencies": world.main_dependencies,
        "test_dependencies": world.test_dependencies,
        "processors": world.processor_entries,
        "target_java_home": world.target_java_home,
        "encoding": world.source_encoding,
        "parameters": world.method_parameters,
        "runtime_paths": tuple(
            Path(value).resolve(strict=False)
            for value in model["testRuntime"]["classpath"]
            if Path(value).resolve(strict=False).exists()
        ),
    }


def freeze_sources(destination: Path, roots: tuple[Path, ...]) -> tuple[Path, ...]:
    frozen: list[Path] = []
    for index, root in enumerate(roots):
        target = destination / str(index)
        target.mkdir(parents=True)
        for source in sorted(root.rglob("*.java")):
            output = target / source.relative_to(root)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, output)
        frozen.append(target)
    return tuple(frozen)


def standard_input_manifest(project: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in (
        "src/main/java",
        "src/test/java",
        "src/main/resources",
        "src/test/resources",
    ):
        root = project / relative
        if not root.is_dir():
            continue
        for source in sorted(root.rglob("*")):
            if source.is_file():
                key = source.relative_to(project).as_posix()
                result[key] = hashlib.sha256(source.read_bytes()).hexdigest()
    return result


def freeze_standard_inputs(
    destination: Path,
    project: Path,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, digest in standard_input_manifest(project).items():
        source = project / key
        output = destination / key
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, output)
        result[key] = hashlib.sha256(output.read_bytes()).hexdigest()
        if result[key] != digest:
            raise AssertionError("SOURCE_CHANGED_DURING_GRADLE_SNAPSHOT")
    return result


def class_tree(*roots: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for index, root in enumerate(roots):
        for source in sorted(root.rglob("*.class")):
            key = f"{index}/{source.relative_to(root).as_posix()}"
            result[key] = hashlib.sha256(source.read_bytes()).hexdigest()
    return result


def complete_tree(*roots: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for index, root in enumerate(roots):
        for source in sorted(root.rglob("*")):
            if source.is_file():
                key = f"{index}/{source.relative_to(root).as_posix()}"
                result[key] = hashlib.sha256(source.read_bytes()).hexdigest()
    return result


def formal_resource_manifest(authority: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for prefix, root in (
        ("main", authority["main_output"]),
        ("test", authority["test_output"]),
    ):
        for source in sorted(root.rglob("*")):
            if source.is_file() and source.suffix != ".class":
                result[f"{prefix}/{source.relative_to(root).as_posix()}"] = (
                    hashlib.sha256(source.read_bytes()).hexdigest()
                )
    return result


def validate_native_processor_resources(
    native: dict[str, str],
    formal: dict[str, str],
) -> None:
    if not native or native != formal:
        raise AssertionError("GRADLE_PROCESSOR_RESOURCE_INCOMPATIBLE")


def authority_fault_injections(
    model: dict,
    *,
    project: Path,
    java11_home: Path,
    root: Path,
) -> int:
    root.mkdir(parents=True, exist_ok=True)
    failures = 0

    def rejected(
        expected: str,
        mutation,
    ) -> None:
        nonlocal failures
        candidate = copy.deepcopy(model)
        mutation(candidate)
        try:
            validate_authority(
                candidate,
                project=project,
                expected_compile_java_home=java11_home,
            )
        except AssertionError as error:
            if expected not in str(error):
                raise
            failures += 1
            return
        raise AssertionError(f"Fault injection {expected} was accepted")

    def reorder_test_classpath(candidate: dict) -> None:
        main = list(candidate["compileJava"]["classpath"])
        test = candidate["compileTestJava"]["classpath"]
        outputs = {
            candidate["compileJava"]["destinationDirectory"],
            candidate["compileTestJava"]["destinationDirectory"],
        }
        extra = next(
            value for value in test if value not in main and value not in outputs
            and "resources/main" not in value.replace("\\", "/")
        )
        test.remove(extra)
        test.insert(0, extra)

    rejected("GRADLE_TEST_CLASSPATH_ORDER_UNMODELED", reorder_test_classpath)
    rejected(
        "GRADLE_COMPILE_TOOLCHAIN_UNMODELED",
        lambda value: value["compileTestJava"].__setitem__(
            "compilerJavaHome", str(root)
        ),
    )
    rejected(
        "GRADLE_COMPILE_CONFIGURATION_UNMODELED",
        lambda value: value["compileJava"].__setitem__("release", 11),
    )
    rejected(
        "GRADLE_SOURCE_LAYOUT_UNSUPPORTED",
        lambda value: value["main"]["javaSourceDirectories"].append(
            str(project / "src/custom/java")
        ),
    )
    rejected(
        "GRADLE_TEST_CONFIGURATION_UNMODELED",
        lambda value: value["testRuntime"].__setitem__(
            "enableAssertions", False
        ),
    )
    rejected(
        "GRADLE_TEST_CONFIGURATION_UNMODELED",
        lambda value: value["testRuntime"]["includeTags"].append("slow"),
    )
    extra_output = root / "extra-output"
    extra_output.mkdir()
    rejected(
        "GRADLE_MAIN_OUTPUT_UNMODELED",
        lambda value: value["main"]["classesDirectories"].append(
            str(extra_output)
        ),
    )
    rejected(
        "GRADLE_CLASSPATH_ENTRY_UNAVAILABLE",
        lambda value: value["compileJava"]["classpath"].__setitem__(
            0, str(root / "missing-dependency.jar")
        ),
    )
    rejected(
        "GRADLE_TEST_RUNTIME_OUTPUT_UNMODELED",
        lambda value: value["testRuntime"]["classpath"].remove(
            value["compileJava"]["destinationDirectory"]
        ),
    )
    fake_lombok = root / "fake-lombok.jar"
    with zipfile.ZipFile(fake_lombok, "w") as archive:
        archive.writestr("lombok/launch/Agent.class", b"class")

    def inject_lombok(candidate: dict) -> None:
        candidate["compileJava"]["annotationProcessorPath"] = [
            str(fake_lombok)
        ]
        candidate["compileTestJava"]["annotationProcessorPath"] = [
            str(fake_lombok)
        ]

    rejected("GRADLE_LOMBOK_UNMODELED", inject_lombok)
    try:
        validate_native_processor_resources(
            {"main/META-INF/generated": "jdt"},
            {"main/META-INF/generated": "gradle"},
        )
    except AssertionError as error:
        if "GRADLE_PROCESSOR_RESOURCE_INCOMPATIBLE" not in str(error):
            raise
        failures += 1
    else:
        raise AssertionError("Processor resource mismatch was accepted")
    return failures


def runtime_classpath(
    authority: dict,
    compiler: PersistentJdtCompileSession,
) -> tuple[Path, ...]:
    result: list[Path] = []
    test_added = False
    main_added = False
    for path in authority["runtime_paths"]:
        if path == authority["test_output"]:
            if not test_added:
                result.append(compiler.test_output_directory)
                test_added = True
            continue
        if path == authority["main_output"]:
            if not main_added:
                result.append(compiler.output_directory)
                main_added = True
            continue
        result.append(path)
    if not test_added or not main_added:
        raise AssertionError("GRADLE_TEST_RUNTIME_OUTPUT_UNMODELED")
    return tuple(dict.fromkeys(result))


def run_test(
    runner: FastTestRunner,
    supervisor: ProcessSupervisor,
    *,
    model: dict,
    authority: dict,
    compiler: PersistentJdtCompileSession,
    project: Path,
    attempt_root: Path,
    sequence: int,
) -> dict:
    owner = AttemptToken(f"gradle_g2_test_{sequence}", sequence)
    try:
        result = runner.run(
            java_executable=Path(
                model["testRuntime"]["javaExecutable"]
            ).resolve(strict=True),
            classpath=runtime_classpath(authority, compiler),
            selectors=("example.TextServiceTest#works",),
            working_directory=Path(
                model["testRuntime"]["workingDirectory"]
            ).resolve(strict=False),
            attempt_directory=attempt_root / f"test-{sequence}",
            timeout_seconds=60,
            owner=owner,
        )
        return {
            "passed": result.passed,
            "framework": result.framework,
            "tests": result.tests,
            "failed_count": result.failed_count,
            "duration_ms": result.duration_ms,
        }
    finally:
        supervisor.release_owner(owner)


def new_session(
    *,
    root: Path,
    candidate: JdtCandidate,
    worker_java_home: Path,
    authority: dict,
    model: dict,
    main_roots: tuple[Path, ...],
    test_roots: tuple[Path, ...],
    frozen_main: tuple[Path, ...],
    frozen_test: tuple[Path, ...],
) -> PersistentJdtCompileSession:
    return PersistentJdtCompileSession(
        root=root,
        candidate=candidate,
        worker_java_home=worker_java_home,
        source_roots=main_roots,
        baseline_source_roots=frozen_main,
        classpath_entries=(
            *discover_target_system_entries(
                authority["target_java_home"], 11
            ),
            *authority["main_dependencies"],
        ),
        source_encoding=authority["encoding"],
        source_level=11,
        method_parameters=authority["parameters"],
        test_source_roots=test_roots,
        baseline_test_source_roots=frozen_test,
        test_classpath_entries=authority["test_dependencies"],
        baseline_main_output=authority["main_output"],
        baseline_test_output=authority["test_output"],
        processor_entries=authority["processors"],
        max_heap_mb=1024,
    )


def assert_supported_test_runtime(model: dict) -> None:
    runtime = model["testRuntime"]
    if runtime["framework"] != "junit_platform":
        raise AssertionError(runtime["framework"])
    if runtime["jvmArgumentProvidersUnmodeled"]:
        raise AssertionError("jvmArgumentProviders are unmodeled")
    if runtime["jvmArgsPrivate"]:
        raise AssertionError("custom Test JVM args are unmodeled")
    if runtime["systemPropertiesPrivate"]:
        raise AssertionError("custom Test system properties are unmodeled")
    if runtime["environmentOverridesPrivate"]:
        raise AssertionError("custom Test environment is unmodeled")
    if runtime["bootstrapClasspath"]:
        raise AssertionError("custom Test bootstrap classpath is unmodeled")
    if runtime["maxParallelForks"] != 1 or runtime["forkEvery"] != 0:
        raise AssertionError("parallel/fork Test semantics are unmodeled")
    if (
        runtime["enableAssertions"] is not True
        or runtime["debug"] is not False
        or runtime["failFast"] is not False
        or runtime["dryRun"] is not False
        or runtime["scanForTestClasses"] is not True
        or runtime["minHeapSize"] is not None
        or runtime["maxHeapSize"] is not None
    ):
        raise AssertionError("GRADLE_TEST_CONFIGURATION_UNMODELED")
    for field in (
        "includePatterns",
        "excludePatterns",
        "includeEngines",
        "excludeEngines",
        "includeTags",
        "excludeTags",
    ):
        if runtime[field]:
            raise AssertionError(
                {"error_code": "GRADLE_TEST_CONFIGURATION_UNMODELED", "field": field}
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-jar", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--gradle", type=Path, required=True)
    parser.add_argument("--gradle-version", required=True)
    parser.add_argument("--kotlin-dsl", action="store_true")
    parser.add_argument("--java-home", type=Path, required=True)
    parser.add_argument("--java8-home", type=Path, required=True)
    parser.add_argument("--java11-home", type=Path, required=True)
    parser.add_argument("--junit-jar", type=Path, action="append", required=True)
    args = parser.parse_args()
    g1 = load_g1()
    probe_jar = args.probe_jar.expanduser().resolve(strict=True)
    lock = json.loads(args.lock.expanduser().resolve(strict=True).read_text())
    if g1.sha256(probe_jar) != lock["sha256"]:
        raise SystemExit("Gradle Probe lock mismatch")
    gradle = args.gradle.expanduser().resolve(strict=True)
    java_home = args.java_home.expanduser().resolve(strict=True)
    java8_home = args.java8_home.expanduser().resolve(strict=True)
    java11_home = args.java11_home.expanduser().resolve(strict=True)
    junit_jars = tuple(
        path.expanduser().resolve(strict=True) for path in args.junit_jar
    )
    environment = {
        **os.environ,
        "JAVA_HOME": str(java_home),
        "PATH": str(java_home / "bin") + os.pathsep + os.environ.get("PATH", ""),
    }
    supervisor = ProcessSupervisor()
    runner = FastTestRunner(supervisor)
    sessions: list[PersistentJdtCompileSession] = []

    def settle() -> bool:
        settled = True
        for session in reversed(sessions):
            settled = session.close() and settled
        return runner.close(deadline=time.monotonic() + 5) and settled

    atexit.register(settle)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="jolink-gradle-g2-") as raw:
        root = Path(raw)
        environment["GRADLE_USER_HOME"] = str(root / "gradle-user-home")
        processor_jar = g1.build_local_processor(root, java8_home)
        project = g1.create_fixture(
            root,
            kotlin_dsl=args.kotlin_dsl,
            processor_jar=processor_jar,
            junit_jars=junit_jars,
            custom_test_runtime=False,
        )
        distribution = g1.private_distribution_zip(
            root, gradle=gradle, version=args.gradle_version
        )
        g1.create_wrapper(
            project,
            gradle=gradle,
            version=args.gradle_version,
            distribution_zip=distribution,
            environment=environment,
        )
        init_script = root / "private-init.gradle"
        init_script.write_bytes(g1.INIT_TEMPLATE.read_bytes())
        init_script.chmod(0o600)
        model_file = root / "private-model.json"
        input_before = standard_input_manifest(project)
        g1.run(
            g1.probe_command(
                project,
                probe_jar=probe_jar,
                init_script=init_script,
                output=model_file,
                probe_sha256=lock["sha256"],
            ),
            cwd=project,
            environment=environment,
            timeout=180,
        )
        model = json.loads(model_file.read_text(encoding="utf-8"))
        authority = validate_authority(
            model,
            project=project,
            expected_compile_java_home=java11_home,
        )
        assert_supported_test_runtime(model)
        fault_injection_count = authority_fault_injections(
            model,
            project=project,
            java11_home=java11_home,
            root=root / "authority-faults",
        )
        input_after = standard_input_manifest(project)
        snapshot_manifest = freeze_standard_inputs(
            root / "frozen-inputs", project
        )
        if not (
            input_before
            == input_after
            == snapshot_manifest
            == standard_input_manifest(project)
        ):
            raise AssertionError("SOURCE_CHANGED_DURING_GRADLE_BOOTSTRAP")

        main_roots = authority["main_roots"]
        test_roots = authority["test_roots"]
        frozen_main = freeze_sources(root / "frozen-main", main_roots)
        frozen_test = freeze_sources(root / "frozen-test", test_roots)
        candidate = JdtCandidate.load_product()
        worker = candidate.select_worker_java(
            (authority["target_java_home"], java_home)
        )
        compiler = new_session(
            root=root / "jdt-session",
            candidate=candidate,
            worker_java_home=worker.home,
            authority=authority,
            model=model,
            main_roots=main_roots,
            test_roots=test_roots,
            frozen_main=frozen_main,
            frozen_test=frozen_test,
        )
        sessions.append(compiler)
        full = compiler.start()
        if not full.compile_ok:
            raise AssertionError(full.diagnostics)
        gradle_main = authority["main_output"]
        gradle_test = authority["test_output"]
        main_tier1 = compare_class_output_tier1(
            gradle_main, compiler.output_directory
        )
        test_tier1 = compare_class_output_tier1(
            gradle_test, compiler.test_output_directory
        )
        if not main_tier1["compatible"] or not test_tier1["compatible"]:
            raise AssertionError({"main": main_tier1, "test": test_tier1})
        formal_resources = formal_resource_manifest(authority)
        native_resources = compiler.native_full_resource_manifest
        validate_native_processor_resources(
            native_resources, formal_resources
        )
        compiler.accept_baseline()

        results: list[dict] = []
        results.append(
            run_test(
                runner,
                supervisor,
                model=model,
                authority=authority,
                compiler=compiler,
                project=project,
                attempt_root=root / "runner-attempts",
                sequence=1,
            )
        )
        if not results[-1]["passed"]:
            raise AssertionError(results[-1])

        main_source = project / "src/main/java/example/TextService.java"
        good_main = main_source.read_text(encoding="utf-8")
        main_source.write_text(
            good_main.replace("return value.strip();", "return value;"),
            encoding="utf-8",
        )
        bad_main_compile = compiler.compile((main_source,))
        results.append(
            run_test(
                runner,
                supervisor,
                model=model,
                authority=authority,
                compiler=compiler,
                project=project,
                attempt_root=root / "runner-attempts",
                sequence=2,
            )
        )
        if results[-1]["passed"]:
            raise AssertionError("main mutation was not observed")
        main_source.write_text(good_main, encoding="utf-8")
        main_recovery_compile = compiler.compile((main_source,))
        results.append(
            run_test(
                runner,
                supervisor,
                model=model,
                authority=authority,
                compiler=compiler,
                project=project,
                attempt_root=root / "runner-attempts",
                sequence=3,
            )
        )
        if not results[-1]["passed"]:
            raise AssertionError(results[-1])

        test_source = project / "src/test/java/example/TextServiceTest.java"
        good_test = test_source.read_text(encoding="utf-8")
        test_source.write_text(
            good_test.replace('assertEquals("x",', 'assertEquals("wrong",'),
            encoding="utf-8",
        )
        bad_test_compile = compiler.compile((test_source,))
        results.append(
            run_test(
                runner,
                supervisor,
                model=model,
                authority=authority,
                compiler=compiler,
                project=project,
                attempt_root=root / "runner-attempts",
                sequence=4,
            )
        )
        if results[-1]["passed"]:
            raise AssertionError("test mutation was not observed")
        test_source.write_text(good_test, encoding="utf-8")
        test_recovery_compile = compiler.compile((test_source,))
        results.append(
            run_test(
                runner,
                supervisor,
                model=model,
                authority=authority,
                compiler=compiler,
                project=project,
                attempt_root=root / "runner-attempts",
                sequence=5,
            )
        )
        if not results[-1]["passed"]:
            raise AssertionError(results[-1])

        oracle_main = freeze_sources(root / "oracle-main", main_roots)
        oracle_test = freeze_sources(root / "oracle-test", test_roots)
        oracle = new_session(
            root=root / "jdt-clean-full-oracle",
            candidate=candidate,
            worker_java_home=worker.home,
            authority=authority,
            model=model,
            main_roots=main_roots,
            test_roots=test_roots,
            frozen_main=oracle_main,
            frozen_test=oracle_test,
        )
        sessions.append(oracle)
        oracle_full = oracle.start()
        if not oracle_full.compile_ok:
            raise AssertionError(oracle_full.diagnostics)
        incremental_tree = complete_tree(
            compiler.output_directory, compiler.test_output_directory
        )
        oracle_tree = complete_tree(
            oracle.output_directory, oracle.test_output_directory
        )
        if incremental_tree != oracle_tree:
            raise AssertionError("incremental output differs from clean-full JDT")
        if (
            compiler.native_full_resource_manifest
            != oracle.native_full_resource_manifest
        ):
            raise AssertionError("native Processor resources differ from clean-full")

        report = {
            "ok": True,
            "gradle_version": model["gradleVersion"],
            "dsl": "kotlin" if args.kotlin_dsl else "groovy",
            "gradle_daemon_java": model["gradleDaemonJavaVersion"],
            "compile_java": model["compileJava"]["compilerJavaVersion"],
            "test_java": model["testRuntime"]["javaVersion"],
            "gradle_main_jdt_tier1": main_tier1["compatible"],
            "gradle_test_jdt_tier1": test_tier1["compatible"],
            "baseline_test_passed": results[0]["passed"],
            "main_failure_observed": not results[1]["passed"],
            "main_recovery_passed": results[2]["passed"],
            "test_failure_observed": not results[3]["passed"],
            "test_recovery_passed": results[4]["passed"],
            "main_incremental_ms": bad_main_compile.elapsed_ms,
            "main_recovery_ms": main_recovery_compile.elapsed_ms,
            "test_incremental_ms": bad_test_compile.elapsed_ms,
            "test_recovery_ms": test_recovery_compile.elapsed_ms,
            "incremental_equals_clean_full": True,
            "native_processor_resource_oracle": True,
            "input_manifest_three_way_equal": True,
            "authority_classpath_order_gate": True,
            "authority_target_jdk_from_model": True,
            "authority_fault_injection_count": fault_injection_count,
            "processor_path_count": len(
                model["compileJava"]["annotationProcessorPath"]
            ),
            "test_runtime_default_gate": True,
            "total_ms": round((time.monotonic() - started) * 1000, 1),
        }
        print(json.dumps(report, separators=(",", ":")))
        if not settle():
            raise RuntimeError("G2 Worker/Runner lifecycle did not settle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
