"""A package-time UI resource must be built, not substituted with test resources."""
import os
from pathlib import Path
import shutil
import subprocess
from urllib.request import urlopen

import anyio
import pytest

from java_support import call_workflow, open_mcp_session, require_real_mcp_java_e2e, reserve_local_port


@pytest.mark.mcp_java_e2e
def test_npm_build_and_late_resources_feed_test_and_application(tmp_path):
    require_real_mcp_java_e2e()
    home = os.getenv("JOLINK_TEST_JAVA17_HOME") or os.getenv("JAVA_HOME")
    if not home or not shutil.which("mvn"):
        pytest.skip("requires Maven, JDK17 and the cached frontend plugin/Node distribution")
    project = tmp_path / "project"
    def write(name, contents):
        f = project / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(contents, encoding="utf-8")
        return f
    start = "<project><modelVersion>4.0.0</modelVersion>"
    coordinates = "<groupId>example</groupId><artifactId>root</artifactId><version>1</version>"
    parent = "<parent>"+coordinates+"</parent>"
    write("pom.xml", start+coordinates+"""<packaging>pom</packaging><modules><module>ui</module><module>app</module></modules>
<properties><maven.compiler.source>11</maven.compiler.source><maven.compiler.target>11</maven.compiler.target>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<build><pluginManagement><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version></plugin>
<plugin><artifactId>maven-resources-plugin</artifactId><version>3.3.1</version></plugin>
<plugin><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version></plugin></plugins></pluginManagement></build></project>""")
    write("ui/pom.xml", start+parent+"""<artifactId>ui</artifactId><build><plugins>
<plugin><groupId>com.github.eirslett</groupId><artifactId>frontend-maven-plugin</artifactId><version>1.15.0</version>
<configuration><installDirectory>target</installDirectory><nodeVersion>v20.18.0</nodeVersion></configuration>
<executions><execution><id>node</id><goals><goal>install-node-and-npm</goal></goals></execution>
<execution><id>build</id><phase>compile</phase><goals><goal>npm</goal></goals><configuration><arguments>run build</arguments></configuration>
</execution></executions></plugin>
<plugin><artifactId>maven-resources-plugin</artifactId><executions><execution><id>assets</id>
<phase>prepare-package</phase><goals><goal>copy-resources</goal></goals>
<configuration><outputDirectory>${project.build.outputDirectory}/assets</outputDirectory><resources>
<resource><directory>dist</directory></resource></resources></configuration></execution></executions>
</plugin></plugins></build></project>""")
    write("ui/package.json",'{"name":"fixture","version":"1.0.0","scripts":{"build":"node bundle.js"}}')
    write("ui/bundle.js", """const fs=require('fs'); fs.mkdirSync('dist',{recursive:true});
fs.copyFileSync('input.html','dist/index.html'); fs.writeFileSync('side-effect.txt','ran');""")
    original = write("ui/input.html","A")
    write("app/pom.xml",start+parent+"""<artifactId>app</artifactId><dependencies>
<dependency><groupId>example</groupId><artifactId>ui</artifactId><version>1</version></dependency>
<dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency>
</dependencies></project>""")
    write("app/src/main/java/example/App.java","""package example; import com.sun.net.httpserver.*; import java.net.*; import java.io.*;
public class App {public static String value() throws IOException {try(InputStream in=App.class.getResourceAsStream("/assets/index.html")) {
return new String(in.readAllBytes(),"UTF-8");}}
public static void main(String[] args) throws Exception {HttpServer s=HttpServer.create(new InetSocketAddress("127.0.0.1",Integer.parseInt(args[0])),0);
s.createContext("/",e->{byte[] b=value().getBytes("UTF-8");e.sendResponseHeaders(200,b.length);e.getResponseBody().write(b);e.close();});s.start();}}""")
    source = write("app/src/test/java/example/UiTest.java", """package example; public class UiTest {
@org.junit.Test public void asset() throws Exception {org.junit.Assert.assertEquals("A",App.value());}}""")
    oracle = tmp_path / "native"
    shutil.copytree(project,oracle)
    env = {**os.environ, "JAVA_HOME":home,"PATH":home+"/bin"+os.pathsep+os.environ["PATH"],
           "XDG_CACHE_HOME":str(tmp_path/"cache"),"MAVEN_ARGS":"--offline","npm_config_offline":"true"}
    if os.name == "nt": env["LOCALAPPDATA"] = env["XDG_CACHE_HOME"]
    settings = os.getenv("JOLINK_TEST_MAVEN_SETTINGS")
    native = subprocess.run(["mvn","--offline","-B",*(["-s",settings] if settings else []),
                             "-pl","app","-am","-Dtest=UiTest","-Dsurefire.failIfNoSpecifiedTests=false","test","prepare-package"],
                            cwd=oracle,env=env,capture_output=True,text=True,errors="replace",timeout=180)
    (tmp_path/"native.log").write_text(native.stdout+native.stderr)
    assert native.returncode == 0, native.stdout+native.stderr
    assert (oracle/"ui/target/classes/assets/index.html").read_text()=="A"
    assert (oracle/"ui/side-effect.txt").exists()
    port = reserve_local_port()
    def http():
        with urlopen(f"http://127.0.0.1:{port}/",timeout=5) as r: return r.read().decode()
    async def scenario():
        for cycle in range(2):
            with (tmp_path/f"mcp-{cycle}.log").open("w") as log:
                async with open_mcp_session(log,environment=env) as s:
                    common=dict(project_path=str(project/"app"),java_home=home,build_java_home=home,timeout=30)
                    if settings: common["maven_settings"]=settings
                    async def test():
                        return await call_workflow(s,"java_fast_test",dict(common,tests=["example.UiTest"]))
                    result=await test()
                    assert result.get("passed") and result["tests"]==1,result
                    if cycle:
                        assert result["compiled_source_count"]==0
                        break
                    try:
                        assert (await test())["compiled_source_count"]==0
                        launch=await call_workflow(s,"java_application",dict(common,action="launch",main_class="example.App",
                                app_args=[str(port)],jdwp_port=reserve_local_port(),ready_port=port))
                        assert launch.get("startup_state")=="ready" and http()=="A",launch
                        original.write_text("B")
                        red=await test()
                        assert red.get("tests")==red.get("failed_count")==1,red
                        source.write_text(source.read_text().replace('"A"','"B"'))
                        assert (await test()).get("passed") and http()=="A"
                        restart=await call_workflow(s,"java_application",{"action":"restart","timeout":30})
                        assert restart.get("ok") and http()=="B",restart
                    finally: await s.call_tool("java_application",{"action":"stop"})
            assert not (project/"ui/side-effect.txt").exists()
            assert not (project/"ui/dist").exists()
    anyio.run(scenario)
