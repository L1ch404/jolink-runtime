# 安装 joLink MCP 与 Agent Skill

[English](INSTALL.md) · [项目介绍](README.zh-CN.md)

为用户当前使用的编程 Agent 配置 joLink MCP，并安装英文 `jolink-java` Skill。
默认用户级安装，保留已有配置。直接完成你能执行的步骤；只有不清楚目标客户端，
或确实需要用户操作时再询问。本次安装不启动业务应用、不运行业务测试。

**配置安装与客户端验证是两个阶段。** 当前会话完成 MCP 配置和 Skill 文件的写入、
检查即可。如果客户端尚未加载它们，在第 5 步交给用户并停止，这是配置完成后的正常
交接，不是安装失败。第 6 步只在客户端已加载工具和 Skill 的会话中执行。

## 1. 确定客户端和安装位置

确认客户端、使用形态（CLI / IDE / 编辑器插件）、操作系统及活动 profile。
会话中已经明确的信息直接使用，不重复询问；不清楚时，问用户要配置哪个客户端。

**只使用下表中匹配的一行。** `~` 表示用户主目录，PowerShell 使用 `$HOME`。
在运行该客户端的环境中安装，不要装到无关远程终端或临时云容器里。

| 客户端 | 用户级 MCP 安装入口 | 英文 Skill 目标位置 | 模板 / 官方文档 |
|---|---|---|---|
| Codex 桌面端 / CLI / IDE 插件 | `codex mcp add jolink-runtime --env JOLINK_DOWNLOAD_MIRROR=cn --env JOLINK_JDT_GC_AFTER_BUILD=1 -- uvx jolink-runtime@latest`；或活动 `config.toml` 的 `[mcp_servers.jolink-runtime]`，通常是 `~/.codex/config.toml`，尊重 `CODEX_HOME` | `~/.agents/skills/jolink-java/SKILL.md`；已有受识别的安装时复用，不创建同名副本 | B · [MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) · [Skills](https://learn.chatgpt.com/docs/build-skills) |
| Claude Code | `claude mcp add --transport stdio --scope user jolink-runtime --env JOLINK_DOWNLOAD_MIRROR=cn --env JOLINK_JDT_GC_AFTER_BUILD=1 -- uvx jolink-runtime@latest`；用户配置是 `~/.claude.json`，不是 `~/.claude/.mcp.json` | `~/.claude/skills/jolink-java/SKILL.md` | A · [MCP](https://code.claude.com/docs/en/mcp) · [Skills](https://code.claude.com/docs/en/skills) |
| Cursor IDE / CLI | `~/.cursor/mcp.json` 中的 `mcpServers` | `~/.cursor/skills/jolink-java/SKILL.md`；也支持 `.agents/skills` | A · [MCP](https://cursor.com/docs/mcp) · [Skills](https://cursor.com/docs/skills) |
| VS Code / GitHub Copilot | 执行 **MCP: Open User Configuration** 打开当前 profile 的配置，合入 `servers` | `~/.copilot/skills/jolink-java/SKILL.md`；也支持 `~/.agents/skills` | C · [MCP](https://code.visualstudio.com/docs/agent-customization/mcp-servers) · [Skills](https://code.visualstudio.com/docs/agent-customization/agent-skills) |
| GitHub Copilot CLI | `copilot mcp add jolink-runtime --env JOLINK_DOWNLOAD_MIRROR=cn --env JOLINK_JDT_GC_AFTER_BUILD=1 -- uvx jolink-runtime@latest`；或 `~/.copilot/mcp-config.json` 中的 `mcpServers`。启用该服务的工具（`tools: ["*"]`），不等于开启自动审批 | `~/.copilot/skills/jolink-java/SKILL.md` | A · [配置](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers) |
| CodeBuddy Code CLI | 使用 `codebuddy mcp` 管理；当前目录文档列出 `~/.codebuddy/mcp.json`。检查实际配置路径，不假设 IDE 共用它 | `~/.codebuddy/skills/jolink-java/SKILL.md` | A · [MCP](https://www.codebuddy.cn/docs/cli/mcp) · [目录](https://www.codebuddy.cn/docs/cli/codebuddy-dir) |
| CodeBuddy IDE / 编辑器插件 | 打开当前产品的 **MCP** 设置及其用户配置，在其中添加本地服务。不能以为写入 CLI 配置就配置了 IDE | 在该产品显示的用户技能目录下创建 `jolink-java`；IDE 文档说明支持 `.codebuddy/skills` 用户/项目级配置 | A · [IDE MCP](https://www.codebuddy.cn/docs/ide/User-guide/MCP) · [IDE Skills](https://www.codebuddy.cn/docs/ide/Features/Skills) · [编辑器插件文档](https://www.codebuddy.cn/docs/plugin/) |
| Gemini CLI | `~/.gemini/settings.json` 中的 `mcpServers`，或用 `gemini mcp add` 选择用户级 | `~/.gemini/skills/jolink-java/SKILL.md`；避免更高优先级 `.agents/skills` 别名目录中存在冲突副本 | A · [MCP](https://geminicli.com/docs/tools/mcp-server/) · [Skills](https://geminicli.com/docs/cli/skills/) |
| OpenCode | 活动用户 `opencode.json`/`opencode.jsonc` 中的 `mcp`，通常在 `~/.config/opencode/` 下 | `~/.config/opencode/skills/jolink-java/SKILL.md` | D · [配置](https://opencode.ai/docs/config/) · [MCP](https://opencode.ai/docs/mcp-servers/) · [Skills](https://opencode.ai/docs/skills/) |
| Cline | IDE：**MCP Servers → Configure → Configure MCP Servers** 打开扩展实际 JSON；CLI：`~/.cline/mcp.json` | `~/.cline/skills/jolink-java/SKILL.md` | A · [MCP](https://docs.cline.bot/mcp/mcp-overview) · [Skills](https://docs.cline.bot/customization/skills) |
| Roo Code | **MCP → Edit Global MCP** 打开 `mcp_settings.json`；项目级是 `.roo/mcp.json` | `~/.roo/skills/jolink-java/SKILL.md` | A · [MCP](https://docs.roocode.com/features/mcp/using-mcp-in-roo) · [Skills](https://docs.roocode.com/features/skills) |
| Windsurf / Cascade | `~/.codeium/windsurf/mcp_config.json` 中的 `mcpServers` | `~/.codeium/windsurf/skills/jolink-java/SKILL.md` | A · [MCP](https://docs.windsurf.com/windsurf/cascade/mcp) · [Skills](https://docs.windsurf.com/windsurf/cascade/skills) |

客户端不在表中时，查它的官方本地 stdio MCP 和 Skill 安装说明，接入同一条 uvx
命令和同一份 Skill，不需要另一套 joLink。没有 Skill 支持时，先配置 MCP，
说明 Skill 未能注册。

## 2. 确认 uvx 可用

执行：

```sh
uvx --version
```

能执行就继续第 3 步；否则按当前系统使用 [uv 官方安装命令](https://docs.astral.sh/uv/getting-started/installation/)。

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

## 3. 配置 MCP

以下命令由**客户端启动**，不要另开终端手动运行：

```text
uvx jolink-runtime@latest
```

`@latest` 会在服务启动时检查最新发布版本并刷新 uv 的解析缓存，下载过的包会缓存复用，
不会替换已经运行中的进程。详见 [uv 版本选择](https://docs.astral.sh/uv/concepts/tools/#tool-versions)。

使用下面模板中的服务环境变量：

- `JOLINK_DOWNLOAD_MIRROR=cn`：Worker JDK/Eclipse 下载依次尝试清华、joLink 镜像、官方源。
- `JOLINK_JDT_GC_AFTER_BUILD=1`：FULL 或增量编译结束后请求一次编译 Worker GC，正常返回
  编译错误诊断时也执行。无改动复用不请求 GC，多模块整轮编译结束后只请求一次；
  不对业务 JVM 或 Test Runner 触发 GC。设为 `0` 可关闭。

将这些变量写在 **joLink 的 MCP 配置项**内，合并已有环境设置，不修改系统全局环境变量。
使用表中的原生 CLI 命令时带上两个 `--env` 参数，或补到生成的配置中。
下载镜像设置不改变 uv/Python/PyPI 或 Skill 的下载源；不需要国内镜像时改为 `official`。
详见[镜像设置](docs/runtime-download-mirror.zh-CN.md)。

读取选中客户端的现有配置，只添加或更新 joLink，保留其他 MCP 和设置，编辑前备份。
如果 joLink 已由插件或其他活动配置提供，更新原入口，不重复注册。用户明确指定了
已有版本时保留，除非用户要求升级。

已安装对应 CLI 时，可以使用表中的原生命令；否则合入下方匹配的配置，不必为了写
配置额外安装客户端 CLI。合并 joLink 这一项，不要覆盖整个文件。

### A. 使用 `mcpServers` 的 JSON

合并下面这一项，不要替换整份文件。若宿主官方 schema 不使用 `type`，按它的原生
本地服务格式保留 `command` 和 `args`。不创建远程 URL 或 OAuth 配置。

```json
{
  "mcpServers": {
    "jolink-runtime": {
      "type": "stdio",
      "command": "uvx",
      "args": ["jolink-runtime@latest"],
      "env": {
        "JOLINK_DOWNLOAD_MIRROR": "cn",
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
JOLINK_DOWNLOAD_MIRROR = "cn"
JOLINK_JDT_GC_AFTER_BUILD = "1"
```

### C. VS Code profile 的 JSON/JSONC，使用 `servers`

```json
{
  "servers": {
    "jolink-runtime": {
      "type": "stdio",
      "command": "uvx",
      "args": ["jolink-runtime@latest"],
      "env": {
        "JOLINK_DOWNLOAD_MIRROR": "cn",
        "JOLINK_JDT_GC_AFTER_BUILD": "1"
      }
    }
  }
}
```

### D. OpenCode：命令是数组，类型为 `local`

```json
{
  "mcp": {
    "jolink-runtime": {
      "type": "local",
      "command": ["uvx", "jolink-runtime@latest"],
      "environment": {
        "JOLINK_DOWNLOAD_MIRROR": "cn",
        "JOLINK_JDT_GC_AFTER_BUILD": "1"
      },
      "enabled": true
    }
  }
}
```

除 OpenCode 的命令数组外，保持 `command` 和 `args` 分开。需要传环境变量时，
OpenCode 使用 `environment`，其他模板使用 `env`（TOML 为嵌套表）。
保留已有代理、证书和工具审批设置。不需要 joLink 账号、API Key 或远程地址。

## 4. 安装英文 Skill

读取 [skills/jolink-java/SKILL.md](skills/jolink-java/SKILL.md)，以 UTF-8 复制到
**选中行给出的完整 Skill 目标位置**，目录不存在就创建。使用中文安装文档时，
也安装这同一份英文文件。

- 从本地源码目录安装：使用该目录的 `skills/jolink-java/SKILL.md`。
- 从 GitHub 文档安装：跟随该文档中同一分支/ref 的 Skill 链接，获取 **Raw** 原始内容，
  不是网页 HTML。默认分支文件是[英文 Skill 原始文件](https://raw.githubusercontent.com/L1ch404/jolink-runtime/main/skills/jolink-java/SKILL.md)。
  不要把 PyPI 包版本当作猜测的 Git 分支或标签。
- 已有同名 Skill 时更新原文件，不向所有兼容目录重复安装。用户修改过内容时，
  保留定制或先确认再替换。

确认文件具有 `name: jolink-java` 的 frontmatter 和正文。如果启用它需要用户操作，
在下面的交接中说明。不安装另一份中文 Skill，也不额外写全局规则。

## 5. 检查配置，需要重新加载时交给用户

检查第 1–4 步的结果：uvx 可用，选中客户端的配置已保存 joLink 项及环境变量，
无关配置保留，英文 Skill 文件在指定位置。这只能确认**安装配置完成**，不代表
MCP 已连接或 Skill 已被客户端加载；软件包下载可能在首次启动时才发生。

如果客户端已经把新的工具和 Skill 加载到当前会话，可继续第 6 步。否则**到此停止**，
告诉用户需要执行的客户端操作：按实际情况重连 MCP 或重启客户端，再新建对话。
安装前新开的对话，不保证能看到安装后新增的工具和 Skill。

不要为了绕过当前会话的工具不可见问题，编写独立 MCP 客户端、手动发送协议消息，
或在终端直接启动服务。不要反复安装、反复修改配置，也不要杀掉或重启用户的 Agent
进程来强行完成本会话验证。独立脚本连接成功不代表当前客户端已接入；直接读取
SKILL.md 文件不代表客户端已注册该 Skill。

报告客户端、MCP 配置位置、Skill 位置和下一步用户操作。例如：“安装配置已完成，
客户端加载和连接验证尚未完成。请重新加载客户端，再在新对话中验证，无需重复安装。”
没有实际观察到连接成功，就不要报告“已经连接成功”。

## 6. 通过已加载的客户端验证

这是验证已有安装，不是再安装一次。使用当前客户端提供的工具/Skill 发现或加载入口。
如果仍不可用，说明缺少哪一项后停止，不因会话仍看不到工具或 Skill 就重做第 1–4 步。

1. 查看客户端实际提供的 joLink 工具，不假定已安装版本具有新版接口。
2. 调用一次已暴露的状态工具：`java_status(action="status")`；仅在确实暴露旧接口时用
   `java_runtime(action="status")`。不为了检查安装而启动或停止业务应用。
3. 确认客户端的 Skill 列表能看到 `jolink-java`，或能通过客户端加载它；仅有文件不算。

分别报告实际观察到的 MCP 和 Skill 验证结果。若客户端报告了具体连接错误，再据此
排查；仅仅是当前对话的工具列表没有更新，不能据此判断安装损坏。

## 遇到问题时

- **客户端找不到 uvx：** 安装 uv 后重连，并检查客户端 PATH。只有这个错误持续存在，
  才排查可执行文件发现问题。
- **第一次启动正在下载：** 等待首次 PyPI/Python 下载完成。宿主超时时查看其 MCP
  启动日志后重连，不重复安装，也不删除无关缓存。
- **网络/代理/证书错误：** 使用用户批准的设置；仍失败时报告具体下载错误，不关闭 TLS 校验。
- **Skill 地址返回错误或 HTML：** 从实际正在阅读的文档/ref 解析 Skill 链接，或请用户
  提供文件，不把错误页面保存成 Skill。
- **配置位置不同：** 查表中官方文档，或用当前客户端“打开配置”的入口找到实际文件，
  然后继续安装同一条命令和同一份 Skill。

客户端入口依据 2026-09-20 核对的官方文档。
