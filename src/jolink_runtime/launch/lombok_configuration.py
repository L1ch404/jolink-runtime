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
        self.generated_roots: dict[Path, Path] = {}

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
        # Generated directories are known before their files exist. Preserve
        # discovered subdirectory scopes when rebinding after a Worker restart.
        if mapped != self._mapped or self.generated_roots:
            path.write_text("".join(
                encode(private) + "\t" + encode(source) + "\n"
                for source, private in sorted(mapped.items(), key=lambda item: str(item[0]))
            ) + "".join("root\t" + encode(private) + "\t" + encode(original) + "\n"
                        for private, original in self.generated_roots.items())
              + "".join("directory\t" + encode(directory) + "\n" for directory in self.groups
                        if any(Path(directory).is_relative_to(root) for root in self.generated_roots.values())),
                encoding="utf-8")
        result = client.command("LOMBOK_CONFIG\t" + encode(path))
        groups = self._groups(result)
        changed = {directory for directory, group in groups.items()
                   if self.groups.get(directory, {}).get("fingerprint") != group["fingerprint"]}
        self.dirty.intersection_update(mapped)
        self.dirty.update(source for source in mapped if str(source.parent) in changed)
        # A generated scope belongs to a JDT project, not necessarily to the
        # handwritten file that caused generation. Re-run that project's inputs.
        affected_projects = {private.parent for private, original in self.generated_roots.items()
                             if any(Path(directory).is_relative_to(original) for directory in changed)}
        self.dirty.update(source for source, private in mapped.items()
                          if any(private.is_relative_to(project) for project in affected_projects))
        self._remember(groups)
        self._mapped = mapped
        return self.dirty

    @staticmethod
    def _groups(result):
        if result.get("ok") is not True:
            from .jdt_compile_session import JdtCompileError
            raise JdtCompileError("LOMBOK_CONFIGURATION_FAILED", result.get("message", "Lombok configuration could not be resolved."))
        return {group["directory"]: group for group in result["groups"]}

    def _remember(self, groups):
        self.groups = groups
        self.stamps = {input_path: self._stamp(input_path)
                       for group in groups.values() for input_path in group["inputs"]}

    def built(self, client=None) -> None:
        if self.enabled and self.generated_roots:
            self._remember(self._groups(client.command("LOMBOK_CONFIG_STATE")))
        self.dirty.clear()
