from jolink_runtime.launch.maven_native_session import _deletion_refresh


def test_only_removed_java_and_selected_dependent_compiler_scopes_are_refreshed(tmp_path):
    def module(name, dependencies=(), test_dependencies=()):
        root = tmp_path / name
        return {"source_roots": [str(root/"main")], "test_source_roots": [str(root/"tests")],
                "output_directory": str(root/"classes"), "test_output_directory": str(root/"test-classes"),
                "classpath": list(dependencies), "test_classpath": list(test_dependencies),
                "test_compiler": {"classpath": list(dependencies) + list(test_dependencies)}}
    a = module("a")
    b = module("b", [a["output_directory"]])
    c = module("c", [b["output_directory"]], [a["test_output_directory"]])
    independent = module("other")
    modules = [c, independent, b, a]  # no reliance on reactor iteration order
    src = lambda m, scope: str(tmp_path / m / scope / "S.java")
    previous = {src(m, s): [1,10] for m in ("a","b","c","other") for s in ("main","tests")}
    current = dict(previous)
    current[src("a","main")] = [2,20]
    assert _deletion_refresh(modules, previous, current) == {}
    del current[src("a","tests")]
    assert _deletion_refresh(modules, previous, current) == {
        a["test_output_directory"]:(True,False), c["test_output_directory"]:(False,True)}
    del current[src("a","main")]
    refresh = _deletion_refresh(modules, previous, current)
    assert set(refresh) == {m[k] for m in (a,b,c) for k in ("output_directory","test_output_directory")}
    assert refresh[a["output_directory"]] == (True,False)
    assert refresh[b["output_directory"]] == (False,True)
