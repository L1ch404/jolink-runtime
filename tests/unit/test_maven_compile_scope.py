import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from jolink_runtime.launch.jdt_compile_session import (
    JdtCompileError,
    select_target_system_home,
)
from jolink_runtime.launch.maven import MavenBuildSystemAdapter
from jolink_runtime.launch.maven_compile_scope import (
    compiler_parameters,
    compiler_scope,
)


def test_scope_preserves_main_and_applies_test_execution_and_properties():
    project = ET.fromstring("""<project><properties>
<maven.compiler.release>8</maven.compiler.release>
<maven.compiler.testRelease>11</maven.compiler.testRelease>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
</properties><build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId>
<configuration><parameters>false</parameters><encoding>ISO-8859-1</encoding></configuration>
<executions><execution><id>default-testCompile</id><configuration>
<encoding>UTF-8</encoding><parameters>true</parameters>
</configuration></execution></executions>
</plugin></plugins></build></project>""")
    original = ET.tostring(project)
    maven = MavenBuildSystemAdapter()
    jdk = SimpleNamespace(compiler_major_version=11, major_version=11)
    main, test = compiler_scope(project), compiler_scope(project, test=True)
    assert (
        maven._compiler_model(main, build_jdk=jdk, runtime_jdk=jdk)["target_level"] == 8
    )
    assert (
        maven._compiler_model(test, build_jdk=jdk, runtime_jdk=jdk)["target_level"]
        == 11
    )
    assert maven._source_encoding(main) == "ISO-8859-1"
    assert maven._source_encoding(test) == "UTF-8"
    assert compiler_parameters(main) is False
    assert compiler_parameters(test) is True
    assert ET.tostring(project) == original


def test_test_release_configuration_precedes_property():
    project = ET.fromstring("""<project><properties><maven.compiler.testRelease>8</maven.compiler.testRelease></properties>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><configuration>
<release>8</release><testRelease>11</testRelease></configuration></plugin></plugins></build></project>""")
    scoped = compiler_scope(project, test=True)
    assert (
        scoped.findtext("./{*}build/{*}plugins/{*}plugin/{*}configuration/{*}release")
        == "11"
    )


def test_java17_is_compiler_limit_not_missing_jdk(tmp_path):
    with pytest.raises(JdtCompileError) as error:
        select_target_system_home((tmp_path,), 17)
    assert error.value.error_code == "JDT_TARGET_PLATFORM_UNSUPPORTED"
