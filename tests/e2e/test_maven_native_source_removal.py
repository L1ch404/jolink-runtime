"""A native Maven no-op must not let old test classes hide a removed Java type."""
import os
from pathlib import Path
import shutil
import socket
import subprocess

import anyio
import pytest
from java_support import call_workflow, open_mcp_session, reserve_local_port
from test_fast_test_scopes import environment


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("layout", ["last-main", "upstream-rename", "untracked-outputs"])
def test_maven_compiler_rechecks_dependents_of_removed_java(tmp_path, layout):
    env = environment(tmp_path)
    multi, untracked = layout == "upstream-rename", layout == "untracked-outputs"
    root = tmp_path / "project"
    lib, app = (root / "lib", root / "app") if multi else (root, root)

    def write(path, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return path

    plugin = "3.13.0" if multi else "3.11.0"
    parent = """<groupId>example</groupId><artifactId>removal</artifactId><version>1</version>"""
    properties = """<properties><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
<maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target></properties>"""
    compiler = f"""<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>{plugin}</version>
<configuration><compilerArgs><arg>-XDignore.symbol.file</arg></compilerArgs>
<useIncrementalCompilation>{str(not untracked).lower()}</useIncrementalCompilation></configuration></plugin></plugins></build>"""
    pom = "<project><modelVersion>4.0.0</modelVersion>"
    junit = "<dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency>"
    if multi:
        write(root / "pom.xml", pom + parent + "<packaging>pom</packaging><modules><module>lib</module><module>app</module></modules>" + properties + compiler + "</project>")
        child = pom + "<parent>" + parent + "</parent>"
        write(lib / "pom.xml", child + "<artifactId>lib</artifactId></project>")
        write(app / "pom.xml", child + "<artifactId>app</artifactId><dependencies><dependency><groupId>example</groupId><artifactId>lib</artifactId><version>1</version></dependency>" + junit + "</dependencies></project>")
    else:
        write(root / "pom.xml", pom + parent + properties + compiler + "<dependencies>" + junit + "</dependencies></project>")

    old_type = 'package example; public class Gone {public static String value(){return "A";}}'
    gone = write(lib / "src/main/java/example/Gone.java", old_type)
    logic = 'static String value(boolean extended){return extended ? Gone.value() : "default";}'
    main = None
    if multi:
        main = write(app / "src/main/java/example/App.java", 'package example; public class App {'
                     + logic + """ public static void main(String[] a) throws Exception {
          try(java.net.ServerSocket s=new java.net.ServerSocket(Integer.parseInt(a[0]))) {
            for(;;) try(java.net.Socket c=s.accept()){c.getOutputStream().write(value(true).getBytes("UTF-8"));}
          }
        }}""")
    write(app / "src/test/java/example/UsesTest.java", "package example; public class UsesTest {"
          + ("" if multi else logic)
          + """ @org.junit.Test public void defaults() throws Exception {
          boolean b=java.nio.file.Files.exists(java.nio.file.Paths.get("check-restored"));
          org.junit.Assert.assertEquals(b ? "B" : "default", """ + ("App." if multi else "")
          + "value(b));}}")
    settings = os.environ.get("JOLINK_TEST_MAVEN_SETTINGS")
    native = [shutil.which("mvn"), "-B", "-o", *(["-s", settings] if settings else [])]
    args = {"project_path": str(app), "java_home": env["JAVA_HOME"], "build_java_home": env["JAVA_HOME"],
            **({"maven_settings":settings} if settings else {})}
    port = reserve_local_port()
    def live():
        with socket.create_connection(("127.0.0.1", port), timeout=6) as c:
            return c.makefile("rb").read().decode()

    async def scenario():
        with (tmp_path / "mcp.log").open("w") as stderr:
            async with open_mcp_session(stderr, environment=env) as s:
                async def test():
                    r = await call_workflow(s, "java_fast_test", dict(args, tests=["example.UsesTest"]), tmp_path / "calls.jsonl")
                    if not r.get("passed") and "test_run_id" in r:
                        return await call_workflow(s, "java_fast_test", {"action":"result","test_run_id":r["test_run_id"]}, tmp_path / "calls.jsonl")
                    return r
                try:
                    first = await test()
                    assert first.get("passed") and first.get("compile_backend") == "maven_native", first
                    assert (await test())["compiled_source_count"] == 0
                    if multi:
                        start = await call_workflow(s, "java_application", dict(args, action="launch", main_class="example.App",
                            ready_port=port, jdwp_port=reserve_local_port(), app_args=[str(port)], timeout=0))
                        assert start.get("startup_state") == "ready" and live() == "A", start
                    gone.unlink()
                    if multi:  # Rename a type while the unedited consumer still refers to Gone.
                        write(gone.with_name("Replacement.java"), old_type.replace("Gone", "Replacement").replace('"A"', '"B"'))
                    oracle = tmp_path / "oracle"
                    shutil.copytree(root, oracle, ignore=shutil.ignore_patterns("target"))
                    baseline = subprocess.run([*native, "test-compile"], cwd=oracle, env=env, capture_output=True)
                    assert baseline.returncode != 0 and b"Gone" in baseline.stdout + baseline.stderr
                    rejected = await test()
                    assert not rejected.get("passed"), rejected
                    if untracked:
                        assert rejected.get("error_code") == "MAVEN_NATIVE_SOURCE_REMOVAL_REQUIRES_CLEAN", rejected
                        assert "Run Maven clean" in rejected.get("error", ""), rejected
                        subprocess.run([*native, "clean"], cwd=root, env=env, check=True, capture_output=True)
                        rejected = await test()
                        assert "MAVEN_NATIVE_SOURCE_REMOVAL_REQUIRES_CLEAN" not in str(rejected), rejected
                    assert rejected.get("error_code") == "MAVEN_NATIVE_COMPILE_FAILED", rejected
                    assert "Gone" in str(rejected.get("diagnostics")), rejected
                    if multi:
                        failed = await call_workflow(s, "java_application", {"action":"restart","timeout":0})
                        assert failed.get("error_code") == "MAVEN_NATIVE_COMPILE_FAILED" and live() == "A", failed
                        main.write_text(main.read_text().replace("Gone.value()", "Replacement.value()"))
                    else:
                        write(gone, old_type.replace('"A"', '"B"'))
                    write(app / "check-restored", "yes")
                    assert (await test()).get("passed")
                    if multi:
                        assert live() == "A"
                        update = await call_workflow(s, "java_application", {"action":"restart","timeout":0})
                        assert update.get("ok") and live() == "B", update
                    assert (await test())["compiled_source_count"] == 0
                finally:
                    if multi:
                        await s.call_tool("java_application", {"action":"stop"})
    anyio.run(scenario)
