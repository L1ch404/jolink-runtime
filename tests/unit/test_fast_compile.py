from __future__ import annotations

import shutil
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from jolink_runtime.adapters.java.classfile import parse_class_file
from jolink_runtime.launch.fast_compile import (
    FastCompileError,
    FastCompilePlan,
    FastCompiler,
    fast_compile_fingerprint,
)
from jolink_runtime.launch.process_supervisor import (
    CancellationReport,
    OperationResult,
)


def _plan(root: Path) -> tuple[FastCompilePlan, Path, Path]:
    javac = shutil.which("javac")
    if javac is None:
        pytest.skip("javac is required for fast-compile tests")
    project = root / "project with spaces"
    source_root = project / "src" / "main" / "java"
    source = source_root / "example" / "FastExample.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        """\
package example;

public class FastExample {
    public static String value(String input) {
        return input + "-fast";
    }
}
""",
        encoding="utf-8",
    )
    output_root = project / "target" / "classes"
    output_root.mkdir(parents=True)
    config = project / "pom.xml"
    config.write_text("<project/>\n", encoding="utf-8")
    javac_path = Path(javac).resolve()
    fingerprint = fast_compile_fingerprint(
        configuration_inputs=(config,),
        javac_executable=javac_path,
        compile_classpath=(output_root,),
    )
    return (
        FastCompilePlan(
            project_root=project,
            module_root=project,
            source_root=source_root,
            output_root=output_root,
            javac_executable=javac_path,
            compile_classpath=(output_root,),
            encoding="UTF-8",
            configuration_inputs=(config,),
            configuration_fingerprint=fingerprint,
        ),
        source,
        config,
    )


def test_resolve_sources_is_confined_to_selected_module(
    tmp_path: Path,
) -> None:
    plan, source, _config = _plan(tmp_path)

    resolved = plan.resolve_sources(
        [
            source.relative_to(plan.project_root).as_posix(),
            str(source),
        ]
    )

    assert resolved == (source.resolve(),)

    outside = tmp_path / "Outside.java"
    outside.write_text("public class Outside {}\n", encoding="utf-8")
    with pytest.raises(FastCompileError) as rejected:
        plan.resolve_sources([str(outside)])
    assert rejected.value.error_code == "SOURCE_OUTSIDE_SELECTED_MODULE"


def test_compile_uses_private_staging_and_preserves_formal_output(
    tmp_path: Path,
) -> None:
    plan, source, _config = _plan(tmp_path)
    marker = plan.output_root / "formal.marker"
    marker.write_text("unchanged", encoding="utf-8")
    attempt_root = tmp_path / "attempt"
    attempt_root.mkdir()
    compiler = FastCompiler()

    result = compiler.compile(
        plan,
        (source.resolve(),),
        attempt_directory=attempt_root,
        source_release=8,
        include_parameters=True,
    )
    try:
        staged_class = (
            result.classes_directory
            / "example"
            / "FastExample.class"
        )
        assert staged_class.is_file()
        parsed = parse_class_file(staged_class.read_bytes())
        assert parsed.binary_name == "example.FastExample"
        assert marker.read_text(encoding="utf-8") == "unchanged"
        assert not (
            plan.output_root / "example" / "FastExample.class"
        ).exists()
        arguments = result.arg_file.read_text(
            encoding="utf-8",
            errors="replace",
        )
        assert "-proc:none" in arguments
        assert "-implicit:none" in arguments
        assert "-parameters" in arguments
        assert str(result.classes_directory) in arguments
        staged_source = (
            result.staging_directory
            / "sources"
            / "example"
            / "FastExample.java"
        )
        assert str(staged_source) in arguments
        assert result.sources_unchanged() is True
        source.write_text(
            source.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        assert result.sources_unchanged() is False
    finally:
        compiler.discard(result)
    assert not result.staging_directory.exists()


def test_compile_plan_fingerprint_detects_configuration_change(
    tmp_path: Path,
) -> None:
    plan, _source, config = _plan(tmp_path)

    assert plan.is_fresh() is True
    config.write_text("<project><changed/></project>\n", encoding="utf-8")
    assert plan.is_fresh() is False


def test_compile_plan_fingerprint_detects_workspace_classpath_change(
    tmp_path: Path,
) -> None:
    plan, _source, _config = _plan(tmp_path)
    dependency = plan.output_root / "example" / "Dependency.class"
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"launch-dependency")
    fingerprint = fast_compile_fingerprint(
        configuration_inputs=plan.configuration_inputs,
        javac_executable=plan.javac_executable,
        compile_classpath=plan.compile_classpath,
    )
    guarded = replace(
        plan,
        configuration_fingerprint=fingerprint,
    )

    assert guarded.is_fresh() is True
    dependency.write_bytes(b"external-build-dependency")
    assert guarded.is_fresh() is False


def test_failed_compile_discards_private_staging(
    tmp_path: Path,
) -> None:
    plan, source, _config = _plan(tmp_path)
    source.write_text(
        "package example; public class FastExample { this is invalid }\n",
        encoding="utf-8",
    )
    attempt_root = tmp_path / "attempt"
    attempt_root.mkdir()
    compiler = FastCompiler()

    with pytest.raises(FastCompileError) as rejected:
        compiler.compile(
            plan,
            (source.resolve(),),
            attempt_directory=attempt_root,
            source_release=8,
            include_parameters=False,
        )

    assert rejected.value.error_code == "FAST_COMPILE_FAILED"
    assert rejected.value.context["return_code"] != 0
    assert rejected.value.context["compile_log_tail"]
    assert list(attempt_root.iterdir()) == []
    assert list(plan.output_root.iterdir()) == []


def test_close_cancels_in_flight_compile_and_discards_staging(
    tmp_path: Path,
) -> None:
    class BlockingSupervisor:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.cancelled = threading.Event()

        def run(self, spec, *, owner):
            self.started.set()
            self.cancelled.wait(5)
            now = time.monotonic()
            return OperationResult(
                operation_name=spec.operation_name,
                return_code=None,
                cancelled=True,
                timed_out=False,
                started_at=now,
                finished_at=now,
                output_capture=spec.output_capture,
            )

        def close(self, *, deadline):
            self.cancelled.set()
            return CancellationReport(
                owner=None,
                requested=True,
                settled=True,
            )

        def force_close(self, *, deadline):
            return self.close(deadline=deadline)

        def release_owner(self, owner):
            return True

    plan, source, _config = _plan(tmp_path)
    attempt_root = tmp_path / "attempt"
    attempt_root.mkdir()
    supervisor = BlockingSupervisor()
    compiler = FastCompiler(supervisor=supervisor)
    errors: list[BaseException] = []

    def compile_in_background() -> None:
        try:
            compiler.compile(
                plan,
                (source.resolve(),),
                attempt_directory=attempt_root,
                source_release=8,
                include_parameters=False,
            )
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=compile_in_background)
    worker.start()
    assert supervisor.started.wait(2)

    assert compiler.close(deadline=time.monotonic() + 2) is True
    worker.join(2)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], FastCompileError)
    assert errors[0].error_code == "FAST_COMPILE_CANCELLED"
    assert list(attempt_root.iterdir()) == []


def test_source_file_limit_is_structured(tmp_path: Path) -> None:
    plan, source, _config = _plan(tmp_path)

    with pytest.raises(FastCompileError) as invalid_shape:
        plan.resolve_sources(str(source))  # type: ignore[arg-type]
    assert invalid_shape.value.error_code == "INVALID_ARGUMENT"

    with pytest.raises(FastCompileError) as rejected:
        plan.resolve_sources([str(source)] * 17)

    assert rejected.value.error_code == "FAST_COMPILE_LIMIT_EXCEEDED"
    assert rejected.value.context["source_file_limit"] == 16
