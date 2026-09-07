# JDT 构建状态立即保存

## 已定位的问题

2026-09-07 使用 `ss-admin-service` 的副本、457 个 main/test Java 源码和真实
stdio MCP 复现：第一次编译及 6 个测试全部结束后，保持旧 Worker 闲置，修改一个
业务文件，再从另一个 MCP 进程运行测试。两次编译没有重叠。

外层 `state.json` 的身份值一致，`reusable=true`，请求是 `INCREMENTAL`，但 JDT
实际执行 `FULL`，重编全部 457 个源码。正常退出旧 Worker 后再执行同样操作，则
只增量编译一个文件。

原因不是身份比对太严格：joLink 写了 class 输出、源码索引和初始化标记，却没有
立即请求 JDT 保存增量构建状态。`state.dat` 在 Worker 内存中对应依赖关系和上次
构建结果；只有应用层的 `state.json` 存在，并不能证明它已保存。

这个问题也发生在已持久化的 workspace 完成下一次增量之后，并非只影响首次 FULL。

## 本轮处理

在共用 `PersistentJdtCompileSession` 中执行：

```text
BUILD FULL / INCREMENTAL 返回
→ 收集本次编译结果和错误状态
→ Worker SAVE，收到 saved
→ 原子替换 source-index.json
→ 返回本次结果
```

- 启动、reload、Fast Test，以及 Maven/Gradle 多模块共用这一处实现。
- 不等 Worker 关闭才保存；旧 Worker 处于闲置状态时，新进程也能读取已保存结果。
- 编译错误同样保存错误事实，不把失败伪装为有效的旧结果。
- 无改动、不调用 BUILD 时不额外 SAVE。
- 编译耗时包含保存耗时；不添加全量扫描、class 副本或新 Worker 协议。
- SAVE 未确认时返回错误，不报告已经保存成功。沿用原有异常处理，不新增自动回滚。

历史上已经丢失的 JDT 增量状态不能凭空恢复；这类 workspace 下一次编译仍可能需要
FULL。本次改动负责在构建完成后及时保存，避免继续依赖进程退出时机。

## 回归入口

- `tests/unit/test_jdt_compile_session.py`：BUILD/SAVE 顺序、源码索引、无改动不保存、
  编译错误持久化、SAVE 被拒时不报告成功或自动 FULL。
- `tests/e2e/test_jdt_immediate_persistence.py`：真实 Java 8/11 Worker，保持先前
  Worker 闲置，连续从新 Worker 修改源码，验证实际只编一个文件，并运行输出。
- `tests/e2e/test_jdt_persisted_configuration.py`：APT 生成类及错误恢复。
- 既有真实 MCP 启动/reload、Maven/Gradle 模块与 Fast Test 用例继续回归。

本机此次执行：普通测试 `781 passed, 31 skipped`；真实 Java 8/11 即时持久化与
APT 回归 `3 passed`；真实 MCP 启动/reload 和 Maven/Gradle 模块 `14 passed`；
Fast Test 完整回归 `6 passed`。跳过的真实环境用例另以对应开关运行，不把 skip 算通过。

`ss-admin-service` 副本通过新 stdio MCP 进程再次验证：

| 场景 | 实际构建 | 编译源码数 |
|---|---|---:|
| 首次完成后旧 Worker 闲置，新窗口修改一个文件 | INCREMENTAL | 1 |
| 已有缓存完成一次增量后，再换窗口修改一个文件 | INCREMENTAL | 1 |
| 同一窗口修改文件 | INCREMENTAL | 1 |

前两条修复前均实际退为 FULL，编译 457 个文件。修改业务逻辑产生预期的两个断言失败，
恢复源码后 6/6 通过；不是只看编译状态。诊断使用源码副本与独立缓存，原项目未修改。

## 先记录、暂不实现的情况

1. **不能只回滚旧 `state.dat`。** 真实 JVM 对照中，旧状态不知道 Client 后来新增了
   对 Flags 常量的依赖；恢复旧状态再把常量从 7 改成 9，编译成功但 Client 漏编，
   实际仍返回 7。旧状态恢复需要处理此后变更和对应 workspace 状态，不是只复制
   一个文件。本轮不加旧状态备份/自动回滚。
2. **多个独立 Worker 的后续写回。** 即使构建任务顺序执行，长期闲置的旧 Worker
   后退出时仍可能写回它较旧的构建状态和源码索引。已看到这种写回顺序；本轮不解决
   多进程 workspace 所有权，也不把“立即保存”描述为支持任意多窗口同时写入。
3. **初始化/恢复异常清理。** 既有 `start()` 异常路径会清理 workspace，
   `claim()` 身份不匹配时也会清理目录；重叠首次初始化曾实际导致目录互删。
   缓存保护、恢复重试留待后续处理。
4. **Worker 包身份变化。** 当前整个 Worker lock 身份参加缓存匹配，即使只改指标
   或日志也可能使缓存失效。编译状态兼容性与包身份如何分开，另行讨论。
5. **保存中断。** JDT SAVE 与 Python 源码索引不是一个跨文件原子事务；磁盘故障、
   两步之间进程被强制结束等情况未增加备份事务或回滚系统。

以上不作为公司那一次 FULL 的唯一归因，也不声称这些情况的发生概率已经测定。
