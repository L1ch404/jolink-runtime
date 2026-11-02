"""Scope the Maven-exported effective compiler configuration for JDT."""

import xml.etree.ElementTree as ET
from copy import deepcopy

from .maven import MavenBuildSystemAdapter


def compiler_scope(
    project: ET.Element, *, test: bool = False, configuration: str | None = None,
) -> ET.Element:
    """Use Maven's selected execution configuration, then test-specific parameters.

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
    # Selection belongs to Maven's native execution plan. Never merge disabled
    # or additional executions by XML order here. Unscoped callers use only the
    # plugin-level configuration; product callers pass the Probe's selected XML.
    selected = ET.fromstring(configuration) if configuration is not None else plugin.find("{*}configuration")
    values = {}
    if selected is not None:
        for item in selected:
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


def compiler_source_patterns(project: ET.Element, *, test: bool = False) -> dict:
    """Read the scoped effective model; testCompile uses testIncludes/testExcludes.

    Main includes/excludes may be inherited into the test execution's XML, but
    Maven's TestCompilerMojo does not use those fields to select test sources.
    """
    plugin = MavenBuildSystemAdapter._find_build_plugin(project, "maven-compiler-plugin")
    if plugin is None:
        return {}
    configuration = plugin.find("{*}configuration")
    if configuration is None:
        return {}
    patterns = {}
    for key in ("includes", "excludes"):
        name = "test" + key.capitalize() if test else key
        node = configuration.find("{*}" + name)
        if node is None:
            continue
        values = [(child.text or "").strip() for child in node]
        if not values and node.text:
            values = [value.strip() for value in node.text.split(",")]
        values = list(dict.fromkeys(value for value in values if value))
        if values:
            patterns[key] = values
    return patterns
