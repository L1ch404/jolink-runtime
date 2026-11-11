import json
from pathlib import Path

from jolink_runtime.launch.processor_file_inputs import ProcessorFileInputs


def test_file_changes_are_scoped_and_survive_reopen(tmp_path):
    main, tests = tmp_path / "workspace/main", tmp_path / "workspace/test"
    first, second = tmp_path / "main input.txt", tmp_path / "test input.txt"
    first.write_text("alpha")
    second.write_text("test")
    sources = {tmp_path / "Main.java": main / "src/Main.java",
               tmp_path / "Test.java": tests / "src/Test.java"}
    scopes = {main: {"anything": str(first)}, tests: {"other": str(second)}}
    tracker = ProcessorFileInputs()
    tracker.configure(scopes)
    assert tracker.refresh(sources) == set(sources)
    tracker.built()
    assert not tracker.refresh(sources)
    saved = json.loads(json.dumps(tracker.snapshot()))
    first.write_text("bravo")  # Equal size; mtime must still invalidate.
    assert tracker.refresh(sources) == {tmp_path / "Main.java"}
    assert tracker.snapshot() == saved  # Scanning is not compilation.
    reopened = ProcessorFileInputs()
    reopened.configure(scopes)
    reopened.restore(saved)
    assert reopened.refresh(sources) == {tmp_path / "Main.java"}
    reopened.built()
    first.unlink()
    assert reopened.refresh(sources) == {tmp_path / "Main.java"}
    reopened.built()
    first.write_text("recovered")
    assert reopened.refresh(sources) == {tmp_path / "Main.java"}


def test_missing_file_is_tracked_before_creation(tmp_path):
    source, project = tmp_path / "Source.java", tmp_path / "workspace"
    input_file = tmp_path / "missing" / "input.txt"
    tracker = ProcessorFileInputs()
    tracker.configure({project: {"custom": str(input_file)}})
    sources = {source: project / "src/Source.java"}
    assert tracker.refresh(sources) == {source}
    tracker.built()  # Compiler may have returned missing-input diagnostics.
    assert not tracker.refresh(sources)
    input_file.parent.mkdir()
    input_file.write_text("created")
    assert tracker.refresh(sources) == {source}


def test_build_preserves_observation_instead_of_postbuild_filesystem(tmp_path):
    file = tmp_path / "input"
    file.write_text("before")
    source, project = tmp_path / "Source.java", tmp_path / "workspace"
    sources = {source: project / "src/Source.java"}
    tracker = ProcessorFileInputs()
    tracker.configure({project: {"custom": str(file)}})
    tracker.refresh(sources)
    before = json.loads(json.dumps(tracker.observed))
    file.write_text("changed-during-build")
    tracker.built()
    assert tracker.snapshot() == before
    assert tracker.refresh(sources) == {source}


def test_nonpaths_and_directory_contents_are_not_scanned(tmp_path, monkeypatch):
    tracker = ProcessorFileInputs()
    tracker.configure({tmp_path: {"flag": None, "empty": "", "mode": "enabled",
                                 "relative": "config/input.txt", "url": "https://example.test/a",
                                 "directory": str(tmp_path)}})
    assert tracker.paths == {tmp_path: (tmp_path,)}
    monkeypatch.setattr(Path, "rglob", lambda *args: (_ for _ in ()).throw(AssertionError("no directory scan")))
    tracker.refresh({})
    tracker.built()
    (tmp_path / "unrelated.txt").write_text("new")
    assert not tracker.refresh({tmp_path / "Main.java": tmp_path / "src/Main.java"})
    empty = ProcessorFileInputs()
    empty.configure({tmp_path: {"mode": "ordinary"}})
    monkeypatch.setattr(Path, "stat", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no stat")))
    assert not empty.refresh({})


def test_native_reads_include_missing_priority_roots_and_survive_reopen(tmp_path):
    project, other = tmp_path / "workspace/main", tmp_path / "workspace/test"
    source, test = tmp_path / "Main.java", tmp_path / "Test.java"
    sources = {source: project / "src/Main.java", test: other / "src/Test.java"}
    earlier = tmp_path / "first.txt"
    fallback = tmp_path / "fallback.txt"
    fallback.write_text("fallback")
    tracker = ProcessorFileInputs()
    tracker.configure({project: {}, other: {}})
    assert not tracker.refresh(sources)
    tracker.built({str(project): {str(earlier): None, str(fallback): tracker._stamp(fallback)}})
    saved = json.loads(json.dumps(tracker.snapshot()))
    assert not tracker.refresh(sources)
    # A new higher-priority sourcepath entry must trigger even though the file
    # actually consumed during the previous build has not changed.
    earlier.write_text("first")
    reopened = ProcessorFileInputs()
    reopened.restore(saved)
    reopened.configure({project: {}, other: {}})
    assert reopened.refresh(sources) == {source}
    reopened.built({})  # Other known dependencies survive partial builds.
    fallback.unlink()
    assert reopened.refresh(sources) == {source}


def test_read_snapshot_does_not_hide_edits_during_compilation(tmp_path):
    project = str(tmp_path / "workspace")
    file = tmp_path / "template.txt"
    file.write_text("old")
    tracker = ProcessorFileInputs()
    source = tmp_path / "Main.java"
    sources = {source: Path(project) / "src/Main.java"}
    tracker.configure({Path(project): {}})
    tracker.refresh(sources)
    first_read = tracker._stamp(file)
    file.write_text("edited-after-first-read")
    tracker.built({project: {str(file): first_read}})
    assert tracker.refresh(sources) == {source}
    before_build = tracker.observed[project][str(file)]
    file.write_text("edited-again-during-build")
    tracker.built({project: {str(file): tracker._stamp(file)}})
    assert tracker.snapshot()[project][str(file)] == before_build
    assert tracker.refresh(sources) == {source}
