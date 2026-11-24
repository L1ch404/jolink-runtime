"""Shared file/directory inputs for the existing Build World caches."""

import hashlib
from functools import lru_cache
import os
import re
from stat import S_ISREG
import xml.etree.ElementTree as ET
from pathlib import Path

from .maven_reactor import maven_build_root
from .source_selection import selected_files


def maven_environment_references(paths):
    """Capture Maven env expressions from small configuration inputs, not all OS env."""
    names = set()
    for path in paths:
        path = Path(path)
        if path.is_file():
            names.update(re.findall(r"\$\{env\.([^}]+)\}", path.read_text(encoding="utf-8", errors="replace")))
    return tuple(sorted(names))


def maven_configuration_files(project, additional=()):
    """Small Maven inputs, including parent chains of Probe-discovered modules.

    Do not scan sources, artifacts or the repository. Maven still resolves
    profiles/properties; additional inputs supply its selected module POMs.
    """
    project = Path(project).resolve(strict=False)
    build_root = maven_build_root(project)
    pending = [project / "pom.xml", build_root / "pom.xml", *(Path(p) for p in additional)]
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
        found[build_root / ".mvn" / name] = None
    return tuple(found)


def build_configuration_stamps(project, build_system, inputs):
    files = (
        maven_configuration_files(project, inputs)
        if build_system == "maven"
        else inputs
    )
    return configuration_file_stamps(files, gradle=build_system == "gradle")


def gradle_directory_files(directory):
    """Read gradle/ configuration, not a nested Gradle project's own runtime outputs."""
    for root, dirs, files in os.walk(directory):
        if not {"settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts"}.isdisjoint(files):
            dirs[:] = (d for d in dirs if d not in {".gradle", "build"})
        for name in files:
            file = Path(root) / name
            if file.is_file():
                yield file


def expand_configuration_inputs(paths, *, gradle=False):
    for value in paths:
        path = Path(value)
        yield path
        if path.is_dir():
            if gradle and path.name == "gradle":
                yield from sorted(gradle_directory_files(path))
            else:
                yield from sorted(item for item in path.rglob("*") if item.is_file())


def configuration_file_stamps(paths, *, gradle=False):
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        if path.is_file()
        else "directory"
        if path.is_dir()
        else "missing"
        for path in expand_configuration_inputs(paths, gradle=gradle)
    }


@lru_cache(maxsize=256)
def _named_output_digest(path, observed_stat):
    # The key is the observed file identity, not just the pathname. Ordinary
    # resources and directories never enter this cache.
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def preparation_stamps(paths, roots=(), resource_sets=(), named_outputs=(), baseline=None):
    """Stat generator inputs and selected resources without reading file contents."""
    result = {}
    named = set(map(str, named_outputs))
    for path in expand_configuration_inputs(dict.fromkeys(paths)):
        try:
            stat = path.stat()
            name = str(path)
            if name in named and S_ISREG(stat.st_mode):
                observed = [stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_dev, stat.st_ino]
                old = (baseline or {}).get(name)
                if isinstance(old, dict) and old.get("stat") == observed:
                    result[name] = old
                else:
                    digest = _named_output_digest(path, tuple(observed))
                    result[name] = old if isinstance(old, dict) and old.get("sha256") == digest else {
                        "stat": observed, "sha256": digest,
                    }
            else:
                result[name] = [stat.st_mtime_ns, stat.st_size] if S_ISREG(stat.st_mode) else "directory"
        except FileNotFoundError:
            result[str(path)] = "missing"
    result.update({"root:" + str(path): Path(path).is_dir() for path in roots})
    for resource in resource_sets:
        root = Path(resource["directory"])
        for path in selected_files(root, resource):
            stat = path.stat()
            result[str(path)] = [stat.st_mtime_ns, stat.st_size]
    return result


def plan_preparation_stamps(plan):
    return preparation_stamps(
        plan.preparation_inputs, plan.preparation_roots, plan.preparation_resource_sets,
        getattr(plan, "preparation_named_outputs", ()), getattr(plan, "preparation_baseline", None),
    )
