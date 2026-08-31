from __future__ import annotations

from pathlib import Path

from jolink_runtime.launch import IdeaEnvironmentImporter


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_imports_only_the_idea_build_environment_subset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        Path,
        "home",
        classmethod(lambda cls: home),
    )
    _write(
        project / ".idea" / "workspace.xml",
        """
        <project>
          <component name="MavenImportPreferences">
            <option name="generalSettings">
              <MavenGeneralSettings>
                <option name="customMavenHome"
                        value="$USER_HOME$/tools/maven" />
                <option name="userSettingsFile"
                        value="$PROJECT_DIR$/settings.xml" />
                <option name="localRepository"
                        value="$USER_HOME$/repository" />
              </MavenGeneralSettings>
            </option>
          </component>
          <component name="MavenRunner">
            <MavenRunnerSettings>
              <option name="jreName" value="build-jdk" />
            </MavenRunnerSettings>
          </component>
          <component name="MavenProjectsManager">
            <option name="activeProfiles">
              <list>
                <option value="company" />
                <option value="local" />
              </list>
            </option>
            <option name="explicitProfiles">
              <map>
                <entry key="enabled-explicit" value="true" />
                <entry key="disabled-explicit" value="false" />
              </map>
            </option>
          </component>
        </project>
        """,
    )
    _write(
        project / ".idea" / "misc.xml",
        """
        <project>
          <component name="ProjectRootManager"
                     project-jdk-name="runtime-jdk" />
        </project>
        """,
    )
    (home / "tools/maven").mkdir(parents=True)
    (project / "settings.xml").write_text(
        "<settings/>\n", encoding="utf-8"
    )
    jdk_table = (
        home
        / "Library"
        / "Application Support"
        / "JetBrains"
        / "Idea"
        / "options"
        / "jdk.table.xml"
    )
    _write(
        jdk_table,
        """
        <application>
          <component name="ProjectJdkTable">
            <jdk version="2">
              <name value="build-jdk" />
              <type value="JavaSDK" />
              <homePath value="$USER_HOME$/jdks/build" />
            </jdk>
            <jdk version="2">
              <name value="runtime-jdk" />
              <type value="JavaSDK" />
              <homePath value="$USER_HOME$/jdks/runtime" />
            </jdk>
          </component>
        </application>
        """,
    )

    preferences = IdeaEnvironmentImporter(
        idea_config_roots=(jdk_table,),
    ).import_preferences(project)

    assert preferences.custom_maven_home == home / "tools" / "maven"
    assert preferences.user_settings_file == project / "settings.xml"
    assert preferences.local_repository == home / "repository"
    assert preferences.project_jdk_name == "runtime-jdk"
    assert preferences.maven_runner_jdk_name == "build-jdk"
    assert preferences.active_profiles == (
        "enabled-explicit",
        "company",
        "local",
    )
    assert preferences.jdk_homes_by_name == {
        "build-jdk": (home / "jdks" / "build",),
        "runtime-jdk": (home / "jdks" / "runtime",),
    }


def test_idea_managed_or_unresolved_values_degrade_to_bounded_warnings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _write(
        project / ".idea" / "workspace.xml",
        """
        <project>
          <component name="MavenImportPreferences">
            <option name="customMavenHome" value="Bundled (Maven 3)" />
            <option name="userSettingsFile" value="$UNKNOWN$/settings.xml" />
          </component>
        </project>
        """,
    )
    preferences = IdeaEnvironmentImporter(
        idea_config_roots=(),
    ).import_preferences(project)

    assert preferences.custom_maven_home is None
    assert preferences.user_settings_file is None
    assert preferences.jdk_homes_by_name == {}
    assert preferences.warnings == (
        "customMavenHome: IDEA-managed value not portable",
        "userSettingsFile: unresolved",
    )


def test_missing_idea_maven_paths_fall_back_with_bounded_warnings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        Path,
        "home",
        classmethod(lambda cls: home),
    )
    _write(
        project / ".idea/workspace.xml",
        """
        <project><component name="MavenImportPreferences">
          <option name="customMavenHome" value="$USER_HOME$/old-maven" />
          <option name="userSettingsFile" value="$USER_HOME$/old-settings.xml" />
        </component></project>
        """,
    )

    preferences = IdeaEnvironmentImporter(
        idea_config_roots=(),
    ).import_preferences(project)

    assert preferences.custom_maven_home is None
    assert preferences.user_settings_file is None
    assert preferences.warnings == (
        "customMavenHome: configured path unavailable; using wrapper/PATH",
        "userSettingsFile: configured path unavailable; using Maven default",
    )


def test_invalid_project_xml_never_exposes_parser_or_file_contents(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    secret = "private-company-token"
    _write(
        project / ".idea" / "workspace.xml",
        f"<!DOCTYPE x [<!ENTITY y '{secret}'>]><project>&y;</project>",
    )

    preferences = IdeaEnvironmentImporter(
        idea_config_roots=(),
    ).import_preferences(project)

    assert preferences.warnings == (".idea/workspace.xml: unreadable",)
    assert secret not in repr(preferences)


def test_imports_legacy_maven_home_option(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "tools/maven").mkdir(parents=True)
    _write(
        project / ".idea" / "workspace.xml",
        """
        <project>
          <component name="MavenImportPreferences">
            <option name="mavenHome" value="$PROJECT_DIR$/tools/maven" />
          </component>
        </project>
        """,
    )

    preferences = IdeaEnvironmentImporter(
        idea_config_roots=(),
    ).import_preferences(project)

    assert preferences.custom_maven_home == project / "tools" / "maven"


def test_non_maven_options_do_not_override_maven_environment(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "correct-maven").mkdir()
    _write(
        project / ".idea" / "workspace.xml",
        """
        <project>
          <component name="MavenImportPreferences">
            <option name="customMavenHome"
                    value="$PROJECT_DIR$/correct-maven" />
          </component>
          <component name="UnrelatedPlugin">
            <option name="customMavenHome"
                    value="$PROJECT_DIR$/wrong-maven" />
            <option name="activeProfiles" value="wrong-profile" />
          </component>
        </project>
        """,
    )

    preferences = IdeaEnvironmentImporter(
        idea_config_roots=(),
    ).import_preferences(project)

    assert preferences.custom_maven_home == project / "correct-maven"
    assert preferences.active_profiles == ()
