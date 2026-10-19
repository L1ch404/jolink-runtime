"""Execute documentation recipes in isolated homes, never the user's client config."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from test_distribution_docs import (
    INSTALL,
    ROOT,
    RULE,
    SKILL,
    blocks,
    expected_environment,
    snippet,
)


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX shell recipe; PowerShell is listed separately"
)
@pytest.mark.parametrize(
    "failed_file", [None, "SKILL.md", "jolink-java-verification.md"]
)
@pytest.mark.parametrize("origin", ["raw", "api"])
def test_staging_recipe_downloads_utf8_and_stops_on_http_errors(
    tmp_path, failed_file, origin
):
    if not shutil.which("curl"):
        pytest.skip("curl required for the documented shell recipe")
    contents = {
        "/main/skills/jolink-java/SKILL.md": SKILL.read_bytes(),
        "/main/rules/jolink-java-verification.md": RULE.read_bytes(),
    }
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path
            if origin == "api":
                parsed = urlsplit(path)
                assert self.headers["Accept"] == "application/vnd.github.raw+json"
                path = (
                    "/"
                    + parse_qs(parsed.query)["ref"][0]
                    + "/"
                    + parsed.path.removeprefix("/contents/")
                )
            requests.append(path)
            if path.rsplit("/", 1)[-1] == failed_file:
                self.send_error(404)
                return
            data = contents[path]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    home = tmp_path / "isolated home 中文"
    home.mkdir()
    recipe = next(
        code
        for code in blocks(INSTALL[0], "sh")
        if code.startswith("jolink_ref=")
        and ("jolink_api=" in code) == (origin == "api")
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Substitute only the origin, so no test needs GitHub availability.
        recipe = recipe.replace(
            (
                "https://api.github.com/repos/L1ch404/jolink-runtime"
                if origin == "api"
                else "https://raw.githubusercontent.com/L1ch404/jolink-runtime"
            ),
            f"http://127.0.0.1:{server.server_port}",
        )
        result = subprocess.run(
            ["sh", "-c", recipe],
            cwd=home,
            env={**os.environ, "HOME": str(home), "TMPDIR": str(home)},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    if failed_file:
        assert result.returncode != 0
        assert not result.stdout.strip(), (
            "must not announce a completed staging directory"
        )
        assert "404" in result.stderr
        if failed_file == "SKILL.md":
            assert len(requests) == 1, "do not continue after the first download fails"
    else:
        assert result.returncode == 0, result.stderr
        stage = Path(result.stdout.strip()).resolve()
        assert stage.is_relative_to(home.resolve())
        assert (stage / "SKILL.md").read_bytes() == SKILL.read_bytes()
        assert (stage / RULE.name).read_bytes() == RULE.read_bytes()
        assert set(requests) == set(contents)
    assert not (home / ".codex").exists()
    assert not (home / ".agents").exists()


@pytest.mark.parametrize("guide", INSTALL, ids=lambda path: path.name)
def test_codex_registration_recipe_preserves_unrelated_config(tmp_path, guide):
    codex = shutil.which("codex")
    if not codex:
        pytest.skip("local Codex CLI not installed")
    home = tmp_path / "isolated-user"
    codex_home = home / "custom-codex-home"
    codex_home.mkdir(parents=True)
    config = codex_home / "config.toml"
    original = 'model = "existing-model"\n[mcp_servers.existing]\ncommand = "existing-tool"\nargs = ["keep"]\n'
    config.write_text(original, encoding="utf-8")
    command = next(
        line
        for block in blocks(guide, "console")
        for line in block.splitlines()
        if line.startswith("codex mcp add ")
    )
    env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(codex_home)}
    registered = subprocess.run(
        [codex, *shlex.split(command)[1:]],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert registered.returncode == 0, registered.stderr
    actual = tomllib.loads(config.read_text(encoding="utf-8"))
    entry = actual["mcp_servers"].pop("jolink-runtime")
    assert actual == tomllib.loads(original)
    assert entry["command"] == "uvx" and entry["args"] == ["jolink-runtime@latest"]
    assert entry["env"] == expected_environment(guide)
    inspected = subprocess.run(
        [codex, "mcp", "get", "jolink-runtime", "--json"],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert inspected.returncode == 0, inspected.stderr
    assert json.loads(inspected.stdout)["name"] == "jolink-runtime"
    # Registration is not a handshake and must not launch joLink or its Worker.
    assert not (home / ".cache/jolink-runtime").exists()


@pytest.mark.parametrize("guide", INSTALL, ids=lambda path: path.name)
def test_every_listed_client_has_an_executable_format(guide):
    text = guide.read_text(encoding="utf-8")
    table = text.split("## 1.", 1)[1].split("## 2.", 1)[0]
    rows = [
        line.split("|")
        for line in table.splitlines()
        if line.startswith("| ") and "https://" in line
    ]
    assert rows
    for row in rows:
        assert set(re.findall(r"`(\w+)`", row[3])) & {
            "codex",
            "basic",
            "gemini",
            "stdio",
            "vscode",
            "copilot",
            "opencode",
        }, row
    assert "Command Palette" not in table and "Add MCP**" not in table


@pytest.mark.parametrize("origin", ["raw", "api"])
def test_powershell_download_recipe_is_not_a_different_payload(origin):
    shell = next(
        code
        for code in blocks(INSTALL[0], "sh")
        if code.startswith("jolink_ref=")
        and ("jolink_api=" in code) == (origin == "api")
    )
    powershell = next(
        code
        for code in blocks(INSTALL[0], "powershell")
        if "$JolinkStage" in code and ("$JolinkApi" in code) == (origin == "api")
    )
    for path in (SKILL, RULE):
        relative = path.relative_to(ROOT).as_posix()
        assert relative in shell and relative in powershell
    assert '$ErrorActionPreference = "Stop"' in powershell
    assert "-OutFile" in powershell and "Out-File" not in powershell
    assert "Write-Output $JolinkStage" in powershell


def execute_recipe(tmp_path, marker, *args, env=None):
    script = tmp_path / (marker + ".py")
    script.write_text(snippet(INSTALL[0], marker), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script), *(str(arg) for arg in args)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )


@pytest.mark.parametrize(
    "kind,key",
    [
        ("basic", "mcpServers"),
        ("gemini", "mcpServers"),
        ("stdio", "mcpServers"),
        ("copilot", "mcpServers"),
        ("vscode", "servers"),
        ("opencode", "mcp"),
    ],
)
@pytest.mark.parametrize("mirror", ["official", "cn"])
def test_documented_json_merge_new_install_and_repeat(tmp_path, kind, key, mirror):
    target = tmp_path / "user 中文" / "agent settings.json"
    result = execute_recipe(tmp_path, "merge-mcp", target, kind, mirror)
    assert result.returncode == 0, result.stderr
    original = target.read_bytes()
    config = json.loads(original)
    entry = config[key]["jolink-runtime"]
    env_key = "environment" if kind == "opencode" else "env"
    assert entry[env_key] == {
        "JOLINK_DOWNLOAD_MIRROR": mirror,
        "JOLINK_JDT_GC_AFTER_BUILD": "1",
    }
    if kind == "opencode":
        assert entry["command"] == ["uvx", "jolink-runtime@latest"]
        assert entry["enabled"] is True and entry["type"] == "local"
    else:
        assert entry["command"] == "uvx" and entry["args"] == ["jolink-runtime@latest"]
        if kind == "copilot":
            assert entry["type"] == "local" and entry["tools"] == ["*"]
        elif kind not in {"basic", "gemini"}:
            assert entry["type"] == "stdio"
        else:
            assert "type" not in entry
    if kind == "gemini":
        assert entry["timeout"] == 60000
    else:
        assert "timeout" not in entry
    assert execute_recipe(tmp_path, "merge-mcp", target, kind, mirror).returncode == 0
    assert target.read_bytes() == original
    assert not list(target.parent.glob("*.bak"))


@pytest.mark.parametrize(
    "kind,key",
    [("basic", "mcpServers"), ("gemini", "mcpServers"),
     ("copilot", "mcpServers"), ("opencode", "mcp")],
)
def test_documented_json_merge_preserves_pins_flags_env_and_other_servers(
    tmp_path, kind, key
):
    target = tmp_path / "settings.json"
    env_key = "environment" if kind == "opencode" else "env"
    launch = (
        {"command": ["uvx", "--python", "3.11", "jolink-runtime@0.1.0a8"]}
        if kind == "opencode"
        else {"command": "uvx", "args": ["--python", "3.11", "jolink-runtime@0.1.0a8"]}
    )
    existing = {
        **launch,
        env_key: {"HTTP_PROXY": "test-proxy", "JOLINK_DOWNLOAD_MIRROR": "official"},
        "disabled": True,
        "autoApprove": [],
        "tools": ["java_status"],
        "timeout": 90000,
    }
    if kind == "opencode":
        existing["enabled"] = False
    before = {
        "clientPreference": "保留",
        key: {"other": {"command": "do-not-run"}, "jolink-runtime": existing},
    }
    raw = b"\xef\xbb\xbf" + json.dumps(before, ensure_ascii=False).encode("utf-8")
    target.write_bytes(raw)
    result = execute_recipe(tmp_path, "merge-mcp", target, kind, "cn")
    assert result.returncode == 0, result.stderr
    after = json.loads(target.read_bytes())
    entry = after[key].pop("jolink-runtime")
    before[key].pop("jolink-runtime")
    assert after == before
    for name, value in launch.items():
        assert entry[name] == value
    assert entry[env_key] == {**existing[env_key], "JOLINK_JDT_GC_AFTER_BUILD": "1"}
    assert entry["disabled"] is True and entry["autoApprove"] == []
    assert entry["tools"] == ["java_status"]
    assert entry["timeout"] == 90000
    if kind == "opencode":
        assert entry["enabled"] is False
    backups = list(tmp_path.glob("*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == raw
    saved = target.read_bytes()
    assert execute_recipe(tmp_path, "merge-mcp", target, kind, "cn").returncode == 0
    assert target.read_bytes() == saved and list(tmp_path.glob("*.bak")) == backups


def test_jsonc_is_not_destroyed_by_strict_json_command(tmp_path):
    target = tmp_path / "settings.jsonc"
    original = b'{// retain my comment\n"mcp":{},}\n'
    target.write_bytes(original)
    result = execute_recipe(tmp_path, "merge-mcp", target, "opencode", "cn")
    assert result.returncode != 0
    assert target.read_bytes() == original and not list(tmp_path.glob("*.bak"))


@pytest.mark.parametrize(
    "mode,fields",
    [
        ("rule", ()),
        ("copilot", ('applyTo: "**"',)),
        ("codebuddy", ("enabled: true", "alwaysApply: true")),
        ("kiro", ("inclusion: always",)),
    ],
)
def test_instruction_install_update_preserves_other_text_and_native_header(
    tmp_path, mode, fields
):
    target = tmp_path / "rules 中文" / "instructions.md"
    target.parent.mkdir()
    header = (
        "---\ndescription: keep my description\nenabled: false\nalwaysApply: false\n---\n"
        if mode == "codebuddy"
        else ""
    )
    prefix, suffix = (
        header + "# Existing guidance\n保持其他指令\n\n",
        "\n\n# Local notes\nkeep these too\n",
    )
    old_rule = "<!-- jolink-java-verification:begin -->\nold joLink version\n<!-- jolink-java-verification:end -->"
    original = (prefix + old_rule + suffix).encode("utf-8")
    target.write_bytes(original)
    result = execute_recipe(tmp_path, "install-text", RULE, target, mode)
    assert result.returncode == 0, result.stderr
    actual = target.read_text(encoding="utf-8")
    assert RULE.read_text(encoding="utf-8").strip() in actual
    assert "保持其他指令" in actual and actual.endswith(suffix)
    assert actual.count("<!-- jolink-java-verification:begin -->") == 1
    for line in fields:
        assert line in actual.split("<!--", 1)[0].splitlines()
    if mode == "codebuddy":
        assert "description: keep my description" in actual
        assert "enabled: false" not in actual and "alwaysApply: false" not in actual
    backups = list(target.parent.glob("*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    saved = target.read_bytes()
    assert execute_recipe(tmp_path, "install-text", RULE, target, mode).returncode == 0
    assert target.read_bytes() == saved and list(target.parent.glob("*.bak")) == backups


@pytest.mark.parametrize("mode", ["rule", "copilot", "codebuddy", "kiro"])
def test_instruction_install_creates_missing_directory(tmp_path, mode):
    target = tmp_path / "new rules" / "rule.md"
    result = execute_recipe(tmp_path, "install-text", RULE, target, mode)
    assert result.returncode == 0, result.stderr
    assert RULE.read_text(encoding="utf-8").strip() in target.read_text(encoding="utf-8")
    assert execute_recipe(tmp_path, "install-text", RULE, target, mode).returncode == 0
    assert not list(target.parent.glob("*.bak"))


def test_skill_copy_is_byte_exact_and_repeat_does_not_add_backup(tmp_path):
    target = tmp_path / "user 中文" / "skills/jolink-java/SKILL.md"
    assert (
        execute_recipe(tmp_path, "install-text", SKILL, target, "skill").returncode == 0
    )
    assert target.read_bytes() == SKILL.read_bytes()
    assert (
        execute_recipe(tmp_path, "install-text", SKILL, target, "skill").returncode == 0
    )
    assert not list(target.parent.glob("*.bak"))


def test_broken_rule_markers_leave_original_untouched(tmp_path):
    target = tmp_path / "AGENTS.md"
    original = b"user text\n<!-- jolink-java-verification:begin -->\nmissing end\n"
    target.write_bytes(original)
    result = execute_recipe(tmp_path, "install-text", RULE, target, "rule")
    assert result.returncode != 0
    assert target.read_bytes() == original and not list(tmp_path.glob("*.bak"))


def test_editor_config_discovery_only_lists_current_host_candidates(tmp_path):
    home = tmp_path / "home"
    base = (
        home / "Library/Application Support"
        if sys.platform == "darwin"
        else home / "config"
    )
    user = base / "CodeBuddy/User"
    candidates = [
        user / "globalStorage/test/settings/mcp.json",
        user / "profiles/another/mcp.json",
    ]
    unrelated = base / "OtherEditor/User/mcp.json"
    for path in [*candidates, unrelated]:
        path.parent.mkdir(parents=True)
        path.write_text("do not print config secrets")
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(base),
        "XDG_CONFIG_HOME": str(base),
    }
    result = execute_recipe(tmp_path, "find-config", "CodeBuddy", env=env)
    assert result.returncode == 0, result.stderr
    assert all(str(path) in result.stdout for path in candidates)
    assert str(unrelated) not in result.stdout and "secrets" not in result.stdout
    assert all(path.read_text() == "do not print config secrets" for path in candidates)


def test_editor_discovery_accepts_custom_user_data_path_without_reading_files(tmp_path):
    user = tmp_path / "isolated profile 中文" / "User"
    candidate = user / "globalStorage/extension/settings/mcp_settings.json"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("private configuration, not JSON")
    result = execute_recipe(tmp_path, "find-config", user)
    assert result.returncode == 0, result.stderr
    assert str(candidate) in result.stdout
    assert "private configuration" not in result.stdout
    assert candidate.read_text() == "private configuration, not JSON"


def test_vscode_native_command_passes_one_json_argument_with_matching_environment():
    for guide in INSTALL:
        command = next(code for code in blocks(guide, "sh")
                       if code.startswith("code --add-mcp "))
        args = shlex.split(command)
        assert args[:2] == ["code", "--add-mcp"] and len(args) == 3
        entry = json.loads(args[2])
        assert entry["name"] == "jolink-runtime"
        assert entry["command"] == "uvx"
        assert entry["args"] == ["jolink-runtime@latest"]
        assert entry["env"] == expected_environment(guide)


@pytest.mark.parametrize("marker", ["find-config", "merge-mcp", "merge-codex", "install-text"])
def test_recipes_print_unicode_paths_in_non_utf8_terminal(tmp_path, marker):
    if marker == "merge-codex":
        pytest.importorskip("tomlkit")
    target = tmp_path / "路径 中文" / "settings.json"
    arguments = {
        "find-config": (target.parent,),
        "merge-mcp": (target, "basic", "cn"),
        "merge-codex": (target.with_suffix(".toml"), "cn"),
        "install-text": (RULE, target.with_suffix(".md"), "rule"),
    }[marker]
    result = execute_recipe(
        tmp_path, marker, *arguments,
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
    )
    assert result.returncode == 0, result.stderr
    assert str(target.parent) in result.stdout


def test_toml_fallback_preserves_comments_and_repeat(tmp_path):
    pytest.importorskip(
        "tomlkit", reason="run with uv run --with tomlkit for optional TOML command"
    )
    target = tmp_path / "config.toml"
    original = '# retain this comment\nmodel = "existing-model"\n[mcp_servers.other]\ncommand="other" # and this\n'
    target.write_text(original)
    result = execute_recipe(tmp_path, "merge-codex", target, "cn")
    assert result.returncode == 0, result.stderr
    text = target.read_text()
    assert "# retain this comment" in text and "# and this" in text
    data = tomllib.loads(text)
    entry = data["mcp_servers"].pop("jolink-runtime")
    assert data == tomllib.loads(original)
    assert entry["args"] == ["jolink-runtime@latest"]
    assert entry["env"]["JOLINK_DOWNLOAD_MIRROR"] == "cn"
    assert execute_recipe(tmp_path, "merge-codex", target, "cn").returncode == 0
    assert target.read_text() == text and len(list(tmp_path.glob("*.bak"))) == 1


@pytest.mark.skipif(
    os.name == "nt" or sys.version_info[:2] != (3, 11),
    reason="execute documented POSIX + Python 3.11 commands",
)
@pytest.mark.parametrize("guide", INSTALL, ids=lambda path: path.name)
def test_command_walkthrough_installs_cursor_then_codex_override_without_gui(
    tmp_path, guide
):
    if not shutil.which("uv") or not shutil.which("codex"):
        pytest.skip("uv and Codex required for command walkthrough")
    home = tmp_path / "用户 home"
    stage = tmp_path / "stage files"
    home.mkdir()
    stage.mkdir()
    shutil.copy2(SKILL, stage / "SKILL.md")
    shutil.copy2(RULE, stage / RULE.name)
    for marker, filename in (
        ("merge-mcp", "merge_mcp.py"),
        ("install-text", "install_text.py"),
    ):
        (stage / filename).write_text(snippet(guide, marker), encoding="utf-8")
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "CODEX_HOME": str(home / "codex config"),
        "UV_CACHE_DIR": str(tmp_path / "uv cache"),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_OFFLINE": "true",
        "PATH": str(Path(sys.executable).resolve().parent)
        + os.pathsep
        + os.environ["PATH"],
    }
    shell = blocks(guide, "sh")
    merge = next(code for code in shell if '/merge_mcp.py" "$HOME/.cursor' in code)
    skill = next(
        code for code in shell if '/install_text.py" "$jolink_stage/SKILL.md"' in code
    )
    prefix = "jolink_stage=" + shlex.quote(str(stage)) + "\n"
    for _ in range(2):
        result = subprocess.run(
            ["sh", "-c", prefix + merge + "\n" + skill],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    config = json.loads((home / ".cursor/mcp.json").read_text())
    assert config["mcpServers"]["jolink-runtime"]["env"] == expected_environment(guide)
    assert (
        home / ".cursor/skills/jolink-java/SKILL.md"
    ).read_bytes() == SKILL.read_bytes()
    assert not list(home.rglob("*.bak"))

    codex_home = Path(env["CODEX_HOME"])
    codex_home.mkdir()
    base = codex_home / "AGENTS.md"
    base.write_text("do not shadow or replace this base")
    override = codex_home / "AGENTS.override.md"
    override.write_text("keep the active override\n")
    register = next(
        line
        for block in blocks(guide, "console")
        for line in block.splitlines()
        if line.startswith("codex mcp add ")
    )
    rule = next(code for code in shell if code.startswith("codex_home="))
    result = subprocess.run(
        ["sh", "-c", prefix + register + "\n" + rule],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert base.read_text() == "do not shadow or replace this base"
    assert override.read_text().startswith("keep the active override\n")
    assert RULE.read_text().strip() in override.read_text()
    assert tomllib.loads((codex_home / "config.toml").read_text())["mcp_servers"][
        "jolink-runtime"
    ]["env"] == expected_environment(guide)
