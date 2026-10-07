import pytest

from jolink_runtime.launch.configuration_inputs import (
    configuration_file_stamps,
    maven_configuration_files,
)
from jolink_runtime.launch.maven_reactor import maven_build_root


def pom(directory, body="", namespace=False):
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / "pom.xml"
    ns = ' xmlns="http://maven.apache.org/POM/4.0.0"' if namespace else ""
    file.write_text(f"<project{ns}>{body}</project>")
    return file


@pytest.mark.parametrize("namespace", [False, True])
@pytest.mark.parametrize("profile", [False, True])
def test_literal_aggregation_not_inheritance_selects_root(tmp_path, namespace, profile):
    root = tmp_path / "reactor"
    app = root / "components/app"
    declaration = "<modules><module>components/app</module></modules>"
    if profile:
        declaration = f"<profiles><profile><id>services</id>{declaration}</profile></profiles>"
    pom(root, declaration, namespace)
    pom(app)  # No inherited parent: aggregation alone is sufficient.
    assert maven_build_root(app) == root
    assert maven_build_root(root) == root


def test_nested_aggregators_choose_outer_root(tmp_path):
    root = tmp_path / "reactor"
    group = root / "modules/services"
    app = group / "app"
    pom(root, "<modules><module>modules/services</module></modules>")
    pom(group, "<modules><module>app</module></modules>")
    pom(app)
    # A higher unrelated Maven project is not the authority for this reactor.
    pom(tmp_path, "<modules><module>unrelated</module></modules>")
    assert maven_build_root(app) == root
    assert maven_build_root(group) == root


def test_inherited_parent_without_aggregation_is_not_a_build_root(tmp_path):
    app = tmp_path / "app"
    pom(tmp_path, "<packaging>pom</packaging>")
    pom(app, "<parent><relativePath>../pom.xml</relativePath></parent>")
    assert maven_build_root(app) == app


def test_sibling_prefix_is_not_module_membership(tmp_path):
    pom(tmp_path, "<modules><module>app-old</module></modules>")
    app = tmp_path / "app"
    pom(app)
    assert maven_build_root(app) == app


def test_unreadable_or_invalid_unrelated_ancestor_is_ignored(tmp_path):
    pom(tmp_path).write_text("broken")
    app = tmp_path / "nested/app"
    pom(app)
    assert maven_build_root(app) == app


def test_canonical_module_paths(tmp_path):
    root = tmp_path / "含空格 reactor"
    app = root / "app"
    pom(root, "<modules><module> ./unused/../app/ </module></modules>")
    pom(app)
    assert maven_build_root(app / "../app") == root


def test_child_cache_tracks_aggregator_config_even_without_inheritance(tmp_path):
    root, app = tmp_path, tmp_path / "app"
    root_pom = pom(root, "<modules><module>app</module><module>library</module></modules>")
    pom(root / "library")
    pom(app)
    inputs = maven_configuration_files(app)
    assert root_pom in inputs
    assert root / "library/pom.xml" in inputs
    assert root / ".mvn/maven.config" in inputs
    assert app / ".mvn/maven.config" in inputs
    before = configuration_file_stamps(inputs)
    (root / ".mvn").mkdir()
    (root / ".mvn/maven.config").write_text("-Pdev")
    assert configuration_file_stamps(maven_configuration_files(app)) != before


def test_adding_and_removing_aggregation_changes_child_cache_inputs(tmp_path):
    app = tmp_path / "app"
    pom(app)
    before = configuration_file_stamps(maven_configuration_files(app))
    root_pom = pom(tmp_path, "<modules><module>app</module></modules>")
    aggregated = configuration_file_stamps(maven_configuration_files(app))
    assert aggregated != before
    root_pom.unlink()
    assert configuration_file_stamps(maven_configuration_files(app)) == before
