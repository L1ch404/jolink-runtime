"""Shared file/directory inputs for the existing Build World caches."""

import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path


def maven_configuration_files(project, additional=()):
    """Small Maven inputs, including parent chains of Probe-discovered modules.

    Do not scan sources, artifacts or the repository. Maven still resolves
    profiles/properties; additional inputs supply its selected module POMs.
    """
    project = Path(project).resolve(strict=False)
    pending = [project / "pom.xml", *(Path(p) for p in additional)]
    found = {}
    while pending:
        path = pending.pop().resolve(strict=False)
        if path in found:
            continue
        found[path] = None
        if path.suffix not in {".xml", ".pom"} or not path.is_file():
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue  # Keep its hash; Maven will report malformed configuration.
        if root.tag.rsplit("}", 1)[-1] != "project":
            continue
        for module in root.findall("./{*}modules/{*}module"):
            if module.text and "${" not in module.text:
                pending.append(path.parent / module.text.strip() / "pom.xml")
        parent = root.find("{*}parent")
        if parent is not None:
            relative = parent.find("{*}relativePath")
            value = "../pom.xml" if relative is None else (relative.text or "").strip()
            if value and "${" not in value:
                parent_path = path.parent / value
                pending.append(
                    parent_path / "pom.xml" if parent_path.is_dir() else parent_path
                )
    for name in ("maven.config", "jvm.config", "extensions.xml"):
        found[project / ".mvn" / name] = None
    return tuple(found)


def build_configuration_stamps(project, build_system, inputs):
    files = (
        maven_configuration_files(project, inputs)
        if build_system == "maven"
        else inputs
    )
    return configuration_file_stamps(files)


def expand_configuration_inputs(paths):
    for value in paths:
        path = Path(value)
        yield path
        if path.is_dir():
            yield from sorted(item for item in path.rglob("*") if item.is_file())


def configuration_file_stamps(paths):
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        if path.is_file()
        else "directory"
        if path.is_dir()
        else "missing"
        for path in expand_configuration_inputs(paths)
    }


def preparation_stamps(paths, roots=()):
    """Generator-declared files/directories: stat only, no repeated content hashes."""
    result = {}
    for path in expand_configuration_inputs(dict.fromkeys(paths)):
        try:
            stat = path.stat()
            result[str(path)] = (
                [stat.st_mtime_ns, stat.st_size] if path.is_file() else "directory"
            )
        except FileNotFoundError:
            result[str(path)] = "missing"
    result.update({"root:" + str(path): Path(path).is_dir() for path in roots})
    return result
