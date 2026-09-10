"""Real Maven resolution -> Eclipse Factory Path -> generated code -> MCP tests."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import uuid
import zipfile
from pathlib import Path

import anyio
import pytest
from java_support import (
    open_mcp_session,
    require_real_mcp_java_e2e,
    reserve_local_port,
    temporary_stderr,
)

from jolink_runtime.launch.maven_probe import ProductMavenProbe


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("manage_transitives", [False, True])
def test_explicit_processor_with_transitive_helper_and_resource(
    tmp_path: Path, manage_transitives
):
    require_real_mcp_java_e2e()
    home = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    maven = os.environ.get("JOLINK_TEST_MAVEN") or shutil.which("mvn")
    if not home or not maven:
        pytest.skip("JDK 8 and Maven required")
    env = {
        **os.environ,
        "JAVA_HOME": home,
        "PATH": str(Path(home) / "bin") + os.pathsep + os.environ.get("PATH", ""),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "MAVEN_ARGS": "--offline",
    }
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")
    group = "io.jolink.fixture.p" + uuid.uuid4().hex
    suffix = ".exe" if os.name == "nt" else ""

    def command(argv, cwd=tmp_path):
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    def artifact(
        name, version, source, *, dependencies="", classpath=(), resources=None
    ):
        root = tmp_path / f"{name}-{version}"
        root.mkdir()
        java = root / ("Helper.java" if name == "helper" else "Generator.java")
        java.write_text(source, encoding="utf-8")
        classes = root / "classes"
        classes.mkdir()
        command(
            [
                str(Path(home) / f"bin/javac{suffix}"),
                "-proc:none",
                "-cp",
                os.pathsep.join(map(str, classpath)) or ".",
                "-d",
                str(classes),
                str(java),
            ]
        )
        jar = root / f"{name}-{version}.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            for file in classes.rglob("*.class"):
                archive.write(file, file.relative_to(classes).as_posix())
            for key, value in (resources or {}).items():
                archive.writestr(key, value)
        pom = root / "pom.xml"
        pom.write_text(f"""<project><modelVersion>4.0.0</modelVersion>
<groupId>{group}</groupId><artifactId>{name}</artifactId><version>{version}</version>
<dependencies>{dependencies}</dependencies></project>""")
        command(
            [
                maven,
                "--offline",
                "-B",
                "org.apache.maven.plugins:maven-install-plugin:3.1.2:install-file",
                f"-Dfile={jar}",
                f"-DpomFile={pom}",
            ]
        )
        return jar

    helper_source = """package auxiliary;
public class Helper {
 public static int offset() throws Exception {
  try(java.io.InputStream stream=Helper.class.getResourceAsStream("/offset.txt")) {
   return stream.read()-'0';
  }
 }
} """
    helper = artifact("helper", "1", helper_source, resources={"offset.txt": "1"})
    artifact("helper", "2", helper_source, resources={"offset.txt": "2"})
    artifact(
        "generator",
        "1",
        """package custom;
import java.io.Writer;
import java.util.Set;
import javax.annotation.processing.*;
import javax.lang.model.SourceVersion;
import javax.lang.model.element.*;
import javax.tools.*;
@SupportedAnnotationTypes("*") @SupportedSourceVersion(SourceVersion.RELEASE_8)
public class Generator extends AbstractProcessor {
 public boolean process(Set<? extends TypeElement> annotations, RoundEnvironment round) {
  for(Element root : round.getRootElements()) {
   if(!root.getSimpleName().contentEquals("Input")) continue;
   for(Element member : root.getEnclosedElements()) {
    if(!member.getSimpleName().contentEquals("VALUE")) continue;
    try {
     int value=((Number)((VariableElement)member).getConstantValue()).intValue();
     int answer=value+auxiliary.Helper.offset();
     try(Writer out=processingEnv.getFiler().createSourceFile("generated.Output",root).openWriter()) {
      out.write("package generated; public class Output {public static int get(){return "+answer+";}}");
     }
     try(Writer out=processingEnv.getFiler().createResource(StandardLocation.CLASS_OUTPUT,
         "", "META-INF/generated-value.txt",root).openWriter()) {out.write(String.valueOf(answer));}
    } catch(Exception error) {throw new RuntimeException(error);}
   }
  }
  return false;
 }
} """,
        classpath=(helper,),
        resources={
            "META-INF/services/javax.annotation.processing.Processor": "custom.Generator\n",
        },
        dependencies=f"""
<dependency><groupId>{group}</groupId><artifactId>helper</artifactId><version>1</version></dependency>
<dependency><groupId>{group}</groupId><artifactId>excluded-missing</artifactId><version>1</version></dependency>""",
    )

    project = tmp_path / "product"
    main = project / "src/main/java/example/Input.java"
    main.parent.mkdir(parents=True)
    main.write_text(
        "package example; public class Input { public static final int VALUE=10; }"
    )
    (main.parent / "App.java").write_text("""package example; public class App {
 public static void main(String[] args) throws Exception {
  try(java.net.ServerSocket server=new java.net.ServerSocket(Integer.parseInt(args[0]))) {
   while(true) {try(java.net.Socket socket=server.accept()) {
    socket.getOutputStream().write(String.valueOf(generated.Output.get()).getBytes("UTF-8"));
   }}
  }
 }
} """)
    test = project / "src/test/java/example/GeneratedTest.java"
    test.parent.mkdir(parents=True)
    answer = 12 if manage_transitives else 11
    test.write_text(f"""package example; public class GeneratedTest {{
 @org.junit.Test public void generated() throws Exception {{
  org.junit.Assert.assertEquals({answer},generated.Output.get());
  for(String type : new String[]{{"custom.Generator", "auxiliary.Helper"}}) {{
   try {{Class.forName(type); org.junit.Assert.fail("Processor leaked into runtime: "+type);}}
   catch(ClassNotFoundException expected) {{}}
  }}
  try(java.io.InputStream in=getClass().getClassLoader().getResourceAsStream("META-INF/generated-value.txt")) {{
   org.junit.Assert.assertNotNull(in); org.junit.Assert.assertEquals('1',in.read());
  }}
 }}
}}""")
    (project / "pom.xml").write_text(f"""<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>generated-app</artifactId><version>1</version>
<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencyManagement><dependencies>
<dependency><groupId>{group}</groupId><artifactId>generator</artifactId><version>1</version></dependency>
<dependency><groupId>{group}</groupId><artifactId>helper</artifactId><version>2</version></dependency>
</dependencies></dependencyManagement>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version><configuration>
<annotationProcessorPaths><path><groupId>{group}</groupId><artifactId>generator</artifactId>
<exclusions><exclusion><groupId>{group}</groupId><artifactId>excluded-missing</artifactId></exclusion></exclusions>
</path></annotationProcessorPaths>
<annotationProcessorPathsUseDepMgmt>{str(manage_transitives).lower()}</annotationProcessorPathsUseDepMgmt>
</configuration></plugin><plugin><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version></plugin></plugins></build>
</project>""")
    baseline = tmp_path / "native"
    shutil.copytree(project, baseline)
    command([maven, "--offline", "-B", "test"], baseline)

    # The same Maven session exports the exact path without putting helpers on
    # the application classpath. Native Maven already generated/tested the code.
    probe = ProductMavenProbe.load()
    prepared = probe.prepare(
        attempt_directory=tmp_path / "probe",
        source_settings=None,
        local_repository=Path.home() / ".m2/repository",
        offline=True,
    )
    command(
        [
            maven,
            "--offline",
            "-B",
            "-s",
            str(prepared.settings_file),
            prepared.goal,
            f"-Djolink.probe.outputDirectory={prepared.output_directory}",
        ],
        baseline,
    )
    snapshots = [
        json.loads(p.read_text()) for p in prepared.output_directory.glob("*.json")
    ]
    snapshot = next(p for p in snapshots if "annotationProcessing" in p)
    for key in ("annotationProcessing", "testAnnotationProcessing"):
        processing = snapshot[key]
        assert processing["discoveryMode"] == "EXPLICIT_PROCESSOR_PATH", processing
        assert [Path(p).name for p in processing["processorPath"]] == [
            "generator-1.jar",
            "helper-2.jar" if manage_transitives else "helper-1.jar",
        ]
        assert [Path(p).name for p in processing["processorProviderArtifactPaths"]] == [
            "generator-1.jar"
        ]
    assert not any(
        "helper-" in p or "generator-" in p
        for p in snapshot["compileClasspathElements"]
    )
    port, debug = reserve_local_port(), reserve_local_port()
    (project / ".run").mkdir()
    (
        project / ".run/App.xml"
    ).write_text(f'''<component name="ProjectRunConfigurationManager">
<configuration name="App" type="Application"><module name="generated-app"/>
<option name="MAIN_CLASS_NAME" value="example.App"/>
<option name="WORKING_DIRECTORY" value="$PROJECT_DIR$"/>
<option name="PROGRAM_PARAMETERS" value="{port}"/>
<method v="2"><option name="Make" enabled="true"/></method></configuration></component>''')

    async def scenario():
        async def run(session):
            result = dict(
                (
                    await session.call_tool(
                        "java_application",
                        {
                            "action": "test",
                            "project_path": str(project),
                            "tests": ["example.GeneratedTest"],
                            "timeout": 20,
                        },
                    )
                ).structuredContent
                or {}
            )
            with anyio.fail_after(120):
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
                assert cold.get("passed") is True and cold["tests"] == 1, cold
                unchanged = await run(session)
                assert (
                    unchanged["passed"] and unchanged["compiled_source_count"] == 0
                ), unchanged
                original = main.read_text()
                main.write_text(original.replace("VALUE=10", "VALUE=20"))
                failed = await run(session)
                assert failed.get("failed_count") == 1, failed
                main.write_text(original)
                assert (await run(session))["passed"]
                test_source = test.read_text()
                test.write_text(test_source + "this is a syntax error")
                failed = await run(session)
                assert failed.get("status") == "compile_failed", failed
                test.write_text(test_source)
                assert (await run(session))["passed"]
        with temporary_stderr() as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                warm = await run(session)
                assert warm.get("passed") is True, warm
                assert warm["compiled_source_count"] == 0, warm
                result = dict(
                    (
                        await session.call_tool(
                            "java_application",
                            {
                                "action": "launch",
                                "project_path": str(project),
                                "launch_name": "App",
                                "jdwp_port": debug,
                                "ready_port": port,
                                "startup_wait_timeout_seconds": 10,
                            },
                        )
                    ).structuredContent
                    or {}
                )
                try:
                    with anyio.fail_after(120):
                        while result.get("launch_phase") != "runtime_active":
                            assert (
                                result.get("ok") is not False
                                and result.get("launch_phase") != "failed"
                            ), result
                            await anyio.sleep(0.05)
                            result = dict(
                                (
                                    await session.call_tool(
                                        "java_status", {"action": "status"}
                                    )
                                ).structuredContent
                            )

                    def running_value():
                        with socket.create_connection(
                            ("127.0.0.1", port), timeout=10
                        ) as connection:
                            return connection.recv(100).decode("utf-8")

                    assert await anyio.to_thread.run_sync(running_value) == str(answer)
                finally:
                    await session.call_tool("java_application", {"action": "stop"})
        assert not (project / "target").exists()

    anyio.run(scenario)

    # Resolve the standard main/test execution configurations independently;
    # this does not make a single Worker use two different Factory Paths.
    pom = project / "pom.xml"
    original = pom.read_text()
    for execution, expected in (
        ("default-testCompile", "DISABLED"),
        ("separate-test-compile", "EXECUTION_CONFIG_UNRESOLVED"),
    ):
        pom.write_text(
            original.replace(
                "</annotationProcessorPathsUseDepMgmt>\n</configuration>",
                "</annotationProcessorPathsUseDepMgmt>\n</configuration><executions><execution>"
                f"<id>{execution}</id><goals><goal>testCompile</goal></goals>"
                "<configuration><proc>none</proc></configuration></execution></executions>",
            )
        )
        command(
            [
                maven,
                "--offline",
                "-B",
                "-s",
                str(prepared.settings_file),
                prepared.goal,
                f"-Djolink.probe.outputDirectory={prepared.output_directory}",
            ],
            project,
        )
        reports = [
            json.loads(p.read_text()) for p in prepared.output_directory.glob("*.json")
        ]
        latest = next(
            report
            for report in reports
            if Path(report["project"]["baseDirectory"]).resolve() == project.resolve()
        )
        assert latest["testAnnotationProcessing"]["discoveryMode"] == expected, latest[
            "testAnnotationProcessing"
        ]
    pom.write_text(original)
