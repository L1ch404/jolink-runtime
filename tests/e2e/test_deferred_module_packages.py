"""Named modules can load new classes only in packages assigned at JVM startup."""

import json
import os
from urllib.request import urlopen

import anyio
import pytest

from java_support import open_mcp_session, reserve_local_port
from test_jpms_runtime import _fixture
from test_lombok_configuration import _write


pytestmark = pytest.mark.mcp_java_e2e


@pytest.mark.parametrize("upstream", [False, True])
def test_new_named_module_package_restarts_then_becomes_a_deferred_target(tmp_path, upstream):
    project, app, service, test, expected, java, env = _fixture(tmp_path, "maven", upstream, True)
    module = "example.lib" if upstream else "example.app"
    package = "library" if upstream else "example"
    producer = project / "lib" if upstream else app
    host_name = "library.Library" if upstream else "example.Service"
    host = producer / ("src/main/java/" + host_name.replace(".", "/") + ".java")
    text = host.read_text()
    end = text.rfind("}")
    host.write_text(text[:end] + """
 public static String inspect(String name) throws Exception {
  Class<?> type=Class.forName(name);
  return type.getModule().getName()+"|"+type.getMethod("value").invoke(null);
 }
""" + text[end:])
    _write(app / "src/main/java/example/App.java", """package example;
public class App {
 public static void main(String[] args) throws Exception {
  com.sun.net.httpserver.HttpServer server=com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1",Integer.parseInt(args[0])),0);
  server.createContext("/",e->{
   String text;
   try {String query=e.getRequestURI().getRawQuery();
    text=query==null?Service.describe():HOST.inspect(query);
   } catch(Throwable failure) {text="ERR:"+failure;}
   byte[] bytes=text.getBytes("UTF-8");e.sendResponseHeaders(200,bytes.length);
   e.getResponseBody().write(bytes);e.close();
  });server.start();
 }
}
""".replace("HOST", host_name))
    selected_class = _write(project / "selected-class.txt", "")
    selected_value = _write(project / "selected-value.txt", "")
    test.write_text(test.read_text().replace("// test edit anchor", """// test edit anchor
  String selected=java.nio.file.Files.readString(java.nio.file.Paths.get(CLASS_FILE)).trim();
  if(!selected.isEmpty()) org.junit.Assert.assertEquals(
   java.nio.file.Files.readString(java.nio.file.Paths.get(VALUE_FILE)).trim(), HOST.inspect(selected));
""".replace("CLASS_FILE", json.dumps(selected_class.as_posix()))
       .replace("VALUE_FILE", json.dumps(selected_value.as_posix())).replace("HOST", host_name)))
    descriptors = {p: p.read_bytes() for p in project.rglob("module-info.java")}
    poms = {p: p.read_bytes() for p in project.rglob("pom.xml")}
    port = reserve_local_port()
    launch = dict(action="launch", project_path=str(app), main_class="example.App", java_home=str(java),
                  build_java_home=str(java), ready_port=port, jdwp_port=reserve_local_port(),
                  app_args=[str(port)], timeout=30)
    test_args = dict(project_path=str(app), tests=["example.ModuleTest"], java_home=str(java),
                     build_java_home=str(java), timeout=30)
    if os.environ.get("JOLINK_TEST_MAVEN_SETTINGS"):
        launch["maven_settings"] = test_args["maven_settings"] = os.environ["JOLINK_TEST_MAVEN_SETTINGS"]

    def http(name=None):
        with urlopen(f"http://127.0.0.1:{port}/" + ("?" + name if name else ""), timeout=5) as response:
            assert response.status == 200
            return response.read().decode("utf-8")

    def new_class(name, value):
        package_name, _, class_name = name.rpartition(".")
        _write(producer / ("src/main/java/" + name.replace(".", "/") + ".java"),
               "package " + package_name + "; public class " + class_name +
               ' {public static String value(){return "' + value + '";}}')
        selected_class.write_text(name)
        selected_value.write_text(module + "|" + value)

    async def call(session, tool, args):
        result = dict((await session.call_tool(tool, args)).structuredContent)
        with (tmp_path / "calls.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(dict(tool=tool, arguments=args, result=result)) + "\n")
        return result

    async def tests(session):
        result = await call(session, "java_fast_test", test_args)
        with anyio.fail_after(180):
            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                await anyio.sleep(.2)
                result = await call(session, "java_fast_test", dict(action="result", test_run_id=result["test_run_id"]))
        assert result.get("passed") and result.get("tests") == result.get("passed_count") == 1, result
        return result

    async def application(session, args):
        result = await call(session, "java_application", args)
        with anyio.fail_after(180):
            while result.get("ok") and result.get("status") in {
                "project_launch_started", "project_launch_restarted", "restart_started", "reload_started"
            } and result.get("launch_phase") != "runtime_active" and result.get("stage") != "succeeded":
                await anyio.sleep(.2)
                status = await call(session, "java_status", dict(action="status", details=True))
                result = (status.get("last_reload") or result) if result.get("reload_id") else status
        assert result.get("ok"), result
        return result

    async def scenario():
        with (tmp_path / "mcp.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                try:
                    await tests(session)
                    started = await application(session, launch)
                    assert started["startup_state"] == "ready", started
                    pid = started["pid"]
                    assert "|v1|" in http()

                    # Existing package: a new class plus a loaded-method edit can HotSwap.
                    first = package + ".First"
                    new_class(first, "first")
                    service.write_text(service.read_text().replace('"v1"', '"v2"'))
                    expected.write_text("v2")
                    await tests(session)
                    assert "|v1|" in http()
                    assert http(first).startswith("ERR:java.lang.ClassNotFoundException:"), "Fast Test must not publish to the application"
                    updated = await application(session, dict(action="restart", timeout=30))
                    assert updated["apply_method"] == "hotswap", updated
                    assert first in updated["deferred_classes"], updated
                    assert (await call(session, "java_status", dict(action="status")))["pid"] == pid
                    assert "|v2|" in http() and http(first) == module + "|first"

                    # A new package changes the running JVM's package-to-module mapping.
                    fresh = "internalfresh.First"
                    new_class(fresh, "fresh")
                    await tests(session)
                    assert http(fresh).startswith("ERR:java.lang.ClassNotFoundException:")
                    restarted = await application(session, dict(action="restart", timeout=30))
                    assert restarted["apply_method"] == "restart", restarted
                    assert restarted["restart_reason"] == "MODULE_PACKAGES_CHANGED", restarted
                    state = await call(session, "java_status", dict(action="status"))
                    assert state["pid"] != pid
                    pid = state["pid"]
                    assert http(fresh) == module + "|fresh"

                    # The new JVM's baseline includes that package; another class can defer.
                    second = "internalfresh.Second"
                    new_class(second, "second")
                    await tests(session)
                    assert http(second).startswith("ERR:java.lang.ClassNotFoundException:")
                    deferred = await application(session, dict(action="restart", timeout=30))
                    assert deferred["apply_method"] == "class_output", deferred
                    assert second in deferred["deferred_classes"] and deferred["redefined_classes"] == [], deferred
                    assert (await call(session, "java_status", dict(action="status")))["pid"] == pid
                    assert http(second) == module + "|second"
                    assert {p: p.read_bytes() for p in descriptors} == descriptors
                    assert {p: p.read_bytes() for p in poms} == poms
                finally:
                    await application(session, dict(action="stop"))
    anyio.run(scenario)
