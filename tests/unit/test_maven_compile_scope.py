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
    main = compiler_scope(project)
    test = compiler_scope(project, test=True, configuration="""<configuration>
<encoding>UTF-8</encoding><parameters>true</parameters></configuration>""")
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


@pytest.mark.parametrize("namespaced", [False, True])
def test_selected_execution_does_not_merge_disabled_or_other_execution_fields(namespaced):
    namespace = ' xmlns="http://maven.apache.org/POM/4.0.0"' if namespaced else ""
    project = ET.fromstring(f'''<project{namespace}><properties>
<maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId>
<configuration><parameters>false</parameters></configuration>
<executions><execution><id>default-compile</id><phase>none</phase>
<configuration><release>17</release><encoding>ISO-8859-1</encoding><parameters>true</parameters>
<compilerArgs><arg>-Xplugin:Disabled</arg></compilerArgs></configuration></execution>
<execution><id>chosen</id><goals><goal>compile</goal></goals><phase>compile</phase>
<configuration><source>8</source><target>8</target></configuration></execution>
<execution><id>later</id><goals><goal>compile</goal></goals><phase>package</phase>
<configuration><release>21</release></configuration></execution></executions>
</plugin></plugins></build></project>''')
    original = ET.tostring(project)
    maven = MavenBuildSystemAdapter()
    jdk = SimpleNamespace(compiler_major_version=17, major_version=17)
    # Explicit input is the complete selected config exported by Maven, not a
    # patch to merge with every declaration bearing the same goal.
    selected = '<configuration><source>8</source><target>8</target><parameters>false</parameters></configuration>'
    for configuration in (selected, None):
        scoped = compiler_scope(project, configuration=configuration)
        model = maven._compiler_model(scoped, build_jdk=jdk, runtime_jdk=jdk)
        assert model["source_level"] == model["target_level"] == 8
        assert compiler_parameters(scoped) is False
        assert maven._source_encoding(scoped) == "UTF-8"
        plugin = maven._find_build_plugin(scoped, "maven-compiler-plugin")
        assert plugin.find("{*}executions") is None
        assert not maven._compiler_argument_profile(maven._compiler_configurations(plugin)).decisions
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


def test_java_level_beyond_compiler_range_is_not_a_missing_jdk(tmp_path):
    with pytest.raises(JdtCompileError) as error:
        select_target_system_home((tmp_path,), 27)
    assert error.value.error_code == "JDT_TARGET_PLATFORM_UNSUPPORTED"
