"""Keep install examples and the shared Skill consistent with executable contracts."""

import json
import re
import shlex
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from jsonschema import Draft202012Validator

from jolink_runtime.server.tool_schema import get_mcp_tools

ROOT = Path(__file__).resolve().parents[2]
INSTALL = (ROOT / "INSTALL.md", ROOT / "INSTALL.zh-CN.md")
SKILL = ROOT / "skills/jolink-java/SKILL.md"


def blocks(path, language):
    return re.findall(r"^```" + language + r"\n(.*?)^```", path.read_text(encoding="utf-8"),
                      re.MULTILINE | re.DOTALL)


def templates(path):
    return [json.loads(value) for value in blocks(path, "json")] + [
        tomllib.loads(value) for value in blocks(path, "toml")
    ]


def test_install_translations_use_identical_commands():
    for language in ("sh", "powershell", "text"):
        assert blocks(INSTALL[0], language) == blocks(INSTALL[1], language)
    # Every client entry must select the same package release.
    for path in INSTALL:
        text = path.read_text(encoding="utf-8")
        versions = re.findall(r"jolink-runtime(?:==|@)([\w.]+)", text)
        assert versions and len(set(versions)) == 1


@pytest.mark.parametrize("path", INSTALL, ids=lambda p: p.name)
def test_native_mcp_templates_describe_the_same_uvx_command(path):
    launch = next(value for value in blocks(path, "text") if value.startswith("uvx ")).strip().split()
    assert len(launch) == 2 and launch[0] == "uvx"
    assert launch[1] == "jolink-runtime@latest"
    for config in templates(path):
        key = next(iter(config))
        entry = config[key]["jolink-runtime"]
        if key == "mcp":
            assert entry["type"] == "local" and entry["enabled"] is True
            argv = entry["command"]
        else:
            assert key in {"mcpServers", "servers", "mcp_servers"}
            assert entry.get("type", "stdio") == "stdio"
            argv = [entry["command"], *entry["args"]]
        assert argv == launch


def server_entry(config):
    key = next(iter(config))
    return key, config[key]["jolink-runtime"]


def expected_environment(path):
    return {
        "JOLINK_DOWNLOAD_MIRROR": "cn" if path.name.endswith("zh-CN.md") else "official",
        "JOLINK_JDT_GC_AFTER_BUILD": "1",
    }


@pytest.mark.parametrize("path", INSTALL, ids=lambda p: p.name)
def test_all_client_formats_pass_mirror_and_gc_to_server(path):
    for config in templates(path):
        key, entry = server_entry(config)
        field = "environment" if key == "mcp" else "env"
        assert entry[field] == expected_environment(path)
        assert ("env" if field == "environment" else "environment") not in entry


def test_install_translations_differ_only_in_download_mirror():
    english, chinese = templates(INSTALL[0]), templates(INSTALL[1])
    assert len(english) == len(chinese)
    for original, translated in zip(english, chinese, strict=True):
        key, entry = server_entry(translated)
        field = "environment" if key == "mcp" else "env"
        entry[field]["JOLINK_DOWNLOAD_MIRROR"] = "official"
        assert original == translated


@pytest.mark.parametrize("path", INSTALL, ids=lambda p: p.name)
def test_native_registration_commands_include_the_template_environment(path):
    commands = re.findall(r"`([^`]*mcp add[^`]*)`", path.read_text(encoding="utf-8"))
    assert commands
    for command in commands:
        args = shlex.split(command)
        # The Gemini table mentions mcp add without providing a command example.
        if "--" not in args:
            continue
        assert args[args.index("--") + 1:] == ["uvx", "jolink-runtime@latest"]
        env = dict(args[i + 1].split("=", 1) for i, value in enumerate(args) if value == "--env")
        assert env == expected_environment(path)


def test_skill_examples_validate_against_current_mcp_schemas():
    schemas = {tool.name: tool.inputSchema for tool in get_mcp_tools()}
    examples = re.findall(
        r"Example .*?tool `([^`]+)`.*?```json\n(.*?)```", SKILL.read_text(encoding="utf-8"), re.DOTALL
    )
    assert examples
    for name, arguments in examples:
        Draft202012Validator(schemas[name]).validate(json.loads(arguments))
    # References to public tools should not silently drift to retired names.
    names = set(re.findall(r"`(java_\w+)(?:`|\()", SKILL.read_text(encoding="utf-8")))
    assert names and names <= schemas.keys()


def test_translated_readme_examples_validate_against_current_mcp_schemas():
    schemas = {tool.name: tool.inputSchema for tool in get_mcp_tools()}
    for value in blocks(ROOT / "README.zh-CN.md", "json"):
        arguments = json.loads(value)
        tool = "java_fast_test" if "tests" in arguments else "java_application"
        Draft202012Validator(schemas[tool]).validate(arguments)


@pytest.mark.parametrize("path", [ROOT / "README.md", ROOT / "README.zh-CN.md", *INSTALL, SKILL,
                                  ROOT / "THIRD_PARTY_NOTICES.md", ROOT / "licenses/README.md"],
                         ids=lambda p: p.name)
def test_distribution_relative_links_resolve(path):
    for destination in re.findall(r"\]\(([^)\s]+)\)", path.read_text(encoding="utf-8")):
        parsed = urlsplit(destination)
        if parsed.scheme or not parsed.path:
            continue
        target = (path.parent / unquote(parsed.path)).resolve()
        assert target.exists(), (path, destination)


def test_language_entrypoints_link_to_matching_installation_guide():
    for suffix in ("", ".zh-CN"):
        readme = (ROOT / f"README{suffix}.md").read_text(encoding="utf-8")
        url = f"https://github.com/L1ch404/jolink-runtime/blob/main/INSTALL{suffix}.md"
        # The copied instruction must contain the URL itself, not just refer to
        # a guide linked elsewhere on the page.
        assert any(
            f"[{url}]({url})" in line
            for line in readme.splitlines() if line.startswith("> ")
        )
        assert not re.search(r"\]\(INSTALL(?:\.zh-CN)?\.md\)", readme)
    # Both installation guides point to the same deployable English Skill.
    for path in INSTALL:
        assert "](skills/jolink-java/SKILL.md)" in path.read_text(encoding="utf-8")
