"""MCP must consume the bytes of declared Gradle outputs, not just create their directories."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.request import urlopen

import anyio
import pytest

from java_support import call_workflow, open_mcp_session, require_real_mcp_java_e2e, reserve_local_port


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("layout", ["main", "upstream", "test-only"])
def test_declared_gradle_outputs_follow_their_producers_and_stay_isolated(tmp_path, layout):
    require_real_mcp_java_e2e()
    gradle = os.environ.get("JOLINK_FAST_TEST_GRADLE")
    home = os.environ.get("JOLINK_TEST_JAVA17_HOME")
    if not gradle or not home:
        pytest.skip("a local Gradle distribution and JDK17 are required")

    root = tmp_path / "project"
    multi = layout == "upstream"
    test_only = layout == "test-only"
    app = root / "application" if multi else root
    owner = root / "producer" if multi else root
    scope = "test" if test_only else "main"
    java_home = Path(home)
    env = {**os.environ, "JAVA_HOME": home, "PATH": str(java_home / "bin") + os.pathsep + os.environ["PATH"],
           "GRADLE_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache"), "JOLINK_LOG_LEVEL": "INFO"}
    if os.name == "nt":
        env["LOCALAPPDATA"] = env["XDG_CACHE_HOME"]

    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    wrapper = write(root / ("gradlew.bat" if os.name == "nt" else "gradlew"),
                    f'@call "{gradle}" %*\r\n' if os.name == "nt" else f'#!/bin/sh\nexec "{gradle}" "$@"\n')
    wrapper.chmod(0o755)
    version = os.environ["JOLINK_FAST_TEST_GRADLE_VERSION"]
    write(root / "gradle/wrapper/gradle-wrapper.properties",
          f"distributionUrl=https://services.gradle.org/distributions/gradle-{version}-bin.zip\n")
    write(root / "settings.gradle", "rootProject.name='fixture'\n" + ("include 'application','producer'\n" if multi else ""))
    m2 = Path.home() / ".m2/repository"
    junit = m2 / "junit/junit/4.13.2/junit-4.13.2.jar"
    ham = m2 / "org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar"
    plugin = "apply plugin:'java-library'\n"
    consumer = f"dependencies {{testImplementation files('{junit.as_posix()}','{ham.as_posix()}')}}\n"
    if multi:
        write(app / "build.gradle", plugin + consumer + "dependencies {implementation project(':producer')}\n")
    # Both registrations are intentional: Mockito's directory has no builtBy; its
    # producer is instead a dependency of classes. The destination can be nested
    # inside the registered output, rather than exactly equal to it.
    script = """
def selected = sourceSets.getByName('SCOPE')
def bytecode = tasks.named(selected.compileJavaTaskName,JavaCompile)
def generated = layout.buildDirectory.dir('extra-tree')
def assemblePayload = tasks.register('assemblePayload', Sync) {
    dependsOn bytecode
    into(generated.map { it.dir('inspect') })
    inputs.dir('inputs')
    from(bytecode.flatMap { it.destinationDirectory }) {
        include 'fixture/Encoded.class'
        rename 'Encoded.class','proof.raw'
    }
    from('inputs') { include '*.txt' }
    from('optional-inputs') {
        caseSensitive = false
        include 'label-*.TXT'
        rename { 'added.txt' }
    }
}
REGISTRATION
gradle.taskGraph.whenReady { graph ->
    if(graph.hasTask(assemblePayload.get().path)) {
        new File(rootDir, 'producer-graphs.txt').append('requested\\n')
    }
}
""".replace("SCOPE", scope)
    declaration = ("selected.output.dir(generated)\ntasks.named(selected.classesTaskName) {dependsOn assemblePayload}"
                   if layout == "main" else "selected.output.dir([builtBy: assemblePayload], generated)")
    write(owner / "build.gradle", plugin + ("" if multi else consumer) + script.replace("REGISTRATION", declaration))
    if multi:
        write(root / "build.gradle", "")
    code = write(owner / f"src/{scope}/java/fixture/Encoded.java",
                 'package fixture; public class Encoded {public static String marker(){return "wire-version-A";}}')
    side = write(owner / "inputs/revision.txt", "r1")
    expected = write(root / "expected.txt", "wire-version-A:r1")
    write(app / "src/main/java/fixture/ProbeApp.java", """package fixture;
import java.io.*; import java.net.*; import com.sun.net.httpserver.*;
public class ProbeApp {
 public static String read() throws Exception {
   ClassLoader loader=ProbeApp.class.getClassLoader();
   try(InputStream in=loader.getResourceAsStream("inspect/fixture/proof.raw")) {
    if(in==null) return "absent";
    byte[] bytes=in.readAllBytes();
    if(bytes.length<4 || bytes[0]!=(byte)0xca || bytes[1]!=(byte)0xfe ||
        bytes[2]!=(byte)0xba || bytes[3]!=(byte)0xbe) return "not-bytecode";
    String contents=new String(bytes,java.nio.charset.StandardCharsets.ISO_8859_1);
    String value=contents.contains("wire-version-B")?"wire-version-B":
       contents.contains("wire-version-A")?"wire-version-A":"wrong-class";
    try(InputStream rev=loader.getResourceAsStream("inspect/revision.txt");
        InputStream extra=loader.getResourceAsStream("inspect/added.txt")){
      return value+":"+(rev==null?"missing":new String(rev.readAllBytes(),"UTF-8"))
                  +(extra==null?"":":"+new String(extra.readAllBytes(),"UTF-8"));
    }
   }
 }
 public static void main(String[] args) throws Exception {
   HttpServer s=HttpServer.create(new InetSocketAddress("127.0.0.1",Integer.parseInt(args[0])),0);
   s.createContext("/",e->{try {byte[] b=read().getBytes("UTF-8");e.sendResponseHeaders(200,b.length);
       e.getResponseBody().write(b); e.close();}catch(Exception x){throw new RuntimeException(x);}});
   s.start();
 }
}""")
    write(app / "src/test/java/fixture/PayloadTest.java", """
package fixture; public class PayloadTest {
 @org.junit.Test public void verifiesRawClassAndSideInput() throws Exception {
   org.junit.Assert.assertEquals(java.nio.file.Files.readString(java.nio.file.Paths.get(EXPECTED)),ProbeApp.read());
 }
}""".replace("EXPECTED", json.dumps(expected.as_posix())))

    # Do not let the native test pre-populate the MCP project's output.
    oracle = tmp_path / "oracle"
    shutil.copytree(root, oracle)
    native = subprocess.run([str(oracle / wrapper.name), "--offline", "--no-daemon",
                             ":application:test" if multi else ":test", "--tests", "fixture.PayloadTest"],
                            cwd=oracle, env=env, capture_output=True, text=True, timeout=150)
    (tmp_path / "native.log").write_text(native.stdout + native.stderr)
    assert native.returncode == 0, native.stdout + native.stderr
    # Creation (not a case-only rename) lets this test distinguish filter
    # semantics on macOS/Windows too. The native copy really does include it.
    oracle_input = oracle / owner.relative_to(root) / "optional-inputs/label-new.txt"
    write(oracle_input, "fresh")
    proof = subprocess.run([str(oracle / wrapper.name), "--offline", "--no-daemon",
                            ":producer:assemblePayload" if multi else ":assemblePayload"],
                           cwd=oracle, env=env, capture_output=True, text=True, timeout=150)
    assert proof.returncode == 0, proof.stdout + proof.stderr
    assert (oracle / owner.relative_to(root) / "build/extra-tree/inspect/added.txt").read_text() == "fresh"
    assert not (owner / "build").exists()
    marker = root / "producer-graphs.txt"
    port = reserve_local_port()
    common = {"project_path": str(app), "java_home": home, "build_java_home": home, "timeout": 30}
    run = dict(common, tests=["fixture.PayloadTest"])
    launch = dict(common, action="launch", main_class="fixture.ProbeApp", ready_port=port,
                  jdwp_port=reserve_local_port(), app_args=[str(port)])

    def http():
        with urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            assert response.status == 200
            return response.read().decode()

    async def call(session, tool, args):
        return await call_workflow(session, tool, args, tmp_path / "calls.jsonl")

    async def scenario():
        for reopened in (False, True):
            with (tmp_path / f"mcp-{reopened}.log").open("w") as log:
                async with open_mcp_session(log, environment=env) as session:
                    async def check():
                        result = await call(session, "java_fast_test", run)
                        assert result.get("passed") and result.get("tests") == 1, result
                        return result
                    try:
                        before = marker.read_bytes() if marker.exists() else b""
                        if layout == "main":
                            # Cold Fast Test without an application's prior build.
                            await check()
                        started = await call(session, "java_application", launch)
                        assert started.get("startup_state") == "ready", started
                        assert http() == ("absent" if test_only else expected.read_text())
                        if test_only:
                            assert started["fast_update"]["strategy"] == "jdt_incremental"
                        await check()
                        assert marker.is_file()
                        after = marker.read_bytes()
                        if reopened:
                            assert before == after
                        assert (await check())["compiled_source_count"] == 0 and marker.read_bytes() == after
                        if reopened:
                            continue
                        # The declared copy tree was empty during the Probe. Its
                        # filter matters as much as discovery of a newly matching file.
                        write(owner / "optional-inputs/ignored.bin", "not a producer input")
                        assert (await check())["compiled_source_count"] == 0 and marker.read_bytes() == after
                        for value in ("wire-version-B:r1", "wire-version-B:r2", "wire-version-B:r2:fresh",
                                      "wire-version-B:missing:fresh"):
                            previous = expected.read_text()
                            if value.endswith(":r1"):
                                code.write_text(code.read_text().replace("wire-version-A","wire-version-B"))
                            elif value.endswith(":r2"):
                                side.write_text("r2")
                            elif value.endswith(":r2:fresh"):
                                write(owner / "optional-inputs/label-new.txt", "fresh")
                            else:
                                side.unlink()
                            expected.write_text(value)
                            await check()
                            assert http() == ("absent" if test_only else previous), "Fast Test must not update the live JVM"
                            if not test_only:
                                updated = await call(session, "java_application", {"action":"restart","timeout":30})
                                assert updated.get("ok") and updated.get("apply_method")=="restart", updated
                                assert http() == value
                    finally:
                        await session.call_tool("java_application", {"action":"stop"})
    anyio.run(scenario)
