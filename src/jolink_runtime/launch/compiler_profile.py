"""Build-system-neutral compiler argument decisions used by JDT products."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

_MEMORY_ARGUMENT = re.compile(r"-J-Xm(?P<kind>[sx])(?P<size>\d+)(?P<unit>[kKmMgG]?)")
_MAVEN_MEMORY = re.compile(r"(?P<size>\d+)(?P<unit>[kKmMgG]?)")

# These configure diagnostics, not Java generation. Keep unknown extensions
# and source-rewriting modes out of this group; do not discard all -Xplugin.
_ERROR_PRONE_CHECK_FLAGS = frozenset(
    {
        "-XepAllErrorsAsWarnings",
        "-XepAllSuggestionsAsWarnings",
        "-XepAllDisabledChecksAsWarnings",
        "-XepDisableAllChecks",
        "-XepDisableAllWarnings",
        "-XepDisableWarningsInGeneratedCode",
        "-XepIgnoreUnknownCheckNames",
        "-XepIgnoreSuppressionAnnotations",
        "-XepCompilingTestOnlyCode",
        "-XepCompilingPubliclyVisibleCode",
        "-XepPrintTimings",
        "-XepRecordTimings",
    }
)
_ERROR_PRONE_CHECK_PREFIXES = (
    "-Xep:",
    "-XepOpt:",
    "-XepExcludedPaths:",
    "-XepIgnoreLargeCodeGenerators:",
)
_JAVAC_CHECK_POLICIES = frozenset(
    {
        "-XDcompilePolicy=simple",
        "--should-stop=ifError=FLOW",
        "-XDshould-stop.ifError=FLOW",
        "-XDaddTypeAnnotationsToSymbol=true",
    }
)
_JAVAC_CHECK_ACCESS = re.compile(
    r"-J--add-(?:exports|opens)=jdk\.compiler/com\.sun\.tools\.javac(?:\.[A-Za-z0-9_]+)*=ALL-UNNAMED"
)


def _skipped_check_arguments(arguments: Sequence[str]) -> set[int]:
    invocations = {
        index: str(raw).split()[1:]
        for index, raw in enumerate(arguments)
        if str(raw).split()[:1] == ["-Xplugin:ErrorProne"]
    }
    if not invocations:
        return set()
    own_flags = [flag for flags in invocations.values() for flag in flags]
    own_flags.extend(
        str(raw).strip() for raw in arguments if str(raw).strip().startswith("-Xep")
    )
    if not all(
        flag in _ERROR_PRONE_CHECK_FLAGS or flag.startswith(_ERROR_PRONE_CHECK_PREFIXES)
        for flag in own_flags
    ):
        return set()
    return {
        index
        for index, raw in enumerate(arguments)
        if index in invocations
        or str(raw).strip() in _JAVAC_CHECK_POLICIES
        or _JAVAC_CHECK_ACCESS.fullmatch(str(raw).strip())
        or str(raw).strip() in _ERROR_PRONE_CHECK_FLAGS
        or str(raw).strip().startswith(_ERROR_PRONE_CHECK_PREFIXES)
    }


def skipped_compiler_checks(modules, *, include_tests: bool = False) -> tuple[str, ...]:
    names = []
    for module in modules:
        names.extend(module.get("skipped_compiler_checks", ()))
        if include_tests:
            names.extend(
                module.get("test_compiler", {}).get("skipped_compiler_checks", ())
            )
    return tuple(sorted(set(names)))


def compiler_check_warnings(checks: Sequence[str]) -> tuple[str, ...]:
    if not checks:
        return ()
    return (
        "JDT did not run compiler checks: "
        + ", ".join(checks)
        + ". Run the project's native build/CI for these checks.",
    )


@dataclass(frozen=True)
class CompilerArgumentDecision:
    argument: str
    disposition: str
    category: str


@dataclass(frozen=True)
class CompilerArgumentProfile:
    worker_min_heap_mb: int
    worker_max_heap_mb: int
    decisions: tuple[CompilerArgumentDecision, ...]
    method_parameters: bool = False
    processor_names: tuple[str, ...] = ()
    processor_options: dict[str, str | None] = field(default_factory=dict)

    @property
    def skipped_checks(self) -> tuple[str, ...]:
        return (
            ("Error Prone",)
            if any(item.category == "javac_check" for item in self.decisions)
            else ()
        )

    @property
    def arguments_without_checks(self) -> tuple[str, ...]:
        return tuple(
            item.argument
            for item in self.decisions
            if item.disposition != "SKIPPED_FOR_JDT"
        )

    @property
    def unresolved_arguments(self) -> tuple[str, ...]:
        return tuple(
            item.argument for item in self.decisions if item.disposition == "UNRESOLVED"
        )


def _memory_mb(size: int, unit: str) -> int:
    normalized = unit.casefold()
    if normalized == "k":
        return max(1, (size + 1023) // 1024)
    if normalized == "m":
        return max(1, size)
    if normalized == "g":
        return max(1, size * 1024)
    return max(1, (size + 1024 * 1024 - 1) // (1024 * 1024))


def parse_maven_memory_megabytes(value: str) -> int | None:
    match = _MAVEN_MEMORY.fullmatch(str(value).strip())
    if match is None:
        return None
    size = int(match.group("size"))
    unit = match.group("unit").casefold()
    if not unit or unit == "m":
        return max(1, size)
    if unit == "k":
        return max(1, (size + 1023) // 1024)
    return max(1, size * 1024)


def classify_compiler_arguments(
    arguments: Sequence[str],
    *,
    default_min_heap_mb: int = 64,
    default_max_heap_mb: int = 2048,
) -> CompilerArgumentProfile:
    min_heap = int(default_min_heap_mb)
    max_heap = int(default_max_heap_mb)
    decisions: list[CompilerArgumentDecision] = []
    method_parameters = False
    processor_names: tuple[str, ...] = ()
    processor_options: dict[str, str | None] = {}
    skipped = _skipped_check_arguments(arguments)
    index = 0
    while index < len(arguments):
        raw = arguments[index]
        argument = str(raw).strip()
        if index in skipped:
            decisions.append(
                CompilerArgumentDecision(argument, "SKIPPED_FOR_JDT", "javac_check")
            )
            index += 1
            continue
        if argument.startswith("-A") and len(argument) > 2:
            key, equals, value = str(raw).lstrip()[2:].partition("=")
            processor_options[key] = value if equals else None
            decisions.append(
                CompilerArgumentDecision(argument, "MAPPED_TO_JDT", "processor_option")
            )
            index += 1
            continue
        if argument == "-processor" and index + 1 < len(arguments):
            processor_names = tuple(str(arguments[index + 1]).split(","))
            decisions.extend(
                CompilerArgumentDecision(
                    str(value), "MAPPED_TO_JDT", "processor_selection"
                )
                for value in arguments[index : index + 2]
            )
            index += 2
            continue
        if argument in {"-Xlint", "-nowarn", "-deprecation"} or argument.startswith(
            "-Xlint:"
        ):
            decisions.append(
                CompilerArgumentDecision(
                    argument=argument,
                    disposition="REDUNDANT_FOR_JDT",
                    category="optional_diagnostics_disabled",
                )
            )
            index += 1
            continue
        if argument == "-proc:none":
            decisions.append(
                CompilerArgumentDecision(
                    argument=argument,
                    disposition="MAPPED_TO_JDT",
                    category="annotation_processing_disabled",
                )
            )
            index += 1
            continue
        if argument == "-Xpkginfo:always":
            decisions.append(
                CompilerArgumentDecision(
                    argument, "REDUNDANT_FOR_JDT", "package_info_always_generated"
                )
            )
            index += 1
            continue
        if (
            argument == "-sourcepath"
            and index + 1 < len(arguments)
            and str(arguments[index + 1]).strip().casefold()
            in {"doesnotexist", "does-not-exist"}
        ):
            value = str(arguments[index + 1]).strip()
            decisions.extend(
                (
                    CompilerArgumentDecision(
                        argument=argument,
                        disposition="REDUNDANT_FOR_JDT",
                        category="implicit_source_discovery_disabled",
                    ),
                    CompilerArgumentDecision(
                        argument=value,
                        disposition="REDUNDANT_FOR_JDT",
                        category="implicit_source_discovery_disabled",
                    ),
                )
            )
            index += 2
            continue
        if argument == "-parameters":
            method_parameters = True
            decisions.append(
                CompilerArgumentDecision(
                    argument=argument,
                    disposition="MAPPED_TO_JDT",
                    category="method_parameters",
                )
            )
            index += 1
            continue
        memory = _MEMORY_ARGUMENT.fullmatch(argument)
        if memory is None:
            decisions.append(
                CompilerArgumentDecision(
                    argument=argument,
                    disposition="UNRESOLVED",
                    category="compiler_extension",
                )
            )
            index += 1
            continue
        size_mb = _memory_mb(
            int(memory.group("size")),
            memory.group("unit"),
        )
        if memory.group("kind") == "s":
            min_heap = max(min_heap, min(8192, size_mb))
        else:
            max_heap = max(max_heap, min(8192, size_mb))
        decisions.append(
            CompilerArgumentDecision(
                argument=argument,
                disposition="MAPPED_TO_WORKER_JVM",
                category="compiler_process_memory",
            )
        )
        index += 1
    return CompilerArgumentProfile(
        worker_min_heap_mb=min_heap,
        worker_max_heap_mb=max_heap,
        decisions=tuple(decisions),
        method_parameters=method_parameters,
        processor_names=processor_names,
        processor_options=processor_options,
    )


__all__ = [
    "CompilerArgumentDecision",
    "CompilerArgumentProfile",
    "classify_compiler_arguments",
    "parse_maven_memory_megabytes",
]
