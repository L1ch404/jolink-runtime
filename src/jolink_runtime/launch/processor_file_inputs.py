"""Persist APT option files and observed source-path resource dependencies."""

from __future__ import annotations

import stat
from pathlib import Path


class ProcessorFileInputs:
    def __init__(self):
        self.paths: dict[Path, tuple[Path, ...]] = {}
        self.baseline: dict = {}
        self.observed: dict = {}

    def configure(self, scopes: dict[Path, dict]) -> None:
        # -A values are opaque strings. Only accept a whole absolute filesystem
        # path; do not guess relative bases, expand variables or scan directories.
        self.paths = {
            project: tuple(dict.fromkeys(Path(value) for value in options.values()
                                        if value and Path(value).is_absolute()))
            for project, options in scopes.items()
        }
        self.paths = {project: paths for project, paths in self.paths.items() if paths}

    @staticmethod
    def _stamp(path: Path):
        try:
            metadata = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            return None
        return [metadata.st_mtime_ns, metadata.st_size] if stat.S_ISREG(metadata.st_mode) else None

    def refresh(self, sources: dict[Path, Path]) -> set[Path]:
        # Keep resources actually read through Filer alongside declared -A
        # paths. Retain known reads when an incremental build visits only part
        # of a project; dropping those would lose other units' dependencies.
        paths = {project: dict.fromkeys(files) for project, files in self.baseline.items()}
        for project, configured in self.paths.items():
            paths.setdefault(str(project), {}).update(dict.fromkeys(str(path) for path in configured))
        self.observed = {project: {path: self._stamp(Path(path)) for path in files}
                         for project, files in paths.items()}
        changed = [Path(project) for project, inputs in self.observed.items()
                   if self.baseline.get(project) != inputs]
        if not changed:
            return set()
        return {source for source, private in sources.items()
                if any(private.is_relative_to(project) for project in changed)}

    def built(self, reads: dict | None = None) -> None:
        # Save what was observed BEFORE compilation, not the filesystem after
        # it. A file changed during the build must remain pending for next time.
        self.baseline = self.observed
        for project, files in (reads or {}).items():
            known = self.baseline.setdefault(project, {})
            for path, stamp in files.items():
                # New dependencies have native pre-lookup observations. For
                # known ones preserve the pre-build source-selection snapshot.
                known.setdefault(path, stamp)

    def snapshot(self) -> dict:
        return self.baseline

    def restore(self, saved: dict) -> None:
        self.baseline = saved
