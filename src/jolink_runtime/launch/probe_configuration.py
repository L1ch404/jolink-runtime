"""Keep a cold Probe's known configuration baseline with the model it produced."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
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

    def check(self, *, native_sources=()):
        current = build_configuration_stamps(self.project, self.build_system, self.inputs)
        changed = sorted(path for path in self._before.keys() | current.keys()
                         if not any(Path(path).is_relative_to(root) for root in native_sources)
                         and self._before.get(path) != current.get(path))
        if changed:
            raise ProbeConfigurationChanged(changed)

    def finish(self, final_inputs, *, native=()):
        """Keep the original Python/native observations with their resolved model.

        Native Probes supply input observations unavailable before discovery.
        Final-only inputs without such evidence still have a post-Probe baseline,
        not a claim of an atomic snapshot across arbitrary build extensions.
        """
        observed = {}
        changed = set()
        source_roots = []
        for model in native:
            changed.update(model.get("configurationChanges", ()))
            source_roots.extend(Path(path) for path in model.get("configurationSourceDirectories", ()))
            for path, stamp in model.get("configurationStamps", {}).items():
                if path in observed and observed[path] != stamp:
                    changed.add(path)
                observed.setdefault(path, stamp)
        # Config scripts can generate build-logic SourceSet inputs before their
        # native compiler consumes them. All other known config keeps the
        # pre-invocation check too (e.g. settings copied before Probe startup).
        self.check(native_sources=source_roots)
        final_inputs = (*final_inputs, *(Path(path) for path in observed))
        final = build_configuration_stamps(self.project, self.build_system, final_inputs)
        changed.update(path for path, stamp in observed.items() if final.get(path) != stamp)
        directories = [Path(path) for path, stamp in observed.items() if stamp == "directory"]
        if directories:
            def files(values):
                return {Path(path).resolve(strict=False) for path, stamp in values.items()
                        if stamp not in {"directory", "missing"}}
            changed.update(str(path) for path in files(observed) ^ files(final)
                           if any(path.is_relative_to(root) for root in directories))
        if changed:
            raise ProbeConfigurationChanged(sorted(changed))
        final.update(observed)
        final.update((path, value) for path, value in self._before.items() if path in final
                     and not any(Path(path).is_relative_to(root) for root in source_roots))
        return final

    def bind(self, model, *, native=()):
        """Both launch and tests retain newly discovered paths and their baseline."""
        native = tuple(native)
        inputs = tuple(dict.fromkeys((*model.configuration_inputs,
            *(Path(path) for item in native for path in item.get("configurationStamps", {})))))
        return replace(model, configuration_inputs=inputs,
                       configuration_stamps=self.finish(inputs, native=native))


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
