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
| Kiro IDE / CLI | **Kiro: Open user MCP config (JSON)**, normally `~/.kiro/settings/mcp.json`, merge `mcpServers`; retain existing approvals | `~/.kiro/skills/jolink-java/SKILL.md`; a custom agent must include the Skill in its resources | A · [MCP](https://kiro.dev/docs/mcp/) · [Skills](https://kiro.dev/docs/skills/) |

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
instructions. If enabling it requires user interaction, include that in the
handoff below. Do not install a second Chinese Skill. Install the optional global
rule separately in step 5; it is not part of the Skill registration.

## 5. Install the global Java verification rule when requested

Read [rules/jolink-java-verification.md](rules/jolink-java-verification.md) and install
its **full English body**, including the begin/end markers. Use the same checkout or
Git branch/ref as this guide, just as for the Skill. The default-branch source is
[the raw rule](https://raw.githubusercontent.com/L1ch404/jolink-runtime/main/rules/jolink-java-verification.md).
Do not install an HTML/error page, a URL-only reference, or a copy of the entire Skill.

Choose **one effective user-level entry for this client** below. The short rule
should load automatically across projects; its body limits behavior to Java
implementation tasks. Do not restrict activation to `**/*.java`, require an
explicit mention, or leave activation to the agent's relevance decision.

| Client / surface | Global destination or native UI | Loading / official reference |
|---|---|---|
| Codex desktop / CLI / IDE extension | Append to `~/.codex/AGENTS.md` (respect `CODEX_HOME`). If a non-empty `AGENTS.override.md` is already active there, use that file instead | Do not create an override file that hides the user's existing instructions. New session required. [Instructions](https://learn.chatgpt.com/docs/agent-configuration/agents-md) |
| Claude Code | `~/.claude/rules/jolink-java-verification.md` | No `paths` frontmatter; user-level rules apply across projects. [Memory](https://code.claude.com/docs/en/memory#user-level-rules) |
| Cursor IDE | **Customize → Rules → User Rules**; add the marked body to the existing user rules | Applies to Agent Chat, not Tab/Inline Edit. Use the UI, not an invented global file or internal database. [Rules](https://cursor.com/docs/rules#user-rules) |
| Cursor CLI | Inspect the active CLI's supported global rule settings using its official documentation | Do not assume IDE User Rules are loaded by the CLI, or silently substitute a project rule. If not confirmed, leave this part pending. [CLI rules](https://cursor.com/docs/cli/using) |
| VS Code / GitHub Copilot | Copilot Agent Host: `~/.copilot/instructions/jolink-java-verification.instructions.md`. Local Agent: **Chat: New Instructions File → User** in the active profile, then use that created file | Add frontmatter A below. The Local Agent's user files are in profile storage, not necessarily `~/.copilot`. [Instructions](https://code.visualstudio.com/docs/agent-customization/custom-instructions) |
| GitHub Copilot CLI | `~/.copilot/instructions/jolink-java-verification.instructions.md` (respect `COPILOT_HOME`) | Add frontmatter A; `/instructions` shows discovered files and toggles. [Instructions](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions) |
| CodeBuddy Code CLI | `~/.codebuddy/rules/jolink-java-verification.md` | Add frontmatter B. CLI configuration is not proof of IDE/plugin installation. [Memory](https://www.codebuddy.cn/docs/cli/memory) |
| CodeBuddy IDE | **Settings / Rules → Create rule → User rule**; name it `jolink-java-verification`, paste the marked body | Select **Always / 总是**, not Agent Request or Manual. Start a new chat. [Rules](https://www.codebuddy.cn/docs/ide/User-guide/Rules) |
| CodeBuddy editor plugin | Check that plugin version's own rule settings for a **user/global** rule and automatic loading | Do not assume the IDE/CLI entry configures the plugin. If only project rules are available, report global-rule setup pending; do not write into the business repo without permission. [Plugin docs](https://www.codebuddy.cn/docs/plugin/) |
| Gemini CLI | Append to `~/.gemini/GEMINI.md`; respect a configured context filename | User-level context applies across projects; inspect with `/memory show` after reloading. [Context](https://geminicli.com/docs/cli/gemini-md/) |
| OpenCode | Append to the active global `AGENTS.md`, normally `~/.config/opencode/AGENTS.md` | If using the Claude global fallback, retain that guidance: add a separate rule through the global config's `instructions` list instead of shadowing the fallback. [Rules](https://opencode.ai/docs/rules/) |
| Cline | Add `jolink-java-verification.md` in the active **Global Rules** directory, normally `~/Documents/Cline/Rules/`; on Windows use the actual Documents directory | Enable the rule; omit path conditions. Resolve redirected Documents/OneDrive via the Rules panel; do not duplicate into fallback directories. [Rules](https://docs.cline.bot/customization/cline-rules) |
| Roo Code | `~/.roo/rules/jolink-java-verification.md` | Use general global rules, not a single-mode directory. [Instructions](https://docs.roocode.com/features/custom-instructions) |
| Windsurf / Cascade | Append to `~/.codeium/windsurf/memories/global_rules.md` | Always loaded; the entire file has a 6,000-character limit. If it will exceed that limit, ask the user to make room rather than deleting their rules. [Memories](https://docs.windsurf.com/windsurf/cascade/memories) |
| Kiro IDE / CLI | `~/.kiro/steering/jolink-java-verification.md` | Add frontmatter C. For a custom agent, include this file in its `resources`, preserving existing entries. [Steering](https://kiro.dev/docs/steering/) |

**Frontmatter A — Copilot instructions.** Prepend to the rule body:

```yaml
---
applyTo: "**"
---
```

**Frontmatter B — CodeBuddy CLI.** Prepend to the rule body:

```yaml
---
enabled: true
alwaysApply: true
---
```

**Frontmatter C — Kiro steering.** Prepend to the rule body:

```yaml
---
inclusion: always
---
```

For other clients, use their current official user-level instructions entry and
automatic-loading setting. Do not guess paths from another product. If the entry
needs UI interaction, provide the rule body and exact steps, then report it pending
until completed. An unavailable global-rule entry does not prevent MCP/Skill setup.

Before writing, read the destination and back it up. In a shared file, add or update
only the block between `<!-- jolink-java-verification:begin -->` and
`<!-- jolink-java-verification:end -->`; preserve all text outside it. In a dedicated
file, manage only joLink's rule and the required native frontmatter. Reinstallation
must not append duplicates. Preserve user customizations or ask before overwriting
them; do not remove conflicting user rules, replace system prompts or alter approvals.
Some clients load another client's compatible files: reuse an already effective
joLink block rather than installing it in multiple recognized locations.

To disable/uninstall on user request, disable the joLink rule in the client's UI,
or remove only its marked block/dedicated file. Leave MCP, Skill and unrelated
instructions alone unless their removal is also requested.

## 6. Check configuration and hand off if reloading is needed

Check the results of steps 1–5: uvx is available, the selected client's saved
configuration contains the joLink entry with its environment settings, unrelated
configuration is preserved, and the English Skill file is in the selected location.
If requested, also check the global rule's destination, preserved surrounding text
and automatic-loading setting. Report any remaining UI step separately.
These checks establish **configuration complete**, not a connected MCP server or
a Skill/rule loaded by the client. Package downloads may still happen on first start.

If the client has already loaded the new tools, Skill and requested rule into this conversation,
continue to step 7. Otherwise, **stop here** and tell the user the required client
action: reconnect MCP or restart the client as appropriate, then start a new chat.
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
4. If enabled, inspect the client's native active-instructions/rules view or loading
   diagnostics for the joLink block. File existence or the model merely repeating
   the rule is not proof of automatic loading. If the client exposes no such evidence,
   report loading unverified; do not modify business code just to demonstrate it.

Report MCP, Skill and global-rule results separately. Observing rule loading does
not prove the model will consistently follow it; that is checked during normal
authorized development, not by running business tests during installation.
Diagnose a concrete client connection error if one is reported; an unchanged conversation tool list alone
is not evidence of a broken installation.

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

MCP/Skill references were checked on 2026-09-20; global-rule references and Kiro
entries on 2026-09-25. These are documented integration routes, not a claim of
end-to-end installation tests on every client/version.
