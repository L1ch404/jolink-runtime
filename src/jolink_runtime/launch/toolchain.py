"""Pure discovery and operation specs for Java and Maven toolchains."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .contracts import BuildOperationSpec
from .idea_environment import IdeaBuildPreferences


_IS_WINDOWS = os.name == "nt"


@dataclass(frozen=True)
class JavaToolchainCandidate:
    home: Path
    java_executable: Path
    javac_executable: Path
    source: str
    detected_major_version: int | None = None
    detected_compiler_major_version: int | None = None

    @property
    def has_runtime(self) -> bool:
        return self.java_executable.is_file()

    @property
    def has_compiler(self) -> bool:
        return self.javac_executable.is_file()

    @property
    def major_version(self) -> int | None:
        """Return the probed or static JDK/JRE major.

        PATH can resolve through a platform launcher such as macOS
        ``/usr/bin/java`` whose parent is not a real JAVA_HOME.  Project launch
        therefore records the supervised ``java -version`` result when
        available and falls back to JAVA_HOME/release for ordinary layouts.
        Fast compilation fails closed when neither source can prove the
        platform version.
        """
        if self.detected_major_version is not None:
            return self.detected_major_version
        release_file = self.home / "release"
        try:
            text = release_file.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            return None
        match = re.search(
            r'(?m)^JAVA_VERSION\s*=\s*"([^"]+)"\s*$',
            text,
        )
        if match is None:
            return None
        version = match.group(1).strip()
        if version.startswith("1."):
            match = re.match(r"1\.(\d+)", version)
        else:
            match = re.match(r"(\d+)", version)
        if match is None:
            return None
        try:
            major = int(match.group(1))
        except ValueError:
            return None
        return major if major > 0 else None

    @staticmethod
    def parse_major_version_output(output: str) -> int | None:
        """Parse common Oracle/OpenJDK ``java -version`` output."""
        match = re.search(
            r'(?im)(?:java|openjdk)\s+version\s+"([^"]+)"',
            output,
        )
        if match is None:
            match = re.search(
                r"(?im)^openjdk\s+([0-9][^\s]*)",
                output,
            )
        if match is None:
            return None
        version = match.group(1).strip()
        if version.startswith("1."):
            major_match = re.match(r"1\.(\d+)", version)
        else:
            major_match = re.match(r"(\d+)", version)
        if major_match is None:
            return None
        try:
            major = int(major_match.group(1))
        except ValueError:
            return None
        return major if major > 0 else None

    @property
    def compiler_major_version(self) -> int | None:
        """Return the probed javac major, then static JDK metadata."""
        if self.detected_compiler_major_version is not None:
            return self.detected_compiler_major_version
        return self.major_version

    @staticmethod
    def parse_compiler_major_version_output(output: str) -> int | None:
        """Parse common ``javac -version`` output."""
        match = re.search(r"(?im)^javac\s+([0-9][^\s]*)", output)
        if match is None:
            return None
        version = match.group(1).strip()
        if version.startswith("1."):
            major_match = re.match(r"1\.(\d+)", version)
        else:
            major_match = re.match(r"(\d+)", version)
        if major_match is None:
            return None
        try:
            major = int(major_match.group(1))
        except ValueError:
            return None
        return major if major > 0 else None


@dataclass(frozen=True)
class MavenToolCandidate:
    argv_prefix: tuple[str, ...]
    source: str
    home: Path | None = None

    @property
    def display_executable(self) -> str:
        return Path(self.argv_prefix[-1]).name if self.argv_prefix else ""


class JavaToolchainResolver:
    """Return ordered candidates; validation remains supervised."""

    def candidates(
        self,
        *,
        preferences: IdeaBuildPreferences,
        explicit_reference: str | None,
        for_build: bool,
    ) -> tuple[JavaToolchainCandidate, ...]:
        references: list[tuple[Path, str]] = []
        preferred_name = (
            preferences.maven_runner_jdk_name if for_build else explicit_reference
        )
        if preferred_name in {"#USE_PROJECT_JDK", "Project JDK"}:
            preferred_name = preferences.project_jdk_name
        if preferred_name == "#JAVA_HOME":
            java_home = os.environ.get("JAVA_HOME")
            if java_home:
                references.append(
                    (Path(java_home).expanduser(), "JAVA_HOME")
                )
            return self._to_candidates(references)

        authoritative_reference = bool(preferred_name)
        if preferred_name and self._looks_like_path(preferred_name):
            references.append(
                (Path(preferred_name).expanduser(), "idea_explicit_jdk")
            )
        elif preferred_name:
            references.extend(
                (home, "idea_named_jdk")
                for home in preferences.jdk_homes_by_name.get(
                    preferred_name,
                    (),
                )
            )

        if (
            for_build
            and not authoritative_reference
            and preferences.project_jdk_name
            and preferences.project_jdk_name != preferred_name
        ):
            authoritative_reference = True
            references.extend(
                (home, "idea_project_jdk")
                for home in preferences.jdk_homes_by_name.get(
                    preferences.project_jdk_name,
                    (),
                )
            )
        elif (
            not for_build
            and not explicit_reference
            and preferences.project_jdk_name
        ):
            authoritative_reference = True
            references.extend(
                (home, "idea_project_jdk")
                for home in preferences.jdk_homes_by_name.get(
                    preferences.project_jdk_name,
                    (),
                )
            )

        # IDEA's explicit alternative JRE, Maven runner JRE, and project JDK
        # are authoritative launch intent. Falling back to another installed
        # Java can silently change bytecode compatibility and runtime behavior.
        if authoritative_reference:
            return self._to_candidates(references)

        java_home = os.environ.get("JAVA_HOME")
        if java_home:
            references.append(
                (Path(java_home).expanduser(), "JAVA_HOME")
            )
        java_on_path = shutil.which("java.exe" if _IS_WINDOWS else "java")
        if java_on_path:
            references.append(
                (Path(java_on_path).resolve().parent.parent, "PATH")
            )

        return self._to_candidates(references)

    @staticmethod
    def _to_candidates(
        references: list[tuple[Path, str]],
    ) -> tuple[JavaToolchainCandidate, ...]:
        candidates: list[JavaToolchainCandidate] = []
        seen: set[str] = set()
        for home, source in references:
            normalized = home.resolve(strict=False)
            key = os.path.normcase(str(normalized))
            if key in seen:
                continue
            seen.add(key)
            executable_suffix = ".exe" if _IS_WINDOWS else ""
            candidates.append(
                JavaToolchainCandidate(
                    home=normalized,
                    java_executable=(
                        normalized / "bin" / f"java{executable_suffix}"
                    ),
                    javac_executable=(
                        normalized / "bin" / f"javac{executable_suffix}"
                    ),
                    source=source,
                )
            )
        return tuple(candidates)

    @staticmethod
    def probe_spec(
        candidate: JavaToolchainCandidate,
        *,
        cwd: Path,
        output_capture: Path,
        operation_name: str,
    ) -> BuildOperationSpec:
        return BuildOperationSpec(
            argv=(str(candidate.java_executable), "-version"),
            cwd=cwd,
            timeout_seconds=15.0,
            output_capture=output_capture,
            operation_name=operation_name,
        )

    @staticmethod
    def compiler_probe_spec(
        candidate: JavaToolchainCandidate,
        *,
        cwd: Path,
        output_capture: Path,
        operation_name: str,
    ) -> BuildOperationSpec:
        return BuildOperationSpec(
            argv=(str(candidate.javac_executable), "-version"),
            cwd=cwd,
            timeout_seconds=15.0,
            output_capture=output_capture,
            operation_name=operation_name,
        )

    @staticmethod
    def maven_environment(
        candidate: JavaToolchainCandidate,
    ) -> dict[str, str]:
        inherited_path = os.environ.get("PATH", "")
        environment = {
            "PATH": os.pathsep.join(
                [
                    str(candidate.java_executable.parent),
                    inherited_path,
                ]
            ),
        }
        # PATH may point at the macOS /usr/bin launcher rather than a true
        # JAVA_HOME. Preserve Maven's own discovery in that fallback case.
        if candidate.source != "PATH":
            environment["JAVA_HOME"] = str(candidate.home)
        return environment

    @staticmethod
    def _looks_like_path(reference: str) -> bool:
        return (
            "/" in reference
            or "\\" in reference
            or reference.startswith((".", "~"))
            or (len(reference) >= 2 and reference[1] == ":")
        )


class MavenToolResolver:
    """Resolve Maven command candidates without invoking them."""

    def candidates(
        self,
        *,
        project_root: Path,
        preferences: IdeaBuildPreferences,
    ) -> tuple[MavenToolCandidate, ...]:
        candidates: list[MavenToolCandidate] = []

        if preferences.custom_maven_home is not None:
            executable = self._home_executable(
                preferences.custom_maven_home
            )
            candidates.append(
                MavenToolCandidate(
                    argv_prefix=(str(executable),),
                    source="idea_custom_maven",
                    home=preferences.custom_maven_home,
                )
            )

        wrapper = project_root / ("mvnw.cmd" if _IS_WINDOWS else "mvnw")
        if wrapper.is_file():
            if _IS_WINDOWS or os.access(wrapper, os.X_OK):
                prefix = (str(wrapper),)
            else:
                prefix = ("/bin/sh", str(wrapper))
            candidates.append(
                MavenToolCandidate(
                    argv_prefix=prefix,
                    source="project_wrapper",
                    home=None,
                )
            )

        for variable in ("MAVEN_HOME", "M2_HOME"):
            value = os.environ.get(variable)
            if value:
                home = Path(value).expanduser().resolve(strict=False)
                candidates.append(
                    MavenToolCandidate(
                        argv_prefix=(str(self._home_executable(home)),),
                        source=variable,
                        home=home,
                    )
                )

        executable = shutil.which("mvn.cmd" if _IS_WINDOWS else "mvn")
        if executable:
            candidates.append(
                MavenToolCandidate(
                    argv_prefix=(str(Path(executable).resolve()),),
                    source="PATH",
                    home=None,
                )
            )
        return tuple(self._deduplicate(candidates))

    @staticmethod
    def probe_spec(
        candidate: MavenToolCandidate,
        *,
        cwd: Path,
        environment: dict[str, str],
        output_capture: Path,
    ) -> BuildOperationSpec:
        timeout = (
            120.0
            if candidate.source == "project_wrapper"
            else 15.0
        )
        return BuildOperationSpec(
            argv=(*candidate.argv_prefix, "--version"),
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout,
            output_capture=output_capture,
            operation_name="maven_probe",
        )

    @staticmethod
    def _home_executable(home: Path) -> Path:
        return home / "bin" / ("mvn.cmd" if _IS_WINDOWS else "mvn")

    @staticmethod
    def _deduplicate(
        candidates: Iterable[MavenToolCandidate],
    ) -> list[MavenToolCandidate]:
        unique: list[MavenToolCandidate] = []
        seen: set[tuple[str, ...]] = set()
        for candidate in candidates:
            key = tuple(
                os.path.normcase(str(Path(value).resolve(strict=False)))
                if index == len(candidate.argv_prefix) - 1
                else value
                for index, value in enumerate(candidate.argv_prefix)
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return unique


__all__ = [
    "JavaToolchainCandidate",
    "JavaToolchainResolver",
    "MavenToolCandidate",
    "MavenToolResolver",
]
