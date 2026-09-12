"""Scope the Maven-exported effective compiler configuration for JDT."""

import xml.etree.ElementTree as ET
from copy import deepcopy

from .maven import MavenBuildSystemAdapter


def compiler_scope(project: ET.Element, *, test: bool = False) -> ET.Element:
    """Apply execution overrides, then test-specific parameters when present.

    The input is Maven's effective POM, not the user's unexpanded POM. Keep
    the existing compiler argument/platform mapping shared by both scopes.
    """
    result = deepcopy(project)
    maven = MavenBuildSystemAdapter()
    plugin = maven._find_build_plugin(result, "maven-compiler-plugin")
    if plugin is None:
        build = result.find("{*}build")
        if build is None:
            build = ET.SubElement(result, "build")
        plugins = build.find("{*}plugins")
        if plugins is None:
            plugins = ET.SubElement(build, "plugins")
        plugin = ET.SubElement(plugins, "plugin")
        ET.SubElement(plugin, "artifactId").text = "maven-compiler-plugin"
    configurations = []
    direct = plugin.find("{*}configuration")
    if direct is not None:
        configurations.append(direct)
    goal = "testCompile" if test else "compile"
    configurations.extend(
        execution.find("{*}configuration")
        for execution in plugin.findall("./{*}executions/{*}execution")
        if goal in {item.text for item in execution.findall("./{*}goals/{*}goal")}
        or execution.findtext("{*}id") == "default-" + goal
    )
    values = {}
    for configuration in configurations:
        if configuration is not None:
            for item in configuration:
                values[maven._local_name(item.tag)] = deepcopy(item)
    if test:
        for field in ("release", "source", "target", "encoding", "parameters"):
            name = "test" + field[0].upper() + field[1:]
            value = values.get(name)
            prop = result.findtext("./{*}properties/{*}maven.compiler." + name)
            if value is not None:
                values[field] = ET.Element(field)
                values[field].text = value.text
            elif prop:
                values[field] = ET.Element(field)
                values[field].text = prop
    for child in list(plugin):
        if maven._local_name(child.tag) in {"configuration", "executions"}:
            plugin.remove(child)
    merged = ET.SubElement(plugin, "configuration")
    merged.extend(values.values())
    return result


def compiler_parameters(project: ET.Element) -> bool:
    maven = MavenBuildSystemAdapter()
    plugin = maven._find_build_plugin(project, "maven-compiler-plugin")
    configurations = maven._compiler_configurations(plugin)
    return (
        maven._compiler_value(
            project,
            configurations,
            config_name="parameters",
            property_name="maven.compiler.parameters",
        ).lower()
        == "true"
        or maven._compiler_argument_profile(configurations).method_parameters
    )
