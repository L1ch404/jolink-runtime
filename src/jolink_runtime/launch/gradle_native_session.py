"""Run Gradle's configured compiler tasks."""
import os
from pathlib import Path

from .native_compile_session import NativeCompileSession


class GradleNativeSession(NativeCompileSession):
    strategy = "gradle_native"
    operation_name = "gradle_joint_compile"

    def __init__(self, root, modules, target_module, build_java_home):
        super().__init__(root, modules, target_module, build_java_home)
        self._scopes = {}
        for module in modules:
            for label, settings in (("main", module), ("test", module.get("test_compiler"))):
                if not settings:
                    continue
                output = Path(module["output_directory" if label == "main" else "test_output_directory"])
                self._scopes[str(output)] = dict(
                    output=self._outputs[output], scope=label, module_path=self.runtime_classpath(settings.get("module_path", ())),
                    resources=self.runtime_classpath(module["native_compile"].get(label + "_patches", ())),
                    module_name=settings.get("module_name"), patched_module_name=settings.get("patch_module"),
                )

    @property
    def module_metadata(self):
        # A versioned descriptor inside an exploded classpath directory is not
        # a named module. Only a root descriptor belongs on the module path.
        return {key: dict(module_name=scope["module_name"] if (scope["output"] / "module-info.class").is_file() else None,
                          patched_module_name=scope["patched_module_name"])
                for key, scope in self._scopes.items() if scope["module_name"] or scope["patched_module_name"]}

    def module_runtime_scopes(self):
        return self._scopes

    def output_packages(self, output):
        prefix = output.name + "/"
        return {p[len(prefix):].rsplit("/", 1)[0].replace("/", ".") for p in self.digests
                if p.startswith(prefix) and p.endswith(".class") and "/" in p[len(prefix):]
                and not p[len(prefix):].startswith("META-INF/")}

    def capture_runtime_module_packages(self):
        result = {}
        for k, info in self.module_metadata.items():
            if info["module_name"]:
                scope = self._scopes[k]
                roots = (scope["output"], *scope["resources"])
                packages = tuple(sorted(set().union(*(self.output_packages(root) for root in roots))))
                result.update((root, packages) for root in roots)
        return result

    def _command(self, inputs):
        wrapper = self.directory / ("gradlew.bat" if os.name == "nt" else "gradlew")
        args = [str(wrapper), "--console=plain", "--no-daemon"]
        if {"--offline", "-o"}.intersection(os.environ.get("GRADLE_ARGS", "").split()):
            args.append("--offline")
        return (*args, *self.tasks)
