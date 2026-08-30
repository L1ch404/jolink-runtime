#!/usr/bin/env python3
"""Validate one JDT project with isolated main/test source and output roots."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate,
    PersistentJdtCompileSession,
    discover_java8_system_entries,
)
from jolink_runtime.launch.fast_test import FastTestRunner
from jolink_runtime.launch.process_supervisor import AttemptToken


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java-home", type=Path, required=True)
    parser.add_argument(
        "--candidate-lock",
        type=Path,
        default=Path(
            "experiments/jdt-incremental-worker/locks/"
            "eclipse-2021-03-apt-spike.json"
        ),
    )
    parser.add_argument(
        "--candidate-cache",
        type=Path,
        default=Path.home() / ".cache/jolink-runtime/jdt-poc",
    )
    parser.add_argument("--junit-jar", type=Path, required=True)
    parser.add_argument("--hamcrest-jar", type=Path, required=True)
    args = parser.parse_args()

    candidate = JdtCandidate.load(args.candidate_lock, args.candidate_cache)
    with tempfile.TemporaryDirectory(prefix="jolink-fast-test-worker-") as raw:
        root = Path(raw)
        main_root = root / "project/src/main/java"
        test_root = root / "project/src/test/java"
        main_source = main_root / "example/Calculator.java"
        test_source = test_root / "example/CalculatorTest.java"
        main_source.parent.mkdir(parents=True)
        test_source.parent.mkdir(parents=True)
        original_main = (
            "package example; public class Calculator { "
            "public int add(int a, int b) { return a + b; } }\n"
        )
        main_source.write_text(original_main, encoding="utf-8")
        test_source.write_text(
            "package example; import org.junit.Test; "
            "import static org.junit.Assert.assertEquals; "
            "public class CalculatorTest { @Test public void adds() { "
            "assertEquals(3, new Calculator().add(1, 2)); } }\n",
            encoding="utf-8",
        )
        session = PersistentJdtCompileSession(
            root=root / "session",
            candidate=candidate,
            worker_java_home=args.java_home,
            source_roots=(main_root,),
            classpath_entries=discover_java8_system_entries(args.java_home),
            source_encoding="UTF-8",
            test_source_roots=(test_root,),
            test_classpath_entries=(args.junit_jar, args.hamcrest_jar),
        )
        try:
            full = session.start()
            if not full.compile_ok:
                raise AssertionError(full.diagnostics)
            if not (session.output_directory / "example/Calculator.class").is_file():
                raise AssertionError("main output missing")
            if not (
                session.test_output_directory / "example/CalculatorTest.class"
            ).is_file():
                raise AssertionError("test output missing")
            session.accept_baseline()

            main_source.write_text(
                "package example; public class Calculator { "
                "public String add() { return \"changed\"; } }\n",
                encoding="utf-8",
            )
            incompatible = session.compile((main_source,))
            if not incompatible.main_compile_ok or incompatible.test_compile_ok:
                raise AssertionError(
                    "main API change did not produce a test-only compile failure: "
                    f"main={incompatible.main_compile_ok} "
                    f"test={incompatible.test_compile_ok} "
                    f"diagnostics={incompatible.diagnostics!r} "
                    f"compiled={incompatible.compiled_source_units!r}"
                )

            main_source.write_text(original_main, encoding="utf-8")
            recovered = session.compile((main_source,))
            if not recovered.compile_ok:
                raise AssertionError(recovered.diagnostics)
            runner = FastTestRunner()
            padding = []
            for index in range(400):
                directory = root / "long classpath 依赖" / str(index)
                directory.mkdir(parents=True)
                padding.append(directory)
            executed = runner.run(
                java_executable=args.java_home / "bin/java",
                classpath=(
                    session.test_output_directory,
                    session.output_directory,
                    *padding,
                    args.junit_jar,
                    args.hamcrest_jar,
                ),
                selectors=("example.CalculatorTest#adds",),
                working_directory=root / "project",
                attempt_directory=root / "test-attempt",
                timeout_seconds=20.0,
                owner=AttemptToken("fast_test_worker", 1),
            )
            runner.close()
            if not executed.passed or executed.tests != 1:
                raise AssertionError(executed)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "full_compiled_sources": full.compiled_source_count,
                        "api_change_main_compile_ok": (
                            incompatible.main_compile_ok
                        ),
                        "api_change_test_compile_ok": (
                            incompatible.test_compile_ok
                        ),
                        "api_change_test_errors": incompatible.test_error_count,
                        "recovery_compile_ok": recovered.compile_ok,
                        "test_framework": executed.framework,
                        "tests": executed.tests,
                        "test_passed": executed.passed,
                        "long_classpath_entry_count": len(padding) + 4,
                    },
                    separators=(",", ":"),
                )
            )
        finally:
            session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
