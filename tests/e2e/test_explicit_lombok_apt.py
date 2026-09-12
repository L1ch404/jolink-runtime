"""Lombok's agent does not replace its explicit APT initialization entry."""

import os
import xml.etree.ElementTree as ET
from pathlib import Path

import anyio
import pytest
from java_support import open_mcp_session, require_real_mcp_java_e2e, temporary_stderr


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("lombok_first", [False, True])
def test_lombok_agent_and_explicit_apt_entry_work_together(
    tmp_path: Path, lombok_first
):
    require_real_mcp_java_e2e()
    home = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not home:
        pytest.skip("set JOLINK_TEST_JAVA8_HOME")
    project = tmp_path / "project"
    main = project / "src/main/java/example"
    main.mkdir(parents=True)
    source = main / "Source.java"
    original = (
        "package example; @lombok.Data public class Source { private String value; }"
    )
    source.write_text(original)
    (main / "Target.java").write_text(
        "package example; public class Target { public String value; }"
    )
    (main / "Mapping.java").write_text("""package example;
@org.mapstruct.Mapper public interface Mapping {
 Mapping INSTANCE=org.mapstruct.factory.Mappers.getMapper(Mapping.class);
 Target convert(Source value);
} """)
    tests = project / "src/test/java/example"
    tests.mkdir(parents=True)
    (
        tests / "MappingTest.java"
    ).write_text("""package example; public class MappingTest {
 @org.junit.Test public void maps() {
  Source input=new Source(); input.setValue("original");
  org.junit.Assert.assertEquals("original", Mapping.INSTANCE.convert(input).value);
 }
} """)
    # Both orders must work without changing or dropping any Factory Path JAR.
    (project / "pom.xml").write_text("""<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>explicit-lombok</artifactId><version>1</version>
<dependencies>
<dependency><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId><version>1.18.30</version><scope>provided</scope></dependency>
<dependency><groupId>org.mapstruct</groupId><artifactId>mapstruct</artifactId><version>1.6.3</version></dependency>
<dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency>
</dependencies><build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.8.1</version><configuration>
<source>8</source><target>8</target><encoding>UTF-8</encoding><annotationProcessorPaths>
<path><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId><version>1.18.30</version></path>
<path><groupId>org.mapstruct</groupId><artifactId>mapstruct-processor</artifactId><version>1.6.3</version></path>
<path><groupId>org.projectlombok</groupId><artifactId>lombok-mapstruct-binding</artifactId><version>0.2.0</version></path>
</annotationProcessorPaths></configuration></plugin></plugins></build></project>""")
    if not lombok_first:
        pom = project / "pom.xml"
        tree = ET.fromstring(pom.read_text())
        paths = tree.find(
            "./build/plugins/plugin/configuration/annotationProcessorPaths"
        )
        first, second, binding = list(paths)
        paths[:] = [second, first, binding]
        pom.write_text(ET.tostring(tree, encoding="unicode"))
    env = {
        **os.environ,
        "JAVA_HOME": home,
        "PATH": str(Path(home) / "bin") + os.pathsep + os.environ.get("PATH", ""),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "MAVEN_ARGS": "--offline",
    }
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")

    async def scenario():
        async def run(session):
            result = dict(
                (
                    await session.call_tool(
                        "java_application",
                        {
                            "action": "test",
                            "project_path": str(project),
                            "tests": ["example.MappingTest"],
                            "timeout": 20,
                        },
                    )
                ).structuredContent
                or {}
            )
            with anyio.fail_after(90):
                while result.get("status") in {
                    "starting",
                    "bootstrapping",
                    "compiling",
                    "running",
                }:
                    await anyio.sleep(0.05)
                    result = dict(
                        (
                            await session.call_tool("java_status", {"action": "status"})
                        ).structuredContent["fast_test"]
                    )
            return result

        with temporary_stderr() as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                cold = await run(session)
                assert cold.get("passed") is True, cold
                source.write_text(
                    original.replace(
                        "private String value;",
                        'private String value; public String getValue(){return value+"!";}',
                    )
                )
                failed = await run(session)
                assert failed.get("failed_count") == 1, failed
                source.write_text(original)
                assert (await run(session))["passed"]
        with temporary_stderr() as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                assert (await run(session))["passed"]
        # Main/test now keep independent ordered Factory Paths. A different
        # order no longer needs a shared-project configuration rejection.
        pom = project / "pom.xml"
        tree = ET.fromstring(pom.read_text())
        plugin = tree.find("./build/plugins/plugin")
        paths = plugin.find("./configuration/annotationProcessorPaths")
        execution = ET.SubElement(ET.SubElement(plugin, "executions"), "execution")
        ET.SubElement(execution, "id").text = "default-testCompile"
        ET.SubElement(ET.SubElement(execution, "goals"), "goal").text = "testCompile"
        test_paths = ET.SubElement(
            ET.SubElement(execution, "configuration"), "annotationProcessorPaths"
        )
        test_paths.extend(reversed(list(paths)))
        pom.write_text(ET.tostring(tree, encoding="unicode"))
        with temporary_stderr() as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                different = await run(session)
                assert different.get("passed") is True, different
                unchanged = await run(session)
                assert (
                    unchanged["passed"] and unchanged["compiled_source_count"] == 0
                ), unchanged
        # Selecting only MapStruct must not silently keep Lombok's AST agent
        # enabled just because the Lombok JAR is still on the Factory Path.
        compiler = tree.find("./build/plugins/plugin")
        compiler.remove(compiler.find("executions"))
        names = ET.SubElement(compiler.find("configuration"), "annotationProcessors")
        ET.SubElement(
            names, "annotationProcessor"
        ).text = "org.mapstruct.ap.MappingProcessor"
        pom.write_text(ET.tostring(tree, encoding="unicode"))
        with temporary_stderr() as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                excluded = await run(session)
                assert excluded.get("ok") is False, excluded
                ET.SubElement(
                    names, "annotationProcessor"
                ).text = "lombok.launch.AnnotationProcessorHider$AnnotationProcessor"
                pom.write_text(ET.tostring(tree, encoding="unicode"))
                assert (await run(session))["passed"]
        assert not (project / "target").exists()

    anyio.run(scenario)
