"""Actual MCP/JVM redefine latency, not a mocked successful HotSwap."""

import json
import subprocess
import urllib.request
import zipfile
from pathlib import Path

import anyio
import pytest
from java_support import open_mcp_session, reserve_local_port, temporary_stderr
from test_fast_test_scopes import environment


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("delay_ms", [0, 6000, 31000])
def test_reload_confirmation_timeout_through_mcp(tmp_path, delay_ms):
    env = environment(tmp_path)
    jdk = Path(env["JAVA_HOME"])
    agent_source = tmp_path / "SlowAgent.java"
    agent_source.write_text("""import java.lang.instrument.*; import java.security.ProtectionDomain;
public class SlowAgent {
 public static void premain(String value, Instrumentation instrumentation) {
  final long delay=Long.parseLong(value);
  instrumentation.addTransformer(new ClassFileTransformer() {
   public byte[] transform(ClassLoader loader,String name,Class<?> redefining,
                           ProtectionDomain domain,byte[] bytes) {
    if(redefining!=null && name.equals("example/Value")) {
     try {Thread.sleep(delay);} catch(InterruptedException e) {Thread.currentThread().interrupt();}
    }
    return null;
   }
  },true);
 }
}""")
    classes = tmp_path / "agent-classes"
    classes.mkdir()
    javac = jdk / "bin" / ("javac.exe" if (jdk / "bin/javac.exe").exists() else "javac")
    subprocess.run(
        [str(javac), "-d", str(classes), str(agent_source)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    jar = tmp_path / "slow-agent.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr(
            "META-INF/MANIFEST.MF",
            "Manifest-Version: 1.0\nPremain-Class: SlowAgent\nCan-Retransform-Classes: true\n\n",
        )
        for path in classes.glob("*.class"):
            archive.write(path, path.name)
    project = tmp_path / "project"
    sources = project / "src/main/java/example"
    sources.mkdir(parents=True)
    source = sources / "Value.java"
    original = (
        "package example; public class Value { public static int get(){return 1;} }"
    )
    source.write_text(original)
    (sources / "App.java").write_text("""package example; public class App {
 public static void main(String[] args) throws Exception {
  com.sun.net.httpserver.HttpServer server=com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1",Integer.parseInt(args[0])),0);
  server.createContext("/", exchange->{byte[] bytes=String.valueOf(Value.get()).getBytes("UTF-8");exchange.sendResponseHeaders(200,bytes.length);try(java.io.OutputStream out=exchange.getResponseBody()){out.write(bytes);}});
  server.start();
 }
}""")
    (project / "pom.xml").write_text(
        """<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>reload-outcome</artifactId><version>1</version><properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties></project>"""
    )
    port, debug = reserve_local_port(), reserve_local_port()
    run_dir = project / ".run"
    run_dir.mkdir()
    (run_dir / "App.xml").write_text(
        f'''<component name="ProjectRunConfigurationManager"><configuration name="App" type="Application"><module name="reload-outcome"/><option name="MAIN_CLASS_NAME" value="example.App"/><option name="WORKING_DIRECTORY" value="$PROJECT_DIR$"/><option name="PROGRAM_PARAMETERS" value="{port}"/><option name="VM_PARAMETERS" value="&quot;-javaagent:{jar.as_posix()}={delay_ms}&quot;"/><method v="2"><option name="Make" enabled="true"/></method></configuration></component>'''
    )

    def response():
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=15) as reply:
            return reply.read()

    async def scenario():
        with temporary_stderr() as stderr:
            async with open_mcp_session(stderr, environment=env) as session:

                async def call(tool, arguments):
                    return dict(
                        (await session.call_tool(tool, arguments)).structuredContent
                        or {}
                    )

                async def poll(predicate):
                    with anyio.fail_after(100):
                        while True:
                            state = await call("java_status", {"action": "status"})
                            assert state.get("launch_phase") != "failed", json.dumps(
                                state
                            )
                            if predicate(state):
                                return state
                            await anyio.sleep(0.1)

                try:
                    accepted = await call(
                        "java_application",
                        {
                            "action": "launch",
                            "project_path": str(project),
                            "launch_name": "App",
                            "jdwp_port": debug,
                            "ready_port": port,
                            "timeout": 10,
                        },
                    )
                    assert accepted["ok"], accepted
                    await poll(
                        lambda state: state.get("launch_phase") == "runtime_active"
                    )
                    assert await anyio.to_thread.run_sync(response) == b"1"
                    source.write_text(original.replace("return 1", "return 2"))
                    accepted = await call(
                        "java_application",
                        {"action": "restart", "timeout": 0},
                    )
                    assert accepted["status"] == "restart_started", accepted
                    state = await poll(
                        lambda state: (
                            (state.get("last_reload") or {}).get("reload_id")
                            == accepted["reload_id"]
                        )
                    )
                    result = state["last_reload"]
                    if delay_ms > 30000:
                        assert result["error_code"] == "HOT_SWAP_OUTCOME_UNKNOWN", (
                            result
                        )
                        assert result["applied"] is None
                        assert result["total_ms"] - result["compile_total_ms"] >= 29000
                    else:
                        assert result["applied"] is True, result
                        assert result["apply_method"] == "hotswap"
                    # Timing out does not cancel the real JVM command. Verify
                    # success/unknown against actual fresh requests in both cases.
                    with anyio.fail_after(15):
                        while await anyio.to_thread.run_sync(response) != b"2":
                            await anyio.sleep(0.1)
                    if delay_ms > 30000:
                        state = await call("java_status", {"action": "status"})
                        assert state["last_reload"]["applied"] is None
                finally:
                    assert (await call("java_application", {"action": "stop"}))["ok"]
                    source.write_text(original)

    anyio.run(scenario)
