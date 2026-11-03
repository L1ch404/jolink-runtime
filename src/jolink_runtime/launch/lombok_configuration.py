"""Track Lombok's native configuration results beside the existing source index."""

from __future__ import annotations

import base64
from pathlib import Path


class LombokConfigurationInputs:
    def __init__(self, root: Path, *, enabled: bool):
        self.root = root
        self.enabled = enabled
        self.groups: dict[str, dict] = {}
        self.stamps: dict[str, list[int] | None] = {}
        self.dirty: set[Path] = set()
        self._mapped: dict[Path, Path] | None = None

    @staticmethod
    def _stamp(path: str) -> list[int] | None:
        try:
            stat = Path(path).stat()
            return [stat.st_mtime_ns, stat.st_size]
        except FileNotFoundError:
            return None

    def restore(self, value: dict) -> None:
        self.groups = value.get("groups", {})
        self.stamps = value.get("stamps", {})

    def snapshot(self) -> dict:
        return {"groups": self.groups, "stamps": self.stamps}

    def refresh(self, client, sources: dict[Path, Path], *, force: bool = False) -> set[Path]:
        if not self.enabled:
            return set()
        # The existing Java change scan already stats source files. Only inspect
        # configuration inputs here, rather than doubling that hot-path I/O.
        mapped = dict(sources)
        if (not force and mapped == self._mapped
                and all(self._stamp(path) == stamp for path, stamp in self.stamps.items())):
            return self.dirty

        path = self.root / "lombok-source-origins.txt"
        encode = lambda value: base64.b64encode(str(value).encode("utf-8")).decode("ascii")
        if mapped != self._mapped:
            path.write_text("".join(
                encode(private) + "\t" + encode(source) + "\n"
                for source, private in sorted(mapped.items(), key=lambda item: str(item[0]))
            ), encoding="utf-8")
        result = client.command("LOMBOK_CONFIG\t" + encode(path))
        if result.get("ok") is not True:
            from .jdt_compile_session import JdtCompileError
            raise JdtCompileError("LOMBOK_CONFIGURATION_FAILED", result.get("message", "Lombok configuration could not be resolved."))
        groups = {group["directory"]: group for group in result["groups"]}
        changed = {directory for directory, group in groups.items()
                   if self.groups.get(directory, {}).get("fingerprint") != group["fingerprint"]}
        self.dirty.intersection_update(mapped)
        self.dirty.update(source for source in mapped if str(source.parent) in changed)
        self.groups = groups
        self.stamps = {input_path: self._stamp(input_path)
                       for group in groups.values() for input_path in group["inputs"]}
        self._mapped = mapped
        return self.dirty

    def built(self) -> None:
        self.dirty.clear()
