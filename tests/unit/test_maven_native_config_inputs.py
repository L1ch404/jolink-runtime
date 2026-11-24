from zipfile import ZipFile

from jolink_runtime.launch.maven_native_session import _config_inputs


def test_tracks_existing_and_missing_lombok_imports_without_resolving_values(tmp_path, monkeypatch):
    package = tmp_path / "src" / "main" / "java" / "p"
    package.mkdir(parents=True)
    (tmp_path / "lombok.config").write_text(
        "import first%20file\nimport <NATIVE_TEST_LOMBOK>/styles.jar!config/shared\n", encoding="utf-8")
    monkeypatch.setenv("NATIVE_TEST_LOMBOK", str(tmp_path))
    first = tmp_path / "first file"
    first.write_text("import lombok.config\nimport second\n", encoding="utf-8")
    archive = tmp_path / "styles.jar"
    with ZipFile(archive, "w") as out:
        out.writestr("config/shared", "lombok.accessors.chain = true")
    before = _config_inputs([package])
    assert before[str(first)] and before[str(archive)]
    assert before[str(tmp_path / "second")] is None
    assert before[str(package / "lombok.config")] is None
    second = tmp_path / "second"
    second.write_text("lombok.accessors.chain = false", encoding="utf-8")
    assert _config_inputs([package])[str(second)]
    first.write_text("clear lombok.accessors.chain", encoding="utf-8")
    assert str(second) not in _config_inputs([package])
