"""Keep a cold Probe's known configuration baseline with the model it produced."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .configuration_inputs import build_configuration_stamps


class ProbeConfigurationChanged(RuntimeError):
    error_code = "BUILD_CONFIGURATION_CHANGED"
    retryable = True
    suggested_next_step = "Wait for build configuration edits to finish, then retry the same request."

    def __init__(self, changed_files):
        super().__init__("Build configuration changed while the Probe was running.")
        self.context = {
            "retryable": self.retryable,
            "changed_configuration_files": list(changed_files),
            "suggested_next_step": self.suggested_next_step,
        }


@dataclass(frozen=True)
class ProbeConfiguration:
    project: Path
    build_system: str
    inputs: tuple[Path, ...]
    _before: dict[str, str] = field(repr=False)

    @classmethod
    def capture(cls, project, build_system, inputs):
        project = Path(project).resolve(strict=False)
        inputs = tuple(dict.fromkeys(Path(path).resolve(strict=False) for path in inputs))
        return cls(project, build_system, inputs,
                   build_configuration_stamps(project, build_system, inputs))

    def check(self):
        current = build_configuration_stamps(self.project, self.build_system, self.inputs)
        changed = sorted(path for path in self._before.keys() | current.keys()
                         if self._before.get(path) != current.get(path))
        if changed:
            raise ProbeConfigurationChanged(changed)

    def finish(self, final_inputs):
        """Validate known inputs and retain that baseline, not a later sample.

        A cold Probe can discover inputs not known before it ran. Those receive
        a post-Probe baseline here; this does not protect their earlier read
        window. Inputs removed from the final model are not kept indefinitely.
        """
        self.check()
        final = build_configuration_stamps(self.project, self.build_system, final_inputs)
        final.update((path, value) for path, value in self._before.items() if path in final)
        return final


def recorded_configuration_inputs(cache_path, key):
    """Use an older cache's path inventory, never its model or recorded values."""
    try:
        raw = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        values = raw.get(key) if isinstance(raw, dict) else None
        if not isinstance(values, dict):
            return ()
        return tuple(Path(name) for name in values if Path(name).is_absolute())
    except (OSError, ValueError):
        return ()
