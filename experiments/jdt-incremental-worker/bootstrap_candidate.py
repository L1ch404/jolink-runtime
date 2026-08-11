#!/usr/bin/env python3
"""Resolve and lock a minimal Eclipse JDT/Equinox bootstrap candidate.

This script is intentionally isolated from the production package. It reads the
official Eclipse p2 metadata, resolves only mandatory OSGi bundle and package
requirements, downloads the corresponding bundle artifacts, and emits a
content-addressed lock. Running it is bootstrap discovery, not Phase 1 evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
from urllib.error import URLError
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote


USER_AGENT = "joLink-JDT-POC/0.1"
SUPPORTED_REQUIREMENT_NAMESPACES = frozenset(
    {"osgi.bundle", "java.package", "osgi.extender"}
)
SOURCE_FILTER_PROPERTY = "org.eclipse.update.install.sources"


class DiscoveryError(RuntimeError):
    """A deterministic bootstrap discovery failure."""


@dataclass(frozen=True, order=True)
class OSGiVersion:
    major: int
    minor: int
    micro: int
    qualifier: str = ""

    @classmethod
    def parse(cls, raw: str | None) -> "OSGiVersion":
        value = (raw or "0.0.0").strip()
        match = re.fullmatch(
            r"(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.([A-Za-z0-9_-]+))?",
            value,
        )
        if match is None:
            raise DiscoveryError(f"Invalid OSGi version: {value!r}")
        return cls(
            int(match.group(1)),
            int(match.group(2) or 0),
            int(match.group(3) or 0),
            match.group(4) or "",
        )

    def text(self) -> str:
        base = f"{self.major}.{self.minor}.{self.micro}"
        return f"{base}.{self.qualifier}" if self.qualifier else base


@dataclass(frozen=True)
class VersionRange:
    lower: OSGiVersion
    lower_inclusive: bool
    upper: OSGiVersion | None
    upper_inclusive: bool

    @classmethod
    def parse(cls, raw: str | None) -> "VersionRange":
        value = (raw or "0.0.0").strip()
        if value.startswith(("[", "(")):
            if not value.endswith(("]", ")")) or "," not in value:
                raise DiscoveryError(f"Invalid OSGi version range: {value!r}")
            lower_raw, upper_raw = value[1:-1].split(",", 1)
            return cls(
                OSGiVersion.parse(lower_raw or "0.0.0"),
                value[0] == "[",
                OSGiVersion.parse(upper_raw) if upper_raw else None,
                value[-1] == "]",
            )
        return cls(OSGiVersion.parse(value), True, None, False)

    def contains(self, version: OSGiVersion) -> bool:
        if version < self.lower or (version == self.lower and not self.lower_inclusive):
            return False
        if self.upper is None:
            return True
        if version > self.upper or (version == self.upper and not self.upper_inclusive):
            return False
        return True


@dataclass(frozen=True)
class Capability:
    namespace: str
    name: str
    version: OSGiVersion


@dataclass(frozen=True)
class Requirement:
    namespace: str
    name: str
    version_range: VersionRange


@dataclass
class Unit:
    unit_id: str
    version: OSGiVersion
    capabilities: tuple[Capability, ...]
    requirements: tuple[Requirement, ...]
    unsupported_mandatory_requirements: tuple[str, ...]
    artifact_classifier: str
    artifact_id: str
    artifact_version: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=f".{destination.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            shutil.copyfileobj(response, stream)
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _content_xml(repository_url: str, cache_root: Path) -> tuple[Path, dict[str, object]]:
    metadata_jar = cache_root / "metadata" / "content.jar"
    if not metadata_jar.exists():
        _download(f"{repository_url.rstrip('/')}/content.jar", metadata_jar)
    try:
        with zipfile.ZipFile(metadata_jar) as archive:
            names = [name for name in archive.namelist() if name.endswith("content.xml")]
            if len(names) != 1:
                raise DiscoveryError("p2 content.jar must contain exactly one content.xml")
            payload = archive.read(names[0])
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise DiscoveryError("Unable to read p2 content metadata.") from exc
    content_xml = cache_root / "metadata" / "content.xml"
    content_xml.write_bytes(payload)
    return content_xml, {
        "url": f"{repository_url.rstrip('/')}/content.jar",
        "sha256": sha256_file(metadata_jar),
        "bytes": metadata_jar.stat().st_size,
    }


def _bundle_artifact(unit_element: ET.Element) -> tuple[str, str, str] | None:
    artifacts = unit_element.find("artifacts")
    if artifacts is None:
        return None
    for artifact in artifacts.findall("artifact"):
        if artifact.get("classifier") == "osgi.bundle":
            artifact_id = artifact.get("id")
            version = artifact.get("version")
            if artifact_id and version:
                return "osgi.bundle", artifact_id, version
    return None


def parse_units(content_xml: Path) -> dict[str, Unit]:
    try:
        tree = ET.parse(content_xml)
    except (OSError, ET.ParseError) as exc:
        raise DiscoveryError("Unable to parse p2 content metadata.") from exc

    units: dict[str, Unit] = {}
    for element in tree.getroot().iter("unit"):
        artifact = _bundle_artifact(element)
        unit_id = element.get("id")
        raw_version = element.get("version")
        if artifact is None or not unit_id or not raw_version:
            continue

        capabilities: list[Capability] = []
        provides = element.find("provides")
        if provides is not None:
            for provided in provides.findall("provided"):
                namespace = provided.get("namespace")
                name = provided.get("name")
                if namespace and name:
                    capabilities.append(
                        Capability(
                            namespace,
                            name,
                            OSGiVersion.parse(provided.get("version")),
                        )
                    )

        requirements: list[Requirement] = []
        unsupported_mandatory: list[str] = []
        requires = element.find("requires")
        if requires is not None:
            for required in requires.findall("required"):
                if required.get("optional") == "true":
                    continue
                filter_text = (required.findtext("filter") or "").strip()
                if SOURCE_FILTER_PROPERTY in filter_text:
                    continue
                namespace = required.get("namespace")
                name = required.get("name")
                if namespace in SUPPORTED_REQUIREMENT_NAMESPACES and name:
                    requirements.append(
                        Requirement(
                            namespace,
                            name,
                            VersionRange.parse(required.get("range")),
                        )
                    )
                elif namespace != "org.eclipse.equinox.p2.iu":
                    unsupported_mandatory.append(
                        f"{namespace}/{name or '<unnamed>'}"
                    )
            for required in requires.findall("requiredProperties"):
                namespace = required.get("namespace")
                if namespace == "osgi.ee":
                    continue
                match = required.get("match") or ""
                if namespace not in SUPPORTED_REQUIREMENT_NAMESPACES:
                    unsupported_mandatory.append(
                        f"{namespace}/{match or '<unparsed>'}"
                    )
                    continue
                name_match = re.search(
                    rf"\({re.escape(namespace or '')}=([^()]+)\)", match
                )
                if namespace is None or name_match is None:
                    raise DiscoveryError(
                        f"Unable to parse mandatory capability filter: {match}"
                    )
                lower_match = re.search(r"\(version>=([^()]+)\)", match)
                upper_match = re.search(r"\(!\(version>=([^()]+)\)\)", match)
                lower = lower_match.group(1) if lower_match else "0.0.0"
                range_text = (
                    f"[{lower},{upper_match.group(1)})"
                    if upper_match
                    else lower
                )
                requirements.append(
                    Requirement(
                        namespace,
                        name_match.group(1),
                        VersionRange.parse(range_text),
                    )
                )

        candidate = Unit(
            unit_id=unit_id,
            version=OSGiVersion.parse(raw_version),
            capabilities=tuple(capabilities),
            requirements=tuple(requirements),
            unsupported_mandatory_requirements=tuple(unsupported_mandatory),
            artifact_classifier=artifact[0],
            artifact_id=artifact[1],
            artifact_version=artifact[2],
        )
        current = units.get(unit_id)
        if current is None or current.version < candidate.version:
            units[unit_id] = candidate
    return units


def parse_system_capabilities(
    content_xml: Path, *, java_major: int
) -> tuple[Capability, ...]:
    """Return JavaSE capabilities supplied by the selected Worker JDK level."""
    try:
        tree = ET.parse(content_xml)
    except (OSError, ET.ParseError) as exc:
        raise DiscoveryError("Unable to parse p2 JavaSE capabilities.") from exc
    expected = f"{java_major}.0.0"
    matches = [
        element
        for element in tree.getroot().iter("unit")
        if element.get("id") == "a.jre.javase"
        and OSGiVersion.parse(element.get("version")).text() == expected
    ]
    if len(matches) != 1:
        raise DiscoveryError(
            f"Expected one a.jre.javase capability unit for Java {java_major}."
        )
    capabilities: list[Capability] = []
    provides = matches[0].find("provides")
    if provides is not None:
        for provided in provides.findall("provided"):
            namespace = provided.get("namespace")
            name = provided.get("name")
            if namespace and name:
                capabilities.append(
                    Capability(
                        namespace,
                        name,
                        OSGiVersion.parse(provided.get("version")),
                    )
                )
    return tuple(capabilities)


def _provider_index(units: Iterable[Unit]) -> dict[tuple[str, str], list[Unit]]:
    providers: dict[tuple[str, str], list[Unit]] = {}
    for unit in units:
        for capability in unit.capabilities:
            providers.setdefault((capability.namespace, capability.name), []).append(unit)
    for values in providers.values():
        values.sort(key=lambda unit: unit.version, reverse=True)
    return providers


def _capability_satisfies(unit: Unit, requirement: Requirement) -> bool:
    return any(
        capability.namespace == requirement.namespace
        and capability.name == requirement.name
        and requirement.version_range.contains(capability.version)
        for capability in unit.capabilities
    )


def resolve_units(
    all_units: dict[str, Unit],
    root_ids: Iterable[str],
    *,
    system_capabilities: Iterable[Capability] = (),
) -> list[Unit]:
    providers = _provider_index(all_units.values())
    selected: dict[str, Unit] = {}
    queue: list[Unit] = []
    for root_id in root_ids:
        unit = all_units.get(root_id)
        if unit is None:
            raise DiscoveryError(f"Root installable unit is unavailable: {root_id}")
        selected[unit.unit_id] = unit
        queue.append(unit)

    while queue:
        consumer = queue.pop(0)
        if consumer.unsupported_mandatory_requirements:
            raise DiscoveryError(
                f"Selected unit {consumer.unit_id} has unsupported mandatory "
                "requirements: "
                + ", ".join(consumer.unsupported_mandatory_requirements)
            )
        for requirement in consumer.requirements:
            if any(
                capability.namespace == requirement.namespace
                and capability.name == requirement.name
                and requirement.version_range.contains(capability.version)
                for capability in system_capabilities
            ):
                continue
            already = next(
                (
                    unit
                    for unit in selected.values()
                    if _capability_satisfies(unit, requirement)
                ),
                None,
            )
            if already is not None:
                continue
            candidates = [
                unit
                for unit in providers.get((requirement.namespace, requirement.name), ())
                if _capability_satisfies(unit, requirement)
                and not unit.unit_id.endswith(".source")
            ]
            if not candidates:
                raise DiscoveryError(
                    "No provider for mandatory requirement "
                    f"{consumer.unit_id}: {requirement.namespace}/"
                    f"{requirement.name}"
                )
            provider = candidates[0]
            if provider.unit_id not in selected:
                selected[provider.unit_id] = provider
                queue.append(provider)

    return sorted(selected.values(), key=lambda unit: unit.unit_id)


def _manifest_headers(jar_path: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(jar_path) as archive:
            payload = archive.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise DiscoveryError(f"Unable to read bundle manifest: {jar_path.name}") from exc

    unfolded = re.sub(r"\r?\n ", "", payload)
    headers: dict[str, str] = {}
    for line in unfolded.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            headers[key] = value
    return headers


def _license_identity(jar_path: Path, headers: dict[str, str]) -> str:
    declared = headers.get("Bundle-License")
    if declared:
        return declared
    try:
        with zipfile.ZipFile(jar_path) as archive:
            names = {name.lower(): name for name in archive.namelist()}
            license_name = names.get("meta-inf/license")
            if license_name:
                text = archive.read(license_name).decode("utf-8", "replace")
                match = re.search(r"SPDX-License-Identifier:\s*([^\r\n]+)", text)
                if match:
                    return match.group(1).strip()
            about_name = names.get("about.html")
            if about_name:
                text = archive.read(about_name).decode("utf-8", "replace")
                if re.search(
                    r"Eclipse\s+Public\s+License\s+Version\s+2\.0", text
                ):
                    return "EPL-2.0"
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise DiscoveryError(
            f"Unable to determine bundle license: {jar_path.name}"
        ) from exc
    return "UNDECLARED"


def download_and_lock(
    *,
    repository_url: str,
    units: Iterable[Unit],
    cache_root: Path,
    candidate_id: str,
    bootstrap_config: dict[str, object],
    metadata: dict[str, object],
    lock_path: Path,
) -> dict[str, object]:
    artifacts: list[dict[str, object]] = []
    plugins = cache_root / "candidates" / candidate_id / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)

    for unit in units:
        filename = f"{unit.artifact_id}_{unit.artifact_version}.jar"
        url = (
            f"{repository_url.rstrip('/')}/plugins/"
            f"{quote(unit.artifact_id)}_{quote(unit.artifact_version)}.jar"
        )
        destination = plugins / filename
        if not destination.exists():
            _download(url, destination)
        headers = _manifest_headers(destination)
        symbolic_name = headers.get("Bundle-SymbolicName", unit.unit_id).split(";", 1)[0]
        artifacts.append(
            {
                "symbolic_name": symbolic_name,
                "version": headers.get("Bundle-Version", unit.artifact_version),
                "origin_url": url,
                "sha256": sha256_file(destination),
                "license_identity": _license_identity(destination, headers),
                "compressed_bytes": destination.stat().st_size,
                "installed_bytes": destination.stat().st_size,
                "bundle_start_level": 4,
                "activation_policy": headers.get(
                    "Bundle-ActivationPolicy", "eager_when_started"
                ),
                "filename": filename,
            }
        )

    lock: dict[str, object] = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "evidence_status": "locked_bootstrap_candidate_not_yet_evidence_bearing",
        "repository": {
            "url": repository_url,
            "content_metadata": metadata,
        },
        "worker_java_minimum": bootstrap_config["worker_java_minimum"],
        "root_installable_units": bootstrap_config["root_installable_units"],
        "equinox": {
            "application_id": "net.jolink.runtime.jdt.worker",
            "configuration_identity": "generated-from-this-lock",
            "default_start_level": 4,
        },
        "artifacts": artifacts,
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return lock


def load_bootstrap(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiscoveryError("Unable to read candidate bootstrap configuration.") from exc
    roots = payload.get("root_installable_units")
    if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
        raise DiscoveryError("root_installable_units must be a string array.")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bootstrap",
        type=Path,
        default=Path(__file__).with_name("candidate-bootstrap.json"),
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path.home() / ".cache" / "jolink-runtime" / "jdt-poc",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path(__file__).with_name("locks") / "eclipse-4.40-current.json",
    )
    parser.add_argument(
        "--resolve-only",
        action="store_true",
        help="Resolve and print the closure without downloading bundle artifacts.",
    )
    args = parser.parse_args(argv)

    try:
        bootstrap = load_bootstrap(args.bootstrap)
        repository_url = str(bootstrap["repository_url"])
        content_xml, metadata = _content_xml(repository_url, args.cache_root)
        all_units = parse_units(content_xml)
        worker_java_minimum = int(bootstrap["worker_java_minimum"])
        system_capabilities = parse_system_capabilities(
            content_xml, java_major=worker_java_minimum
        )
        units = resolve_units(
            all_units,
            bootstrap["root_installable_units"],
            system_capabilities=system_capabilities,
        )
        if args.resolve_only:
            print(
                json.dumps(
                    {
                        "candidate_id": bootstrap["candidate_id"],
                        "unit_count": len(units),
                        "units": [
                            {"id": unit.unit_id, "version": unit.version.text()}
                            for unit in units
                        ],
                    },
                    indent=2,
                )
            )
            return 0
        lock = download_and_lock(
            repository_url=repository_url,
            units=units,
            cache_root=args.cache_root,
            candidate_id=str(bootstrap["candidate_id"]),
            bootstrap_config=bootstrap,
            metadata=metadata,
            lock_path=args.lock,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "candidate_id": lock["candidate_id"],
                    "artifact_count": len(lock["artifacts"]),
                    "lock_path": str(args.lock.resolve()),
                    "cache_root": str(args.cache_root.resolve()),
                    "evidence_status": lock["evidence_status"],
                }
            )
        )
        return 0
    except (DiscoveryError, OSError, URLError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "JDT_CANDIDATE_DISCOVERY_FAILED",
                    "message": str(exc),
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
