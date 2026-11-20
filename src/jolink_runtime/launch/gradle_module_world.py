"""Translate resolved Gradle projects into the shared persistent JDT model."""

from __future__ import annotations

from pathlib import Path

from .compiler_profile import classify_compiler_arguments
from .jdt_compile_session import JdtCompileError, select_target_system_home
from .module_configuration import module_configuration, unrepresented_patches


class GradleModuleError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.error_code = code


LANGUAGE_SUFFIXES = (".java", ".groovy", ".kt", ".scala")


def _foreign_inputs(source, task):
    """Only consider additional compilers in the selected project/source-set graph."""
    if not source.get("classesTask") or not (set(source["classesDirectories"]) - {task["destinationDirectory"]}):
        return {}
    java = set(source["javaSourceDirectories"])
    return {p: LANGUAGE_SUFFIXES[1:] if p in java else LANGUAGE_SUFFIXES
            for p in source["languageRoots"]}


def mixed_sources_present(candidates):
    return any(path.suffix in suffixes for root, suffixes in candidates.items()
               for path in Path(root).rglob("*") if path.is_file())


def dormant_language_roots(modules):
    return next((m["dormant_language_roots"] for m in modules if m.get("dormant_language_roots")), {})


def _compiler_settings(task: dict, *, native=False) -> dict:
    from .gradle_runtime_build_world import _processor_facts

    args = task["compilerArgsPrivate"]
    level = int(str(task.get("release") or task["targetCompatibility"]).removeprefix("1."))
    if native:
        # Gradle evaluates the actual toolchains, compiler plugins and APT; do
        # not translate them into ECJ options or claim checks were skipped.
        jpms = task.get("jpms", {})
        if jpms.get("moduleName") or jpms.get("patchModule"):
            raise GradleModuleError("GRADLE_MIXED_JPMS_UNSUPPORTED", "JPMS execution with native Gradle compilation is not yet modeled.")
        # Prefer the project's target for an implicit application JVM, but do
        # not require a separate platform installation to run native Gradle.
        home = Path(task["compilerJavaHome"])
        try:
            home = select_target_system_home((home,), level)
        except JdtCompileError as error:
            if error.error_code not in {"JDT_TARGET_PLATFORM_UNAVAILABLE", "JDT_TARGET_PLATFORM_UNSUPPORTED"}:
                raise
        return dict(module_path=[], source_encoding=task["encoding"], source_level=level,
                    method_parameters="-parameters" in args,
                    target_java_home=str(home),
                    processor_entries=[], lombok_entries=[])
    profile = classify_compiler_arguments(args)
    if profile.unresolved_arguments or task.get("compilerArgumentProvidersUnmodeled"):
        raise GradleModuleError(
            "GRADLE_COMPILE_CONFIGURATION_UNMODELED",
            "Gradle compiler arguments cannot be mapped to JDT.",
        )
    processors, lombok = [], []
    for path in task["annotationProcessorPath"] if "-proc:none" not in args else []:
        _, is_lombok = _processor_facts(Path(path))
        if is_lombok:
            lombok.append(path)
        if not is_lombok or profile.processor_names:
            processors.append(path)
    return {
        **module_configuration(task.get("jpms", {}), profile, basedir=task.get("projectDirectory", ".")),
        "skipped_compiler_checks": list(profile.skipped_checks),
        "source_level": level,
        "generated_source_directory": task.get("generatedSourceOutputDirectory"),
        "processor_source_roots": task.get("sourcePath"),
        "source_encoding": task["encoding"],
        "target_java_home": str(
            select_target_system_home((Path(task["compilerJavaHome"]),), level)
        ),
        "method_parameters": profile.method_parameters,
        "processor_entries": processors,
        "lombok_entries": lombok,
        "processor_names": list(profile.processor_names),
        "processor_options": profile.processor_options,
    }


def module_world(model: dict) -> tuple[tuple[dict, ...], dict, tuple[Path, ...]]:
    facts = {item["projectDirectory"]: item for item in model["modules"]}
    selected = model["projectDirectory"]
    candidates = {}
    for item in facts.values():
        for scope, task in (("main", "compileJava"), ("test", "compileTestJava")):
            if task in item:
                candidates.update(_foreign_inputs(item[scope], item[task]))
    native = any(m[scope].get("extraOutputDirs") or m[task].get("customCompileActions")
                 for m in facts.values() for scope, task in (("main", "compileJava"), ("test", "compileTestJava"))
                 if task in m) or mixed_sources_present(candidates)
    output_sources = {}
    for item in facts.values():
        for source in (item["main"], item.get("test", {})):
            if source.get("resourcesDirectory"):
                output_sources[source["resourcesDirectory"]] = source.get(
                    "preparedResourceDirectories", source["resourceDirectories"]
                )

    def classpath(entries, *, runtime=False):
        paths = []
        for raw in entries:
            artifact = model.get("projectArtifacts", {}).get(raw)
            if artifact:
                producer = facts[artifact["projectDirectory"]]
                if native and artifact["kind"] == "jar":
                    paths.extend(producer["main"]["classesDirectories"])
                elif native and artifact["kind"] in {"classes", "extra"}:
                    paths.append(raw)
                elif artifact["kind"] != "resources":
                    paths.append(producer["compileJava"]["destinationDirectory"])
                if runtime and artifact["kind"] in {"jar", "resources"}:
                    paths.extend(
                        p
                        for p in producer["main"].get("preparedResourceDirectories", producer["main"]["resourceDirectories"])
                        if native or Path(p).is_dir()
                    )
                if native and artifact["kind"] == "jar":
                    paths.extend(producer["main"].get("extraOutputDirs", ()))
            elif raw in output_sources:
                if runtime:
                    paths.extend(p for p in output_sources[raw] if native or Path(p).is_dir())
            else:
                paths.append(raw)
        return list(dict.fromkeys(paths))

    modules = []
    for item in facts.values():
        main = item["main"]
        compile = item["compileJava"]
        test = item.get("compileTestJava")
        settings = _compiler_settings(compile, native=native)
        for sources in (main, item.get("test", {})):
            if not native and any(sources.get(field) for field in ("javaIncludes", "javaExcludes")):
                raise GradleModuleError(
                    "GRADLE_SOURCE_PATTERN_UNMODELED",
                    "Gradle Java source include/exclude patterns are not mapped to the JDT source index.",
                )
        output = compile["destinationDirectory"]
        test_output = (
            test["destinationDirectory"]
            if test
            else str(Path(item["projectDirectory"]) / "build/classes/java/test")
        )
        dependencies = classpath(compile["classpath"])
        allowed_patches = [*main["javaSourceDirectories"], *item.get("test", {}).get("javaSourceDirectories", ()), output, test_output]
        for task in (compile, test):
            if not native and task and unrepresented_patches(classify_compiler_arguments(task["compilerArgsPrivate"]), allowed_patches, item["projectDirectory"]):
                raise GradleModuleError("GRADLE_COMPILE_CONFIGURATION_UNMODELED", "External --patch-module inputs are not mapped to the JDT project.")
        settings["module_path"] = classpath(settings["module_path"])
        modules.append(
            {
                "module_root": item["projectDirectory"],
                "artifact_aliases": [path for path, artifact in model.get("projectArtifacts", {}).items()
                                     if artifact["projectDirectory"] == item["projectDirectory"]
                                     and artifact["kind"] != "resources"],
                "source_roots": main["javaSourceDirectories"],
                "test_source_roots": item["test"]["javaSourceDirectories"]
                if test
                else [],
                "output_directory": output,
                "test_output_directory": test_output,
                "classpath": dependencies,
                "test_classpath": [
                    p
                    for p in classpath(test["classpath"])
                    if p not in {output, test_output} and p not in dependencies
                ]
                if test
                else [],
                "resource_roots": main.get("preparedResourceDirectories", main["resourceDirectories"]),
                "test_resource_roots": item.get("test", {}).get("preparedResourceDirectories", []),
                "preparation_inputs": list(dict.fromkeys(
                    p for sources in (main, item.get("test", {}))
                    for p in sources.get("resourcePreparationInputs", ())
                )),
                **settings,
            }
        )
        if test:
            modules[-1]["test_compiler"] = {
                **_compiler_settings(test, native=native),
                "classpath": [
                    p for p in classpath(test["classpath"]) if p != test_output
                ],
            }
            modules[-1]["test_compiler"]["module_path"] = classpath(modules[-1]["test_compiler"]["module_path"])
        if native:
            scopes = [main] + ([item["test"]] if test else [])
            outputs = list(dict.fromkeys(p for s in scopes for p in
                           (*s["classesDirectories"], *s.get("preparedResourceDirectories", ()),
                            *s.get("extraOutputDirs", ())) if p))
            resource_inputs = dict.fromkeys(p for s in scopes for p in
                                            (*s["resourceDirectories"], *s.get("resourcePreparationInputs", ())))
            modules[-1]["native_compile"] = {
                "build_root": model["rootDirectory"],
                "tasks": list(dict.fromkeys(t for s in scopes for t in
                                            (s["classesTask"], *s.get("extraOutputTasks", ())))),
                "outputs": outputs,
                "resource_inputs": [p for p in resource_inputs if p not in outputs],
            }
            for field in ("extraInputFiles", "extraInputTrees", "extraGeneratedPaths", "extraUnownedDirs"):
                values = [v for part in (*scopes, compile, test or {}) for v in part.get(field, ())]
                if values:
                    modules[-1]["native_compile"][field] = values
            modules[-1]["source_roots"] = main["languageRoots"]
            modules[-1]["test_source_roots"] = item["test"]["languageRoots"] if test else []
    target = next(item for item in modules if item["module_root"] == selected)
    if candidates and not native:
        target["dormant_language_roots"] = candidates
        # Empty optional language outputs are not missing Java dependencies.
        empty = {p for item in facts.values() for scope, task in (("main","compileJava"),("test","compileTestJava"))
                 if task in item and _foreign_inputs(item[scope], item[task])
                 for p in item[scope]["classesDirectories"] if p != item[task]["destinationDirectory"] and not Path(p).exists()}
        for module in modules:
            for m in (module, module.get("test_compiler", {})):
                for key in ("classpath", "test_classpath"):
                    if key in m:
                        m[key] = [p for p in m[key] if p not in empty]
    runtime = (
        model["testRuntime"]["classpath"]
        if model["exportScope"] == "test"
        else model["main"]["runtimeClasspath"]
    )
    return (
        tuple(modules),
        target,
        tuple(Path(p) for p in classpath(runtime, runtime=True)),
    )
