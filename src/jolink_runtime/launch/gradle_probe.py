"""Content-checked Gradle init Probe assets and private model protocol."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class GradleProbeError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class PreparedGradleProbe:
    probe_jar: Path
    init_script: Path
    output_file: Path
    request_id: str
    probe_sha256: str
    task_name: str
    scope: str = "test"


class ProductGradleProbe:
    def __init__(self, lock: dict[str, Any], raw: bytes, init: bytes) -> None:
        self.lock = dict(lock)
        self.raw = bytes(raw)
        self.init = bytes(init)
        self.sha256 = str(lock["sha256"])
        self.version = str(lock["probe_version"])
        self.supported_versions = tuple(
            str(value) for value in lock["supported_gradle_versions"]
        )
        if hashlib.sha256(raw).hexdigest() != self.sha256:
            raise GradleProbeError(
                "GRADLE_PROBE_ASSET_INTEGRITY_MISMATCH",
                "The bundled Gradle Probe does not match its lock.",
            )

    @classmethod
    def load(cls) -> "ProductGradleProbe":
        package = Path(__file__).parent
        try:
            lock = json.loads(
                (package / "gradle-build-world-probe-lock.json").read_text(
                    encoding="utf-8"
                )
            )
            raw = base64.b64decode(
                (package / "gradle-build-world-probe.jar.b64").read_text(
                    encoding="ascii"
                ),
                validate=False,
            )
            init = (package / "gradle-init.gradle").read_bytes()
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            raise GradleProbeError(
                "GRADLE_PROBE_ASSETS_UNAVAILABLE",
                "The bundled Gradle Probe assets are unavailable.",
            ) from error
        return cls(lock, raw, init)

    def prepare(
        self,
        attempt_directory: Path,
        *,
        scope: str = "test",
    ) -> PreparedGradleProbe:
        if scope not in {"test", "runtime"}:
            raise GradleProbeError(
                "GRADLE_PROBE_SCOPE_INVALID",
                "The Gradle Probe scope is invalid.",
            )
        attempt_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache = Path.home() / ".cache/jolink-runtime/gradle-probe" / self.sha256
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        probe = cache / "jolink-gradle-probe.jar"
        if (
            not probe.is_file()
            or hashlib.sha256(probe.read_bytes()).hexdigest() != self.sha256
        ):
            temporary = probe.with_name(f".{probe.name}.{os.getpid()}.tmp")
            temporary.write_bytes(self.raw)
            os.replace(temporary, probe)
        init = attempt_directory / "gradle-init.private.gradle"
        init.write_bytes(self.init)
        try:
            init.chmod(0o600)
        except OSError:
            pass
        output = attempt_directory / "gradle-build-world.private.json"
        request_id = f"req_{secrets.token_hex(12)}"
        return PreparedGradleProbe(
            probe_jar=probe,
            init_script=init,
            output_file=output,
            request_id=request_id,
            probe_sha256=self.sha256,
            task_name=f"jolinkExportBuildWorld_{self.sha256[:12]}",
            scope=scope,
        )

    def command(
        self,
        *,
        wrapper: Path,
        prepared: PreparedGradleProbe,
        offline: bool,
        scope: str | None = None,
    ) -> tuple[str, ...]:
        scope = prepared.scope if scope is None else scope
        if scope not in {"test", "runtime"}:
            raise GradleProbeError(
                "GRADLE_PROBE_SCOPE_INVALID",
                "The Gradle Probe scope is invalid.",
            )
        command = [
            str(wrapper),
            "--no-configuration-cache",
            "--stacktrace",
            "-I",
            str(prepared.init_script),
            f"-Djolink.gradle.probeJar={prepared.probe_jar}",
            f"-Djolink.gradle.output={prepared.output_file}",
            "-Djolink.gradle.targetProject=:",
            f"-Djolink.gradle.requestId={prepared.request_id}",
            f"-Djolink.gradle.probeSha256={prepared.probe_sha256}",
            "-Djolink.gradle.slowCompileMillis=0",
            f"-Djolink.gradle.scope={scope}",
        ]
        if offline:
            command.append("--offline")
        command.append(prepared.task_name)
        return tuple(command)

    def load_model(self, prepared: PreparedGradleProbe) -> dict[str, Any]:
        try:
            model = json.loads(prepared.output_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise GradleProbeError(
                "GRADLE_PROBE_OUTPUT_UNAVAILABLE",
                "The Gradle Probe did not produce a valid private model.",
            ) from error
        if model.get("ok") is not True:
            raise GradleProbeError(
                str(model.get("errorCode", "GRADLE_BUILD_WORLD_UNSUPPORTED")),
                str(model.get("message", "The Gradle Build World is unsupported.")),
            )
        if (
            model.get("schema") != "jolink.gradle-build-world-probe.v1"
            or model.get("probeVersion") != self.version
            or model.get("probeSha256") != prepared.probe_sha256
            or model.get("requestId") != prepared.request_id
            or model.get("exportTaskName") != prepared.task_name
            or model.get("targetProjectPath") != ":"
            or model.get("exportScope", "test") != prepared.scope
        ):
            raise GradleProbeError(
                "GRADLE_PROBE_IDENTITY_MISMATCH",
                "The Gradle Probe output has invalid identity.",
            )
        version = str(model.get("gradleVersion", ""))
        if version not in self.supported_versions:
            raise GradleProbeError(
                "GRADLE_VERSION_UNSUPPORTED",
                "This Gradle version has no product evidence.",
            )
        return model

    @staticmethod
    def cleanup(prepared: PreparedGradleProbe) -> None:
        prepared.init_script.unlink(missing_ok=True)
        prepared.output_file.unlink(missing_ok=True)
        prepared.output_file.with_name(
            f"{prepared.output_file.name}.started"
        ).unlink(missing_ok=True)


def wrapper_version(project: Path) -> str:
    properties = project / "gradle/wrapper/gradle-wrapper.properties"
    try:
        text = properties.read_text(encoding="utf-8")
    except OSError as error:
        raise GradleProbeError(
            "GRADLE_WRAPPER_UNAVAILABLE",
            "A Gradle Wrapper is required for Fast Test.",
        ) from error
    match = re.search(r"gradle-([0-9]+\.[0-9]+(?:\.[0-9]+)?)-", text)
    if match is None:
        raise GradleProbeError(
            "GRADLE_WRAPPER_VERSION_UNAVAILABLE",
            "The Gradle Wrapper distribution version is unavailable.",
        )
    return match.group(1)


def gradle_configuration_inputs(project: Path) -> tuple[Path, ...]:
    root = project.expanduser().resolve(strict=True)
    gradle_home = (
        Path(os.environ.get("GRADLE_USER_HOME", str(Path.home() / ".gradle")))
        .expanduser()
        .resolve(strict=False)
    )
    candidates = [
        root / "build.gradle",
        root / "build.gradle.kts",
        root / "settings.gradle",
        root / "settings.gradle.kts",
        root / "gradle.properties",
        root / "gradle/libs.versions.toml",
        root / "gradle/wrapper/gradle-wrapper.properties",
        root / "gradle/wrapper/gradle-wrapper.jar",
        gradle_home / "gradle.properties",
        gradle_home / "init.gradle",
        gradle_home / "init.gradle.kts",
        Path(__file__).parent / "gradle-build-world-probe-lock.json",
    ]
    candidates.extend(root.glob("*.gradle"))
    candidates.extend(root.glob("*.gradle.kts"))
    for directory in (root / "gradle", gradle_home / "init.d"):
        if directory.is_dir():
            candidates.extend(directory.rglob("*.gradle"))
            candidates.extend(directory.rglob("*.gradle.kts"))
    return tuple(dict.fromkeys(path.resolve(strict=False) for path in candidates))


def gradle_configuration_environment_names() -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                "GRADLE_USER_HOME",
                "GRADLE_OPTS",
                "JAVA_OPTS",
                *(
                    name
                    for name in os.environ
                    if name.startswith("ORG_GRADLE_PROJECT_")
                ),
            }
        )
    )


__all__ = [
    "GradleProbeError",
    "PreparedGradleProbe",
    "ProductGradleProbe",
    "gradle_configuration_inputs",
    "gradle_configuration_environment_names",
    "wrapper_version",
]
