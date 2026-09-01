"""Build-system-neutral compiler argument decisions used by JDT products."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence


_MEMORY_ARGUMENT = re.compile(
    r"-J-Xm(?P<kind>[sx])(?P<size>\d+)(?P<unit>[kKmMgG]?)"
)
_MAVEN_MEMORY = re.compile(r"(?P<size>\d+)(?P<unit>[kKmMgG]?)")


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

    @property
    def unresolved_arguments(self) -> tuple[str, ...]:
        return tuple(
            item.argument
            for item in self.decisions
            if item.disposition == "UNRESOLVED"
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
    index = 0
    while index < len(arguments):
        raw = arguments[index]
        argument = str(raw).strip()
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
    )


__all__ = [
    "CompilerArgumentDecision",
    "CompilerArgumentProfile",
    "classify_compiler_arguments",
    "parse_maven_memory_megabytes",
]
