"""Private Maven-native Build World probe injection helpers.

The probe remains a normal Maven Mojo.  joLink exposes its bundled artifact
through a content-addressed ``file://`` plugin repository and creates an
attempt-local settings file; user POMs and settings files are never modified.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from jolink_runtime.experiments.jdt_build_world import BuildWorldError, sha256_file


_SETTINGS_NS = "http://maven.apache.org/SETTINGS/1.0.0"
_MAX_SETTINGS_BYTES = 4 * 1024 * 1024
PROBE_GROUP_ID = "io.jolink"
PROBE_ARTIFACT_ID = "jolink-maven-probe"
PROBE_VERSION = "0.1.0-spike6"


@dataclass(frozen=True)
class StagedProbeRepository:
    root: Path
    repository_id: str
    version: str
    jar_sha256: str
    pom_sha256: str

    @property
    def goal(self) -> str:
        return (
            f"{PROBE_GROUP_ID}:{PROBE_ARTIFACT_ID}:{self.version}:"
            "export-build-world"
        )


def resolve_source_settings(
    explicit: Path | None,
    *,
    user_home: Path | None = None,
) -> tuple[Path | None, str]:
    """Resolve Maven user settings without replacing Maven's default semantics."""

    if explicit is not None:
        return explicit.expanduser().resolve(strict=True), "explicit"
    home = (
        user_home.expanduser().resolve(strict=False)
        if user_home is not None
        else Path.home()
    )
    default = home / ".m2" / "settings.xml"
    if default.is_file():
        return default.resolve(strict=True), "maven_user_default"
    return None, "generated_empty"


def _write_atomic(path: Path, data: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    try:
        temporary.chmod(mode)
    except OSError:
        pass
    os.replace(temporary, path)


def stage_probe_repository(
    *,
    probe_jar: Path,
    probe_pom: Path,
    repository_root: Path,
    version: str = PROBE_VERSION,
) -> StagedProbeRepository:
    """Create an immutable Maven2-layout file repository for the Probe."""

    jar = probe_jar.expanduser().resolve(strict=True)
    pom = probe_pom.expanduser().resolve(strict=True)
    jar_sha = sha256_file(jar)
    pom_sha = sha256_file(pom)
    repository_id = f"jolink-local-probe-{jar_sha[:12]}"
    root = (
        repository_root.expanduser().resolve(strict=False)
        / jar_sha[:16]
    )
    _stage_probe_artifacts(
        probe_jar=jar,
        probe_pom=pom,
        repository_root=root,
        version=version,
        jar_sha=jar_sha,
        pom_sha=pom_sha,
    )
    return StagedProbeRepository(
        root=root,
        repository_id=repository_id,
        version=version,
        jar_sha256=jar_sha,
        pom_sha256=pom_sha,
    )


def _stage_probe_artifacts(
    *,
    probe_jar: Path,
    probe_pom: Path,
    repository_root: Path,
    version: str,
    jar_sha: str,
    pom_sha: str,
) -> None:
    artifact = (
        repository_root
        / "io"
        / "jolink"
        / PROBE_ARTIFACT_ID
        / version
    )
    files = {
        artifact / f"{PROBE_ARTIFACT_ID}-{version}.jar": probe_jar,
        artifact / f"{PROBE_ARTIFACT_ID}-{version}.pom": probe_pom,
    }
    for destination, source in files.items():
        if destination.exists():
            expected = jar_sha if destination.suffix == ".jar" else pom_sha
            if sha256_file(destination) != expected:
                raise BuildWorldError(
                    "MAVEN_PROBE_REPOSITORY_COLLISION",
                    "The content-addressed Maven Probe repository contains "
                    "different artifact bytes.",
                    suggested_next_step="Discard the Probe cache and retry.",
                )
            continue
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.with_name(f".{destination.name}.tmp")
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
        digest = jar_sha if destination.suffix == ".jar" else pom_sha
        _write_atomic(
            destination.with_suffix(destination.suffix + ".sha1"),
            hashlib.sha1(destination.read_bytes()).hexdigest().encode("ascii"),
            mode=0o600,
        )
        if sha256_file(destination) != digest:
            raise BuildWorldError(
                "MAVEN_PROBE_REPOSITORY_WRITE_FAILED",
                "The staged Maven Probe artifact failed verification.",
                suggested_next_step="Discard the Probe cache and retry.",
                retryable=True,
            )


def stage_probe_in_local_repository(
    *,
    probe_jar: Path,
    probe_pom: Path,
    local_repository: Path,
    version: str = PROBE_VERSION,
) -> dict[str, object]:
    """Seed the Probe coordinate for a strict Maven offline invocation.

    Maven does not consult even a ``file://`` remote repository in offline
    mode.  A strict-offline attempt therefore needs the bundled Probe in the
    selected Maven local repository before Maven starts.  This is an explicit,
    bounded cache write; it never edits a user POM or settings file.
    """

    jar = probe_jar.expanduser().resolve(strict=True)
    pom = probe_pom.expanduser().resolve(strict=True)
    root = local_repository.expanduser().resolve(strict=False)
    jar_sha = sha256_file(jar)
    pom_sha = sha256_file(pom)
    _stage_probe_artifacts(
        probe_jar=jar,
        probe_pom=pom,
        repository_root=root,
        version=version,
        jar_sha=jar_sha,
        pom_sha=pom_sha,
    )
    return {
        "local_repository": root,
        "coordinate": f"{PROBE_GROUP_ID}:{PROBE_ARTIFACT_ID}:{version}",
        "jar_sha256": jar_sha,
        "pom_sha256": pom_sha,
    }


def _namespace(tag: str) -> str:
    if tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


def _tag(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}" if namespace else local


def _child(parent: ET.Element, namespace: str, local: str) -> ET.Element:
    found = parent.find(_tag(namespace, local))
    if found is not None:
        return found
    return ET.SubElement(parent, _tag(namespace, local))


def _exclude_probe_from_wildcard_mirrors(
    root: ET.Element, namespace: str, repository_id: str
) -> int:
    adjusted = 0
    mirrors = root.find(_tag(namespace, "mirrors"))
    if mirrors is None:
        return adjusted
    for mirror in mirrors.findall(_tag(namespace, "mirror")):
        mirror_of = mirror.find(_tag(namespace, "mirrorOf"))
        if mirror_of is None or not mirror_of.text:
            continue
        tokens = [item.strip() for item in mirror_of.text.split(",")]
        exclusion = f"!{repository_id}"
        if "*" in tokens and exclusion not in tokens:
            tokens.append(exclusion)
            mirror_of.text = ",".join(tokens)
            adjusted += 1
    return adjusted


def create_probe_settings(
    *,
    source_settings: Path | None,
    destination: Path,
    repository: StagedProbeRepository,
) -> dict[str, object]:
    """Create an attempt-private settings file with one local plugin repo."""

    source_sha: str | None = None
    if source_settings is not None:
        source = source_settings.expanduser().resolve(strict=True)
        data = source.read_bytes()
        if len(data) > _MAX_SETTINGS_BYTES:
            raise BuildWorldError(
                "MAVEN_SETTINGS_UNSUPPORTED",
                "The Maven settings file exceeds Probe safety limits.",
                suggested_next_step="Use the formal Maven build.",
            )
        lowered = data.lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise BuildWorldError(
                "MAVEN_SETTINGS_UNSUPPORTED",
                "The Maven settings file uses unsupported XML declarations.",
                suggested_next_step="Use the formal Maven build.",
            )
        try:
            root = ET.fromstring(data)
        except ET.ParseError as error:
            raise BuildWorldError(
                "MAVEN_SETTINGS_UNSUPPORTED",
                "The Maven settings file is not valid XML.",
                suggested_next_step="Correct the Maven settings file and retry.",
                retryable=True,
            ) from error
        source_sha = hashlib.sha256(data).hexdigest()
    else:
        ET.register_namespace("", _SETTINGS_NS)
        root = ET.Element(_tag(_SETTINGS_NS, "settings"))

    namespace = _namespace(root.tag)
    if namespace:
        # Maven's settings reader accepts the default settings namespace but
        # warns about (and may ignore) an equivalent prefixed root such as
        # ``ns0:settings``.  Preserve it as the default namespace when the
        # attempt-private copy is serialized.
        ET.register_namespace("", namespace)
    if root.tag.rsplit("}", 1)[-1] != "settings":
        raise BuildWorldError(
            "MAVEN_SETTINGS_UNSUPPORTED",
            "The Maven settings document has an unexpected root element.",
            suggested_next_step="Use the formal Maven build.",
        )
    profile_id = repository.repository_id
    profiles = _child(root, namespace, "profiles")
    if any(
        (item.findtext(_tag(namespace, "id")) or "").strip() == profile_id
        for item in profiles.findall(_tag(namespace, "profile"))
    ):
        raise BuildWorldError(
            "MAVEN_PROBE_SETTINGS_COLLISION",
            "The Maven settings already define the content-addressed Probe profile.",
            suggested_next_step="Discard the Probe attempt and retry.",
        )
    profile = ET.SubElement(profiles, _tag(namespace, "profile"))
    ET.SubElement(profile, _tag(namespace, "id")).text = profile_id
    repositories = ET.SubElement(
        profile, _tag(namespace, "pluginRepositories")
    )
    plugin_repository = ET.SubElement(
        repositories, _tag(namespace, "pluginRepository")
    )
    ET.SubElement(plugin_repository, _tag(namespace, "id")).text = profile_id
    ET.SubElement(plugin_repository, _tag(namespace, "url")).text = (
        repository.root.as_uri()
    )
    releases = ET.SubElement(plugin_repository, _tag(namespace, "releases"))
    ET.SubElement(releases, _tag(namespace, "enabled")).text = "true"
    ET.SubElement(releases, _tag(namespace, "updatePolicy")).text = "never"
    snapshots = ET.SubElement(plugin_repository, _tag(namespace, "snapshots"))
    ET.SubElement(snapshots, _tag(namespace, "enabled")).text = "false"

    active_profiles = _child(root, namespace, "activeProfiles")
    ET.SubElement(
        active_profiles, _tag(namespace, "activeProfile")
    ).text = profile_id
    adjusted = _exclude_probe_from_wildcard_mirrors(
        root, namespace, profile_id
    )
    rendered = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    target = destination.expanduser().resolve(strict=False)
    _write_atomic(target, rendered, mode=0o600)
    return {
        "settings_path": target,
        "source_settings_sha256": source_sha,
        "probe_repository_id": profile_id,
        "wildcard_mirror_adjustment_count": adjusted,
    }


__all__ = [
    "PROBE_ARTIFACT_ID",
    "PROBE_GROUP_ID",
    "PROBE_VERSION",
    "StagedProbeRepository",
    "create_probe_settings",
    "resolve_source_settings",
    "stage_probe_in_local_repository",
    "stage_probe_repository",
]
