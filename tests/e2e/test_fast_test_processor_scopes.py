"""Separate factory paths really generate code in separate main/test projects."""

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
def test_independent_processor_loading_and_generated_outputs(
    tmp_path, build_system, main_processor
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
        command(
            [
                str(home / ("bin/javac.exe" if os.name == "nt" else "bin/javac")),
                "-d",
                str(classes),
                str(source),
            ]
        )
        jar = source.parent / (scope + ".jar")
        with zipfile.ZipFile(jar, "w") as archive:
            for path in classes.rglob("*.class"):
                archive.write(path, path.relative_to(classes).as_posix())
            archive.writestr(
                "META-INF/services/javax.annotation.processing.Processor",
                "processor.Generator\n",
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
        + ("generated.MainValue.get()" if main_processor else "VALUE")
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
    if build_system == "maven":
        executions = ""
        for scope in ("main", "test") if main_processor else ("test",):
            goal = "compile" if scope == "main" else "testCompile"
            executions += f"""<execution><id>default-{goal}</id><goals><goal>{goal}</goal></goals><configuration>
<annotationProcessorPaths><path><groupId>{group}</groupId><artifactId>{scope}</artifactId><version>1</version></path></annotationProcessorPaths>
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

    async def scenario():
        for cycle in range(2):
            with temporary_stderr() as stderr:
                async with open_mcp_session(stderr, environment=env) as session:

                    async def check():
                        return await run(session, project, ["example.ScopeTest"])

                    result = await check()
                    assert result.get("passed"), result
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
        assert not (project / "target").exists()

    anyio.run(scenario)
