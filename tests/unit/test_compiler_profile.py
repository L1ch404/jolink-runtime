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
    assert profile.unresolved_arguments == ("-Xpkginfo:nonempty", "-Xplugin:ErrorProne")
