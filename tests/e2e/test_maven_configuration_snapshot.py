"""Native Maven input/model association; this is not an MCP transport test.

The test-only Mojo embeds the exact product Probe classes and adds a small
observer. Maven builds the real projects and injects its own ModelProcessor;
the file gate controls edits before/after capture without timing guesses.
"""

import io
import json
import os
import shutil
import subprocess
import time
import zipfile
from hashlib import sha256
from pathlib import Path

import pytest

from java_support import require_real_mcp_java_e2e
from jolink_runtime.launch.maven_probe import ProductMavenProbe


pytestmark = pytest.mark.mcp_java_e2e

DESCRIPTOR = """<plugin><name>Configuration snapshot harness</name>
<groupId>io.jolink.tests</groupId><artifactId>configuration-snapshot</artifactId><version>1</version>
<goalPrefix>configuration-snapshot</goalPrefix><isolatedRealm>false</isolatedRealm><mojos><mojo>
<goal>observe</goal><requiresProject>true</requiresProject><aggregator>true</aggregator>
<implementation>io.jolink.maven.MavenConfigurationSnapshotHarness</implementation><language>java</language>
<instantiationStrategy>per-lookup</instantiationStrategy><executionStrategy>once-per-session</executionStrategy>
<parameters>
<parameter><name>session</name><type>org.apache.maven.execution.MavenSession</type><readonly>true</readonly><required>true</required></parameter>
<parameter><name>destination</name><type>java.lang.String</type><required>true</required><editable>true</editable></parameter>
<parameter><name>gate</name><type>java.lang.String</type><editable>true</editable></parameter>
</parameters><configuration>
<session implementation="org.apache.maven.execution.MavenSession" default-value="${session}"/>
<destination implementation="java.lang.String">${snapshot.output}</destination>
<gate implementation="java.lang.String">${snapshot.gate}</gate>
</configuration><requirements><requirement>
<role>org.apache.maven.model.building.ModelProcessor</role><field-name>modelProcessor</field-name>
</requirement></requirements></mojo></mojos><dependencies/></plugin>"""


@pytest.fixture(scope="module", params=[("3.3.3", 8), ("3.3.9", 8), ("3.9.11", 17)],
                ids=["maven333-jdk8", "maven339-jdk8", "maven3911-jdk17"])
def native_probe(request, tmp_path_factory):
    require_real_mcp_java_e2e()
    version, major = request.param
    java_home = os.environ.get(f"JOLINK_TEST_JAVA{major}_HOME")
    maven_home = os.environ.get(f"JOLINK_TEST_MAVEN{version.replace('.', '')}_HOME")
    if not maven_home and version == "3.9.11" and shutil.which("mvn"):
        maven_home = str(Path(shutil.which("mvn")).resolve().parent.parent)
    if not java_home or not maven_home:
        pytest.skip(f"JDK{major} and Maven{version} homes are required")
    java_home, maven_home = Path(java_home), Path(maven_home)
    assert (maven_home / "lib" / f"maven-core-{version}.jar").is_file(), maven_home
    # Maven 3 injects this dependency for a plain plugin descriptor. Reuse it
    # offline, rather than manufacturing a different JAR at that coordinate.
    plexus = Path.home() / ".m2/repository/org/codehaus/plexus/plexus-utils/1.1"
    if not (plexus / "plexus-utils-1.1.jar").is_file():
        pytest.skip("cached plexus-utils:1.1 is required for the offline harness")
    root = tmp_path_factory.mktemp("native-maven-snapshot-" + version)
    repository = root / "repository"
    shutil.copytree(plexus, repository / "org/codehaus/plexus/plexus-utils/1.1")
    product = ProductMavenProbe.load().jar
    probe = root / "probe.jar"
    probe.write_bytes(product)
    classes = root / "classes"
    classes.mkdir()
    source = Path(__file__).parents[1] / "java/MavenConfigurationSnapshotHarness.java"
    classpath = os.pathsep.join(map(str, (probe, *sorted((maven_home / "lib").glob("*.jar")))))
    suffix = ".exe" if os.name == "nt" else ""
    environment = {**os.environ, "JAVA_HOME": str(java_home),
                   "PATH": str(java_home / "bin") + os.pathsep + os.environ.get("PATH", "")}
    compiled = subprocess.run([str(java_home / f"bin/javac{suffix}"), "-encoding", "UTF-8", "-source", "8",
        "-target", "8", "-proc:none", "-cp", classpath, "-d", str(classes), str(source)],
        env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    (root / "javac.log").write_text(compiled.stdout + compiled.stderr, encoding="utf-8")
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    coordinate = repository / "io/jolink/tests/configuration-snapshot/1"
    coordinate.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(product)) as original, zipfile.ZipFile(coordinate / "configuration-snapshot-1.jar", "w") as jar:
        for name in original.namelist():
            if name != "META-INF/maven/plugin.xml":
                jar.writestr(name, original.read(name))
        for path in classes.rglob("*.class"):
            jar.write(path, path.relative_to(classes).as_posix())
        jar.writestr("META-INF/maven/plugin.xml", DESCRIPTOR)
    (coordinate / "configuration-snapshot-1.pom").write_text(
        "<project><modelVersion>4.0.0</modelVersion><groupId>io.jolink.tests</groupId>"
        "<artifactId>configuration-snapshot</artifactId><version>1</version><packaging>maven-plugin</packaging></project>",
        encoding="utf-8")
    maven = maven_home / "bin" / ("mvn.cmd" if os.name == "nt" else "mvn")
    return maven, environment, repository


@pytest.mark.parametrize("mode", ["unchanged", "changed-before-capture", "changed-after-capture",
                                  "deleted-before-capture", "malformed-before-capture", "external-parent",
                                  "utf8-bom", "utf16-chinese"])
def test_native_maven_configuration_matches_retained_models(tmp_path, native_probe, mode):
    maven, environment, repository = native_probe
    project = tmp_path / "project"
    child = project / "child"
    child.mkdir(parents=True)
    root_pom = project / "pom.xml"
    root_pom.write_text("""<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>root</artifactId><version>1</version><packaging>pom</packaging>
<properties><child>child</child><label>original</label></properties>
<profiles><profile><id>active</id><activation><activeByDefault>true</activeByDefault></activation>
<modules><module>${child}</module></modules></profile></profiles>
<build><plugins><plugin><groupId>example</groupId><artifactId>duplicate</artifactId><version>1</version></plugin>
<plugin><groupId>example</groupId><artifactId>duplicate</artifactId><version>2</version></plugin></plugins></build>
</project>""", encoding="utf-8")
    target = child / "pom.xml"
    content = """<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<parent><groupId>example</groupId><artifactId>root</artifactId><version>1</version></parent><artifactId>child</artifactId>
<description>中文配置</description><properties><local>old</local></properties>
<dependencies><dependency><groupId>example</groupId><artifactId>not-resolved</artifactId><version>${label}</version></dependency></dependencies>
<profiles><profile><id>values</id><activation><activeByDefault>true</activeByDefault></activation>
<properties><property>inside</property></properties></profile></profiles></project>"""
    encoding = "utf-16" if mode == "utf16-chinese" else "utf-8-sig" if mode == "utf8-bom" else "utf-8"
    declaration = "UTF-16" if mode == "utf16-chinese" else "UTF-8"
    target.write_bytes((f'<?xml version="1.0" encoding="{declaration}"?>\n' + content).encode(encoding))
    if mode == "external-parent":
        target = tmp_path / "external-parent/pom.xml"
        target.parent.mkdir()
        target.write_text("<project><modelVersion>4.0.0</modelVersion><groupId>external</groupId><artifactId>parent</artifactId>"
                          "<version>1</version><packaging>pom</packaging><properties><inherited>old</inherited></properties></project>", encoding="utf-8")
        root_pom.write_text(root_pom.read_text().replace("<groupId>example</groupId>",
            "<parent><groupId>external</groupId><artifactId>parent</artifactId><version>1</version>"
            "<relativePath>../external-parent/pom.xml</relativePath></parent><groupId>example</groupId>", 1), encoding="utf-8")
    before = target.read_bytes()
    output, gate = tmp_path / "snapshot.json", tmp_path / "gate"
    gated = mode not in {"unchanged", "utf8-bom", "utf16-chinese"}
    command = [str(maven), "--offline", "-B", "-f", str(root_pom),
               "-Dmaven.repo.local=" + str(repository), "-Dsnapshot.output=" + str(output)]
    if gated:
        command.append("-Dsnapshot.gate=" + str(gate))
    if mode == "changed-after-capture":
        command.append("-Dsnapshot.beforeGate=true")
    command.append("io.jolink.tests:configuration-snapshot:1:observe")
    with (tmp_path / "maven.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=project, env=environment, stdout=log, stderr=log)
        try:
            if gated:
                deadline = time.monotonic() + 30
                while not (gate / "entered").is_file():
                    assert process.poll() is None, (tmp_path / "maven.log").read_text(encoding="utf-8", errors="replace")
                    assert time.monotonic() < deadline, "Maven did not reach the post-model gate"
                    time.sleep(.02)
                if mode == "deleted-before-capture":
                    target.unlink()
                elif mode == "malformed-before-capture":
                    target.write_text("<not a model", encoding="utf-8")
                else:
                    target.write_bytes(before.replace(b"old", b"new").replace(b"inside", b"changed"))
                (gate / "release").write_text("go", encoding="utf-8")
            assert process.wait(30) == 0, (tmp_path / "maven.log").read_text(encoding="utf-8", errors="replace")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(5)
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    key = str(target.resolve())
    assert snapshot["native_session"] is True
    assert key in snapshot["configurationStamps"]
    # A model comparison alone can't distinguish normalization from an edit;
    # only the caller with a pre-Probe baseline may corroborate this signal.
    assert snapshot["configurationModelMismatches"] == (
        [key] if mode in {"changed-before-capture", "external-parent"} else []
    ), snapshot
    if mode in {"unchanged", "changed-after-capture", "utf8-bom", "utf16-chinese"}:
        assert snapshot["configurationChanges"] == [], snapshot
    else:
        assert snapshot["configurationChanges"] == [key], snapshot
    if mode == "changed-after-capture":
        assert snapshot["configurationStamps"][key] == sha256(before).hexdigest()
        assert snapshot["configurationStamps"][key] != sha256(target.read_bytes()).hexdigest()
    elif mode == "deleted-before-capture":
        assert snapshot["configurationStamps"][key] == "missing"
    else:
        assert snapshot["configurationStamps"][key] == sha256(target.read_bytes()).hexdigest()
