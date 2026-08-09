# joLink Java/Lombok 公司环境编译实验手册

状态：`内部实验 / 不属于 MCP 公共能力`

适用分支：`experiment/lombok-processor-model`

本文档写给负责执行实验的 Coding Agent。请先完整读完，再运行任何命令。

## 1. 实验目标

本次实验只回答两个问题：

1. joLink 能否准确复现目标项目的 Maven、JDK、编译参数、依赖和
   Lombok Annotation Processor 模型？
2. 在完全相同且被冻结的输入下，直接全模块 `javac` 是否能生成与
   fresh Maven compile 完全一致的 class，并且明显更快？

完整证据链是：

```text
私有复制项目，且不复制 target/build 输出
→ 解析 IDEA 使用的 JDK、Maven、settings、local repository 和 profiles
→ 解析 effective POM、compile classpath 和 Lombok Processor
→ fresh Maven compile，作为 baseline
→ module_full_javac 第一次
→ module_full_javac 第二次
→ 比较 class 集合和每个 class 的 SHA-256
→ 输出阶段耗时
```

成功标准不是“`javac` 返回 0”，而是：

```text
direct javac A == direct javac B == fresh Maven baseline
```

## 2. 本次实验明确不做什么

实验不会：

- 修改用户项目的源码、POM 或 IDEA 配置；
- 修改用户项目现有的 `target/classes`；
- 启动、停止或重启业务 JVM；
- attach JDWP 或执行 HotSwap；
- 把实验 class 发布回项目；
- 自动修改 Maven 配置以“帮助实验跑通”；
- 自动将失败降级成另一种编译策略；
- 宣称一次实验结果已经等于产品级支持。

实验会执行 Maven 和 `javac` 子进程。它们受到进程生命周期监督，但不是
OS 安全沙箱。因此只能对操作者已经信任的公司源码和 Maven 配置执行。

## 3. 给执行 Agent 的强制约束

执行 Agent 必须遵守以下规则：

1. 禁止修改业务项目源码、`pom.xml`、`.idea`、`lombok.config`、
   `settings.xml` 或环境变量持久配置。
2. 禁止为了通过 Probe 而升级 Lombok、Maven Compiler Plugin、JDK 或 Maven。
3. 禁止执行 `mvn clean/package/install/deploy` 作为额外补救动作。
4. 禁止使用已有 `target/classes` 代替 fresh baseline。
5. 禁止在实验运行期间触发 IDEA Build、Maven Build、Git 切换或源码编辑。
6. Probe 被拒绝时立即停止，不得绕过错误继续完整实验。
7. 完整实验返回 `requires_review` 时，不得描述为“验证通过”。
8. 不得把私有 attempt 目录、完整源码快照、Maven 日志或原始结果上传到外部。
9. 报告必须区分：事实、推断和下一步建议。
10. 实验结束后不要自动删除 attempt 目录；先让用户确认是否保留证据。

## 4. 当前 P0 支持边界

目标项目必须满足：

- 一个 standalone Maven `jar` 模块；
- 标准 `src/main/java`；
- 标准 `target/classes`；
- 使用 `javac`；
- Annotation Processor 只有可验证的 Lombok；
- Processor、依赖、编译参数和 Lombok 配置都能被冻结；
- 没有 joLink 尚未建模的源码生成、AspectJ、class enhancer 或编译后变换；
- 没有外部、环境变量、归档或越过项目边界的 `lombok.config` import。

以下情况被拒绝是正常实验结果，不代表 joLink 或项目存在 Bug：

- Maven Reactor 多模块；
- 自定义输出目录；
- MapStruct、自定义 Processor 或混合 Processor；
- AspectJ、字节码增强、代码生成插件；
- ECJ、自定义 compiler、forked compiler；
- 无法验证的 profile、toolchain、extension 或 compiler argument；
- 非标准/外部 Lombok 配置图。

## 5. 敏感信息规则

attempt 目录会包含私有项目快照和编译产物，属于公司内部数据。

不得向外部发送：

- attempt 目录或其压缩包；
- 源码、POM、`settings.xml`；
- Maven 私服地址、用户名、密码、Token；
- 私有仓库、本地仓库和绝对路径；
- Maven/Javac 完整日志；
- 内部包名、类名、模块名、组织名、客户名或业务数据。

原始 JSON 也可能包含相对配置路径和 mismatch class 名称，不能直接粘贴到外部。
对外报告时必须替换为：

```text
C:\真实项目路径                → <PROJECT_PATH>
E:\真实JDK路径                 → <JAVA_HOME>
内部 artifactId                → <MODULE>
com.company.xxx.SomeService    → <INTERNAL_CLASS_1>
公司私服 URL                    → <MAVEN_REPOSITORY>
```

允许保留：版本号、数量、耗时、布尔状态、错误码和脱敏后的原因分类。

## 6. 实验前环境发现

以下命令以 Windows PowerShell 为例。不要在 CMD 中直接复制 PowerShell 语法。

先定义四个路径。必须替换占位符，不要把占位符原样执行：

```powershell
$JoLinkRepo = "C:\path\to\jolink-runtime"
$ProjectPath = "C:\path\to\large-java-service"
$JavaHome = "C:\path\to\idea-project-jdk8"
$MavenExe = "C:\path\to\apache-maven\bin\mvn.cmd"
```

选择依据：

- `$ProjectPath`：目标项目包含 `pom.xml` 的根目录；
- `$JavaHome`：IDEA 实际用于项目/Maven Runner 的 JDK，不是随便找到的 `java`；
- `$MavenExe`：IDEA 实际使用的 `mvn.cmd`；
- joLink 会尽力从项目 `.idea` 配置读取 Maven `settings.xml`、本地仓库和
  active profiles。若它们没有保存在可读取的 IDEA 项目配置中，不要猜测或改
  POM，应在报告中说明。

验证路径：

```powershell
$ErrorActionPreference = "Stop"

if (!(Test-Path -LiteralPath $JoLinkRepo -PathType Container)) {
    throw "joLink source directory does not exist."
}
if (!(Test-Path -LiteralPath (Join-Path $ProjectPath "pom.xml") -PathType Leaf)) {
    throw "Project pom.xml does not exist."
}
if (!(Test-Path -LiteralPath (Join-Path $JavaHome "bin\java.exe") -PathType Leaf)) {
    throw "JAVA_HOME does not contain java.exe."
}
if (!(Test-Path -LiteralPath (Join-Path $JavaHome "bin\javac.exe") -PathType Leaf)) {
    throw "JAVA_HOME does not contain javac.exe."
}
if (!(Test-Path -LiteralPath $MavenExe -PathType Leaf)) {
    throw "Maven executable does not exist."
}
```

记录工具版本。只改变当前 PowerShell 进程，不修改系统环境变量：

```powershell
$PreviousJavaHome = $env:JAVA_HOME
$PreviousPath = $env:Path
try {
    $env:JAVA_HOME = $JavaHome
    $env:Path = "$(Join-Path $JavaHome 'bin');$PreviousPath"
    & (Join-Path $JavaHome "bin\java.exe") -version
    & (Join-Path $JavaHome "bin\javac.exe") -version
    & $MavenExe -version
    & uv --version
} finally {
    $env:JAVA_HOME = $PreviousJavaHome
    $env:Path = $PreviousPath
}
```

报告中记录：

- Windows 大版本；
- Java 与 Javac 版本；
- Maven 版本；
- Python 与 uv 版本；
- 项目是否单模块；
- 当前项目是否存在未提交修改，只记录 `yes/no`，不要粘贴文件名。

不要输出用户名、机器名、绝对路径或 Maven 私服配置。

## 7. 准备 joLink 实验分支

本实验必须从 joLink 源码分支运行，不能使用当前 PyPI Alpha 包代替。

```powershell
Push-Location $JoLinkRepo
try {
    git fetch origin
    git switch experiment/lombok-processor-model
    git pull --ff-only

    $Branch = git branch --show-current
    if ($Branch -ne "experiment/lombok-processor-model") {
        throw "Wrong joLink branch: $Branch"
    }

    $JoLinkChanges = git status --porcelain
    if ($JoLinkChanges) {
        throw "joLink experiment checkout is not clean."
    }

    uv sync --extra dev --locked
} finally {
    Pop-Location
}
```

如果公司网络导致 `git fetch` 或 `uv sync` 失败，停止并报告网络/依赖准备失败。
不要切换到其他分支，也不要临时修改锁文件。

## 8. 运行前稳定性检查

实验需要稳定的源码和构建输出观察窗口：

1. 停止正在运行的 IDEA Build 和 Maven Build；
2. 关闭 IDEA 的自动构建/保存触发编译，或保证实验期间不会触发；
3. 不要在实验过程中编辑源码、切换 Git 分支或更新依赖；
4. 最好停止本地业务 JVM，为计时释放 CPU、内存和磁盘 IO；
5. 确认系统盘有足够空间保存项目快照、Maven baseline 和两份 direct class；
6. attempt 目录必须位于项目目录之外，也不能是项目的父目录；
7. attempt 目录必须尚不存在，每次运行使用新目录。

可以查看是否存在构建进程，但不要自动结束任何未知进程：

```powershell
Get-Process java, javac, mvn -ErrorAction SilentlyContinue |
    Select-Object ProcessName, Id
```

发现不确定的进程时，询问用户，不要直接 `Stop-Process`。

## 9. 第一阶段：Probe Only

Probe 会执行必要的 Maven metadata/Processor 解析，但不会执行 Maven compile
baseline，也不会执行 direct javac。

创建本地报告目录和全新的 attempt 路径：

```powershell
$ReportRoot = Join-Path $env:LOCALAPPDATA "jolink-runtime\experiment-reports"
$AttemptParent = Join-Path $env:LOCALAPPDATA "jolink-runtime\experiment-attempts"
New-Item -ItemType Directory -Force -Path $ReportRoot | Out-Null
New-Item -ItemType Directory -Force -Path $AttemptParent | Out-Null

$ProbeStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$ProbeAttempt = Join-Path $AttemptParent "probe-$ProbeStamp"
$ProbeReport = Join-Path $ReportRoot "probe-$ProbeStamp.json"
$ProbeStderr = Join-Path $ReportRoot "probe-$ProbeStamp.stderr.log"

if (Test-Path -LiteralPath $ProbeAttempt) {
    throw "Probe attempt directory must not already exist."
}
```

执行 Probe：

```powershell
Push-Location $JoLinkRepo
try {
    & uv run python -m jolink_runtime.experiments.compile `
        --project-path $ProjectPath `
        --strategy module_full_javac `
        --java-home $JavaHome `
        --maven $MavenExe `
        --attempt-root $ProbeAttempt `
        --metadata-timeout-seconds 600 `
        --probe-only `
        2> $ProbeStderr |
        Tee-Object -FilePath $ProbeReport
    $ProbeExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
```

解析结果：

```powershell
if (!(Test-Path -LiteralPath $ProbeReport -PathType Leaf)) {
    throw "Probe did not produce a JSON report. Inspect local stderr only."
}

$Probe = Get-Content -LiteralPath $ProbeReport -Raw | ConvertFrom-Json

$Probe | Select-Object `
    ok, status, verification_state, trusted_for_product_decision, `
    maven_baseline_executed, direct_javac_executed, `
    target_outputs_modified, runtime_jdwp_touched
```

Probe 合格条件必须全部满足：

```text
进程退出码                         = 0
ok                                 = true
status                             = probe_ready
verification_state                 = model_resolved
trusted_for_product_decision       = false
maven_baseline_executed            = false
direct_javac_executed              = false
target_outputs_modified            = false
runtime_jdwp_touched               = false
```

`trusted_for_product_decision=false` 是 Probe 的正确结果，不是失败。Probe 只证明
模型可解析，不证明编译一致。

如果任意条件不满足：

1. 停止，不执行完整实验；
2. 保存 Probe JSON、stderr 和私有 attempt；
3. 只在公司本地查看 stderr；
4. 向用户返回脱敏错误码、阶段和建议；
5. 不得通过修改项目配置继续尝试。

## 10. 第二阶段：完整 Maven vs Direct Javac 实验

只有 Probe 全部合格时才能继续。

该阶段可能运行较久。对于编译需要 2～3 分钟的大项目，预计完整过程包含多次
Maven metadata、一次 fresh Maven compile 和两次 direct javac。Agent 不得因为
几分钟没有新输出就主动取消。

创建新的 attempt，禁止复用 Probe attempt：

```powershell
$FullStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$FullAttempt = Join-Path $AttemptParent "full-$FullStamp"
$FullReport = Join-Path $ReportRoot "full-$FullStamp.json"
$FullStderr = Join-Path $ReportRoot "full-$FullStamp.stderr.log"

if (Test-Path -LiteralPath $FullAttempt) {
    throw "Full attempt directory must not already exist."
}
```

执行完整实验：

```powershell
Push-Location $JoLinkRepo
try {
    & uv run python -m jolink_runtime.experiments.compile `
        --project-path $ProjectPath `
        --strategy module_full_javac `
        --java-home $JavaHome `
        --maven $MavenExe `
        --attempt-root $FullAttempt `
        --repeat 2 `
        --timeout-seconds 1200 `
        --maven-baseline-timeout-seconds 1800 `
        --metadata-timeout-seconds 600 `
        2> $FullStderr |
        Tee-Object -FilePath $FullReport
    $FullExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
```

读取结果：

```powershell
if (!(Test-Path -LiteralPath $FullReport -PathType Leaf)) {
    throw "Full experiment did not produce a JSON report. Inspect local stderr only."
}

$Full = Get-Content -LiteralPath $FullReport -Raw | ConvertFrom-Json

$Full | Select-Object `
    ok, status, verification_state, trusted_for_product_decision, `
    target_outputs_modified, runtime_jdwp_touched
```

## 11. 如何解释完整结果

### 11.1 强证据成功

只有以下条件全部满足，才可以写“本次快照 verified exact”：

```text
进程退出码                                      = 0
ok                                              = true
status                                          = completed
verification_state                              = verified_exact
trusted_for_product_decision                    = true
determinism.repeat_count                        = 2
determinism.exact_match                         = true
maven_baseline.comparison.exact_match           = true
target_outputs_modified                         = false
runtime_jdwp_touched                            = false
```

它证明的是：

> 在本次被冻结的项目快照、JDK、Maven、依赖、Processor 和配置输入下，两次
> direct javac 与 fresh Maven compile 生成了相同 class 集合和 SHA-256。

它不证明：

- 所有公司项目都支持；
- 后续任意源码改动仍然一致；
- HotSwap 一定兼容；
- 业务行为一定正确；
- joLink 已经可以删除 Maven fallback。

### 11.2 `requires_review`

以下结果不是验证成功：

```text
verification_state = requires_review
trusted_for_product_decision = false
```

需要记录：

- Direct A 与 Direct B 是否一致；
- Maven 与 Direct 是否 class-set 一致；
- missing/unexpected/changed class 数量；
- 不一致发生在普通 class、Lombok生成 class、匿名/内部 class 还是其他类别；
- 不得在对外报告中暴露真实 class 名。

`requires_review` 只表示本次模型不具备自动提升证据，不能直接推断是 Maven、
Lombok、javac 或 joLink 的哪一方存在 Bug。

### 11.3 `ok=false`

`ok=false` 是结构化拒绝或执行失败。遵循返回中的：

```text
error_code
retryable
suggested_next_step
```

但 suggested next step 不能覆盖本文档的“禁止修改业务项目”规则。

常见结果：

| error_code | 含义 | 本轮动作 |
| --- | --- | --- |
| `COMPILE_EXPERIMENT_UNSUPPORTED` | 项目超出 standalone jar P0 | 记录并停止 |
| `COMPILER_ENVIRONMENT_UNVERIFIED` | 环境参数会影响编译模型 | 记录变量名类别并停止 |
| `COMPILE_MODEL_UNAVAILABLE` | Maven/Compiler/Transform 模型不可证明 | 记录阶段并停止 |
| `ANNOTATION_PROCESSING_UNVERIFIED` | Processor 集合或模式不可证明 | 记录 Processor 数量/类别并停止 |
| `PROCESSOR_PATH_UNRESOLVED` | 无法得到准确 Lombok Processor | 本地检查私有日志，脱敏报告 |
| `LOMBOK_CONFIG_UNVERIFIED` | Lombok 配置图超出冻结能力 | 记录配置类型，禁止上传内容 |
| `MAVEN_BASELINE_FAILED` | 私有 fresh Maven compile 失败 | 对比 IDEA 构建环境，禁止改 POM |
| `COMPILE_MODEL_CHANGED_DURING_BASELINE` | baseline 前后输入或模型漂移 | 停止外部构建/编辑后再由用户决定是否重跑 |
| `JAVAC_EXECUTION_FAILED` | direct javac 无法复现构建 | 保留日志，继续使用 Maven |
| `JAVAC_TIMEOUT` | direct javac 超时 | 记录耗时，由用户决定是否增加超时 |
| `ORIGINAL_OUTPUT_CHANGED_DURING_EXPERIMENT` | 外部进程改动了原 target | 本次证据作废 |
| `PROCESS_CLEANUP_UNSETTLED` | 子进程未完全结束 | 先确认残留进程，不自动重试 |

## 12. 性能数据提取

只有结果结构完整时执行：

```powershell
$MavenMs = [double]$Full.maven_baseline.duration_ms
$DirectRuns = @($Full.attempts | ForEach-Object {
    [double]$_.javac_duration_ms
})
$DirectAverageMs = ($DirectRuns | Measure-Object -Average).Average
$Speedup = if ($DirectAverageMs -gt 0) {
    [math]::Round($MavenMs / $DirectAverageMs, 2)
} else {
    $null
}

[pscustomobject]@{
    MavenBaselineMs = $MavenMs
    DirectRun1Ms = $DirectRuns[0]
    DirectRun2Ms = $DirectRuns[1]
    DirectAverageMs = [math]::Round($DirectAverageMs, 3)
    MavenToDirectRatio = $Speedup
    WorkspaceSnapshotMs = $Full.durations_ms.workspace_snapshot
    MetadataResolutionMs = $Full.durations_ms.metadata_resolution
    ProcessorResolutionMs = $Full.durations_ms.processor_resolution
    ModelValidationMs = $Full.durations_ms.model_validation
    ArtifactFreezeMs = $Full.durations_ms.artifact_freeze
    DirectOverheadMs = $Full.durations_ms.direct_attempt_overhead
    ComparisonMs = $Full.durations_ms.comparison
    TotalMs = $Full.durations_ms.total
}
```

不要只报告倍率。必须同时报告：

- Maven baseline 绝对耗时；
- 每次 direct javac 绝对耗时；
- 两次 direct 的差异；
- metadata/model/fingerprint/freeze 开销；
- 总耗时；
- class 数量和源码数量。

IDEA `Ctrl+F9` 的耗时可以作为另一列人工参考，但它可能复用缓存和旧输出，不是
fresh Maven baseline，也不能替代 class SHA 一致性证据。

## 13. 原项目未被修改的确认

实验结果必须满足：

```text
target_outputs_modified = false
runtime_jdwp_touched = false
```

实验后只检查状态，不执行清理或构建：

```powershell
Push-Location $ProjectPath
try {
    $AfterStatus = git status --porcelain
    if ($AfterStatus) {
        Write-Host "Project has working-tree changes. Compare only with its pre-run status."
    } else {
        Write-Host "Project working tree is clean."
    }
} finally {
    Pop-Location
}
```

如果项目实验前本来就有修改，不能仅凭实验后的 dirty 状态认定 joLink 修改了项目；
应比较实验前后状态。不要把具体公司文件名复制到外部报告。

## 14. 给用户的脱敏报告模板

执行完成后，按照下面格式返回。不要附原始 JSON 或完整日志。

```markdown
# joLink Lombok 编译实验报告

## 环境

- OS：Windows <大版本>
- JDK/Javac：<版本>
- Maven：<版本>
- Maven Compiler Plugin：<版本或 unavailable>
- Lombok：<版本或 unavailable>
- 项目形态：single-module / multi-module
- packaging：jar / other
- 项目运行前有未提交修改：yes / no
- 实验期间发现外部构建活动：yes / no

## Probe

- exit_code：
- ok：
- status：
- verification_state：
- source_count：
- compile_classpath_entry_count：
- Processor mode：
- Processor artifact count：
- Lombok version：
- config file count：
- warnings：仅写脱敏后的类别
- 结论：probe_ready / rejected

## 完整实验

- exit_code：
- ok：
- status：
- verification_state：
- trusted_for_product_decision：
- generated class count：
- Direct A == Direct B：
- Maven == Direct A：
- missing class count：
- unexpected class count：
- changed class count：
- target_outputs_modified：
- runtime_jdwp_touched：

## 耗时

- workspace snapshot：
- metadata resolution：
- Processor resolution：
- model validation：
- fresh Maven baseline：
- baseline class scan：
- artifact freeze：
- direct javac A：
- direct javac B：
- direct javac average：
- direct overhead：
- comparison：
- total：
- Maven/direct ratio：

## 事实

- 只写工具结果直接证明的事实。

## 尚未证明

- 不把 exact class match 扩大解释为业务正确或所有项目可用。

## 错误或差异

- error_code：如无则写 none
- retryable：
- mismatch 分类与数量：不得出现真实包名或类名
- 私有日志是否保留在公司本机：yes / no

## 建议

- 根据证据给出继续实验、分析差异或保持 Maven fallback 的建议。
```

## 15. 最终判定规则

执行 Agent 最终只能选择以下结论之一：

### A. Model rejected

```text
Probe 未通过，当前公司项目超出 P0 模型。
未执行完整实验，未修改项目。
```

### B. Compiles but not equivalent

```text
Direct javac 执行完成，但 determinism 或 Maven exact comparison 不成立。
不得进入 HotSwap/Fast Restart 产品接入。
```

### C. Verified exact for this snapshot

```text
两次 direct javac 与 fresh Maven baseline 的 class set 和 SHA-256 完全一致。
该证据仅绑定本次项目快照与编译环境，可以继续评估性能和下一阶段策略。
```

无论得到哪一种结论，都不要在本轮继续实现新功能或修复业务项目。先把脱敏报告
交给用户决定下一步。
