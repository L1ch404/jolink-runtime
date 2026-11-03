import base64
import json
from pathlib import Path

from jolink_runtime.launch.lombok_configuration import LombokConfigurationInputs


class Worker:
    def __init__(self, groups):
        self.groups = groups
        self.calls = []

    def command(self, command):
        self.calls.append(command)
        return {"ok": True, "status": "lombok_configured", "groups": self.groups}


def test_configuration_delta_is_scoped_and_survives_reopen(tmp_path):
    root = tmp_path / "session"
    root.mkdir()
    first, second = tmp_path / "one/A.java", tmp_path / "two/B.java"
    config = tmp_path / "shared.config"
    inputs = [str(config)]
    group = lambda source, fingerprint: {
        "directory": str(source.parent), "fingerprint": fingerprint, "inputs": inputs,
    }
    worker = Worker([group(first, "one-original"), group(second, "two-original")])
    sources = {first: root / "src/example/A.java", second: root / "src/example/B.java"}
    tracker = LombokConfigurationInputs(root, enabled=True)
    assert tracker.refresh(worker, sources) == {first, second}
    tracker.built()
    mapping = root / "lombok-source-origins.txt"
    assert [tuple(base64.b64decode(part).decode() for part in line.split("\t"))
            for line in mapping.read_text().splitlines()] == [
        (str(sources[first]), str(first)), (str(sources[second]), str(second)),
    ]
    stamp = mapping.stat().st_mtime_ns
    assert not tracker.refresh(worker, sources)
    assert len(worker.calls) == 1

    config.write_text("newly created import")
    worker.groups = [group(first, "one-changed"), group(second, "two-original")]
    assert tracker.refresh(worker, sources) == {first}
    assert tracker.refresh(worker, sources) == {first}  # Until a build consumes it.
    assert mapping.stat().st_mtime_ns == stamp
    tracker.built()

    reopened = LombokConfigurationInputs(root, enabled=True)
    reopened.restore(json.loads(json.dumps(tracker.snapshot())))
    assert not reopened.refresh(worker, sources, force=True)
    assert len(worker.calls) == 3
    config.unlink()
    worker.groups = [group(first, "one-original"), group(second, "two-original")]
    assert reopened.refresh(worker, sources) == {first}


def test_nonsemantic_configuration_change_does_not_dirty_java(tmp_path):
    source, private, config = tmp_path / "App.java", tmp_path / "src/App.java", tmp_path / "lombok.config"
    config.write_text("config.stopBubbling=true")
    worker = Worker([{"directory": str(tmp_path), "fingerprint": "same", "inputs": [str(config)]}])
    tracker = LombokConfigurationInputs(tmp_path, enabled=True)
    assert tracker.refresh(worker, {source: private}) == {source}
    tracker.built()
    config.write_text("# only a comment\nconfig.stopBubbling=true")
    assert not tracker.refresh(worker, {source: private})
    assert len(worker.calls) == 2
    assert not tracker.refresh(worker, {source: private})
    assert len(worker.calls) == 2


def test_without_lombok_never_calls_worker_or_touches_files(tmp_path):
    tracker = LombokConfigurationInputs(tmp_path / "absent", enabled=False)
    assert not tracker.refresh(None, {tmp_path / "App.java": tmp_path / "private/App.java"}, force=True)
    assert not (tmp_path / "absent").exists()
