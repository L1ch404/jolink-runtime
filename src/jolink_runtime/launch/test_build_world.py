"""Build-system-neutral authority model consumed by Fast Test."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any, Protocol, Sequence


@dataclass(frozen=True)
class JavaTestBuildWorld:
    build_system: str
    project_root: Path
    module_root: Path
    main_source_roots: tuple[Path, ...]
    test_source_roots: tuple[Path, ...]
    main_output: Path
    test_output: Path
    main_dependencies: tuple[Path, ...]
    test_dependencies: tuple[Path, ...]
    test_runtime_classpath: tuple[Path, ...]
    resource_roots: tuple[Path, ...]
    target_java_home: Path
    source_encoding: str
    source_level: int
    method_parameters: bool
    processor_entries: tuple[Path, ...]
    java_agents: tuple[str, ...]
    extra_worker_jvm_arguments: tuple[str, ...]
    test_java_executable: Path
    test_framework: str | None
    test_working_directory: Path
    test_classes_directories: tuple[Path, ...]
    runner_environment: dict[str, str]
    javac_executable: Path
    configuration_inputs: tuple[Path, ...]
    configuration_environment_names: tuple[str, ...]
    upstream_source_roots: tuple[Path, ...] = ()
    runner_support_provenance: dict[str, object] = field(default_factory=dict)
    native_resource_oracle_required: bool = False
    expected_input_manifest: dict[str, str] = field(default_factory=dict)


class TestBuildWorldBootstrap(Protocol):
    kind: str

    def detect(self, project_path: Path) -> bool:
        """Return whether this provider owns the supplied project root."""

    def bootstrap(self, manager: Any, attempt: Any) -> Any:
        """Create the build world using the selected provider."""


@dataclass(frozen=True)
class MavenTestBuildWorldBootstrap:
    kind: str = "maven"

    def detect(self, project_path: Path) -> bool:
        return (project_path / "pom.xml").is_file()

    def bootstrap(self, manager: Any, attempt: Any) -> Any:
        return manager._bootstrap_maven(attempt)


@dataclass(frozen=True)
class GradleTestBuildWorldBootstrap:
    kind: str = "gradle"

    def detect(self, project_path: Path) -> bool:
        return (project_path / "gradlew").is_file() and any(
            (project_path / name).is_file()
            for name in ("build.gradle", "build.gradle.kts")
        )

    def bootstrap(self, manager: Any, attempt: Any) -> Any:
        return manager._bootstrap_gradle(attempt)


def ordered_existing_paths(values: Sequence[str]) -> tuple[Path, ...]:
    """Resolve existing paths without changing producer order."""

    result: list[Path] = []
    for value in values:
        path = Path(value).expanduser().resolve(strict=False)
        if path.exists():
            result.append(path)
    return tuple(result)


def build_input_manifest(
    source_roots: Sequence[Path],
    resource_roots: Sequence[Path],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for kind, roots in (("source", source_roots), ("resource", resource_roots)):
        for index, root in enumerate(roots):
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    key = f"{kind}/{index}/{path.relative_to(root).as_posix()}"
                    result[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


__all__ = [
    "JavaTestBuildWorld",
    "TestBuildWorldBootstrap",
    "MavenTestBuildWorldBootstrap",
    "GradleTestBuildWorldBootstrap",
    "ordered_existing_paths",
    "build_input_manifest",
]
