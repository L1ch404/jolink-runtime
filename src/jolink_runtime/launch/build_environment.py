"""Shared per-call build choices for project launch and Fast Test."""

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

from .maven_probe import MavenProbeError


def apply_build_preferences(preferences, *, build_java_home=None, maven_home=None, maven_settings=None):
    changes = {}
    for value, field in ((maven_home, "custom_maven_home"), (maven_settings, "user_settings_file")):
        if value:
            changes[field] = Path(value).expanduser().resolve(strict=True)
    if build_java_home:
        changes["maven_runner_jdk_name"] = str(Path(build_java_home).expanduser().resolve(strict=True))
    return replace(preferences, **changes)


def maven_local_repository(preferences, maven, settings):
    """Use an explicit IDE repository, then user/global settings, then Maven's default."""
    if preferences.local_repository is not None:
        return preferences.local_repository.expanduser().resolve(strict=False)
    candidates = [settings]
    if maven.home is not None:
        candidates.append(maven.home / "conf/settings.xml")
    for file in candidates:
        if file is None or not file.is_file():
            continue
        try:
            root = ET.parse(file).getroot()
        except ET.ParseError as error:
            raise MavenProbeError("MAVEN_SETTINGS_UNSUPPORTED", "The selected Maven settings file is invalid XML.") from error
        value = root.findtext("{*}localRepository", "").strip()
        if value:
            value = value.replace("${user.home}", str(Path.home()))
            value = re.sub(r"\$\{env\.([^}]+)\}", lambda m: os.environ.get(m[1], m[0]), value)
            return Path(value).expanduser().resolve(strict=False)
    return Path.home() / ".m2/repository"
