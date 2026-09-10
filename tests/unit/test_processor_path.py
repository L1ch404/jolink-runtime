import zipfile

from jolink_runtime.launch.maven import MavenBuildSystemAdapter
from jolink_runtime.launch.processor_path import jdt_processor_paths, processor_path


def test_complete_explicit_path_keeps_non_processor_helpers_in_order(tmp_path):
    paths = []
    for name, contents in (
        (
            "processor.jar",
            {
                "META-INF/services/javax.annotation.processing.Processor": "custom.Generator"
            },
        ),
        ("helper.jar", {"support/resource.txt": "used by Processor"}),
        ("collaboration.jar", {"lombok/mapstruct/Helper.class": b""}),
    ):
        path = tmp_path / name
        with zipfile.ZipFile(path, "w") as archive:
            for entry, content in contents.items():
                archive.writestr(entry, content)
        paths.append(path)
    facts = {
        "discoveryMode": "EXPLICIT_PROCESSOR_PATH",
        "processorPath": list(map(str, paths)),
        "processorProviderArtifactPaths": [str(paths[0])],
    }
    assert processor_path(facts) == tuple(paths)
    factories, agents = jdt_processor_paths(facts, processor_path(facts))
    assert factories == tuple(paths)
    assert agents == ()
    assert MavenBuildSystemAdapter._jdt_dependency_facts(paths[-1]) == (True, (), False)


def test_lombok_agent_requires_real_entry_class_and_implicit_behavior_unchanged(
    tmp_path,
):
    jar = tmp_path / "agent.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("lombok/launch/Agent.class", b"")
    facts = {
        "discoveryMode": "IMPLICIT_COMPILE_CLASSPATH",
        "processorProviderArtifactPaths": [str(jar)],
    }
    assert processor_path(facts) == (jar,)
    assert jdt_processor_paths(facts, processor_path(facts)) == ((), (jar,))
    facts["discoveryMode"] = "EXPLICIT_PROCESSOR_PATH"
    facts["processorPath"] = [str(jar)]
    assert jdt_processor_paths(facts, processor_path(facts)) == ((jar,), (jar,))


def test_explicit_empty_path_does_not_fall_back_to_provider_list(tmp_path):
    assert (
        processor_path(
            {
                "processorPath": [],
                "processorProviderArtifactPaths": [str(tmp_path / "missing")],
            }
        )
        == ()
    )
