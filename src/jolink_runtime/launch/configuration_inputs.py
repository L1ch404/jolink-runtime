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


def preparation_stamps(paths, roots=()):
    """Generator-declared files/directories: stat only, no repeated content hashes."""
    result = {}
    for path in expand_configuration_inputs(dict.fromkeys(paths)):
        try:
            stat = path.stat()
            result[str(path)] = (
                [stat.st_mtime_ns, stat.st_size] if path.is_file() else "directory"
            )
        except FileNotFoundError:
            result[str(path)] = "missing"
    result.update({"root:" + str(path): Path(path).is_dir() for path in roots})
    return result
