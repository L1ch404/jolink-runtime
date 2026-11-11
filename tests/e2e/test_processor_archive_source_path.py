"""Archive SOURCE_PATH resources retain lookup order and incremental input tracking."""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path

import anyio
import pytest

import test_processor_auxiliary_inputs as auxiliary
import test_processor_source_path as source_path
from java_support import open_mcp_session, reserve_local_port
from test_generated_lombok_configuration import _command, _maven_arguments
from test_lombok_configuration import _write


pytestmark = pytest.mark.mcp_java_e2e


def _archive(path, value=None, *, entry="example/template.txt", encoding="utf-8"):
    """Replace the archive at its stable path without leaving a reader handle open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("must-not-extract/archive-marker.bin", b"not a source file")
        if value is not None:
            archive.writestr(entry, (value + "\n").encode(encoding))
    temporary.replace(path)


def _fixture(tmp_path, monkeypatch, *, system="gradle", entry="template.txt", encoding="UTF-8"):
    processor = source_path.PROCESSOR.replace('"template.txt"', json.dumps(entry))
    processor = processor.replace('String value = input.getCharContent', '''java.net.URLConnection connection = input.toUri().toURL().openConnection();
    connection.setUseCaches(false);
    try (InputStream uriStream = connection.getInputStream()) {
     if (uriStream.read() < 0) throw new IOException("Expected readable resource URI");
    }
    String value = input.getCharContent''')
    monkeypatch.setattr(auxiliary, "PROCESSOR", processor)
    project, env, java, old_main, old_test, expected = auxiliary._fixture(tmp_path, system)
    archives = project / "source archives 中文"
    missing = archives / "created later.jar"
    override = project / "first directory"
    override.mkdir()
    primary = archives / "主 primary.jar"
    fallback = archives / "后备 #百分% archive.zip"
    test_archive = archives / "测试 #百分% archive.zip"
    relative = "example/" + entry
    _archive(primary)
    _archive(fallback, "alpha", entry=relative, encoding=encoding)
    _archive(test_archive, "test-alpha", entry=relative, encoding=encoding)
    main_paths = [p.relative_to(project).as_posix() for p in (missing, override, primary, fallback)]
    test_path = test_archive.relative_to(project).as_posix()
    if system == "maven":
        build = project / "pom.xml"
        extra_sources = "".join("<source>${project.basedir}/" + path + "</source>" for path in main_paths)
        plugin = f'''<plugin><groupId>org.codehaus.mojo</groupId><artifactId>build-helper-maven-plugin</artifactId><version>3.5.0</version>
<executions><execution><id>archive-source-path</id><phase>generate-sources</phase><goals><goal>add-source</goal></goals>
<configuration><sources>{extra_sources}</sources></configuration></execution>
<execution><id>archive-test-source-path</id><phase>generate-test-sources</phase><goals><goal>add-test-source</goal></goals>
<configuration><sources><source>${{project.basedir}}/{test_path}</source></sources></configuration></execution>
</executions></plugin>'''
        build.write_text(build.read_text().replace(f"<arg>-Ainput={old_main.as_posix()}</arg>", "").replace(
            f"<arg>-Ainput={old_test.as_posix()}</arg>", "").replace("</plugins>", plugin + "</plugins>").replace(
            "<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>",
            f"<project.build.sourceEncoding>{encoding}</project.build.sourceEncoding>"))
    else:
        # Incorrect resources in ordinary Java roots must not override explicit sourcepath.
        _write(project / "src/main/java/example" / entry, "wrong-main\n")
        _write(project / "src/test/java/example" / entry, "wrong-test\n")
        build = project / "build.gradle"
        build.write_text(build.read_text().replace(f"['-Ainput={old_main.as_posix()}']", "[]").replace(
            f"['-Ainput={old_test.as_posix()}']", "[]") + f'''
tasks.named('compileJava') {{ options.sourcepath = files({', '.join(repr(p) for p in main_paths)}) }}
tasks.named('compileTestJava') {{ options.sourcepath = files({test_path!r}) }}
tasks.withType(JavaCompile).configureEach {{ options.encoding = {encoding!r} }}
''')
    return project, env, java, expected, missing, override, primary, fallback, test_archive


def _native_copy(tmp_path, project, system="gradle"):
    native = tmp_path / "native"
    shutil.copytree(project, native)
    args = ([*_maven_arguments(), "-Dtest=AuxTest", "clean", "test"] if system == "maven" else
            [str(native / ("gradlew.bat" if os.name == "nt" else "gradlew")),
             "--offline", "--no-daemon", "clean", "test", "--tests", "example.AuxTest"])
    return native, args


@pytest.mark.parametrize("system", ["maven", "gradle"])
def test_archive_inputs_cross_test_application_and_reopen(tmp_path, monkeypatch, system):
    project, env, java, expected, missing, override, primary, fallback, test_archive = _fixture(tmp_path, monkeypatch, system=system)
    native, native_args = _native_copy(tmp_path, project, system)
    _command(native_args, native, env, tmp_path / "native-cold.log")
    immutable = {p: p.read_bytes() for p in project.rglob("*")
                 if p.is_file() and (p.suffix == ".java" or p.name in {"build.gradle", "pom.xml"})}
    port = reserve_local_port()

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                client = auxiliary.Calls(session, project, java, tmp_path / "calls.jsonl", system)

                async def changed(value, old, *, test_value="test-beta", recovery=False):
                    expected.write_text(value + "\n" + test_value, encoding="utf-8")
                    result = await client.tests()
                    assert result["compiled_source_count"] > 0, result
                    assert "bootstrap_ms" not in result, result
                    assert auxiliary._http(port) == old, "Fast Test must not publish to the running application"
                    result = await client.application(dict(action="restart", timeout=30))
                    assert result["compiled_source_count"] > 0, result
                    # Failure recovery can recreate output resources, requiring
                    # a real JVM restart instead of a class-only HotSwap.
                    assert result["apply_method"] in ({"hotswap", "restart"} if recovery else {"hotswap"}), result
                    assert auxiliary._http(port) == value
                    assert (await client.tests())["compiled_source_count"] == 0

                try:
                    assert (await client.tests())["compiled_source_count"] > 0
                    assert (await client.tests())["compiled_source_count"] == 0
                    await client.application(client.launch_args(port))
                    assert auxiliary._http(port) == "alpha"

                    _archive(fallback, "bravo")
                    _archive(native / fallback.relative_to(project), "bravo")
                    expected.write_text("bravo\ntest-alpha", encoding="utf-8")
                    _command(native_args, native, env, tmp_path / "native-replaced.log")
                    await changed("bravo", "alpha", test_value="test-alpha")
                    # Test-scope archive changes must not dirty main outputs.
                    main_outputs = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                                    for p in (tmp_path / "cache").rglob("Aux.class")}
                    assert main_outputs
                    _archive(test_archive, "test-beta")
                    expected.write_text("bravo\ntest-beta", encoding="utf-8")
                    assert (await client.tests())["compiled_source_count"] > 0
                    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in main_outputs} == main_outputs
                    assert auxiliary._http(port) == "bravo"

                    # Existing higher-priority archive had no entry; adding it wins.
                    _archive(primary, "preferred")
                    await changed("preferred", "bravo")
                    earlier = _write(override / "example/template.txt", "directory\n")
                    await changed("directory", "preferred")
                    earlier.unlink()
                    await changed("preferred", "directory")

                    # A sourcepath root that was entirely absent can become an archive.
                    _archive(missing, "first")
                    await changed("first", "preferred")
                    missing.unlink()
                    await changed("preferred", "first")
                    _archive(primary)  # Removing its entry restores the later ZIP hit.
                    await changed("bravo", "preferred")

                    # Stream/archive lifetimes must allow replacing/removing the files.
                    renamed = fallback.with_name("renamed temporarily.zip")
                    fallback.rename(renamed)
                    renamed.unlink()
                    await client.tests(passed=False)
                    failed = await client.application(dict(action="restart", timeout=30), ok=False)
                    assert failed.get("error_code") == "JDT_COMPILE_FAILED" and failed.get("applied") is False, failed
                    assert auxiliary._http(port) == "bravo"
                    _archive(fallback, "charlie")
                    await changed("charlie", "bravo", recovery=True)
                finally:
                    await client.application(dict(action="stop"))

        for phase in ("unchanged", "changed-offline"):
            if phase == "changed-offline":
                _archive(fallback, "delta")
                expected.write_text("delta\ntest-beta", encoding="utf-8")
            with (tmp_path / f"{phase}-stderr.log").open("w", encoding="utf-8") as stderr:
                async with open_mcp_session(stderr, environment=env) as session:
                    client = auxiliary.Calls(session, project, java, tmp_path / "calls.jsonl", system)
                    try:
                        result = await client.tests()
                        assert (result["compiled_source_count"] == 0) is (phase == "unchanged"), result
                        started = await client.application(client.launch_args(port))
                        assert started["jdt_bootstrap_reused"] is True, started
                        assert started["jdt_bootstrap_build_kind"] == (None if phase == "unchanged" else "INCREMENTAL"), started
                        assert auxiliary._http(port) == ("charlie" if phase == "unchanged" else "delta")
                    finally:
                        await client.application(dict(action="stop"))
        assert {p: p.read_bytes() for p in immutable} == immutable
        assert not list(project.rglob("archive-marker.bin")), "Source archives must not be extracted into the project"
        assert not list((tmp_path / "cache").rglob("archive-marker.bin")), "Read entries directly, without extraction"

    anyio.run(scenario)


@pytest.mark.parametrize("encoding,value,next_value", [("UTF-8", "中文甲", "中文乙"), ("GBK", "中文丙", "中文丁")])
def test_archive_entry_names_and_character_encoding(tmp_path, monkeypatch, encoding, value, next_value):
    entry = "模板.txt"
    project, env, java, expected, _, _, _, fallback, test_archive = _fixture(
        tmp_path, monkeypatch, entry=entry, encoding=encoding)
    _archive(fallback, value, entry="example/" + entry, encoding=encoding)
    _archive(test_archive, "中文测试", entry="example/" + entry, encoding=encoding)
    expected.write_text(value + "\n中文测试", encoding="utf-8")
    native, args = _native_copy(tmp_path, project)
    _command(args, native, env, tmp_path / "native-encoding.log")
    port = reserve_local_port()

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=env) as session:
                client = auxiliary.Calls(session, project, java, tmp_path / "calls.jsonl", "gradle")
                try:
                    assert (await client.tests())["compiled_source_count"] > 0
                    assert (await client.tests())["compiled_source_count"] == 0
                    await client.application(client.launch_args(port))
                    assert auxiliary._http(port) == value
                    _archive(fallback, next_value, entry="example/" + entry, encoding=encoding)
                    expected.write_text(next_value + "\n中文测试", encoding="utf-8")
                    assert (await client.tests())["compiled_source_count"] > 0
                    await client.application(dict(action="restart", timeout=30))
                    assert auxiliary._http(port) == next_value
                    assert (await client.tests())["compiled_source_count"] == 0
                finally:
                    await client.application(dict(action="stop"))

    anyio.run(scenario)
