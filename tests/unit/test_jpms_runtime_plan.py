from pathlib import Path
from types import SimpleNamespace

from jolink_runtime.launch.jpms_runtime import module_runtime, module_vm_arguments


def fixture(tmp_path):
    main, tests, lib = (tmp_path / name for name in ("main", "test", "lib"))
    outputs = {name: path / "bin" for name, path in (("main", main), ("test", tests), ("lib", lib))}
    resources = {name: tmp_path / (name + "-resources") for name in outputs}
    junit = tmp_path / "junit.jar"
    scopes = {name: {"output": output, "module_path": (outputs["lib"],),
                     "resources": (resources[name],), "scope": "test" if name == "test" else "main"}
              for name, output in outputs.items()}
    compiler = SimpleNamespace(output_directory=outputs["main"],
        module_metadata={"main": {"module_name": "demo.app"}, "lib": {"module_name": "demo.lib"},
                         "test": {"module_name": None, "patched_module_name": "demo.app"}},
        module_runtime_scopes=lambda: scopes,
        _source_map={tmp_path / "SomeTest.java": tests / "src/example/tests/SomeTest.java"},
        _outputs={str(tmp_path / "target/classes"): outputs["main"],
                  str(tmp_path / "target/test-classes"): outputs["test"]})
    entries = (outputs["test"], outputs["main"], outputs["lib"], *resources.values(), junit)
    return compiler, entries, outputs, resources, junit


def test_named_tests_share_module_paths_and_patch_resources(tmp_path):
    compiler, entries, output, resources, junit = fixture(tmp_path)
    plan = module_runtime(compiler, entries, tests=True)
    assert plan.name == "demo.app"
    assert plan.module_path == (output["main"], output["lib"])
    assert plan.classpath == (junit,)
    assert "demo.app=ALL-UNNAMED" in plan.arguments
    assert "demo.app/example.tests=ALL-UNNAMED" in plan.arguments
    patch = next(arg for arg in plan.arguments if arg.startswith("demo.app=") and str(output["test"]) in arg)
    assert str(resources["main"]) in patch and str(resources["test"]) in patch


def test_native_classpath_test_policy_is_not_forced_to_named(tmp_path):
    compiler, entries, *_ = fixture(tmp_path)
    plan = module_runtime(compiler, entries, tests=True, enabled=False)
    assert plan.name is None and plan.classpath == entries
    assert not plan.module_path and not plan.arguments


def test_explicit_test_module_arguments_do_not_gain_automatic_access(tmp_path):
    compiler, entries, *_ = fixture(tmp_path)
    plan = module_runtime(compiler, entries, tests=True, test_access=False)
    assert plan.name == "demo.app"
    assert "--patch-module" in plan.arguments
    assert "--add-reads" not in plan.arguments and "--add-opens" not in plan.arguments


def test_native_output_paths_are_remapped_and_patches_merged(tmp_path):
    compiler, entries, output, resources, _ = fixture(tmp_path)
    plan = module_runtime(compiler, entries, tests=True, test_access=False)
    native = ("-Dkeep=yes", "--module-path=target/classes", "--patch-module",
              "demo.app=target/test-classes", "--add-reads", "demo.app=ALL-UNNAMED")
    arguments, paths = module_vm_arguments(native, plan, compiler, directory=tmp_path)
    assert paths == (output["main"], output["lib"])
    assert arguments.count("--patch-module") == 2  # One patch per app/lib, not duplicate app patches.
    assert "-Dkeep=yes" in arguments
    patches = [arguments[i + 1] for i, arg in enumerate(arguments) if arg == "--patch-module"]
    app = next(value for value in patches if value.startswith("demo.app="))
    assert app.count(str(output["test"])) == 1
    assert str(resources["main"]) in app
    assert not any("target/test-classes" in arg for arg in arguments)


def test_nonmodule_compiler_needs_no_module_methods(tmp_path):
    entries = (tmp_path / "classes",)
    assert module_runtime(SimpleNamespace(), entries).classpath == entries
