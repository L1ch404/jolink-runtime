"""Shared Build World identity; preserve existing persisted fingerprints."""

import hashlib
import os
from pathlib import Path
from typing import Iterable
from .configuration_inputs import expand_configuration_inputs


def build_world_fingerprint(
    *,
    configuration_inputs: Iterable[Path],
    configuration_environment_names: Iterable[str] = (),
    javac_executable: Path,
    compile_classpath: Iterable[Path],
) -> str:
    """Hash configuration content and dependency identity, never source output."""
    digest = hashlib.sha256()
    for path in sorted(
        (Path(item).resolve(strict=False) for item in expand_configuration_inputs(configuration_inputs)),
        key=lambda item: os.path.normcase(str(item)),
    ):
        digest.update(b"config\0")
        digest.update(str(path).encode("utf-8", errors="surrogateescape"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    for name in sorted(set(configuration_environment_names)):
        digest.update(b"environment\0")
        digest.update(name.encode("utf-8", errors="surrogateescape"))
        value = os.environ.get(name)
        if value is None:
            digest.update(b"<unset>")
        else:
            # Environment values can contain credentials. They participate in
            # the in-memory digest but are never retained in the plan or
            # exposed through Runtime results.
            digest.update(value.encode("utf-8", errors="surrogateescape"))
    javac = javac_executable.resolve(strict=False)
    digest.update(b"javac\0")
    digest.update(str(javac).encode("utf-8", errors="surrogateescape"))
    try:
        stat = javac.stat()
        digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
    except OSError:
        digest.update(b"<missing>")
    for path in compile_classpath:
        normalized = Path(path).resolve(strict=False)
        digest.update(b"classpath\0")
        digest.update(str(normalized).encode("utf-8", errors="surrogateescape"))
        if normalized.is_file():
            try:
                stat = normalized.stat()
                digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
            except OSError:
                digest.update(b"<missing>")
        elif normalized.is_dir():
            # Workspace output directories are mutable compile inputs. Hash
            # their class contents so an external Maven/IDE build cannot make
            # javac see a newer dependency generation than the running JVM.
            try:
                class_files = sorted(
                    normalized.rglob("*.class"),
                    key=lambda item: os.path.normcase(
                        item.relative_to(normalized).as_posix()
                    ),
                )
            except OSError:
                digest.update(b"<unreadable-directory>")
                continue
            for class_file in class_files:
                try:
                    relative = class_file.relative_to(normalized).as_posix()
                    digest.update(b"class\0")
                    digest.update(relative.encode("utf-8", errors="surrogateescape"))
                    digest.update(class_file.read_bytes())
                except OSError:
                    digest.update(b"<unreadable-class>")
        else:
            digest.update(b"<missing>")
    return digest.hexdigest()
