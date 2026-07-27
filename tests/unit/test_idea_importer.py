from __future__ import annotations

import os
from pathlib import Path

import pytest

from jolink_runtime.launch import (
    IdeaLaunchImportError,
    IdeaLaunchImporter,
    LaunchErrorCode,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_imports_application_with_module_macro_arguments_and_redacted_env(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(
        project / ".idea" / "modules.xml",
        """
        <project>
          <component name="ProjectModuleManager">
            <modules>
              <module filepath="$PROJECT_DIR$/service/service.iml" />
            </modules>
          </component>
        </project>
        """,
    )
    _write(
        project / ".run" / "Application.run.xml",
        """
        <component name="ProjectRunConfigurationManager">
          <configuration name="Application" type="Application"
                         factoryName="Application">
            <option name="MAIN_CLASS_NAME"
                    value="com.example.Application" />
            <module name="service" />
            <option name="WORKING_DIRECTORY"
                    value="file://$MODULE_WORKING_DIR$" />
            <option name="VM_PARAMETERS"
                    value="-Dspring.profiles.active=local -Xmx512m" />
            <option name="PROGRAM_PARAMETERS" value="--port 9090" />
            <option name="ALTERNATIVE_JRE_PATH" value="$USER_HOME$/jdk8" />
            <option name="ALTERNATIVE_JRE_PATH_ENABLED" value="true" />
            <envs>
              <env name="ACCESS_TOKEN" value="company-secret" />
            </envs>
            <method v="2">
              <option name="Make" enabled="true" />
            </method>
          </configuration>
        </component>
        """,
    )

    imported = IdeaLaunchImporter().select(project, "Application")

    assert imported.intent.source == "idea"
    assert imported.intent.launch_type == "java_application"
    assert imported.intent.ide_module_name == "service"
    assert imported.intent.main_class == "com.example.Application"
    assert imported.intent.working_directory == (project / "service").resolve()
    assert imported.intent.jvm_args == (
        "-Dspring.profiles.active=local",
        "-Xmx512m",
    )
    assert imported.intent.program_args == ("--port", "9090")
    assert imported.intent.environment["ACCESS_TOKEN"] == "company-secret"
    assert imported.intent.build_before_run is True
    assert imported.intent.runtime_jdk_reference == str(Path.home() / "jdk8")

    summary = str(imported.redacted_summary())
    assert "ACCESS_TOKEN" in summary
    assert "company-secret" not in summary


def test_imports_spring_boot_from_workspace_and_ignores_default_config(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(
        project / ".idea" / "workspace.xml",
        """
        <project>
          <component name="RunManager">
            <configuration default="true" name="Template"
                 type="Application">
              <option name="MAIN_CLASS_NAME" value="ignored.Template" />
            </configuration>
            <configuration name="WebApplication"
                 type="SpringBootApplicationConfigurationType"
                 factoryName="Spring Boot">
              <option name="SPRING_BOOT_MAIN_CLASS"
                      value="com.example.WebApplication" />
              <option name="WORKING_DIRECTORY" value="$PROJECT_DIR$" />
            </configuration>
          </component>
        </project>
        """,
    )

    discovered = IdeaLaunchImporter().discover(project)

    assert len(discovered) == 1
    assert discovered[0].intent.launch_name == "WebApplication"
    assert discovered[0].intent.launch_type == "spring_boot"
    assert discovered[0].intent.build_before_run is False


def test_select_requires_exact_candidate_when_multiple_exist(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for name in ("First", "Second"):
        _write(
            project / ".run" / f"{name}.xml",
            f"""
            <component>
              <configuration name="{name}" type="Application">
                <option name="MAIN_CLASS_NAME"
                        value="com.example.{name}" />
              </configuration>
            </component>
            """,
        )

    importer = IdeaLaunchImporter()
    with pytest.raises(IdeaLaunchImportError) as captured:
        importer.select(project)

    payload = captured.value.to_payload()
    assert payload["error_code"] == "AMBIGUOUS_LAUNCH_CONFIGURATION"
    assert [item["launch_name"] for item in payload["candidates"]] == [
        "First",
        "Second",
    ]
    assert importer.select(project, "Second").intent.main_class.endswith(
        ".Second"
    )
    with pytest.raises(IdeaLaunchImportError) as wrong_case:
        importer.select(project, "second")
    assert (
        wrong_case.value.error_code
        is LaunchErrorCode.LAUNCH_CONFIGURATION_NOT_FOUND
    )


def test_explicit_unsupported_configuration_returns_safe_error(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(
        project / ".run" / "Docker.xml",
        """
        <component>
          <configuration name="Docker App" type="docker-deploy" />
        </component>
        """,
    )

    with pytest.raises(IdeaLaunchImportError) as captured:
        IdeaLaunchImporter().select(project, "Docker App")

    payload = captured.value.to_payload()
    assert payload["error_code"] == "UNSUPPORTED_LAUNCH_CONFIGURATION"
    assert payload["configuration_type"] == "docker-deploy"
    assert payload["retryable"] is False
    assert "code" not in payload


def test_unresolved_macro_is_not_guessed_and_secret_env_is_not_reported(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(
        project / ".run" / "Application.xml",
        """
        <component>
          <configuration name="Application" type="Application">
            <option name="MAIN_CLASS_NAME"
                    value="com.example.Application" />
            <option name="WORKING_DIRECTORY" value="$UNKNOWN_HOME$/app" />
            <envs>
              <env name="PASSWORD" value="do-not-return-this" />
            </envs>
          </configuration>
        </component>
        """,
    )

    with pytest.raises(IdeaLaunchImportError) as captured:
        IdeaLaunchImporter().select(project, "Application")

    payload_text = str(captured.value.to_payload())
    assert captured.value.error_code is LaunchErrorCode.UNRESOLVED_IDEA_MACRO
    assert payload_text.count("$UNKNOWN_HOME$") == 1
    assert "do-not-return-this" not in payload_text


@pytest.mark.parametrize(
    "unsafe_xml",
    [
        """<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
           <component>&xxe;</component>""",
        "<component><configuration>",
    ],
)
def test_unsafe_or_invalid_xml_is_rejected_without_parser_details(
    tmp_path: Path,
    unsafe_xml: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / ".idea" / "workspace.xml", unsafe_xml)

    with pytest.raises(IdeaLaunchImportError) as captured:
        IdeaLaunchImporter().select(project)

    payload = captured.value.to_payload()
    assert payload["error_code"] == "LAUNCH_CONFIGURATION_NOT_FOUND"
    assert payload["source_warnings"] == [
        ".idea/workspace.xml: IDEA_CONFIGURATION_READ_FAILED"
    ]
    assert "passwd" not in str(payload)


def test_xml_limits_and_symlinks_cannot_expand_import_scope(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.xml"
    _write(
        outside,
        """
        <component>
          <configuration name="Outside" type="Application">
            <option name="MAIN_CLASS_NAME" value="secret.Outside" />
          </configuration>
        </component>
        """,
    )
    run_dir = project / ".run"
    run_dir.mkdir()
    symlink = run_dir / "Outside.xml"
    try:
        symlink.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable in this environment")
    _write(
        project / ".idea" / "workspace.xml",
        "<component>" + (" " * 512) + "</component>",
    )

    importer = IdeaLaunchImporter(max_xml_bytes=128)
    with pytest.raises(IdeaLaunchImportError) as captured:
        importer.select(project)

    payload = captured.value.to_payload()
    assert payload["candidates"] == []
    assert "secret.Outside" not in str(payload)
    assert payload["source_warnings"] == [
        ".idea/workspace.xml: IDEA_CONFIGURATION_READ_FAILED"
    ]


def test_disabled_alternative_jre_is_not_imported(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(
        project / ".run" / "Application.xml",
        """
        <component>
          <configuration name="Application" type="Application">
            <option name="MAIN_CLASS_NAME"
                    value="com.example.Application" />
            <option name="ALTERNATIVE_JRE_PATH"
                    value="/old/jdk" />
            <option name="ALTERNATIVE_JRE_PATH_ENABLED"
                    value="false" />
          </configuration>
        </component>
        """,
    )

    imported = IdeaLaunchImporter().select(project)

    assert imported.intent.runtime_jdk_reference is None


def test_spring_profiles_and_quoted_arguments_preserve_launch_semantics(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(
        project / ".run" / "Application.xml",
        r"""
        <component>
          <configuration name="Application"
               type="SpringBootApplicationConfigurationType">
            <option name="SPRING_BOOT_MAIN_CLASS"
                    value="com.example.Application" />
            <option name="ACTIVE_PROFILES" value="local,worker" />
            <option name="VM_PARAMETERS"
                    value='-Dpath="C:\Program Files\App" -Xmx1g' />
            <option name="PROGRAM_PARAMETERS"
                    value='--name "hello world"' />
          </configuration>
        </component>
        """,
    )

    imported = IdeaLaunchImporter().select(project)

    assert imported.intent.jvm_args == (
        r"-Dpath=C:\Program Files\App",
        "-Xmx1g",
        "-Dspring.profiles.active=local,worker",
    )
    assert imported.intent.program_args == ("--name", "hello world")


@pytest.mark.parametrize(
    ("extra_xml", "expected_context_key"),
    [
        ("<target name='remote' />", "configuration_type"),
        (
            """
            <method>
              <option name="RunConfigurationTask" enabled="true" />
            </method>
            """,
            "unsupported_before_launch_tasks",
        ),
        (
            '<option name="PASS_PARENT_ENVS" value="false" />',
            "argument",
        ),
    ],
)
def test_semantics_joLink_cannot_reproduce_are_rejected_explicitly(
    tmp_path: Path,
    extra_xml: str,
    expected_context_key: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(
        project / ".run" / "Application.xml",
        f"""
        <component>
          <configuration name="Application" type="Application">
            <option name="MAIN_CLASS_NAME"
                    value="com.example.Application" />
            {extra_xml}
          </configuration>
        </component>
        """,
    )

    with pytest.raises(IdeaLaunchImportError) as captured:
        IdeaLaunchImporter().select(project, "Application")

    payload = captured.value.to_payload()
    assert payload["error_code"] == "UNSUPPORTED_LAUNCH_CONFIGURATION"
    assert payload["launch_name"] == "Application"
    assert payload["source_file"] == ".run/Application.xml"
    assert expected_context_key in payload


def test_nested_options_do_not_override_direct_launch_fields(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(
        project / ".run" / "Application.xml",
        """
        <component>
          <configuration name="Application" type="Application">
            <option name="MAIN_CLASS_NAME"
                    value="com.example.Application" />
            <coverage>
              <option name="MAIN_CLASS_NAME"
                      value="wrong.CoverageClass" />
            </coverage>
            <method>
              <option name="Make" enabled="true">
                <option name="VM_PARAMETERS" value="-Dwrong=true" />
              </option>
            </method>
          </configuration>
        </component>
        """,
    )

    imported = IdeaLaunchImporter().select(project)

    assert imported.intent.main_class == "com.example.Application"
    assert imported.intent.jvm_args == ()


def test_utf16_xml_is_rejected_before_entity_scanning(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / ".idea" / "workspace.xml"
    source.parent.mkdir(parents=True)
    source.write_bytes(
        """<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///secret">]>
        <component>&xxe;</component>""".encode("utf-16")
    )

    with pytest.raises(IdeaLaunchImportError) as captured:
        IdeaLaunchImporter().select(project)

    payload = captured.value.to_payload()
    assert payload["error_code"] == "LAUNCH_CONFIGURATION_NOT_FOUND"
    assert payload["source_warnings"] == [
        ".idea/workspace.xml: IDEA_CONFIGURATION_READ_FAILED"
    ]
    assert "secret" not in str(payload)
