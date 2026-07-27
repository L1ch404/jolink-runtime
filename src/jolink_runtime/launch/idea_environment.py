"""Read the small IDEA environment subset needed by the Maven launcher."""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


_MAX_XML_BYTES = 2 * 1024 * 1024
_MACRO_PATTERN = re.compile(r"\$[A-Z][A-Z0-9_]*\$")


@dataclass(frozen=True)
class IdeaBuildPreferences:
    custom_maven_home: Path | None = None
    user_settings_file: Path | None = None
    local_repository: Path | None = None
    project_jdk_name: str | None = None
    maven_runner_jdk_name: str | None = None
    active_profiles: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    jdk_homes_by_name: dict[str, tuple[Path, ...]] = field(
        default_factory=dict,
        repr=False,
    )

    def redacted_summary(self) -> dict[str, object]:
        return {
            "custom_maven_home": (
                str(self.custom_maven_home)
                if self.custom_maven_home is not None
                else None
            ),
            "user_settings_file": (
                str(self.user_settings_file)
                if self.user_settings_file is not None
                else None
            ),
            "local_repository": (
                str(self.local_repository)
                if self.local_repository is not None
                else None
            ),
            "project_jdk_name": self.project_jdk_name,
            "maven_runner_jdk_name": self.maven_runner_jdk_name,
            "active_profiles": list(self.active_profiles),
            "known_jdk_names": sorted(self.jdk_homes_by_name),
            "warnings": list(self.warnings),
        }


class IdeaEnvironmentImporter:
    """Best-effort IDEA toolchain metadata import; never executes a command."""

    def __init__(
        self,
        *,
        idea_config_roots: tuple[Path, ...] | None = None,
    ) -> None:
        self._idea_config_roots = (
            idea_config_roots
            if idea_config_roots is not None
            else self._default_idea_config_roots()
        )

    def import_preferences(
        self,
        project_path: str | os.PathLike[str],
    ) -> IdeaBuildPreferences:
        project_root = Path(project_path).expanduser().resolve(strict=True)
        warnings: list[str] = []
        workspace = self._read_project_xml(
            project_root,
            project_root / ".idea" / "workspace.xml",
            warnings,
        )
        misc = self._read_project_xml(
            project_root,
            project_root / ".idea" / "misc.xml",
            warnings,
        )

        workspace_options = self._maven_options(workspace)
        custom_maven_home = self._path_option(
            (
                workspace_options.get("customMavenHome")
                or workspace_options.get("mavenHome")
            ),
            project_root,
            warnings,
            "customMavenHome",
        )
        settings = self._path_option(
            workspace_options.get("userSettingsFile"),
            project_root,
            warnings,
            "userSettingsFile",
        )
        local_repository = self._path_option(
            workspace_options.get("localRepository"),
            project_root,
            warnings,
            "localRepository",
        )

        project_jdk_name = None
        if misc is not None:
            manager = next(
                (
                    element
                    for element in misc.iter("component")
                    if element.get("name") == "ProjectRootManager"
                ),
                None,
            )
            if manager is not None:
                project_jdk_name = (
                    str(manager.get("project-jdk-name", "")).strip()
                    or None
                )

        maven_runner_jdk_name = self._maven_runner_jdk(workspace)
        active_profiles = self._active_profiles(workspace, misc)
        jdk_homes = self._read_jdk_tables(warnings)
        return IdeaBuildPreferences(
            custom_maven_home=custom_maven_home,
            user_settings_file=settings,
            local_repository=local_repository,
            project_jdk_name=project_jdk_name,
            maven_runner_jdk_name=maven_runner_jdk_name,
            active_profiles=active_profiles,
            warnings=tuple(warnings),
            jdk_homes_by_name=jdk_homes,
        )

    def _read_project_xml(
        self,
        project_root: Path,
        source: Path,
        warnings: list[str],
    ) -> ET.Element | None:
        if not source.is_file() or source.is_symlink():
            return None
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(project_root)
            return self._read_safe_utf8_xml(resolved)
        except (OSError, ValueError, ET.ParseError):
            warnings.append(
                f"{source.relative_to(project_root).as_posix()}: unreadable"
            )
            return None

    def _read_jdk_tables(
        self,
        warnings: list[str],
    ) -> dict[str, tuple[Path, ...]]:
        discovered: dict[str, list[Path]] = {}
        sources: list[tuple[Path, Path]] = []
        for root in self._idea_config_roots:
            if root.name == "jdk.table.xml":
                sources.append((root, root.parent))
            elif root.is_dir():
                sources.extend(
                    (source, root)
                    for source in root.glob("*/options/jdk.table.xml")
                )
                sources.extend(
                    (source, root)
                    for source in root.glob("options/jdk.table.xml")
                )
        for source, allowed_root in sorted(
            set(sources),
            key=lambda item: (
                self._safe_mtime(item[0]),
                str(item[0]),
            ),
            reverse=True,
        ):
            try:
                resolved = source.expanduser().resolve(strict=True)
                resolved.relative_to(
                    allowed_root.expanduser().resolve(strict=True)
                )
                xml_root = self._read_safe_utf8_xml(resolved)
            except (OSError, ValueError, ET.ParseError):
                warnings.append("IDEA JDK table could not be read safely.")
                continue
            for jdk in xml_root.iter("jdk"):
                name_element = jdk.find("./name")
                type_element = jdk.find("./type")
                home_element = jdk.find("./homePath")
                if (
                    name_element is None
                    or home_element is None
                    or (
                        type_element is not None
                        and type_element.get("value") != "JavaSDK"
                    )
                ):
                    continue
                name = str(name_element.get("value", "")).strip()
                raw_home = str(home_element.get("value", "")).strip()
                home = self._expand_path(
                    raw_home,
                    project_root=None,
                )
                if not name or home is None:
                    continue
                homes = discovered.setdefault(name, [])
                if home not in homes:
                    homes.append(home)
        return {
            name: tuple(homes)
            for name, homes in discovered.items()
        }

    @staticmethod
    def _maven_options(
        xml_root: ET.Element | None,
    ) -> dict[str, str]:
        if xml_root is None:
            return {}
        values: dict[str, str] = {}
        accepted_names = {
            "customMavenHome",
            "mavenHome",
            "userSettingsFile",
            "localRepository",
        }
        for component in xml_root.iter("component"):
            component_name = str(component.get("name", ""))
            if component_name not in {
                "MavenImportPreferences",
                "MavenProjectsManager",
            }:
                continue
            for option in component.iter("option"):
                name = str(option.get("name", ""))
                if name in accepted_names:
                    values[name] = str(option.get("value", ""))
        return values

    @staticmethod
    def _maven_runner_jdk(xml_root: ET.Element | None) -> str | None:
        if xml_root is None:
            return None
        for element in xml_root.iter():
            if element.tag not in {"component", "MavenRunnerSettings"}:
                continue
            marker = str(element.get("name", element.tag))
            if "MavenRunner" not in marker:
                continue
            for option in element.iter("option"):
                if option.get("name") in {"jreName", "runnerJre"}:
                    value = str(option.get("value", "")).strip()
                    if value:
                        return value
        return None

    @staticmethod
    def _active_profiles(
        *roots: ET.Element | None,
    ) -> tuple[str, ...]:
        profiles: list[str] = []
        for root in roots:
            if root is None:
                continue
            maven_components = [
                component
                for component in root.iter("component")
                if "Maven" in str(component.get("name", ""))
            ]
            for component in maven_components:
                for option in component.iter("option"):
                    if option.get("name") == "explicitProfiles":
                        for entry in option.iter("entry"):
                            enabled = str(
                                entry.get("value", "true")
                            ).casefold()
                            name = str(entry.get("key", "")).strip()
                            if name and enabled not in {"false", "0", "no"}:
                                profiles.append(name)
            for option in (
                option
                for component in maven_components
                for option in component.iter("option")
            ):
                if option.get("name") not in {
                    "activeProfiles",
                    "enabledProfiles",
                }:
                    continue
                direct = str(option.get("value", "")).strip()
                if direct:
                    profiles.extend(
                        value.strip()
                        for value in direct.split(",")
                        if value.strip()
                    )
                for child in option.iter("option"):
                    value = str(child.get("value", "")).strip()
                    if value:
                        profiles.append(value)
        return tuple(dict.fromkeys(profiles))

    @classmethod
    def _path_option(
        cls,
        raw: str | None,
        project_root: Path,
        warnings: list[str],
        option_name: str,
    ) -> Path | None:
        if not raw:
            return None
        normalized = raw.strip().casefold()
        if (
            normalized.startswith(("bundled", "wrapper"))
            or normalized in {
                "use maven wrapper",
                "use bundled maven",
            }
        ):
            warnings.append(f"{option_name}: IDEA-managed value not portable")
            return None
        path = cls._expand_path(raw, project_root=project_root)
        if path is None:
            warnings.append(f"{option_name}: unresolved")
        return path

    @staticmethod
    def _expand_path(
        raw: str,
        *,
        project_root: Path | None,
    ) -> Path | None:
        expanded = raw.removeprefix("file://")
        replacements = {"$USER_HOME$": str(Path.home())}
        if project_root is not None:
            replacements["$PROJECT_DIR$"] = str(project_root)
        for macro, value in replacements.items():
            expanded = expanded.replace(macro, value)
        if _MACRO_PATTERN.search(expanded):
            return None
        path = Path(expanded).expanduser()
        if not path.is_absolute():
            if project_root is None:
                return None
            path = project_root / path
        return path.resolve(strict=False)

    @staticmethod
    def _read_safe_utf8_xml(source: Path) -> ET.Element:
        if source.stat().st_size > _MAX_XML_BYTES:
            raise ET.ParseError("XML source exceeds the size limit")
        raw = source.read_bytes()
        if b"\x00" in raw:
            raise ET.ParseError("XML source must use UTF-8")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ET.ParseError("XML source must use UTF-8") from error
        lowered = text.casefold()
        if "<!doctype" in lowered or "<!entity" in lowered:
            raise ET.ParseError("DTD and entity declarations are rejected")
        root = ET.fromstring(text)
        node_count = 0
        stack: list[tuple[ET.Element, int]] = [(root, 1)]
        while stack:
            node, depth = stack.pop()
            node_count += 1
            if node_count > 20_000 or depth > 64:
                raise ET.ParseError("XML source exceeds structural limits")
            stack.extend((child, depth + 1) for child in node)
        return root

    @staticmethod
    def _safe_mtime(path: Path) -> float:
        try:
            return float(path.stat().st_mtime)
        except OSError:
            return 0.0

    @staticmethod
    def _default_idea_config_roots() -> tuple[Path, ...]:
        home = Path.home()
        if os.name == "nt":
            appdata = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
            return (appdata / "JetBrains",)
        if sys_platform() == "darwin":
            return (home / "Library/Application Support/JetBrains",)
        return (home / ".config/JetBrains",)


def sys_platform() -> str:
    """Small seam for platform-specific tests."""
    import sys

    return sys.platform


__all__ = [
    "IdeaBuildPreferences",
    "IdeaEnvironmentImporter",
]
