"""Java API oracle for the bundled Probe filter, not an MCP transport test.

Kept in the opt-in real-Java suite: execute the product resolver with a tiny
RepositorySystem proxy to capture its actual request, then compare with the
selected Maven distribution's original Aether implementation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from java_support import require_real_mcp_java_e2e
from jolink_runtime.launch.maven_probe import ProductMavenProbe


pytestmark = pytest.mark.mcp_java_e2e

ORACLE = """package io.jolink.maven;
import java.lang.reflect.Proxy;
import java.util.Collections;
import java.util.List;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;
import org.eclipse.aether.DefaultRepositorySystemSession;
import org.eclipse.aether.RepositorySystem;
import org.eclipse.aether.artifact.DefaultArtifact;
import org.eclipse.aether.graph.DefaultDependencyNode;
import org.eclipse.aether.graph.Dependency;
import org.eclipse.aether.graph.DependencyFilter;
import org.eclipse.aether.graph.DependencyNode;
import org.eclipse.aether.resolution.DependencyRequest;
import org.eclipse.aether.resolution.DependencyResult;
import org.eclipse.aether.util.filter.DependencyFilterUtils;

public class ProcessorFilterOracle {
 public static void main(String[] arguments) throws Exception {
  final DependencyRequest[] captured = new DependencyRequest[1];
  RepositorySystem resolver = (RepositorySystem) Proxy.newProxyInstance(
    RepositorySystem.class.getClassLoader(), new Class<?>[]{RepositorySystem.class},
    (proxy, method, args) -> {
     if (!method.getName().equals("resolveDependencies")) throw new AssertionError(method);
     captured[0] = (DependencyRequest) args[1];
     return new DependencyResult(captured[0]).setArtifactResults(Collections.emptyList());
    });
  List<String> result = ProcessorPathResolver.resolve(new MavenProject(), resolver,
      new DefaultRepositorySystemSession(), new Xpp3Dom("annotationProcessorPaths"), false);
  if (!result.isEmpty() || captured[0] == null || captured[0].getFilter() == null)
   throw new AssertionError("Product resolver did not submit a filtered dependency request");
  DependencyFilter actual = captured[0].getFilter();
  DependencyFilter expected = DependencyFilterUtils.classpathFilter("runtime");
  List<DependencyNode> noParents = Collections.emptyList();
  List<DependencyNode> providedParent = Collections.singletonList(new DefaultDependencyNode(
      new Dependency(new DefaultArtifact("example:parent:jar:1"), "provided")));
  compare("root", new DefaultDependencyNode((Dependency) null), noParents, actual, expected);
  for (String scope : new String[]{null,"","compile","runtime","provided","system","test","custom","TEST","RUNTIME"}) {
   for (boolean optional : new boolean[]{false,true}) {
    DependencyNode node = new DefaultDependencyNode(new Dependency(
       new DefaultArtifact("example:dependency:jar:1"), scope, optional));
    compare(String.valueOf(scope)+" optional="+optional, node, noParents, actual, expected);
    compare(String.valueOf(scope)+" optional="+optional+" provided-parent", node, providedParent, actual, expected);
   }
  }
  System.out.println("Bundled runtime filter matches native Aether");
 }
 private static void compare(String label, DependencyNode node, List<DependencyNode> parents,
     DependencyFilter actual, DependencyFilter expected) {
  boolean wanted = expected.accept(node, parents), observed = actual.accept(node, parents);
  if (wanted != observed) throw new AssertionError(label+": expected="+wanted+", actual="+observed);
  System.out.println(label+" => "+observed);
 }
}
"""


@pytest.mark.parametrize("maven_version,java_major", [("3.3.3", 8), ("3.9.11", 17)])
def test_bundled_processor_filter_matches_native_aether(tmp_path, maven_version, java_major):
    require_real_mcp_java_e2e()
    java_home = os.environ.get(f"JOLINK_TEST_JAVA{java_major}_HOME")
    maven_home = os.environ.get(f"JOLINK_TEST_MAVEN{maven_version.replace('.', '')}_HOME")
    if not maven_home and maven_version == "3.9.11" and shutil.which("mvn"):
        maven_home = str(Path(shutil.which("mvn")).resolve().parent.parent)
    if not java_home or not maven_home:
        pytest.skip(f"JDK{java_major} and Maven{maven_version} homes are required")
    java_home, maven_home = Path(java_home), Path(maven_home)
    assert (maven_home / "lib" / f"maven-core-{maven_version}.jar").is_file(), maven_home
    libraries = sorted((maven_home / "lib").glob("*.jar"))
    assert libraries, maven_home
    probe = tmp_path / "probe.jar"
    probe.write_bytes(ProductMavenProbe.load().jar)
    source = tmp_path / "ProcessorFilterOracle.java"
    source.write_text(ORACLE, encoding="utf-8")
    classes = tmp_path / "classes"
    classes.mkdir()
    classpath = os.pathsep.join(str(p) for p in (probe, *libraries))
    suffix = ".exe" if os.name == "nt" else ""
    env = {**os.environ, "JAVA_HOME": str(java_home),
           "PATH": str(java_home / "bin") + os.pathsep + os.environ.get("PATH", "")}
    commands = [
        ("compile", [str(java_home / f"bin/javac{suffix}"), "-source", "8", "-target", "8",
                     "-proc:none", "-encoding", "UTF-8", "-cp", classpath, "-d", str(classes), str(source)]),
        ("oracle", [str(java_home / f"bin/java{suffix}"), "-cp", str(classes) + os.pathsep + classpath,
                    "io.jolink.maven.ProcessorFilterOracle"]),
    ]
    for label, argv in commands:
        completed = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True,
                                   text=True, encoding="utf-8", timeout=30)
        (tmp_path / (label + ".log")).write_text(completed.stdout + completed.stderr, encoding="utf-8")
        assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Bundled runtime filter matches native Aether" in completed.stdout
