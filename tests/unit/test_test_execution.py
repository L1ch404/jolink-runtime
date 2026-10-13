from pathlib import Path
from types import SimpleNamespace

import pytest

from jolink_runtime.launch.fast_test import FastTestError, FastTestResult
from jolink_runtime.launch.test_execution import (
    execution_groups,
    run_test_executions,
    runtime_options,
    split_jvm_arguments,
)


def test_jvm_tokens_keep_windows_paths_and_quoted_values():
    assert split_jvm_arguments(
        r'-Xss256k -Dpath=C:\work\fixture -Dlabel="中文 value" -Dempty=""'
    ) == ("-Xss256k", r"-Dpath=C:\work\fixture", "-Dlabel=中文 value", "-Dempty=")


def test_file_properties_override_legacy_properties(project):
    e = execution(
        extra="<systemProperties><property><name>value</name><value>legacy</value></property></systemProperties>",
        fileSystemPropertiesPrivate={"value": "file"},
    )
    assert "-Dvalue=file" in runtime_options(e, project)[0]["jvm_arguments"]


def test_modern_property_container_accepts_named_property_entries(project):
    e = execution(
        extra="<systemPropertyVariables>\n<property><name>path</name><value>folder</value></property>\n</systemPropertyVariables>"
    )
    assert runtime_options(e, project)[0]["jvm_arguments"] == ("-ea", "-Dpath=folder")


def test_development_policy_reports_fork_isolation_changes(project):
    e = execution(
        extra="<forkCount>0</forkCount><reuseForks>false</reuseForks><parallel>none</parallel>"
    )
    assert runtime_options(e, project)[1] == ["forkCount", "reuseForks"]


def test_method_patterns_and_negative_includes_choose_execution():
    unit = execution(patterns="**/UnitTest.java")
    it = execution("it", "**/FooIT#one+two,!**/FooIT#blocked")
    assert execution_groups((unit, it), ("a.FooIT#two",))[0][0] is it
    assert execution_groups((unit, it), ("a.FooIT#blocked",))[0][0] is unit


def test_execution_groups_stop_on_cancellation(project):
    project.test_executions = (execution(), execution("it", "**/*IT.java"))
    calls = []

    def check():
        if calls:
            raise FastTestError("TEST_CANCELLED", "cancelled between groups")

    def run(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace()

    attempt = SimpleNamespace(
        tests=("x.FooTest", "x.FooIT"),
        output_logs=(),
        timeout_seconds=30,
        owner=object(),
        require_not_cancelled=check,
    )
    with pytest.raises(FastTestError) as error:
        run_test_executions(
            SimpleNamespace(run=run), project, attempt, project.module_root / "run"
        )
    assert error.value.error_code == "TEST_CANCELLED" and len(calls) == 1


def execution(name="unit", patterns="**/*Test.java", extra="", **values):
    return {
        "id": name,
        "kind": "surefire" if name == "unit" else "failsafe",
        "configurationPrivate": f"<configuration><includes><include>{patterns}</include></includes>{extra}</configuration>",
        "workingDirectory": "/tmp",
        "enableAssertions": True,
        **values,
    }


@pytest.fixture
def project(tmp_path):
    return SimpleNamespace(
        module_root=tmp_path,
        test_java_executable=tmp_path / "java",
        runner_environment={"INHERIT": "yes", "REMOVE": "old"},
        runner_jvm_arguments=(),
        test_framework=None,
        test_run_order="",
        test_working_directory=tmp_path,
        compiler=SimpleNamespace(
            test_output_directory=tmp_path / "tests",
            output_directory=tmp_path / "classes",
        ),
        runtime_classpath=(),
        test_executions=(),
    )


@pytest.mark.parametrize(
    "selectors", [("x.FooTest#one",), ("x.FooTest",), ("x.FooTest", "x.BarTest")]
)
def test_development_scheduling_does_not_depend_on_selector_count(project, selectors):
    e = execution(
        extra="<parallel>methods</parallel><threadCount>8</threadCount><forkCount>4</forkCount>"
    )
    assert execution_groups((e,), selectors) == [(e, selectors)]
    _, notes = runtime_options(e, project)
    assert notes == ["forkCount", "parallel", "threadCount"]


@pytest.mark.parametrize(
    "pattern,selector",
    [
        ("**/*IT.java", "a.b.SampleIT"),
        ("**/FooTest.class", "a.FooTest#one"),
        ("a.b.FooTest", "a.b.FooTest"),
        ("**/FooTest#one", "a.FooTest#one"),
        ("%regex[.*/.*IT.class]", "a.b.SampleIT"),
    ],
)
def test_resolved_discovery_rules_route_environment(pattern, selector):
    unit, integration = execution(patterns="**/Unit.java"), execution("it", pattern)
    assert execution_groups((unit, integration), (selector,)) == [
        (integration, (selector,))
    ]


def test_explicit_tests_not_silently_filtered_and_environment_not_merged(project):
    unit = execution(extra="<excludes><exclude>**/FooTest.java</exclude></excludes>")
    it = execution("it", "**/*IT.java", configurationError="IOException")
    assert execution_groups((unit, it), ("a.FooTest",)) == [(unit, ("a.FooTest",))]
    runtime_options(
        unit, project
    )  # Irrelevant broken Failsafe does not veto unit tests.
    with pytest.raises(FastTestError, match="resolve"):
        runtime_options(it, project)


def test_runtime_values_are_preserved_and_report_settings_do_not_block(project):
    e = execution(
        extra="""<argLine>-Xss256k -Dfixture="two words" -da</argLine>
<systemPropertyVariables><label> 中文 = text </label></systemPropertyVariables>
<additionalClasspathElements><additionalClasspathElement>fixtures</additionalClasspathElement></additionalClasspathElements>
<groups>fast | unit</groups><excludedGroups>slow</excludedGroups>
<includeJUnit5Engines><engine>junit-jupiter</engine></includeJUnit5Engines>
<properties><configurationParameters>custom.key = value</configurationParameters></properties>
<printSummary>false</printSummary><reportsDirectory>reports</reportsDirectory>""",
        environmentOverridesPrivate={"REMOVE": None, "EMPTY": "", "LABEL": "中文"},
    )
    opts, notes = runtime_options(e, project)
    assert opts["jvm_arguments"] == (
        "-ea",
        "-Xss256k",
        "-Dfixture=two words",
        "-da",
        "-Dlabel= 中文 = text ",
    )
    assert opts["environment"] == {
        "INHERIT": "yes",
        "REMOVE": None,
        "EMPTY": "",
        "LABEL": "中文",
    }
    assert opts["additional_classpath"] == (project.module_root / "fixtures",)
    assert opts["framework_options"] == {
        "groups": "fast | unit",
        "excluded_groups": "slow",
        "include_engines": "junit-jupiter",
        "configuration_parameters": "custom.key = value",
    }
    assert not notes


def test_unimplemented_runtime_setting_only_rejects_its_selected_execution(project):
    bad = execution(
        "it", "**/*IT.java", "<suiteXmlFiles><file>suite.xml</file></suiteXmlFiles>"
    )
    good = execution()
    assert execution_groups((good, bad), ("x.UnitTest",))[0][0] is good
    with pytest.raises(FastTestError) as error:
        runtime_options(bad, project)
    assert error.value.context["unsupported_configuration_names"] == ["suiteXmlFiles"]


def test_groups_share_timeout_and_aggregate_failures(project, monkeypatch):
    project.test_executions = (execution(), execution("it", "**/*IT.java"))
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        passed = len(calls) == 1
        return FastTestResult(
            "id",
            "junit5",
            1,
            int(passed),
            int(not passed),
            int(not passed),
            0,
            0,
            passed,
            5,
            () if passed else ({"name": "failed"},),
            False,
            Path("log"),
        )

    ticks = iter([100, 101, 110])
    monkeypatch.setattr(
        "jolink_runtime.launch.test_execution.time.monotonic", lambda: next(ticks)
    )
    attempt = SimpleNamespace(
        tests=("x.FirstTest", "x.SecondIT"),
        output_logs=(),
        timeout_seconds=30,
        owner=object(),
        require_not_cancelled=lambda: None,
    )
    result, ids, _ = run_test_executions(
        SimpleNamespace(run=run), project, attempt, project.module_root / "run"
    )
    assert [c["timeout_seconds"] for c in calls] == [29, 20]
    assert (
        result.tests == 2
        and result.passed_count == 1
        and result.failed_count == 1
        and not result.passed
    )
    assert ids == ["unit", "it"]
