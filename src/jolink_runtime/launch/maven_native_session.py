"""Maven owns compiler/plugin ordering for scopes that cannot use JDT outputs."""
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET

from .native_compile_session import NativeCompileSession
from .maven_probe import ProductMavenProbe


def attach_maven_native(modules, *, target, build_root, maven_argv, settings, local_repository, offline, profiles, tests):
    if not any(m.get("native_main_compile") or (tests and (m.get("native_test_compile") or m.get("native_test_phase"))) for m in modules):
        return
    for m in modules:
        # Maven will run the actual compiler configuration, including checks
        # that would have been skipped on the JDT path.
        m["skipped_compiler_checks"] = []
        if tests and "test_compiler" in m:
            m["test_compiler"]["skipped_compiler_checks"] = []
        outputs = [m["output_directory"]]
        if tests and "test_compiler" in m:
            outputs.append(m["test_output_directory"])
        m["native_compile"] = {
            "build_root": str(build_root), "outputs": outputs,
            "resource_inputs": m.get("post_test_inputs", ()),
        }
        if Path(m["module_root"]) == target:
            m["native_compile"]["maven"] = {
                "command": list(maven_argv), "settings": str(settings) if settings else None,
                "profiles": list(profiles), "repository": str(local_repository), "offline": offline,
                "scope": "test" if tests else "runtime",
            }


class MavenNativeSession(NativeCompileSession):
    strategy = "maven_native"
    operation_name = "maven_native_compile"
    source_suffixes = (".java",)

    def __init__(self, root, modules, target_module, build_java_home):
        super().__init__(root, modules, target_module, build_java_home)
        self.modules, self.target = modules, target_module
        self.options = next(m["native_compile"]["maven"] for m in modules if Path(m["module_root"]) == target_module)
        self.product_files = {p for m in modules for p in m.get("post_test_products", ())}
        self.aliases = {}
        for m in modules:
            for test in ("", "test_"):
                for resource in m.get(test + "resource_roots", ()):
                    self.aliases[Path(resource)] = Path(m[test + "output_directory"])

    def runtime_classpath(self, entries):
        return super().runtime_classpath(self.aliases.get(Path(p), p) for p in entries)

    def _committed_inputs(self, before):
        # Tests may open a generated file relative to the project directory
        # instead of using their private classpath. Observe only named files,
        # not every generated class or an entire target tree.
        result = dict(before)
        for name in self.product_files:
            path = Path(name)
            if path.is_file():
                st = path.stat()
                result[name] = [st.st_mtime_ns, st.st_size]
            else:
                result.pop(name, None)
        return result

    def _inputs(self):
        return self._committed_inputs(super()._inputs())

    def _command(self):
        plan = self.options
        directory = self._attempt_directory
        probe = ProductMavenProbe.load().prepare(attempt_directory=directory,
            source_settings=Path(plan["settings"]) if plan["settings"] else None,
            local_repository=Path(plan["repository"]), offline=plan["offline"])
        probe.output_directory.mkdir(parents=True, exist_ok=True)
        manifest = ET.Element("modules")
        for m in self.modules:
            module = ET.SubElement(manifest, "module", root=m["module_root"])
            for key, tag in (("source_roots","main"), ("test_source_roots","test"), ("post_test_executions","post")):
                for value in m.get(key, ()):
                    ET.SubElement(module, tag).text = value
        ET.ElementTree(manifest).write(probe.output_directory/"compile-inputs.xml", encoding="utf-8")
        return (*plan["command"], "--batch-mode", "-f", str(self.directory/"pom.xml"), "-s", str(probe.settings_file),
                "-Dmaven.repo.local=" + plan["repository"], *(("--offline",) if plan["offline"] else ()),
                *(("-P", ",".join(plan["profiles"])) if plan["profiles"] else ()),
                probe.goal.replace("export-build-world", "export-reactor-world"), "-Djolink.probe.nativeCompile=true",
                "-Djolink.probe.scope=" + plan["scope"], "-Djolink.probe.targetDirectory=" + str(self.target),
                "-Djolink.probe.outputDirectory=" + str(probe.output_directory))

    def _build(self, run_operation=None):
        with TemporaryDirectory(prefix="native-maven-", dir=self.root) as directory:
            self._attempt_directory = Path(directory)
            return super()._build(run_operation)
