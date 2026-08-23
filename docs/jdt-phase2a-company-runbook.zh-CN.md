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

## 当前 canonical 状态

截至 2026-08-21，公司环境已经保存一份 canonical Phase 2A JSON：

```text
4201 Java sources
JDT FULL 0 errors
Tier 1 compatible
status   = phase2a_passed_with_incremental_blockers
decision = PHASE2B_BLOCKED_BY_BUILD_WORLD
```

当前唯一 Phase 2B blocker 是 `unknown_compile_time_annotation_processor`。本手册
仍保留完整执行步骤用于复现和回归；更早 DOCX 中“大项目尚未通过”的描述只代表历史
排查阶段。详细脱敏记录见 `jdt-phase2a-company-evidence.zh-CN.md`。

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
uv run python experiments/jdt-incremental-worker/run_maven_probe_spike.py --help
uv run python experiments/jdt-incremental-worker/run_real_maven_build_world.py --help
```

## 3. 确认锁定的 JDT 3.25 candidate 已存在

Phase 2A 默认使用：

```text
experiments/jdt-incremental-worker/locks/eclipse-2021-03-lombok-anchor-diagnostics-v2.json
```

旧 `eclipse-2021-03-lombok-anchor` 的 A1-A10/Phase 1B 证据不能继承到
diagnostics-v2。若新 candidate 不存在，执行：

```powershell
uv run python experiments/jdt-incremental-worker/bootstrap_candidate.py `
  --bootstrap experiments/jdt-incremental-worker/candidate-bootstrap-eclipse-2021-03-diagnostics-v2.json `
  --lock experiments/jdt-incremental-worker/locks/eclipse-2021-03-lombok-anchor-diagnostics-v2.json
```

如果 Worker JAR 缺失或 identity 不一致，必须用同一套 Worker JDK 17 重新构建：

```powershell
uv run python experiments/jdt-incremental-worker/build_worker.py `
  --lock experiments/jdt-incremental-worker/locks/eclipse-2021-03-lombok-anchor-diagnostics-v2.json `
  --java-home D:\tools\jdk-17
```

注意：重新构建会改变 lock/Worker artifact identity。只有确实缺失时才做，不要为了
跑实验随意重建。

### 3.1 可选：未验证 Processor APT Dogfood candidate

仅用于公司真实项目探索，不改变默认fail-closed语义：

```powershell
uv run python experiments/jdt-incremental-worker/bootstrap_candidate.py `
  --bootstrap experiments/jdt-incremental-worker/candidate-bootstrap-eclipse-2021-03-apt-spike.json `
  --lock experiments/jdt-incremental-worker/locks/eclipse-2021-03-apt-spike.json

uv run python experiments/jdt-incremental-worker/build_worker.py `
  --lock experiments/jdt-incremental-worker/locks/eclipse-2021-03-apt-spike.json `
  --java-home D:\tools\jdk-17
```

这个candidate包含标准Eclipse APT能力，但不会把未知Processor升级为可信产品能力。

## 4. 公司单模块项目命令

本轮必须先由 Maven Probe 导出事实，再把同一份私有 Probe 报告交给 Phase 2A。不要只跑
第二条命令，否则 JDT 仍会使用历史 discovery，无法回答本轮问题。

先设置一次变量。所有路径都是脱敏占位值，执行前必须替换成公司电脑上 IDEA 实际使用的
项目、Maven、settings、本地仓库和 JDK 路径：

```powershell
cd C:\work\jolink-runtime

$Project = "C:\work\your-service"
$Maven = "D:\tools\apache-maven\bin\mvn.cmd"
$Settings = "D:\tools\apache-maven\conf\settings.xml"
$Repository = "D:\maven-repository"
$BuildJdk = "D:\tools\jdk-8"
$WorkerJdk = "D:\tools\jdk-17"
$Cache = "$env:LOCALAPPDATA\jolink-runtime\jdt-poc"
```

### 4.1 Maven-native Probe

```powershell
$ProbeRaw = uv run python experiments/jdt-incremental-worker/run_maven_probe_spike.py `
  --project-root $Project `
  --maven-executable $Maven `
  --settings-file $Settings `
  --local-repository $Repository `
  --java-home $BuildJdk `
  --cache-root "$Cache\maven-probe" `
  --timeout 1800 `
  --keep-attempt

if ($LASTEXITCODE -ne 0) { throw "Maven Probe process failed" }
$Probe = $ProbeRaw | Select-Object -Last 1 | ConvertFrom-Json
if (-not $Probe.ok) { throw "Maven Probe failed" }
if ($Probe.ephemeral_settings_retained) { throw "Temporary Maven settings retained" }
$ProbePrivateReport = $Probe.private_report_path
if (-not (Test-Path $ProbePrivateReport)) { throw "Probe private report missing" }
```

如 IDEA 使用 profile，在 Probe 命令中重复增加：

```powershell
  --profile profile-a --profile profile-b
```

Probe 会读取显式 settings；如果省略 `--settings-file`，则保留 Maven 默认的
`~/.m2/settings.xml` 语义。包含 server 凭证的 attempt-local settings 只在 Maven 执行期间
存在，即使使用 `--keep-attempt` 也必须在结果中看到：

```text
ephemeral_settings_retained = false
```

Probe 成功只能证明 Maven-native 基础事实已经导出；不要分享 `private_report_path` 指向的
文件，因为其中包含公司绝对路径和本地仓库信息。

### 4.2 Probe Build World -> JDT FULL

```powershell

uv run python experiments/jdt-incremental-worker/run_real_maven_build_world.py `
  --project-path $Project `
  --maven-executable $Maven `
  --settings-file $Settings `
  --local-repository $Repository `
  --build-java-home $BuildJdk `
  --target-java-home $BuildJdk `
  --worker-java-home $WorkerJdk `
  --maven-probe-private-report $ProbePrivateReport `
  --cache-root $Cache `
  --maven-timeout 1800 `
  --worker-timeout 900 `
  --keep-attempt
```

如需探索公司项目的标准Processor FULL，可在同一命令增加：

```powershell
  --experimental-allow-unverified-apt-providers
```

该开关只放开“Provider尚未进入verified allowlist/runtime dependency closure未证明”；
以下Maven语义仍然fail closed：

```text
-A options
explicit Processor names
execution-level Processor config
maven.compiler.proc property
raw -proc/-processor/-processorpath/-s args
proc=only
directory Provider
```

实验报告必须看到：

```text
trusted_for_product_decision = false
apt_experiment.unverified_provider_fast_path = true
apt_experiment.factory_path_verified = true
warnings包含UNVERIFIED_APT_PROVIDER_FAST_PATH
```

这次结果只用于判断真实Processor能否在JDT FULL中运行，不能据此删除Phase 2B blocker
或发布class/resource。

如果是 reactor 多模块，在命令中增加精确选择：

```powershell
  --module your-service-module
```

如 IDEA 使用 profile，Phase 2A 必须传入完全相同且顺序相同的 profile：

```powershell
  --profile profile-a --profile profile-b
```

第一轮建议保留 `--keep-attempt`。成功或失败后都不要立刻删除本地 attempt。Phase 2A 会
校验项目、POM、Maven executable、settings 内容、本地仓库、profile、模块和 Probe
implementation identity；不一致时会 fail closed，不能拿旧报告凑数。

## 5. 运行中的正常现象

公司项目 Maven baseline 本来就需要几分钟，Phase 2A 第一目标是编对，不是速度。
Runner 会依次执行：

```text
Maven Probe compile + Maven-native export（第一条命令）
Maven clean compile
legacy effective POM / compiler / artifact-type metadata discovery
Probe source roots + compile classpath + reactor outputs validation
BuildWorldSnapshot freeze
private source materialization
JDT FULL BUILD
class structural comparison
Worker shutdown
```

终端不会打印 Maven 全量日志。详细 Maven/JDT 证据在本机 attempt；可分享报告只包含
脱敏统计和 SHA-256。

当前是有意保留的混合模型：

```text
source roots / compile classpath / reactor outputs
    <- Maven Probe（权威）

source/target/encoding/compiler config/processor declaration/artifact type
    <- effective POM + dependency metadata（暂时）
```

Runner 会把 effective encoding 显式传给 Worker，并要求 READY 同时回报：

```text
source_encoding_requested
source_encoding_requested_canonical
source_encoding_effective
source_encoding_verified
```

raw requested 必须与 Build World 一致，Java canonical 必须与 Eclipse effective 一致，
且 `verified=true`；否则必须在 FULL 前停止。encoding 不能只停留在 Snapshot 或 JDT
compiler options 中，还必须 materialize 到 Eclipse Resources source folder。

因此这轮能验证“基础 Maven Build World 是否忠实”，但不能宣称 Maven compiler invocation
已被 100% 重建。

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

同时必须检查：

```text
build_world_provider.source_roots = maven_probe_v1
build_world_provider.compile_classpath = maven_probe_v1
build_world_provider.reactor_outputs = maven_probe_v1
build_world_provider.compiler_configuration = legacy_effective_pom
build_world_provider.hybrid_model = true
build_world_provider.probe_implementation_id 非空
```

若前三项不是 `maven_probe_v1`，这次结果不能作为本轮公司验证证据。

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

报告中的诊断摘要会优先保留 ERROR，并明确返回：

```text
error_count / warning_count / info_count
returned_error_count / returned_warning_count / returned_info_count
diagnostics_truncated
diagnostic_selection_policy=errors_first_then_warnings_then_info
```

如果本地 raw diagnostics 显示 javac 能接受、ECJ 拒绝同一段源码（例如 raw 集合与
匿名双括号初始化触发的泛型推断差异），将它记录为
`cross-compiler-source-compatibility`，不要当成 Build World classpath 缺失。

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
- `MAVEN_PROBE_IDENTITY_MISMATCH`：Maven 命中了旧 Probe。当前收紧 Processor边界的
  Probe 使用独立 `0.1.0-spike6` 坐标；保留历史 `spike1` 至 `spike5`，只清理发生冲突的精确版本，
  不要清空整个仓库；
- `MAVEN_PROBE_PROJECT_CHANGED`：Probe 后 POM 发生变化，重新跑 Probe；
- `MAVEN_PROBE_INVOCATION_MISMATCH` / `MAVEN_PROBE_SETTINGS_CHANGED`：两步使用的
  Maven、settings、本地仓库或 profile 不一致。

这些都应记录，不要通过复制旧 class、删除 Processor、升级 Lombok 或改公司 POM 强行
让实验通过。

## 7. 给 joLink 开发侧的脱敏反馈模板

不要发送公司源码、POM、settings.xml、Maven 日志、绝对路径、依赖坐标或 raw
diagnostics。建议只发：

```text
status / decision
candidate_id / JDT version
Build/Target/Worker JDK major
Maven version / Maven executable SHA
Probe implementation identity
Build World provider 各字段（不要私有路径）
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
