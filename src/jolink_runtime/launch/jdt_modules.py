"""Multiple module projects sharing one persistent JavaBuilder workspace."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .jdt_compile_session import PersistentJdtCompileSession, discover_target_system_entries


def module_name(directory: str | Path) -> str:
    return "module_" + hashlib.sha256(os.path.normcase(str(Path(directory))).encode()).hexdigest()[:12]


class ModuleCompileSession(PersistentJdtCompileSession):
    def __init__(self, *, modules: tuple[dict, ...], target_module: Path, **kwargs):
        super().__init__(**kwargs)
        self.modules = modules
        self.target_name = module_name(target_module)
        self.private_project = self.root / "workspace"
        self.output_directory = self.private_project / self.target_name / "bin"
        self.test_output_directory = self.private_project / self.target_name / "test-bin"
        self._module_destinations = {}
        self._outputs = {}
        for module in modules:
            name = module_name(module["module_root"])
            destination = self.private_project / name
            for field, leaf in (("source_roots", "src"), ("test_source_roots", "test-src")):
                for source in module.get(field, ()):
                    self._module_destinations[Path(source)] = destination / leaf
            self._outputs[str(Path(module["output_directory"]))] = destination / "bin"
            self._outputs[str(Path(module["test_output_directory"]))] = destination / "test-bin"

    def _materialize_sources(self):
        for source, destination in self._module_destinations.items():
            if not source.is_dir():
                continue
            # _materialize_source_group also records the original -> mirror index.
            if destination.exists():
                import shutil
                for item in source.rglob("*.java"):
                    copied = destination / item.relative_to(source)
                    copied.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(item, copied)
                    self._remember_source(item.resolve(), copied)
            else:
                self._materialize_source_group(source_roots=(source,), baseline_roots=(source,), destination_root=destination)
        for module in self.modules:
            (self.private_project / module_name(module["module_root"]) / "src").mkdir(parents=True, exist_ok=True)

    def _private_path_for_workspace_source(self, source):
        for root, destination in self._module_destinations.items():
            if source.is_relative_to(root):
                return destination / source.relative_to(root)
        return None

    def workspace_source_roots(self):
        return tuple(self._module_destinations)

    def class_file(self, relative):
        name, path = relative.split("/", 1)
        return path, self.private_project / name / "bin" / path

    def runtime_classpath(self, entries):
        result = []
        resources = {str(Path(m["output_directory"])): m.get("resource_roots", ()) for m in self.modules}
        for entry in entries:
            output = self._outputs.get(str(Path(entry)))
            result.append(output if output is not None else Path(entry))
            result.extend(Path(p) for p in resources.get(str(Path(entry)), ()) if Path(p).is_dir())
        return tuple(dict.fromkeys(result))

    def _prepare_worker_command(self):
        # Reuse the existing JVM command/configuration materialization once.
        command = super()._prepare_worker_command()
        properties = []
        names = {str(Path(m["output_directory"])): module_name(m["module_root"]) for m in self.modules}
        test_outputs = {str(Path(m["test_output_directory"])): module_name(m["module_root"]) for m in self.modules}
        properties.append("modules=" + ",".join(module_name(m["module_root"]) for m in self.modules))
        for module in self.modules:
            name = module_name(module["module_root"])
            def write_paths(suffix, paths):
                path = self.root / (name + suffix)
                path.write_text("".join(str(item)+"\n" for item in paths), encoding="utf-8")
                return path.as_posix()
            entries = list(discover_target_system_entries(Path(module["target_java_home"]), module["source_level"]))
            for entry in module["classpath"]:
                local = names.get(str(Path(entry)))
                entries.append("project:"+local if local else entry)
            prefix = name + "."
            properties.extend((prefix+"classpath="+write_paths(".classpath.txt", entries),
                prefix+"encoding="+module["source_encoding"],
                prefix+"level="+str(module["source_level"]),
                prefix+"parameters="+str(module.get("method_parameters",False)).lower()))
            if module.get("processor_entries"):
                properties.append(prefix+"processors="+write_paths(".processors.txt", module["processor_entries"]))
            if module.get("test_source_roots"):
                test_entries = ["project-tests:"+test_outputs[str(Path(p))] if str(Path(p)) in test_outputs
                    else "project:"+names[str(Path(p))] if str(Path(p)) in names else p for p in module.get("test_classpath", ())]
                properties.append(prefix+"tests="+write_paths(".tests.txt", test_entries))
        spec = self.root / "modules.properties"
        spec.write_text("\n".join(properties)+"\n", encoding="utf-8")
        return [*command, "--modules-file", str(spec)]
