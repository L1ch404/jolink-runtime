# 公司真实 Maven 项目：JDT Phase 2A 执行手册

这份手册用于让 Agent 在公司电脑上执行：

```text
Maven clean compile
-> 冻结 BuildWorldSnapshot
-> 私有 JDT FULL BUILD
-> 跨编译器结构比较
```

它不会启动业务 JVM、不会 attach JDWP、不会 HotSwap，也不会修改 joLink 的公开
MCP 接口。除 Maven baseline 正常写入项目 `target` 外，JDT 全部工作都发生在 joLink
私有 cache 中。

## 1. 运行前要求

- joLink 仓库分支：`experiment/jdt-incremental-worker`；
- 工作区尽量干净；
- 实验期间不要改源码、切 Git 分支或触发 IDEA/Maven 并发构建；
- Build JDK 和 Target JDK：公司项目实际使用的 JDK 8；
- Worker JDK：之前构建锁定 Worker artifact 时使用的 JDK 17；
- Maven：项目/IDEA 实际使用的 Maven；
- settings.xml、本地仓库和 profile 与 IDEA 保持一致；
- 第一轮只选一个代表性模块，不跑整个 reactor 的所有业务模块。

当前 P0 只接受 Maven effective source/target 都是 Java 8。不是 Java 8 时，结构化
拒绝是正确结果。

## 2. 同步分支与依赖

PowerShell：

```powershell
cd C:\work\jolink-runtime
git fetch origin
git switch experiment/jdt-incremental-worker
git pull --ff-only
uv sync
```

检查：

```powershell
git status --short --branch
uv run python experiments/jdt-incremental-worker/run_real_maven_build_world.py --help
```

## 3. 确认锁定的 JDT 3.25 candidate 已存在

Phase 2A 默认使用：

```text
experiments/jdt-incremental-worker/locks/eclipse-2021-03-lombok-anchor.json
```

如果本机 cache 中已跑过 A1-A9，通常不需要重新下载。若 candidate 不存在，执行：

```powershell
uv run python experiments/jdt-incremental-worker/bootstrap_candidate.py `
  --bootstrap experiments/jdt-incremental-worker/candidate-bootstrap-eclipse-2021-03.json `
  --lock experiments/jdt-incremental-worker/locks/eclipse-2021-03-lombok-anchor.json
```

如果 Worker JAR 缺失或 identity 不一致，必须用同一套 Worker JDK 17 重新构建：

```powershell
uv run python experiments/jdt-incremental-worker/build_worker.py `
  --lock experiments/jdt-incremental-worker/locks/eclipse-2021-03-lombok-anchor.json `
  --java-home D:\tools\jdk-17
```

注意：重新构建会改变 lock/Worker artifact identity。只有确实缺失时才做，不要为了
跑实验随意重建。

## 4. 公司单模块项目命令

命令模板如下。所有路径都是脱敏占位值，执行前必须替换成公司电脑上 IDEA 实际使用的
项目、Maven、settings、本地仓库和 JDK 路径：

```powershell
cd C:\work\jolink-runtime

uv run python experiments/jdt-incremental-worker/run_real_maven_build_world.py `
  --project-path C:\work\your-service `
  --maven-executable D:\tools\apache-maven\bin\mvn.cmd `
  --settings-file D:\tools\apache-maven\conf\settings.xml `
  --local-repository D:\maven-repository `
  --build-java-home D:\tools\jdk-8 `
  --target-java-home D:\tools\jdk-8 `
  --worker-java-home D:\tools\jdk-17 `
  --maven-timeout 1200 `
  --worker-timeout 900 `
  --keep-attempt
```

如果是 reactor 多模块，在命令中增加精确选择：

```powershell
  --module your-service-module
```

如 IDEA 使用 profile，可重复传入：

```powershell
  --profile profile-a --profile profile-b
```

第一轮建议保留 `--keep-attempt`。成功或失败后都不要立刻删除本地 attempt。

## 5. 运行中的正常现象

公司项目 Maven baseline 本来就需要几分钟，Phase 2A 第一目标是编对，不是速度。
Runner 会依次执行：

```text
Maven clean compile
Maven compile classpath + effective POM discovery
BuildWorldSnapshot freeze
private source materialization
JDT FULL BUILD
class structural comparison
Worker shutdown
```

终端不会打印 Maven 全量日志。详细 Maven/JDT 证据在本机 attempt；可分享报告只包含
脱敏统计和 SHA-256。

## 6. 结果判断

### A. `phase2a_passed`

这是最理想结果。必须同时检查：

```text
decision = GO_FOR_PHASE2B_DESIGN
self_output_on_compile_classpath = false
stale_candidate_output_on_classpath = false
jdt_full.compile_ok = true
cross_compiler_comparison.tier1.status = compatible
maven_target_fingerprint_unchanged_after_jdt = true
project_inputs_unchanged_after_jdt = true
```

如果 `phase2b_incremental_eligible=false`，仍然不能直接进入 incremental；先看
`phase2b_blockers`，通常是未知 Processor 或 compile-time generated source 刷新语义。

### B. `phase2a_jdt_full_failed`

这不是实验无价值。查看报告中的：

```text
jdt_full.diagnostics.buckets
```

优先按下面分类：

- `missing_dependency`：Build World classpath 不完整；
- `missing_generated_source`：generated root 或 generator provenance 不完整；
- `processor_or_generated_api_mismatch`：Lombok/Processor/生成 API 不一致；
- `language_or_compiler_incompatibility`：JDT/Java level 兼容问题；
- `other`：需要在本机查看 raw Worker diagnostics。

报告不会包含诊断原文，避免公司类名、路径或 SQL/URL 等信息泄漏。分析原文只在公司
电脑本地进行。

### C. `phase2a_structural_or_isolation_gap`

JDT 可能编译成功，但不能证明兼容或隔离。重点查看 Tier 1 mismatch 数量与 isolation。
Tier 2 compiler-generated 差异本身不是失败；Tier 1 差异不能忽略。

### D. 结构化错误

常见错误：

- `JDT_PROJECT_MODEL_UNSUPPORTED`：不是 Java 8；
- `SELF_OUTPUT_ON_COMPILE_CLASSPATH`：发现当前模块旧输出；
- `SOURCE_ROOT_COLLISION`：多个 source root 相对路径冲突；
- `LOMBOK_CONFIG_LAYOUT_UNREPRESENTABLE`：冻结的一根 source model 无法忠实映射配置；
- `WORKER_JDK_IDENTITY_MISMATCH`：Worker JDK 与 lock 不一致；
- `MAVEN_BOOTSTRAP_FAILED`：先看本地 Maven 日志。

这些都应记录，不要通过复制旧 class、删除 Processor、升级 Lombok 或改公司 POM 强行
让实验通过。

## 7. 给 joLink 开发侧的脱敏反馈模板

不要发送公司源码、POM、settings.xml、Maven 日志、绝对路径、依赖坐标或 raw
diagnostics。建议只发：

```text
status / decision
candidate_id / JDT version
Build/Target/Worker JDK major
source_root_count / java_source_count
compile_classpath_entry_count
generated source provenance counts
annotation_processor_artifact_count
phase2b_blockers
JDT error/warning counts
diagnostic bucket counts
Tier 1/Tier 2 count summary
isolation booleans
elapsed time
```

如果必须让模型分析原始日志，只允许模型在公司电脑本地读取，不把内容粘贴到外部。

## 8. 第一轮验收报告

执行 Agent 最后应输出四段：

```text
Fact
    实际命令环境、阶段状态、计数、布尔 gate

Inference
    Build World 缺口的分类推断

Not proven
    未验证 incremental、HotSwap、Runtime、其他模块/平台

Next experiment
    只给一个最小下一步，不扩大范围
```

如果 Phase 2A PASS，下一步只是设计 3～5 个 Phase 2B mutation case；不要直接接
HotSwap。若失败，先让错误数量按 Build World 分类收敛，再决定是否继续这条路线。
