# 安装 joLink MCP、Skill 与 Java 验证准则

[English](INSTALL.md) · [项目介绍](README.zh-CN.md)

为用户当前使用的编程 Agent 配置 joLink MCP，并安装英文 `jolink-java` Skill。
用户要求时，在第 5 步启用全局 Java 自动验证准则。README 的安装提示词已明确要求
启用，不重复询问。如果用户只要求安装 MCP/Skill，则询问一次是否启用；不同意不影响
安装。用户明确不启用时直接跳过，不再追问。
默认用户级安装，保留已有配置。直接完成你能执行的步骤；只有不清楚目标客户端，
或确实需要用户操作时再询问。本次安装不启动业务应用、不运行业务测试。

**配置安装与客户端验证是两个阶段。** 当前会话完成 MCP 配置、Skill 和用户要求的
规则的写入、检查即可。如果客户端尚未加载它们，在第 6 步交给用户并停止，这是配置
完成后的正常交接，不是安装失败。第 7 步只在客户端已加载工具和指令的会话中执行。

本文是一份可以直接执行的安装指南，不是让安装 Agent 临时查资料的链接清单。
已列出的客户端按本文步骤和模板操作；官网链接作为出处，仅在实际版本或界面不符时再查。
只读公共步骤和**当前客户端的一行配置**。本文仅依赖终端和文件读写工具，
不把 GUI 自动化作为安装前提。

本文为**已经安装的 Agent**接入 joLink，不负责替用户注册客户端账户或配置模型密钥。
全新客户端若仍停在登录／模型设置页，可以完成已知文件的安装；缺少的客户端初始化
交给用户，不复制其他客户端的凭据，也不把“尚未登录”当成 joLink 安装失败。

## 0. 先准备两份指令文件

安装目标有三项：MCP 配置、英文 Skill、用户要求的全局规则。安装 PyPI 包不会自动安装后两项。
先把两份文件下载到临时目录，读取确认后再写客户端配置。不需要克隆仓库。
已有本地源码目录时直接使用其中的文件；若本文来自非 `main` 分支/ref，将下方 ref 改为
相同值，不要根据 PyPI 版本猜 Git 标签。

macOS/Linux（在同一 shell 中执行）：

```sh
jolink_ref=main
jolink_stage="$(mktemp -d "${TMPDIR:-/tmp}/jolink-install.XXXXXX")" || exit 1
jolink_raw="https://raw.githubusercontent.com/L1ch404/jolink-runtime/$jolink_ref"
curl --fail --location --show-error --retry 2 "$jolink_raw/skills/jolink-java/SKILL.md" --output "$jolink_stage/SKILL.md" &&
curl --fail --location --show-error --retry 2 "$jolink_raw/rules/jolink-java-verification.md" --output "$jolink_stage/jolink-java-verification.md" &&
printf '%s\n' "$jolink_stage"
```

Windows PowerShell（在同一会话中执行）：

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

以 UTF-8 读取两份文件。Skill 应以含有 `name: jolink-java` 的 YAML frontmatter 开头；
规则应包含 `jolink-java-verification:begin` 与 `:end` 的 HTML 注释标记。
Skill 保留英文，规则保留原文，不翻译、不概括。下载失败或拿到 HTML 时，保留原安装，
报告具体地址／错误；不要把下载响应直接覆盖到活动配置。后续第 4–5 步复用这份临时文件。
如果终端工具每次启动新 shell，记住实际的临时目录绝对路径，不依赖上次的 shell 变量。

### 仅在 Raw GitHub 不可达时使用

GitHub 官方 [Contents API](https://docs.github.com/en/rest/repos/contents#get-repository-content)
可以返回同样的原始文件，不是第三方镜像。只在第一种下载方式失败时改用下面一组，
ref 与本文保持一致，公开文件不需要 Token。两种方式都失败时，使用用户批准的代理或
用户提供的文件，不继续搜索其他下载站；包／Worker 的镜像设置不解决此处下载问题。

macOS/Linux：

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


## 1. 确定当前客户端的文件位置

根据当前会话确认客户端及形态（CLI、桌面端、编辑器插件），不明确才询问一次。
命令在客户端实际创建 MCP 进程的开发机上执行，不在无关 SSH／容器中安装。

只使用匹配的一行。表中是用户级默认位置；写入前展开真实用户目录和配置覆盖路径，
复用已生效的安装。格式名是第 3 步合并命令的参数，不是新增的 joLink 参数。

| 客户端 | MCP 配置／操作方式 | 格式 | 英文 Skill 文件 | 依据 |
|---|---|---|---|---|
| Codex 桌面端 / CLI / IDE 插件 | `$CODEX_HOME/config.toml`，否则 `~/.codex/config.toml`；使用下方原生命令或 TOML 命令 | `codex` | `~/.agents/skills/jolink-java/SKILL.md` | [MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)、[Skills](https://learn.chatgpt.com/docs/build-skills) |
| Claude Code | `~/.claude.json` 的顶层服务配置，不是项目 `.mcp.json` | `stdio` | `~/.claude/skills/jolink-java/SKILL.md` | [MCP](https://code.claude.com/docs/en/mcp)、[Skills](https://code.claude.com/docs/en/skills) |
| Cursor IDE / CLI | `~/.cursor/mcp.json` | `basic` | `~/.cursor/skills/jolink-java/SKILL.md` | [MCP](https://cursor.com/docs/mcp)、[Skills](https://cursor.com/docs/skills) |
| VS Code / GitHub Copilot | Local harness：活动 profile 的 `mcp.json`，新条目可用第 3.1 节 `code --add-mcp`；Copilot harness：`$COPILOT_HOME/mcp-config.json`，否则 `~/.copilot/mcp-config.json`。只配置当前使用的 harness | Local 用 `vscode`；Copilot 用 `copilot` | `~/.copilot/skills/jolink-java/SKILL.md` | [MCP](https://code.visualstudio.com/docs/agent-customization/mcp-servers)、[Skills](https://code.visualstudio.com/docs/agent-customization/agent-skills) |
| GitHub Copilot CLI | `$COPILOT_HOME/mcp-config.json`，否则 `~/.copilot/mcp-config.json` | `copilot` | `~/.copilot/skills/jolink-java/SKILL.md` | [配置](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers) |
| CodeBuddy Code CLI | 依次使用第一个已存在文件：`~/.codebuddy/.mcp.json` → `~/.codebuddy/mcp.json` → `~/.codebuddy.json`；都不存在才创建第一个。配置目录尊重 `CODEBUDDY_CONFIG_DIR` | `stdio` | `~/.codebuddy/skills/jolink-java/SKILL.md` | [MCP 与优先级](https://www.codebuddy.cn/docs/cli/mcp) |
| CodeBuddy IDE / 编辑器插件 | CodeBuddy CN IDE 4.12 实测为 `~/.codebuddy/mcp.json`（不是 CLI 默认的 `.mcp.json`）；其他版本／插件复用已有文件，不明确时用下方命令定位 | `stdio` | IDE：`~/.codebuddy/skills/jolink-java/SKILL.md`；插件复用已确认的用户 Skill 目录 | [IDE MCP](https://www.codebuddy.cn/docs/ide/User-guide/MCP)、[IDE 作用域](https://www.codebuddy.cn/docs/ide/User-guide/Overview) |
| Gemini CLI | `~/.gemini/settings.json` | `gemini` | `~/.gemini/skills/jolink-java/SKILL.md` | [MCP](https://geminicli.com/docs/tools/mcp-server/)、[Skills](https://geminicli.com/docs/cli/skills/) |
| OpenCode | 活动用户配置目录内的 `opencode.json` / `opencode.jsonc`，通常是 `~/.config/opencode/`；尊重指定的配置覆盖 | `opencode` | `~/.config/opencode/skills/jolink-java/SKILL.md` | [配置](https://opencode.ai/docs/config/)、[MCP](https://opencode.ai/docs/mcp-servers/) |
| Cline | CLI 与新版扩展（4.1 实测）：`~/.cline/data/settings/cline_mcp_settings.json`，尊重 `CLINE_MCP_SETTINGS_PATH`；旧扩展已有的文件在编辑器 `User/globalStorage` 下，用下方命令定位并原地更新，不另外创建一份 | `basic` | `~/.cline/skills/jolink-java/SKILL.md` | [MCP](https://docs.cline.bot/mcp/mcp-overview)、[Skills](https://docs.cline.bot/customization/skills) |
| Roo Code | 用下方命令定位编辑器插件的 `mcp_settings.json`；用户级安装不创建项目 `.roo/mcp.json` | `basic` | `~/.roo/skills/jolink-java/SKILL.md` | [MCP](https://docs.roocode.com/features/mcp/using-mcp-in-roo)、[Skills](https://docs.roocode.com/features/skills) |
| Windsurf / Cascade | 旧版：`~/.codeium/windsurf/mcp_config.json`；新版 Devin Desktop Cascade：`$XDG_CONFIG_HOME/devin/mcp_config.json` 或 `~/.config/devin/mcp_config.json`，Windows 为 `%APPDATA%\\devin\\mcp_config.json`。使用已安装产品的有效位置，不写所有位置 | `basic` | `~/.codeium/windsurf/skills/jolink-java/SKILL.md`；已有生效的 `~/.config/devin/skills/jolink-java/SKILL.md` 则复用 | [MCP](https://docs.windsurf.com/windsurf/cascade/mcp)、[Skills](https://docs.windsurf.com/windsurf/cascade/skills) |
| Kiro IDE / CLI | `~/.kiro/settings/mcp.json` | `basic` | `~/.kiro/skills/jolink-java/SKILL.md` | [MCP](https://kiro.dev/docs/mcp/configuration/)、[Skills](https://kiro.dev/docs/skills/) |

不要新建高优先级文件遮蔽 CodeBuddy 的已有配置。用户级修改被覆盖时，检查同名项目／
插件条目。部分客户端还会识别 `.agents/skills` 或另一客户端的兼容 Skill 目录；
更新已生效的副本，不重复安装。

### 用终端定位编辑器管理的配置

仅用于准确路径未知的编辑器／profile。第 2 步使 uv 可用后，用 Agent 的文件工具把
下面代码保存为临时目录内的 `find_mcp.py`。它只列文件名，不读取秘密、不写配置，
也不会任意选中第一个结果。

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

参数使用当前编辑器实际的用户数据目录名，例如 `Code`、`Code - Insiders`、
`Cursor`、`CodeBuddy`、`Windsurf`。以下是 CodeBuddy 的例子：

```sh
uv run --no-project --python 3.11 python "$jolink_stage/find_mcp.py" CodeBuddy
```

```powershell
uv run --no-project --python 3.11 python (Join-Path $JolinkStage "find_mcp.py") CodeBuddy
```

读取结果中属于当前客户端／profile 的配置再选择。多个结果要确认活动 profile；
没有结果说明路径尚未确定。指定了自定义 user-data 目录时，将其 `User` 目录的绝对路径
作为参数传入同一命令。此命令只搜索所选编辑器目录；不会代替表中 Cline、CodeBuddy 等
位于 home 下的固定路径检查，不要因为搜索结果为空就反复安装。
全新闭源 IDE／插件若既无已记录的配置路径、也无 CLI，明确报告需要用户提供配置文件路径。
不猜路径，不改内部数据库，不安装另一个客户端，也不偷偷切换项目级安装。

固定路径客户端没有配置文件属于正常情况，合并命令可以创建。
未列出的客户端先确认其本地 stdio 配置格式，再使用相同命令和指令文件。

## 2. 确认 uvx 可用

**不要求预装 Python 或 pip。** 使用 uv 的独立安装器，不执行 `pip install uv`。
后续命令所需 Python 由 uv 提供；默认支持[自动下载缺少的 Python](https://docs.astral.sh/uv/guides/install-python/#automatic-python-downloads)，
不需要替换用户已有的 Python。

执行：

```sh
uv --version
uvx --version
```

两条都能执行就进行下方 Python 检查；否则按当前系统使用 [uv 官方安装命令](https://docs.astral.sh/uv/getting-started/installation/)。

macOS/Linux：

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

安装后确认 `uvx --version` 能执行。uvx 会从 PyPI 下载并缓存 joLink，准备 Python
运行环境；不需要另外全局安装 joLink、克隆仓库或查找 joLink 可执行文件。

修改客户端文件前，确认 uv 可以运行安装辅助命令所需的解释器：

```console
uv run --no-project --python 3.11 python --version
```

没有 Python 3.11 时，uv 自动下载并管理。该命令不全局安装系统 Python，不修改用户
默认解释器，也不要求管理员权限。网络策略或 `UV_PYTHON_DOWNLOADS=never` 禁止下载时，
报告这个具体阻断，不反复重试，不擅自改变策略。

刚安装 uv 后本 shell 找不到 uvx 时，直接使用安装器报告的可执行路径，
或只将该目录加入本次 shell 的 PATH，不要求用户先打开新终端才能继续安装。
GUI 客户端若明确报 `uvx not found`，用 `command -v uvx`（macOS/Linux）或
`(Get-Command uvx -CommandType Application).Source`（PowerShell）取得真实可执行路径，
再把该绝对路径作为 `command`，参数仍分开。路径含空格时仍是一个 JSON 字符串，
不额外嵌套 shell 引号；不要仅因为是 Windows 就加 `cmd /c` 包装。

## 3. 用命令注册 MCP

由客户端启动下面这条命令；不要在终端直接运行它来“证明”当前 Agent 已连接：

```text
uvx jolink-runtime@latest
```

`@latest` 在进程启动时解析最新发布包，不替换运行中的进程，也不更新单独安装的
Skill／规则。下列默认值写入 MCP 条目，不修改系统全局环境：

- `JOLINK_DOWNLOAD_MIRROR=cn`：Worker JDK/Eclipse 下载源；境外用户使用
  `official`。不影响 PyPI/Python 或 GitHub 下载。
- `JOLINK_JDT_GC_AFTER_BUILD=1`：编译后请求一次编译器 GC，无改动复用不请求；
  不对应用／测试 JVM 做 GC。

先读取所选配置。保留其他服务、代理、审批和用户明确固定的包版本。合并脚本不擅自
启用已禁用的服务、放开工具限制。用户要求升级时，才把 joLink 的包参数改为
`jolink-runtime@latest`；否则保留原固定版本。不要添加无条件自动审批参数。

注册方式**三选一**，完成后直接继续第 4 步：

- 新条目且对应 CLI 已安装：**3.1**。
- JSON 客户端，含重新安装：**3.2**。
- Codex 没有 CLI，或更新已有 TOML 并保留注释：**3.3**。

不要三个小节依次全跑，也不要照搬另一个客户端的示例路径。
先从表中确定当前客户端实际的 MCP、Skill、规则位置，后续始终使用这组位置。

### 3.1 原生 CLI 注册：仅用于新条目

已有对应 CLI 才运行**匹配的一条**。命令在 shell 和 PowerShell 都可使用，
不要为了写配置额外安装客户端 CLI。重装时用后面的合并命令，保留额外设置。

Codex：

```console
codex mcp add jolink-runtime --env JOLINK_DOWNLOAD_MIRROR=cn --env JOLINK_JDT_GC_AFTER_BUILD=1 -- uvx jolink-runtime@latest
codex mcp get jolink-runtime
```

Claude Code：

```console
claude mcp add --transport stdio --scope user jolink-runtime --env JOLINK_DOWNLOAD_MIRROR=cn --env JOLINK_JDT_GC_AFTER_BUILD=1 -- uvx jolink-runtime@latest
claude mcp get jolink-runtime
```

GitHub Copilot CLI：

```console
copilot mcp add jolink-runtime --env JOLINK_DOWNLOAD_MIRROR=cn --env JOLINK_JDT_GC_AFTER_BUILD=1 -- uvx jolink-runtime@latest
```

VS Code Local harness（macOS/Linux，新条目）：

```sh
code --add-mcp '{"name":"jolink-runtime","command":"uvx","args":["jolink-runtime@latest"],"env":{"JOLINK_DOWNLOAD_MIRROR":"cn","JOLINK_JDT_GC_AFTER_BUILD":"1"}}'
```

该命令由 VS Code 写入用户 profile。使用命名 profile／自定义 user-data 时，带上该实例
原有的 `--profile`／`--user-data-dir` 参数。Windows、已有条目或没有 `code` 命令时，
按第 1 步找到当前 profile 的 `mcp.json`，使用第 3.2 节 `vscode` 格式合并，不另装 CLI。

这些命令完成注册／查看配置，不代表当前对话的工具列表已刷新。
报“已存在”就更新该条目，不换一个服务名再注册。

### 3.2 JSON 配置：共用一个合并命令

把下面代码保存为临时目录内的 `merge_mcp.py`，只使用 Python 标准库。
uv 可以提供 Python 3.11，不需要全局安装 Python/pip，也不增加 joLink CLI。

它会备份有变化的原文件，只合入 joLink，保留已有命令参数和环境变量，输出保存后的条目。
内容相同的重复执行不会再备份。不修改审批、不启动 MCP，也不代替 Skill／规则安装。

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

从第 1 步选定配置文件和格式。以下以 **Cursor** 为例；安装 Agent 应自行使用当前客户端
对应的路径／格式，不让用户手工修改命令：

```sh
uv run --no-project --python 3.11 python "$jolink_stage/merge_mcp.py" "$HOME/.cursor/mcp.json" basic cn
```

```powershell
uv run --no-project --python 3.11 python (Join-Path $JolinkStage "merge_mcp.py") (Join-Path $HOME ".cursor/mcp.json") basic cn
```

格式：`stdio` 使用 `mcpServers` 和 `type: stdio`；
`basic` 使用 `mcpServers` 和 command/args；
`gemini` 在 `basic` 基础上默认设置 `timeout: 60000`（毫秒），保留已有自定义值；
`copilot` 使用 `mcpServers`、`type: local`、`tools: ["*"]`；
`vscode` 使用 profile 的 `servers`；
`opencode` 使用 `mcp`、命令数组和 `environment`。
Copilot 的 `tools` 表示工具可见，不代表自动批准调用。
Gemini CLI 0.64 的 `mcp list` 未配置时默认只等 5 秒，本机实测会在 joLink 握手完成前
显示 Disconnected；60 秒配置已验证连接成功。这是宿主超时，不是 Java 编译超时。

**已有 JSONC：**含注释／末尾逗号时，不使用严格 JSON 脚本整文件重写。
先对临时目录里的新文件执行合并脚本，生成所选格式的 joLink 对象，再用 Agent
文件编辑工具把该对象精准加入／更新到实际 JSONC，保留周围文本。
随后用只读 JSONC 解析器检查，不把 JSONC 转成 JSON 后整文件覆盖：

```sh
uv run --no-project --python 3.11 --with json5 python -c "import json5,sys; json5.load(open(sys.argv[1],encoding='utf-8-sig')); print('JSONC valid')" "$jolink_config"
```

```powershell
uv run --no-project --python 3.11 --with json5 python -c "import json5,sys; json5.load(open(sys.argv[1],encoding='utf-8-sig')); print('JSONC valid')" $JolinkConfig
```

执行前将 `jolink_config` / `$JolinkConfig` 设为实际选中的 JSONC 路径。
合并脚本解析 JSON 失败时不会写文件。这是文件操作，不需要图形界面。
已有自定义包装命令或远程条目，也应先读再决定如何更新；脚本故意不覆盖已有启动命令。

### 3.3 没有 Codex CLI／保留已有 TOML 配置

把下面代码保存为临时目录内的 `merge_codex.py`。`tomlkit` 仅由临时命令使用，
用于保留 TOML 注释，不安装到 joLink 的运行环境。

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
uv run --no-project --python 3.11 --with tomlkit python "$jolink_stage/merge_codex.py" "${CODEX_HOME:-$HOME/.codex}/config.toml" cn
```

```powershell
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
uv run --no-project --python 3.11 --with tomlkit python (Join-Path $JolinkStage "merge_codex.py") (Join-Path $CodexHome "config.toml") cn
```

无论用哪种方式，保存后都检查 joLink 条目。可执行文件和参数分开；Windows JSON
路径使用 `/` 或转义后的 `\\`，不嵌套 shell 引号。
只有客户端明确找不到 uvx 时，才按第 2 步换成其真实可执行文件绝对路径；不要无故加 `cmd /c`。

## 4. 用命令写入英文 Skill

使用第 0 步已检查的[英文 Skill](skills/jolink-java/SKILL.md)，目标为第 1 步的完整路径。
目录名 `jolink-java`，文件名必须是 `SKILL.md`，不是 `SKILL.md.txt`。
先读取已有副本；存在用户定制时保留定制或确认替换，不静默覆盖。

把下方共用写入代码保存为临时目录内的 `install_text.py`。Skill 按原始字节复制；
第 5 步也使用它更新规则标记段落，不替换用户其他指令。
已有文件内容有变化才备份，相同内容不写入。

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

下面以 **Cursor** 为例；其他客户端使用表中对应的 Skill 路径：

```sh
uv run --no-project --python 3.11 python "$jolink_stage/install_text.py" "$jolink_stage/SKILL.md" "$HOME/.cursor/skills/jolink-java/SKILL.md" skill
```

```powershell
uv run --no-project --python 3.11 python (Join-Path $JolinkStage "install_text.py") (Join-Path $JolinkStage "SKILL.md") (Join-Path $HOME ".cursor/skills/jolink-java/SKILL.md") skill
```

以 UTF-8 重新读取目标，确认 `name: jolink-java` 的 frontmatter 和完整正文，不翻译。
文件存在仅证明复制完成，不证明当前客户端已加载。

Kiro 自定义 Agent 若未覆盖该 Skill，向其已有 `resources` 数组追加
`skill://~/.kiro/skills/jolink-java/SKILL.md`，用精准文件编辑保留其他字段；
不替换整个 Agent 定义，不额外授予工具权限。

## 5. 用命令写入用户要求的全局规则

使用同一临时下载目录里的 [rules/jolink-java-verification.md](rules/jolink-java-verification.md)，
原文写入。用户要求时启用（README 的安装提示词已要求，不再询问）；没有要求则询问一次，
明确拒绝就跳过。

从表中选择一个目标及模式，交给第 4 步的 `install_text.py`。
它只更新 `<!-- jolink-java-verification:begin -->` 与
`<!-- jolink-java-verification:end -->` 标记范围，保留周围指令。
独立规则格式另补必要的原生 frontmatter，不替换用户系统提示词或其他规则。

| 客户端 | 用户级规则目标 | 模式 | 依据／限制 |
|---|---|---|---|
| Codex 桌面端 / CLI / IDE 插件 | `~/.codex/AGENTS.md`（尊重 `CODEX_HOME`）；若已有非空 `AGENTS.override.md` 则改它，不新建 override | `rule` | [指令](https://learn.chatgpt.com/docs/agent-configuration/agents-md) |
| Claude Code | `~/.claude/rules/jolink-java-verification.md` | `rule` | [用户规则](https://code.claude.com/docs/en/memory#user-level-rules) |
| Cursor IDE / CLI | 未确认 IDE User Rules 或 CLI 用户全局规则的终端接口，只交接此组件 | manual | [IDE 规则](https://cursor.com/docs/rules#user-rules)、[CLI 规则](https://cursor.com/docs/cli/using) |
| VS Code / GitHub Copilot | Copilot harness：`~/.copilot/instructions/jolink-java-verification.instructions.md`；Local harness 复用已定位的 profile 指令文件 | `copilot` | [指令位置](https://code.visualstudio.com/docs/agent-customization/custom-instructions) |
| GitHub Copilot CLI | `~/.copilot/instructions/jolink-java-verification.instructions.md`，尊重 `COPILOT_HOME` | `copilot` | [指令](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions) |
| CodeBuddy Code CLI | `~/.codebuddy/rules/jolink-java-verification.md`，尊重 `CODEBUDDY_CONFIG_DIR` | `codebuddy` | [规则](https://www.codebuddy.cn/docs/cli/memory) |
| CodeBuddy IDE / 编辑器插件 | 已定位用户规则文件就原地更新；否则交接全局规则设置，不假设兼容 CLI 目录 | 路径未知时 manual | [IDE 规则](https://www.codebuddy.cn/docs/ide/User-guide/Rules) |
| Gemini CLI | `~/.gemini/GEMINI.md`，或已配置的用户上下文文件名 | `rule` | [上下文](https://geminicli.com/docs/cli/gemini-md/) |
| OpenCode | 活动 `~/.config/opencode/AGENTS.md`；正在使用 Claude 回退时，另写独立规则文件，把绝对路径追加到全局 `instructions` 列表 | `rule` | [规则](https://opencode.ai/docs/rules/) |
| Cline | `~/Documents/Cline/Rules/jolink-java-verification.md`，除非已配置其他全局规则目录 | `rule` | [规则](https://docs.cline.bot/customization/cline-rules)；Windows 用 `[Environment]::GetFolderPath("MyDocuments")` 取得真实 Documents |
| Roo Code | `~/.roo/rules/jolink-java-verification.md` | `rule` | [指令](https://docs.roocode.com/features/custom-instructions) |
| Windsurf / Cascade | `~/.codeium/windsurf/memories/global_rules.md` | `rule` | [规则](https://docs.windsurf.com/windsurf/cascade/memories)；整个文件上限 6,000 字符，不为塞入新规则删除用户原规则 |
| Kiro IDE / CLI | `~/.kiro/steering/jolink-java-verification.md` | `kiro` | [Steering](https://kiro.dev/docs/steering/)；需要时纳入活动自定义 Agent 的 resources |

以下为 **Codex** 的例子，已包含 override 优先级：

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

其他客户端替换为表中的实际目标和模式，例如 CodeBuddy CLI 使用它的规则路径和
`codebuddy`，Copilot 使用 `copilot`，Kiro 使用 `kiro`。
重复执行更新同一标记段落，不重复追加。已有用户定制或破损标记需要处理时，
先读取并与用户确认，不直接覆盖。

manual 项只交接该组件：完成 MCP／Skill，给出准备好的规则文件路径／正文，
明确“全局规则尚需用户启用”。不操作 GUI，不改内部设置数据库，不偷偷替换成项目规则，
也不把写入未知路径说成已经生效。

## 6. 检查配置，需要重新加载时交给用户

检查第 1–5 步的结果：uvx 可用，选中客户端的配置已保存 joLink 项及环境变量，
无关配置保留，英文 Skill 文件在指定位置。这只能确认**安装配置完成**，不代表
MCP 已连接或 Skill/规则已被客户端加载；软件包下载可能在首次启动时才发生。
用户要求启用准则时，还要检查全局规则位置、原内容保留情况和自动加载设置；未完成
的人工组件单独报告。

如果客户端已经把新的工具、Skill 和用户要求的规则加载到当前会话，可继续第 7 步。
否则**到此停止**，告诉用户需要执行的客户端操作：按实际情况重连 MCP 或重启客户端，
再新建对话。重载当前客户端是交接动作，安装 Agent 不应杀进程来强行代办。
安装前新开的对话，不保证能看到安装后新增的工具和 Skill。

不要为了绕过当前会话的工具不可见问题，编写独立 MCP 客户端、手动发送协议消息，
或在终端直接启动服务。不要反复安装、反复修改配置，也不要杀掉或重启用户的 Agent
进程来强行完成本会话验证。独立脚本连接成功不代表当前客户端已接入；直接读取
SKILL.md 文件不代表客户端已注册该 Skill。

报告客户端、MCP 配置位置、Skill 位置、全局规则状态／位置和下一步用户操作。例如：
“安装配置已完成，客户端加载和连接验证尚未完成。请重新加载客户端，再在新对话中
验证，无需重复安装。”
没有实际观察到连接成功，就不要报告“已经连接成功”。

交接要写具体：实际 MCP 配置路径和服务名、Skill 文件路径、规则文件／界面条目，
以及该客户端需要的一个重载动作。区分“已保存”与“已加载／连接”。只有某组件需人工完成时，
交付其准备好的内容并说明未生效事项，不再把整套安装变成图形界面教程。

若要在交接前检查宿主连接，使用已有客户端自己的诊断命令即可：

| 客户端 | 原生检查 | 能证明什么 |
|---|---|---|
| Claude Code / CodeBuddy CLI | `claude mcp list` / `codebuddy mcp list` | 观察 Connected；不等于当前对话工具表已刷新 |
| Cursor CLI | `agent mcp list`、`agent mcp list-tools jolink-runtime` | ready 与实际工具列表 |
| Gemini CLI | `gemini mcp list`、`gemini skills list` | 连接与 Skill 发现，分别检查 |
| OpenCode | `opencode mcp list`、`opencode debug skill` | 连接与 Skill 发现，分别检查 |
| GitHub Copilot CLI | `copilot mcp get jolink-runtime`；`copilot skill list`、`copilot instruction list` | MCP 仅配置（Enabled 不是握手成功）；后二者分别检查 Skill／规则发现 |
| Cline CLI | `cline config --json` | 查看 joLink 的 MCP、Skill、规则是否被识别；不等于握手或模型调用成功 |
| Kiro CLI | `kiro-cli mcp status --name jolink-runtime` | 先满足客户端登录；要求登录时交接，不绕过 |

没有 CLI 时不为验证额外安装一套客户端。GUI 客户端已提供的 MCP 状态／工具清单也可
作为证据；只读日志需明确出现连接／工具发现成功，不能把“已启动进程”当作连接成功。

## 7. 通过已加载的客户端验证

这是验证已有安装，不是再安装一次。使用当前客户端提供的工具/Skill 发现或加载入口。
如果仍不可用，说明缺少哪一项后停止，不因会话仍看不到工具、Skill 或规则就重做第 1–5 步。

1. 查看客户端实际提供的 joLink 工具，不假定已安装版本具有新版接口。
2. 调用一次已暴露的状态工具：`java_status(action="status")`；仅在确实暴露旧接口时用
   `java_runtime(action="status")`。不为了检查安装而启动或停止业务应用。
3. 确认客户端的 Skill 列表能看到 `jolink-java`，或能通过客户端加载它；仅有文件不算。
4. 启用准则且客户端提供原生指令加载查询／诊断工具时，用它检查 joLink 规则。
   文件存在或复述规则都不证明已自动加载。没有这类查询能力就报告“文件已保存、
   加载尚未验证”，不编造工具，不运行业务代码来证明。

分别报告 MCP、Skill 和全局规则的验证结果。观察到规则加载，不代表模型一定遵循；
后者在正常授权的开发任务中观察，不在安装时运行业务测试。
若客户端报告了具体连接错误，再据此排查；仅仅是当前对话的工具列表没有更新，
不能据此判断安装损坏。

## 遇到问题时

- **工作目录不受信任：** Gemini 等客户端会禁用 MCP，即使用户级配置正确。
  交给用户按客户端正常流程信任其正在工作的项目目录；不关闭全局信任保护，不默认
  信任父目录或整块磁盘。重新加载后再检查连接，无需重装。
- **客户端找不到 uvx：** 安装 uv 后重连，并检查客户端 PATH。只有这个错误持续存在，
  才排查可执行文件发现问题。
- **第一次启动正在下载：** 等待首次 PyPI/Python 下载完成。宿主超时时查看其 MCP
  启动日志后重连，不重复安装，也不删除无关缓存。
- **网络/代理/证书错误：** 使用用户批准的设置；仍失败时报告具体下载错误，不关闭 TLS 校验。
- **Skill 地址返回错误或 HTML：** 从实际正在阅读的文档/ref 解析 Skill 链接，或请用户
  提供文件，不把错误页面保存成 Skill。
- **配置位置不同：** 用第 1 步的只读定位命令，并核对活动 profile。
  仍不明确时只询问缺少的具体路径，不把整套安装退回图形界面教程。
- **重装提示服务已存在：** 更新当前有效条目，不重复执行 `mcp add` 或换个名字再加一份。
  检查同名项目覆盖、插件管理的入口，保留其他设置。
- **升级后仍看到旧工具／Skill：** 通过客户端重启 joLink 服务，再新建对话。
  `@latest` 仅在该进程启动时更新包，不会更新单独复制的 Skill／规则；后二者更新原文件即可。
- **国内下载卡住：** 区分 PyPI/Python、Worker JDK/Eclipse、GitHub 指令文件三类下载。
  `JOLINK_DOWNLOAD_MIRROR=cn` 只影响 Worker 下载。针对实际失败的域名使用用户认可的
  代理／索引，或使用用户提供的 Skill／规则文件；不关闭 TLS，不尝试来历不明的镜像。
- **当前版本没有用户全局入口：** 完成可支持的部分，列出缺少的组件和界面操作，
  不悄悄改为项目级安装。

各客户端步骤于 2026-10-19 对照官方资料，并在 macOS 隔离用户目录中实走。
Codex 已完成原生工具调用；Claude Code、CodeBuddy CLI/IDE、Cursor CLI、Gemini、
OpenCode、VS Code、Kiro IDE 已观察到宿主连接／工具发现。Skill 与全局规则需独立
确认：例如 VS Code 已在原生清单识别两者，但不能据此替其他客户端宣称通过。
Copilot CLI、Cline CLI 已识别配置、Skill 和规则；Cline、Roo、Kiro CLI 等的模型调用
仍有登录／初始化交接点。Cursor IDE、Devin Desktop 的连接尚未验证，不能用其 CLI
或配置文件代替。
不是所有客户端的完整对话验收，也不是 Windows/Linux 实测结论。
