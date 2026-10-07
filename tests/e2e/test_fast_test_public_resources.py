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
    ("JOLINK_TEST_DIFF_UTILS", "java-diff-utils", ["com.github.difflib.text.DiffRowGeneratorTest"], 41, "JOLINK_TEST_JAVA8_HOME"),
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
    env = {**os.environ, "JAVA_HOME": os.environ["JOLINK_TEST_JAVA8_HOME"], "XDG_CACHE_HOME": str(tmp_path / "cache")}

    async def scenario():
        for _ in range(2):
            with temporary_stderr() as log:
                async with open_mcp_session(log, environment=env) as session:
                    for iteration in range(2):
                        result = dict((await session.call_tool("java_fast_test", options)).structuredContent)
                        with anyio.fail_after(300):
                            while result.get("status") in {"starting", "bootstrapping", "compiling", "running"}:
                                await anyio.sleep(.5)
                                result = dict((await session.call_tool("java_fast_test", {"action": "result", "test_run_id": result["test_run_id"]})).structuredContent)
                        details = dict((await session.call_tool("java_fast_test", {"action": "result", "test_run_id": result["test_run_id"]})).structuredContent)
                        assert details.get("passed") is True and details["tests"] == count, details
                        if iteration:
                            assert "bootstrap_ms" not in result and result["compiled_source_count"] == 0, result
        if module == "java-diff-utils":
            assert (project / "target/test-classes/mocks/issue15_1.txt").is_file()
    anyio.run(scenario)
