"""Translate resolved Gradle projects into the shared persistent JDT model."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .compiler_profile import classify_compiler_arguments
from .jdt_compile_session import select_target_system_home


class GradleModuleError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.error_code = code


def module_world(model: dict) -> tuple[tuple[dict, ...], dict, tuple[Path, ...]]:
    from .gradle_runtime_build_world import _processor_facts

    facts = {item["projectDirectory"]: item for item in model["modules"]}
    selected = model["projectDirectory"]
    output_sources = {}
    for item in facts.values():
        for source in (item["main"], item.get("test", {})):
            if source.get("resourcesDirectory"):
                output_sources[source["resourcesDirectory"]] = source[
                    "resourceDirectories"
                ]

    def classpath(entries, *, runtime=False):
        paths = []
        for raw in entries:
            artifact = model.get("projectArtifacts", {}).get(raw)
            if artifact:
                producer = facts[artifact["projectDirectory"]]
                if artifact["kind"] != "resources":
                    paths.append(producer["compileJava"]["destinationDirectory"])
                if runtime and artifact["kind"] in {"jar", "resources"}:
                    paths.extend(
                        p
                        for p in producer["main"]["resourceDirectories"]
                        if Path(p).is_dir()
                    )
            elif raw in output_sources:
                if runtime:
                    paths.extend(p for p in output_sources[raw] if Path(p).is_dir())
            else:
                paths.append(raw)
        return list(dict.fromkeys(paths))

    modules = []
    for item in facts.values():
        main = item["main"]
        compile = item["compileJava"]
        test = item.get("compileTestJava")
        args = compile["compilerArgsPrivate"]
        profile = classify_compiler_arguments(args)
        if test and any(
            test.get(key) != compile.get(key)
            for key in (
                "sourceCompatibility",
                "targetCompatibility",
                "release",
                "encoding",
                "annotationProcessorPath",
                "compilerArgsPrivate",
            )
        ):
            raise GradleModuleError(
                "GRADLE_TEST_COMPILER_CONFIGURATION_UNMODELED",
                "Main and test need different JDT compiler/Processor settings; this module currently shares one JDT project.",
            )
        if profile.unresolved_arguments or compile.get(
            "compilerArgumentProvidersUnmodeled"
        ):
            raise GradleModuleError(
                "GRADLE_COMPILE_CONFIGURATION_UNMODELED",
                "Gradle compiler arguments cannot be mapped to JDT.",
            )
        for sources in (main, item.get("test", {})):
            if any(sources.get(field) for field in ("javaIncludes", "javaExcludes")):
                raise GradleModuleError(
                    "GRADLE_SOURCE_PATTERN_UNMODELED",
                    "Gradle Java source include/exclude patterns are not mapped to the JDT source index.",
                )
        level = int(
            str(compile.get("release") or compile["targetCompatibility"]).removeprefix(
                "1."
            )
        )
        home = select_target_system_home((Path(compile["compilerJavaHome"]),), level)
        processors, lombok = [], []
        processor_path = (
            compile["annotationProcessorPath"] if "-proc:none" not in args else []
        )
        for path in processor_path:
            _, is_lombok = _processor_facts(Path(path))
            (lombok if is_lombok else processors).append(path)
        output = compile["destinationDirectory"]
        test_output = (
            test["destinationDirectory"]
            if test
            else str(Path(item["projectDirectory"]) / "build/classes/java/test")
        )
        dependencies = classpath(compile["classpath"])
        modules.append(
            {
                "module_root": item["projectDirectory"],
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
                "resource_roots": main["resourceDirectories"],
                "source_level": level,
                "source_encoding": compile["encoding"],
                "target_java_home": str(home),
                "method_parameters": profile.method_parameters,
                "processor_entries": processors,
                "lombok_entries": lombok,
            }
        )
    target = next(item for item in modules if item["module_root"] == selected)
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


def module_fingerprint(
    modules: tuple[dict, ...], configuration: tuple[Path, ...]
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "modules": modules,
                "configuration": {
                    str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in configuration
                    if p.is_file()
                },
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
