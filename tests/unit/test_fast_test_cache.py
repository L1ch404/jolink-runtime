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
