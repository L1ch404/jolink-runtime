"""Gradle owns the module descriptor/version. Verify the JVM, not just compilation."""
import shutil

import anyio
import pytest

from java_support import call_workflow, open_mcp_session, reserve_local_port
from test_jpms_runtime import _fixture, _http, _native, _native_command, _native_launch

pytestmark = pytest.mark.mcp_java_e2e


@pytest.mark.parametrize("reactor,patched,versioned_dependency", [(False, False, False), (True, True, False), (True, True, True)])
def test_native_gradle_keeps_runtime_module_version_and_test_policy(tmp_path, reactor, patched, versioned_dependency):
    project, app, source, _, expected_file, java, env = _fixture(tmp_path, "gradle", reactor, patched)
    with (app / "build.gradle").open("a") as f:
        f.write("\ncompileJava.options.compilerArgs += ['--module-version','1.2.3']\n")
    if versioned_dependency:
        build = project / "build.gradle"
        build.write_text(build.read_text().replace("project(':app') {", """
project(':lib') {
def base = layout.buildDirectory.dir('classes/java/main')
compileJava.destinationDirectory.set(base.map { it.dir('META-INF/versions/17') })
sourceSets.main.output.classesDirs.setFrom(base)
jar { dependsOn compileJava; manifest.attributes('Multi-Release': 'true') }
}
project(':app') {
configurations.compileClasspath.attributes {
 attribute(LibraryElements.LIBRARY_ELEMENTS_ATTRIBUTE, objects.named(LibraryElements, LibraryElements.JAR))
}
"""))
    source.write_text(source.read_text().replace('+"|"+upstream()', '+"@"+Service.class.getModule().getDescriptor().rawVersion().get()+"|"+upstream()'))
    native = tmp_path / "native"
    shutil.copytree(project, native)
    _native(_native_command(native, "gradle"), native, env, tmp_path / "native-test.log")
    def body(value):
        upstream = "example.lib:40" if reactor else "none"
        return f"example.app@1.2.3|{upstream}|{value}|resource-v1"
    _native_launch(native, "gradle", reactor, java, env, body("v1"), tmp_path / "native-launch.log")
    common = dict(project_path=str(app), build_java_home=str(java), java_home=str(java), timeout=30)
    port = reserve_local_port()
    launch = dict(common, action="launch", main_class="example.App", app_args=[str(port)], ready_port=port,
                  jdwp_port=reserve_local_port())
    async def scenario():
        for reopened in (False, True):
            with (tmp_path / f"server-{reopened}.log").open("w") as out:
                async with open_mcp_session(out, environment=env) as session:
                    async def app_call(args):
                        return await call_workflow(session, "java_application", args, tmp_path / "calls.jsonl")
                    async def check():
                        result = await call_workflow(session, "java_fast_test", dict(common, tests=["example.ModuleTest"]), tmp_path / "calls.jsonl")
                        assert result.get("passed") and result["tests"] == 1 and result["compile_backend"] == "gradle_native", result
                        return result
                    try:
                        cold = await check()
                        if reopened:
                            assert cold["compiled_source_count"] == 0
                        started = await app_call(launch)
                        assert started.get("startup_state") == "ready", started
                        assert _http(port) == body("v2" if reopened else "v1")
                        assert (await check())["compiled_source_count"] == 0
                        if not reopened:
                            source.write_text(source.read_text().replace('return "v1";', 'return "v2";'))
                            red = await call_workflow(session, "java_fast_test", dict(common, tests=["example.ModuleTest"]))
                            assert red.get("failed_count") == 1, red
                            expected_file.write_text("v2")
                            await check()
                            assert _http(port) == body("v1")
                            updated = await app_call(dict(action="restart", timeout=30))
                            assert updated.get("apply_method") == "hotswap" and _http(port) == body("v2"), updated
                        else:
                            # Named modules cannot acquire a previously unknown
                            # package by putting files in an existing JVM's output.
                            new_class = app / "src/main/java/fresh/Next.java"
                            new_class.parent.mkdir()
                            new_class.write_text('package fresh; public class Next {public static String id(){return "v3";}}')
                            source.write_text(source.read_text().replace('return "v2";', 'return fresh.Next.id();'))
                            updated = await app_call(dict(action="restart", timeout=30))
                            assert updated.get("apply_method") == "restart" and updated.get("pid") != started["pid"], updated
                            assert _http(port) == body("v3")
                    finally:
                        await app_call(dict(action="stop"))
    anyio.run(scenario)
