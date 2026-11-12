"""Native module facts and explicit compiler flags, shared by both build systems."""

import os
from pathlib import Path


def module_configuration(facts, profile, *, basedir):
    def path(value):
        value = Path(value)
        return str((value if value.is_absolute() else Path(basedir) / value).resolve(strict=False))
    patch = facts.get("patchModule")
    patch_paths = []
    if profile.patch_modules:
        name, _, values = profile.patch_modules[0].partition("=")
        patch = name
        patch_paths = [path(p) for p in values.split(os.pathsep) if p]
    return {
        "module_descriptor": facts.get("moduleDescriptor"),
        "module_name": facts.get("moduleName"),
        "module_path": [path(p) for p in (profile.module_path if profile.module_path is not None else facts.get("modulePath", ()))],
        "patch_module": patch,
        "patch_paths": patch_paths,
        "add_reads": list(dict.fromkeys((*facts.get("addReads", ()), *profile.add_reads))),
        "add_exports": list(profile.add_exports),
    }


def descriptor_inputs(modules):
    return tuple(dict.fromkeys(Path(root) / "module-info.java" for module in modules
                              for field in ("source_roots", "test_source_roots")
                              for root in module.get(field, ())))


def main_module_fields(module):
    return {"module_name": module.get("module_name"),
            "module_descriptor": Path(module["module_descriptor"]) if module.get("module_descriptor") else None,
            "module_path": tuple(Path(p) for p in module.get("module_path", ())),
            "patch_module": module.get("patch_module"),
            "add_reads": tuple(module.get("add_reads", ())),
            "add_exports": tuple(module.get("add_exports", ()))}


def unrepresented_patches(profile, roots, basedir):
    allowed = {Path(p).resolve(strict=False) for p in roots}
    if len(profile.patch_modules) > 1:
        return profile.patch_modules
    unsupported = []
    for patch in profile.patch_modules:
        name, separator, paths = patch.partition("=")
        values = paths.split(os.pathsep)
        if not separator or not name or any(not p or (Path(p) if Path(p).is_absolute() else Path(basedir) / p).resolve(strict=False) not in allowed for p in values):
            unsupported.append(patch)
    return tuple(unsupported)
