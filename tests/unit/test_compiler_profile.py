from jolink_runtime.launch.compiler_profile import classify_compiler_arguments


def test_compiler_process_memory_is_mapped_without_hiding_unknowns() -> None:
    profile = classify_compiler_arguments(
        ["-J-Xms512m", "-J-Xmx3g", "-Xplugin:Custom"]
    )

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
