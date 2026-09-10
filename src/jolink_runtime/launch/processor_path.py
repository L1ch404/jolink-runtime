"""Maven-exported processor paths shared by launch and Fast Test."""

from pathlib import Path

from .maven import MavenBuildSystemAdapter


def processor_path(processing: dict) -> tuple[Path, ...]:
    return tuple(
        Path(str(value)).resolve(strict=True)
        for value in processing.get(
            "processorPath", processing.get("processorProviderArtifactPaths", ())
        )
    )


def jdt_processor_paths(
    processing: dict,
    paths: tuple[Path, ...],
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    lombok = tuple(
        path for path in paths if MavenBuildSystemAdapter._jdt_dependency_facts(path)[2]
    )
    # Explicit paths are loading paths: keep helper JARs even without a
    # Processor service, exactly as Eclipse Factory Path does.
    # Agent transformation and APT initialization are distinct responsibilities.
    # Keep an explicit Factory Path intact; retain the old implicit-path behavior.
    factories = (
        paths
        if processing.get("discoveryMode") == "EXPLICIT_PROCESSOR_PATH"
        else tuple(path for path in paths if path not in lombok)
    )
    return factories, lombok
