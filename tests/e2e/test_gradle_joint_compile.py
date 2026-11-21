"""Real Gradle joint compilers, an independent Test Runner, and a live application."""
import json
import os
import subprocess
from pathlib import Path
from urllib.request import urlopen

import anyio
import pytest
from java_support import call_workflow, open_mcp_session, require_real_mcp_java_e2e, reserve_local_port


def fixture(root, language, multi):
    def write(path, text):
        path = root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path
    gradle = os.environ.get("JOLINK_FAST_TEST_GRADLE")
    if not gradle:
        pytest.skip("a local Gradle distribution is required")
    windows = os.name == "nt"
    wrapper = write("gradlew.bat" if windows else "gradlew",
                    f'@call "{gradle}" %*\r\n' if windows else f'#!/bin/sh\nexec "{gradle}" "$@"\n')
    wrapper.chmod(0o755)
    version = os.environ["JOLINK_FAST_TEST_GRADLE_VERSION"]
    write("gradle/wrapper/gradle-wrapper.properties", f"distributionUrl=https://services.gradle.org/distributions/gradle-{version}-bin.zip")
    write("settings.gradle", "rootProject.name='mixed'\n" + ("include 'app','lib'\n" if multi else ""))
    app = Path("app") if multi else Path(".")
    lib = Path("lib") if multi else app
    m2 = Path.home()/".m2/repository"
    junit = m2/"junit/junit/4.13.2/junit-4.13.2.jar"
    ham = m2/"org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar"
    build = "apply plugin:'java-library'\n" + f"dependencies {{ testImplementation files('{junit.as_posix()}','{ham.as_posix()}') }}\n"
    if language == "groovy":
        foreign = "apply plugin:'groovy'\ndependencies {implementation localGroovy()}\n"
        text = "package example; class Foreign {static String value(){return Base.value() + '-one'}; static String back(){return JointJava.value()}}"
        lazy = "package example; class Lazy {static String value(){return 'one'}}"
        ext = "groovy"
    elif language == "scala":
        local = os.environ.get("JOLINK_TEST_SCALA_REPO")
        repositories = ("repositories {" + (f"maven {{url uri('{Path(local).as_posix()}')}};" if local else "")
                        + "mavenCentral()}\n")
        foreign = "apply plugin:'scala'\n"+repositories+"dependencies {implementation 'org.scala-lang:scala-library:2.13.14'}\n"
        build += repositories
        text = 'package example\nobject Foreign {def value(): String = Base.value() + "-one"; def back(): String = JointJava.value()}'
        lazy = 'package example\nobject Lazy {def value(): String = "one"}'
        ext = "scala"
    else:
        # CI may point to private/offline Maven-layout mirrors. No project
        # buildscript repository is added to the user's global configuration.
        mirrors = [p for p in os.environ.get("JOLINK_TEST_KOTLIN_MAVEN_REPOS", "").split(os.pathsep) if p]
        repos = " ".join("maven {url uri("+repr(Path(p).as_posix())+")};" for p in mirrors) + " mavenLocal(); mavenCentral()"
        foreign = ("buildscript {repositories {"+repos+"}; dependencies {classpath 'org.jetbrains.kotlin:kotlin-gradle-plugin:1.9.22'}}\n"
                   +"apply plugin:'org.jetbrains.kotlin.jvm'\nrepositories {"+repos+"}\n")
        text = 'package example\nobject Foreign {@JvmStatic fun value() = Base.value() + "-one"}'
        lazy = 'package example\nobject Lazy {@JvmStatic fun value() = "one"}'
        ext = "kt"
        build += "repositories {"+repos+"}\n"
    if multi:
        write("build.gradle", "")
        write(app/"build.gradle", build+"dependencies {implementation project(':lib')}\n")
    if language == "both":
        foreign += """apply plugin:'groovy'
dependencies {implementation localGroovy()}
tasks.named('compileGroovy') {
 dependsOn(tasks.named('compileKotlin'))
 classpath += files(tasks.named('compileKotlin').flatMap { it.destinationDirectory })
}
"""
    write(lib/"build.gradle", foreign+build)
    write(lib/"src/main/java/example/Base.java", 'package example; public class Base { public static String value(){return "java";} }')
    directory = "kotlin" if language == "both" else language
    foreign_src = write(lib/f"src/main/{directory}/example/Foreign.{ext}", text)
    lazy_src = write(lib/f"src/main/{directory}/example/Lazy.{ext}", lazy)
    if language == "both":
        write(lib/"src/main/groovy/example/Bridge.groovy",
              'package example; class Bridge { static String value(){return Foreign.value()}; static String back(){return JointJava.value()} }')
    joint = "scala" if language == "scala" else "groovy"
    if language != "kotlin":
        write(lib/f"src/main/{joint}/example/JointJava.java",
              'package example; public class JointJava {public static String value(){return OWNER.value();}}'.replace(
                  'OWNER','Bridge' if language == "both" else 'Foreign'))
    value = ('(String)Class.forName("example.JointJava").getMethod("value").invoke(null)'
             if language != "kotlin" else 'Foreign.value()')
    write(app/"src/main/java/example/App.java", '''package example;
import com.sun.net.httpserver.HttpServer; import java.net.InetSocketAddress;
public class App {
 public static String value() throws Exception {return VALUE;}
 public static void main(String[] args) throws Exception {
  HttpServer s=HttpServer.create(new InetSocketAddress("127.0.0.1",Integer.parseInt(args[0])),0);
  s.createContext("/", x->{try{
   String v=x.getRequestURI().getPath().equals("/lazy")?(String)Class.forName("example.Lazy").getMethod("value").invoke(null):value();
   byte[] b=v.getBytes("UTF-8"); x.sendResponseHeaders(200,b.length);x.getResponseBody().write(b);x.close();
  }catch(Exception e){throw new RuntimeException(e);}});s.start();
 }}'''.replace("VALUE", value))
    expected = write("expect.txt", "java-one")
    write(app/"src/test/java/example/MixedTest.java", '''package example; public class MixedTest {
 @org.junit.Test public void verify() throws Exception {org.junit.Assert.assertEquals(
 java.nio.file.Files.readString(java.nio.file.Paths.get(EXPECTED)), App.value());}}'''.replace("EXPECTED", json.dumps(expected.as_posix())))
    return wrapper, app, foreign_src, lazy_src, expected


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("language", ["groovy", "kotlin", "both", "scala"])
@pytest.mark.parametrize("multi", [False, True])
def test_gradle_joint_compilation_keeps_live_and_test_outputs_separate(tmp_path, language, multi):
    require_real_mcp_java_e2e()
    root = tmp_path/"project"
    home = Path(os.environ["JOLINK_TEST_JAVA17_HOME"])
    wrapper, app, source, lazy, expected = fixture(root, language, multi)
    env = {**os.environ,"JAVA_HOME":str(home), "PATH":str(home/"bin")+os.pathsep+os.environ["PATH"],
           "GRADLE_ARGS":"--offline","XDG_CACHE_HOME":str(tmp_path/"cache")}
    if os.name == "nt": env["LOCALAPPDATA"] = env["XDG_CACHE_HOME"]
    # A native oracle using the actual foreign compiler and the same selector.
    native = subprocess.run([str(wrapper), "--offline","--no-daemon",":app:test" if multi else ":test",
                             "--tests","example.MixedTest"],cwd=root,env=env,capture_output=True,text=True,timeout=180)
    assert native.returncode == 0, native.stdout + native.stderr
    subprocess.run([str(wrapper),"--offline","--no-daemon","clean"],cwd=root,env=env,check=True,
                   capture_output=True,timeout=90)
    common = dict(project_path=str(root/app), java_home=str(home), build_java_home=str(home), timeout=30)
    port = reserve_local_port()
    launch = dict(common, action="launch", main_class="example.App", ready_port=port, jdwp_port=reserve_local_port(), app_args=[str(port)])
    def http(path=""):
        with urlopen(f"http://127.0.0.1:{port}/{path}", timeout=5) as r: return r.read().decode()
    async def call(s, tool, args):
        return await call_workflow(s, tool, args, tmp_path / "calls.jsonl")
    async def scenario():
        tests = dict(common, tests=["example.MixedTest"])
        for reopened in (False, True):
            with (tmp_path/f"mcp-{reopened}.log").open("w") as log:
                async with open_mcp_session(log,environment=env) as s:
                    try:
                        first = await call(s,"java_fast_test",tests)
                        assert first.get("passed") and first.get("compile_backend")=="gradle_native", first
                        warm = await call(s,"java_fast_test",tests)
                        assert warm["compiled_source_count"] == 0 and warm["passed"]
                        started = await call(s,"java_application",launch)
                        assert started.get("startup_state")=="ready", started
                        assert http() == expected.read_text()
                        if reopened:
                            assert first["compiled_source_count"] == 0
                            continue
                        source.write_text(source.read_text().replace("one","two"))
                        lazy.write_text(lazy.read_text().replace("one","two"))
                        expected.write_text("java-two")
                        test = await call(s,"java_fast_test",tests)
                        assert test.get("passed") and "compiled_source_count" not in test, test
                        detail = await call(s, "java_fast_test", dict(action="result", test_run_id=test["test_run_id"]))
                        assert "compiled_source_count" not in detail and detail["passed"], detail
                        assert http() == "java-one" and http("lazy") == "one"
                        updated = await call(s,"java_application",dict(action="restart",timeout=30))
                        assert updated.get("apply_method")=="hotswap", updated
                        assert "compiled_source_count" not in updated, updated
                        for details in (False, True):
                            state = dict((await s.call_tool("java_status", dict(action="status", details=details))).structuredContent)
                            assert state["last_reload"]["reload_id"] == updated["reload_id"]
                            assert "compiled_source_count" not in state["last_reload"]
                        assert updated.get("compile_backend")=="gradle_native" and http()=="java-two" and http("lazy")=="two"
                        saved = source.read_text()
                        source.write_text("BROKEN {")
                        for tool,args in (("java_fast_test", tests), ("java_application", dict(action="restart",timeout=30))):
                            failed = await call(s,tool,args)
                            assert failed.get("error_code") == "GRADLE_NATIVE_COMPILE_FAILED", failed
                            assert "compiled_source_count" not in failed, failed
                            assert failed["diagnostics"][0]["message"] and "compile_log" in failed
                            assert http() == "java-two"
                        source.write_text(saved.replace("two","three"))
                        expected.write_text("java-three")
                        assert (await call(s,"java_fast_test",tests))["passed"]
                        assert (await call(s,"java_application",dict(action="restart",timeout=30)))["ok"]
                        assert http() == "java-three"
                        owner = root/("lib" if multi else "")/"src/main"
                        joint = "scala" if language == "scala" else "groovy"
                        java_file = owner/(f"{joint}/example/JointJava.java" if language != "kotlin"
                                           else "java/example/Base.java")
                        original = java_file.read_text()
                        edit = (original.replace(".value();", '.value()+"!";') if language != "kotlin"
                                else original.replace('"java"', '"java!"'))
                        edit = edit.replace("public static String value()", "public static int added; public static String value()")
                        assert edit != original
                        java_file.write_text(edit)
                        next_value = "java-three!" if language != "kotlin" else "java!-three"
                        expected.write_text(next_value)
                        assert (await call(s,"java_fast_test",tests))["passed"]
                        assert http() == "java-three"
                        restarted = await call(s,"java_application",dict(action="restart",timeout=30))
                        assert restarted.get("apply_method")=="restart" and restarted["restart_reason"]=="HOT_SWAP_REJECTED",restarted
                        assert restarted["pid"] != started["pid"] and http() == next_value
                    finally:
                        await s.call_tool("java_application",dict(action="stop"))
    anyio.run(scenario)


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("language", ["groovy","kotlin","scala"])
def test_test_only_language_first_source_switches_only_test_backend(tmp_path, language):
    require_real_mcp_java_e2e()
    root = tmp_path / "project"
    home = Path(os.environ["JOLINK_TEST_JAVA17_HOME"])
    _, _, foreign, lazy, expected, = fixture(root, language, False)
    foreign.unlink()
    lazy.unlink()
    (root/f"src/main/{language}/example/JointJava.java").unlink(missing_ok=True)
    app = root/"src/main/java/example/App.java"
    app.write_text('''package example; public class App {
      public static String value(){return Base.value();}
      public static void main(String[] args) throws Exception {
        try(java.net.ServerSocket s=new java.net.ServerSocket(Integer.parseInt(args[0]))){
          for(;;)try(java.net.Socket c=s.accept()){c.getOutputStream().write(value().getBytes("UTF-8"));}
        }
      }}''')
    expected.write_text("java")
    env = {**os.environ,"JAVA_HOME":str(home),"PATH":str(home/"bin")+os.pathsep+os.environ["PATH"],
           "GRADLE_ARGS":"--offline","XDG_CACHE_HOME":str(tmp_path/"cache")}
    if os.name == "nt": env["LOCALAPPDATA"]=env["XDG_CACHE_HOME"]
    args = dict(project_path=str(root), java_home=str(home), build_java_home=str(home),timeout=30)
    port = reserve_local_port()
    async def scenario():
        with (tmp_path/"mcp.log").open("w") as log:
            async with open_mcp_session(log,environment=env) as s:
                async def call(tool, **kw):
                    return await call_workflow(s, tool, kw)
                try:
                    launch = await call("java_application",**args,action="launch", main_class="example.App",ready_port=port,jdwp_port=reserve_local_port(),app_args=[str(port)])
                    assert launch.get("startup_state")=="ready" and launch["fast_update"]["strategy"]=="jdt_incremental",launch
                    base=await call("java_fast_test",**args, tests=["example.MixedTest"])
                    assert base.get("passed") and not base.get("compile_backend"),base
                    ext = "kt" if language=="kotlin" else language
                    f=root/f"src/test/{language}/example/NewSpec.{ext}"
                    f.parent.mkdir(parents=True,exist_ok=True)
                    script = {
                        "groovy": 'package example; class NewSpec {@org.junit.Test void testValue(){assert App.value()=="java"}}',
                        "kotlin": 'package example\nclass NewSpec {@org.junit.Test fun testValue(){org.junit.Assert.assertEquals("java",App.value())}}',
                        "scala": 'package example\nclass NewSpec {@org.junit.Test def testValue(): Unit = org.junit.Assert.assertEquals("java",App.value())}',
                    }[language]
                    f.write_text(script)
                    # Re-use the old Java selector first: the first new language file
                    # must invalidate even a live JDT session, not only a cold selector miss.
                    after=await call("java_fast_test",**args, tests=["example.MixedTest"])
                    assert after.get("passed") and after.get("compile_backend")=="gradle_native", after
                    for broken in (False, True, False):
                        f.write_text(script.replace('"java"','"wrong"') if broken else script)
                        new=await call("java_fast_test",**args,tests=["example.NewSpec#testValue"])
                        assert new.get("tests")==1 and new["passed"] is (not broken),new
                    # Gradle must remove an obsolete output, including from the
                    # persistent private directory that is actually on the runner classpath.
                    f.unlink()
                    test = root/"src/test/java/example/MixedTest.java"
                    test.write_text(test.read_text().replace(
                        "org.junit.Assert.assertEquals(",
                        'try {Class.forName("example.NewSpec"); org.junit.Assert.fail("stale output");} '
                        'catch(ClassNotFoundException removed) {} org.junit.Assert.assertEquals('))
                    assert (await call("java_fast_test",**args,tests=["example.MixedTest"]))["passed"]
                    status=await call("java_status",action="status",details=True)
                    assert status["pid"]==launch["pid"] and status["fast_update"]["strategy"]=="jdt_incremental"
                finally:
                    await s.call_tool("java_application",dict(action="stop"))
    anyio.run(scenario)
