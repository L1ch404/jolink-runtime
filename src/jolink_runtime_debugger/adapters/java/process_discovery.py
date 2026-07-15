"""Discover local Java processes without writing to stdout.

The implementation preserves the process discovery behavior from joLink's
Hermes ``java-monitor`` plugin: prefer ``jps``, fall back to ``ps`` when
``jps`` is unavailable, optionally include JVM arguments, and support
case-insensitive main-class or exact-PID filtering.

Stdout is reserved for the MCP stdio transport.  Diagnostics therefore use
the standard logging module and never include discovered command-line values.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any


logger = logging.getLogger(__name__)


def _find_jps() -> str:
    """Locate the ``jps`` binary.

    Search order is ``JAVA_HOME``, the active macOS JDK, common Homebrew
    locations, then ``jps`` on ``PATH``.
    """
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        jps_path = os.path.join(java_home, "bin", "jps")
        if os.path.isfile(jps_path):
            return jps_path

    if os.path.isfile("/usr/libexec/java_home"):
        try:
            result = subprocess.run(
                ["/usr/libexec/java_home"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            jdk_path = result.stdout.strip()
            if jdk_path:
                jps_path = os.path.join(jdk_path, "bin", "jps")
                if os.path.isfile(jps_path):
                    return jps_path
        except Exception:
            logger.debug("macOS Java home discovery failed", exc_info=True)

    for prefix in ("/opt/homebrew/opt/openjdk", "/usr/local/opt/openjdk"):
        jps_path = os.path.join(prefix, "bin", "jps")
        if os.path.isfile(jps_path):
            return jps_path

    return "jps"


def _resolve_macos_java_home() -> str | None:
    """Return the active macOS JDK path, when available."""
    if os.path.isfile("/usr/libexec/java_home"):
        try:
            result = subprocess.run(
                ["/usr/libexec/java_home"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip() or None
        except Exception:
            logger.debug("macOS Java home discovery failed", exc_info=True)
    return None


def _detect_runtime() -> str:
    """Detect the local Java distribution and version."""
    configured_java_home = os.environ.get("JAVA_HOME")
    java_home = configured_java_home or _resolve_macos_java_home()
    java_bin = os.path.join(java_home, "bin", "java") if java_home else "java"

    try:
        result = subprocess.run(
            [java_bin, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        logger.debug("Java runtime detection failed", exc_info=True)
        return "Java"

    # ``java -version`` normally writes to stderr.
    output = (result.stderr + result.stdout).strip()
    lines = output.split("\n")
    if not lines:
        return "Java"

    version_match = re.search(r'version\s+"?([\d._]+)', output)
    version = version_match.group(1) if version_match else "?"
    runtime_line = lines[1].strip() if len(lines) > 1 else ""
    runtime_lower = runtime_line.lower()

    distributions = (
        ("zulu", "Zulu"),
        ("temurin", "Temurin"),
        ("corretto", "Corretto"),
        ("graalvm", "GraalVM"),
        ("liberica", "Liberica"),
        ("sapmachine", "SAP Machine"),
        ("openjdk", "OpenJDK"),
        ("java(tm)", "Oracle JDK"),
    )
    for marker, name in distributions:
        if marker in runtime_lower:
            return f"{name} {version}"
    return f"Java {version}"


_runtime_cache: str | None = None


def _get_runtime() -> str:
    """Return the detected Java runtime, cached for this Python process."""
    global _runtime_cache
    if _runtime_cache is None:
        _runtime_cache = _detect_runtime()
    return _runtime_cache


def _run_jps(full: bool) -> list[dict[str, Any]]:
    """Run ``jps -l`` or ``jps -lv`` and parse its process list."""
    jps_bin = _find_jps()
    runtime = _get_runtime()
    processes: list[dict[str, Any]] = []
    try:
        result = subprocess.run(
            [jps_bin, "-lv" if full else "-l"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            pid = parts[0]
            class_and_args = parts[1].split(None, 1)
            entry: dict[str, Any] = {
                "pid": int(pid),
                "main_class": class_and_args[0],
                "runtime": runtime,
            }
            if full and len(class_and_args) > 1:
                entry["jvm_args"] = class_and_args[1]
            processes.append(entry)
        return processes
    except FileNotFoundError:
        raise
    except Exception as exc:
        logger.debug("jps discovery failed: %s", exc)
        return []


def _run_ps(_full: bool = False) -> list[dict[str, Any]]:
    """Run ``ps aux`` and return commands containing ``java``."""
    runtime = _get_runtime()
    processes: list[dict[str, Any]] = []
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.split("\n"):
            if "java" not in line.lower() or "grep" in line:
                continue
            parts = line.split(None, 10)
            if len(parts) < 11:
                continue
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            processes.append(
                {
                    "pid": pid,
                    "main_class": parts[10][:200],
                    "runtime": runtime,
                }
            )
        return processes
    except Exception as exc:
        logger.debug("ps discovery failed: %s", exc)
        return []


def discover_java_processes(
    filter_text: str | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Return local Java processes using the existing joLink result shape.

    ``filter_text`` performs a case-insensitive substring match against the
    main class or an exact string match against the PID.
    """
    logger.info(
        "java_processes.discovery.start filtered=%s full=%s",
        bool(filter_text),
        full,
    )
    source = "jps"
    try:
        processes = _run_jps(full=full)
    except FileNotFoundError:
        source = "ps"
        processes = _run_ps()

    if filter_text:
        normalized_filter = filter_text.lower()
        processes = [
            process
            for process in processes
            if normalized_filter in process.get("main_class", "").lower()
            or normalized_filter == str(process.get("pid", ""))
        ]

    suffix = f" matching '{filter_text}'" if filter_text else ""
    if not processes:
        result = {
            "message": f"No Java processes found{suffix}. Is a JVM running?",
            "processes": [],
            "count": 0,
        }
    else:
        result = {
            "message": f"Found {len(processes)} Java process(es){suffix}",
            "processes": processes,
            "count": len(processes),
        }

    logger.info(
        "java_processes.discovery.finish source=%s count=%s",
        source,
        result["count"],
    )
    return result


__all__ = ["discover_java_processes"]
