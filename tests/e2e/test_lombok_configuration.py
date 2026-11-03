"""Original-source Lombok configuration remains effective in private JDT projects."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import anyio
import pytest

from java_support import open_mcp_session, require_real_mcp_java_e2e


pytestmark = pytest.mark.mcp_java_e2e
LOMBOK_VERSION = "1.18.48"


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _environment(tmp_path):
    require_real_mcp_java_e2e()
    java = os.environ.get("JOLINK_TEST_JAVA17_HOME") or os.environ.get("JAVA_HOME")
    if not java or not shutil.which("mvn"):
        pytest.skip("JDK17 and Maven are required")
    lombok = Path.home() / f".m2/repository/org/projectlombok/lombok/{LOMBOK_VERSION}/lombok-{LOMBOK_VERSION}.jar"
    if not lombok.is_file():
        pytest.skip(f"cached Lombok {LOMBOK_VERSION} is required")
    env = {**os.environ, "JAVA_HOME": java,
           "PATH": str(Path(java) / "bin") + os.pathsep + os.environ.get("PATH", ""),
           "XDG_CACHE_HOME": str(tmp_path / "cache"), "JOLINK_LOG_LEVEL": "INFO",
           "MAVEN_ARGS": "--offline", "GRADLE_ARGS": "--offline"}
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")
    return env, Path(java), lombok


def _project(tmp_path, system, lombok, *, additional_root=False):
    project = tmp_path / "project"
    project.mkdir()
    if system == "maven":
        registration = """<plugin><groupId>org.codehaus.mojo</groupId><artifactId>build-helper-maven-plugin</artifactId><version>3.5.0</version>
<executions><execution><id>extra-source</id><phase>generate-sources</phase><goals><goal>add-source</goal></goals>
<configuration><sources><source>${project.basedir}/other-java</source></sources></configuration>
</execution></executions></plugin>""" if additional_root else ""
        _write(project / "pom.xml", f"""<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>lombok-configuration</artifactId><version>1</version>
<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies>
<dependency><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId><version>{LOMBOK_VERSION}</version><scope>provided</scope></dependency>
<dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency>
</dependencies><build><plugins>
<plugin><artifactId>maven-compiler-plugin</artifactId><version>3.10.1</version></plugin>
<plugin><artifactId>maven-resources-plugin</artifactId><version>3.3.0</version></plugin>
<plugin><artifactId>maven-surefire-plugin</artifactId><version>3.0.0-M7</version></plugin>
{registration}</plugins></build></project>""")
    else:
        gradle = os.environ.get("JOLINK_FAST_TEST_GRADLE")
        if not gradle:
            pytest.skip("JOLINK_FAST_TEST_GRADLE is required")
        wrapper = _write(project / ("gradlew.bat" if os.name == "nt" else "gradlew"),
                         f'@call "{gradle}" %*\r\n' if os.name == "nt" else f'#!/bin/sh\nexec "{gradle}" "$@"\n')
        wrapper.chmod(0o755)
        version = os.environ.get("JOLINK_FAST_TEST_GRADLE_VERSION", "8.14")
        _write(project / "gradle/wrapper/gradle-wrapper.properties",
               f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{version}-bin.zip\n")
        junit = Path.home() / ".m2/repository/junit/junit/4.13.2/junit-4.13.2.jar"
        hamcrest = Path.home() / ".m2/repository/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar"
        assert junit.is_file() and hamcrest.is_file()
        _write(project / "settings.gradle", "rootProject.name='lombok-configuration'\n")
        _write(project / "build.gradle", f"""plugins {{ id 'java' }}
java {{ sourceCompatibility='8'; targetCompatibility='8' }}
dependencies {{
 compileOnly files('{lombok.as_posix()}'); annotationProcessor files('{lombok.as_posix()}')
 testCompileOnly files('{lombok.as_posix()}'); testAnnotationProcessor files('{lombok.as_posix()}')
 testImplementation files('{junit.as_posix()}', '{hamcrest.as_posix()}')
}}
""")
    return project


async def _run(session, project, java, transcript, *, selector="example.ConfigTest"):
    async def call(args):
        value = dict((await session.call_tool("java_fast_test", args)).structuredContent)
        with transcript.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"arguments": args, "result": value}, ensure_ascii=False) + "\n")
        return value

    options = dict(project_path=str(project), tests=[selector], java_home=str(java),
                   build_java_home=str(java), timeout=30)
    if (project / "pom.xml").is_file() and os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
        options["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]
    value = await call(options)
    with anyio.fail_after(180):
        while value.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
            await anyio.sleep(.25)
            value = await call(dict(action="result", test_run_id=value["test_run_id"]))
    if not value.get("passed"):
        value = await call(dict(action="result", test_run_id=value["test_run_id"]))
    assert value.get("passed") is True and value["tests"] == value["passed_count"] == 1, value
    return value


def _class_tree(cache):
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in cache.rglob("*.class")}


BASE = """package example;
public class Base {
 protected final String tenant;
 public Base(String tenant) { this.tenant=tenant; }
 @Override public boolean equals(Object value) { return value instanceof Base && tenant.equals(((Base)value).tenant); }
 @Override public int hashCode() { return tenant.hashCode(); }
 @Override public String toString() { return "Base(tenant="+tenant+")"; }
}
"""
CHILD = """package example;
@lombok.Data public class Child extends Base {
 private String name;
 public Child(String tenant) { super(tenant); }
}
"""
STATE = """package example;
public class State {
 public static String value() throws Exception {
  Child first=new Child("tenantA"), second=new Child("tenantB");
  first.setName("same"); second.setName("same");
  boolean chain=Child.class.getMethod("setName",String.class).getReturnType()==Child.class;
  return chain+"|"+!first.equals(second)+"|"+(first.hashCode()!=second.hashCode())+"|"+first.toString().contains("super=Base(");
 }
 public static void main(String[] args) throws Exception { System.out.print(value()); }
}
"""


def _config(enabled):
    return (f"lombok.accessors.chain = {str(enabled).lower()}\n"
            f"lombok.equalsAndHashCode.callSuper = {'call' if enabled else 'skip'}\n"
            f"lombok.toString.callSuper = {'call' if enabled else 'skip'}\n")


def _native(tmp_path, java, lombok, roots, main_class, label):
    output = tmp_path / ("native-" + label)
    output.mkdir()
    suffix = ".exe" if os.name == "nt" else ""
    compiled = subprocess.run([str(java / f"bin/javac{suffix}"), "-g", "-encoding", "UTF-8",
        "-cp", str(lombok), "-processorpath", str(lombok), "-d", str(output),
        *(str(path) for root in roots for path in sorted(root.rglob("*.java")))],
        capture_output=True, text=True, timeout=45)
    (tmp_path / ("native-" + label + ".log")).write_text(compiled.stdout + compiled.stderr, encoding="utf-8")
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    return subprocess.run([str(java / f"bin/java{suffix}"), "-cp", str(output), main_class],
                          capture_output=True, text=True, timeout=20, check=True).stdout


@pytest.mark.parametrize("system", ["maven", "gradle"])
def test_config_only_edits_change_generated_semantics_and_survive_reopen(tmp_path, system):
    env, java, lombok = _environment(tmp_path)
    project = _project(tmp_path, system, lombok)
    _write(tmp_path / "lombok.config", "config.stopBubbling=true\n" + _config(False))
    config_text = "import settings/shared.config\nimport settings/optional.config\nconfig.stopBubbling=true\n"
    config = _write(project / "lombok.config", config_text)
    shared = _write(project / "settings/shared.config", _config(True))
    optional = project / "settings/optional.config"
    main = project / "src/main/java"
    _write(main / "example/Base.java", BASE)
    _write(main / "example/Child.java", CHILD)
    _write(main / "example/State.java", STATE)
    tests = project / "src/test/java"
    _write(tests / "lombok.config", "lombok.accessors.chain=false\n")
    _write(tests / "example/TestBean.java", "package example; @lombok.Data public class TestBean {private String value;}")
    expected = _write(project / "expected.txt", "true|true|true|true")
    _write(tests / "example/ConfigTest.java", '''package example;
public class ConfigTest { @org.junit.Test public void actual() throws Exception {
 org.junit.Assert.assertEquals(new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(EXPECTED)),"UTF-8"),State.value());
 org.junit.Assert.assertEquals(void.class,TestBean.class.getMethod("setValue",String.class).getReturnType());
}}'''.replace("EXPECTED", json.dumps(expected.as_posix())))
    java_before = {str(p): p.read_bytes() for root in (main, tests) for p in root.rglob("*.java")}
    assert _native(tmp_path, java, lombok, [main], "example.State", "initial") == expected.read_text()

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                assert (await _run(session, project, java, tmp_path / "calls.jsonl"))["compiled_source_count"] > 0
                before = _class_tree(tmp_path / "cache")
                warm = await _run(session, project, java, tmp_path / "calls.jsonl")
                assert warm["compiled_source_count"] == 0 and "bootstrap_ms" not in warm, warm
                assert _class_tree(tmp_path / "cache") == before
                for phase, action, enabled in (
                    ("missing-import-created", lambda: optional.write_text(_config(False)), False),
                    ("import-deleted", lambda: optional.unlink(), True),
                    ("import-edit", lambda: shared.write_text(_config(False)), False),
                    ("import-restore", lambda: shared.write_text(_config(True)), True),
                    ("delete", lambda: config.unlink(), False),
                    ("add", lambda: config.write_text(config_text), True),
                ):
                    action()
                    expected.write_text("|".join([str(enabled).lower()] * 4))
                    assert _native(tmp_path, java, lombok, [main], "example.State", phase) == expected.read_text()
                    changed = await _run(session, project, java, tmp_path / "calls.jsonl")
                    assert changed["compiled_source_count"] > 0, (phase, changed)
                    assert (await _run(session, project, java, tmp_path / "calls.jsonl"))["compiled_source_count"] == 0
                assert {str(p): p.read_bytes() for root in (main, tests) for p in root.rglob("*.java")} == java_before
        with (tmp_path / "reopen-stderr.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                reopened = await _run(session, project, java, tmp_path / "calls.jsonl")
                assert reopened["compiled_source_count"] == 0, reopened
                # The imported configuration can change while the Worker is offline too.
        shared.write_text(_config(False))
        expected.write_text("false|false|false|false")
        with (tmp_path / "changed-reopen-stderr.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                reopened = await _run(session, project, java, tmp_path / "calls.jsonl")
                assert reopened["compiled_source_count"] > 0, reopened
    anyio.run(scenario)


def test_original_directories_not_package_mirror_define_config_scope(tmp_path):
    env, java, lombok = _environment(tmp_path)
    project = _project(tmp_path, "maven", lombok, additional_root=True)
    _write(project / "lombok.config", "config.stopBubbling=true\nlombok.accessors.chain=true\n")
    main, other = project / "src/main/java", project / "other-java"
    # Both roots compile into package example, but their original directory rules differ.
    _write(main / "example/A.java", "package example; @lombok.Data public class A {private String value;}")
    _write(other / "lombok.config", "lombok.accessors.chain=false\n")
    _write(other / "example/B.java", "package example; @lombok.Data public class B {private String value;}")
    _write(main / "physical/nested/lombok.config", "lombok.accessors.chain=false\n")
    _write(main / "physical/nested/C.java", "package example; @lombok.Data public class C {private String value;}")
    _write(main / "example/Scope.java", '''package example;
public class Scope { public static String value() throws Exception {
 return A.class.getMethod("setValue",String.class).getReturnType().getName()+"|"+
 B.class.getMethod("setValue",String.class).getReturnType().getName()+"|"+
 C.class.getMethod("setValue",String.class).getReturnType().getName();
} public static void main(String[] args) throws Exception {System.out.print(value());}}''')
    expected = _native(tmp_path, java, lombok, [main, other], "example.Scope", "roots")
    assert expected == "example.A|void|void", expected
    _write(project / "src/test/java/example/ConfigTest.java", '''package example;
public class ConfigTest {@org.junit.Test public void scope() throws Exception {
org.junit.Assert.assertEquals(EXPECTED, Scope.value());}}'''.replace("EXPECTED", json.dumps(expected)))

    async def scenario():
        for cycle in range(2):
            with (tmp_path / f"mcp-{cycle}.log").open("w", encoding="utf-8") as log:
                async with open_mcp_session(log, environment=env) as session:
                    result = await _run(session, project, java, tmp_path / "calls.jsonl")
                    assert result["compiled_source_count"] == 0 if cycle else result["compiled_source_count"] > 0
    anyio.run(scenario)


def test_reactor_module_configs_and_upstream_config_only_change(tmp_path):
    env, java, lombok = _environment(tmp_path)
    project = _project(tmp_path, "maven", lombok)
    pom = project / "pom.xml"
    pom.write_text(pom.read_text().replace("<version>1</version>",
        "<version>1</version><packaging>pom</packaging><modules><module>lib</module><module>app</module></modules>", 1))
    _write(project / "lombok.config", "config.stopBubbling=true\nlombok.accessors.chain=true\n")
    parent = "<parent><groupId>example</groupId><artifactId>lombok-configuration</artifactId><version>1</version></parent>"
    for name in ("lib", "app"):
        dependency = ("<dependencies><dependency><groupId>example</groupId><artifactId>lib</artifactId><version>1</version></dependency></dependencies>"
                      if name == "app" else "")
        _write(project / name / "pom.xml", f"<project><modelVersion>4.0.0</modelVersion>{parent}<artifactId>{name}</artifactId>{dependency}</project>")
    left = project / "lib/src/main/java"
    right = project / "app/src/main/java"
    config = _write(project / "lib/lombok.config", "lombok.accessors.chain=false\n")
    _write(left / "example/Left.java", "package example; @lombok.Data public class Left {private String value;}")
    _write(right / "example/Right.java", "package example; @lombok.Data public class Right {private String value;}")
    _write(right / "example/Modules.java", '''package example;
public class Modules { public static String value() throws Exception {
 return Left.class.getMethod("setValue",String.class).getReturnType().getName()+"|"+
 Right.class.getMethod("setValue",String.class).getReturnType().getName();
} public static void main(String[] args) throws Exception {System.out.print(value());}}''')
    expected = _write(project / "expected.txt", "void|example.Right")
    _write(project / "app/src/test/java/example/ConfigTest.java", '''package example;
public class ConfigTest {@org.junit.Test public void modules() throws Exception {
 org.junit.Assert.assertEquals(new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(EXPECTED)),"UTF-8"),Modules.value());
}}'''.replace("EXPECTED", json.dumps(expected.as_posix())))
    assert _native(tmp_path, java, lombok, [left, right], "example.Modules", "modules") == expected.read_text()
    originals = {str(p): p.read_bytes() for p in project.rglob("*.java")}

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                await _run(session, project / "app", java, tmp_path / "calls.jsonl")
                assert (await _run(session, project / "app", java, tmp_path / "calls.jsonl"))["compiled_source_count"] == 0
                config.write_text("lombok.accessors.chain=true\n")
                expected.write_text("example.Left|example.Right")
                assert _native(tmp_path, java, lombok, [left, right], "example.Modules", "modules-changed") == expected.read_text()
                changed = await _run(session, project / "app", java, tmp_path / "calls.jsonl")
                assert changed["compiled_source_count"] > 0, changed
        with (tmp_path / "reopen-stderr.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                assert (await _run(session, project / "app", java, tmp_path / "calls.jsonl"))["compiled_source_count"] == 0
        assert {str(p): p.read_bytes() for p in project.rglob("*.java")} == originals
    anyio.run(scenario)


@pytest.mark.parametrize("cycle", [False, True], ids=["duplicate-import-clear", "cyclic-import"])
def test_import_graph_stop_bubbling_uses_native_lombok_semantics(tmp_path, cycle):
    env, java, lombok = _environment(tmp_path)
    project = _project(tmp_path, "maven", lombok)
    _write(project / "lombok.config", "config.stopBubbling=true\nlombok.accessors.chain=true\nlombok.log.fieldName=parentLogger\n")
    source = project / "src/main/java/example"
    _write(source / "shared.config", "config.stopBubbling=true\n")
    _write(source / "first.config", "import shared.config\nclear config.stopBubbling\n")
    _write(source / "second.config", "import shared.config\n")
    _write(source / "lombok.config", "import first.config\nimport second.config\n")
    if cycle:
        _write(source / "shared.config", "import first.config\nconfig.stopBubbling=false\n")
    _write(source / "Bean.java", "package example; @lombok.Data @lombok.extern.java.Log public class Bean {private String value;}")
    _write(source / "Graph.java", '''package example;
public class Graph { public static String value() throws Exception {
 String logger=""; for(java.lang.reflect.Field f:Bean.class.getDeclaredFields())
  if(f.getType()==java.util.logging.Logger.class)logger=f.getName();
 return Bean.class.getMethod("setValue",String.class).getReturnType().getName()+"|"+logger;
} public static void main(String[] args) throws Exception {System.out.print(value());}}''')
    expected = _native(tmp_path, java, lombok, [source], "example.Graph", "graph")
    _write(project / "src/test/java/example/ConfigTest.java", '''package example;
public class ConfigTest {@org.junit.Test public void graph() throws Exception {
org.junit.Assert.assertEquals(EXPECTED, Graph.value());}}'''.replace("EXPECTED", json.dumps(expected)))
    (tmp_path / "native-oracle.txt").write_text(expected, encoding="utf-8")

    async def scenario():
        with (tmp_path / "mcp.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                await _run(session, project, java, tmp_path / "calls.jsonl")
                assert (await _run(session, project, java, tmp_path / "calls.jsonl"))["compiled_source_count"] == 0
    anyio.run(scenario)
