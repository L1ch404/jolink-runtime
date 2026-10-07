"""Locate an enclosing reactor without confusing inheritance with aggregation."""

import xml.etree.ElementTree as ET
from pathlib import Path


def maven_build_root(project: Path) -> Path:
    """Follow ancestor module declarations; Maven still resolves the active model.

    Looking only at parent/relativePath is insufficient: an inherited parent need
    not aggregate the project, and an aggregator need not be its inherited parent.
    This is entry-point discovery, not dependency or profile resolution.
    """
    project = project.expanduser().resolve(strict=False)
    root = project
    members = {project, project / "pom.xml"}
    for directory in project.parents:
        try:
            pom = ET.parse(directory / "pom.xml").getroot()
        except (OSError, ET.ParseError):
            continue
        declarations = (
            *pom.findall("./{*}modules/{*}module"),
            *pom.findall("./{*}profiles/{*}profile/{*}modules/{*}module"),
        )
        if any((directory / (item.text or "").strip()).resolve(strict=False) in members
               for item in declarations if (item.text or "").strip()):
            root = directory
            members.update((directory, directory / "pom.xml"))
    return root
