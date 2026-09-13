"""Separate factory paths really generate code in separate main/test projects."""

import json
import os
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path

import anyio
import pytest
from java_support import open_mcp_session, temporary_stderr
from test_fast_test_scopes import environment, run


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("build_system", ["maven", "gradle"])
@pytest.mark.parametrize("main_processor", [False, True])
@pytest.mark.parametrize("selection", ["service", "explicit"])
def test_independent_processor_loading_and_generated_outputs(
    tmp_path, build_system, main_processor, selection, *, main_uses_generated_output=True
):
    env = environment(tmp_path)
    home = Path(env["JAVA_HOME"])
    maven = shutil.which("mvn")
    gradle = os.environ.get("JOLINK_FAST_TEST_GRADLE")
    if not maven or (build_system == "gradle" and not gradle):
        pytest.skip("Maven and configured Gradle required")
    group = "io.jolink.scopes.p" + uuid.uuid4().hex

    def command(args):
        result = subprocess.run(
            args,
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    artifacts = {}
    for scope, input_type, output_type in (
        ("main", "Main", "MainValue"),
        ("test", "ScopeTest", "TestValue"),
    ):
        source = tmp_path / scope / "Generator.java"
        source.parent.mkdir()
        source.write_text(
            """package processor;
import java.io.Writer; import java.util.Set;
import javax.annotation.processing.*; import javax.lang.model.*; import javax.lang.model.element.*;
@SupportedAnnotationTypes("*") @SupportedSourceVersion(SourceVersion.RELEASE_8)
public class Generator extends AbstractProcessor {
 public boolean process(Set<? extends TypeElement> annotations, RoundEnvironment round) {
  for(Element type:round.getRootElements()) {
   if(!type.getSimpleName().contentEquals("INPUT")) continue;
   for(Element field:type.getEnclosedElements()) if(field.getSimpleName().contentEquals("VALUE")) {
    Object value=((VariableElement)field).getConstantValue();
    try(Writer w=processingEnv.getFiler().createSourceFile("generated.OUTPUT",type).openWriter()) {
     w.write("package generated; public class OUTPUT {public static int get(){return "+value+";}}");
    } catch(Exception e){throw new RuntimeException(e);}
   }
  }
  return false;
 }
}""".replace("INPUT", input_type).replace("OUTPUT", output_type)
        )
        classes = source.parent / "classes"
        classes.mkdir()
        trap = source.with_name("Trap.java")
        trap.write_text("""package processor;
@javax.annotation.processing.SupportedAnnotationTypes("*")
public class Trap extends javax.annotation.processing.AbstractProcessor {
 static { if (System.getProperty("never.initialize") == null) throw new AssertionError("unselected static initializer"); }
 public Trap() { throw new AssertionError("unselected constructor"); }
 public void init(javax.annotation.processing.ProcessingEnvironment env) { throw new AssertionError("unselected init"); }
 public boolean process(java.util.Set<? extends javax.lang.model.element.TypeElement> a, javax.annotation.processing.RoundEnvironment r) { throw new AssertionError("unselected process"); }
}""")
        if selection != "service":
            source.write_text(
                source.read_text().replace(
                    "Object value=",
                    """
    java.util.Map<String,String> options=processingEnv.getOptions();
    if(!options.containsKey("flag") || options.get("flag")!=null || !"".equals(options.get("empty"))
       || !"0".equals(options.get("offset")) || !"SCOPE = 世界 with spaces".equals(options.get("label")))
       throw new AssertionError("processor options differ: "+options);
    Object value=""".replace("SCOPE", scope),
                )
            )
        command(
            [
                str(home / ("bin/javac.exe" if os.name == "nt" else "bin/javac")),
                "-d",
                str(classes),
                str(source),
                str(trap),
            ]
        )
        jar = source.parent / (scope + ".jar")
        with zipfile.ZipFile(jar, "w") as archive:
            for path in classes.rglob("*.class"):
                archive.write(path, path.relative_to(classes).as_posix())
            archive.writestr(
                "META-INF/services/javax.annotation.processing.Processor",
                "processor.Trap\n"
                if selection != "service"
                else "processor.Generator\n",
            )
        artifacts[scope] = jar
        if build_system == "maven":
            command(
                [
                    maven,
                    "--offline",
                    "-B",
                    "org.apache.maven.plugins:maven-install-plugin:3.1.2:install-file",
                    f"-Dfile={jar}",
                    f"-DgroupId={group}",
                    f"-DartifactId={scope}",
                    "-Dversion=1",
                    "-Dpackaging=jar",
                ]
            )

    project = tmp_path / "project"
    main = project / "src/main/java/example/Main.java"
    test = project / "src/test/java/example/ScopeTest.java"
    main.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    main.write_text(
        "package example; public class Main { public static final int VALUE=10; public static int get(){return "
        + (
            "generated.MainValue.get()"
            if main_processor and main_uses_generated_output
            else "VALUE"
        )
        + ";} }"
    )
    test.write_text("""package example; public class ScopeTest {
 public static final int VALUE=20;
 @org.junit.Test public void generated() throws Exception {
  org.junit.Assert.assertEquals(10, Main.get());
  org.junit.Assert.assertEquals(20, generated.TestValue.get());
  try {Class.forName("processor.Generator"); org.junit.Assert.fail("processor leaked into test runtime");}
  catch(ClassNotFoundException expected){}
 }
}""")
    if main_processor:
        # APT must run even when main compilation has no reference to its output.
        # Reflection also checks freshness after incremental edits and reopen.
        test.write_text(
            test.read_text().replace(
                "org.junit.Assert.assertEquals(10, Main.get());",
                """org.junit.Assert.assertEquals(Main.VALUE,
   ((Number)Class.forName("generated.MainValue").getMethod("get").invoke(null)).intValue());
  org.junit.Assert.assertEquals(10, Main.get());""",
            )
        )
    if build_system == "maven":
        executions = ""
        for scope in ("main", "test") if main_processor else ("test",):
            goal = "compile" if scope == "main" else "testCompile"
            settings = (
                f"""<annotationProcessors><annotationProcessor>processor.Generator</annotationProcessor></annotationProcessors>
<compilerArgs><arg>-Aoffset=99</arg><arg>-Aoffset=0</arg><arg>-Aflag</arg><arg>-Aempty=</arg><arg>-Alabel={scope} = 世界 with spaces</arg></compilerArgs>"""
                if selection != "service"
                else ""
            )
            executions += f"""<execution><id>default-{goal}</id><goals><goal>{goal}</goal></goals><configuration>
<annotationProcessorPaths><path><groupId>{group}</groupId><artifactId>{scope}</artifactId><version>1</version></path></annotationProcessorPaths>
{settings}
</configuration></execution>"""
        (project / "pom.xml").write_text(f"""<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>processor-scopes</artifactId><version>1</version>
<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version><executions>{executions}</executions></plugin></plugins></build></project>""")
    else:
        wrapper = project / ("gradlew.bat" if os.name == "nt" else "gradlew")
        wrapper.write_text(
            f'@echo off\r\ncall "{gradle}" %*\r\n'
            if os.name == "nt"
            else f'#!/bin/sh\nexec "{gradle}" "$@"\n'
        )
        wrapper.chmod(0o755)
        (project / "settings.gradle").write_text("rootProject.name='processor-scopes'")
        junit = Path.home() / ".m2/repository/junit/junit/4.13.2/junit-4.13.2.jar"
        hamcrest = (
            Path.home()
            / ".m2/repository/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar"
        )
        main_dependency = (
            f"annotationProcessor files('{artifacts['main'].as_posix()}')"
            if main_processor
            else ""
        )
        (project / "build.gradle").write_text(f"""plugins {{ id 'java' }}
sourceCompatibility=8; targetCompatibility=8
dependencies {{ testImplementation files('{junit.as_posix()}', '{hamcrest.as_posix()}');
 {main_dependency}
 testAnnotationProcessor files('{artifacts["test"].as_posix()}')
}}
tasks.withType(JavaCompile).configureEach {{ options.encoding='UTF-8'; doFirst {{ throw new GradleException('native compile must not run') }} }}
test {{ doFirst {{ throw new GradleException('native test must not run') }} }}
""")

    if selection == "classpath":
        import xml.etree.ElementTree as ET

        pom = project / "pom.xml"
        xml = ET.fromstring(pom.read_text())
        config = xml.find("./build/plugins/plugin/executions/execution/configuration")
        config.remove(config.find("annotationProcessorPaths"))
        dependency = ET.SubElement(xml.find("dependencies"), "dependency")
        for name, value in {
            "groupId": group,
            "artifactId": "test",
            "version": "1",
            "scope": "test",
        }.items():
            ET.SubElement(dependency, name).text = value
        pom.write_text(ET.tostring(xml, encoding="unicode"))
        # This JAR is deliberately a test dependency in this variant.
        test.write_text(
            test.read_text().replace(
                'try {Class.forName("processor.Generator"); org.junit.Assert.fail("processor leaked into test runtime");}\n  catch(ClassNotFoundException expected){}',
                'org.junit.Assert.assertNotNull(Class.forName("processor.Generator"));',
            )
        )

    if build_system == "gradle" and selection == "explicit":
        build = project / "build.gradle"
        settings = ""
        for scope in ("main", "test") if main_processor else ("test",):
            task = "compileJava" if scope == "main" else "compileTestJava"
            settings += f"\n{task}.options.compilerArgs.addAll(['-processor', 'processor.Generator', '-Aoffset=99', '-Aoffset=0', '-Aflag', '-Aempty=', '-Alabel={scope} = 世界 with spaces'])\n"
        build.write_text(build.read_text() + settings)

    async def scenario():
        for cycle in range(2):
            with temporary_stderr() as stderr:
                async with open_mcp_session(stderr, environment=env) as session:

                    async def check():
                        return await run(session, project, ["example.ScopeTest"])

                    result = await check()
                    assert result.get("passed"), json.dumps(result, ensure_ascii=False)
                    if cycle:
                        assert result["compiled_source_count"] == 0, result
                        continue
                    for source in (main, test):
                        original = source.read_text()
                        source.write_text(
                            original.replace("VALUE=10", "VALUE=11").replace(
                                "VALUE=20", "VALUE=21"
                            )
                        )
                        result = await check()
                        assert result.get("failed_count") == 1, result
                        source.write_text(original)
                        assert (await check())["passed"]
                    # A main source must not see the test project's generated class.
                    original = main.read_text()
                    main.write_text(
                        original.replace("return ", "return generated.TestValue.get()+")
                    )
                    result = await check()
                    assert result.get("ok") is False, result
                    main.write_text(original)
                    assert (await check())["passed"]
        if selection != "service":
            config = project / (
                "pom.xml" if build_system == "maven" else "build.gradle"
            )
            original_config = config.read_text()
            with temporary_stderr() as stderr:
                async with open_mcp_session(stderr, environment=env) as session:
                    try:
                        config.write_text(
                            original_config.replace(
                                "processor.Generator", "processor.Missing"
                            )
                        )
                        missing = await run(session, project, ["example.ScopeTest"])
                        assert missing.get("ok") is False, missing
                    finally:
                        config.write_text(original_config)
                    restored = await run(session, project, ["example.ScopeTest"])
                    assert restored.get("passed"), restored
        assert not (project / "target").exists()

    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
def test_named_processor_without_service_uses_default_compile_classpath(tmp_path):
    test_independent_processor_loading_and_generated_outputs(
        tmp_path, "maven", False, "classpath"
    )


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("build_system", ["maven", "gradle"])
def test_main_processor_runs_without_compile_time_reference(tmp_path, build_system):
    for attempt in range(3):
        cold = tmp_path / f"cold-{attempt}"
        cold.mkdir()
        test_independent_processor_loading_and_generated_outputs(
            cold, build_system, True, "service", main_uses_generated_output=False
        )
