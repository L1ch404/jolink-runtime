"""Another consumer may rewrite a preparation goal's output without changing any bytes."""
import os
from urllib.request import urlopen

import anyio
import pytest
from java_support import call_workflow, open_mcp_session, reserve_local_port, temporary_stderr
from test_build_configuration_mcp import project_fixture
from test_fast_test_scopes import environment


@pytest.mark.mcp_java_e2e
def test_rewrite_and_real_change_of_named_output_take_different_restart_paths(tmp_path):
    env = environment(tmp_path, java11=True)
    root, _, pom, java, port = project_fixture(tmp_path, "maven", False)
    pom.write_text(pom.read_text().replace("</plugins>", """<plugin><artifactId>maven-antrun-plugin</artifactId>
<version>3.2.0</version><executions><execution><id>metadata</id><phase>generate-resources</phase>
<goals><goal>run</goal></goals><configuration><target><echo file="${project.build.directory}/named-data">stable</echo></target>
</configuration></execution></executions></plugin></plugins>"""))
    product = root / "target/named-data"
    options = {"project_path":str(root), **({"maven_settings":env["JOLINK_TEST_MAVEN_SETTINGS"]}
                                         if env.get("JOLINK_TEST_MAVEN_SETTINGS") else {})}
    def http():
        with urlopen(f"http://127.0.0.1:{port}", timeout=5) as r:
            return r.read().decode()
    async def run():
        with temporary_stderr() as log:
            async with open_mcp_session(log, environment=env) as session:
                async def call(tool, **args):
                    return await call_workflow(session, tool, args, tmp_path/"calls.jsonl")
                try:
                    launch = await call("java_application", **options, action="launch", main_class="example.App",
                                        ready_port=port, jdwp_port=reserve_local_port(), timeout=0)
                    assert launch.get("startup_state") == "ready" and http() == "A", launch
                    initial = product.stat()
                    assert (await call("java_fast_test", **options, tests=["example.ReadyTest"])).get("passed")
                    assert product.read_text() == "stable" and product.stat().st_mtime_ns != initial.st_mtime_ns
                    old = java.read_text()
                    java.write_text(old.replace("return Character.toString", 'return "B"+Character.toString'))
                    hot = await call("java_application", action="restart", timeout=0)
                    assert hot.get("apply_method") == "hotswap" and http() == "BA", hot
                    assert (await call("java_status", action="status"))["pid"] == launch["pid"]
                    replacement = product.with_suffix(".new")
                    replacement.write_bytes(b"broken")  # same size as the saved baseline
                    replacement.replace(product)
                    os.utime(product, ns=(initial.st_atime_ns, initial.st_mtime_ns))
                    java.write_text(old)
                    refreshed = await call("java_application", action="restart", timeout=0)
                    assert refreshed.get("startup_state") == "ready" and refreshed["pid"] != launch["pid"], refreshed
                    assert product.read_text() == "stable" and http() == "A"
                finally:
                    await session.call_tool("java_application", {"action":"stop"})
    anyio.run(run)
