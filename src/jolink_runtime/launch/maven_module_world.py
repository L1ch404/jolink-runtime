"""Maven-exported module facts shared by Runtime and Fast Test."""
from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET

from .jdt_compile_session import select_target_system_home
from .maven import MavenBuildSystemAdapter


def load_module_worlds(output: Path, target: Path, build_jdk, *, tests: bool) -> tuple[dict, ...]:
    maven = MavenBuildSystemAdapter()
    effective = {}
    for path in output.glob("*.pom.xml"):
        root = ET.parse(path).getroot()
        coordinate = tuple(root.findtext("{*}"+key) for key in ("groupId", "artifactId", "version"))
        effective[coordinate] = root
    snapshots = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(output.glob("*.json"))]
    local_outputs = {str(Path(s["outputDirectory"])) for s in snapshots}
    local_outputs.update(str(Path(s["testOutputDirectory"])) for s in snapshots)
    modules = []
    for snapshot in snapshots:
        project = snapshot["project"]
        directory = Path(project["baseDirectory"])
        selected = directory == target
        include_tests = tests and (selected or snapshot.get("testSourcesRequired", False))
        root = effective[tuple(project[key] for key in ("groupId", "artifactId", "version"))]
        compiler = maven._compiler_model(root, build_jdk=build_jdk, runtime_jdk=build_jdk)
        plugin = maven._find_build_plugin(root, "maven-compiler-plugin")
        profile = maven._compiler_argument_profile(maven._compiler_configurations(plugin))
        parameters = plugin.findtext("./{*}configuration/{*}parameters") if plugin is not None else None
        if parameters is None:
            parameters = root.findtext("./{*}properties/{*}maven.compiler.parameters")
        target_home = select_target_system_home((build_jdk.home,), compiler["target_level"])
        classpath = []
        for raw in snapshot["compileClasspathElements"]:
            path = str(Path(raw))
            if path == str(Path(snapshot["outputDirectory"])): continue
            if path in local_outputs or maven._jdt_dependency_facts(Path(path))[0]: classpath.append(path)
        processing = snapshot["annotationProcessing"]
        processor_paths = [Path(path) for path in processing.get("processorProviderArtifactPaths", ())]
        lombok = [str(path) for path in processor_paths if maven._jdt_dependency_facts(path)[2]]
        modules.append({
            "module_root": str(directory),
            "source_roots": [path for path in snapshot["compileSourceRoots"] if Path(path).is_dir()],
            "test_source_roots": [path for path in snapshot["testCompileSourceRoots"] if Path(path).is_dir()] if include_tests else [],
            "output_directory": str(Path(snapshot["outputDirectory"])),
            "test_output_directory": str(Path(snapshot["testOutputDirectory"])),
            "classpath": classpath,
            "test_classpath": [p for p in snapshot["testClasspathElements"] if p not in classpath and p not in {snapshot["outputDirectory"],snapshot["testOutputDirectory"]} and (p in local_outputs or Path(p).exists())] if include_tests else [],
            "runtime_classpath": snapshot.get("runtimeClasspathElements", snapshot["compileClasspathElements"]),
            "resource_roots": snapshot["resourceDirectories"],
            "source_level": compiler["source_level"],
            "source_encoding": maven._source_encoding(root),
            "target_java_home": str(target_home),
            "method_parameters": profile.method_parameters or parameters == "true",
            "processor_entries": [str(path) for path in processor_paths if str(path) not in lombok],
            "lombok_entries": lombok,
        })
    return tuple(modules)


def combine_effective_poms(output: Path, destination: Path) -> None:
    projects = ET.Element("projects")
    for path in sorted(output.glob("*.pom.xml")):
        projects.append(ET.parse(path).getroot())
    ET.ElementTree(projects).write(destination, encoding="utf-8", xml_declaration=True)
