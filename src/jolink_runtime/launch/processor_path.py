"""Maven-exported processor paths shared by launch and Fast Test."""

from pathlib import Path

from .compiler_profile import classify_compiler_arguments
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
        or processing.get("explicitProcessorNames")
        else tuple(path for path in paths if path not in lombok)
    )
    return factories, lombok


def processor_settings(processing: dict) -> dict:
    return {
        "processor_names": list(processing.get("explicitProcessorNames", ())),
        "processor_options": classify_compiler_arguments(
            processing.get("options", ())
        ).processor_options,
    }


def write_processor_settings(path: Path, names, options) -> Path:
    """UTF-8 Java Properties in the existing private workspace; flags stay null."""

    def escape(value):
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
            .replace(" ", "\\ ")
            .replace("=", "\\=")
            .replace(":", "\\:")
        )

    lines = ["names=" + escape(",".join(names))]
    lines.extend(
        ("flag." if value is None else "option.")
        + escape(key)
        + "="
        + ("" if value is None else escape(value))
        for key, value in options.items()
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
