"""Cold native discovery must associate configuration content with its model."""

import json
import os
from urllib.request import urlopen

import anyio
import pytest

from java_support import open_mcp_session, reserve_local_port
from test_gradle_probe_configuration_race import _fixture as gradle_fixture
from test_probe_configuration_race import _fixture as maven_fixture


pytestmark = pytest.mark.mcp_java_e2e


def _fixture(tmp_path, scenario):
    if scenario.startswith("gradle"):
        child = scenario in {"gradle-child-from-root", "gradle-child-properties"}
        root, app, config, gate, port, env, launch, test = gradle_fixture(tmp_path, child)
        if child:
            # Neither known target-directory inputs nor a previous cache reveal
            # this child's build file before the first native module selection.
            launch["project_path"] = test["project_path"] = str(root)
            if scenario == "gradle-child-properties":
                config.write_text(config.read_text().replace("def parameterNames = false",
                    "def parameterNames = project.findProperty('parameterNames').toString().toBoolean()"))
                config = app / "gradle.properties"
                config.write_text("parameterNames = false\n")
                # Child properties are installed before root script evaluation;
                # a beforeProject callback for the child is already too late.
                (root / "build.gradle").write_text("""
def rootGate = file(GATE)
if(new File(rootGate,'armed').isFile()) {
 new File(rootGate,'observed').text = project(':app').property('parameterNames').toString()
 long deadline=System.nanoTime()+120000000000L
 while(!new File(rootGate,'release').isFile()) {
  if(System.nanoTime()>deadline) throw new GradleException('root property gate timed out')
  Thread.sleep(25)
 }
}
""".replace("GATE", json.dumps(gate.as_posix())))
        else:
            script = tmp_path / "external/compilation.rules"
            script.parent.mkdir()
            text = config.read_text()
            start = text.index("def parameterNames")
            end = text.index("dependencies {")
            script.write_text(text[start:end], encoding="utf-8")
            config.write_text(text[:start] + "apply from: " + json.dumps(script.as_posix()) + "\n" + text[end:])
            config = script
            if scenario == "gradle-script-evaluation":
                # Gate inside the applied script itself: recording its bytes
                # only at projectsEvaluated/export would already be too late.
                config.write_text(config.read_text() + """
def evaluationGate = file(GATE)
if(new File(evaluationGate,'armed').isFile()) {
 new File(evaluationGate,'observed').text = tasks.named('compileJava').get().options.compilerArgs.toString()
 long deadline=System.nanoTime()+120000000000L
 while(!new File(evaluationGate,'release').isFile()) {
  if(System.nanoTime()>deadline) throw new GradleException('script evaluation gate timed out')
  Thread.sleep(25)
 }
}
""".replace("GATE", json.dumps(gate.as_posix())))
        marker = gate / "observed"
        def configure(value):
            old = "false" if value else "true"
            new = "true" if value else "false"
            config.write_text(config.read_text().replace("parameterNames = " + old, "parameterNames = " + new))
    else:
        root, app, root_pom, gate, expected, source, java, env = maven_fixture(tmp_path, True)
        # Unlike the one-shot legacy barrier, a released gate must not rewrite
        # its marker on every Probe: it is a real declared preparation input,
        # and doing so would invalidate the sibling launch/test resource cache.
        app_pom = app / "pom.xml"
        app_pom.write_text(app_pom.read_text().replace("<target>",
            '<target xmlns:unless="ant:unless"><available file="' + (gate / "release").as_posix()
            + '" property="gate.released"/>').replace("<echo file=", '<echo unless:set="gate.released" file='))
        # Python's static ordinary-modules traversal does not discover this
        # active profile's upstream module; Maven owns profile interpretation.
        modules = "<modules><module>lib</module><module>app</module></modules>"
        root_pom.write_text(root_pom.read_text().replace(modules,
            "<profiles><profile><id>native-modules</id><activation><activeByDefault>true</activeByDefault>"
            "</activation>" + modules + "</profile></profiles>"))
        config = root / "lib/pom.xml"
        if scenario == "maven-profile-parent":
            original_parent = "<parent><groupId>example</groupId><artifactId>race-root</artifactId><version>1</version></parent>"
            config.write_text(config.read_text().replace(original_parent,
                "<parent><groupId>example</groupId><artifactId>configuration-parent</artifactId><version>1</version>"
                "<relativePath>../../external-parent/pom.xml</relativePath></parent>"))
            config = tmp_path / "external-parent/pom.xml"
            config.parent.mkdir()
            config.write_text("""<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>configuration-parent</artifactId><version>1</version><packaging>pom</packaging>
<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding><maven.compiler.parameters>false</maven.compiler.parameters></properties>
<build><plugins><plugin><artifactId>maven-resources-plugin</artifactId><version>3.3.0</version></plugin>
<plugin><artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version>
<configuration><parameters>${maven.compiler.parameters}</parameters></configuration></plugin></plugins></build>
</project>""")
        else:
            config.write_text(config.read_text().replace("</project>",
                "<properties><maven.compiler.parameters>false</maven.compiler.parameters></properties></project>"))
        # Only the unknown upstream POM controls the observed bytecode; the
        # root/target POMs and application source remain unchanged during races.
        source.write_text(source.read_text().replace(
            'App.class.getMethod("argument",String.class).getParameters()[0].isNamePresent() && ', ""))
        port = reserve_local_port()
        common = dict(project_path=str(app), java_home=str(java), build_java_home=str(java), timeout=0)
        if os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
            common["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]
        launch = dict(common, action="launch", main_class="example.App", ready_port=port,
                      jdwp_port=reserve_local_port(), app_args=[str(port)])
        test = dict(common, tests=["example.ConfigTest"])
        # The barrier in app originally observes the root's false property. Keep
        # it false for the cold proof; subsequent races still have a real gate.
        marker = gate / "entered"
        def configure(value):
            old = "false" if value else "true"
            new = "true" if value else "false"
            config.write_text(config.read_text().replace(
                "<maven.compiler.parameters>" + old, "<maven.compiler.parameters>" + new))
            expected.write_text(new)
    return root, config, gate, marker, port, env, launch, test, configure


@pytest.mark.parametrize("entry,scenario", [
    (entry, scenario) for entry in ("test", "launch")
    for scenario in ("gradle-child-from-root", "gradle-external-script", "maven-profile-upstream", "maven-profile-parent", "gradle-child-properties")
] + [("test", "gradle-script-evaluation")])
def test_cold_discovered_configuration_edit_is_not_published(tmp_path, scenario, entry):
    root, config, gate, marker, port, env, launch, test, configure = _fixture(tmp_path, scenario)
    observations = {}
    unchanged = {path: path.read_bytes() for path in root.rglob("*.java")}

    def http():
        with urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            assert response.status == 200
            return response.read().decode("utf-8")

    async def call(session, tool, args):
        result = dict((await session.call_tool(tool, args)).structuredContent)
        with (tmp_path / "calls.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(dict(tool=tool, arguments=args, result=result), ensure_ascii=False) + "\n")
        return result

    async def finish(session, result, kind):
        with anyio.fail_after(180):
            if kind == "test":
                while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                    await anyio.sleep(.1)
                    result = await call(session, "java_fast_test", dict(action="result", test_run_id=result["test_run_id"]))
            else:
                while result.get("ok") and result.get("launch_phase") not in {"runtime_active", "failed", "cancelled"}:
                    await anyio.sleep(.1)
                    result = await call(session, "java_status", dict(action="status", details=True))
        return result

    async def run(session, kind):
        tool = "java_fast_test" if kind == "test" else "java_application"
        arguments = test if kind == "test" else launch if kind == "launch" else dict(action="restart", timeout=0)
        return await call(session, tool, {**arguments, "timeout": 0})

    async def success(session, kind):
        result = await finish(session, await run(session, kind), kind)
        if kind == "test":
            assert result.get("passed") and result.get("tests") == 1, result
        else:
            assert result.get("startup_state") == "ready" and http() == "true", result
        return result

    async def race(session, kind):
        marker.unlink(missing_ok=True)
        (gate / "release").unlink(missing_ok=True)
        (gate / "armed").touch()
        configure(False)
        initial = await run(session, kind)
        try:
            boolean_marker = scenario.startswith("maven") or scenario == "gradle-child-properties"
            complete_values = {"false", "true"} if boolean_marker else {"[]", "[-parameters]"}
            with anyio.fail_after(100):
                # The native writer creates/truncates before writing its value.
                # Wait for a complete fixture value, including the wrong value
                # that must fail the next assertion, rather than a partial write.
                while True:
                    marker_value = marker.read_text().strip() if marker.is_file() else ""
                    if marker_value in complete_values:
                        break
                    await anyio.sleep(.05)
            if boolean_marker:
                assert marker_value == "false"
            else:
                assert marker_value == "[]"
            configure(True)
        finally:
            (gate / "armed").unlink(missing_ok=True)
            (gate / "release").touch()
        result = await finish(session, initial, kind)
        observations[kind] = result
        (tmp_path / "observations.json").write_text(json.dumps(observations, indent=2))
        error = result.get("launch_error", result)
        assert error.get("error_code") == "BUILD_CONFIGURATION_CHANGED", result
        assert error.get("retryable") is True, result
        assert result.get("compiled_source_count", 0) == 0, result
        assert {path: path.read_bytes() for path in unchanged} == unchanged
        return result

    async def scenario_run():
        with (tmp_path / "mcp.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                try:
                    await race(session, entry)
                    assert not list((tmp_path / "cache").rglob("build-world.json"))
                    await success(session, entry)
                    await success(session, "launch" if entry == "test" else "test")
                    assert (await success(session, "test"))["compiled_source_count"] == 0
                    await race(session, "restart")
                    await success(session, "restart")
                    assert (await success(session, "test"))["passed"]
                    assert (await success(session, "test"))["compiled_source_count"] == 0
                finally:
                    if not (gate / "release").is_file():
                        (gate / "release").touch()
                    await call(session, "java_application", dict(action="stop"))
        with (tmp_path / "reopen.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                try:
                    assert (await success(session, "test"))["compiled_source_count"] == 0
                    launched = await success(session, "launch")
                    assert launched.get("probe_cache_reused") is True, launched
                    assert launched.get("jdt_bootstrap_reused") is True, launched
                    assert launched.get("jdt_bootstrap_build_kind") is None, launched
                finally:
                    await call(session, "java_application", dict(action="stop"))
    anyio.run(scenario_run)
