"""Actual package-info output semantics, including removal and MCP reopen."""

import shutil
import subprocess

import anyio
import pytest
from java_support import open_mcp_session, temporary_stderr
from test_fast_test_scopes import environment, run


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("javac_flag", [False, True])
def test_jdt_package_info_matches_javac_always(tmp_path, javac_flag):
    env = environment(tmp_path)
    project = tmp_path / "package-info"
    main = project / "src/main/java"
    test = project / "src/test/java/example/PackageTest.java"
    test.parent.mkdir(parents=True)
    for package, annotation in (
        ("empty", ""),
        ("marked", "@Deprecated"),
        ("sourceonly", "@example.SourceOnly"),
    ):
        source = main / package / "package-info.java"
        source.parent.mkdir(parents=True)
        source.write_text(
            f"/** package documentation */\n{annotation}\npackage {package};"
        )
    annotation = main / "example/SourceOnly.java"
    annotation.parent.mkdir()
    annotation.write_text(
        "package example; @java.lang.annotation.Retention(java.lang.annotation.RetentionPolicy.SOURCE) public @interface SourceOnly {}"
    )
    test.write_text("""package example; public class PackageTest {
@org.junit.Test public void generated() throws Exception {
 for(String pkg:new String[]{"empty", "marked", "sourceonly"}) {
  Class<?> info=Class.forName(pkg+".package-info");
  org.junit.Assert.assertEquals(pkg, info.getPackage().getName());
  org.junit.Assert.assertEquals(pkg.equals("marked"), info.getPackage().isAnnotationPresent(Deprecated.class));
 }
}
}""")
    flag = (
        "<compilerArgs><arg>-Xpkginfo:always</arg></compilerArgs>" if javac_flag else ""
    )
    pom = f"""<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>package-info</artifactId><version>1</version>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version>
<configuration><source>8</source><target>8</target><encoding>UTF-8</encoding>{flag}</configuration>
</plugin></plugins></build></project>"""
    (project / "pom.xml").write_text(pom)
    if javac_flag:
        native = tmp_path / "native"
        shutil.copytree(project, native)
        result = subprocess.run(
            [shutil.which("mvn"), "-o", "-B", "test"],
            cwd=native,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    async def scenario():
        for cycle in range(2):
            with temporary_stderr() as stderr:
                async with open_mcp_session(stderr, environment=env) as session:

                    async def check():
                        return await run(session, project, ["example.PackageTest"])

                    first = await check()
                    assert first.get("passed"), first
                    if cycle:
                        assert first["compiled_source_count"] == 0, first
                        continue
                    source = main / "marked/package-info.java"
                    original = source.read_text()
                    source.write_text(original.replace("@Deprecated", ""))
                    assert (await check())["failed_count"] == 1
                    source.write_text(original)
                    assert (await check())["passed"]
                    source = main / "empty/package-info.java"
                    original = source.read_text()
                    source.unlink()
                    assert (await check())["failed_count"] == 1
                    source.write_text(original)
                    assert (await check())["passed"]
        assert not (project / "target").exists()

    anyio.run(scenario)
