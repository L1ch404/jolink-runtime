"""Shared file/directory inputs for the existing Build World caches."""

import hashlib
from pathlib import Path


def expand_configuration_inputs(paths):
    for value in paths:
        path = Path(value)
        yield path
        if path.is_dir():
            yield from sorted(item for item in path.rglob("*") if item.is_file())


def configuration_file_stamps(paths):
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        if path.is_file()
        else "directory"
        if path.is_dir()
        else "missing"
        for path in expand_configuration_inputs(paths)
    }
