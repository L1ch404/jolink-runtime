"""Safe, read-only import of supported IntelliJ IDEA launch configurations."""

from __future__ import annotations

import os
import re
import stat
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .contracts import LaunchErrorCode, LaunchIntent


_MAX_XML_BYTES = 2 * 1024 * 1024
_MAX_XML_NODES = 20_000
_MAX_XML_DEPTH = 64
_MACRO_PATTERN = re.compile(r"\$[A-Z][A-Z0-9_]*\$")

_APPLICATION_TYPES = frozenset(
    {
        "Application",
        "JavaApplication",
    }
)
_SPRING_BOOT_TYPES = frozenset(
    {
        "SpringBootApplicationConfigurationType",
        "SpringBootApplication",
    }
)
_SUPPORTED_TYPES = _APPLICATION_TYPES | _SPRING_BOOT_TYPES

_MAIN_CLASS_OPTIONS = (
    "MAIN_CLASS_NAME",
    "SPRING_BOOT_MAIN_CLASS",
    "MAIN_CLASS",
)
_WORKING_DIRECTORY_OPTIONS = (
    "WORKING_DIRECTORY",
    "WORKING_DIR",
)
_VM_ARGUMENT_OPTIONS = (
    "VM_PARAMETERS",
    "VM_OPTIONS",
)
_PROGRAM_ARGUMENT_OPTIONS = (
    "PROGRAM_PARAMETERS",
    "PROGRAM_ARGS",
)
_JRE_OPTIONS = (
    "ALTERNATIVE_JRE_PATH",
    "JRE_PATH",
)


class IdeaLaunchImportError(RuntimeError):
    """Structured importer failure whose payload never includes env values."""

    def __init__(
        self,
        error_code: LaunchErrorCode,
        message: str,
        *,
        retryable: bool = True,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.context = context or {}

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "ok": False,
            "error": str(self),
            "error_code": self.error_code.value,
            "retryable": self.retryable,
        }
        payload.update(
            {
                key: value
                for key, value in self.context.items()
                if key not in payload and key != "code"
            }
        )
        return payload


@dataclass(frozen=True)
class ImportedIdeaLaunch:
    """One supported configuration plus safe provenance."""

    intent: LaunchIntent
    source_file: str
    configuration_type: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def redacted_summary(self) -> dict[str, Any]:
        return {
            **self.intent.redacted_summary(),
            "source_file": self.source_file,
            "configuration_type": self.configuration_type,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class _UnsupportedConfiguration:
    name: str
    configuration_type: str
    source_file: str


@dataclass(frozen=True)
class _RejectedConfiguration:
    name: str
    source_file: str
    error: IdeaLaunchImportError


@dataclass(frozen=True)
class _ScanResult:
    candidates: tuple[ImportedIdeaLaunch, ...]
    unsupported: tuple[_UnsupportedConfiguration, ...]
    rejected: tuple[_RejectedConfiguration, ...]
    source_warnings: tuple[str, ...]


class IdeaLaunchImporter:
    """Import IDEA launch intent without executing IDE-defined tasks."""

    def __init__(
        self,
        *,
        max_xml_bytes: int = _MAX_XML_BYTES,
        max_xml_nodes: int = _MAX_XML_NODES,
        max_xml_depth: int = _MAX_XML_DEPTH,
    ) -> None:
        self._max_xml_bytes = int(max_xml_bytes)
        self._max_xml_nodes = int(max_xml_nodes)
        self._max_xml_depth = int(max_xml_depth)

    def discover(self, project_path: str | os.PathLike[str]) -> tuple[ImportedIdeaLaunch, ...]:
        """Return deterministic, de-duplicated supported configurations."""
        root = self._project_root(project_path)
        return self._scan(root).candidates

    def select(
        self,
        project_path: str | os.PathLike[str],
        launch_name: str | None = None,
    ) -> ImportedIdeaLaunch:
        """Select one exact configuration or return a structured ambiguity."""
        root = self._project_root(project_path)
        scan = self._scan(root)
        candidates = list(scan.candidates)

        if launch_name:
            exact = [
                item
                for item in candidates
                if item.intent.launch_name == launch_name
            ]
            matches = exact
            if not matches:
                rejected = [
                    item
                    for item in scan.rejected
                    if item.name == launch_name
                ]
                if rejected:
                    self._raise_rejected(rejected[0])
                unsupported = [
                    item
                    for item in scan.unsupported
                    if item.name == launch_name
                ]
                if unsupported:
                    selected = unsupported[0]
                    raise IdeaLaunchImportError(
                        LaunchErrorCode.UNSUPPORTED_LAUNCH_CONFIGURATION,
                        (
                            f"IDEA launch configuration '{launch_name}' uses "
                            "a type that joLink P0 does not execute."
                        ),
                        retryable=False,
                        context={
                            "launch_name": launch_name,
                            "configuration_type": (
                                selected.configuration_type
                            ),
                            "source_file": selected.source_file,
                            "suggested_next_step": (
                                "Choose an IDEA Application or Spring Boot "
                                "configuration, or use the existing direct "
                                "classpath/JAR launch parameters."
                            ),
                        },
                    )
                self._raise_not_found(
                    root,
                    launch_name=launch_name,
                    candidates=candidates,
                    source_warnings=scan.source_warnings,
                )
            return self._only_candidate(
                root,
                matches,
                requested_name=launch_name,
            )

        if not candidates and len(scan.rejected) == 1:
            self._raise_rejected(scan.rejected[0])
        if not candidates:
            self._raise_not_found(
                root,
                launch_name=None,
                candidates=candidates,
                source_warnings=scan.source_warnings,
            )
        return self._only_candidate(root, candidates, requested_name=None)

    def _scan(self, root: Path) -> _ScanResult:
        module_directories, module_warnings = self._module_directories(root)
        candidates: list[ImportedIdeaLaunch] = []
        unsupported: list[_UnsupportedConfiguration] = []
        rejected: list[_RejectedConfiguration] = []
        source_warnings = list(module_warnings)

        for source in self._configuration_sources(root):
            try:
                xml_root = self._safe_xml_root(root, source)
            except IdeaLaunchImportError as error:
                source_warnings.append(
                    f"{source.relative_to(root)}: {error.error_code.value}"
                )
                continue

            for configuration in xml_root.iter("configuration"):
                if configuration.get("default", "").casefold() == "true":
                    continue
                name = (configuration.get("name") or "").strip()
                configuration_type = self._configuration_type(configuration)
                if not name:
                    continue
                relative_source = source.relative_to(root).as_posix()
                if not self._is_supported(
                    configuration_type,
                    configuration.get("factoryName", ""),
                ):
                    unsupported.append(
                        _UnsupportedConfiguration(
                            name=name,
                            configuration_type=configuration_type or "unknown",
                            source_file=relative_source,
                        )
                    )
                    continue
                try:
                    imported = self._parse_configuration(
                        root,
                        source,
                        configuration,
                        configuration_type,
                        module_directories,
                    )
                except IdeaLaunchImportError as error:
                    rejected.append(
                        _RejectedConfiguration(
                            name=name,
                            source_file=relative_source,
                            error=error,
                        )
                    )
                    source_warnings.append(
                        f"{relative_source}:{name}: {error.error_code.value}"
                    )
                    continue
                candidates.append(imported)

        return _ScanResult(
            candidates=tuple(self._deduplicate(candidates)),
            unsupported=tuple(unsupported),
            rejected=tuple(rejected),
            source_warnings=tuple(source_warnings),
        )

    def _parse_configuration(
        self,
        root: Path,
        source: Path,
        configuration: ET.Element,
        configuration_type: str,
        module_directories: dict[str, Path],
    ) -> ImportedIdeaLaunch:
        options = {
            str(option.get("name")): str(option.get("value", ""))
            for option in configuration.findall("./option")
            if option.get("name")
        }
        main_class = self._first_option(options, _MAIN_CLASS_OPTIONS).strip()
        name = str(configuration.get("name", "")).strip()
        if not main_class:
            raise IdeaLaunchImportError(
                LaunchErrorCode.UNSUPPORTED_LAUNCH_CONFIGURATION,
                (
                    f"IDEA launch configuration '{name}' does not declare "
                    "a supported main class."
                ),
                retryable=False,
            )

        if (
            configuration.find("./target") is not None
            or configuration.find("./projectPathOnTarget") is not None
        ):
            raise IdeaLaunchImportError(
                LaunchErrorCode.UNSUPPORTED_LAUNCH_CONFIGURATION,
                (
                    f"IDEA launch configuration '{name}' targets a remote "
                    "environment, which joLink P0 does not execute."
                ),
                retryable=False,
                context={
                    "configuration_type": configuration_type,
                    "suggested_next_step": (
                        "Choose a local IDEA Application or Spring Boot "
                        "configuration."
                    ),
                },
            )

        unsupported_tasks = [
            str(option.get("name", "")).strip() or "unknown"
            for option in configuration.findall("./method/option")
            if str(option.get("enabled", "true")).casefold() != "false"
            and str(option.get("name", "")).casefold() != "make"
        ]
        if unsupported_tasks:
            raise IdeaLaunchImportError(
                LaunchErrorCode.UNSUPPORTED_LAUNCH_CONFIGURATION,
                (
                    f"IDEA launch configuration '{name}' contains an "
                    "unsupported enabled before-launch task."
                ),
                retryable=False,
                context={
                    "unsupported_before_launch_tasks": unsupported_tasks,
                    "suggested_next_step": (
                        "Disable the extra IDEA before-launch task or use "
                        "explicit direct launch parameters."
                    ),
                },
            )

        if options.get("PASS_PARENT_ENVS", "true").casefold() == "false":
            raise IdeaLaunchImportError(
                LaunchErrorCode.UNSUPPORTED_LAUNCH_CONFIGURATION,
                (
                    f"IDEA launch configuration '{name}' disables parent "
                    "environment inheritance, which joLink P0 cannot yet "
                    "reproduce."
                ),
                retryable=False,
                context={
                    "argument": "PASS_PARENT_ENVS",
                    "suggested_next_step": (
                        "Use a configuration that inherits the parent "
                        "environment or use explicit direct launch parameters."
                    ),
                },
            )

        module_name = self._module_name(configuration, options)
        module_directory = (
            module_directories.get(module_name)
            if module_name
            else root
        )
        macros = {
            "$PROJECT_DIR$": str(root),
            "$USER_HOME$": str(Path.home()),
        }
        if module_directory is not None:
            macros["$MODULE_DIR$"] = str(module_directory)
            macros["$MODULE_WORKING_DIR$"] = str(module_directory)

        warnings: list[str] = []
        working_raw = (
            self._first_option(options, _WORKING_DIRECTORY_OPTIONS)
            or "$PROJECT_DIR$"
        )
        working_raw = self._strip_file_url(working_raw)
        working_directory = Path(
            self._expand_macros(
                working_raw,
                macros,
                field_name="working_directory",
            )
        ).expanduser()
        if not working_directory.is_absolute():
            working_directory = root / working_directory
        working_directory = working_directory.resolve(strict=False)

        vm_raw = self._first_option(options, _VM_ARGUMENT_OPTIONS)
        program_raw = self._first_option(options, _PROGRAM_ARGUMENT_OPTIONS)
        jvm_args = self._split_arguments(
            self._expand_macros(vm_raw, macros, field_name="jvm_args"),
            field_name="jvm_args",
            warnings=warnings,
        )
        active_profiles = options.get("ACTIVE_PROFILES", "").strip()
        if active_profiles and not any(
            argument.startswith("-Dspring.profiles.active=")
            for argument in jvm_args
        ):
            jvm_args = (
                *jvm_args,
                f"-Dspring.profiles.active={active_profiles}",
            )
        program_args = self._split_arguments(
            self._expand_macros(
                program_raw,
                macros,
                field_name="program_args",
            ),
            field_name="program_args",
            warnings=warnings,
        )

        environment: dict[str, str] = {}
        for env in configuration.findall("./envs/env"):
            env_name = str(env.get("name", "")).strip()
            if not env_name:
                continue
            environment[env_name] = self._expand_macros(
                str(env.get("value", "")),
                macros,
                field_name=f"environment.{env_name}",
            )

        jre_reference = (
            self._first_option(options, _JRE_OPTIONS).strip() or None
        )
        if (
            options.get(
                "ALTERNATIVE_JRE_PATH_ENABLED",
                "true",
            ).casefold()
            == "false"
        ):
            jre_reference = None
        if jre_reference is not None:
            jre_reference = self._expand_macros(
                jre_reference,
                macros,
                field_name="runtime_jdk_reference",
            )

        build_before_run = any(
            str(option.get("name", "")).casefold() == "make"
            and str(option.get("enabled", "true")).casefold() != "false"
            for option in configuration.findall("./method/option")
        )
        launch_type = (
            "spring_boot"
            if self._is_spring_boot(
                configuration_type,
                configuration.get("factoryName", ""),
            )
            else "java_application"
        )
        return ImportedIdeaLaunch(
            intent=LaunchIntent(
                source="idea",
                launch_name=name,
                launch_type=launch_type,
                ide_module_name=module_name,
                main_class=main_class,
                working_directory=working_directory,
                jvm_args=jvm_args,
                program_args=program_args,
                environment=environment,
                build_before_run=build_before_run,
                runtime_jdk_reference=jre_reference,
            ),
            source_file=source.relative_to(root).as_posix(),
            configuration_type=configuration_type,
            warnings=tuple(warnings),
        )

    def _module_directories(
        self,
        root: Path,
    ) -> tuple[dict[str, Path], tuple[str, ...]]:
        modules_file = root / ".idea" / "modules.xml"
        if not modules_file.is_file() or modules_file.is_symlink():
            return {}, ()
        try:
            xml_root = self._safe_xml_root(root, modules_file)
        except IdeaLaunchImportError as error:
            return {}, (f".idea/modules.xml: {error.error_code.value}",)

        macros = {
            "$PROJECT_DIR$": str(root),
            "$USER_HOME$": str(Path.home()),
        }
        modules: dict[str, Path] = {}
        for module in xml_root.iter("module"):
            raw = module.get("filepath") or module.get("fileurl") or ""
            raw = raw.removeprefix("file://")
            try:
                expanded = self._expand_macros(
                    raw,
                    macros,
                    field_name="module_path",
                )
            except IdeaLaunchImportError:
                continue
            path = Path(expanded).expanduser()
            if not path.is_absolute():
                path = root / path
            path = path.resolve(strict=False)
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if path.suffix.casefold() != ".iml":
                continue
            modules[path.stem] = path.parent
        return modules, ()

    def _configuration_sources(self, root: Path) -> tuple[Path, ...]:
        sources: list[Path] = []
        for directory in (
            root / ".run",
            root / ".idea" / "runConfigurations",
        ):
            if not directory.is_dir() or directory.is_symlink():
                continue
            sources.extend(
                path
                for path in sorted(directory.glob("*.xml"))
                if path.is_file() and not path.is_symlink()
            )
        workspace = root / ".idea" / "workspace.xml"
        if workspace.is_file() and not workspace.is_symlink():
            sources.append(workspace)
        return tuple(sources)

    def _safe_xml_root(self, project_root: Path, source: Path) -> ET.Element:
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(project_root)
            metadata = resolved.stat()
        except (OSError, ValueError) as error:
            raise IdeaLaunchImportError(
                LaunchErrorCode.IDEA_CONFIGURATION_READ_FAILED,
                "IDEA configuration source is outside the project or unreadable.",
            ) from error
        if not stat.S_ISREG(metadata.st_mode) or source.is_symlink():
            raise IdeaLaunchImportError(
                LaunchErrorCode.IDEA_CONFIGURATION_READ_FAILED,
                "IDEA configuration source is not a regular project file.",
            )
        if metadata.st_size > self._max_xml_bytes:
            raise IdeaLaunchImportError(
                LaunchErrorCode.IDEA_CONFIGURATION_READ_FAILED,
                "IDEA configuration source exceeds the safe size limit.",
            )
        try:
            raw = resolved.read_bytes()
        except OSError as error:
            raise IdeaLaunchImportError(
                LaunchErrorCode.IDEA_CONFIGURATION_READ_FAILED,
                "IDEA configuration source could not be read.",
            ) from error
        if b"\x00" in raw:
            raise IdeaLaunchImportError(
                LaunchErrorCode.IDEA_CONFIGURATION_READ_FAILED,
                "IDEA configuration XML must use UTF-8 encoding.",
                retryable=False,
            )
        try:
            xml_text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise IdeaLaunchImportError(
                LaunchErrorCode.IDEA_CONFIGURATION_READ_FAILED,
                "IDEA configuration XML must use UTF-8 encoding.",
                retryable=False,
            ) from error
        lowered = xml_text.casefold()
        if "<!doctype" in lowered or "<!entity" in lowered:
            raise IdeaLaunchImportError(
                LaunchErrorCode.IDEA_CONFIGURATION_READ_FAILED,
                "DTD and entity declarations are not accepted.",
                retryable=False,
            )
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as error:
            raise IdeaLaunchImportError(
                LaunchErrorCode.IDEA_CONFIGURATION_READ_FAILED,
                "IDEA configuration source is not valid XML.",
            ) from error

        node_count = 0
        stack: list[tuple[ET.Element, int]] = [(root, 1)]
        while stack:
            node, depth = stack.pop()
            node_count += 1
            if node_count > self._max_xml_nodes or depth > self._max_xml_depth:
                raise IdeaLaunchImportError(
                    LaunchErrorCode.IDEA_CONFIGURATION_READ_FAILED,
                    "IDEA configuration XML exceeds structural safety limits.",
                    retryable=False,
                )
            stack.extend((child, depth + 1) for child in node)
        return root

    @staticmethod
    def _configuration_type(configuration: ET.Element) -> str:
        return str(configuration.get("type") or "").strip()

    @staticmethod
    def _is_spring_boot(configuration_type: str, factory_name: str) -> bool:
        return (
            configuration_type in _SPRING_BOOT_TYPES
            or "spring boot" in factory_name.casefold()
        )

    @classmethod
    def _is_supported(
        cls,
        configuration_type: str,
        factory_name: str,
    ) -> bool:
        if configuration_type:
            return configuration_type in _SUPPORTED_TYPES
        return factory_name.casefold() in {"application", "spring boot"}

    @staticmethod
    def _first_option(
        options: dict[str, str],
        names: Iterable[str],
    ) -> str:
        for name in names:
            if name in options:
                return options[name]
        return ""

    @staticmethod
    def _module_name(
        configuration: ET.Element,
        options: dict[str, str],
    ) -> str | None:
        module = configuration.find("./module")
        if module is not None and module.get("name"):
            return str(module.get("name")).strip() or None
        for option_name in ("MODULE_NAME", "MODULE"):
            if options.get(option_name):
                return options[option_name].strip() or None
        return None

    @staticmethod
    def _expand_macros(
        value: str,
        macros: dict[str, str],
        *,
        field_name: str,
    ) -> str:
        expanded = value
        for macro, replacement in macros.items():
            expanded = expanded.replace(macro, replacement)
        unresolved = sorted(set(_MACRO_PATTERN.findall(expanded)))
        if unresolved:
            raise IdeaLaunchImportError(
                LaunchErrorCode.UNRESOLVED_IDEA_MACRO,
                (
                    f"IDEA field '{field_name}' contains an unsupported or "
                    "unresolved macro."
                ),
                retryable=False,
                context={
                    "argument": field_name,
                    "unresolved_macros": unresolved,
                    "suggested_next_step": (
                        "Use PROJECT_DIR, MODULE_DIR, MODULE_WORKING_DIR, or "
                        "USER_HOME only, or provide equivalent explicit "
                        "launch parameters."
                    ),
                },
            )
        return expanded

    @staticmethod
    def _split_arguments(
        value: str,
        *,
        field_name: str,
        warnings: list[str],
    ) -> tuple[str, ...]:
        if not value.strip():
            return ()
        arguments: list[str] = []
        current: list[str] = []
        quote: str | None = None
        for character in value:
            if character in {'"', "'"}:
                if quote is None:
                    quote = character
                elif quote == character:
                    quote = None
                else:
                    current.append(character)
                continue
            if character.isspace() and quote is None:
                if current:
                    arguments.append("".join(current))
                    current = []
                continue
            current.append(character)
        if quote is not None:
            warnings.append(
                f"{field_name} could not be tokenized; preserved as one value."
            )
            return (value,)
        if current:
            arguments.append("".join(current))
        return tuple(arguments)

    @staticmethod
    def _strip_file_url(value: str) -> str:
        if value.casefold().startswith("file://"):
            return value[7:]
        return value

    @staticmethod
    def _deduplicate(
        candidates: list[ImportedIdeaLaunch],
    ) -> list[ImportedIdeaLaunch]:
        unique: list[ImportedIdeaLaunch] = []
        seen: set[tuple[Any, ...]] = set()
        for candidate in candidates:
            intent = candidate.intent
            key = (
                intent.launch_name,
                intent.launch_type,
                intent.ide_module_name,
                intent.main_class,
                str(intent.working_directory),
                intent.jvm_args,
                intent.program_args,
                tuple(sorted(intent.environment.items())),
                intent.build_before_run,
                intent.runtime_jdk_reference,
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return unique

    @staticmethod
    def _project_root(project_path: str | os.PathLike[str]) -> Path:
        try:
            root = Path(project_path).expanduser().resolve(strict=True)
        except OSError as error:
            raise IdeaLaunchImportError(
                LaunchErrorCode.INVALID_PROJECT_PATH,
                "project_path does not identify a readable local directory.",
                context={
                    "argument": "project_path",
                    "suggested_next_step": (
                        "Provide the absolute path of the local project root."
                    ),
                },
            ) from error
        if not root.is_dir():
            raise IdeaLaunchImportError(
                LaunchErrorCode.INVALID_PROJECT_PATH,
                "project_path does not identify a readable local directory.",
                context={
                    "argument": "project_path",
                    "suggested_next_step": (
                        "Provide the absolute path of the local project root."
                    ),
                },
            )
        return root

    @staticmethod
    def _only_candidate(
        root: Path,
        candidates: list[ImportedIdeaLaunch],
        *,
        requested_name: str | None,
    ) -> ImportedIdeaLaunch:
        if len(candidates) == 1:
            return candidates[0]
        raise IdeaLaunchImportError(
            LaunchErrorCode.AMBIGUOUS_LAUNCH_CONFIGURATION,
            "Multiple supported IDEA launch configurations match.",
            context={
                "project_path": str(root),
                "launch_name": requested_name,
                "candidates": [
                    candidate.redacted_summary()
                    for candidate in candidates
                ],
                "suggested_next_step": (
                    "Retry run with the exact launch_name of one candidate."
                ),
            },
        )

    @staticmethod
    def _raise_not_found(
        root: Path,
        *,
        launch_name: str | None,
        candidates: list[ImportedIdeaLaunch],
        source_warnings: tuple[str, ...],
    ) -> None:
        raise IdeaLaunchImportError(
            LaunchErrorCode.LAUNCH_CONFIGURATION_NOT_FOUND,
            "No matching supported IDEA launch configuration was found.",
            context={
                "project_path": str(root),
                "launch_name": launch_name,
                "candidates": [
                    candidate.redacted_summary()
                    for candidate in candidates
                ],
                "source_warnings": list(source_warnings),
                "suggested_next_step": (
                    "Create or choose an IDEA Application or Spring Boot "
                    "configuration, or use the existing direct classpath/JAR "
                    "launch parameters."
                ),
            },
        )

    @staticmethod
    def _raise_rejected(rejected: _RejectedConfiguration) -> None:
        error = rejected.error
        context = {
            **error.context,
            "launch_name": rejected.name,
            "source_file": rejected.source_file,
        }
        raise IdeaLaunchImportError(
            error.error_code,
            str(error),
            retryable=error.retryable,
            context=context,
        )


__all__ = [
    "IdeaLaunchImportError",
    "IdeaLaunchImporter",
    "ImportedIdeaLaunch",
]
