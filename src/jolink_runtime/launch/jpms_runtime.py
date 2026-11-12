"""Use the compiler's module facts for application and test JVM paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModuleRuntime:
    name: str | None
    classpath: tuple[Path, ...]
    module_path: tuple[Path, ...] = ()
    arguments: tuple[str, ...] = ()


def module_runtime(compiler, classpath, *, resources=(), tests=False, enabled=True, test_access=True):
    entries = tuple(dict.fromkeys(Path(p) for p in classpath))
    if not enabled:
        return ModuleRuntime(None, entries)
    metadata = getattr(compiler, "module_metadata", {})
    if not metadata:
        return ModuleRuntime(None, entries)
    scopes = compiler.module_runtime_scopes()
    target = next((key for key, value in scopes.items()
                   if value["output"] == compiler.output_directory), None)
    name = metadata.get(target, {}).get("module_name")
    if not name:
        return ModuleRuntime(None, entries)
    modular_entries = {Path(p) for scope in scopes.values() for p in scope["module_path"]}
    patches = {}
    excluded = set()
    for key, scope in scopes.items():
        info = metadata.get(key, {})
        own_name = info.get("module_name")
        patched = info.get("patched_module_name") if tests else None
        if own_name and not patched:
            modular_entries.add(scope["output"])
        owner = patched or own_name
        if owner and (tests or scope["scope"] != "test"):
            paths = list(scope["resources"])
            if key == target:
                paths.extend(Path(p) for p in resources)
            if patched:
                paths.insert(0, scope["output"])
            if paths:
                patches.setdefault(owner, []).extend(paths)
                excluded.update(paths)
    module_path = tuple(p for p in entries if p in modular_entries and p not in excluded)
    cp = tuple(p for p in entries if p not in modular_entries and p not in excluded)
    arguments = []
    for owner, paths in patches.items():
        arguments.extend(("--patch-module", owner + "=" + os.pathsep.join(str(p) for p in dict.fromkeys(paths))))
    if tests and test_access:
        arguments.extend(("--add-modules", name))
        for key, scope in scopes.items():
            owner = metadata.get(key, {}).get("patched_module_name")
            if not owner:
                continue
            arguments.extend(("--add-reads", owner + "=ALL-UNNAMED"))
            source_root = scope["output"].parent / "src"
            packages = {p.parent.relative_to(source_root).as_posix().replace("/", ".")
                        for p in compiler._source_map.values() if p.is_relative_to(source_root)}
            for package in sorted(packages - {"."}):
                arguments.extend(("--add-opens", owner + "/" + package + "=ALL-UNNAMED"))
    return ModuleRuntime(name, cp, module_path, tuple(arguments))


def module_vm_arguments(arguments, layout, compiler, *, directory, outputs=None):
    """Map native output paths and merge generated resource/test patches once."""
    mapping = {str(Path(k).resolve()): Path(v) for k, v in getattr(compiler, "_outputs", {}).items()}
    mapping.update({str(Path(k).resolve()): Path(v) for k, v in (outputs or {}).items()})
    module_paths = list(layout.module_path)
    patches = {}
    kept = []

    def paths(value):
        result = []
        for raw in value.split(os.pathsep):
            if not raw:
                continue
            path = Path(raw)
            if not path.is_absolute():
                path = Path(directory) / path
            result.append(mapping.get(str(path.resolve()), path))
        return result

    values = list(arguments) + list(layout.arguments)
    index = 0
    while index < len(values):
        value = values[index]
        key, separator, inline = value.partition("=")
        if key not in {"--module-path", "-p", "--patch-module"}:
            kept.append(value)
            index += 1
            continue
        if separator:
            argument = inline
            index += 1
        else:
            if index + 1 == len(values):
                kept.append(value)  # Let the JVM diagnose a missing native argument.
                break
            argument = values[index + 1]
            index += 2
        if key in {"--module-path", "-p"}:
            module_paths.extend(paths(argument))
        else:
            if "=" not in argument:
                kept.extend(("--patch-module", argument))
                continue
            name, _, raw_paths = argument.partition("=")
            patches.setdefault(name, []).extend(paths(raw_paths))
    for name, entries in patches.items():
        kept.extend(("--patch-module", name + "=" + os.pathsep.join(str(p) for p in dict.fromkeys(entries))))
    return tuple(kept), tuple(dict.fromkeys(module_paths))
