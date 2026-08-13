"""Private Phase 2A Maven-to-JDT Build World experiment model.

This module is deliberately not reachable from the MCP boundary.  It freezes
and materializes compiler inputs below an attempt directory so the JDT worker
cannot consume the selected module's Maven output by accident.  Shareable
summaries contain counts and fingerprints, never workspace or repository
paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jolink_runtime.adapters.java.classfile import (
    ClassFileFormatError,
    ParsedClassFile,
    parse_class_file,
)


_PROCESSOR_SERVICE = "META-INF/services/javax.annotation.processing.Processor"
_MAX_SERVICE_BYTES = 64 * 1024
_MAX_SOURCE_FILES = 50_000
_MAX_SOURCE_BYTES = 1024 * 1024 * 1024
_MAX_CLASS_FILES = 100_000
_DECLARED_CLASS = re.compile(r"^(?!.*\$\d+)(?!.*\$\$Lambda\$).+$")
_PUBLIC_OR_PROTECTED = 0x0001 | 0x0004
_SYNTHETIC = 0x1000
_BRIDGE = 0x0040
_CLASS_API_FLAGS = 0x0001 | 0x0010 | 0x0200 | 0x0400 | 0x2000 | 0x4000
_API_METADATA = frozenset(
    {
        "Signature",
        "RuntimeVisibleAnnotations",
        "RuntimeVisibleParameterAnnotations",
        "RuntimeVisibleTypeAnnotations",
        "AnnotationDefault",
        "Exceptions",
        "Record",
        "PermittedSubclasses",
    }
)


class BuildWorldError(RuntimeError):
    """Structured experiment failure safe to surface without private values."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        suggested_next_step: str,
        retryable: bool = False,
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.suggested_next_step = suggested_next_step
        self.context = dict(context or {})

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": False,
            "error_code": self.error_code,
            "error": str(self),
            "retryable": self.retryable,
            "suggested_next_step": self.suggested_next_step,
            **self.context,
        }


@dataclass(frozen=True)
class SourceRootInput:
    path: Path = field(repr=False)
    mount_relative: Path = field(repr=False)
    provenance: str
    content_sha256: str
    java_source_count: int

    def redacted_summary(self) -> dict[str, object]:
        return {
            "provenance": self.provenance,
            "content_sha256": self.content_sha256,
            "java_source_count": self.java_source_count,
        }


@dataclass(frozen=True)
class DependencyInput:
    path: Path = field(repr=False)
    content_sha256: str
    entry_type: str
    processor_providers: tuple[str, ...] = field(default_factory=tuple, repr=False)
    lombok_version: str | None = None

    def redacted_summary(self) -> dict[str, object]:
        return {
            "entry_type": self.entry_type,
            "content_sha256": self.content_sha256,
            "annotation_processor_count": len(self.processor_providers),
            "processor_identity_sha256": canonical_fingerprint(
                self.processor_providers
            ),
            "contains_lombok": self.lombok_version is not None,
            "lombok_version": self.lombok_version,
        }


@dataclass(frozen=True)
class BuildWorldSnapshot:
    workspace_root: Path = field(repr=False)
    module_root: Path = field(repr=False)
    maven_output: Path = field(repr=False)
    source_roots: tuple[SourceRootInput, ...]
    dependencies: tuple[DependencyInput, ...]
    source_level: int
    target_level: int
    encoding: str
    configuration_fingerprint: str
    declared_processor_count: int
    declared_processor_identity_sha256: str
    declared_processor_kinds: tuple[str, ...]
    processor_option_count: int
    processor_option_identity_sha256: str
    self_output_on_compile_classpath: bool
    stale_candidate_output_on_classpath: bool
    phase2b_incremental_eligible: bool
    phase2b_blockers: tuple[str, ...]
    fingerprint: str

    @property
    def lombok_dependencies(self) -> tuple[DependencyInput, ...]:
        return tuple(item for item in self.dependencies if item.lombok_version)

    def redacted_summary(self) -> dict[str, object]:
        generated = [
            item.redacted_summary()
            for item in self.source_roots
            if item.provenance != "DECLARED_SOURCE"
        ]
        return {
            "schema": "jolink.build-world-snapshot.v1",
            "snapshot_fingerprint": self.fingerprint,
            "configuration_fingerprint": self.configuration_fingerprint,
            "source_level": self.source_level,
            "target_level": self.target_level,
            "encoding": self.encoding,
            "source_root_count": len(self.source_roots),
            "java_source_count": sum(
                item.java_source_count for item in self.source_roots
            ),
            "generated_source_roots": generated,
            "compile_classpath_entry_count": len(self.dependencies),
            "compile_classpath_fingerprint": canonical_fingerprint(
                [item.content_sha256 for item in self.dependencies]
            ),
            "annotation_processor_artifact_count": sum(
                bool(item.processor_providers) for item in self.dependencies
            ),
            "annotation_processor_identity_sha256": canonical_fingerprint(
                sorted(
                    provider
                    for item in self.dependencies
                    for provider in item.processor_providers
                )
            ),
            "declared_processor_count": self.declared_processor_count,
            "declared_processor_identity_sha256": (
                self.declared_processor_identity_sha256
            ),
            "declared_processor_kinds": list(self.declared_processor_kinds),
            "processor_option_count": self.processor_option_count,
            "processor_option_identity_sha256": (
                self.processor_option_identity_sha256
            ),
            "lombok_versions": sorted(
                {
                    item.lombok_version
                    for item in self.dependencies
                    if item.lombok_version
                }
            ),
            "self_output_on_compile_classpath": (
                self.self_output_on_compile_classpath
            ),
            "stale_candidate_output_on_classpath": (
                self.stale_candidate_output_on_classpath
            ),
            "phase2b_incremental_eligible": self.phase2b_incremental_eligible,
            "phase2b_blockers": list(self.phase2b_blockers),
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def tree_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(child.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(child)))
    return digest.hexdigest()


def canonical_fingerprint(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def path_fingerprint(path: Path) -> str:
    if path.is_file():
        return sha256_file(path)
    if path.is_dir():
        return tree_fingerprint(path)
    raise BuildWorldError(
        "COMPILE_CLASSPATH_ENTRY_UNAVAILABLE",
        "A compile classpath entry is unavailable.",
        suggested_next_step="Refresh the Maven Build World and retry.",
        retryable=True,
    )


def is_within(path: Path, boundary: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(boundary.resolve(strict=False))
        return True
    except ValueError:
        return False


def filter_self_outputs(
    entries: Iterable[Path],
    *,
    module_root: Path,
    maven_output: Path,
    private_candidate_output: Path,
    current_module_coordinate: tuple[str, str, str] | None = None,
) -> tuple[tuple[Path, ...], dict[str, object]]:
    """Remove every current-module output before JDT sees the classpath."""

    module = module_root.resolve(strict=True)
    target = (module / "target").resolve(strict=False)
    output = maven_output.resolve(strict=False)
    private_output = private_candidate_output.resolve(strict=False)
    accepted: list[Path] = []
    excluded = 0

    def current_module_artifact(entry: Path) -> bool:
        if current_module_coordinate is None or not entry.is_file():
            return False
        group_id, artifact_id, version = current_module_coordinate
        expected_name = f"{artifact_id}-{version}.jar"
        if entry.name != expected_name:
            return False
        suffix = (*group_id.split("."), artifact_id, version, expected_name)
        return tuple(entry.parts[-len(suffix) :]) == suffix

    for raw in entries:
        entry = raw.expanduser().resolve(strict=False)
        if (
            entry == output
            or entry == private_output
            or is_within(entry, target)
            or current_module_artifact(entry)
        ):
            excluded += 1
            continue
        if entry not in accepted:
            accepted.append(entry)
    self_present = any(
        entry == output or is_within(entry, target) for entry in accepted
    )
    stale_present = any(entry == private_output for entry in accepted)
    if self_present or stale_present:
        raise BuildWorldError(
            "SELF_OUTPUT_ON_COMPILE_CLASSPATH",
            "The selected module output remained on the JDT classpath.",
            suggested_next_step="Discard the attempt and inspect Build World discovery.",
        )
    return tuple(accepted), {
        "excluded_self_output_entry_count": excluded,
        "self_output_on_compile_classpath": False,
        "stale_candidate_output_on_classpath": False,
    }


def inspect_dependency(path: Path) -> DependencyInput:
    resolved = path.resolve(strict=True)
    providers: tuple[str, ...] = ()
    lombok_version: str | None = None
    if resolved.is_file():
        entry_type = "archive"
        try:
            with zipfile.ZipFile(resolved) as archive:
                names = set(archive.namelist())
                if _PROCESSOR_SERVICE in names:
                    info = archive.getinfo(_PROCESSOR_SERVICE)
                    if info.file_size > _MAX_SERVICE_BYTES:
                        raise BuildWorldError(
                            "ANNOTATION_PROCESSOR_METADATA_UNAVAILABLE",
                            "An annotation Processor declaration exceeds safety limits.",
                            suggested_next_step="Use the formal Maven build.",
                        )
                    providers = parse_processor_service(
                        archive.read(_PROCESSOR_SERVICE)
                    )
                try:
                    manifest = archive.read("META-INF/MANIFEST.MF")
                except KeyError:
                    manifest = b""
                match = re.search(
                    rb"(?im)^Lombok-Version:\s*([^\r\n]+)", manifest
                )
                if match is not None:
                    lombok_version = match.group(1).decode("ascii").strip()
                elif any(
                    name.startswith("lombok/") and name.endswith(".class")
                    for name in names
                ):
                    lombok_version = "unversioned"
        except zipfile.BadZipFile as error:
            raise BuildWorldError(
                "COMPILE_CLASSPATH_ENTRY_UNAVAILABLE",
                "A compile classpath archive is invalid.",
                suggested_next_step="Refresh Maven dependencies and retry.",
                retryable=True,
            ) from error
    elif resolved.is_dir():
        entry_type = "class_directory"
        service = resolved / _PROCESSOR_SERVICE
        if service.is_file():
            if service.stat().st_size > _MAX_SERVICE_BYTES:
                raise BuildWorldError(
                    "ANNOTATION_PROCESSOR_METADATA_UNAVAILABLE",
                    "An annotation Processor declaration exceeds safety limits.",
                    suggested_next_step="Use the formal Maven build.",
                )
            providers = parse_processor_service(service.read_bytes())
        if (resolved / "lombok").is_dir():
            lombok_version = "unversioned"
    else:
        raise BuildWorldError(
            "COMPILE_CLASSPATH_ENTRY_UNAVAILABLE",
            "A compile classpath entry is not a file or directory.",
            suggested_next_step="Refresh the Maven Build World and retry.",
            retryable=True,
        )
    return DependencyInput(
        path=resolved,
        content_sha256=path_fingerprint(resolved),
        entry_type=entry_type,
        processor_providers=providers,
        lombok_version=lombok_version,
    )


def parse_processor_service(data: bytes) -> tuple[str, ...]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise BuildWorldError(
            "ANNOTATION_PROCESSOR_METADATA_UNAVAILABLE",
            "An annotation Processor declaration is not UTF-8.",
            suggested_next_step="Use the formal Maven build.",
        ) from error
    providers: list[str] = []
    for line in text.splitlines():
        value = line.partition("#")[0].strip()
        if value and value not in providers:
            providers.append(value)
    return tuple(providers)


def describe_source_root(
    path: Path, provenance: str, *, workspace_root: Path | None = None
) -> SourceRootInput:
    resolved = path.resolve(strict=True)
    workspace = (
        workspace_root.resolve(strict=True)
        if workspace_root is not None
        else resolved
    )
    try:
        mount = resolved.relative_to(workspace)
    except ValueError as error:
        raise BuildWorldError(
            "SOURCE_ROOT_OUTSIDE_WORKSPACE",
            "A Build World source root is outside the Maven workspace.",
            suggested_next_step="Select a workspace-contained representative module.",
        ) from error
    sources = list(resolved.rglob("*.java"))
    return SourceRootInput(
        path=resolved,
        mount_relative=mount,
        provenance=provenance,
        content_sha256=tree_fingerprint(resolved),
        java_source_count=len(sources),
    )


def create_snapshot(
    *,
    workspace_root: Path,
    module_root: Path,
    maven_output: Path,
    source_roots: Sequence[SourceRootInput],
    compile_classpath: Sequence[Path],
    private_candidate_output: Path,
    source_level: int,
    target_level: int,
    encoding: str,
    configuration_fingerprint: str,
    current_module_coordinate: tuple[str, str, str] | None = None,
    declared_processor_identities: Sequence[str] = (),
    declared_processor_kinds: Sequence[str] = (),
    processor_option_identities: Sequence[str] = (),
) -> BuildWorldSnapshot:
    if source_level != 8 or target_level != 8:
        raise BuildWorldError(
            "JDT_PROJECT_MODEL_UNSUPPORTED",
            "The frozen Phase 2A worker currently models Java 8 projects only.",
            suggested_next_step=(
                "Use a Java 8 representative module or extend the versioned "
                "JDT project model in a later experiment."
            ),
            context={"source_level": source_level, "target_level": target_level},
        )
    filtered, invariants = filter_self_outputs(
        compile_classpath,
        module_root=module_root,
        maven_output=maven_output,
        private_candidate_output=private_candidate_output,
        current_module_coordinate=current_module_coordinate,
    )
    dependencies = tuple(inspect_dependency(path) for path in filtered)
    providers = {
        provider
        for dependency in dependencies
        for provider in dependency.processor_providers
    }
    lombok_providers = {
        provider for provider in providers if provider.startswith("lombok.")
    }
    unknown = providers - lombok_providers
    compile_time_generated = any(
        root.provenance == "COMPILE_TIME_AP_GENERATED" for root in source_roots
    )
    blockers: list[str] = []
    if unknown:
        blockers.append("unknown_compile_time_annotation_processor")
    if "unknown" in declared_processor_kinds:
        blockers.append("unknown_declared_annotation_processor")
    if compile_time_generated:
        blockers.append("compile_time_generated_source_refresh_unverified")
    material = {
        "source_roots": [
            (
                item.mount_relative.as_posix(),
                item.provenance,
                item.content_sha256,
                item.java_source_count,
            )
            for item in source_roots
        ],
        "dependencies": [item.content_sha256 for item in dependencies],
        "source_level": source_level,
        "target_level": target_level,
        "encoding": encoding,
        "configuration_fingerprint": configuration_fingerprint,
        "declared_processor_identities": sorted(declared_processor_identities),
        "declared_processor_kinds": sorted(declared_processor_kinds),
        "processor_option_identities": sorted(processor_option_identities),
        **invariants,
        "phase2b_blockers": blockers,
    }
    return BuildWorldSnapshot(
        workspace_root=workspace_root.resolve(strict=True),
        module_root=module_root.resolve(strict=True),
        maven_output=maven_output.resolve(strict=False),
        source_roots=tuple(source_roots),
        dependencies=dependencies,
        source_level=source_level,
        target_level=target_level,
        encoding=encoding,
        configuration_fingerprint=configuration_fingerprint,
        declared_processor_count=len(declared_processor_identities),
        declared_processor_identity_sha256=canonical_fingerprint(
            sorted(declared_processor_identities)
        ),
        declared_processor_kinds=tuple(sorted(set(declared_processor_kinds))),
        processor_option_count=len(processor_option_identities),
        processor_option_identity_sha256=canonical_fingerprint(
            sorted(processor_option_identities)
        ),
        self_output_on_compile_classpath=False,
        stale_candidate_output_on_classpath=False,
        phase2b_incremental_eligible=not blockers,
        phase2b_blockers=tuple(blockers),
        fingerprint=canonical_fingerprint(material),
    )


def materialize_private_sources(
    snapshot: BuildWorldSnapshot,
    *,
    destination: Path,
    config_files: Sequence[tuple[Path, Path]] = (),
) -> dict[str, object]:
    """Merge frozen roots into the Worker's one private Java source folder."""

    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(destination.iterdir()):
        raise BuildWorldError(
            "PRIVATE_SOURCE_DIRECTORY_NOT_EMPTY",
            "The private JDT source directory is not empty before materialization.",
            suggested_next_step="Discard the attempt and retry with a fresh workspace.",
        )
    count = 0
    total_bytes = 0
    copied: dict[str, str] = {}
    for root in snapshot.source_roots:
        for source in sorted(root.path.rglob("*")):
            if source.is_symlink() or _is_link_or_reparse(source):
                raise BuildWorldError(
                    "SOURCE_LINK_UNSUPPORTED",
                    "A source input contains a link or reparse point.",
                    suggested_next_step="Use regular workspace source files.",
                )
            if not source.is_file():
                continue
            # The frozen Worker has one source folder named ``src``.  Merge
            # Maven roots at that source-folder boundary so Java package paths
            # remain valid; collisions are rejected instead of using Maven
            # root order as an implicit precedence rule.
            relative_path = source.relative_to(root.path)
            relative = relative_path.as_posix()
            if source.suffix != ".java":
                continue
            count += 1
            total_bytes += source.stat().st_size
            if count > _MAX_SOURCE_FILES or total_bytes > _MAX_SOURCE_BYTES:
                raise BuildWorldError(
                    "SOURCE_SNAPSHOT_LIMIT_EXCEEDED",
                    "The representative module exceeds Phase 2A source limits.",
                    suggested_next_step="Select one smaller representative module.",
                )
            digest = sha256_file(source)
            previous = copied.get(relative)
            if previous is not None and previous != digest:
                raise BuildWorldError(
                    "SOURCE_ROOT_COLLISION",
                    "Two Build World source roots contain different files at one relative path.",
                    suggested_next_step="Inspect generated-source root precedence.",
                )
            if previous is not None:
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if sha256_file(target) != digest:
                raise BuildWorldError(
                    "SOURCE_SNAPSHOT_CHANGED",
                    "A private source copy could not be frozen.",
                    suggested_next_step="Discard the attempt and retry.",
                    retryable=True,
                )
            copied[relative] = digest
    for source, relative_target in config_files:
        if relative_target.is_absolute() or ".." in relative_target.parts:
            raise BuildWorldError(
                "PRIVATE_CONFIG_LAYOUT_UNREPRESENTABLE",
                "A compiler configuration cannot be mapped into the private workspace.",
                suggested_next_step="Use the formal Maven build for this module.",
            )
        target = destination.parent / relative_target
        target.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        if target.exists() and target.read_bytes() != data:
            raise BuildWorldError(
                "PRIVATE_CONFIG_LAYOUT_UNREPRESENTABLE",
                "Two compiler configurations map to the same private path.",
                suggested_next_step="Use the formal Maven build for this module.",
            )
        target.write_bytes(data)
    return {
        "java_source_count": count,
        "source_bytes": total_bytes,
        "private_source_fingerprint": canonical_fingerprint(copied),
        "configuration_file_count": len(config_files),
    }


def write_worker_classpath(
    *, system_library_file: Path, snapshot: BuildWorldSnapshot, output: Path
) -> None:
    system_entries = [
        line.strip()
        for line in system_library_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    entries = [*system_entries, *(str(item.path) for item in snapshot.dependencies)]
    output.write_text("\n".join(entries) + "\n", encoding="utf-8")
    try:
        output.chmod(0o600)
    except OSError:
        pass


def classify_diagnostics(diagnostics: Sequence[str]) -> dict[str, object]:
    buckets = {
        "missing_dependency": 0,
        "missing_generated_source": 0,
        "processor_or_generated_api_mismatch": 0,
        "language_or_compiler_incompatibility": 0,
        "other": 0,
    }
    identities: list[str] = []
    for diagnostic in diagnostics:
        lowered = diagnostic.casefold()
        if any(
            token in lowered
            for token in (
                "the import ",
                "cannot be resolved to a type",
                "indirectly referenced from required .class files",
            )
        ):
            bucket = "missing_dependency"
        elif any(
            token in lowered
            for token in ("mapperimpl", "generated", "query type", "qclass")
        ):
            bucket = "missing_generated_source"
        elif any(
            token in lowered
            for token in (
                "builder()",
                "log cannot be resolved",
                "cannot be resolved or is not a field",
            )
        ):
            bucket = "processor_or_generated_api_mismatch"
        elif any(
            token in lowered
            for token in ("syntax error", "preview feature", "source level")
        ):
            bucket = "language_or_compiler_incompatibility"
        else:
            bucket = "other"
        buckets[bucket] += 1
        identities.append(hashlib.sha256(diagnostic.encode("utf-8")).hexdigest())
    return {
        "diagnostic_count": len(diagnostics),
        "buckets": buckets,
        "diagnostic_identity_sha256": canonical_fingerprint(sorted(identities)),
        "raw_diagnostics_in_report": False,
    }


def compare_class_outputs(
    *, maven_output: Path, jdt_output: Path
) -> dict[str, object]:
    maven = _parse_class_tree(maven_output)
    jdt = _parse_class_tree(jdt_output)
    maven_declared = {name for name in maven if _is_declared_class(name, maven[name])}
    jdt_declared = {name for name in jdt if _is_declared_class(name, jdt[name])}
    missing = sorted(maven_declared - jdt_declared)
    extra = sorted(jdt_declared - maven_declared)
    common = sorted(maven_declared & jdt_declared)
    api_mismatches: list[str] = []
    major_mismatches: list[str] = []
    for name in common:
        if maven[name].major_version != jdt[name].major_version:
            major_mismatches.append(name)
        if _api_shape(maven[name]) != _api_shape(jdt[name]):
            api_mismatches.append(name)
    tier1_ok = not (missing or extra or api_mismatches or major_mismatches)
    tier2_maven = set(maven) - maven_declared
    tier2_jdt = set(jdt) - jdt_declared
    return {
        "comparison": "maven_javac_vs_jdt_structural_v1",
        "class_loading_or_initialization_used": False,
        "maven_class_count": len(maven),
        "jdt_class_count": len(jdt),
        "tier1": {
            "status": "compatible" if tier1_ok else "incompatible",
            "source_declared_type_sets_equal": not missing and not extra,
            "missing_declared_type_count": len(missing),
            "extra_declared_type_count": len(extra),
            "api_mismatch_count": len(api_mismatches),
            "class_major_mismatch_count": len(major_mismatches),
            "mismatch_identity_sha256": canonical_fingerprint(
                {
                    "missing": missing,
                    "extra": extra,
                    "api": api_mismatches,
                    "major": major_mismatches,
                }
            ),
        },
        "tier2": {
            "maven_compiler_generated_class_count": len(tier2_maven),
            "jdt_compiler_generated_class_count": len(tier2_jdt),
            "compiler_generated_sets_equal": tier2_maven == tier2_jdt,
            "difference_identity_sha256": canonical_fingerprint(
                sorted(tier2_maven ^ tier2_jdt)
            ),
            "status": "recorded_not_gate",
        },
    }


def _parse_class_tree(root: Path) -> dict[str, ParsedClassFile]:
    result: dict[str, ParsedClassFile] = {}
    paths = sorted(root.rglob("*.class")) if root.is_dir() else []
    if len(paths) > _MAX_CLASS_FILES:
        raise BuildWorldError(
            "CLASS_OUTPUT_LIMIT_EXCEEDED",
            "The compiler output exceeds the Phase 2A class limit.",
            suggested_next_step="Select one representative module.",
        )
    for path in paths:
        try:
            parsed = parse_class_file(path.read_bytes())
        except (OSError, ClassFileFormatError) as error:
            raise BuildWorldError(
                "CLASS_STRUCTURE_UNAVAILABLE",
                "A compiler output class cannot be parsed safely.",
                suggested_next_step="Inspect the local attempt and compiler output.",
            ) from error
        if parsed.binary_name in result:
            raise BuildWorldError(
                "DUPLICATE_CLASS_OUTPUT",
                "A compiler output contains duplicate binary names.",
                suggested_next_step="Inspect generated-source and output roots.",
            )
        result[parsed.binary_name] = parsed
    return result


def _is_declared_class(name: str, parsed: ParsedClassFile) -> bool:
    return not (parsed.access_flags & _SYNTHETIC) and bool(_DECLARED_CLASS.match(name))


def _metadata_subset(metadata: Sequence[tuple[str, Any]]) -> tuple[tuple[str, Any], ...]:
    return tuple((name, value) for name, value in metadata if name in _API_METADATA)


def _api_shape(parsed: ParsedClassFile) -> object:
    def members(values: Sequence[Any]) -> list[object]:
        return sorted(
            (
                item.name,
                item.descriptor,
                item.access_flags & ~(_SYNTHETIC | _BRIDGE),
                _metadata_subset(item.metadata),
            )
            for item in values
            if item.access_flags & _PUBLIC_OR_PROTECTED
            and not item.access_flags & (_SYNTHETIC | _BRIDGE)
        )

    return (
        parsed.binary_name,
        parsed.major_version,
        parsed.access_flags & _CLASS_API_FLAGS,
        parsed.super_binary_name,
        parsed.interfaces,
        _metadata_subset(parsed.metadata),
        members(parsed.fields),
        members(parsed.methods),
    )


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


__all__ = [
    "BuildWorldError",
    "BuildWorldSnapshot",
    "DependencyInput",
    "SourceRootInput",
    "canonical_fingerprint",
    "classify_diagnostics",
    "compare_class_outputs",
    "create_snapshot",
    "describe_source_root",
    "filter_self_outputs",
    "materialize_private_sources",
    "path_fingerprint",
    "sha256_file",
    "tree_fingerprint",
    "write_worker_classpath",
]
