# JDT Worker FULL 后内存：初步定位

## 当前判断

公司观察到某个 Java 进程约 1.5 GB，仅凭任务管理器截图不能确认它就是 Worker，
也不能把该数值全部解释为仍然存活的 Java 对象。本机已在真正的 JDT Worker 中
复现 FULL 后约 0.9～1.2 GiB RSS，说明这个量级并不意外，值得在常驻前解决。

本节保留改动前的诊断证据。后续已停用搜索索引，见下方实测。
2026-09-10 增加可选的编译后 GC，默认关闭，使用方法见文末；默认堆大小未改变。

## 已确认

- Worker 默认 `-Xms64m -Xmx2048m`，上限不是实际常驻占用；项目编译内存配置
  可以影响最终参数，排查时应查看具体 Worker 的启动命令。
- FULL 返回后，真实线程栈中仍有 RUNNABLE 的 `Java indexing`。
  栈包含 `SourceIndexer.indexResolvedDocument`、`JavaSearchNameEnvironment`、
  `JavaModelManager.getZipFile` 等；此时后台仍在读取依赖/解析类型。
- 显式 GC 能明显减少 heap used，但 committed heap 与进程 RSS 未必同步下降。
- GC 后仍能进行真正的 INCREMENTAL，本次三种 JDK 的单文件增量均通过。

## 本机测量

macOS，使用本机业务工程已成功建立的 Fast Test Build World；457 个 main/test
Java 源码，每种 JDK 都新建一个临时 Worker、执行 FULL。没有运行应用或测试业务，
没有修改项目源文件；增量验证只在临时 Worker 的源码镜像末尾添加注释。

以下单位为 MiB，均为进程 RSS，不是 Windows 任务管理器的专用工作集。

| Worker JDK / GC | FULL 后闲置 2 秒 | 第一次 GC 后立即 | 第一次 GC 后约 10 秒 | 第二次 GC 后 1 秒 |
|---|---:|---:|---:|---:|
| 11.0.27 / G1 | 1029 | 408 | 912 | 423 |
| 8u332 / Parallel | 1078 | 1079 | 1077 | 1077 |
| 17.0.16 / G1 | 1084 | 1091 | 788 | 476 |

JDK 17 第一次 GC 后，heap committed 已从 740 MiB 降为 170 MiB，但 RSS 没有
立即下降；一秒后的 RSS 为 633 MiB。这说明不能只看 GC 命令返回瞬间的进程数值。

第二次 GC 后的 heap used / committed：

| JDK | heap used | heap committed | 单次 GC 请求耗时 | GC 后单文件增量 |
|---|---:|---:|---:|---:|
| 11 | 27 | 100 | 约 43～45ms | 52ms，INCREMENTAL |
| 8 | 31 | 746 | 约 51～60ms | 54ms，INCREMENTAL |
| 17 | 28 | 110 | 约 24～25ms | 34ms，INCREMENTAL |

所以 JDK 8 这轮虽然对象已经回收，JVM 仍保留较大堆空间，RSS 基本不下降。
JDK 11/17 则有明显回落，但后台索引仍能再次产生分配。以上是短时、单项目样本，
不是长期泄漏检测，也不能直接代替公司 4000+ 源码、Windows、具体 JDK 的测量。

## 原始诊断阶段的建议

1. 公司先记录 PID、命令行、Worker JDK 与任务管理器具体列，确认是哪一个 JVM。
2. 分别观察 heap used、heap committed、RSS/工作集，不能只看一个总数。
3. 用显式 GC 做一次对照可行，但不能据此认定“FULL 后固定 GC 一次”就是最终方案。
4. 常驻前值得单独验证搜索索引工作能否从编译 Worker 中移除。先证明真实 FULL、
   增量、跨模块引用和 Lombok 正常，不能仅凭线程名就关闭内部服务。
5. 若索引确实需要保留，再验证空闲阶段 GC 与合适的回收器/堆策略；不在本轮调参。

## 重复诊断

当前脚本读取 Maven Fast Test 缓存。先为目标工程成功建立一次缓存，再运行：

```bash
uv run python scripts/diagnose_jdt_worker_memory.py /path/to/project \
  --java-home /path/to/worker-jdk \
  --report /tmp/jdt-worker-memory.json
```

可重复传入 `--java-home` 比较不同 Worker JDK。脚本复用缓存模型，在临时目录编译，
结束后关闭自身 Worker；不会向当前应用 JVM 发送 GC。报告保存在本地。

若针对已经运行的 Worker 排查，可先用 `jcmd <pid> VM.command_line`、
`jcmd <pid> help` 确认身份及可用命令，再使用支持的 heap 信息命令和 `GC.run`。
显式 GC 会暂停/影响被测 JVM，应只针对确认的编译 Worker、在编译空闲时执行。
[Oracle jcmd 文档](https://docs.oracle.com/en/java/javase/11/tools/jcmd.html) 说明
`GC.run` 请求 `System.gc()`，它不是“把全部 JVM 内存归还操作系统”的命令。

## 停用搜索索引后的验证（2026-09-07）

### 实现

改动集中在 Worker：`CompilerOnlyIndexManager.install()` 在工程初始化前结束原索引
管理器，替换为不启动搜索线程、不接受排队任务的实现。`reset()`也不再启动线程，
因此重开 workspace 不会重新启动索引。并未删除 JDT Core，也未改 JavaBuilder、
`State / ReferenceCollection`、Lombok/APT、JDWP 或业务编排。

这不是把线程 suspend/sleep：新的索引请求直接不入队，避免长期暂停后积压对象。
`METRICS.search_indexing`记录停用状态、待处理数和被丢弃的请求数，供测试验证。
依赖的是当前锁定 JDT 3.25 的内部接口，升级 JDT 时需要重新回归。

### 产物和功能

- 同一份本机工程457个main/test源码，旧Worker（`d3fd957`）与新Worker的476个class
  完整SHA树一致。对照FULL分别约4.34秒、4.36秒，没有证据宣称FULL本体明显提速。
- 新增自动化：次要类型、跨文件引用、泛型、Lambda、构造器/方法引用、类型重命名、
  编译失败恢复、删除恢复、Worker重开、100次增量修改；最终与独立clean-full class树完全一致。
- 索引待处理队列全程为0，线程检查没有`Java indexing`。
- 扩展APT回归：生成Java源码，原源码用方法引用调用生成类；FULL、增量、错误恢复、
  重开后都通过。
- 原MCP调试/生命周期、Maven多模块、Gradle Groovy/Kotlin多模块、JUnit/TestNG、
  Lombok、metadata Processor、reload/重启恢复均通过既有真实回归。
- 当前Codex连接的真实MCP：本地业务服务样本（项目标识已脱敏）的6项Mockito测试通过，单文件修改产生
  预期2项失败，恢复后6项通过；JsonPath为85通过、11原有跳过，无失败。
- 当前真实MCP的Spring多模块应用：启动、HTTP Trigger断点、变量读取、resume、
  上游HotSwap和restart后的实际HTTP结果均正确。测试源码及临时配置已恢复。

自动化入口：`tests/e2e/test_jdt_without_search_index.py` 和扩展后的
`tests/e2e/test_jdt_persisted_configuration.py`；已有Maven/Gradle与MCP套件继续复用。
公开项目的完整操作与历史版本说明见`gradle-modules.zh-CN.md`。

### 内存变化

同源、同JDK 11、同堆配置的独立对照（MiB RSS）：

| 阶段 | 原Worker | 停用搜索索引 |
|---|---:|---:|
| FULL后闲置2秒，未GC | 1085 | 751 |
| 显式GC后立即 | 390 | 272 |
| GC后约10秒 | 845 | 272 |

另一组跨JDK测量中，新Worker的JDK 11/17在GC后约10秒分别为248/341 MiB，
heap used约13～14 MiB，没有出现先前的持续回涨。JDK 8虽然heap used也降到
约14～29 MiB，RSS仍约977 MiB，堆空间归还仍受回收器策略影响。

**不能把GC后的数字当作产品默认常驻内存。** 这组测量时产品没有自动GC，FULL遗留垃圾和
已扩张的堆仍可能使刚编译完的RSS很高。本轮解决的是搜索后台工作及其持续分配，
没有同时改变GC策略。数据来自macOS和457源码工程，不能直接替代Windows/4000+源码验证。

开发者可为同一项目输出新旧Worker的class树摘要和内存报告：

```bash
uv run python scripts/diagnose_jdt_worker_memory.py /path/to/project \
  --java-home /path/to/jdk --candidate-ref d3fd957 --report /tmp/index-before.json
uv run python scripts/diagnose_jdt_worker_memory.py /path/to/project \
  --java-home /path/to/jdk --report /tmp/index-after.json
```

`--candidate-ref`用于开发对照，需要该历史Worker已经安装在本地缓存。
搜索索引服务不再提供给本Worker；未来若增加Java模型搜索类能力，不能直接假定可用。
本轮覆盖的编译/调试/测试路径没有发现受影响，尚未证明所有第三方处理器和特殊项目都无影响。

## 可选的编译后 GC（2026-09-10）

在 MCP Server 的 `env` 中配置：

```json
{
  "JOLINK_JDT_GC_AFTER_BUILD": "1",
  "JOLINK_LOG_LEVEL": "INFO"
}
```

`JOLINK_JDT_GC_AFTER_BUILD` 默认关闭，设为 `1` 开启；设为 `0` 或删除即可关闭。
日志级别只影响记录，不影响是否执行 GC。修改 MCP 环境变量后重新连接 MCP。

流程只增加一个调用：

```text
实际 FULL / INCREMENTAL 完成
→ 保存 JDT workspace 和源码索引
→ 开关开启时，向编译 Worker 发送一次 GC
→ 返回原编译结果
```

- 无改动直接复用、包括复用上次编译失败状态，不请求 GC。
- 多模块整轮 BUILD 后一次；不按文件、main/test 分组或模块分别 GC。
- 正常返回编译错误诊断时同样请求一次 GC，保留原错误，不把它变成成功。
- 只针对编译 Worker，不触发业务 JVM 或 Test Runner 的 GC。
- 复用 Worker 已有 `GC` 命令，本次不改 Worker JAR、缓存身份或 `reusable`。
- 不加定时器、内存阈值、重试、等待 RSS 下降、自动换 GC 算法或常驻 Worker。

GC 请求耗时计入原总编译耗时，`jdt_build_ms` 仍只表示 JDT 编译本体。
在 INFO/DEBUG 的 `mcp.log` 中，`jdt.gc.requested` 记录 build_id、Worker PID、
请求耗时，以及返回时的 heap used/committed。它可以通过 build_id 对应前面的
`jdt.build.finished`。不额外请求一次 METRICS，也不在每次编译后扫描进程内存。

`status=gc_requested` 只表示执行了显式 GC 请求，不保证垃圾全部回收或 RSS 下降。
尤其 JDK 8 的已提交堆可能继续由 JVM 保留。GC 通信超时或断连沿用现有 Worker
通道错误处理，不重试，不继续用未消费完响应的连接发送后续 BUILD。

回归入口：

- `tests/unit/test_jdt_compile_session.py`：默认/关闭/开启、BUILD→SAVE→GC 顺序，
  错误诊断保留、无改动不调用、GC 耗时计入总耗时、通信异常。
- `tests/e2e/test_jdt_build_gc.py`：真实 stdio MCP，FULL、增量、编译错误及恢复、
  缓存失败、无改动重开；开关开启时每个实际 build_id 恰好对应一次 GC 请求。
- `tests/e2e/test_jdt_noop_reopen.py`：Java 8/11，单/多模块，分别开启和关闭 GC；
  连续无改动重开后仍实际增量只编一个文件，并运行 JVM 验证结果。

### 产品开关实测

macOS，本地业务服务样本源码副本，457 个 main/test 源码，Worker JDK 11。
开启和关闭各使用独立缓存、新 stdio MCP 与同一份已缓存 Build World；原项目未修改。
每组 14 次实际编译：首次 FULL、5 轮业务修改/恢复（10 次增量）、编译错误、
恢复、跨 MCP 重开后的增量；另包含普通 no-op、缓存编译失败和重开 no-op。

两组业务修改均产生预期的 2 个测试失败，恢复后 6/6 通过；编译错误没有启动 Runner。
开启组 14 个 build_id 恰好对应 14 次 GC，关闭组 0 次；全部后续编译仍为 INCREMENTAL。

| 指标 | GC 关闭 | 每次实际编译后 GC |
|---|---:|---:|
| FULL 和首轮测试结束后的 Worker RSS | 581.5 MiB | 294.1 MiB |
| 同一 Worker 后续采样最高 RSS | 661.1 MiB | 327.0 MiB |
| 10 次业务修改/恢复的编译总耗时中位数 | 66.4ms | 78.7ms |
| 对应 Fast Test 总耗时中位数 | 1769.2ms | 1786.3ms |
| 单次 GC 请求耗时中位数（14 次） | — | 27.0ms |

这是一次本机顺序对照，不是严格性能基准；编译总耗时差不等于单独 GC 耗时，
JIT、调度和各次编译也有波动。这里只确认实际存在内存收益与额外耗时，
不能据此保证公司 Windows 大项目也是几十毫秒。

另外用原内存诊断脚本，分别在 Java 8/11、开关关闭/开启的新 Worker 中测量。
取 FULL 后闲置 2 秒的快照，**早于脚本后续手工 GC 对照**：

| Worker | heap used：关 → 开 | heap committed：关 → 开 | RSS：关 → 开 |
|---|---:|---:|---:|
| JDK 8u332 / Parallel | 338.1 → 19.4 MiB | 708 → 704 MiB | 913.0 → 768.3 MiB |
| JDK 11.0.27 / G1 | 236.1 → 12.8 MiB | 392 → 64 MiB | 617.2 → 341.0 MiB |

四组的 476 个 class 输出 SHA 树一致，后续增量均只编一个源码。Java 8 即使 heap
used 明显下降，仍保留约 704 MiB 已提交堆；本轮没有改回收器或堆参数。

本机验证：普通测试 `798 passed, 46 skipped`；开关/重开定向真实用例 `10 passed`，
开启 GC 的真实 MCP 启动/reload、Maven/Gradle 多模块、即时持久化、APT、
禁用索引（含 100 次增量）回归 `18 passed`；开启 GC 的完整 Fast Test 回归
`6 passed`。真实环境用例单独开启对应开关，不将 skip 算通过。
wheel/sdist、`compileall` 与 `git diff --check` 通过。尚未在公司 Windows 大项目验收。
