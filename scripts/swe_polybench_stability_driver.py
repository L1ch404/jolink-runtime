#!/usr/bin/env python3
"""Run one joLink Fast Test stability roundtrip inside a benchmark image."""

from __future__ import annotations

import argparse
import json
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import psutil

from jolink_runtime.core.dispatcher import Dispatcher


TERMINAL = {"completed", "compile_failed", "failed", "cancelled"}


def _call(dispatcher: Dispatcher, tool: str, arguments: dict[str, Any]) -> dict:
    return dict(dispatcher.dispatch(tool, arguments))


def _wait_test(
    dispatcher: Dispatcher,
    *,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _call(dispatcher, "java_status", {"action": "status"})
        status = str(last.get("fast_test", {}).get("status", last.get("status", "")))
        payload = dict(last.get("fast_test", last))
        if status in TERMINAL or payload.get("passed") is not None:
            return payload
        time.sleep(0.1)
    raise TimeoutError(last)


def _run_test(
    dispatcher: Dispatcher,
    *,
    project: Path,
    selector: str,
    source_files: tuple[str, ...],
    timeout: float,
    build_system: str | None,
) -> dict[str, Any]:
    started = _call(
        dispatcher,
        "java_application",
        {
            "action": "test",
            "project_path": str(project),
            "source_files": list(source_files),
            "tests": [selector],
            "timeout": timeout,
            **(
                {"build_system": build_system}
                if build_system is not None
                else {}
            ),
        },
    )
    if started.get("status") in TERMINAL or started.get("passed") is not None:
        return started
    return _wait_test(dispatcher, timeout=timeout + 30)


def _test_source(
    project: Path,
    class_name: str,
    source_hint: str | None = None,
) -> Path:
    top_level = class_name.split("$", 1)[0]
    suffix = Path(*top_level.split(".")).with_suffix(".java")
    if source_hint:
        hinted = (project / source_hint).resolve(strict=True)
        if project not in hinted.parents:
            raise RuntimeError("TEST_SOURCE_HINT_OUTSIDE_PROJECT")
        if tuple(hinted.parts[-len(suffix.parts) :]) != suffix.parts:
            raise RuntimeError("TEST_SOURCE_HINT_MISMATCH")
        return hinted
    matches = [
        path.resolve()
        for path in project.rglob(suffix.name)
        if path.is_file()
        and tuple(path.parts[-len(suffix.parts) :]) == suffix.parts
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"TEST_SOURCE_AMBIGUOUS:{class_name}:{len(matches)}"
        )
    return matches[0]


def _official_outcome(
    project: Path,
    *,
    class_name: str,
    method_name: str,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for report in project.rglob("TEST-*.xml"):
        if not report.is_file():
            continue
        try:
            root = ET.parse(report).getroot()
        except (ET.ParseError, OSError):
            continue
        for case in root.iter("testcase"):
            candidate_class = str(case.attrib.get("classname", ""))
            candidate_method = str(case.attrib.get("name", ""))
            method_matches = (
                candidate_method == method_name
                or candidate_method.startswith(f"{method_name}[")
                or candidate_method.startswith(f"{method_name}(")
            )
            if candidate_class == class_name and method_matches:
                outcome = "passed"
                if case.find("failure") is not None or case.find("error") is not None:
                    outcome = "failed"
                elif case.find("skipped") is not None:
                    outcome = "skipped"
                matches.append(
                    {
                        "outcome": outcome,
                        "report": str(report.relative_to(project)),
                        "reported_name": candidate_method,
                    }
                )
    if not matches:
        return {"outcome": "not_found", "match_count": 0}
    outcomes = {item["outcome"] for item in matches}
    return {
        "outcome": next(iter(outcomes)) if len(outcomes) == 1 else "ambiguous",
        "match_count": len(matches),
        "matches": matches[:8],
    }


def _compiled_identity(result: dict[str, Any], source: Path) -> bool:
    units = [str(value).replace("\\", "/") for value in result.get(
        "compiled_source_units", []
    )]
    suffix = "/".join(source.parts[-4:])
    return int(result.get("compiled_source_count", 0)) > 0 and any(
        value.endswith(suffix) or value.endswith(source.name) for value in units
    )


def _remaining_owned_processes() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    current = psutil.Process()
    for process in current.children(recursive=True):
        try:
            command = " ".join(process.cmdline())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if any(
            marker in command
            for marker in (
                "net.jolink.runtime.jdt.worker",
                "net.jolink.runtime.test.TestRunner",
                "jolink-fast-test",
            )
        ):
            result.append({"pid": process.pid, "command": command[:512]})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.input.read_text(encoding="utf-8"))
    java_home = config.get("java_home")
    if java_home:
        os.environ["JAVA_HOME"] = str(java_home)
        os.environ["PATH"] = (
            str(Path(str(java_home)) / "bin")
            + os.pathsep
            + os.environ.get("PATH", "")
        )
        os.environ["MAVEN_OPTS"] = (
            os.environ.get("MAVEN_OPTS", "")
            + " -Djdk.tls.client.protocols=TLSv1.2"
            + " -Dhttps.protocols=TLSv1.2"
        ).strip()
    project = Path(config["project_path"]).resolve(strict=True)
    selector = str(config["selector"])
    class_name, separator, method_name = selector.rpartition("#")
    if not separator:
        raise SystemExit("selector must be Class#method")
    source = _test_source(
        project,
        class_name,
        config.get("source_hint"),
    )
    source_relative = source.relative_to(project).as_posix()
    official = _official_outcome(
        project,
        class_name=class_name,
        method_name=method_name,
    )
    report: dict[str, Any] = {
        "instance_id": config["instance_id"],
        "selector": selector,
        "source_file": source_relative,
        "source_selection": (
            "test_patch" if config.get("source_hint") else "unique_search"
        ),
        "source_hint_candidate_count": int(
            config.get("source_hint_candidate_count", 0)
        ),
        "official_selected": official,
        "java_home": java_home,
        "build_system": config.get("build_system"),
        "official_failure_kind": config.get("official_failure_kind"),
        "stages": {},
    }
    official_known = official.get("outcome") in {"passed", "failed"}
    dispatcher = Dispatcher()
    original = source.read_bytes()
    try:
        baseline = _run_test(
            dispatcher,
            project=project,
            selector=selector,
            source_files=(
                (source_relative,)
                if int(config.get("source_hint_candidate_count", 0)) > 1
                else ()
            ),
            timeout=float(config.get("fast_test_timeout", 600)),
            build_system=config.get("build_system"),
        )
        report["stages"]["baseline"] = baseline
        if baseline.get("ok") is not True:
            official_compile_failed = (
                config.get("official_failure_kind") == "compile_failed"
            )
            report.update(
                {
                    "ok": False,
                    "stage": (
                        "official_baseline"
                        if official_compile_failed
                        else "bootstrap"
                    ),
                    "error_code": (
                        "OFFICIAL_BASE_COMPILE_FAILED"
                        if official_compile_failed
                        else baseline.get("error_code", "FAST_TEST_FAILED")
                    ),
                    "jolink_error_code": baseline.get("error_code"),
                    "error": baseline.get("error"),
                }
            )
            return 0
        expected_passed = (
            official.get("outcome") == "passed" if official_known else None
        )
        report["baseline_parity"] = (
            bool(baseline.get("passed")) is expected_passed
            if official_known
            else None
        )

        source.write_bytes(
            original
            + (b"\n" if original and not original.endswith(b"\n") else b"")
            + b"// jolink stability probe\n"
        )
        forward = _run_test(
            dispatcher,
            project=project,
            selector=selector,
            source_files=(source_relative,),
            timeout=float(config.get("fast_test_timeout", 600)),
            build_system=config.get("build_system"),
        )
        report["stages"]["forward"] = forward
        report["forward_compiled_identity"] = _compiled_identity(forward, source)

        source.write_bytes(original)
        reverse = _run_test(
            dispatcher,
            project=project,
            selector=selector,
            source_files=(source_relative,),
            timeout=float(config.get("fast_test_timeout", 600)),
            build_system=config.get("build_system"),
        )
        report["stages"]["reverse"] = reverse
        report["reverse_compiled_identity"] = _compiled_identity(reverse, source)
        report["result_parity"] = (
            baseline.get("passed") == forward.get("passed") == reverse.get("passed")
        )
        report["stability_ok"] = bool(
            forward.get("ok") is True
            and reverse.get("ok") is True
            and report["forward_compiled_identity"]
            and report["reverse_compiled_identity"]
            and report["result_parity"]
        )
        report["ok"] = bool(
            report["stability_ok"]
            and report["baseline_parity"] is True
        )
        if not official_known:
            report["stage"] = "official_baseline"
            report["error_code"] = "OFFICIAL_SELECTED_TEST_UNAVAILABLE"
        elif not report["ok"]:
            report["stage"] = "incremental_roundtrip"
            report["error_code"] = "STABILITY_INVARIANT_FAILED"
    except Exception as error:
        report.update(
            {
                "ok": False,
                "stage": "driver",
                "error_code": type(error).__name__,
                "error": str(error),
            }
        )
    finally:
        source.write_bytes(original)
        try:
            dispatcher.close_all_sessions()
        except Exception as error:
            report["cleanup_error"] = f"{type(error).__name__}: {error}"
        time.sleep(0.2)
        report["remaining_owned_processes"] = _remaining_owned_processes()
        report["cleanup_ok"] = (
            not report["remaining_owned_processes"]
            and "cleanup_error" not in report
        )
        report["ok"] = bool(report.get("ok") and report["cleanup_ok"])
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
