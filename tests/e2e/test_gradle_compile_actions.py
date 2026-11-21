"""A JavaCompile action can change the meaning of the existing class output."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.request import urlopen

import anyio
import pytest

from java_support import call_workflow, open_mcp_session, require_real_mcp_java_e2e, reserve_local_port


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("layout", ["project-main", "project-main-explicit", "upstream-plugin", "project-test",
                                    "javac-main", "javac-test"])
def test_gradle_java_compile_actions_are_applied_to_the_correct_scope(tmp_path, layout):
    require_real_mcp_java_e2e()
    gradle = os.environ.get("JOLINK_FAST_TEST_GRADLE")
    home = os.environ.get("JOLINK_TEST_JAVA17_HOME")
    if not gradle or not home:
        pytest.skip("a local Gradle distribution and JDK 17 are required")
    root = tmp_path / "project"
    multi, test_only = layout == "upstream-plugin", layout in {"project-test", "javac-test"}
    native_args = layout.startswith("javac-")
    main_action = layout.startswith("project-main")
    app, owner = (root / "app", root / "producer") if multi else (root, root)
    wrapper = _write(root / ("gradlew.bat" if os.name == "nt" else "gradlew"),
                     f'@call "{gradle}" %*\r\n' if os.name == "nt" else f'#!/bin/sh\nexec "{gradle}" "$@"\n')
    wrapper.chmod(0o755)
    version = os.environ["JOLINK_FAST_TEST_GRADLE_VERSION"]
    _write(root / "gradle/wrapper/gradle-wrapper.properties",
           f"distributionUrl=https://services.gradle.org/distributions/gradle-{version}-bin.zip\n")
    _write(root / "settings.gradle", "rootProject.name='compile-action'\n" + ("include 'app','producer'\n" if multi else ""))
    m2 = Path.home() / ".m2/repository"
    junit = m2 / "junit/junit/4.13.2/junit-4.13.2.jar"
    hamcrest = m2 / "org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar"
    consumer = f"dependencies {{testImplementation files('{junit.as_posix()}','{hamcrest.as_posix()}')}}\n"
    java = "apply plugin: 'java-library'\n"
    # No additional class output. The action changes the existing JavaCompile
    # output in place; byte length and class structure do not change.
    target = "TestPayload" if test_only else "Payload"
    transform = """
        task.doLast {
            File output = new File(task.destinationDirectory.get().asFile, 'example/TARGET.class')
            String original = new String(output.bytes, 'ISO-8859-1')
            if (!original.contains('-marker-')) throw new GradleException('missing bytecode marker')
            String prefix = PREFIX
            output.bytes = original.replaceAll(/(?:raw|new|alt|opt)-marker-/, prefix + '-marker-').getBytes('ISO-8859-1')
        }
    """.replace("TARGET", target)
    mode_task = ""
    if main_action:
        _write(root / "weave-mode.txt", "new")
        mode_task = """
            def prepareMode = tasks.register('prepareMode', Copy) {
                from layout.projectDirectory.file('weave-mode.txt')
                into layout.buildDirectory.dir('compiler-config')
            }
        """
        mode = ("def mode = prepareMode.map { new File(it.destinationDir, 'weave-mode.txt') }"
                if layout == "project-main" else """task.dependsOn prepareMode
                def mode = layout.buildDirectory.file('compiler-config/weave-mode.txt').map { it.asFile }""")
        transform = """
            MODE_INPUT
            FileTree overrides = project.fileTree('optional-mode') { include '**/*.rule' }
            task.inputs.file(mode).withPropertyName('weaveMode')
            task.inputs.files(overrides).withPropertyName('weaveOverrides')
        """.replace("MODE_INPUT", mode) + transform.replace("PREFIX", "(overrides.files.sort() ?: [mode.get()]).first().text.trim()")
    else:
        transform = transform.replace("PREFIX", "'new'")
    if multi:
        _write(root / "build.gradle", "")
        _write(app / "build.gradle", java + consumer + "dependencies {implementation project(':producer')}\n")
        # A plugin, applied before the Java plugin, configures a lazily created task.
        plugin = """
            class PayloadPlugin implements Plugin<Project> {
                void apply(Project p) {
                    p.tasks.withType(JavaCompile).configureEach { task ->
                        if (task.name == 'compileJava') { ACTION }
                    }
                }
            }
            apply plugin: PayloadPlugin
        """
        _write(owner / "build.gradle", plugin.replace("ACTION", transform) + java)
    else:
        task = "compileTestJava" if test_only else "compileJava"
        if native_args:
            _write(root / "warning-mode", "allow")
            mode_task = """
            class WarningArgs implements org.gradle.process.CommandLineArgumentProvider {
                @InputFile @PathSensitive(PathSensitivity.RELATIVE) File mode
                Iterable<String> asArguments() { mode.text.trim() == 'deny' ? ['-Xlint:rawtypes','-Werror'] : ['-Xlint:none'] }
            }
            """
            transform = """task.options.compilerArgumentProviders.add(new WarningArgs(mode: project.file('warning-mode')))"""
            if not test_only:
                transform += "\ntask.options.compilerArgs.add('-XDenableSunApiLintControl')"
        _write(owner / "build.gradle", java + consumer + mode_task + f"tasks.named('{task}', JavaCompile) {{ task ->\n" + transform + "\n}\n")

    def payload(scope, name, value):
        return _write(owner / f"src/{scope}/java/example/{name}.java",
                      f'package example; public class {name} {{public static String value() {{return "{value}";}}}}\n')

    payload("main", "Payload", "raw-marker-A")
    if test_only:
        payload("test", "TestPayload", "raw-marker-A")
    if native_args:
        _write(owner / f"src/{'test' if test_only else 'main'}/java/example/Warn.java",
               'package example; class Warn { java.util.List raw; }')
    _write(app / "src/main/java/example/App.java", """package example;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
public class App {
    public static void main(String[] args) throws Exception {
        HttpServer s = HttpServer.create(new InetSocketAddress("127.0.0.1", Integer.parseInt(args[0])), 0);
        s.createContext("/", e -> {
            byte[] bytes = Payload.value().getBytes("UTF-8");
            e.sendResponseHeaders(200, bytes.length);
            try (java.io.OutputStream out = e.getResponseBody()) {out.write(bytes);}
        });
        s.start();
    }
}""")
    version_file = _write(tmp_path / "expected-version", "A")
    prefix_file = _write(tmp_path / "expected-prefix", "raw" if test_only or native_args else "new")
    _write(app / "src/test/java/example/PayloadTest.java", """package example;
public class PayloadTest {
    @org.junit.Test public void verifiesExecutableClass() throws Exception {
        String version = java.nio.file.Files.readString(java.nio.file.Paths.get(EXPECTED));
        String prefix = java.nio.file.Files.readString(java.nio.file.Paths.get(EXPECTED_PREFIX));
        org.junit.Assert.assertEquals(prefix+"-marker-"+version, Payload.value());
        TEST_ASSERTION
    }
}""".replace("EXPECTED_PREFIX", json.dumps(prefix_file.as_posix()))
            .replace("EXPECTED", json.dumps(version_file.as_posix()))
            .replace("TEST_ASSERTION", ('org.junit.Assert.assertEquals("'+('raw' if native_args else 'new')
                     +'-marker-"+version, TestPayload.value());') if test_only else ""))

    env = {**os.environ, "JAVA_HOME": home, "PATH": str(Path(home) / "bin") + os.pathsep + os.environ["PATH"],
           "GRADLE_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache")}
    if os.name == "nt":
        env["LOCALAPPDATA"] = env["XDG_CACHE_HOME"]
    oracle = tmp_path / "native"
    shutil.copytree(root, oracle)
    native = subprocess.run([str(oracle / wrapper.name), "--offline", "--no-daemon", ":app:test" if multi else ":test",
                             "--tests", "example.PayloadTest"], cwd=oracle, env=env, capture_output=True, text=True, timeout=180)
    (tmp_path / "native.log").write_text(native.stdout + native.stderr, encoding="utf-8")
    assert native.returncode == 0, native.stdout + native.stderr
    if native_args:
        _write(oracle / "warning-mode", "deny")
        strict = subprocess.run([str(oracle / wrapper.name), "--offline", "--no-daemon", ":testClasses"],
                                cwd=oracle, env=env, capture_output=True, text=True, timeout=180)
        assert strict.returncode != 0 and "-Werror" in strict.stdout + strict.stderr
    if main_action:
        for path, value in (("weave-mode.txt", "alt"), ("optional-mode/nested/switch.rule", "opt")):
            _write(oracle / path, value)
            compile_again = subprocess.run([str(oracle / wrapper.name), "--offline", "--no-daemon", ":compileJava"],
                                           cwd=oracle, env=env, capture_output=True, text=True, timeout=180)
            assert compile_again.returncode == 0, compile_again.stdout + compile_again.stderr
            assert (value + "-marker-A").encode() in (oracle / "build/classes/java/main/example/Payload.class").read_bytes()
            assert (oracle / "build/compiler-config/weave-mode.txt").read_text() == "alt"
    assert not (owner / "build").exists()

    port = reserve_local_port()
    common = dict(project_path=str(app), java_home=home, build_java_home=home, timeout=30)
    launch = dict(common, action="launch", main_class="example.App", app_args=[str(port)],
                  ready_port=port, jdwp_port=reserve_local_port())
    def expected():
        return prefix_file.read_text(encoding="utf-8") + "-marker-" + version_file.read_text(encoding="utf-8")

    def http():
        with urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            return response.read().decode()

    async def run_session(reopened):
        with (tmp_path / f"mcp-{reopened}.log").open("w", encoding="utf-8") as log:
            async with open_mcp_session(log, environment=env) as session:
                async def call(tool, arguments):
                    return await call_workflow(session, tool, arguments, tmp_path / "calls.jsonl")

                async def check():
                    result = await call("java_fast_test", dict(common, tests=["example.PayloadTest"]))
                    if not result.get("passed") and "test_run_id" in result:
                        result = await call("java_fast_test", dict(action="result", test_run_id=result["test_run_id"]))
                    assert result.get("passed") and result.get("tests") == 1, result
                    return result

                try:
                    if not reopened and main_action:
                        await check()  # No existing app outputs can satisfy a cold Fast Test.
                    started = await call("java_application", dict(launch, timeout=0))
                    assert started.get("startup_state") == "ready", started
                    assert http() == expected()
                    if test_only:
                        assert started["fast_update"]["strategy"] == "jdt_incremental", started
                    first = await check()
                    if native_args:
                        assert first.get("compile_backend") == "gradle_native", first
                    if reopened:
                        assert first.get("compiled_source_count") == 0, first
                        return
                    assert (await check()).get("compiled_source_count") == 0
                    if native_args:
                        # Only change the provider's declared input: no Java change masks a stale native session.
                        _write(root / "warning-mode", "deny")
                        failed = await call("java_fast_test", dict(common, tests=["example.PayloadTest"]))
                        assert failed.get("error_code") == "GRADLE_NATIVE_COMPILE_FAILED", failed
                        if not test_only:
                            failed = await call("java_application", dict(action="restart", timeout=0))
                            assert failed.get("error_code") == "GRADLE_NATIVE_COMPILE_FAILED", failed
                        assert http() == expected()
                        _write(root / "warning-mode", "allow")
                        await check()
                    previous = expected()
                    payload("main", "Payload", "raw-marker-B")
                    if test_only:
                        payload("test", "TestPayload", "raw-marker-B")
                    version_file.write_text("B", encoding="utf-8")
                    await check()
                    assert http() == previous, "test compilation changed the live application"
                    updated = await call("java_application", dict(action="restart", timeout=0))
                    assert updated.get("ok") and http() == expected(), updated
                    if test_only:
                        assert updated.get("compile_backend") != "gradle_native", updated
                    if main_action:
                        # Input changes alone must re-run the action, including a
                        # matching file created after a cold, empty fileTree.
                        _write(root / "optional-mode/skip.bin", "not an input")
                        assert (await check()).get("compiled_source_count") == 0
                        override = root / "optional-mode/nested/switch.rule"
                        for path, contents, prefix in ((root / "weave-mode.txt", "alt", "alt"),
                                                       (override, "opt", "opt"), (override, None, "alt")):
                            old = expected()
                            if contents is None:
                                path.unlink()
                            else:
                                _write(path, contents)
                            prefix_file.write_text(prefix, encoding="utf-8")
                            await check()
                            assert http() == old
                            update = await call("java_application", dict(action="restart", timeout=30))
                            assert update.get("ok") and http() == expected(), update
                finally:
                    await session.call_tool("java_application", dict(action="stop"))

    anyio.run(run_session, False)
    anyio.run(run_session, True)
