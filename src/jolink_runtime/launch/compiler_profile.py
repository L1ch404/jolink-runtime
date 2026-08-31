"""Build-system-neutral compiler argument decisions used by JDT products."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence


_MEMORY_ARGUMENT = re.compile(
    r"-J-Xm(?P<kind>[sx])(?P<size>\d+)(?P<unit>[kKmMgG]?)"
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


def classify_compiler_arguments(
    arguments: Sequence[str],
    *,
    default_min_heap_mb: int = 64,
    default_max_heap_mb: int = 2048,
) -> CompilerArgumentProfile:
    min_heap = int(default_min_heap_mb)
    max_heap = int(default_max_heap_mb)
    decisions: list[CompilerArgumentDecision] = []
    for raw in arguments:
        argument = str(raw).strip()
        memory = _MEMORY_ARGUMENT.fullmatch(argument)
        if memory is None:
            decisions.append(
                CompilerArgumentDecision(
                    argument=argument,
                    disposition="UNRESOLVED",
                    category="compiler_extension",
                )
            )
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
    return CompilerArgumentProfile(
        worker_min_heap_mb=min_heap,
        worker_max_heap_mb=max_heap,
        decisions=tuple(decisions),
    )


__all__ = [
    "CompilerArgumentDecision",
    "CompilerArgumentProfile",
    "classify_compiler_arguments",
]
