"""One compiler configuration and workspace lifecycle for launch and Fast Test."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .build_world_identity import build_world_fingerprint
from .jdt_compile_session import (
    PersistentJdtCompileSession,
    discover_target_system_entries,
    lombok_worker_jvm_arguments,
)
from .jdt_modules import ModuleCompileSession, compilation_modules, module_name, module_resources


def launch_compilation_configuration(plan):
    return capture_compilation_configuration(
        target_java_home=plan.target_java_home, modules=plan.modules,
        target_module=plan.module_root,
        source_roots=plan.source_roots, classpath_entries=plan.dependency_entries,
        source_encoding=plan.source_encoding, source_level=plan.source_level,
        method_parameters=plan.method_parameters,
        processor_entries=plan.processor_entries, processor_names=plan.processor_names,
        processor_options=plan.processor_options,
        java_agents=tuple(f"{path}=ECJ" for path in plan.lombok_entries),
        min_heap_mb=plan.worker_min_heap_mb, max_heap_mb=plan.worker_max_heap_mb,
    )


def test_compilation_configuration(world):
    return capture_compilation_configuration(
        target_java_home=world.target_java_home, modules=world.modules,
        target_module=world.module_root, split_tests=True,
        source_roots=world.main_source_roots, classpath_entries=world.main_dependencies,
        test_source_roots=world.test_source_roots, test_classpath_entries=world.test_dependencies,
        source_encoding=world.source_encoding, source_level=world.source_level,
        method_parameters=world.method_parameters,
        processor_entries=world.processor_entries, processor_names=world.processor_names,
        processor_options=world.processor_options, java_agents=world.java_agents,
        extra_jvm_arguments=world.extra_worker_jvm_arguments,
        min_heap_mb=world.worker_min_heap_mb, max_heap_mb=world.worker_max_heap_mb,
    )


def capture_compilation_configuration(*, target_java_home, modules=(), target_module=None,
                                      split_tests=False, **options):
    """Capture the constructor arguments once after Probe; cache hits only read JSON."""
    target_java_home = Path(target_java_home)
    options = {
        "test_source_roots": (), "test_classpath_entries": (), "extra_jvm_arguments": (),
        **options,
        "classpath_entries": (
            *discover_target_system_entries(target_java_home, options["source_level"]),
            *options["classpath_entries"],
        ),
    }
    if modules:
        options.update(
            modules={module_name(m["module_root"]): m for m in compilation_modules(modules)},
            target_module=target_module, split_tests=split_tests,
        )
    # Use this exact JSON for identity AND compiler construction. No second
    # identity-only field list that can drift away from the actual JDT inputs.
    options = json.loads(json.dumps(options, default=str))
    outputs = {str(Path(m[key])) for m in modules
               for key in ("output_directory", "test_output_directory") if key in m}
    scopes = [m for m in options.get("modules", {}).values()]
    if split_tests:
        scopes += [m["test_compiler"] for m in scopes if m.get("test_compiler")]
    binaries = dict.fromkeys((
        *options["classpath_entries"], *options["test_classpath_entries"],
        *options["processor_entries"],
        *(agent.split("=", 1)[0] for agent in options["java_agents"]),
        *(p for scope in scopes for key in ("classpath", "test_classpath", "processor_entries", "lombok_entries")
          for p in scope.get(key, ())),
    ))
    binary_inputs = build_world_fingerprint(
        configuration_inputs=(),
        javac_executable=target_java_home / "bin" / ("javac.exe" if os.name == "nt" else "javac"),
        compile_classpath=(Path(p) for p in binaries if str(Path(p)) not in outputs),
    )
    return {"options": options, "binary_inputs": binary_inputs}


def open_compile_session(configuration, *, store, project_root, module_root,
                         candidate, worker_java, resource_modules=()):
    """Bind the saved compiler arguments to the selected Worker and workspace."""
    options = dict(configuration["options"])
    options["extra_jvm_arguments"] = [
        *options["extra_jvm_arguments"],
        *lombok_worker_jvm_arguments(worker_java.major, lombok_enabled=bool(options["java_agents"])),
    ]
    workspace = store.claim(
        project_root=project_root, module_root=module_root,
        identity={"worker": candidate.root.name, "worker_java_home": str(worker_java.home),
                  "options": options, "binary_inputs": configuration["binary_inputs"]},
    )
    arguments = dict(options)
    for key in ("source_roots", "classpath_entries", "test_source_roots", "test_classpath_entries", "processor_entries"):
        arguments[key] = tuple(Path(p) for p in arguments[key])
    factory = PersistentJdtCompileSession
    if "modules" in arguments:
        factory = ModuleCompileSession
        arguments["modules"] = tuple(arguments["modules"].values())
        arguments["target_module"] = Path(arguments["target_module"])
        arguments["runtime_resources"] = module_resources(resource_modules)
    compiler = factory(root=workspace.root, candidate=candidate, worker_java_home=worker_java.home,
                       preserve_root_on_close=True, **arguments)
    return workspace, compiler


def initialize_compile_session(compiler, *, reuse_workspace):
    """Cold FULL, or reopen then synchronize only edits; return the actual build."""
    result = compiler.start(reuse_workspace=True, build_on_reuse=False) if reuse_workspace else compiler.start()
    if reuse_workspace:
        changed = compiler.workspace_source_changes()
        if changed:
            result = compiler.compile(changed)
    return result


def accept_compile_session(compiler, workspace):
    """Publish the accepted source baseline after the caller checks its outcome/cancellation."""
    compiler.accept_baseline()
    compiler.save_source_index()
    workspace.mark_initialized()
