import pytest

from jolink_runtime.launch.compiler_profile import (
    classify_compiler_arguments,
    parse_maven_memory_megabytes,
)


def test_compiler_process_memory_is_mapped_without_hiding_unknowns() -> None:
    profile = classify_compiler_arguments(["-J-Xms512m", "-J-Xmx3g", "-Xplugin:Custom"])

    assert profile.worker_min_heap_mb == 512
    assert profile.worker_max_heap_mb == 3072
    assert profile.unresolved_arguments == ("-Xplugin:Custom",)
    assert [item.disposition for item in profile.decisions] == [
        "MAPPED_TO_WORKER_JVM",
        "MAPPED_TO_WORKER_JVM",
        "UNRESOLVED",
    ]


def test_bare_jvm_memory_value_is_interpreted_as_bytes() -> None:
    profile = classify_compiler_arguments(["-J-Xmx2147483648"])
    assert profile.worker_max_heap_mb == 2048


def test_method_parameters_is_mapped_to_jdt() -> None:
    profile = classify_compiler_arguments(["-proc:none", "-parameters", "-parameters"])
    assert profile.method_parameters is True
    assert profile.unresolved_arguments == ()
    assert {item.disposition for item in profile.decisions} == {"MAPPED_TO_JDT"}


def test_nonexistent_sourcepath_sentinel_is_redundant_for_jdt() -> None:
    profile = classify_compiler_arguments(
        ["-sourcepath", "doesnotexist", "-parameters"]
    )

    assert profile.unresolved_arguments == ()
    assert profile.method_parameters is True
    assert [item.disposition for item in profile.decisions] == [
        "REDUNDANT_FOR_JDT",
        "REDUNDANT_FOR_JDT",
        "MAPPED_TO_JDT",
    ]


def test_real_sourcepath_remains_a_compatibility_boundary() -> None:
    profile = classify_compiler_arguments(["-sourcepath", "generated/java"])

    assert profile.unresolved_arguments == (
        "-sourcepath",
        "generated/java",
    )


def test_maven_structured_memory_values_are_megabytes_by_default() -> None:
    assert parse_maven_memory_megabytes("1024") == 1024
    assert parse_maven_memory_megabytes("2g") == 2048
    assert parse_maven_memory_megabytes("2048k") == 2
    assert parse_maven_memory_megabytes("invalid") is None


def test_optional_javac_diagnostics_follow_worker_errors_only_policy():
    profile = classify_compiler_arguments(
        ["-Xlint:all,-options,-path", "-nowarn", "-Xplugin:Unknown"]
    )
    assert profile.unresolved_arguments == ("-Xplugin:Unknown",)
    assert profile.decisions[0].category == "optional_diagnostics_disabled"


def test_processor_names_and_options_are_mapped_without_losing_values():
    profile = classify_compiler_arguments(
        [
            "-processor",
            "example.Second,example.First",
            "-Avalue=first",
            "-Avalue=last",
            "-Aflag",
            "-Aempty=",
            "-Alabel= 中文 = value with spaces ",
            "-Xplugin:Unknown",
        ]
    )
    assert profile.processor_names == ("example.Second", "example.First")
    assert profile.processor_options == {
        "value": "last",
        "flag": None,
        "empty": "",
        "label": " 中文 = value with spaces ",
    }
    assert profile.unresolved_arguments == ("-Xplugin:Unknown",)


def test_package_info_always_is_native_jdt_behavior_not_all_javac_options():
    profile = classify_compiler_arguments(
        ["-Xpkginfo:always", "-Xpkginfo:nonempty", "-Xplugin:ErrorProne"]
    )
    assert profile.decisions[0].category == "package_info_always_generated"
    assert profile.unresolved_arguments == ("-Xpkginfo:nonempty",)
    assert profile.skipped_checks == ("Error Prone",)


@pytest.mark.parametrize(
    "plugin",
    [
        "-Xplugin:ErrorProne",
        "-Xplugin:ErrorProne -XepDisableAllChecks -Xep:DeadException:ERROR",
        "-Xplugin:ErrorProne\n-XepDisableWarningsInGeneratedCode -XepCompilingTestOnlyCode",
        r"-Xplugin:ErrorProne -XepExcludedPaths:C:\work\generated\.* -XepOpt:Checker:Value=data",
    ],
)
def test_skip_javac_checks_keeps_parameters_processor_selection_and_values(plugin):
    args = [
        "-XDcompilePolicy=simple",
        "--should-stop=ifError=FLOW",
        plugin,
        "-J--add-exports=jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED",
        "-parameters",
        "-processor",
        "example.Factory",
        "-Amessage= spaces 中文 = preserved ",
    ]
    profile = classify_compiler_arguments(args)
    assert profile.unresolved_arguments == ()
    assert profile.skipped_checks == ("Error Prone",)
    assert profile.method_parameters
    assert profile.processor_names == ("example.Factory",)
    assert profile.processor_options == {"message": " spaces 中文 = preserved "}
    assert profile.arguments_without_checks[:3] == (
        "-parameters",
        "-processor",
        "example.Factory",
    )


@pytest.mark.parametrize(
    "invocation",
    [
        "-Xplugin:ErrorProne -XepPatchChecks:DeadException -XepPatchLocation:IN_PLACE",
        "-Xplugin:ErrorProne @checks.cfg",
        "-Xplugin:ErrorProne -XepFutureUnknownMode",
        "-Xplugin:OtherCompilerExtension",
        "-Xplugin:ErrorProneCustom",
    ],
)
def test_unknown_extensions_and_rewriting_are_not_treated_as_skipped_checks(invocation):
    profile = classify_compiler_arguments([invocation, "-XDcompilePolicy=simple"])
    assert profile.skipped_checks == ()
    assert profile.unresolved_arguments == (invocation, "-XDcompilePolicy=simple")


def test_separate_error_prone_options_and_unrelated_jvm_exports():
    unrelated = "-J--add-opens=java.base/java.lang=ALL-UNNAMED"
    profile = classify_compiler_arguments(
        ["-Xplugin:ErrorProne", "-XepCompilingTestOnlyCode", unrelated]
    )
    assert profile.skipped_checks == ("Error Prone",)
    assert profile.unresolved_arguments == (unrelated,)
    assert classify_compiler_arguments(["-Xep:DeadException:OFF"]).unresolved_arguments
    patched = classify_compiler_arguments(
        ["-Xplugin:ErrorProne", "-XepPatchLocation:IN_PLACE"]
    )
    assert patched.skipped_checks == ()
    assert len(patched.unresolved_arguments) == 2


def test_check_metadata_does_not_change_main_or_test_compilation_identity():
    from jolink_runtime.launch.jdt_modules import compilation_modules

    before = ({"source_level": 8, "test_compiler": {"source_level": 11}},)
    after = (
        {
            **before[0],
            "skipped_compiler_checks": ["Error Prone"],
            "skipped_build_steps": ["example:plugin:check@quality"],
            "test_compiler": {
                "source_level": 11,
                "skipped_compiler_checks": ["Error Prone"],
            },
        },
    )
    assert compilation_modules(before) == compilation_modules(after)
    after[0]["test_compiler"]["source_level"] = 17
    assert compilation_modules(before) != compilation_modules(after)
