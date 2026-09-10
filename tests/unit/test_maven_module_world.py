import json
from pathlib import Path
from types import SimpleNamespace

from jolink_runtime.launch.maven_module_world import load_module_worlds


def test_native_module_world_preserves_roots_encoding_and_heap(tmp_path, monkeypatch):
    project = tmp_path / "project"
    sources = project / "custom-java"
    sources.mkdir(parents=True)
    output = project / "custom-output"
    snapshot = {
        "project": {
            "baseDirectory": str(project),
            "groupId": "example",
            "artifactId": "app",
            "version": "1",
        },
        "outputDirectory": str(output),
        "testOutputDirectory": str(project / "test-output"),
        "compileSourceRoots": [str(sources)],
        "testCompileSourceRoots": [],
        "compileClasspathElements": [str(output)],
        "testClasspathElements": [],
        "runtimeClasspathElements": [str(output)],
        "resourceDirectories": [],
        "annotationProcessing": {"processorProviderArtifactPaths": []},
    }
    exports = tmp_path / "probe"
    exports.mkdir()
    (exports / "app.json").write_text(json.dumps(snapshot))
    (exports / "app.pom.xml").write_text("""<project>
<groupId>example</groupId><artifactId>app</artifactId><version>1</version>
<properties><project.build.sourceEncoding>GBK</project.build.sourceEncoding></properties>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><configuration>
<source>8</source><target>8</target><parameters>true</parameters>
<meminitial>128m</meminitial><maxmem>4g</maxmem>
<compilerArgs><arg>-J-Xms96m</arg><arg>-J-Xmx3g</arg></compilerArgs>
</configuration></plugin>
<plugin><artifactId>cobertura-maven-plugin</artifactId><executions><execution>
<goals><goal>clean</goal><goal>check</goal></goals></execution></executions></plugin>
</plugins></build></project>""")
    monkeypatch.setattr(
        "jolink_runtime.launch.maven_module_world.select_target_system_home",
        lambda *_args: tmp_path / "jdk",
    )
    jdk = SimpleNamespace(
        home=tmp_path / "jdk", compiler_major_version=8, major_version=8
    )
    modules = load_module_worlds(exports, project, jdk, tests=False)
    assert len(modules) == 1
    world = modules[0]
    assert world["source_roots"] == [str(sources)]
    assert world["output_directory"] == str(output)
    assert world["source_encoding"] == "GBK"
    assert world["method_parameters"] is True
    assert world["worker_min_heap_mb"] == 128
    assert world["worker_max_heap_mb"] == 4096
    assert world["classpath"] == []
