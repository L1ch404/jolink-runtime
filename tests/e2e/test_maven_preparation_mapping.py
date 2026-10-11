"""Real Maven execution mappings, generators, JDT and MCP; no plugin-name fixtures in product."""

import json
import os
import shutil
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
from test_build_configuration_mcp import project_fixture
from test_review_model_cache import response


def fixture_plugin(tmp_path, env):
    mvn = shutil.which("mvn.cmd" if os.name == "nt" else "mvn")
    if not mvn:
        pytest.skip("Maven is required")
    home = Path(
        os.environ.get("JOLINK_TEST_MAVEN_HOME") or Path(mvn).resolve().parent.parent
    )
    if not (home / "lib").is_dir() and (home / "libexec/lib").is_dir():
        home /= "libexec"
    if not (home / "lib").is_dir():
        pytest.skip("set JOLINK_TEST_MAVEN_HOME to a Maven 3 installation")
    group = "io.jolink.preparation.p" + uuid.uuid4().hex
    source = tmp_path / "Step.java"
    source.write_text("""package fixture;
public class Step extends org.apache.maven.plugin.AbstractMojo {
 public org.apache.maven.project.MavenProject project;
 public String mode;
 public java.io.File valueFile;
 public void execute() throws org.apache.maven.plugin.MojoExecutionException {
  if(!"generate".equals(mode))throw new org.apache.maven.plugin.MojoExecutionException("fixture-check-executed");
  try {
   java.nio.file.Path root=new java.io.File(project.getBuild().getDirectory(),"generated-fixture").toPath();
   java.nio.file.Path output=root.resolve("generated/Value.java");
   java.nio.file.Files.createDirectories(output.getParent());
   String value=new String(java.nio.file.Files.readAllBytes(valueFile.toPath()),"UTF-8").trim();
   String quote=Character.toString((char)34);
   java.nio.file.Files.write(output,("package generated; public class Value {public static String get(){return "+quote+value+quote+";}}").getBytes("UTF-8"));
   project.addCompileSourceRoot(root.toString());
   java.nio.file.Files.write(root.resolve("calls"),new byte[]{1},java.nio.file.StandardOpenOption.CREATE,java.nio.file.StandardOpenOption.APPEND);
  }catch(Exception e){throw new org.apache.maven.plugin.MojoExecutionException("fixture-generator-failed",e);}
 }
}""")
    classes = tmp_path / "plugin-classes"
    classes.mkdir()
    classpath = os.pathsep.join(str(p) for p in (home / "lib").glob("*.jar"))
    javac = Path(env["JAVA_HOME"]) / (
        "bin/javac.exe" if os.name == "nt" else "bin/javac"
    )
    compiled = subprocess.run(
        [
            str(javac),
            "-source",
            "8",
            "-target",
            "8",
            "-cp",
            classpath,
            "-d",
            str(classes),
            str(source),
        ],
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )
    assert compiled.returncode == 0, compiled.stderr
    mojos = ""
    for goal in ("work", "fork"):
        mojos += f"""<mojo><goal>{goal}</goal><implementation>fixture.Step</implementation>
<language>java</language><phase>process-sources</phase><requiresProject>true</requiresProject>
<instantiationStrategy>per-lookup</instantiationStrategy><executionStrategy>once-per-session</executionStrategy>
{"<executePhase>compile</executePhase>" if goal == "fork" else ""}
<parameters>
<parameter><name>project</name><type>org.apache.maven.project.MavenProject</type><required>true</required><editable>false</editable></parameter>
<parameter><name>mode</name><type>java.lang.String</type><required>true</required><editable>true</editable></parameter>
<parameter><name>valueFile</name><type>java.io.File</type><required>true</required><editable>true</editable></parameter>
</parameters><configuration>
<project implementation="org.apache.maven.project.MavenProject" default-value="${{project}}"/>
<mode implementation="java.lang.String" default-value="generate"/>
<valueFile implementation="java.io.File" default-value="${{project.basedir}}/spec/value.txt"/>
</configuration></mojo>"""
    jar = tmp_path / "fixture.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        archive.write(classes / "fixture/Step.class", "fixture/Step.class")
        archive.writestr(
            "META-INF/maven/plugin.xml",
            f"""<plugin><name>Preparation fixture</name>
<groupId>{group}</groupId><artifactId>fixture-plugin</artifactId><version>1.0</version>
<goalPrefix>fixture</goalPrefix><mojos>{mojos}</mojos><dependencies/></plugin>""",
        )
        archive.writestr(
            "META-INF/m2e/lifecycle-mapping-metadata.xml",
            """<lifecycleMappingMetadata><pluginExecutions>
<pluginExecution><pluginExecutionFilter><goals><goal>work</goal></goals><parameters><mode>quality</mode></parameters></pluginExecutionFilter>
<action><ignore/></action></pluginExecution></pluginExecutions></lifecycleMappingMetadata>""",
        )
    mvn = Path(home) / ("bin/mvn.cmd" if os.name == "nt" else "bin/mvn")
    command = [str(mvn), "--offline", "-B"]
    settings = os.environ.get("JOLINK_TEST_MAVEN_SETTINGS")
    if settings:
        command += ["-s", settings]
    installed = subprocess.run(
        [
            *command,
            "org.apache.maven.plugins:maven-install-plugin:2.5.2:install-file",
            f"-Dfile={jar}",
            f"-DgroupId={group}",
            "-DartifactId=fixture-plugin",
            "-Dversion=1.0",
            "-Dpackaging=jar",
        ],
        check=False,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    return group


def mapping(group, goal="fork", action="ignore", version="[1,2)", parameters=""):
    return f"""<pluginExecution><pluginExecutionFilter><groupId>{group}</groupId>
<artifactId>fixture-plugin</artifactId><versionRange>{version}</versionRange><goals><goal>{goal}</goal></goals>
{parameters}</pluginExecutionFilter><action><{action}/></action></pluginExecution>"""


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("location", ["project", "profile", "parent"])
def test_maven_mapping_keeps_generators_and_skips_declared_checks(tmp_path, location):
    require_real_mcp_java_e2e()
    java = os.environ.get("JOLINK_TEST_JAVA17_HOME") or os.environ.get("JAVA_HOME")
    if not java:
        pytest.skip("set JOLINK_TEST_JAVA17_HOME")
    env = {
        **os.environ,
        "JAVA_HOME": java,
        "PATH": java + "/bin" + os.pathsep + os.environ["PATH"],
        "MAVEN_ARGS": "--offline",
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    if os.name == "nt":
        env["LOCALAPPDATA"] = str(tmp_path / "cache")
    group = fixture_plugin(tmp_path, env)
    root, _app, config, source, port = project_fixture(tmp_path, "maven", False)
    value = root / "spec/value.txt"
    value.parent.mkdir()
    value.write_text("A")
    source.write_text(
        source.read_text().replace(
            "return Character.toString((char)s.read());",
            "return generated.Value.get();",
        )
    )
    plugin = f"""<plugin><groupId>{group}</groupId><artifactId>fixture-plugin</artifactId><version>1.0</version>
<executions>
<execution><id>generated</id><phase>process-sources</phase><goals><goal>work</goal></goals></execution>
<execution><id>quality</id><phase>process-sources</phase><goals><goal>work</goal></goals><configuration><mode>quality</mode></configuration></execution>
<execution><id>forked</id><phase>process-sources</phase><goals><goal>fork</goal></goals><configuration><mode>quality</mode></configuration></execution>
</executions></plugin>"""
    declaration = mapping(group)
    management = f"""<pluginManagement><plugins><plugin><groupId>org.eclipse.m2e</groupId><artifactId>lifecycle-mapping</artifactId><version>1.0.0</version>
<configuration><lifecycleMappingMetadata><pluginExecutions>{declaration}</pluginExecutions></lifecycleMappingMetadata></configuration></plugin></plugins></pluginManagement>"""
    text = config.read_text().replace("</plugins>", plugin + "</plugins>")
    if location == "profile":
        text = text.replace(
            "</project>",
            f"<profiles><profile><id>development</id><activation><activeByDefault>true</activeByDefault></activation><build>{management}</build></profile></profiles></project>",
        )
    elif location == "parent":
        parent = root / "parent/pom.xml"
        parent.parent.mkdir()
        parent.write_text(
            f"<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>mapping-parent</artifactId><version>1</version><packaging>pom</packaging><build>{management}</build></project>"
        )
        text = text.replace(
            "</modelVersion>",
            "</modelVersion><parent><groupId>example</groupId><artifactId>mapping-parent</artifactId><version>1</version><relativePath>parent/pom.xml</relativePath></parent>",
        )
        # Keep later override tests on the actual inherited configuration source.
        config.write_text(text)
        config = parent
        text = parent.read_text()
    else:
        text = text.replace("<build>", "<build>" + management)
    config.write_text(text)
    settings = os.environ.get("JOLINK_TEST_MAVEN_SETTINGS")

    async def scenario():
        for cycle in range(2):
            with temporary_stderr() as log:
                async with open_mcp_session(log, environment=env) as session:

                    async def call(tool, **args):
                        return dict(
                            (await session.call_tool(tool, args)).structuredContent
                        )

                    async def run():
                        args = {
                            "project_path": str(root),
                            "tests": ["example.ReadyTest"],
                        }
                        if settings:
                            args["maven_settings"] = settings
                        if os.environ.get("JOLINK_TEST_MAVEN_HOME"):
                            args["maven_home"] = os.environ["JOLINK_TEST_MAVEN_HOME"]
                        result = await call("java_fast_test", **args)
                        with anyio.fail_after(120):
                            while result.get("status") in {
                                "starting",
                                "bootstrapping",
                                "compiling",
                                "running",
                            }:
                                await anyio.sleep(0.2)
                                result = await call(
                                    "java_fast_test",
                                    action="result",
                                    test_run_id=result["test_run_id"],
                                )
                        if not result.get("passed"):
                            result = await call(
                                "java_fast_test",
                                action="result",
                                test_run_id=result["test_run_id"],
                            )
                        return result

                    result = await run()
                    assert result.get("passed") is True, json.dumps(result)
                    assert set(result["skipped_build_steps"]) == {
                        f"{group}:fixture-plugin:work@quality",
                        f"{group}:fixture-plugin:fork@forked",
                    }
                    calls = root / "target/generated-fixture/calls"
                    before = calls.read_bytes()
                    assert (await run())["compiled_source_count"] == 0
                    assert calls.read_bytes() == before
                    if not cycle:
                        source.write_text(source.read_text() + "\n// incremental\n")
                        assert (await run())["passed"]
                        assert calls.read_bytes() == before
                        value.write_text("B")
                        assert (await run()).get("failed_count") == 1
                        value.write_text("A")
                        assert (await run())["passed"]
                        # Invalid matches must not accidentally suppress a forked goal.
                        for bad in (
                            mapping(group, version="[2,3)"),
                            mapping(group + ".other"),
                            mapping(group, goal="absent"),
                        ):
                            config.write_text(text.replace(declaration, bad))
                            failed = await run()
                            assert (
                                failed.get("error_code") == "FAST_TEST_BOOTSTRAP_FAILED"
                            ), failed
                            assert "forked lifecycle" in "\n".join(
                                failed["bootstrap_log_tail"]
                            ), failed
                        # An explicit project execute overrides the plugin's ignore.
                        override = mapping(
                            group,
                            "work",
                            "execute",
                            parameters="<parameters><mode>quality</mode></parameters>",
                        )
                        config.write_text(
                            text.replace(declaration, declaration + override)
                        )
                        failed = await run()
                        assert "fixture-check-executed" in "\n".join(
                            failed["bootstrap_log_tail"]
                        ), failed
                        config.write_text(text)
                        assert (await run())["passed"]
                    args = {
                        "action": "launch",
                        "project_path": str(root),
                        "main_class": "example.App",
                        "ready_port": port,
                        "jdwp_port": reserve_local_port(),
                    }
                    if settings:
                        args["maven_settings"] = settings
                    if os.environ.get("JOLINK_TEST_MAVEN_HOME"):
                        args["maven_home"] = os.environ["JOLINK_TEST_MAVEN_HOME"]
                    try:
                        launched = await call("java_application", **args)
                        with anyio.fail_after(120):
                            while launched.get("launch_phase") not in {
                                "runtime_active",
                                "failed",
                            } and launched.get("ok", True):
                                await anyio.sleep(0.2)
                                launched = await call(
                                    "java_status", action="status", details=True
                                )
                        assert launched.get("startup_state") == "ready", launched
                        assert any(
                            "skipped build steps" in warning
                            for warning in launched.get("warnings", [])
                        ), launched
                        assert response(port) == "A"
                    finally:
                        await call("java_application", action="stop")

    anyio.run(scenario)
