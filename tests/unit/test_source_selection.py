import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_jdt_compile_session import _candidate

from jolink_runtime.launch.jdt_modules import ModuleCompileSession
from jolink_runtime.launch.maven_compile_scope import (
    compiler_scope,
    compiler_source_patterns,
)
from jolink_runtime.launch.source_selection import SourceSelection, ant_pattern


@pytest.mark.parametrize("pattern,path,expected", [
    ("**/*.java", "App.java", True),
    ("**/*.java", "a/b/App.java", True),
    ("*.java", "a/App.java", False),
    ("a/*/App.java", "a/App.java", False),
    ("a/*/App.java", "a/b/App.java", True),
    ("a/**/App.java", "a/App.java", True),
    ("a/**/App.java", "a/b/c/App.java", True),
    ("**/App?.java", "a/App1.java", True),
    ("**/App?.java", "a/App12.java", False),
    ("**/App?.java", "a/App.java", False),
    ("a/", "a/b/C.java", True),
    ("a/**", "ab/C.java", False),
    ("**/App.java", "App.java", True),
    ("**/App.java", "app.java", False),
    (r"a\**\App.java", "a/b/App.java", True),
    ("**/A[12].java", "A1.java", False),
    ("**/A[12].java", "A[12].java", True),
    ("**/中文?.java", "空间/中文甲.java", True),
])
def test_ant_source_patterns(pattern, path, expected):
    assert bool(ant_pattern(pattern).fullmatch(path)) is expected


def test_inclusions_are_unioned_and_exclusions_win():
    rules = SourceSelection({"includes": ["api/**", "impl/**"], "excludes": ["**/Legacy*.java"]})
    assert rules.accepts("api/App.java")
    assert rules.accepts("impl/Thing.java")
    assert not rules.accepts("other/Thing.java")
    assert not rules.accepts("api/LegacyApp.java")
    assert SourceSelection({}).accepts("anything/App.java")


@pytest.mark.parametrize("namespaced", [False, True])
def test_effective_main_and_test_patterns_do_not_leak_between_scopes(namespaced):
    ns = ' xmlns="http://maven.apache.org/POM/4.0.0"' if namespaced else ""
    project = ET.fromstring(f'''<project{ns}><build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId>
<configuration><includes><include>main/**/*.java</include></includes><excludes><exclude>**/MainIgnored.java</exclude></excludes>
<testIncludes><testInclude>checks/**/*.java</testInclude></testIncludes><testExcludes><testExclude>**/TestIgnored.java</testExclude></testExcludes></configuration>
<executions><execution><id>default-compile</id><configuration><excludes><exclude>legacy/**</exclude></excludes></configuration></execution>
<execution><id>default-testCompile</id><configuration><testExcludes><testExclude>slow/**</testExclude></testExcludes></configuration></execution></executions>
</plugin></plugins></build></project>''')
    assert compiler_source_patterns(compiler_scope(project)) == {"includes": ["main/**/*.java"], "excludes": ["legacy/**"]}
    assert compiler_source_patterns(compiler_scope(project, test=True), test=True) == {"includes": ["checks/**/*.java"], "excludes": ["slow/**"]}
    basic = ET.fromstring('''<project><build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><configuration>
<includes><include>main/**</include></includes></configuration></plugin></plugins></build></project>''')
    assert compiler_source_patterns(compiler_scope(basic, test=True), test=True) == {}
    assert compiler_source_patterns(ET.fromstring("<project/>")) == {}


def test_filtering_precedes_layout_mapping_and_is_shared_with_reopen_and_hints(tmp_path, monkeypatch):
    main, tests = tmp_path / "source", tmp_path / "checks"
    main.mkdir()
    tests.mkdir()
    accepted = main / "App.java"
    accepted.write_text("package real; class App {}")
    excluded = main / "legacy/Hidden.java"
    excluded.parent.mkdir()
    excluded.write_text("this is not valid Java")
    test = tests / "MainIgnored.java"
    test.write_text("package real; class MainIgnored {}")
    rejected_test = tests / "TestIgnored.java"
    rejected_test.write_text("invalid Java")
    module = {
        "module_root": str(tmp_path), "source_roots": [str(main)], "test_source_roots": [str(tests)],
        "output_directory": str(tmp_path / "target/classes"), "test_output_directory": str(tmp_path / "target/test-classes"),
        "classpath": [], "source_encoding": "UTF-8", "source_level": 8,
        "source_patterns": {"excludes": ["legacy/**", "**/MainIgnored.java"]},
        "test_source_patterns": {"excludes": ["**/TestIgnored.java"]},
    }
    def session():
        return ModuleCompileSession(modules=(module,), target_module=tmp_path, split_tests=True,
            root=tmp_path / "session", candidate=_candidate(tmp_path), worker_java_home=tmp_path / "jdk",
            source_roots=(main,), test_source_roots=(tests,), classpath_entries=(), source_encoding="UTF-8")
    compiler = session()
    compiler.root.mkdir()
    read = Path.read_bytes
    def observed_read(path):
        assert path not in {excluded, rejected_test}, "excluded source contents must not be read"
        return read(path)
    monkeypatch.setattr(Path, "read_bytes", observed_read)
    compiler._materialize_sources()
    assert set(compiler._source_map) == {accepted, test}
    assert compiler._source_map[accepted].relative_to(compiler.private_project).as_posix().endswith("/src/real/App.java")
    excluded.write_text("still invalid")
    assert compiler.workspace_source_changes() == ()
    compiler.accept_baseline()
    compiler._client = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))
    assert compiler.compile((excluded,)).actual_build_kind is None
    compiler.save_source_index()
    reopened = session()
    reopened._restore_persisted_source_map()
    assert reopened.workspace_source_changes() == ()
    assert set(reopened._source_map) == {accepted, test}
    accepted.unlink()
    assert reopened.workspace_source_changes() == (accepted,)
    accepted.write_text("package real; class App { int next; }")
    reopened._synchronize_persisted_sources()
    assert set(reopened._source_map) == {accepted, test}
