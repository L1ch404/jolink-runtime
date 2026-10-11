"""Opt-in cold-clone regressions for public Maven projects, including WAR.

Point the environment variables at local checkouts; tests copy them without
build outputs and never modify the originals or invoke mvn test/resources first.
"""

import os
import shutil

import anyio
import pytest
from java_support import open_mcp_session, require_real_mcp_java_e2e, temporary_stderr


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("project_key,module,selectors,count,java_key", [
    ("JOLINK_TEST_JSOUP", ".", ["org.jsoup.nodes.FormElementTest", "org.jsoup.internal.StringUtilTest",
        "org.jsoup.nodes.EntitiesTest", "org.jsoup.nodes.NodeTest"], 73, "JOLINK_TEST_JAVA17_HOME"),
    ("JOLINK_TEST_MYBATIS", ".", [
        "org.apache.ibatis.reflection.MetaObjectTest",
        "org.apache.ibatis.session.SqlSessionTest",
        "org.apache.ibatis.submitted.record_type.RecordTypeTest",
    ], 87, "JOLINK_TEST_JAVA17_HOME"),
    ("JOLINK_TEST_DIFF_UTILS", "java-diff-utils", ["com.github.difflib.text.DiffRowGeneratorTest"], 41, "JOLINK_TEST_JAVA8_HOME"),
    ("JOLINK_TEST_DIFF_UTILS", "java-diff-utils-jgit", [
        "com.github.difflib.algorithm.jgit.HistogramDiffTest",
        "com.github.difflib.algorithm.jgit.LRHistogramDiffTest",
    ], 3, "JOLINK_TEST_JAVA8_HOME"),
    ("JOLINK_TEST_PETCLINIC", ".", ["org.springframework.samples.petclinic.vet.VetControllerTests", "org.springframework.samples.petclinic.service.ClinicServiceTests"], 14, "JOLINK_TEST_JAVA17_HOME"),
    ("JOLINK_TEST_WAR_PETCLINIC", ".", [
        "org.springframework.samples.petclinic.web.PetTypeFormatterTests",
        "org.springframework.samples.petclinic.model.OwnerTests",
        "org.springframework.samples.petclinic.model.PetTests",
        "org.springframework.samples.petclinic.model.VetTests",
        "org.springframework.samples.petclinic.model.ValidatorTests",
        "org.springframework.samples.petclinic.service.ClinicServiceJdbcTests",
        "org.springframework.samples.petclinic.service.ClinicServiceJpaTests",
        "org.springframework.samples.petclinic.service.ClinicServiceSpringDataJpaTests",
        "org.springframework.samples.petclinic.web.CrashControllerTests",
        "org.springframework.samples.petclinic.web.OwnerControllerTests",
        "org.springframework.samples.petclinic.web.PetControllerTests",
        "org.springframework.samples.petclinic.web.VetControllerTests",
        "org.springframework.samples.petclinic.web.VisitControllerTests",
    ], 75, "JOLINK_TEST_JAVA17_HOME"),
])
def test_cold_public_project_explicit_environment(tmp_path, project_key, module, selectors, count, java_key):
    require_real_mcp_java_e2e()
    if not all(os.environ.get(k) for k in (project_key, java_key, "JOLINK_TEST_JAVA8_HOME")):
        pytest.skip("set public-project checkout and JDK paths")
    checkout = tmp_path / "checkout"
    shutil.copytree(os.environ[project_key], checkout,
                    ignore=shutil.ignore_patterns(".git", "target", "build", ".gradle", ".idea"))
    project = (checkout / module).resolve()
    assert not (project / "target").exists()
    options = {"project_path": str(project), "tests": selectors, "build_system": "maven", "java_home": os.environ[java_key]}
    for key in ("maven_home", "maven_settings"):
        if value := os.environ.get("JOLINK_TEST_" + key.upper()):
            options[key] = value
    env = {**os.environ, "JAVA_HOME": os.environ["JOLINK_TEST_JAVA8_HOME"], "XDG_CACHE_HOME": str(tmp_path / "cache"), "JOLINK_LOG_LEVEL": "INFO"}

    async def scenario():
        for cycle in range(2):
            with temporary_stderr() as log:
                async with open_mcp_session(log, environment=env) as session:
                    async def run_selected():
                        result = dict((await session.call_tool("java_fast_test", options)).structuredContent)
                        with anyio.fail_after(300):
                            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                                await anyio.sleep(.5)
                                result = dict((await session.call_tool("java_fast_test", {"action": "result", "test_run_id": result["test_run_id"]})).structuredContent)
                        details = dict((await session.call_tool("java_fast_test", {"action": "result", "test_run_id": result["test_run_id"]})).structuredContent)
                        return result, details

                    for iteration in range(2):
                        result, details = await run_selected()
                        assert details.get("passed") is True and details["tests"] == count, details
                        if project_key == "JOLINK_TEST_MYBATIS":
                            assert any(":rewrite-maven-plugin:run@" in step for step in details.get("skipped_build_steps", ())), details
                        if project_key == "JOLINK_TEST_JSOUP":
                            assert details["test_execution_ids"] == ["maven-surefire-plugin:default-test"]
                            assert "not_applied_test_settings" not in details
                        if iteration:
                            assert "bootstrap_ms" not in result and result["compiled_source_count"] == 0, result
                    if project_key == "JOLINK_TEST_MYBATIS" and not cycle:
                        test = project / "src/test/java/org/apache/ibatis/submitted/record_type/RecordTypeTest.java"
                        original = test.read_text()

                        try:
                            test.write_text(original.replace('assertEquals("Val1!",', 'assertEquals("wrong-value",', 1))
                            _, failed = await run_selected()
                            assert failed.get("failed_test_count") == 1
                            test.write_text(original + "\ninvalid java source\n")
                            _, failed = await run_selected()
                            assert failed.get("error_count", 0) > 0 and not failed.get("passed"), failed
                        finally:
                            test.write_text(original)
                        _, recovered = await run_selected()
                        assert recovered.get("passed") is True and recovered["tests"] == count, recovered
                        build_lines = [line for path in (tmp_path / "cache").rglob("mcp.log")
                                       for line in path.read_text().splitlines() if "jdt.build.finished" in line]
                        assert "actual=FULL" in build_lines[0]
                        assert len(build_lines[1:]) == 3
                        assert all("actual=INCREMENTAL" in line and "compiled_sources=1 " in line
                                   for line in build_lines[1:]), build_lines
        if module == "java-diff-utils":
            assert (project / "target/test-classes/mocks/issue15_1.txt").is_file()
    anyio.run(scenario)
