# JDT 编译决策日志

这组日志用于定位“复用了 workspace，但实际又 FULL”的原因。
启动、reload、Fast Test、单/多模块共用日志入口；不需要打开断点调试。

## 日志级别

设置 MCP 服务进程的环境变量 `JOLINK_LOG_LEVEL`，重启 MCP/Worker 后生效：

| 值 | 输出 |
|---|---|
| `WARNING`（默认，亦接受 `WARN`） | 警告和错误；增量退 FULL 留一条摘要，不采集 JDT 原生详细原因 |
| `ERROR` | 只记录错误 |
| `INFO` | 缓存、变化文件、编译、保存和 JDT FULL 原因摘要 |
| `DEBUG` | INFO 内容，加 Worker 详细文本到 `worker.stderr.log` |
| `OFF` | 关闭 joLink 诊断文件和日志输出，不新建 mcp.log |

也接受标准 `CRITICAL`；大小写不敏感，空白会去除，未知值回到 WARNING，不阻止启动。
排查公司 FULL 时，建议先使用 INFO。通用 MCP 配置的 server `env` 增加：

```json
{
  "JOLINK_LOG_LEVEL": "INFO"
}
```

保留原有 JAVA_HOME 等其他 env 项，不只是改外层 shell。实际生效级别在
`java_status(status).server_diagnostics.level`；OFF 时 status 为 disabled。
这不关闭应用自身日志，也不改变结构化工具错误、测试结果或编译规则。
HTTP/MCP 原始协议报文不会因为设置 DEBUG 就自动写入诊断文件。
新增 JDT 日志统一通过 `log_diagnostic()` 输出，级别判断只在该函数内完成；
状态读取等开销较大的字段延迟计算，关闭日志时不执行这些采集。

## 日志位置与关键行

- Windows：`%LOCALAPPDATA%\jolink-runtime\logs\mcp.log`。
- macOS/Linux：`$XDG_CACHE_HOME/jolink-runtime/logs/mcp.log`，或默认
  `~/.cache/jolink-runtime/logs/mcp.log`。
- 以 `java_status(status).server_diagnostics.log_file` 的实际路径为准。

以下完整事件在 INFO/DEBUG 下可见。搜索 `jdt.workspace`、`jdt.worker`、`jdt.sources`、`jdt.build`。沿同一个 workspace、
Worker PID、build_id 和时间顺序读取，不混用不同启动或 Fast Test workspace。

| 日志事件 | 含义 |
|---|---|
| `jdt.workspace.claim` | 外层 reusable、判定原因、前后身份摘要；在原有清理动作之前记录 |
| `jdt.worker.start` | 是否尝试恢复、是否使用持久启动命令、实际 java 路径、磁盘构建状态文件大小 |
| `jdt.worker.ready` | 新 Worker PID、工程 created/reopened、配置是否复用 |
| `jdt.sources.changed` | 相对源码索引的实际变化数量、新增/删除数量、最多 16 个变化路径样本 |
| `jdt.build.started` | 本次 build_id、请求类型、传入的 touched 数量、构建前磁盘状态 |
| `jdt.build.finished` | 实际构建类型、是否从增量退成 FULL、编译源码数、错误数、JDT 决策摘要 |
| `jdt.workspace.saved` | SAVE 已确认且源码索引已写入、保存耗时、保存后的构建状态文件大小 |
| `jdt.workspace.save_failed` / `jdt.build.failed` | 未确认保存或协议/Worker 失败，不伪装成正常完成 |

例如，下面是摘要示意，省略路径和时间：

```text
jdt.workspace.claim reusable=True reason=identity_matched
jdt.worker.ready worker_pid=123 project_state=reopened
jdt.sources.changed changed_sources=1 added_sources=0 deleted_sources=0
jdt.build.started build_id=abc requested=INCREMENTAL touched_sources=1
jdt.build.finished build_id=abc requested=INCREMENTAL actual=FULL full_fallback=True compiled_sources=22
  diagnostics={"source":"jdt_builder_trace","decisions":[
    {"project":"plain-fixture","reason":"INCREMENTAL_LOOP_LIMIT_EXCEEDED"},
    {"project":"plain-fixture","reason":"INCREMENTAL_BUILD_ABORTED"}
  ],"truncated":false,"incremental_loop_limit":10}
jdt.workspace.saved elapsed_ms=30.0
```

`changed_sources` 是相对上次源码索引的变化，不等于 Git 最后两个提交的 diff。
`touched_sources=0` 在首次 FULL 时表示没有传局部文件列表，不表示没有编译源码。
`compiled_sources` 是实际观测到的编译单元去重数量，包括受影响的依赖方。

`build_states` 只读取每个工程 `state.dat` 的文件大小，不全量哈希、不参与 reusable
判定。有文件不等于状态一定可用；没有文件时，旧 Worker 也可能仍有内存状态。
必须结合实际构建决策判断，不能单凭文件存在与否归因。

## 决策摘要如何解释

这些原因来自当前锁定 JDT 的原生 JavaBuilder trace，不是 joLink 根据文件数量猜测。
仅 INFO/DEBUG 在构建期间启用 trace，构建结束恢复原开关；其他级别不采集，
source 标为 disabled。最多保留 32 个去重的工程/原因组合。

| reason | JDT 实际报告的含义 |
|---|---|
| `INCREMENTAL_LOOP_LIMIT_EXCEEDED` | 增量依赖传播超过当前轮数上限 |
| `INCREMENTAL_BUILD_ABORTED` | 增量构建中止，JavaBuilder 改做全量；优先看此前更具体原因 |
| `SAVED_STATE_NOT_FOUND` | 没有可读取的上次构建状态 |
| `CLASSPATH_CHANGED` | JavaBuilder 发现 classpath 变化 |
| `RESOURCE_DELTAS_MISSING` | 请求增量，但 Eclipse 的资源变化记录缺失 |
| `PROJECT_SETTINGS_CHANGED` | 工程 JDT 设置变化 |
| `STRUCTURAL_DELTAS` | JavaBuilder 的相应结构变化分支要求全量 |
| `TYPE_RENAME_ABORT` | 增量类型处理遇到重命名等问题而中止 |
| `OUTPUT_CLASS_CHANGED` | 输出 class 被改变，需要全量 |
| `BUILDER_RECEIVED_FULL` | JavaBuilder 收到 FULL 请求；可能来自 Eclipse 上层或编译参与者，不代表用户或 joLink 显式请求了 FULL |

如果外层 requested=INCREMENTAL，但只有 BUILDER_RECEIVED_FULL，仍不能凭这一行
区分究竟哪个 Eclipse 上层条件触发。保留构建前状态、Worker 日志继续定位，不编造
“变更文件太多”等原因。未采集到原因时，决策列表为空；旧 Worker 没有字段时标记
source=unavailable，而不是补一个猜测原因。

DEBUG 下完整 JDT 文本和 Processor 的 stdout 被分流至 workspace 内 `worker.stderr.log`；
其他级别不保留这些 stdout 文本，编译器错误标记和返回结果仍正常收集。
Worker 的原始 stdout 仅用于 JSON 协议；mcp.log 只接收编译摘要、有限路径样本和
归一化原因，不转存每一行 JDT trace 或 Processor 输出。对外提供日志前仍需检查
本地路径和业务标识，不能把整个公司 workspace 打包出去。

## 增量轮数：当前固定为 10

上游 JDT 3.25 的 `IncrementalImageBuilder.MaxCompileLoop` 默认是 5；joLink 现按
用户决定在 Worker 启动时固定设为 10，与日志级别无关。这不是修改文件
数量、Java 继承深度或 HotSwap 限制，而是一次增量构建中允许继续处理依赖影响的轮数。

```text
修改 A 的公开常量
→ 第一轮编译 A，发现导出内容改变，B 需要重编
→ 第二轮编译 B，B 的常量/类型结果改变，C 需要重编
→ 继续传播
→ 超过当前 10 轮上限，增量策略中止，转 FULL
```

此前 5 轮时，9 个常量依次引用的类只改第一个文件就会 FULL；改为 10 后该样例可以
增量完成。12 个类的同类依赖链仍会触发 FULL 回退，未取消该策略。
而 11 个类同时修改，在真实项目副本中曾只增量编译 35 个受影响文件。
因此不能用修改行数或文件数替代实际决策。

这个数是可调整的内部策略，不是不可突破的能力限制。调高后，较长依赖链可能在增量
中完成；代价是影响范围很大时会执行更多轮解析/编译，未必比一次全量快。也不能用
调高轮数解决状态缺失、classpath 或设置变化。

### 暂缓研究：20 或更大

本轮只改到 10，不做自动调参，不增加编译校验/回滚机制。未来再研究 20 或更大时，
固定 JDK、源码变更和状态，对照编译范围、耗时、CPU/内存、取消响应，以及增量结果
与同一编译器 clean-full/业务测试的差异。提高轮数本身不跳过依赖分析；主要直接风险
是更多编译工作，但不据当前小样例保证更高上限在所有项目上都正确或更快。
本轮没有尝试 20/更大，也没有设置无限轮数。

## 验证入口与升级注意

- `tests/e2e/test_jdt_build_logging.py`：真实 stdio MCP，在默认/INFO/DEBUG/ERROR/OFF
  下验证 9 类链增量、12 类链 FULL、正确业务结果、日志分级及无改动不构建。
- `tests/unit/test_jdt_compile_session.py`：请求/实际类型及保存日志的区分。
- 既有即时持久化、APT、索引关闭、Maven/Gradle 多模块用例继续回归。

上一轮日志初版的本机验证：普通测试 `782 passed, 32 skipped`；新日志 MCP 用例 1 项、
Java 8/11 持久化/APT/索引关闭 4 项、Maven/Gradle 模块 3 项、启动/reload 4 项真实
回归均通过。另使用本地业务服务样本（项目标识已脱敏）的源码副本和未加旁路补丁的 stdio MCP 完成
457 源码 FULL、预期断言失败、恢复、重新打开后的增量，确认摘要确实写入 mcp.log。
这些是本机证据，不是公司那一次 FULL 的原因结论。

固定 10 轮、增加日志环境变量并统一日志函数后的复测：普通测试
`791 passed, 36 skipped`；默认/INFO/DEBUG/ERROR/OFF 五组真实 MCP 对照全部通过，
9 类链增量、12 类链回退和测试结果一致。即时持久化、APT、索引关闭及 Maven/Gradle
模块的 7 项真实回归也通过；更高轮数留待后续，不在本轮实现。

这次加入了 Worker trace 采集代码，需要新的 Worker 包。当前缓存策略把 Worker 包身份
作为匹配输入，因此升级后可能有一次 identity_changed/FULL；开启 INFO 时日志会明确显示它。
这与升级后业务 git pull 引发的增量回退不是一回事。本轮没有修改该缓存兼容策略。
