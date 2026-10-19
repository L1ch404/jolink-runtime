# Install joLink MCP, Skill and Java verification rule

[简体中文](INSTALL.zh-CN.md) · [Overview](README.md)

Configure joLink MCP and install the English `jolink-java` Skill for the user's
current coding agent. Default to user scope and preserve existing configuration.
When requested, also enable the global Java change-verification rule in step 5.
The README installation prompt explicitly requests it, so do not ask again. If the
user only requested MCP/Skill installation, ask once whether to enable the rule;
declining it does not block installation. Respect an explicit opt-out without asking.
Complete the steps you can perform; ask only when the target client or an action
requiring the user's participation is unclear. Do not launch a business application
or run its tests as part of installation.

**Configuration and client verification are separate stages.** This conversation
can finish by saving and checking the MCP configuration, Skill and requested rule.
If the client has not loaded them, hand off to the user in step 6 and stop; that is
a normal completion of configuration, not a failed installation. Step 7 is only
for a conversation in which the client has loaded the tools and instructions.

This is an executable installation guide, not a list of research assignments.
For a listed client, use the steps and templates here; the official links are
references, not required reading during every installation. Consult them only
when the installed client's version or UI differs. Read the common steps and
**one** matching client row, not every client's instructions. This guide assumes
terminal and file-edit tools only; GUI automation is not a prerequisite.

## 0. Prepare the two instruction files

There are three independent install targets: the MCP entry, the English Skill,
and the requested global rule. Installing the PyPI package does not install the
other two. First download the instruction files to a temporary staging directory;
inspect them before changing any client files. No repository clone is needed.
If using a local checkout, use its files instead. If this guide came from a ref
other than `main`, set that same ref below; do not guess a Git tag from a PyPI version.

macOS/Linux (run in one shell):

```sh
jolink_ref=main
jolink_stage="$(mktemp -d "${TMPDIR:-/tmp}/jolink-install.XXXXXX")" || exit 1
jolink_raw="https://raw.githubusercontent.com/L1ch404/jolink-runtime/$jolink_ref"
curl --fail --location --show-error --retry 2 "$jolink_raw/skills/jolink-java/SKILL.md" --output "$jolink_stage/SKILL.md" &&
curl --fail --location --show-error --retry 2 "$jolink_raw/rules/jolink-java-verification.md" --output "$jolink_stage/jolink-java-verification.md" &&
printf '%s\n' "$jolink_stage"
```

Windows PowerShell (run in one session):

```powershell
$ErrorActionPreference = "Stop"
$JolinkRef = "main"
$JolinkStage = Join-Path ([IO.Path]::GetTempPath()) ("jolink-install-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $JolinkStage | Out-Null
$JolinkRaw = "https://raw.githubusercontent.com/L1ch404/jolink-runtime/$JolinkRef"
Invoke-WebRequest -UseBasicParsing -Uri "$JolinkRaw/skills/jolink-java/SKILL.md" -OutFile (Join-Path $JolinkStage "SKILL.md")
Invoke-WebRequest -UseBasicParsing -Uri "$JolinkRaw/rules/jolink-java-verification.md" -OutFile (Join-Path $JolinkStage "jolink-java-verification.md")
Write-Output $JolinkStage
```

Read both files as UTF-8. The Skill starts with YAML frontmatter containing
`name: jolink-java`; the rule has the `jolink-java-verification:begin` and `:end`
HTML comments. Keep the Skill in English and the rule verbatim, not translated or
summarized. If a download fails or returns HTML, leave the existing installation
untouched and report that URL/error; do not keep downloading into live config.
Keep the staging path for steps 4–5. If your terminal tool creates a fresh shell
for every command, retain the returned absolute path instead of relying on shell variables.

### Only if Raw GitHub is unreachable

GitHub's own [Contents API](https://docs.github.com/en/rest/repos/contents#get-repository-content)
can return the same raw files. This is not a third-party mirror. Use this alternative
only if the first download route fails; keep the same ref. Public files need no token.
If both routes fail, use the user's approved proxy or supplied files, then stop trying
other download hosts. The package/Worker mirror setting does not fix these downloads.

macOS/Linux:

```sh
jolink_ref=main
jolink_stage="$(mktemp -d "${TMPDIR:-/tmp}/jolink-install.XXXXXX")" || exit 1
jolink_api="https://api.github.com/repos/L1ch404/jolink-runtime/contents"
curl --fail --location --show-error --header 'Accept: application/vnd.github.raw+json' "$jolink_api/skills/jolink-java/SKILL.md?ref=$jolink_ref" --output "$jolink_stage/SKILL.md" &&
curl --fail --location --show-error --header 'Accept: application/vnd.github.raw+json' "$jolink_api/rules/jolink-java-verification.md?ref=$jolink_ref" --output "$jolink_stage/jolink-java-verification.md" &&
printf '%s\n' "$jolink_stage"
```

Windows PowerShell:

```powershell
$ErrorActionPreference = "Stop"
$JolinkRef = "main"
$JolinkStage = Join-Path ([IO.Path]::GetTempPath()) ("jolink-install-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $JolinkStage | Out-Null
$JolinkApi = "https://api.github.com/repos/L1ch404/jolink-runtime/contents"
$JolinkHeaders = @{ Accept = "application/vnd.github.raw+json" }
Invoke-WebRequest -UseBasicParsing -Headers $JolinkHeaders -Uri "$JolinkApi/skills/jolink-java/SKILL.md?ref=$JolinkRef" -OutFile (Join-Path $JolinkStage "SKILL.md")
Invoke-WebRequest -UseBasicParsing -Headers $JolinkHeaders -Uri "$JolinkApi/rules/jolink-java-verification.md?ref=$JolinkRef" -OutFile (Join-Path $JolinkStage "jolink-java-verification.md")
Write-Output $JolinkStage
```


## 1. Select paths for the current client

Identify the client and its surface (CLI, desktop, or editor extension) from the
current session. Ask once only if unclear. The commands run on the developer machine
where that client starts MCP, not an unrelated SSH/container environment.

Use the matching row only. Paths below are user-level defaults; expand the actual
home/config overrides before writing. Reuse an already active installation.
The format name is an argument to the merge command in step 3, not a new joLink option.

This guide adds joLink to an **already installed agent**. It does not register
client accounts or configure model credentials. For a fresh client still at its
login/provider setup screen, finish the known file installation and hand off the
missing client initialization. Do not copy another client's credentials or treat
“not logged in” as a joLink installation failure.

| Client | MCP config / method | Format | English Skill file | Reference |
|---|---|---|---|---|
| Codex desktop / CLI / IDE extension | `$CODEX_HOME/config.toml`, otherwise `~/.codex/config.toml`; native command or TOML command below | `codex` | `~/.agents/skills/jolink-java/SKILL.md` | [MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), [Skills](https://learn.chatgpt.com/docs/build-skills) |
| Claude Code | `~/.claude.json`, top-level server entry; not project `.mcp.json` | `stdio` | `~/.claude/skills/jolink-java/SKILL.md` | [MCP](https://code.claude.com/docs/en/mcp), [Skills](https://code.claude.com/docs/en/skills) |
| Cursor IDE / CLI | `~/.cursor/mcp.json` | `basic` | `~/.cursor/skills/jolink-java/SKILL.md` | [MCP](https://cursor.com/docs/mcp), [Skills](https://cursor.com/docs/skills) |
| VS Code / GitHub Copilot | Local harness: active profile's `mcp.json`, or `code --add-mcp` in step 3.1 for a new entry. Copilot harness: `$COPILOT_HOME/mcp-config.json`, otherwise `~/.copilot/mcp-config.json`. Configure only the harness in use | `vscode` for Local; `copilot` for Copilot | `~/.copilot/skills/jolink-java/SKILL.md` | [MCP](https://code.visualstudio.com/docs/agent-customization/mcp-servers), [Skills](https://code.visualstudio.com/docs/agent-customization/agent-skills) |
| GitHub Copilot CLI | `$COPILOT_HOME/mcp-config.json`, otherwise `~/.copilot/mcp-config.json` | `copilot` | `~/.copilot/skills/jolink-java/SKILL.md` | [Configuration](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers) |
| CodeBuddy Code CLI | First existing: `~/.codebuddy/.mcp.json` → `~/.codebuddy/mcp.json` → `~/.codebuddy.json`; if none exist create the first. Respect `CODEBUDDY_CONFIG_DIR` for the config directory | `stdio` | `~/.codebuddy/skills/jolink-java/SKILL.md` | [MCP and precedence](https://www.codebuddy.cn/docs/cli/mcp) |
| CodeBuddy IDE / editor plugin | CodeBuddy CN IDE 4.12 was tested with `~/.codebuddy/mcp.json` (not the CLI's default `.mcp.json`). Reuse the active file for other versions/plugins; discover it below if unclear | `stdio` | IDE: `~/.codebuddy/skills/jolink-java/SKILL.md`; plugin: reuse its known user Skill location | [IDE MCP](https://www.codebuddy.cn/docs/ide/User-guide/MCP), [IDE scope](https://www.codebuddy.cn/docs/ide/User-guide/Overview) |
| Gemini CLI | `~/.gemini/settings.json` | `gemini` | `~/.gemini/skills/jolink-java/SKILL.md` | [MCP](https://geminicli.com/docs/tools/mcp-server/), [Skills](https://geminicli.com/docs/cli/skills/) |
| OpenCode | Active `opencode.json` / `opencode.jsonc` in the user config directory, normally `~/.config/opencode/`; respect explicit config overrides | `opencode` | `~/.config/opencode/skills/jolink-java/SKILL.md` | [Configuration](https://opencode.ai/docs/config/), [MCP](https://opencode.ai/docs/mcp-servers/) |
| Cline | CLI and newer extension (4.1 tested): `~/.cline/data/settings/cline_mcp_settings.json`, respecting `CLINE_MCP_SETTINGS_PATH`. Older extensions keep the file under editor `User/globalStorage`; discover and update that existing file, not a second copy | `basic` | `~/.cline/skills/jolink-java/SKILL.md` | [MCP](https://docs.cline.bot/mcp/mcp-overview), [Skills](https://docs.cline.bot/customization/skills) |
| Roo Code | Discover the editor extension's `mcp_settings.json` below; do not create project `.roo/mcp.json` for a user install | `basic` | `~/.roo/skills/jolink-java/SKILL.md` | [MCP](https://docs.roocode.com/features/mcp/using-mcp-in-roo), [Skills](https://docs.roocode.com/features/skills) |
| Windsurf / Cascade | Legacy: `~/.codeium/windsurf/mcp_config.json`. Newer Devin Desktop Cascade: `$XDG_CONFIG_HOME/devin/mcp_config.json` or `~/.config/devin/mcp_config.json`; Windows `%APPDATA%\\devin\\mcp_config.json`. Use the installed product's existing route, not all of them | `basic` | `~/.codeium/windsurf/skills/jolink-java/SKILL.md`; reuse recognized `~/.config/devin/skills/jolink-java/SKILL.md` if already active | [MCP](https://docs.windsurf.com/windsurf/cascade/mcp), [Skills](https://docs.windsurf.com/windsurf/cascade/skills) |
| Kiro IDE / CLI | `~/.kiro/settings/mcp.json` | `basic` | `~/.kiro/skills/jolink-java/SKILL.md` | [MCP](https://kiro.dev/docs/mcp/configuration/), [Skills](https://kiro.dev/docs/skills/) |

Do not create a higher-priority file that hides an existing CodeBuddy configuration.
Inspect same-name project/plugin entries if a user-level edit is being overridden.
Compatible clients may also load `.agents/skills` or another client's Skill folder:
update the recognized copy instead of installing duplicates.

### Discover an editor-managed config from the terminal

Use this only for an editor/profile route whose exact file is not known. After
step 2 makes uv available, save this block as `find_mcp.py` in the staging directory
using the agent's file tool. It only prints filenames; it does not inspect secrets,
write configuration, or select an arbitrary first match.

```python
# jolink: find-config
import os, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
home = Path.home()
base = (home / "Library/Application Support" if sys.platform == "darwin"
        else Path(os.environ["APPDATA"]) if os.name == "nt"
        else Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))))
location = Path(os.path.expandvars(sys.argv[1])).expanduser()
root = location if location.is_absolute() else base / location / "User"
names = {"mcp.json", ".mcp.json", "mcp_config.json",
         "cline_mcp_settings.json", "mcp_settings.json"}
print("search_root:", root)
for path in sorted(root.rglob("*")) if root.is_dir() else ():
    if path.is_file() and (path.name in names or path.name == "mcp.jsonc"):
        print(path)
```

Run with the actual editor's user-data folder name, for example `Code`,
`Code - Insiders`, `Cursor`, `CodeBuddy`, or `Windsurf`. Example for CodeBuddy:

```sh
uv run --no-project --python 3.11 python "$jolink_stage/find_mcp.py" CodeBuddy
```

```powershell
uv run --no-project --python 3.11 python (Join-Path $JolinkStage "find_mcp.py") CodeBuddy
```

Read the matching client/profile file before selecting it. Multiple results require
identifying the active profile; zero results means the path is still unknown.
For a custom user-data directory, pass its absolute `User` path to this same command.
It searches only the selected editor directory, not the fixed home paths listed
for Cline or CodeBuddy. An empty result is not a reason to repeatedly reinstall.
For a fresh closed-source IDE/plugin without a documented file path or CLI,
report that component as needing the user's config-file path. Do not guess a path,
edit internal databases, install another client, or switch to project scope silently.

For a listed fixed-path client, an absent config is normal: the merge command can
create it. For a client not listed, establish its supported local stdio configuration
first, then use the same command and instruction files.

## 2. Make uvx available

No preinstalled Python or pip is required. Install uv with its standalone installer,
not `pip install uv`. uv supplies a compatible Python for the commands below.
[uv's automatic Python downloads](https://docs.astral.sh/uv/guides/install-python/#automatic-python-downloads)
are enabled by default; an existing installation does not need to be replaced.

Run:

```sh
uv --version
uvx --version
```

If both work, use the Python check below. Otherwise install uv using the appropriate
[official command](https://docs.astral.sh/uv/getting-started/installation/).

macOS/Linux:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Confirm `uvx --version` works after installation. uvx will download and cache
joLink from PyPI and prepare its Python environment. Do not separately install
joLink globally, clone its repository, or locate a joLink executable.

Confirm that uv can run the helper interpreter before editing client files:

```console
uv run --no-project --python 3.11 python --version
```

If Python 3.11 is missing, uv downloads and manages it. This command does not
install a system-wide Python, change the user's default interpreter, or require
an administrator. If network policy or `UV_PYTHON_DOWNLOADS=never` prevents it,
report that precise blocker; do not repeatedly retry or override the policy.

If uvx was just installed but this shell cannot find it, use the executable path
reported by the installer, or add that directory to this shell's PATH only. Do not
block the installation on asking the user to open another terminal. For a GUI client's specific `uvx not found`
error, resolve it with `command -v uvx` (macOS/Linux) or
`(Get-Command uvx -CommandType Application).Source` (PowerShell), then use that
executable's absolute path as `command`. Keep its arguments separate; an executable
path containing spaces is one JSON string, without extra embedded shell quotes.
Do not add a `cmd /c` wrapper merely because the OS is Windows.

## 3. Register the MCP server by command

The client starts this command; do not run it directly in a terminal to “prove”
that the current agent has connected:

```text
uvx jolink-runtime@latest
```

`@latest` resolves the newest published package when the process starts; it does
not replace a running process or update the separately installed Skill/rule.
Set these defaults on the MCP entry, not in the system environment:

- `JOLINK_DOWNLOAD_MIRROR=official` (`cn` for users in China): Worker JDK/Eclipse
  download source only; it does not configure PyPI/Python or GitHub downloads.
- `JOLINK_JDT_GC_AFTER_BUILD=1`: request compiler GC after a build, not on no-change
  reuse; this does not GC the application/test JVM.

Read the selected config before editing. Preserve other servers, proxies,
approvals, and deliberate package pins. Existing disabled servers/restricted tools
are not automatically re-enabled by the merge helpers. If the user requested an
upgrade, change only the joLink package argument to `jolink-runtime@latest`;
otherwise retain an explicit pin. Do not use blanket approval flags.

Choose **one** registration path, then continue to step 4:

- New entry and the matching CLI already exists: **3.1**.
- JSON-based client, including reinstallations: **3.2**.
- Codex without its CLI, or updating an existing TOML while retaining comments: **3.3**.

Do not execute all three or copy another client's example paths. Determine the
selected client's actual config, Skill and rule destinations once from the tables,
then use those same destinations throughout.

### 3.1 Native CLI registration: new entries only

Use **one** matching command, only if that CLI is already installed. These commands
work in a shell or PowerShell. Do not install a client CLI merely to write config.
For reinstallation use the merge commands below so extra settings are retained.

Codex:

```console
codex mcp add jolink-runtime --env JOLINK_DOWNLOAD_MIRROR=official --env JOLINK_JDT_GC_AFTER_BUILD=1 -- uvx jolink-runtime@latest
codex mcp get jolink-runtime
```

Claude Code:

```console
claude mcp add --transport stdio --scope user jolink-runtime --env JOLINK_DOWNLOAD_MIRROR=official --env JOLINK_JDT_GC_AFTER_BUILD=1 -- uvx jolink-runtime@latest
claude mcp get jolink-runtime
```

GitHub Copilot CLI:

```console
copilot mcp add jolink-runtime --env JOLINK_DOWNLOAD_MIRROR=official --env JOLINK_JDT_GC_AFTER_BUILD=1 -- uvx jolink-runtime@latest
```

VS Code Local harness (macOS/Linux, new entry):

```sh
code --add-mcp '{"name":"jolink-runtime","command":"uvx","args":["jolink-runtime@latest"],"env":{"JOLINK_DOWNLOAD_MIRROR":"official","JOLINK_JDT_GC_AFTER_BUILD":"1"}}'
```

VS Code writes the user profile. For a named profile/custom user-data directory,
include the instance's existing `--profile`/`--user-data-dir` arguments. On Windows,
for an existing entry, or without `code`, locate the active profile's `mcp.json`
in step 1 and merge with the `vscode` format in step 3.2; do not install another CLI.

These inspect/register the server. They do not prove this chat's tool list has
reloaded. An “already exists” response means update that entry, not add another name.

### 3.2 JSON config: one reusable merge command

Save the following as `merge_mcp.py` in the staging directory. This helper uses
Python's standard library. uv can supply Python 3.11; no global Python/pip install
or new joLink CLI is needed.

It backs up changed files, merges only joLink, preserves existing command arguments
and environment values, and prints the saved entry. On an unchanged repeat it makes
no additional backup. It does not change approvals, start MCP, or install Skill/rules.

```python
# jolink: merge-mcp
import json, os, shutil, sys, time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
target = Path(os.path.expandvars(sys.argv[1])).expanduser()
kind, mirror = sys.argv[2:4]
root_key = {"stdio": "mcpServers", "basic": "mcpServers", "gemini": "mcpServers",
            "copilot": "mcpServers", "vscode": "servers", "opencode": "mcp"}[kind]
original = target.read_bytes() if target.exists() else None
# JSONC is edited separately below; a parse error leaves the original untouched.
data = json.loads(original.decode("utf-8-sig")) if original else {}
entry = data.setdefault(root_key, {}).setdefault("jolink-runtime", {})
if kind == "opencode":
    entry["type"] = "local"
    entry.setdefault("command", ["uvx", "jolink-runtime@latest"])
    entry.setdefault("enabled", True)
    env = entry.setdefault("environment", {})
else:
    entry.setdefault("command", "uvx")
    entry.setdefault("args", ["jolink-runtime@latest"])
    if kind in {"stdio", "vscode"}:
        entry["type"] = "stdio"
    elif kind == "copilot":
        entry["type"] = "local"
        entry.setdefault("tools", ["*"])
    env = entry.setdefault("env", {})
if kind == "gemini":
    entry.setdefault("timeout", 60000)
env.setdefault("JOLINK_DOWNLOAD_MIRROR", mirror)
env.setdefault("JOLINK_JDT_GC_AFTER_BUILD", "1")
encoded = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
if encoded != original:
    target.parent.mkdir(parents=True, exist_ok=True)
    if original is not None:
        backup = target.with_name(target.name + ".jolink-" + str(time.time_ns()) + ".bak")
        shutil.copy2(target, backup)
        print("backup:", backup)
    target.write_bytes(encoded)
print("saved:", target)
print(json.dumps({root_key: {"jolink-runtime": entry}}, ensure_ascii=False, indent=2))
```

Select the config and format from step 1. Example for **Cursor**; the installer
must substitute the selected client's path/format, not ask the user to edit the command:

```sh
uv run --no-project --python 3.11 python "$jolink_stage/merge_mcp.py" "$HOME/.cursor/mcp.json" basic official
```

```powershell
uv run --no-project --python 3.11 python (Join-Path $JolinkStage "merge_mcp.py") (Join-Path $HOME ".cursor/mcp.json") basic official
```

Formats: `stdio` → `mcpServers` with `type: stdio`;
`basic` → `mcpServers` with command/args;
`gemini` → `basic` plus a default `timeout: 60000` (milliseconds), retaining an existing value;
`copilot` → `mcpServers` with `type: local` and `tools: ["*"]`;
`vscode` → profile `servers`;
`opencode` → `mcp`, command array and `environment`.
Copilot's `tools` exposes tools; it does not approve their execution.
Gemini CLI 0.64's `mcp list` defaults to only 5 seconds without this setting.
Locally it reported Disconnected before joLink finished its handshake; a 60-second
setting connected successfully. This is a host timeout, not a Java compile timeout.

**Existing JSONC:** do not run a strict-JSON rewrite over comments/trailing commas.
First run the merge helper against a new file in the staging directory to produce
the correct joLink object for the selected format. Then use the agent's file-edit
tool to insert/update that object in the actual JSONC, preserving surrounding text.
Check the result with a read-only JSONC parser, not a JSONC-to-JSON rewrite:

```sh
uv run --no-project --python 3.11 --with json5 python -c "import json5,sys; json5.load(open(sys.argv[1],encoding='utf-8-sig')); print('JSONC valid')" "$jolink_config"
```

```powershell
uv run --no-project --python 3.11 --with json5 python -c "import json5,sys; json5.load(open(sys.argv[1],encoding='utf-8-sig')); print('JSONC valid')" $JolinkConfig
```

Set `jolink_config` / `$JolinkConfig` to the actual selected JSONC path before this
check. The merge helper stops without writing if JSON parsing fails. This is a
file operation, not a GUI requirement. Likewise,
read a custom wrapper/remote entry before deciding how to update it; the merge
helper deliberately does not overwrite an existing launch command.

### 3.3 Codex config without the native CLI / preserving an existing TOML

Save this as `merge_codex.py` in the staging directory. `tomlkit` is used only by
this temporary command to preserve TOML comments; it is not installed into joLink.

```python
# jolink: merge-codex
import os, shutil, sys, time
from pathlib import Path
import tomlkit

sys.stdout.reconfigure(encoding="utf-8")
target = Path(os.path.expandvars(sys.argv[1])).expanduser()
mirror = sys.argv[2]
original = target.read_bytes() if target.exists() else b""
data = tomlkit.parse(original.decode("utf-8-sig"))
entry = data.setdefault("mcp_servers", {}).setdefault("jolink-runtime", {})
entry.setdefault("command", "uvx")
entry.setdefault("args", ["jolink-runtime@latest"])
env = entry.setdefault("env", {})
env.setdefault("JOLINK_DOWNLOAD_MIRROR", mirror)
env.setdefault("JOLINK_JDT_GC_AFTER_BUILD", "1")
encoded = tomlkit.dumps(data).encode("utf-8")
if encoded != original:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(target.name + ".jolink-" + str(time.time_ns()) + ".bak")
        shutil.copy2(target, backup)
        print("backup:", backup)
    target.write_bytes(encoded)
print("saved:", target)
```

```sh
uv run --no-project --python 3.11 --with tomlkit python "$jolink_stage/merge_codex.py" "${CODEX_HOME:-$HOME/.codex}/config.toml" official
```

```powershell
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
uv run --no-project --python 3.11 --with tomlkit python (Join-Path $JolinkStage "merge_codex.py") (Join-Path $CodexHome "config.toml") official
```

After either method, inspect the saved entry. Keep executable and arguments
separate. On Windows use `/` or escaped `\\` in JSON paths, not embedded shell
quotes. If the client specifically reports that it cannot find uvx, resolve its
absolute executable as described in step 2; do not add `cmd /c` without a reason.

## 4. Write the English Skill by command

Use the inspected [English Skill](skills/jolink-java/SKILL.md) from step 0 and the
full destination from step 1. Its name is `jolink-java` and filename exactly
`SKILL.md`, not `SKILL.md.txt`. Read any existing copy first: if it has user
customizations, preserve those or confirm replacement rather than silently losing them.

Save this shared file-writing helper as `install_text.py` in the staging directory.
It copies Skill bytes unchanged. Step 5 also uses it to update the rule's marked
span without replacing other instructions. Changed existing files get backups;
identical content is left untouched.

```python
# jolink: install-text
import os, re, shutil, sys, time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
source = Path(os.path.expandvars(sys.argv[1])).expanduser()
target = Path(os.path.expandvars(sys.argv[2])).expanduser()
mode = sys.argv[3]
incoming = source.read_bytes()
original = target.read_bytes() if target.exists() else b""
if mode == "skill":
    encoded = incoming
else:
    begin = "<!-- jolink-java-verification:begin -->"
    end = "<!-- jolink-java-verification:end -->"
    rule = incoming.decode("utf-8-sig").strip()
    if rule.count(begin) != 1 or rule.count(end) != 1:
        raise SystemExit("Expected the full marked rule, not HTML or a summary.")
    current = original.decode("utf-8-sig")
    if begin in current or end in current:
        if current.count(begin) != 1 or current.count(end) != 1:
            raise SystemExit("Review duplicate/broken joLink markers before editing.")
        start, finish = current.index(begin), current.index(end) + len(end)
        if finish <= start:
            raise SystemExit("Review reversed joLink markers before editing.")
        current = current[:start] + rule + current[finish:]
    else:
        current = current.rstrip() + ("\n\n" if current.strip() else "") + rule + "\n"
    headers = {"rule": {}, "copilot": {"applyTo": '"**"'},
               "codebuddy": {"enabled": "true", "alwaysApply": "true"},
               "kiro": {"inclusion": "always"}}[mode]
    if headers:
        match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", current, re.S)
        header = match[1] if match else ""
        body = current[match.end():] if match else current
        for key, value in headers.items():
            line = key + ": " + value
            if re.search(r"^" + re.escape(key) + r":.*$", header, re.M):
                header = re.sub(r"^" + re.escape(key) + r":.*$", line, header, flags=re.M)
            else:
                header = header.rstrip() + ("\n" if header else "") + line
        current = "---\n" + header + "\n---\n" + body
    encoded = current.encode("utf-8")
if encoded != original:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(target.name + ".jolink-" + str(time.time_ns()) + ".bak")
        shutil.copy2(target, backup)
        print("backup:", backup)
    target.write_bytes(encoded)
print("saved:", target)
```

Example for **Cursor**; use the selected client's Skill destination instead when different:

```sh
uv run --no-project --python 3.11 python "$jolink_stage/install_text.py" "$jolink_stage/SKILL.md" "$HOME/.cursor/skills/jolink-java/SKILL.md" skill
```

```powershell
uv run --no-project --python 3.11 python (Join-Path $JolinkStage "install_text.py") (Join-Path $JolinkStage "SKILL.md") (Join-Path $HOME ".cursor/skills/jolink-java/SKILL.md") skill
```

Re-read the destination as UTF-8. Confirm frontmatter `name: jolink-java` and the
full instructions. Do not translate it. File existence confirms the copy, not that
the current client has loaded it.

For a Kiro custom agent, append `skill://~/.kiro/skills/jolink-java/SKILL.md` to its
existing `resources` array if not already covered, using a narrow file edit.
Do not replace the agent definition or grant additional tools.

## 5. Write the requested global rule by command

Use [rules/jolink-java-verification.md](rules/jolink-java-verification.md) from the
same staging download, verbatim. Enable it when requested (the README prompt does
request it); otherwise ask once. An explicit opt-out skips this step.

Select one destination and pass its mode to `install_text.py` from step 4.
The helper updates the block bounded by `<!-- jolink-java-verification:begin -->`
and `<!-- jolink-java-verification:end -->`, preserving surrounding instructions.
Dedicated rule formats also get their required native frontmatter. This is not
a replacement for the user's system prompt or other rules.

| Client | User-level rule destination | Mode | Reference / limit |
|---|---|---|---|
| Codex desktop / CLI / IDE extension | `~/.codex/AGENTS.md` (respect `CODEX_HOME`); use an existing nonempty `AGENTS.override.md` instead, never create a new override | `rule` | [Instructions](https://learn.chatgpt.com/docs/agent-configuration/agents-md) |
| Claude Code | `~/.claude/rules/jolink-java-verification.md` | `rule` | [User rules](https://code.claude.com/docs/en/memory#user-level-rules) |
| Cursor IDE / CLI | No verified terminal interface for IDE User Rules or CLI user-global rules; hand off this component only | manual | [IDE rules](https://cursor.com/docs/rules#user-rules), [CLI rules](https://cursor.com/docs/cli/using) |
| VS Code / GitHub Copilot | Copilot harness: `~/.copilot/instructions/jolink-java-verification.instructions.md`; Local harness: reuse its identified profile instructions file | `copilot` | [Instruction locations](https://code.visualstudio.com/docs/agent-customization/custom-instructions) |
| GitHub Copilot CLI | `~/.copilot/instructions/jolink-java-verification.instructions.md`, respect `COPILOT_HOME` | `copilot` | [Instructions](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions) |
| CodeBuddy Code CLI | `~/.codebuddy/rules/jolink-java-verification.md`, respect `CODEBUDDY_CONFIG_DIR` | `codebuddy` | [Rules](https://www.codebuddy.cn/docs/cli/memory) |
| CodeBuddy IDE / editor plugin | Update an already identified user-rule file; otherwise hand off global-rule setup instead of inventing a CLI-compatible path | manual if path unknown | [IDE rules](https://www.codebuddy.cn/docs/ide/User-guide/Rules) |
| Gemini CLI | `~/.gemini/GEMINI.md`, or the configured user context filename | `rule` | [Context](https://geminicli.com/docs/cli/gemini-md/) |
| OpenCode | Active `~/.config/opencode/AGENTS.md`; if Claude fallback is active, write a separate rule file and append its absolute path to the global `instructions` list instead | `rule` | [Rules](https://opencode.ai/docs/rules/) |
| Cline | `~/Documents/Cline/Rules/jolink-java-verification.md` unless a different global rules directory is configured | `rule` | [Rules](https://docs.cline.bot/customization/cline-rules); on Windows resolve Documents using `[Environment]::GetFolderPath("MyDocuments")` |
| Roo Code | `~/.roo/rules/jolink-java-verification.md` | `rule` | [Instructions](https://docs.roocode.com/features/custom-instructions) |
| Windsurf / Cascade | `~/.codeium/windsurf/memories/global_rules.md` | `rule` | [Rules](https://docs.windsurf.com/windsurf/cascade/memories); total file limit 6,000 characters, do not delete user rules to fit |
| Kiro IDE / CLI | `~/.kiro/steering/jolink-java-verification.md` | `kiro` | [Steering](https://kiro.dev/docs/steering/); include this file in the active custom agent's resources when needed |

Example for **Codex**, including its override precedence:

```sh
codex_home="${CODEX_HOME:-$HOME/.codex}"
jolink_rule="$codex_home/AGENTS.md"
if [ -s "$codex_home/AGENTS.override.md" ]; then jolink_rule="$codex_home/AGENTS.override.md"; fi
uv run --no-project --python 3.11 python "$jolink_stage/install_text.py" "$jolink_stage/jolink-java-verification.md" "$jolink_rule" rule
```

```powershell
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$JolinkRule = Join-Path $CodexHome "AGENTS.md"
$Override = Join-Path $CodexHome "AGENTS.override.md"
if ((Test-Path -LiteralPath $Override) -and (Get-Item -LiteralPath $Override).Length -gt 0) { $JolinkRule = $Override }
uv run --no-project --python 3.11 python (Join-Path $JolinkStage "install_text.py") (Join-Path $JolinkStage "jolink-java-verification.md") $JolinkRule rule
```

For another client, substitute its actual destination and mode from the table.
For example, CodeBuddy CLI uses its rules path and `codebuddy`; Copilot uses
`copilot`; Kiro uses `kiro`. Re-run updates the same marked span, not an appended
duplicate. If existing customized text/markers need review, inspect and resolve it
with the user's consent before overwriting.

For a manual-only entry, finish MCP/Skill, return the prepared rule file path/body,
and state precisely that global-rule activation remains manual. Do not open a GUI,
modify an internal settings database, silently use project rules, or report that
a file written to an unverified path is active.

## 6. Check configuration and hand off if reloading is needed

Check the results of steps 1–5: uvx is available, the selected client's saved
configuration contains the joLink entry with its environment settings, unrelated
configuration is preserved, and the English Skill file is in the selected location.
If requested, also check the global rule's destination, preserved surrounding text
and automatic-loading setting. Report any manual-only component separately.
These checks establish **configuration complete**, not a connected MCP server or
a Skill/rule loaded by the client. Package downloads may still happen on first start.

If the client has already loaded the new tools, Skill and requested rule into this conversation,
continue to step 7. Otherwise, **stop here** and tell the user the required client
action: reconnect MCP or restart the client as appropriate, then start a new chat.
Reloading the user's active client is the handoff, not a command the installer
should force by killing processes.
Opening a chat before installation does not guarantee it sees what is installed later.

Do not bypass an unavailable client tool list by writing a standalone MCP client,
manually sending protocol messages, or directly launching the server from a terminal.
Do not reinstall, edit the configuration repeatedly, or kill/restart the user's
agent process to finish verification in this chat. Independently connecting to a
server does not prove this client is connected; reading SKILL.md as a file does
not prove the client has registered the Skill.

Report the client, MCP configuration location, Skill location, global-rule status/location
and the next user action. For example: “Configuration complete; client loading and connection
verification are pending. Please reload the client, then verify in a new chat.
No reinstall is needed.” Do not claim “connected” without observing it.

Give concrete handoff details: the actual MCP config path and server name, Skill
file path, rule file/UI entry, and the one reload action needed in this client.
Separate “saved” from “loaded/connected”. If a manual-only component remains, deliver its
prepared content and explain what is not active. Do not turn the whole installation
back into a set of GUI instructions.

For a host connection check before handoff, use the existing client's native diagnostics:

| Client | Native check | What it proves |
|---|---|---|
| Claude Code / CodeBuddy CLI | `claude mcp list` / `codebuddy mcp list` | Observe Connected; not proof this chat's tool list reloaded |
| Cursor CLI | `agent mcp list`, `agent mcp list-tools jolink-runtime` | ready and the actual tool list |
| Gemini CLI | `gemini mcp list`, `gemini skills list` | Connection and Skill discovery, checked separately |
| OpenCode | `opencode mcp list`, `opencode debug skill` | Connection and Skill discovery, checked separately |
| GitHub Copilot CLI | `copilot mcp get jolink-runtime`; `copilot skill list`, `copilot instruction list` | MCP configuration only (Enabled is not a handshake); the latter two check Skill/rule discovery |
| Cline CLI | `cline config --json` | Check MCP/Skill/rule recognition; not proof of a handshake or model call |
| Kiro CLI | `kiro-cli mcp status --name jolink-runtime` | Requires client login first; hand off a login requirement, do not bypass it |

Do not install another CLI just to verify a GUI client. Its native MCP status/tool
list is also evidence. Logs must explicitly show connection/tool discovery success;
“process started” alone does not prove a connection.

## 7. Verify through the loaded client

This is verification of an existing installation, not another installation run.
Use the current client's supported tool/Skill discovery or loading facilities.
If either remains unavailable, report the missing item and stop. Do not repeat
steps 1–5 just because the conversation still lacks the tools, Skill or rule.

1. Inspect the joLink tools actually exposed by the client; do not assume the
   installed release has a newer interface.
2. Call the exposed status tool once: `java_status(action="status")`, or
   `java_runtime(action="status")` only if that older interface is exposed.
   Do not launch or stop a business application for this check.
3. Confirm `jolink-java` appears in the client's Skill list or can be loaded
   through the client. File existence alone is not this check.
4. If enabled and the client exposes an instructions-loading query/diagnostic tool,
   use it to check the joLink rule. File existence or repeating its text is not
   proof of automatic loading. If no such query is available, report the file
   saved and loading unverified; do not invent a tool or run business code to prove it.

Report MCP, Skill and global-rule results separately. Observing rule loading does
not prove the model will consistently follow it; that is checked during normal
authorized development, not by running business tests during installation.
Diagnose a concrete client connection error if one is reported; an unchanged conversation tool list alone
is not evidence of a broken installation.

## If a step fails

- **Untrusted working directory:** clients such as Gemini disable MCP even with
  correct user-level configuration. Have the user trust their intended project
  through the normal client flow. Do not disable global trust protection or trust
  parent directories/whole disks by default. Reload and recheck; no reinstall.
- **Client cannot find uvx:** reconnect after uv installation and check the client's
  PATH. Investigate executable discovery only if that specific error persists.
- **First start is downloading:** allow the initial PyPI/Python download to finish.
  If the host times out, inspect its MCP startup log and reconnect. Do not submit
  duplicate installs or delete unrelated caches.
- **Network/proxy/certificate error:** use the user's approved settings; report the
  failed download if unresolved. Do not disable TLS verification.
- **Skill URL returns an error or HTML:** resolve the Skill link from the actual
  document/ref you are reading, or request the file. Do not save the error page as a Skill.
- **Configuration location differs:** use the read-only discovery command in step 1
  and inspect the current profile. If the path remains unknown, ask for that exact
  missing path; do not send the entire installation back to a GUI tutorial.
- **Reinstall reports an existing server:** update its active entry, do not keep
  invoking `mcp add` or add a second server name. Inspect same-name project overrides
  and plugin-managed entries; preserve unrelated settings.
- **Old tools/Skill after an upgrade:** restart only joLink through the client's
  controls and open a new chat. `@latest` updates the package when that process
  starts, not the separately copied Skill/rule; update their existing files too.
- **Downloads stall in China:** distinguish PyPI/Python downloads from Worker
  JDK/Eclipse downloads and GitHub instruction files. `JOLINK_DOWNLOAD_MIRROR=cn`
  only affects the Worker downloads. Use an approved proxy/index for the actual
  failing host, or user-provided Skill/rule files; do not disable TLS or try random mirrors.
- **No user-global entry in this client/version:** finish the supported components,
  list the missing component and UI action. Do not silently switch to project scope.

Procedures were checked against official references and exercised in isolated
macOS homes on 2026-10-19. Codex completed a native tool call; host connection/tool
discovery was observed in Claude Code, CodeBuddy CLI/IDE, Cursor CLI, Gemini,
OpenCode, VS Code and Kiro IDE. Skill and rule discovery are separate checks:
VS Code's native lists recognized both, which is not proof for other clients.
Copilot CLI and Cline CLI recognized configuration, Skill and rules; model calls
in Cline, Roo and Kiro CLI still have login/initialization handoffs. Cursor IDE and
Devin Desktop connections remain unverified; their CLI/config files are not a substitute.
These are not complete conversational tests
of every client, nor Windows/Linux runtime results.
