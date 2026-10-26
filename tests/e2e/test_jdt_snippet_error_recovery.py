"""Actual snippet codegen failures must leave the shared compiler usable."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import subprocess

import pytest

from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate,
    PersistentJdtCompileSession,
    discover_target_system_entries,
)


SOURCE = """package project;
import java.util.function.IntSupplier;
public class Subject {
    private int bias = 7;
    public int number = 11;
    public int observed() {
        IntSupplier privateField = () -> bias;
        IntSupplier publicField = () -> number;
        return privateField.getAsInt() + publicField.getAsInt();
    }
    public static void main(String[] args) throws Exception {
        if (args.length > 0) {
            Class.forName(args[0]);
            return;
        }
        System.out.print(new Subject().observed());
    }
}
"""


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("level", [8, 17, 21])
def test_snippet_codegen_failure_keeps_worker_and_business_incremental_alive(
    tmp_path: Path, level: int,
) -> None:
    if os.environ.get("JOLINK_RUN_MCP_JAVA_E2E") != "1":
        pytest.skip("set JOLINK_RUN_MCP_JAVA_E2E=1")
    home = os.environ.get(f"JOLINK_TEST_JAVA{level}_HOME")
    if not home:
        pytest.skip(f"set JOLINK_TEST_JAVA{level}_HOME")
    java = Path(home)
    sources = tmp_path / "sources"
    source = sources / "project/Subject.java"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    candidate = JdtCandidate.load_product()
    compiler = PersistentJdtCompileSession(
        root=tmp_path / "worker",
        candidate=candidate,
        worker_java_home=candidate.select_worker_java().home,
        source_roots=(sources,),
        classpath_entries=discover_target_system_entries(java, level),
        source_encoding="UTF-8",
        source_level=level,
        preserve_root_on_close=True,
    )

    def output_tree() -> dict[str, str]:
        return {
            str(path.relative_to(compiler.output_directory)):
            hashlib.sha256(path.read_bytes()).hexdigest()
            for path in compiler.output_directory.rglob("*.class")
        }

    def run_business() -> str:
        suffix = ".exe" if os.name == "nt" else ""
        ran = subprocess.run(
            [str(java / f"bin/java{suffix}"), "-cp", str(compiler.output_directory),
             "project.Subject"],
            check=True, capture_output=True, text=True, timeout=15,
        )
        return ran.stdout

    try:
        full = compiler.start()
        assert full.compile_ok, full.diagnostics
        compiler.accept_baseline()
        assert run_business() == "18"
        assert compiler._client is not None
        process = compiler._client.process
        pid = process.pid
        before = output_tree()
        request = {
            "declaring_type": "project.Subject",
            "local_names": ("count",),
            "local_types": ("I",),
        }
        for field in ("bias", "number"):
            # This is an observed ECJ 3.46 snippet-codegen defect, not a
            # rejection invented by joLink. Ordinary Java above supports it.
            failed = compiler.compile_snippet(
                f"((java.util.function.IntSupplier) () -> {field}).getAsInt()",
                **request,
            )
            if level == 8:
                # ECJ's Java 8 codegen does not raise the newer operand-stack
                # assertion. Actually load its class: success from compile
                # alone would falsely count this broken snippet as supported.
                assert failed["ok"] is True, failed
                evaluation = tmp_path / "evaluation" / field
                for item in failed["classes"]:
                    target = evaluation / (item["name"].replace(".", "/") + ".class")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(base64.b64decode(item["bytes_base64"]))
                suffix = ".exe" if os.name == "nt" else ""
                loaded = subprocess.run(
                    [str(java / f"bin/java{suffix}"), "-cp",
                     os.pathsep.join((str(evaluation), str(compiler.output_directory))),
                     "project.Subject", failed["snippet_class"]],
                    capture_output=True, text=True, timeout=15,
                )
                assert loaded.returncode != 0
                assert "java.lang.VerifyError: Bad local variable type" in loaded.stderr
                assert "lambda$0()I @0: aload_0" in loaded.stderr
            else:
                assert failed["ok"] is False, failed
                assert failed["error_code"] == "EVALUATION_COMPILE_FAILED", failed
                assert "java.lang.AssertionError" in failed["message"], failed
                assert "Attempt to push null on operand stack!" in failed["message"], failed
            assert compiler.ready
            assert compiler._client.process.pid == pid
            assert process.poll() is None
            good = compiler.compile_snippet("count + 1", **request)
            assert good["ok"] is True, good
            assert good["classes"]
            assert output_tree() == before

        if level != 8:
            stderr = (compiler.root / "worker.stderr.log").read_text(encoding="utf-8")
            assert "CodeSnippetSingleNameReference.generateReceiver" in stderr
            assert "LambdaExpression.generateCode" in stderr

        # Error isolation must preserve real JavaBuilder state, not just allow
        # the command loop to emit one more response after the assertion.
        source.write_text(SOURCE.replace("bias = 7", "bias = 9"), encoding="utf-8")
        incremental = compiler.compile((source,))
        assert incremental.compile_ok, incremental.diagnostics
        assert incremental.actual_build_kind == "INCREMENTAL"
        assert incremental.compiled_source_count == 1
        assert compiler._client.process.pid == pid
        assert process.poll() is None
        assert run_business() == "20"
        assert compiler.compile_snippet("count * 2", **request)["ok"] is True
    finally:
        compiler.close()
