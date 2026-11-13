"""Real MCP updates loaded classes and leaves new bytecode for first loading."""

import json
import os
from pathlib import Path
from urllib.request import urlopen

import anyio
import pytest

from java_support import open_mcp_session, require_real_mcp_java_e2e, reserve_local_port


pytestmark = pytest.mark.mcp_java_e2e


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fixture(tmp_path, system, reactor):
    require_real_mcp_java_e2e()
    home = os.environ.get("JOLINK_TEST_JAVA8_HOME")
    if not home:
        pytest.skip("JOLINK_TEST_JAVA8_HOME is required")
    java = Path(home)
    root = tmp_path / "project"
    app = root / "app" if reactor else root
    library = root / "library" if reactor else root
    if system == "maven":
        plugins = """<build><plugins>
<plugin><artifactId>maven-resources-plugin</artifactId><version>3.3.0</version></plugin>
<plugin><artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version></plugin>
<plugin><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version></plugin>
</plugins></build>"""
        properties = "<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>"
        junit = "<dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency>"
        if reactor:
            _write(root / "pom.xml", "<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>deferred-parent</artifactId><version>1</version><packaging>pom</packaging><modules><module>library</module><module>app</module></modules>" + properties + plugins + "</project>")
            parent = "<project><modelVersion>4.0.0</modelVersion><parent><groupId>example</groupId><artifactId>deferred-parent</artifactId><version>1</version></parent>"
            _write(library / "pom.xml", parent + "<artifactId>library</artifactId></project>")
            _write(app / "pom.xml", parent + "<artifactId>app</artifactId><dependencies><dependency><groupId>example</groupId><artifactId>library</artifactId><version>1</version></dependency>" + junit + "</dependencies></project>")
        else:
            _write(root / "pom.xml", "<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>deferred</artifactId><version>1</version>" + properties + plugins + "<dependencies>" + junit + "</dependencies></project>")
    else:
        gradle = os.environ.get("JOLINK_FAST_TEST_GRADLE")
        if not gradle:
            pytest.skip("JOLINK_FAST_TEST_GRADLE is required")
        wrapper = _write(root / ("gradlew.bat" if os.name == "nt" else "gradlew"),
                         f'@call "{gradle}" %*\r\n' if os.name == "nt" else f'#!/bin/sh\nexec "{gradle}" "$@"\n')
        wrapper.chmod(0o755)
        _write(root / "gradle/wrapper/gradle-wrapper.properties", "distributionUrl=https\\://services.gradle.org/distributions/gradle-" + os.environ.get("JOLINK_FAST_TEST_GRADLE_VERSION", "8.14") + "-bin.zip\n")
        _write(root / "settings.gradle", "rootProject.name='deferred'\n" + ("include 'library','app'\n" if reactor else ""))
        junit = Path.home() / ".m2/repository/junit/junit/4.13.2/junit-4.13.2.jar"
        hamcrest = Path.home() / ".m2/repository/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar"
        assert junit.is_file() and hamcrest.is_file()
        common = "apply plugin:'java-library'\njava {sourceCompatibility='8';targetCompatibility='8'}\n"
        dependencies = f"dependencies {{testImplementation files('{junit.as_posix()}','{hamcrest.as_posix()}')" + ("; implementation project(':library')" if reactor else "") + "}\n"
        if reactor:
            _write(root / "build.gradle", "subprojects {\n" + common + "}\n")
            _write(library / "build.gradle", "")
            _write(app / "build.gradle", dependencies)
        else:
            _write(root / "build.gradle", common + dependencies)
    main = library / "src/main/java/example"
    _write(main / "Tracker.java", "package example; public class Tracker { public static int lazy,nested,added; public static String counts(){return lazy+\":\"+nested+\":\"+added;} }")
    live = _write(main / "Live.java", """package example; public class Live {
 public static String value(){return "live1";}
 public static class Nested {
  static {Tracker.nested++;}
  public static String value(){return "nested1";}
 }
} """)
    lazy = _write(main / "Lazy.java", """package example; public class Lazy {
 static {Tracker.lazy++;}
 public static String value(){return "lazy1";}
} """)
    _write(app / "src/main/java/example/App.java", """package example;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
public class App {
 public static void main(String[] args) throws Exception {
  Live.value(); Tracker.counts();
  HttpServer server=HttpServer.create(new InetSocketAddress("127.0.0.1",Integer.parseInt(args[0])),0);
  server.createContext("/", e -> {
   String path=e.getRequestURI().getPath(), value;
   int status=200;
   try {
    if(path.equals("/live")) value=Live.value();
    else if(path.equals("/counts")) value=Tracker.counts();
    else if(path.equals("/loaded")) {
     java.lang.reflect.Method find=ClassLoader.class.getDeclaredMethod("findLoadedClass",String.class);
     find.setAccessible(true);
     ClassLoader loader=App.class.getClassLoader();
     value=(find.invoke(loader,"example.Lazy")!=null?"1":"0")+":"
      +(find.invoke(loader,"example.Live$Nested")!=null?"1":"0")+":"
      +(find.invoke(loader,"example.Added")!=null?"1":"0");
    }
    else {
     String name=path.equals("/lazy")?"example.Lazy":path.equals("/nested")?"example.Live$Nested":"example.Added";
     value=(String)Class.forName(name).getMethod("value").invoke(null);
    }
   } catch(Throwable error) {status=500;value=error.toString();}
   byte[] body=value.getBytes("UTF-8"); e.sendResponseHeaders(status,body.length);
   e.getResponseBody().write(body);e.close();
  }); server.start();
 }
}
""")
    expected = _write(root / "expected.txt", "live1|lazy1|nested1|none")
    test = _write(app / "src/test/java/example/DeferredTest.java", """package example; public class DeferredTest {
 @org.junit.Test public void currentSources() throws Exception {
  String added="none";
  try {added=(String)Class.forName("example.Added").getMethod("value").invoke(null);}
  catch(ClassNotFoundException notYetAdded) {}
  String expected=new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(EXPECTED)),"UTF-8").trim();
  org.junit.Assert.assertEquals(expected,Live.value()+"|"+Lazy.value()+"|"+Live.Nested.value()+"|"+added);
 }
}
""".replace("EXPECTED", json.dumps(expected.as_posix())))
    env = {**os.environ, "JAVA_HOME": str(java), "PATH": str(java / "bin") + os.pathsep + os.environ["PATH"],
           "MAVEN_ARGS": "--offline", "GRADLE_ARGS": "--offline", "XDG_CACHE_HOME": str(tmp_path / "cache")}
    if system == "gradle":
        # Gradle 8 runs on a modern JDK; application/Java source target stays 8.
        build = Path(os.environ["JOLINK_TEST_JAVA17_HOME"])
        env.update(JAVA_HOME=str(build), PATH=str(build / "bin") + os.pathsep + os.environ["PATH"])
    if os.name == "nt":
        env["LOCALAPPDATA"] = env["XDG_CACHE_HOME"]
    return root, app, main, live, lazy, expected, test, java, env


@pytest.mark.parametrize("system,reactor", [("maven", False), ("maven", True), ("gradle", False), ("gradle", True)])
def test_unloaded_outputs_are_deferred_without_restarting_or_initializing(tmp_path, system, reactor):
    root, app, main, live, lazy, expected, _, java, env = _fixture(tmp_path, system, reactor)
    port = reserve_local_port()
    launch = dict(action="launch", project_path=str(app), main_class="example.App", java_home=str(java),
                  build_java_home=env["JAVA_HOME"], ready_port=port, jdwp_port=reserve_local_port(),
                  app_args=[str(port)], timeout=30)
    test_args = dict(project_path=str(app), tests=["example.DeferredTest"], java_home=str(java),
                     build_java_home=env["JAVA_HOME"], timeout=30)
    if system == "maven" and os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
        launch["maven_settings"] = test_args["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]

    def http(path):
        with urlopen(f"http://127.0.0.1:{port}/{path}", timeout=5) as response:
            assert response.status == 200
            return response.read().decode("utf-8")

    async def call(session, tool, args):
        result = dict((await session.call_tool(tool, args)).structuredContent)
        with (tmp_path / "calls.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(dict(tool=tool, arguments=args, result=result), ensure_ascii=False) + "\n")
        return result

    async def tests(session, succeeds=True):
        result = await call(session, "java_fast_test", test_args)
        with anyio.fail_after(180):
            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                await anyio.sleep(.2)
                result = await call(session, "java_fast_test", dict(action="result", test_run_id=result["test_run_id"]))
        assert result.get("passed") is succeeds, result
        if succeeds:
            assert result.get("tests") == result.get("passed_count") == 1, result
        return result

    async def application(session, args, succeeds=True):
        result = await call(session, "java_application", args)
        with anyio.fail_after(180):
            while result.get("ok") and result.get("status") in {
                "project_launch_started", "project_launch_restarted", "restart_started", "reload_started"
            } and result.get("launch_phase") != "runtime_active" and result.get("stage") != "succeeded":
                await anyio.sleep(.2)
                status = await call(session, "java_status", dict(action="status", details=True))
                result = (status.get("last_reload") or result) if result.get("reload_id") else status
        assert result.get("ok") is succeeds, result
        return result

    async def same_pid(session, pid):
        state = await call(session, "java_status", dict(action="status"))
        assert state["pid"] == pid, state

    async def scenario():
        with (tmp_path / "mcp.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                try:
                    assert (await tests(session))["compiled_source_count"] > 0
                    assert (await tests(session))["compiled_source_count"] == 0
                    started = await application(session, launch)
                    assert started.get("startup_state") == "ready", started
                    pid = started["pid"]
                    assert http("live") == "live1" and http("counts") == "0:0:0"
                    assert http("loaded") == "0:0:0"

                    # All outputs unloaded: no RedefineClasses and no eager initialization.
                    lazy.write_text(lazy.read_text().replace('"lazy1"', '"lazy2"').replace('public class Lazy {', 'public class Lazy { public static int extra;'))
                    expected.write_text("live1|lazy2|nested1|none")
                    assert (await tests(session))["passed"]
                    assert http("counts") == "0:0:0"
                    deferred = await application(session, dict(action="restart", timeout=30))
                    assert deferred["apply_method"] == "class_output", deferred
                    assert deferred["deferred_classes"] == ["example.Lazy"], deferred
                    assert deferred["redefined_classes"] == [], deferred
                    await same_pid(session, pid)
                    assert http("counts") == "0:0:0"
                    assert http("loaded") == "0:0:0", "Class output publication must not force class loading"
                    assert http("lazy") == "lazy2" and http("counts") == "1:0:0"
                    assert http("loaded") == "1:0:0"

                    # Same source generates one loaded class and one unloaded nested class.
                    live.write_text(live.read_text().replace('"live1"', '"live2"').replace('"nested1"', '"nested2"'))
                    expected.write_text("live2|lazy2|nested2|none")
                    await tests(session)
                    assert http("live") == "live1" and http("counts") == "1:0:0"
                    mixed = await application(session, dict(action="restart", timeout=30))
                    assert mixed["apply_method"] == "hotswap", mixed
                    assert "example.Live" in mixed["redefined_classes"], mixed
                    assert "example.Live$Nested" in mixed["deferred_classes"], mixed
                    await same_pid(session, pid)
                    assert http("live") == "live2" and http("counts") == "1:0:0"
                    assert http("loaded") == "1:0:0"
                    assert http("nested") == "nested2" and http("counts") == "1:1:0"

                    # A new top-level class plus an already loaded caller in one build.
                    added = _write(main / "Added.java", 'package example; public class Added {static {Tracker.added++;} public static String value(){return "added1";}}')
                    live.write_text(live.read_text().replace('return "live2";', 'return "live3";'))
                    expected.write_text("live3|lazy2|nested2|added1")
                    await tests(session)
                    assert http("live") == "live2" and http("counts") == "1:1:0"
                    result = await application(session, dict(action="restart", timeout=30))
                    assert result["apply_method"] == "hotswap", result
                    assert "example.Added" in result["deferred_classes"], result
                    await same_pid(session, pid)
                    assert http("live") == "live3" and http("counts") == "1:1:0"
                    assert http("loaded") == "1:1:0"
                    assert http("new") == "added1" and http("counts") == "1:1:1"

                    valid = added.read_text()
                    added.write_text("package example; public class Added { broken }")
                    await tests(session, succeeds=False)
                    failed = await application(session, dict(action="restart", timeout=30), succeeds=False)
                    assert failed["error_code"] == "JDT_COMPILE_FAILED", failed
                    await same_pid(session, pid)
                    assert http("new") == "added1"
                    added.write_text(valid.replace('"added1"', '"added2"'))
                    expected.write_text("live3|lazy2|nested2|added2")
                    await tests(session)
                    recovered = await application(session, dict(action="restart", timeout=30))
                    assert recovered["apply_method"] == "hotswap", recovered
                    await same_pid(session, pid)
                    assert http("new") == "added2"

                    # Once loaded, structural changes still use the normal restart path.
                    added.write_text(added.read_text().replace('public class Added {', 'public class Added { public static int shape;'))
                    await tests(session)
                    structural = await application(session, dict(action="restart", timeout=30))
                    assert structural["apply_method"] == "restart", structural
                    assert structural["restart_reason"] == "HOT_SWAP_REJECTED", structural
                    state = await call(session, "java_status", dict(action="status"))
                    assert state["pid"] != pid
                    assert http("counts") == "0:0:0" and http("new") == "added2"
                finally:
                    await application(session, dict(action="stop"))
        with (tmp_path / "reopen.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                try:
                    assert (await tests(session))["compiled_source_count"] == 0
                    started = await application(session, launch)
                    assert started["jdt_bootstrap_reused"] and started["jdt_bootstrap_build_kind"] is None, started
                    assert http("live") == "live3" and http("lazy") == "lazy2"
                    assert http("nested") == "nested2" and http("new") == "added2"
                finally:
                    await application(session, dict(action="stop"))
    anyio.run(scenario)
