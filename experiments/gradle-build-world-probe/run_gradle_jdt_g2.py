#!/usr/bin/env python3
"""Run Gradle Build World -> JDT FULL/Tier1/Incremental -> JUnit G2."""

from __future__ import annotations

import argparse
import atexit
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from types import ModuleType

from jolink_runtime.adapters.java.classfile import compare_class_output_tier1
from jolink_runtime.launch.fast_test import FastTestRunner
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


def ordered_paths(values: list[str]) -> tuple[Path, ...]:
    result: list[Path] = []
    for value in values:
        path = Path(value).expanduser().resolve(strict=False)
        if path.exists():
            result.append(path)
    return tuple(result)


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


def class_tree(*roots: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for index, root in enumerate(roots):
        for source in sorted(root.rglob("*.class")):
            key = f"{index}/{source.relative_to(root).as_posix()}"
            result[key] = hashlib.sha256(source.read_bytes()).hexdigest()
    return result


def runtime_classpath(
    model: dict,
    compiler: PersistentJdtCompileSession,
) -> tuple[Path, ...]:
    formal_test_outputs = {
        str(Path(value).resolve(strict=False))
        for value in model["testRuntime"]["testClassesDirectories"]
    }
    formal_main_outputs = {
        str(Path(value).resolve(strict=False))
        for value in model["main"]["classesDirectories"]
    }
    result: list[Path] = []
    test_added = False
    main_added = False
    for raw in model["testRuntime"]["classpath"]:
        normalized = str(Path(raw).resolve(strict=False))
        if normalized in formal_test_outputs:
            if not test_added:
                result.append(compiler.test_output_directory)
                test_added = True
            continue
        if normalized in formal_main_outputs:
            if not main_added:
                result.append(compiler.output_directory)
                main_added = True
            continue
        dependency = Path(raw).resolve(strict=False)
        if dependency.exists():
            result.append(dependency)
    if not test_added:
        result.insert(0, compiler.test_output_directory)
    if not main_added:
        result.insert(1, compiler.output_directory)
    return tuple(dict.fromkeys(result))


def run_test(
    runner: FastTestRunner,
    supervisor: ProcessSupervisor,
    *,
    model: dict,
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
            classpath=runtime_classpath(model, compiler),
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
    java11_home: Path,
    model: dict,
    main_roots: tuple[Path, ...],
    test_roots: tuple[Path, ...],
    frozen_main: tuple[Path, ...],
    frozen_test: tuple[Path, ...],
) -> PersistentJdtCompileSession:
    main_output = Path(model["compileJava"]["destinationDirectory"]).resolve(
        strict=True
    )
    test_output = Path(
        model["compileTestJava"]["destinationDirectory"]
    ).resolve(strict=True)
    main_output_keys = {
        str(path) for path in ordered_paths(model["main"]["classesDirectories"])
    }
    test_output_keys = {
        str(path) for path in ordered_paths(model["test"]["classesDirectories"])
    }
    main_dependencies = tuple(
        path
        for path in ordered_paths(model["compileJava"]["classpath"])
        if str(path) not in main_output_keys | test_output_keys
    )
    main_keys = {str(path) for path in main_dependencies}
    test_dependencies = tuple(
        path
        for path in ordered_paths(model["compileTestJava"]["classpath"])
        if str(path) not in main_output_keys | test_output_keys
        and str(path) not in main_keys
    )
    main_processors = ordered_paths(
        model["compileJava"]["annotationProcessorPath"]
    )
    test_processors = ordered_paths(
        model["compileTestJava"]["annotationProcessorPath"]
    )
    if main_processors != test_processors:
        raise AssertionError("G2 requires identical main/test Processor paths")
    if model["compileJava"]["compilerArgsPrivate"] or model[
        "compileTestJava"
    ]["compilerArgsPrivate"]:
        raise AssertionError("G2 does not model compiler arguments yet")
    return PersistentJdtCompileSession(
        root=root,
        candidate=candidate,
        worker_java_home=worker_java_home,
        source_roots=main_roots,
        baseline_source_roots=frozen_main,
        classpath_entries=(
            *discover_target_system_entries(java11_home, 11),
            *main_dependencies,
        ),
        source_encoding=model["compileJava"]["encoding"] or "UTF-8",
        source_level=11,
        test_source_roots=test_roots,
        baseline_test_source_roots=frozen_test,
        test_classpath_entries=test_dependencies,
        baseline_main_output=main_output,
        baseline_test_output=test_output,
        processor_entries=main_processors,
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
        source_before = g1.source_manifest(project)
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
        if source_before != g1.source_manifest(project):
            raise AssertionError("source changed during Gradle Bootstrap")
        model = json.loads(model_file.read_text(encoding="utf-8"))
        assert_supported_test_runtime(model)

        main_roots = ordered_paths(model["main"]["javaSourceDirectories"])
        test_roots = ordered_paths(model["test"]["javaSourceDirectories"])
        frozen_main = freeze_sources(root / "frozen-main", main_roots)
        frozen_test = freeze_sources(root / "frozen-test", test_roots)
        candidate = JdtCandidate.load_product()
        worker = candidate.select_worker_java((java11_home, java_home))
        compiler = new_session(
            root=root / "jdt-session",
            candidate=candidate,
            worker_java_home=worker.home,
            java11_home=java11_home,
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
        gradle_main = Path(model["compileJava"]["destinationDirectory"])
        gradle_test = Path(model["compileTestJava"]["destinationDirectory"])
        main_tier1 = compare_class_output_tier1(
            gradle_main, compiler.output_directory
        )
        test_tier1 = compare_class_output_tier1(
            gradle_test, compiler.test_output_directory
        )
        if not main_tier1["compatible"] or not test_tier1["compatible"]:
            raise AssertionError({"main": main_tier1, "test": test_tier1})
        compiler.accept_baseline()

        results: list[dict] = []
        results.append(
            run_test(
                runner,
                supervisor,
                model=model,
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
            java11_home=java11_home,
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
        incremental_tree = class_tree(
            compiler.output_directory, compiler.test_output_directory
        )
        oracle_tree = class_tree(
            oracle.output_directory, oracle.test_output_directory
        )
        if incremental_tree != oracle_tree:
            raise AssertionError("incremental output differs from clean-full JDT")

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
