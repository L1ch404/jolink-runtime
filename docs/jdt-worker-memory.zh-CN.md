# JDT Worker FULL 后内存：初步定位

## 当前判断

公司观察到某个 Java 进程约 1.5 GB，仅凭任务管理器截图不能确认它就是 Worker，
也不能把该数值全部解释为仍然存活的 Java 对象。本机已在真正的 JDT Worker 中
复现 FULL 后约 0.9～1.2 GiB RSS，说明这个量级并不意外，值得在常驻前解决。

目前没有加入自动 GC、修改默认堆大小或关闭索引。这里记录的是诊断证据。

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

## 下一步建议

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
