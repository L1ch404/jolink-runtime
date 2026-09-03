"""Real Worker reopen with APT, errors-only diagnostics, and unchanged config."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import zipfile

import pytest

from jolink_runtime.launch.jdt_compile_session import (
    JdtCandidate, PersistentJdtCompileSession, discover_java8_system_entries,
)


@pytest.mark.mcp_java_e2e
def test_real_persisted_worker_reuses_apt_and_collects_only_errors(tmp_path: Path) -> None:
    if os.environ.get("JOLINK_RUN_MCP_JAVA_E2E") != "1":
        pytest.skip("set JOLINK_RUN_MCP_JAVA_E2E=1")
    home = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not home:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    jdk = Path(home)
    suffix = ".exe" if os.name == "nt" else ""
    processor_source = tmp_path / "EvidenceProcessor.java"
    processor_source.write_text('''
import java.io.Writer;
import java.util.Set;
import javax.annotation.processing.*;
import javax.lang.model.SourceVersion;
import javax.lang.model.element.*;
import javax.tools.*;
@SupportedAnnotationTypes("*")
@SupportedSourceVersion(SourceVersion.RELEASE_8)
public class EvidenceProcessor extends AbstractProcessor {
  public boolean process(Set<? extends TypeElement> annotations, RoundEnvironment round) {
    for (Element root : round.getRootElements()) {
      if (!root.getSimpleName().contentEquals("App")) continue;
      processingEnv.getMessager().printMessage(Diagnostic.Kind.WARNING, "warning ignored", root);
      processingEnv.getMessager().printMessage(Diagnostic.Kind.NOTE, "info ignored", root);
      for (Element member : root.getEnclosedElements()) {
        if (!member.getSimpleName().contentEquals("FLAG")) continue;
        Object value = ((VariableElement) member).getConstantValue();
        if (Integer.valueOf(3).equals(value)) {
          processingEnv.getMessager().printMessage(Diagnostic.Kind.ERROR, "requested error", member);
        } else {
          try (Writer writer = processingEnv.getFiler().createResource(
              StandardLocation.CLASS_OUTPUT, "", "META-INF/apt-value.txt", root).openWriter()) {
            writer.write(String.valueOf(value));
          } catch (Exception e) { throw new RuntimeException(e); }
        }
      }
    }
    return false;
  }
}
''', encoding="utf-8")
    processor_classes = tmp_path / "processor-classes"
    processor_classes.mkdir()
    subprocess.run([str(jdk / f"bin/javac{suffix}"), "-proc:none", "-d",
                    str(processor_classes), str(processor_source)], check=True,
                   capture_output=True, timeout=30)
    processor = tmp_path / "processor.jar"
    with zipfile.ZipFile(processor, "w") as archive:
        for path in processor_classes.rglob("*.class"):
            archive.write(path, path.relative_to(processor_classes).as_posix())
        archive.writestr("META-INF/services/javax.annotation.processing.Processor", "EvidenceProcessor\n")

    sources = tmp_path / "source"
    sources.mkdir()
    source = sources / "App.java"

    def edit(value: int) -> None:
        source.write_text(f'''import java.util.List;
@Deprecated public class App {{
    private int unused;
    public static final int FLAG = {value};
    public static void main(String[] args) {{ System.out.print(FLAG); }}
}}''', encoding="utf-8")

    def compiler() -> PersistentJdtCompileSession:
        return PersistentJdtCompileSession(
            root=tmp_path / "worker", candidate=JdtCandidate.load_product(),
            worker_java_home=jdk, source_roots=(sources,),
            classpath_entries=discover_java8_system_entries(jdk),
            processor_entries=(processor,), source_encoding="UTF-8",
            preserve_root_on_close=True,
        )

    edit(1)
    first = compiler()
    try:
        full = first.start()
        assert full.compile_ok, full.diagnostics
        assert full.warning_count == 0
        assert full.diagnostics == ()
        assert first.output_directory.joinpath("META-INF/apt-value.txt").read_text() == "1"
        first.accept_baseline()
    finally:
        first.close()
    persisted = [first.root / path for path in (
        "worker-launch.json", "worker-classpath.private.txt", "apt-processors.private.txt",
        "configuration/config.ini", "workspace/plain-fixture/.classpath",
        "workspace/plain-fixture/.factorypath",
        "workspace/plain-fixture/.settings/org.eclipse.jdt.core.prefs",
    )]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in persisted}
    second = compiler()
    try:
        reopened = second.start(reuse_workspace=True, build_on_reuse=False)
        assert reopened.compile_ok
        assert second._worker_ready_frame["configuration_reused"] is True
        assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in persisted} == before
        for value in (2, 3, 4):
            edit(value)
            result = second.compile((source,))
            assert result.compile_ok is (value != 3), result.diagnostics
            assert result.warning_count == 0
            assert all(d["severity_name"] == "ERROR" for d in result.diagnostics)
            if value == 3:
                assert any("requested error" in d["message"] for d in result.diagnostics)
            else:
                assert second.output_directory.joinpath("META-INF/apt-value.txt").read_text() == str(value)
                actual = subprocess.run([str(jdk / f"bin/java{suffix}"), "-cp",
                    str(second.output_directory), "App"], capture_output=True,
                    text=True, check=True, timeout=15)
                assert actual.stdout == str(value)
    finally:
        second.close()
