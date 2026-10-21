"""One attempt owns compile observations from initialization and later builds."""

from types import SimpleNamespace

import pytest

from jolink_runtime.launch.fast_test_manager import FastTestManager, TestAttempt as Attempt
from jolink_runtime.launch.process_supervisor import AttemptToken


def build(kind, sources=(), elapsed=0.0, ok=True):
    return SimpleNamespace(actual_build_kind=kind, compiled_source_units=sources,
                           elapsed_ms=elapsed, compile_ok=ok)


@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("ok", [False, True])
def test_initialization_records_build_before_publication_or_failure(tmp_path, reuse, ok):
    result = build("INCREMENTAL" if reuse else "FULL", ("main/A.java", "test/T.java"), 32.4, ok)
    compiler = SimpleNamespace(
        start=lambda **kw: build(None) if reuse else result,
        workspace_source_changes=lambda: (tmp_path / "A.java",),
        compile=lambda files: result,
        close=lambda: True,
    )
    attempt = Attempt("one", 1, AttemptToken("one", 1), tmp_path, (), ("T",), 30)
    manager = FastTestManager()

    def finish(observed):
        assert observed is result
        if not observed.compile_ok:
            raise RuntimeError("compiler diagnostics")
        return object()

    try:
        if ok:
            manager._run_compiler_initialization_transaction(
                attempt=attempt, compiler=compiler, finish=finish, reuse_workspace=reuse)
        else:
            with pytest.raises(RuntimeError, match="compiler diagnostics"):
                manager._run_compiler_initialization_transaction(
                    attempt=attempt, compiler=compiler, finish=finish, reuse_workspace=reuse)
        assert attempt.summary()["compiled_source_count"] == 2
        assert attempt.compile_ms == 32.4
        # An explicit source hint after initialization can produce a no-op;
        # it must not erase the work done during bootstrap.
        attempt.record_compilation(build(None))
        assert attempt.compiled_source_count == 2 and attempt.compile_ms == 32.4
        attempt.record_compilation(build("INCREMENTAL", ("main/A.java", "main/B.java"), 8.5))
        assert attempt.compiled_source_count == 3
        assert attempt.compile_ms == 40.9
        assert "compiled_source_units" not in attempt.summary()
    finally:
        manager.close()


def test_reopening_unchanged_workspace_does_not_invent_compile_time(tmp_path):
    attempt = Attempt("noop", 1, AttemptToken("noop", 1), tmp_path, (), ("T",), 30)
    attempt.record_compilation(build(None))
    assert attempt.summary()["compiled_source_count"] == 0
    assert "compile_ms" not in attempt.summary()
