"""Select runtime configurations independently of the persistent compiler world."""

import re
import shlex
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path

from .fast_test import FastTestError
from .source_selection import ant_pattern
from .test_output import TestOutputLog
from .toolchain import java_executable_path

# Maven owns batch scheduling/reporting; explicit development runs use one disposable Runner.
_SCHEDULING = {
    "parallel",
    "threadCount",
    "threadCountClasses",
    "threadCountMethods",
    "threadCountSuites",
    "perCoreThreadCount",
    "useUnlimitedThreads",
    "parallelOptimized",
    "forkCount",
    "forkMode",
    "reuseForks",
    "rerunFailingTestsCount",
    "skipAfterFailureCount",
    "testFailureIgnore",
    "parallelTestsTimeoutInSeconds",
    "parallelTestsTimeoutForcedInSeconds",
    "forkedProcessTimeoutInSeconds",
    "forkedProcessExitTimeoutInSeconds",
    "runOrder",
    "runOrderRandomSeed",
    "shutdown",
    "enableProcessChecker",
}
_REPORTING = {
    "summaryFile",
    "disableXmlReport",
    "printSummary",
    "redirectTestOutputToFile",
    "reportFormat",
    "reportsDirectory",
    "trimStackTrace",
    "useFile",
    "reportNameSuffix",
    "encoding",
    "consoleOutputReporter",
    "statelessTestsetReporter",
    "statelessTestsetInfoReporter",
    "enableOutErrElements",
    "enablePropertiesElement",
}
_MODELED = {
    "includes",
    "excludes",
    "includesFile",
    "excludesFile",
    "skip",
    "skipTests",
    "skipITs",
    "test",
    "it.test",
    "failIfNoTests",
    "failIfNoSpecifiedTests",
    "workingDirectory",
    "jvm",
    "enableAssertions",
    "environmentVariables",
    "excludedEnvironmentVariables",
    "argLine",
    "systemProperties",
    "systemPropertyVariables",
    "systemPropertiesFile",
    "additionalClasspathElements",
    "groups",
    "excludedGroups",
    "includeJUnit5Engines",
    "excludeJUnit5Engines",
    "properties",
    "useSystemClassLoader",
    "childDelegation",
}

_DEVELOPMENT_DEFAULTS = {
    "parallel": {"none", "false"},
    "threadCount": {"1"},
    "forkCount": {"1"},
    "forkMode": {"once"},
    "reuseForks": {"true"},
    "rerunFailingTestsCount": {"0"},
    "skipAfterFailureCount": {"0"},
    "testFailureIgnore": {"false"},
}


def _text(config, name, default=""):
    return (config.findtext(name) or default).strip()


def split_jvm_arguments(value):
    # Maven's quoted command line is not a shell program. In particular, do not
    # consume Windows path backslashes as POSIX escape characters.
    parser = shlex.shlex(value, posix=True)
    parser.whitespace_split = True
    parser.commenters = ""
    parser.escape = ""
    return tuple(parser)


def _system_properties(config, field):
    values = {}
    for node in config.findall(field + "/*"):
        if node.tag == "property" and node.find("name") is not None:
            values[node.findtext("name")] = node.findtext("value", "")
        else:
            values[node.tag] = node.text or ""
    return values


def _patterns(config, name):
    values = [(node.text or "").strip() for node in config.findall(f"{name}/*")]
    return [
        part.strip()
        for value in values
        for part in ([value] if value.startswith("%regex[") else value.split(","))
        if part.strip()
    ]


def _matches(pattern, selector):
    class_name, _, method = selector.partition("#")
    path = class_name.replace(".", "/") + ".class"
    if pattern.startswith("%regex[") and pattern.endswith("]"):
        try:
            return re.fullmatch(pattern[7:-1], path) is not None
        except re.error as error:
            raise FastTestError(
                "TEST_SELECTION_PATTERN_UNSUPPORTED",
                "The test execution regex cannot be evaluated by the runtime selector.",
            ) from error
    name, _, method_pattern = pattern.partition("#")
    name = name.removesuffix(".java").removesuffix(".class")
    if "/" not in name:
        name = name.replace(".", "/")
    name = name if name.startswith("**/") else "**/" + name
    return bool(ant_pattern(name + ".class").fullmatch(path)) and (
        not method_pattern
        or bool(
            method
            and any(ant_pattern(p).fullmatch(method) for p in method_pattern.split("+"))
        )
    )


def _handles(execution, config, selector):
    if "configurationPrivate" not in execution:
        return False
    includes = _patterns(config, "includes")
    excludes = _patterns(config, "excludes") + [
        p[1:] for p in includes if p.startswith("!")
    ]
    includes = [p for p in includes if not p.startswith("!")]
    return (not includes or any(_matches(p, selector) for p in includes)) and not any(
        _matches(p, selector) for p in excludes
    )


def execution_groups(executions, selectors):
    """Patterns choose the environment, not whether an explicitly requested test may run."""
    if not executions:
        return [(None, tuple(selectors))]
    parsed = [
        (
            execution,
            ET.fromstring(execution.get("configurationPrivate", "<configuration/>")),
        )
        for execution in executions
    ]
    groups = OrderedDict()
    for selector in selectors:
        selected = next(
            (
                execution
                for execution, config in parsed
                if _handles(execution, config, selector)
            ),
            executions[0],
        )
        groups.setdefault(selected["id"], (selected, []))[1].append(selector)
    return [(execution, tuple(tests)) for execution, tests in groups.values()]


def runtime_options(execution, project):
    if execution is None:
        return {}, []
    if execution.get("configurationError"):
        raise FastTestError(
            "TEST_EXECUTION_CONFIGURATION_INVALID",
            "Maven could not resolve the selected test execution.",
            context={
                "test_execution_id": execution["id"],
                "error_type": execution["configurationError"],
            },
        )
    config = ET.fromstring(execution["configurationPrivate"])
    configured = {node.tag for node in config if (node.text or "").strip() or len(node)}
    unsupported = configured - _MODELED - _SCHEDULING - _REPORTING
    if execution.get("pluginDependencies"):
        unsupported.add("pluginDependencies")
    if _text(config, "childDelegation", "false").lower() == "true":
        unsupported.add("childDelegation")
    if _text(config, "useSystemClassLoader", "true").lower() == "false":
        unsupported.add("useSystemClassLoader")
    properties = config.find("properties")
    if properties is not None:
        unsupported.update(
            "properties." + p.tag
            for p in properties
            if p.tag != "configurationParameters"
        )
    if unsupported:
        raise FastTestError(
            "TEST_EXECUTION_CONFIGURATION_UNSUPPORTED",
            "The selected test execution has unmapped runtime settings.",
            context={
                "test_execution_id": execution["id"],
                "unsupported_configuration_names": sorted(unsupported),
            },
        )
    directory = execution.get("workingDirectory")
    if directory is None:
        raise FastTestError(
            "TEST_WORKING_DIRECTORY_UNAVAILABLE",
            "Maven could not resolve the selected test working directory.",
        )
    # Keep JVM token order: later -D / -ea/-da entries have their native override semantics.
    args = ["-ea" if execution.get("enableAssertions", True) else "-da"]
    args.extend(split_jvm_arguments(_text(config, "argLine")))
    system = _system_properties(config, "systemProperties")
    system.update(execution.get("fileSystemPropertiesPrivate", {}))
    system.update(_system_properties(config, "systemPropertyVariables"))
    args.extend("-D" + key + "=" + value for key, value in system.items())
    framework = {}
    for source, target in (
        ("groups", "groups"),
        ("excludedGroups", "excluded_groups"),
        ("includeJUnit5Engines", "include_engines"),
        ("excludeJUnit5Engines", "exclude_engines"),
    ):
        node = config.find(source)
        if node is not None:
            framework[target] = (
                ",".join(child.text or "" for child in node)
                if len(node)
                else node.text or ""
            )
    value = config.findtext("properties/configurationParameters")
    if value:
        framework["configuration_parameters"] = value
    order = _text(config, "runOrder")
    adjustments = sorted(
        name
        for name in configured & _SCHEDULING
        if _text(config, name)
        and _text(config, name).lower() not in _DEVELOPMENT_DEFAULTS.get(name, set())
        and not (
            name == "runOrder" and order in {"alphabetical", "reversealphabetical"}
        )
    )
    additional = tuple(
        (project.module_root / (node.text or "").strip()).resolve()
        for node in config.findall(
            "additionalClasspathElements/additionalClasspathElement"
        )
        if node.text
    )
    java = _text(config, "jvm")
    return {
        "java_executable": (project.module_root / java).resolve()
        if java
        else project.test_java_executable,
        "working_directory": Path(directory),
        "environment": {
            **project.runner_environment,
            **execution.get("environmentOverridesPrivate", {}),
        },
        "jvm_arguments": tuple(args),
        "run_order": order if order in {"alphabetical", "reversealphabetical"} else "",
        "framework_options": framework,
        "additional_classpath": additional,
    }, adjustments


def run_test_executions(runner, project, attempt, directory):
    groups = execution_groups(getattr(project, "test_executions", ()), attempt.tests)
    deadline = time.monotonic() + attempt.timeout_seconds
    results, notices, ids = [], set(), []
    for index, (execution, selectors) in enumerate(groups):
        attempt.require_not_cancelled()
        options, changes = runtime_options(execution, project)
        # Per-run choice, not compiler state: applies equally to Maven and Gradle
        # without replacing the cached project's native test JVM configuration.
        if getattr(attempt, "java_home", ""):
            options["java_executable"] = java_executable_path(attempt.java_home)
        notices.update(changes)
        if execution:
            ids.append(execution["id"])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FastTestError(
                "TEST_TIMEOUT",
                "The test execution groups exceeded their shared execution timeout.",
            )
        extra = options.pop("additional_classpath", ())
        group_directory = directory if len(groups) == 1 else directory / str(index)
        output_log = TestOutputLog(group_directory / "test.log", (execution or {}).get("id", ""))
        attempt.output_logs += (output_log,)
        result = runner.run(
            **{
                "java_executable": project.test_java_executable,
                "framework": project.test_framework,
                "run_order": project.test_run_order,
                "working_directory": project.test_working_directory,
                "environment": project.runner_environment,
                "jvm_arguments": project.runner_jvm_arguments,
                "framework_options": getattr(project, "framework_options", {}),
                **options,
            },
            classpath=(
                project.compiler.test_output_directory,
                project.compiler.output_directory,
                *project.runtime_classpath,
                *extra,
            ),
            selectors=selectors,
            attempt_directory=group_directory,
            output_log=output_log,
            timeout_seconds=remaining,
            owner=attempt.owner,
        )
        results.append(result)
    first = results[0]
    if len(results) == 1:
        return first, ids, sorted(notices)
    failures = [failure for result in results for failure in result.failures]
    merged = replace(
        first,
        framework=first.framework
        if all(r.framework == first.framework for r in results)
        else "mixed",
        **{
            key: sum(getattr(r, key) for r in results)
            for key in (
                "tests",
                "passed_count",
                "failed_count",
                "failed_test_count",
                "failed_container_count",
                "skipped_count",
                "duration_ms",
            )
        },
        passed=all(r.passed for r in results),
        failures=tuple(failures[:8]),
        failures_truncated=len(failures) > 8
        or any(r.failures_truncated for r in results),
    )
    return merged, ids, sorted(notices)
