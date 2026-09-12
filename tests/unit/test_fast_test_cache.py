from pathlib import Path

from jolink_runtime.launch.fast_test_cache import _inputs


def test_maven_cache_tracks_only_small_build_configuration(tmp_path: Path) -> None:
    parent = tmp_path / "pom.xml"
    module = tmp_path / "app"
    module.mkdir()
    pom = module / "pom.xml"
    parent.write_text("<project><modelVersion>4.0.0</modelVersion></project>")
    pom.write_text(
        "<project><modelVersion>4.0.0</modelVersion><parent>"
        "<relativePath>../pom.xml</relativePath></parent></project>"
    )
    source = module / "src/main/java/App.java"
    source.parent.mkdir(parents=True)
    source.write_text("class App {}")

    before = _inputs(module, "maven")
    source.write_text("class App { int value; }")
    assert _inputs(module, "maven") == before
    parent.write_text("<project><modelVersion>4.0.0</modelVersion><!-- changed --></project>")
    assert _inputs(module, "maven") != before


def test_reactor_cache_detects_upstream_pom_edit_without_reading_sources(tmp_path):
    (tmp_path / "pom.xml").write_text('<project><modules><module>lib</module></modules></project>')
    module = tmp_path / "lib"
    module.mkdir()
    (module / "pom.xml").write_text('<project/>')
    source = module / "src/main/java/Value.java"
    source.parent.mkdir(parents=True)
    source.write_text('class Value {}')
    before = _inputs(tmp_path, "maven")
    source.write_text('broken source')
    assert _inputs(tmp_path, "maven") == before
    (module / "pom.xml").write_text('<project><!-- dependency changed --></project>')
    assert _inputs(tmp_path, "maven") != before


def test_gradle_cache_uses_exported_subproject_build_files(tmp_path):
    from jolink_runtime.launch.project_launch_cache import _configuration_stamps
    (tmp_path / "settings.gradle").write_text("include 'lib', 'app'")
    script = tmp_path / "custom-module/location/build.gradle.kts"
    script.parent.mkdir(parents=True)
    script.write_text('plugins { `java-library` }')
    source = script.parent / "src/main/java/Value.java"
    source.parent.mkdir(parents=True)
    source.write_text("class Value {}")
    before = _inputs(tmp_path, "gradle", (script,))
    startup = _configuration_stamps((script,))
    source.write_text("class Value { int n; }")
    assert _inputs(tmp_path, "gradle", (script,)) == before
    assert _configuration_stamps(startup) == startup
    script.write_text('plugins { `java-library` }; dependencies { implementation("g:a:1") }')
    assert _inputs(tmp_path, "gradle", (script,)) != before
    assert _configuration_stamps(startup) != startup


def test_gradle_build_logic_inputs_detect_add_edit_remove_not_business_source(tmp_path):
    from jolink_runtime.launch.project_launch_cache import _configuration_stamps
    logic = tmp_path / "conventions/custom-source"
    logic.mkdir(parents=True)
    business = tmp_path / "src/main/java/App.java"
    business.parent.mkdir(parents=True)
    business.write_text("class App {}")
    before = _inputs(tmp_path, "gradle", (logic,))
    launch = _configuration_stamps((logic,))
    business.write_text("class App { int changed; }")
    assert _inputs(tmp_path, "gradle", (logic,)) == before
    assert _configuration_stamps((logic,)) == launch
    plugin = logic / "Convention.java"
    plugin.write_text("class Convention {}")
    added = _inputs(tmp_path, "gradle", (logic,))
    assert added != before
    assert _configuration_stamps((logic,)) != launch
    plugin.write_text("class Convention { int changed; }")
    assert _inputs(tmp_path, "gradle", (logic,)) != added
    plugin.unlink()
    assert _inputs(tmp_path, "gradle", (logic,)) == before
    assert _configuration_stamps((logic,)) == launch


def test_gradle_script_glob_does_not_include_gradle_cache_directory(tmp_path):
    from jolink_runtime.launch.gradle_probe import gradle_configuration_inputs
    (tmp_path / "build.gradle").write_text("plugins { id 'java' }")
    transient = tmp_path / ".gradle/cache.bin"
    transient.parent.mkdir()
    transient.write_bytes(b"first")
    inputs = gradle_configuration_inputs(tmp_path)
    assert transient.parent not in inputs
    before = _inputs(tmp_path, "gradle", inputs)
    transient.write_bytes(b"second")
    assert _inputs(tmp_path, "gradle", inputs) == before
