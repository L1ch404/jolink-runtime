# Install joLink MCP and its Agent Skill

[简体中文](INSTALL.zh-CN.md) · [Overview](README.md)

Configure joLink MCP and install the English `jolink-java` Skill for the user's
current coding agent. Default to user scope and preserve existing configuration.
Complete the steps you can perform; ask only when the target client or an action
requiring the user's participation is unclear. Do not launch a business application
or run its tests as part of installation.

## 1. Select the client and destination

Identify the client, its surface (CLI / IDE / editor extension), OS and active
profile. If this is already clear from the session, do not ask again. If it is
unclear, ask which client the user wants configured.

Use **only the matching row** below. `~` means the user's home directory; in
PowerShell use `$HOME`. Perform installation in the environment running that
client, not an unrelated remote shell or temporary cloud container.

| Client | MCP user-scope installation | English Skill destination | Template / official reference |
|---|---|---|---|
| Codex desktop / CLI / IDE extension | `codex mcp add jolink-runtime --env JOLINK_DOWNLOAD_MIRROR=official --env JOLINK_JDT_GC_AFTER_BUILD=1 -- uvx jolink-runtime@latest`; or `[mcp_servers.jolink-runtime]` in the active `config.toml`, normally `~/.codex/config.toml` (respect `CODEX_HOME`) | `~/.agents/skills/jolink-java/SKILL.md`; reuse an existing recognized installation instead of adding a duplicate | B · [MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) · [Skills](https://learn.chatgpt.com/docs/build-skills) |
| Claude Code | `claude mcp add --transport stdio --scope user jolink-runtime --env JOLINK_DOWNLOAD_MIRROR=official --env JOLINK_JDT_GC_AFTER_BUILD=1 -- uvx jolink-runtime@latest`; user config is `~/.claude.json`, not `~/.claude/.mcp.json` | `~/.claude/skills/jolink-java/SKILL.md` | A · [MCP](https://code.claude.com/docs/en/mcp) · [Skills](https://code.claude.com/docs/en/skills) |
| Cursor IDE / CLI | `mcpServers` in `~/.cursor/mcp.json` | `~/.cursor/skills/jolink-java/SKILL.md`; `.agents/skills` is another supported location | A · [MCP](https://cursor.com/docs/mcp) · [Skills](https://cursor.com/docs/skills) |
| VS Code / GitHub Copilot | Run **MCP: Open User Configuration** for the active profile, then merge into `servers` | `~/.copilot/skills/jolink-java/SKILL.md`; `~/.agents/skills` is also supported | C · [MCP](https://code.visualstudio.com/docs/agent-customization/mcp-servers) · [Skills](https://code.visualstudio.com/docs/agent-customization/agent-skills) |
| GitHub Copilot CLI | `copilot mcp add jolink-runtime --env JOLINK_DOWNLOAD_MIRROR=official --env JOLINK_JDT_GC_AFTER_BUILD=1 -- uvx jolink-runtime@latest`; or `mcpServers` in `~/.copilot/mcp-config.json`. Enable this server's tools (`tools: ["*"]`), not automatic approvals | `~/.copilot/skills/jolink-java/SKILL.md` | A · [Configuration](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers) |
| CodeBuddy Code CLI | Use `codebuddy mcp` management; current directory reference lists `~/.codebuddy/mcp.json`. Check the actual configured path instead of assuming the IDE shares it | `~/.codebuddy/skills/jolink-java/SKILL.md` | A · [MCP](https://www.codebuddy.cn/docs/cli/mcp) · [Directories](https://www.codebuddy.cn/docs/cli/codebuddy-dir) |
| CodeBuddy IDE / editor plugin | Open the selected product's **MCP** settings and its user configuration; merge the local server there. Do not write CLI configuration on the assumption it configures the IDE | User-level `jolink-java` under the Skill directory shown by that product; the IDE documents `.codebuddy/skills` user/project support | A · [IDE MCP](https://www.codebuddy.cn/docs/ide/User-guide/MCP) · [IDE Skills](https://www.codebuddy.cn/docs/ide/Features/Skills) · [Editor plugin docs](https://www.codebuddy.cn/docs/plugin/) |
| Gemini CLI | `mcpServers` in `~/.gemini/settings.json`, or `gemini mcp add` at user scope | `~/.gemini/skills/jolink-java/SKILL.md`; avoid a conflicting copy in the higher-priority `.agents/skills` alias | A · [MCP](https://geminicli.com/docs/tools/mcp-server/) · [Skills](https://geminicli.com/docs/cli/skills/) |
| OpenCode | `mcp` in the active user `opencode.json`/`opencode.jsonc`, normally under `~/.config/opencode/` | `~/.config/opencode/skills/jolink-java/SKILL.md` | D · [Config](https://opencode.ai/docs/config/) · [MCP](https://opencode.ai/docs/mcp-servers/) · [Skills](https://opencode.ai/docs/skills/) |
| Cline | IDE: **MCP Servers → Configure → Configure MCP Servers** opens the actual extension JSON; CLI: `~/.cline/mcp.json` | `~/.cline/skills/jolink-java/SKILL.md` | A · [MCP](https://docs.cline.bot/mcp/mcp-overview) · [Skills](https://docs.cline.bot/customization/skills) |
| Roo Code | **MCP → Edit Global MCP** opens its `mcp_settings.json`; project scope is `.roo/mcp.json` | `~/.roo/skills/jolink-java/SKILL.md` | A · [MCP](https://docs.roocode.com/features/mcp/using-mcp-in-roo) · [Skills](https://docs.roocode.com/features/skills) |
| Windsurf / Cascade | `mcpServers` in `~/.codeium/windsurf/mcp_config.json` | `~/.codeium/windsurf/skills/jolink-java/SKILL.md` | A · [MCP](https://docs.windsurf.com/windsurf/cascade/mcp) · [Skills](https://docs.windsurf.com/windsurf/cascade/skills) |

If the client is not listed, use its official local stdio MCP and Skill instructions
with the same uvx command and Skill. Different client names do not require a
different joLink implementation. If the client lacks Skill support, configure MCP
and explain that the Skill could not be registered.

## 2. Make uvx available

Run:

```sh
uvx --version
```

If it works, continue to step 3. Otherwise install uv using the appropriate
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

## 3. Register the MCP server

The **client**, not a separate terminal, starts this command:

```text
uvx jolink-runtime@latest
```

`@latest` requests the latest published version and refreshes uv's resolution cache
when the server is started; it does not update a running process. Downloaded packages
are cached. See [uv's version selection](https://docs.astral.sh/uv/concepts/tools/#tool-versions).

Apply the server environment shown in the templates:

- `JOLINK_DOWNLOAD_MIRROR=official`: use official Worker JDK/Eclipse download sources.
- `JOLINK_JDT_GC_AFTER_BUILD=1`: request one compiler-Worker GC after a full or
  incremental build, including a build that returns error diagnostics. No-change
  reuse does not request GC; a multi-module build requests it once for the whole round.
  This does not GC the application/test JVM. Set `0` to turn it off.

Set these on the **joLink MCP entry**, not the system-wide shell environment. Merge
with existing environment settings. If using a native CLI command, pass both `--env`
options shown in the table or edit the resulting server entry. The download setting
does not change uv/Python/PyPI or Skill download sources. For a user in China, use
`cn` instead of `official`; see [mirror settings](docs/runtime-download-mirror.zh-CN.md).

Read the selected client's existing configuration. Add or update only the joLink
entry, preserving other servers and settings. Back up a file before editing it.
If joLink already comes from a plugin or another active configuration, update that
entry rather than adding a second server. Preserve an explicitly user-selected
version unless an upgrade is requested.

Use either the native command in the table, if that CLI is already installed,
or the matching configuration below. Do not install an extra client CLI just to
edit its configuration. Merge the joLink entry; do not replace the whole file.

### A. JSON with `mcpServers`

Merge this entry into the existing object; do not replace the entire file.
If a host's documented schema omits `type`, retain `command` and `args` in its
native local-server shape. Do not create a remote URL or OAuth configuration.

```json
{
  "mcpServers": {
    "jolink-runtime": {
      "type": "stdio",
      "command": "uvx",
      "args": ["jolink-runtime@latest"],
      "env": {
        "JOLINK_DOWNLOAD_MIRROR": "official",
        "JOLINK_JDT_GC_AFTER_BUILD": "1"
      }
    }
  }
}
```

### B. Codex TOML

```toml
[mcp_servers.jolink-runtime]
command = "uvx"
args = ["jolink-runtime@latest"]

[mcp_servers.jolink-runtime.env]
JOLINK_DOWNLOAD_MIRROR = "official"
JOLINK_JDT_GC_AFTER_BUILD = "1"
```

### C. VS Code profile JSON/JSONC with `servers`

```json
{
  "servers": {
    "jolink-runtime": {
      "type": "stdio",
      "command": "uvx",
      "args": ["jolink-runtime@latest"],
      "env": {
        "JOLINK_DOWNLOAD_MIRROR": "official",
        "JOLINK_JDT_GC_AFTER_BUILD": "1"
      }
    }
  }
}
```

### D. OpenCode: command is an array, type is `local`

```json
{
  "mcp": {
    "jolink-runtime": {
      "type": "local",
      "command": ["uvx", "jolink-runtime@latest"],
      "environment": {
        "JOLINK_DOWNLOAD_MIRROR": "official",
        "JOLINK_JDT_GC_AFTER_BUILD": "1"
      },
      "enabled": true
    }
  }
}
```

Keep `command` and `args` separate, except OpenCode's command array.
If environment overrides are needed, OpenCode uses `environment`; the other
templates use `env` (a nested table for TOML). Preserve existing proxy/certificate
settings and tool approvals. No joLink account, API key or remote URL is needed.

## 4. Install the English Skill

Read [skills/jolink-java/SKILL.md](skills/jolink-java/SKILL.md), then copy it as
UTF-8 to the **full Skill destination in the selected row**. Create its parent
directory if necessary. Install the English file regardless of this guide's language.

- From a local checkout, use that checkout's `skills/jolink-java/SKILL.md`.
- From a GitHub document, follow the linked Skill in the same branch/ref and use
  its **Raw** content, not the HTML page. The default-branch source is
  [the raw English Skill](https://raw.githubusercontent.com/L1ch404/jolink-runtime/main/skills/jolink-java/SKILL.md).
  Do not use the PyPI package version as a guessed Git branch/tag.
- If the same Skill already exists, update that copy rather than installing it
  into every recognized directory. Preserve user edits or confirm before replacing
  a customized copy.

Verify that the file contains the `name: jolink-java` frontmatter and the actual
instructions. Enable the Skill in the client's UI if it was disabled. Do not install
a second Chinese Skill or add global rules: this Skill is the usage guidance.

## 5. Reconnect and verify

Reload joLink in the client's MCP settings, then:

1. Confirm the server connects and the client lists its tools. Inspect the actual
   tool schemas; don't assume the installed release has a newer interface.
2. Call the available status tool once: `java_status(action="status")`, or
   `java_runtime(action="status")` when the server exposes that older interface.
   Do not launch or stop a business application for this check.
3. Confirm `jolink-java` appears in the client's Skill list or can be loaded there.

If this conversation cannot refresh its tool/Skill list, finish writing the files
and ask the user to reconnect or start a new conversation in that client. Do not
kill the user's agent process. A native `mcp list` command showing saved settings
proves registration, not a completed server connection.

Finish with a short report: which client was configured, the Skill location,
whether MCP and Skill loading were verified, and any single remaining user action.
Do not report “connected” when only the configuration was written.

## If a step fails

- **Client cannot find uvx:** reconnect after uv installation and check the client's
  PATH. Investigate executable discovery only if that specific error persists.
- **First start is downloading:** allow the initial PyPI/Python download to finish.
  If the host times out, inspect its MCP startup log and reconnect. Do not submit
  duplicate installs or delete unrelated caches.
- **Network/proxy/certificate error:** use the user's approved settings; report the
  failed download if unresolved. Do not disable TLS verification.
- **Skill URL returns an error or HTML:** resolve the Skill link from the actual
  document/ref you are reading, or request the file. Do not save the error page as a Skill.
- **Configuration location differs:** use the linked official documentation or the
  current client's “open configuration” operation, then continue with the same
  command and Skill.

Client references checked against official documentation on 2026-09-20.
