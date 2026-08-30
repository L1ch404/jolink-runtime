"""Bundled Maven-native main/test Build World Probe."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SETTINGS_NS = "http://maven.apache.org/SETTINGS/1.0.0"
_MAX_SETTINGS_BYTES = 4 * 1024 * 1024
_MAX_REPORT_BYTES = 8 * 1024 * 1024


class MavenProbeError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    try:
        temporary.chmod(mode)
    except OSError:
        pass
    os.replace(temporary, path)


@dataclass(frozen=True)
class PreparedMavenProbe:
    goal: str
    settings_file: Path
    output_directory: Path
    implementation_id: str
    version: str
    repository_id: str


class ProductMavenProbe:
    """Verify and expose bundled Probe artifacts without editing user files."""

    def __init__(
        self,
        *,
        lock: dict[str, Any],
        jar: bytes,
        pom: bytes,
    ) -> None:
        probe = lock["maven_probe"]
        self.lock = lock
        self.jar = jar
        self.pom = pom
        self.group_id = str(probe["group_id"])
        self.artifact_id = str(probe["artifact_id"])
        self.version = str(probe["version"])
        self.implementation_id = str(probe["implementation_id"])
        self.schema = str(probe["schema"])
        if (
            _sha256(jar) != probe["sha256"]
            or _sha256(pom) != probe["pom_sha256"]
        ):
            raise MavenProbeError(
                "MAVEN_PROBE_INTEGRITY_MISMATCH",
                "The bundled Maven Probe does not match its product lock.",
            )
        majors: set[int] = set()
        try:
            with zipfile.ZipFile(__import__("io").BytesIO(jar)) as archive:
                for name in archive.namelist():
                    if name.endswith(".class"):
                        raw = archive.read(name)
                        if len(raw) < 8 or raw[:4] != b"\xca\xfe\xba\xbe":
                            raise ValueError("invalid class")
                        majors.add(int.from_bytes(raw[6:8], "big"))
        except (ValueError, zipfile.BadZipFile) as error:
            raise MavenProbeError(
                "MAVEN_PROBE_INTEGRITY_MISMATCH",
                "The bundled Maven Probe class files are invalid.",
            ) from error
        if majors != {int(lock["class_major"])}:
            raise MavenProbeError(
                "MAVEN_PROBE_INTEGRITY_MISMATCH",
                "The bundled Maven Probe uses an unexpected Java level.",
            )

    @classmethod
    def load(cls) -> "ProductMavenProbe":
        root = Path(__file__).parent
        try:
            lock = json.loads(
                (root / "fast-test-assets.json").read_text(encoding="utf-8")
            )
            jar = base64.b64decode(
                (root / "maven-build-world-probe.jar.b64").read_text(
                    encoding="ascii"
                ),
                validate=False,
            )
            pom = (root / "maven-build-world-probe.pom").read_bytes()
            return cls(lock=lock, jar=jar, pom=pom)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            raise MavenProbeError(
                "MAVEN_PROBE_UNAVAILABLE",
                "The bundled Maven Probe assets are unavailable.",
            ) from error

    @property
    def goal(self) -> str:
        return (
            f"{self.group_id}:{self.artifact_id}:{self.version}:"
            "export-build-world"
        )

    def prepare(
        self,
        *,
        attempt_directory: Path,
        source_settings: Path | None,
        local_repository: Path,
        offline: bool,
    ) -> PreparedMavenProbe:
        repository_identity = _sha256(self.jar + self.pom)
        repository_id = f"jolink-local-probe-{repository_identity[:12]}"
        repository_root = (
            Path.home()
            / ".cache/jolink-runtime/maven-probe"
            / repository_identity[:16]
        )
        self._stage(repository_root)
        # Maven can become offline through MAVEN_ARGS, .mvn/maven.config, or
        # wrapper policy. Always seed this one content-checked coordinate into
        # the selected local repository; Maven would cache the same plugin
        # there after an online resolution anyway.
        self._stage(local_repository)
        settings = attempt_directory / "maven-probe-settings.xml"
        self._create_settings(
            source_settings=source_settings,
            destination=settings,
            repository_root=repository_root,
            repository_id=repository_id,
        )
        output = attempt_directory / "maven-probe-output"
        output.mkdir(parents=True, exist_ok=False, mode=0o700)
        return PreparedMavenProbe(
            goal=self.goal,
            settings_file=settings,
            output_directory=output,
            implementation_id=self.implementation_id,
            version=self.version,
            repository_id=repository_id,
        )

    def _stage(self, repository_root: Path) -> None:
        artifact = (
            repository_root
            / Path(*self.group_id.split("."))
            / self.artifact_id
            / self.version
        )
        for suffix, data in (("jar", self.jar), ("pom", self.pom)):
            destination = artifact / (
                f"{self.artifact_id}-{self.version}.{suffix}"
            )
            if destination.exists():
                if _sha256(destination.read_bytes()) != _sha256(data):
                    raise MavenProbeError(
                        "MAVEN_PROBE_REPOSITORY_COLLISION",
                        "The content-addressed Probe repository has different bytes.",
                    )
                continue
            _write_atomic(destination, data)
            _write_atomic(
                destination.with_suffix(destination.suffix + ".sha1"),
                hashlib.sha1(data).hexdigest().encode("ascii"),
            )

    def _create_settings(
        self,
        *,
        source_settings: Path | None,
        destination: Path,
        repository_root: Path,
        repository_id: str,
    ) -> None:
        if source_settings is not None:
            data = source_settings.expanduser().resolve(strict=True).read_bytes()
            if len(data) > _MAX_SETTINGS_BYTES:
                raise MavenProbeError(
                    "MAVEN_SETTINGS_UNSUPPORTED",
                    "The Maven settings file exceeds the Probe limit.",
                )
            lowered = data.lower()
            if b"<!doctype" in lowered or b"<!entity" in lowered:
                raise MavenProbeError(
                    "MAVEN_SETTINGS_UNSUPPORTED",
                    "The Maven settings file contains unsupported declarations.",
                )
            try:
                root = ET.fromstring(data)
            except ET.ParseError as error:
                raise MavenProbeError(
                    "MAVEN_SETTINGS_UNSUPPORTED",
                    "The Maven settings file is invalid XML.",
                ) from error
        else:
            ET.register_namespace("", _SETTINGS_NS)
            root = ET.Element(f"{{{_SETTINGS_NS}}}settings")
        namespace = (
            root.tag[1:].split("}", 1)[0]
            if root.tag.startswith("{")
            else ""
        )
        if namespace:
            ET.register_namespace("", namespace)
        if root.tag.rsplit("}", 1)[-1] != "settings":
            raise MavenProbeError(
                "MAVEN_SETTINGS_UNSUPPORTED",
                "The Maven settings document has an unexpected root.",
            )

        def tag(local: str) -> str:
            return f"{{{namespace}}}{local}" if namespace else local

        def child(parent: ET.Element, local: str) -> ET.Element:
            found = parent.find(tag(local))
            return found if found is not None else ET.SubElement(parent, tag(local))

        mirrors = root.find(tag("mirrors"))
        if mirrors is not None:
            for mirror in mirrors.findall(tag("mirror")):
                mirror_of = mirror.find(tag("mirrorOf"))
                if mirror_of is None or not mirror_of.text:
                    continue
                tokens = [item.strip() for item in mirror_of.text.split(",")]
                exclusion = f"!{repository_id}"
                if "*" in tokens and exclusion not in tokens:
                    tokens.append(exclusion)
                    mirror_of.text = ",".join(tokens)

        profiles = child(root, "profiles")
        if any(
            (item.findtext(tag("id")) or "").strip() == repository_id
            for item in profiles.findall(tag("profile"))
        ):
            raise MavenProbeError(
                "MAVEN_PROBE_SETTINGS_COLLISION",
                "The Maven settings already contain the Probe profile id.",
            )
        profile = ET.SubElement(profiles, tag("profile"))
        ET.SubElement(profile, tag("id")).text = repository_id
        repositories = ET.SubElement(profile, tag("pluginRepositories"))
        repository = ET.SubElement(repositories, tag("pluginRepository"))
        ET.SubElement(repository, tag("id")).text = repository_id
        ET.SubElement(repository, tag("url")).text = repository_root.as_uri()
        releases = ET.SubElement(repository, tag("releases"))
        ET.SubElement(releases, tag("enabled")).text = "true"
        ET.SubElement(releases, tag("updatePolicy")).text = "never"
        snapshots = ET.SubElement(repository, tag("snapshots"))
        ET.SubElement(snapshots, tag("enabled")).text = "false"
        active = child(root, "activeProfiles")
        ET.SubElement(active, tag("activeProfile")).text = repository_id
        _write_atomic(
            destination,
            ET.tostring(root, encoding="utf-8", xml_declaration=True),
        )

    def load_snapshot(
        self,
        prepared: PreparedMavenProbe,
        *,
        module_root: Path,
    ) -> dict[str, Any]:
        matches: list[dict[str, Any]] = []
        for path in sorted(prepared.output_directory.glob("*.json")):
            if path.stat().st_size > _MAX_REPORT_BYTES:
                raise MavenProbeError(
                    "MAVEN_PROBE_OUTPUT_INVALID",
                    "The Maven Probe output exceeds the safety limit.",
                )
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise MavenProbeError(
                    "MAVEN_PROBE_OUTPUT_INVALID",
                    "The Maven Probe output is invalid.",
                ) from error
            project = payload.get("project")
            if (
                payload.get("schema") != self.schema
                or payload.get("probeImplementationId")
                != prepared.implementation_id
                or not isinstance(project, dict)
            ):
                raise MavenProbeError(
                    "MAVEN_PROBE_IDENTITY_MISMATCH",
                    "Maven executed an unexpected Probe implementation.",
                )
            base = project.get("baseDirectory")
            if isinstance(base, str) and Path(base).resolve(strict=False) == (
                module_root.resolve(strict=False)
            ):
                matches.append(payload)
        if len(matches) != 1:
            raise MavenProbeError(
                "MAVEN_PROBE_MODULE_AMBIGUOUS",
                "The Probe did not produce one exact selected-module snapshot.",
            )
        return matches[0]


__all__ = [
    "MavenProbeError",
    "PreparedMavenProbe",
    "ProductMavenProbe",
]
