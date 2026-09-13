"""Real JavaBuilder coverage with no search thread or queued indexing work."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

import pytest

from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate,
    PersistentJdtCompileSession,
    discover_java8_system_entries,
)


@pytest.mark.mcp_java_e2e
def test_compiler_without_index_handles_secondary_types_and_reopens(tmp_path: Path):
    if os.environ.get("JOLINK_RUN_MCP_JAVA_E2E") != "1":
        pytest.skip("set JOLINK_RUN_MCP_JAVA_E2E=1")
    home = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not home:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    jdk = Path(home)
    suffix = ".exe" if os.name == "nt" else ""
    sources = tmp_path / "sources"
    package = sources / "example"
    package.mkdir(parents=True)
    holder = package / "Holder.java"
    use = package / "Use.java"
    app = package / "App.java"
    holder_text = """package example;
public class Holder { public interface Box<T> { T value(); } }
class Secondary implements Holder.Box<String> {
  public String value() { return "one"; }
}
"""
    use_text = """package example;
public class Use {
  public static String value() {
    java.util.function.Supplier<Holder.Box<String>> factory = Secondary::new;
    return java.util.stream.Stream.of(factory.get()).map(Holder.Box::value)
        .map(value -> value + "!").map(String::toUpperCase)
        .collect(java.util.stream.Collectors.joining());
  }
}
"""
    holder.write_text(holder_text)
    use.write_text(use_text)
    app.write_text(
        "package example; public class App { public static void main(String[] a) { System.out.print(Use.value()); } }"
    )
    candidate = JdtCandidate.load_product()

    def session(name="worker"):
        return PersistentJdtCompileSession(
            root=tmp_path / name,
            candidate=candidate,
            worker_java_home=candidate.select_worker_java().home,
            source_roots=(sources,),
            classpath_entries=discover_java8_system_entries(jdk),
            source_encoding="UTF-8",
            preserve_root_on_close=True,
        )

    def check(compiler, expected):
        index = compiler._client.command("METRICS")["metrics"]["search_indexing"]
        assert index["disabled"] is True
        assert index["queued_jobs"] == 0
        assert index["discarded_requests"] > 0
        ran = subprocess.run(
            [
                str(jdk / f"bin/java{suffix}"),
                "-cp",
                str(compiler.output_directory),
                "example.App",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        assert ran.stdout == expected

    def tree(compiler):
        return {
            str(p.relative_to(compiler.output_directory)): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in compiler.output_directory.rglob("*.class")
        }

    first = session()
    try:
        full = first.start()
        assert full.compile_ok, full.diagnostics
        first.accept_baseline()
        check(first, "ONE!")
        assert first.output_directory.joinpath("example/Secondary.class").is_file()
        # A secondary type is located by the builder, despite its different filename.
        holder.write_text(holder_text.replace('"one"', '"two"'))
        changed = first.compile((holder,))
        assert changed.compile_ok, changed.diagnostics
        assert changed.actual_build_kind == "INCREMENTAL"
        check(first, "TWO!")
        holder_text = holder_text.replace("Secondary", "Replacement")
        use_text = use_text.replace("Secondary", "Replacement")
        holder.write_text(holder_text)
        use.write_text(use_text)
        assert first.compile((holder, use)).compile_ok
        assert not first.output_directory.joinpath("example/Secondary.class").exists()
        check(first, "ONE!")
        use.write_text(use_text.replace("String::toUpperCase", "String::missingMethod"))
        invalid = first.compile((use,))
        assert not invalid.compile_ok
        assert invalid.error_count > 0
        use.write_text(use_text)
        assert first.compile((use,)).compile_ok
        holder.unlink()
        assert not first.compile((holder,)).compile_ok
        holder.write_text(holder_text)
        assert first.compile((holder,)).compile_ok
        check(first, "ONE!")
        first.accept_baseline()
        first.save_source_index()
    finally:
        first.close()

    reopened = session()
    try:
        assert reopened.start(reuse_workspace=True, build_on_reuse=False).compile_ok
        assert reopened.workspace_reopened
        # Repeated edits must not accumulate postponed indexing requests.
        for number in range(100):
            holder.write_text(holder_text.replace('"one"', f'"value{number}"'))
            result = reopened.compile((holder,))
            assert result.compile_ok, result.diagnostics
            assert result.actual_build_kind == "INCREMENTAL"
            assert (
                reopened._client.command("METRICS")["metrics"]["search_indexing"][
                    "queued_jobs"
                ]
                == 0
            )
        check(reopened, "VALUE99!")
        dump = subprocess.run(
            [
                str(jdk / f"bin/jcmd{suffix}"),
                str(reopened._client.process.pid),
                "Thread.print",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout
        assert '"Java indexing"' not in dump
        assert "org.eclipse.jdt.internal.core.search.indexing" not in dump
        oracle = session("oracle")
        try:
            assert oracle.start().compile_ok
            assert tree(reopened) == tree(oracle)
        finally:
            oracle.close()
    finally:
        reopened.close()
