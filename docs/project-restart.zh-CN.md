# 编译感知的 Restart

`java_application(action="restart")` 是当前代码生效的统一入口，合并了原来的
公开 `reload` 操作。默认 `hotswap=true`，需要真实重启时设置 `hotswap=false`。

```json
{"action":"restart","timeout":30}
```

```json
{"action":"restart","hotswap":false,"timeout":30}
```

## 现有 JDT 会话下的流程

1. 复用当前 Build World 和持久 JDT workspace。
2. `workspace_source_changes()` 检查目标及所需上游模块的源码。
3. 对实际新增、修改、删除的源码调用一次 `compile()`，由 JavaBuilder 处理依赖传播。
   无源码变化时不发出 JDT build；上轮未应用的输出差异仍参与本轮处理。
4. 正常编译结果和构建状态沿用现有持久化，无产物副本。
5. 根据结果选择 HotSwap 或真实重启。

公开 `restart` 不接收 `source_files`，自动发现所有变化，不套用旧 reload 的 16 文件限制。Maven/Gradle、
单模块/多模块共用这条编译流程，不调用正式 Maven/Gradle 编译任务。

| 情况 | 处理 |
|---|---|
| 有 class 差异且 JVM 接受 | HotSwap，PID 不变 |
| `hotswap=false` 或明确传入 JVM/程序/端口参数 | 编译后真实重启 |
| 没有待应用 class 差异 | 不编译，真实重启 |
| 删除 class、生成资源变化、类未加载或加载器不唯一 | 使用刚编好的产物重启 |
| JVM 明确拒绝 RedefineClasses | 使用刚编好的产物重启，不再编译一次 |
| 编译错误 | 返回诊断，不主动停止原 JVM；不能带错误继续启动 |
| RedefineClasses 的回复超时或通信中断 | 返回 `HOT_SWAP_OUTCOME_UNKNOWN`，不假设成功或拒绝 |

未知结果可以通过 `restart(hotswap=false)` 建立新的进程状态。HotSwap 成功后，
本地记录/断点刷新出错仍保留已成功应用这一事实。

## 返回与等待

沿用 `timeout`：默认 30 秒，大于 30 按 30 等待，0 立即返回。
后台仍用已有操作 ID `reload_id`、`active_operation` 和 `last_reload`，不再增加
一套状态存储。`active_operation.operation` 为 `restart`，阶段包括 compiling、
applying_hotswap、restarting。终态明确返回：

- `apply_method=hotswap`，`status=reloaded`：字节码已更新，应用未重新初始化。
- `apply_method=restart`，`status=restarted`：新进程已达到配置的启动就绪条件。
- 失败：实际 error/diagnostics，不把已提交的启动任务当成已应用。

超时仅结束本次回复等待，不取消后台操作。`java_status` 可观察进展，`stop` 可停止。
保留原操作对象，不会把后续另一轮 restart 的结果误当成本轮结果。

## 与完整启动的关系

- 没有可用的旧 JDT 会话（例如已 stop）时，复用现有项目 launch 流程，读取持久缓存。
- POM/Gradle 配置已改变时，也走现有重新 launch 路径，刷新 Probe 模型和对应 JDT
  workspace；不拿旧编译配置去编译新项目。**这条模型重建路径沿用先停止旧 JVM 的
  启动流程，不提供旧进程保留、Candidate 或 rollback。**
- 显式提供新 `project_path`/启动选择时，沿用指定项目的完整重新启动。
- 直接 JAR/classpath 启动没有源码 Build World，只重启该产物，不承诺源码编译。

HotSwap 不重新执行构造器、静态初始化或 Spring 启动流程。修改初始化逻辑、框架
配置、资源且需要重新加载时，明确使用 `hotswap=false`。不能因为 JVM 接受字节码，
就认为框架已重新初始化。资源目录仍直接在 Runtime classpath 上，不新增全量资源扫描。

## 回归位置

- `tests/unit/test_project_restart_service.py`：编译一次、自动扫描、错误/unknown、选择应用方式。
- `tests/e2e/test_compile_restart.py`：真实 MCP/JDT/JVM/HTTP，修改/错误/恢复、结构变化、
  新增删除、强制/无变动重启、跨 MCP 缓存、Fast Test 不影响应用。
- `tests/e2e/test_maven_modules.py`、`test_gradle_modules.py`：上游修改与真实依赖传播。
- `tests/e2e/test_reload_reply_timeout.py`：真实 JVM 延迟回应，超时不等于未应用。
- `tests/e2e/test_build_configuration_mcp.py`：活动模型不被另一份新缓存误认，restart 刷新。

实现复用现有 JDT/reload/launch 等待机制。项目重启的旧 adapter 编排已移至
`launch/project_restart.py`；没有改变 Worker、Probe 或持久缓存格式。

## 本机验收（2026-09-17，未提交工作区）

- 普通测试：898 passed / 13 skipped / 93 deselected（真实MCP另行开启运行）。
- 29项不同的真实stdio MCP用例通过，包含上述新restart闭环、32秒延迟启动的
  30秒回复预算、stop中止、应用退出恢复、Maven/Gradle多模块、配置刷新、包路径、
  完整调试链、3组真实HotSwap回复延迟，以及独立Fast Test。
- Java8目标/Java21 Worker的产品验证脚本通过，结构变化自动重启已验证。
- 真实Spring Petclinic：普通Controller修改HotSwap，HTTP由200变302且PID不变；
  新增字段后自动重启且新行为保持；恢复源码并强制重启回到200；3项所选Fast Test
  通过，期间应用PID不变。测试源码及临时启动项已恢复。
- compileall、定向静态检查、diff检查、wheel/sdist构建通过。
- 额外旧Gradle单模块脚本末尾的doLast拒绝预期不通过；干净e5bad1d也已真实复现，
  单独记录为兼容性文档U13。没有将该整组报告为通过。

运行环境为本机macOS，不能替代Windows公司项目验收。真实MCP测试启动的是当前
工作区的新stdio进程，不依赖客户端里可能仍存活的旧MCP实例。
