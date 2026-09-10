from pathlib import Path

from jolink_runtime.launch.build_world_identity import build_world_fingerprint


def test_build_world_identity_tracks_configuration_and_directory_dependencies(
    tmp_path, monkeypatch
):
    pom = tmp_path / "pom.xml"
    pom.write_text("<project/>")
    compiler = tmp_path / "javac"
    compiler.write_bytes(b"tool identity")
    classes = tmp_path / "classes"
    classes.mkdir()
    entry = classes / "App.class"
    entry.write_bytes(b"version-1")

    def identity():
        return build_world_fingerprint(
            configuration_inputs=(pom,),
            configuration_environment_names=("JOLINK_TEST_BUILD_IDENTITY",),
            javac_executable=compiler,
            compile_classpath=(classes,),
        )

    first = identity()
    assert first == identity()
    pom.write_text("<project><changed/></project>")
    second = identity()
    assert second != first
    entry.write_bytes(b"version-2")
    third = identity()
    assert third != second
    monkeypatch.setenv("JOLINK_TEST_BUILD_IDENTITY", "changed")
    assert identity() != third


def test_product_does_not_import_retired_backend_or_experiment_policy():
    import jolink_runtime

    root = Path(jolink_runtime.__file__).parent
    assert not (root / "launch/fast_compile.py").exists()
    import ast

    for file in root.rglob("*.py"):
        if "experiments" in file.relative_to(root).parts:
            continue
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert "fast_compile" not in (node.module or ""), file
                assert "experiments" not in (node.module or ""), file
