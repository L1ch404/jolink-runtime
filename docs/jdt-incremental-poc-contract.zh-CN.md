# joLink Headless JDT 增量编译 POC 契约（中文版）

契约版本：`0.1`

设计状态：`已批准进入 Phase 1A 实验`

实现状态：`A1-A9 实现证据已通过；canonical clean-worktree 重跑与 A10 待验证`

产品状态：`仅实验 / 不改变 MCP 或 Runtime 行为`

英文版：[`jdt-incremental-poc-contract.md`](jdt-incremental-poc-contract.md)

本文与同一提交中的英文版是同步契约。两者出现语义差异时，应把它
当作文档缺陷修正，不能选择约束较宽的一版继续实验。

本契约定义 joLink 在把 Headless Eclipse JDT Java Builder 视为可行的增量
编译 Worker 前，必须取得哪些证据。它不预先决定 JDT 就是生产架构，也不
改变当前公开的 `update`、Schema、Annotation Processor 拒绝策略和
HotSwap 安全边界。

direct-javac 研究成果继续保留但冻结。它的 Maven 模型、指纹、私有
staging、进程监管和 fail-closed 边界可以复用，但“完整复刻 Maven 到
direct-javac”不再是当前主线。参见
[`java-compile-strategy-roadmap.md`](java-compile-strategy-roadmap.md)。

## 要验证的决策假设

```text
Maven（未来阶段）
    构造带版本的 Build World

Headless JDT Java Builder
    进行一次私有 full build
    保留依赖关系和 last-build state
    对普通 Java delta 做增量构建

joLink（未来阶段）
    使过期 Build World 失效
    监管 Compiler Worker
    选择 HotSwap 或 restart
    验证实际运行的应用
```

“ECJ 能编译 Java”不代表 POC 成功。必须证明 joLink 能以可接受的工程与
资源成本隔离、控制、测量、停止并持续维护真正的 JDT 增量项目 Builder。

## 固定术语

- `Worker JDK`：启动 Equinox 和 Compiler Worker 的 Java Runtime。
- `Source compliance`：JDT 接受的 Java 语言级别。
- `Class target`：JDT 输出的 class 字节码级别。
- `TargetSystemLibrarySnapshot`：由一个精确 JDK 8 安装提供的、有顺序、
  有状态和内容指纹的编译器平台库视图。它包含 bootstrap 与 extension
  mechanism，独立于 Worker JDK 定义编译器能看到的平台 API 世界。
- `Target JVM`：未来可能运行产物的 JVM；Phase 1 不启动它。
- `Java Builder`：以 `org.eclipse.jdt.core.javabuilder` 注册、运行在
  Eclipse Workspace/Core Resources 中的 Builder。
- `真正的增量构建`：使用 Java Builder 既有 build state 和 resource
  delta 执行 `IProject.build(INCREMENTAL_BUILD, ...)` 或等价 Workspace
  build。Python 自己挑源文件再调用 ECJ Batch 不属于本契约的增量构建。
- `Workspace lineage`：一个私有 workspace，以及它的编译器/项目身份、持久 JDT
  build state、ownership 历史和保存状态 manifest。`workspace_lineage_id` 可跨
  source edit、build、Worker 优雅重启和已验证 offline source delta 保持不变。
  cancelled/aborted build 可以使 lineage 进入 `RECOVERY_REQUIRED`，但不能改写
  那次 build 的不可变结果。
- `Build generation`：由 `build_generation_id` 标识的一次不可变 Workspace build
  operation，包含精确 source-tree fingerprint、request/operation identity、operation
  result、可为空的 compiler result、diagnostic 和观察到的输出状态，覆盖 `CLEAN`、
  `FULL` 与 `INCREMENTAL`。终态只能是 `SUCCEEDED`、`FAILED_COMPILE`、
  `CANCELLED` 或 `ABORTED`，一旦进入就永不改变。不同 build generation 的结果
  不能混用。
- `Publication transaction`：未来产品接入时的发布边界。Runtime 在 candidate
  build generation 编译和验证期间继续使用已提交的 last-good build generation。
  既有字段 `generation_publishable` 属于 build generation，目前只是逻辑发布门禁，
  并不会把 JDT 可变的 `bin` 目录变成物理 last-good 存储。
- `Clean-full oracle`：从同一冻结输入、使用同一锁定 JDT 栈创建的新私有
  workspace lineage 和 full-build generation，是判断增量结果正确性的基准。
- `Evidence candidate`：由精确的 Worker JDK、Equinox/bundle lock、JDT、
  `TargetSystemLibrarySnapshot`、编译器/项目选项以及 instrumentation
  artifact/config 共同构成的实验栈。任一项变化都会形成新的 candidate。

## 范围

Phase 1 分成两个独立 gate：

```text
Phase 1A
    纯 Java fixture
    真正的 Headless Java Builder
    full/incremental 正确性
    生命周期和资源测量

Phase 1B
    同一个 Worker 模型
    Java 8 source/target
    精确 Lombok 1.18.20
    Lombok 生成成员及依赖传播正确性
```

本契约只批准 Phase 1A。只有记录 Phase 1A Go 后才能开始 Phase 1B。两者
都通过也只允许评审 Phase 2，不自动授权实现。

Phase 2 才引入 Maven Bootstrap 和真实项目 Build World；Phase 3 才引入
Runtime launch、class-shape 比较、JDWP HotSwap、Fast Restart、readiness
和 HTTP 验证。

## 明确不做

第一版 POC 不得：

- 新增或修改 MCP Tool、action、Schema、返回或 description；
- 修改生产 `run`、`update`、restart、JDWP 或 class reader；
- 运行 Maven、导入 Maven/IDEA 项目或检查公司项目；
- 写入用户源码树、`target`、IDE workspace 或 Maven 本地仓库；
- 使用 JDT LS 作为 Worker；
- 为图方便加入 LSP4J、M2E、Buildship、JDT UI、Eclipse UI、补全、导航、
  重构、语言服务器或 debug bundle；
- 实现资源处理、Maven filtering、generated sources、MapStruct、QueryDSL、
  Dagger、Spring metadata 或任意 JSR-269；
- 实现 HotSwap、增强 HotSwap、Fast Restart 或 HTTP 验证；
- 并行加入“ECJ BatchCompiler + joLink 自建依赖图”第二套实现；
- 宣称兼容任意 Eclipse、JDT、Lombok、JDK 或 OS 版本。

JDT LS 可以作为对照基线单独测量，但不能作为 fallback，也不能满足 Phase 1。

## 依赖与版本边界

Worker 必须使用能够运行真正 Java Builder 的最小已证明 Equinox/JDT bundle
集合：

```text
Equinox launcher 和 OSGi runtime
Eclipse core runtime/jobs/filesystem/resources
JDT Core 与 Java project nature/builder
这些能力严格需要的传递 bundle
```

实际集合必须通过 bundle requirement 解析得出，不能复制整个 Eclipse IDE，
也不能靠“删 JAR 直到还能启动”决定。Bootstrap discovery 阶段允许调整
bundle，但这些运行不计入 Phase 1 证据。

第一次产生证据的 Phase 1A 运行之前，candidate 必须提交 artifact/config
lock。每个 bundle 和 launcher artifact 都要记录 symbolic name、精确版本、
来源 repository/release、SHA-256、license identity、压缩/安装字节数、
start level、activation policy 和 Equinox application/config identity。
同时必须锁定每个 selected bundle 的 `osgi.ee` requirement，以及由 Worker
JDK 提供并实际匹配的 execution-environment capability。

运行时不允许 floating `latest`、version range、snapshot 或静默替换
artifact。Worker 构建 fixture 时不能下载依赖。
若 selected bundle 的 mandatory `osgi.ee` filter 无法解析，或 locked Worker
JDK 无法满足，resolver 必须 fail closed。Equinox 碰巧能够启动不构成 EE
证据。

Phase 1 决策前最多评估两个已锁定 evidence candidate；尚未成为 candidate
的 bootstrap 尝试不占名额：

1. 可维护的当前版本 candidate，在第一次 evidence-bearing run 前锁定；
2. 必要时使用 Eclipse 2021-03 兼容锚点。其 JDT Core 为
   `3.25.0.v20210223-0522`，其余 artifact 与 Worker JDK 仍须锁定。

兼容锚点只是证据，不是自动产品选择。若 Lombok 只能在过时栈工作，结果为
`conditional`，等待维护性和安全性决策。

### Evidence candidate 血缘规则

每条 evidence-bearing 结果只能属于一个已锁定 candidate。任何进入 Phase 1B
的 candidate，都必须先在完全相同的栈上独立满足完整 Phase 1A Go。

不得跨 candidate 累加证据。不能把 candidate A 的 Phase 1A 成功和
candidate B 的 Phase 1B 成功拼成 Phase 1 Go。任何 candidate identity
重新锁定都会建立新血缘并失去此前 gate 继承，即使 fixture 未变化。

### 必须报告的版本矩阵

| 维度 | Phase 1A | Phase 1B |
| --- | --- | --- |
| Worker JDK vendor/version | candidate 精确值 | 除兼容测试外保持同一精确值 |
| Equinox/Platform | 精确锁定值 | 精确锁定值 |
| JDT Core | 精确锁定值 | 精确锁定值 |
| Source compliance | `1.8` | `1.8` |
| Class target | `1.8` | `1.8` |
| Target system library | 精确 JDK 8 库指纹 | 精确 JDK 8 库指纹 |
| Target JVM | 不启动；兼容目标 Java 8 | 不启动；兼容目标 Java 8 |
| Lombok | 无 | 精确 `1.18.20` |

Worker JDK 不能证明 source/bytecode 兼容性；`target=1.8` 也不能证明
Lombok 1.18.20 能在所选 Worker JDK/JDT 进程中运行。

### TargetSystemLibrarySnapshot

Phase 1 使用最小 JDT Core classpath，不使用 `org.eclipse.jdt.launching`
及其 `JRE_CONTAINER`、VM install model 和传递依赖。

一个受限 helper 必须从精确 target JDK 8 安装本身推导 javac 实际使用的
platform class path，并记录：

```text
Java vendor/version 与 JDK-home identity
sun.boot.class.path 声明的有序 bootstrap entries
java.ext.dirs 声明的有序 extension directories
java.endorsed.dirs provenance
target javac 报告的有序 effective PLATFORM_CLASS_PATH
entry 状态：PRESENT 或 ABSENT
entry 类型：archive、class directory 或 absent placeholder
每个 advertised entry 的 path identity 指纹
每个 present archive 的 SHA-256
每个 present class directory 的确定性内容指纹
可选 runtime Extension ClassLoader URLs，仅作交叉验证
最终 materialize 到 JDT project 的精确 effective 顺序
system_library_discovery_method
```

不能简单排序 JRE 目录里的全部 JAR，不能依赖目录布局猜测，也不能从 Worker
JDK 发现系统库。runtime extension loader URLs 可以交叉验证 compiler view，
但不能代替 compiler view 成为规范来源。

若 exact target JDK 持续将一个 advertised entry 报告为不存在，它是允许的
placeholder，并以 `state=ABSENT` 留在 snapshot 中，但不 materialize 到 JDT。
javac 可能在 `PLATFORM_CLASS_PATH` 中继续保留同一个 absent placeholder；它仍是
证据，但同样不 materialize。若 compiler view 出现无法对应 advertised absent
bootstrap placeholder 的缺失项，则视为 unresolved 并禁止复用 workspace lineage。
`PRESENT` 与 `ABSENT` 的双向状态变化、内容指纹变化，或 present compiler
entry 不可读，也会禁止复用 workspace lineage。所有 present javac platform entries
按 javac 原始顺序转换为 `JavaCore.newLibraryEntry(...)`。不得 fallback 到
Worker JDK；公开报告只暴露指纹和 target-JDK identity，不暴露敏感绝对路径。

Phase 1A 只记录 endorsed-directory provenance，不建模实际存在 endorsed
archive 的 target JDK。若发现有效 endorsed archive，candidate 进入
conditional 并重新 review，不允许静默近似。

以后若引入 `org.eclipse.jdt.launching`，必须修改契约，不能为让 Phase 1
通过而静默加入。

## 隔离与生命周期契约

POC Worker 是受监管的独立 JVM，不加载进 Python MCP 进程或目标应用 JVM。

每次运行必须：

- 在 fixture checkout 外创建新的私有 attempt root；
- 创建私有 Eclipse configuration area 和 workspace data area；
- 绑定精确 `TargetSystemLibrarySnapshot`，而不是 Worker JDK API；
- 复制 fixture 输入，或证明 build 不能修改 fixture checkout；
- class、generated file、marker、日志、缓存、workspace state 全写私有目录；
- 禁用 automatic build，只显式调用 build；
- 同时只允许一个命令修改一个 workspace；
- incremental build 前 refresh Eclipse resource model；
- startup/build/cancel/shutdown 都有有界 deadline；
- 取消或超时先协作取消，再终止身份已验证的 owned process tree；
- cancelled/timed-out build generation 不可发布，并使 workspace lineage 进入
  `RECOVERY_REQUIRED`；infrastructure abort 或 crash 也采用相同处理；
- 不遗留 Worker、Equinox、compiler 或 fixture application 进程；
- Phase 1 不 attach JDWP、不启动 fixture 应用；
- 只有 runner 明确要求时才保留失败 attempt。

若用 stdio 控制 Worker，stdout 只能有协议帧，诊断写 stderr 或私有日志。
公开结果不得包含原始环境值、用户 home、仓库凭据或无限编译输出。

优雅 shutdown 前必须请求 Workspace save。每条已保存的 workspace lineage 都有
manifest 和由 Runner 独占的 clean-shutdown marker。manifest 指纹化 Worker、bundle、
compiler/project model、system library、保存时源码状态和 classpath。Worker 只负责
返回 `SAVE_ACK`，绝不能写入或续期 clean marker。启动 Worker 前，Runner 必须原子
消费旧 marker，使 workspace 进入 owned/dirty；只有收到 `SAVE_ACK`、Worker 以 0
退出且完整 identity-bound process tree 已确认 settled 后，Runner 才能发布新 marker。
发布使用临时文件、平台支持时 flush/file sync、同文件系统 atomic replace，以及有意义
且平台支持时同步父目录；任何不支持或失败都必须明确报告，不能夸大 durability。

marker 缺失、save 失败、异常退出、process tree 未 settled，或不变 compiler/project
fingerprint 变化都会使 state 失效。Worker 停止期间发生的源码变化本身不是失效条件，
而是未信任的 offline delta：它必须被限制在 owned source root，refresh 到重开的
resource model，并由 build-kind、增量观测和 clean-full oracle 证明。同一 workspace
lineage 同时只能被一个 Worker 拥有。空闲退出先给 5 秒 settlement budget，之后只能
终止身份已验证的 owned process tree。

跨进程 state 恢复是测量项：

```text
preferred
    restart 恢复 state，下一次合格 edit 仍 incremental

acceptable for continued evaluation
    state 在 Worker 存活时可靠；重开需要一次私有 full build

no-go
    同一健康 Worker 反复丢 state，或普通 delta 静默变 full
```

## Phase 1A Fixture 与 Case

纯 Java fixture 必须很小，不用 Maven、Gradle、Lombok、annotation
processing、resources、modules 或外部依赖，至少包含：

```text
Api.java
    Service 使用的 public API
    一个被下游使用的 compile-time constant
Service.java
    实现或调用 Api
Application.java
    调用 Service
```

### A1 — Full build

创建带 `JavaCore.NATURE_ID` 的 project；build spec 必须恰好有一个
`JavaCore.BUILDER_ID`；配置 source/output 和系统库；用真正 Java Builder
执行 `FULL_BUILD`；不允许 ERROR marker/build-path error；完整输出 class
family；class major version 必须为 Java 8。

引用 `List.of` 等 Java 9+ API 的反例必须失败。仅 major version 52 不能
证明 Java 8 API 兼容。

Instrumentation OFF/ON 各跑一次 A1，完整 class 集合、SHA-256 和 diagnostics
必须完全一致，才能证明观测不会干扰编译。

### A2 — No-op incremental

不改输入后执行 `INCREMENTAL_BUILD`；必须看到空 delta，或明确记录
`build_outcome=NO_COMPILE`（没有 compilation callback 且没有 compiled
unit），输出集合与 SHA-256 不变。没有 callback 直接表明有效 build kind
时，`actual_build_kind` 必须保持不可用。耗时快不是 no-op 证据。

某些 JDT 版本在真实 no-op 时既不调用 `buildStarting()`，也不调用
`buildFinished()`。Runner 必须如实记录，不能伪造 callback。此时还必须证明
`project.build()` 正常返回、同一个 Worker 中启用的 participant 已观察到相邻
编译，并且 source unit、diagnostic 和 output SHA 都没有变化。
仅凭 participant 没有 callback，还不能直接证明 Java Builder 没有被调用；
在 builder 或 resource-delta instrumentation 真正观察到之前，报告不得声明该事实。

### A3 — 叶子方法体修改

只改 `Application.java` 方法体且不改 schema。增量输出必须等于同输入的
clean-full oracle。

### A4 — 上游方法体修改

只改 `Api.java` 实现体，不改 public schema。结果正确且不能无必要
clean/full。本契约不预设未变化 consumer 一定需要重编。

### A5 — 依赖传播修改

分别执行 public API 变化和 compile-time constant 变化。受影响下游或
diagnostics 必须与 clean-full oracle 一致。

首次 macOS 证据使用了专门的依赖 fixture，`Service.java` 和
`Application.java` 都直接依赖 `Api`。仅修改上游方法签名，以及另一轮
仅修改 compile-time constant 时，真实 Java Builder 都增量编译了三个
受影响单元；class family 和 diagnostics 分别与独立 clean-full oracle
完全一致。这证明的是该 fixture 的 A5 依赖传播行为，尚不能外推到任意企业项目依赖图。

### A6 — 删除与重命名

分别删除、重命名一个 source/type。过期 top-level、inner、anonymous 和
synthetic class family 都必须清除，输出和 diagnostics 等于 clean-full。

首次 macOS A6 证据 fixture 包含 top-level class、member class、anonymous
class、local class 和 generic override。Runner 从 full-build 实际输出中动态发现
该 candidate 的 class family，并直接验证 override class 中包含
`ACC_BRIDGE | ACC_SYNTHETIC` 方法。删除 source 后，完整的已发现
class family 都被清除，
不相关 class 保持不变。独立的 source/type 重命名实验同样清除了全部旧
family，并生成完整新 family。两组增量输出树和 diagnostics 都与各自的
clean-full oracle 完全一致。

### A7 — 错误与恢复

引入确定性编译错误。Worker 必须返回 problem marker 的有界结构化诊断，
不能宣布 build generation 可发布，也不能把过期 class 当成功输出。修复后除非
Java Builder 自己要求 full，否则应在同一 Worker 恢复。

首次 macOS A7 证据在一个原本可编译的 class 中引入 unresolved symbol。
同一 Worker 返回包含 resource、line、character range、severity 和 message 的
有界结构化 ERROR 诊断，明确标记 build generation 不可发布，并且不暴露任何
可发布 changed class。将错误修复为另一个有效实现后，同一 Worker 完成
增量恢复、清空全部 diagnostics，并产生与独立 clean-full oracle 完全相同的
可发布 class tree。失败构建期间生成或保留的 class 只作为证据记录，绝不作为
可发布输出对外呈现。

#### 未来 Runtime 发布边界（仅记录，Phase 1 不实现）

JDT 会直接写入私有 workspace lineage 的可变输出树；失败 build 可能保留、删除或
替换其中的 class。因此 `generation_publishable=false` 是逻辑安全门禁，不是
物理 last-good output 隔离。产品 Runtime 接入时绝不能扫描当前 `bin` 后把
其中所有 class 直接 HotSwap，而必须把编译视为 publication transaction：

```text
已提交的 last-good build generation N 继续服务 Runtime
    candidate build generation N+1 编译失败 -> ABORT；N+1 的 class 一个也不发布
    candidate build generation N+1 编译成功 -> COMMIT 明确验证过的输出集合
                                            然后才允许 HotSwap 或 restart
```

物理实现可以是独立输出根、不可变快照、copy-on-commit 或以后评审确定的其他
方案，当前故意留到产品化阶段再决定。Phase 1 只记录编译器现实并执行逻辑门禁，
不宣称已经具备事务化输出存储。

### A8 — Workspace restart

成功 build 后优雅保存、停止、重启同一 workspace lineage，再做普通方法体修改。
结果是 incremental 或 required full 都必须显式且正确，禁止静默 fallback。

首次 macOS A8 证据先完成 full build，收到明确的 Workspace save 确认并协作式
关闭 Worker，然后用同一 configuration area 和 workspace lineage 启动新的
Worker 进程。重开的 Worker 接收普通方法体修改和 requested incremental build；
真实 Java Builder 报告 `actual_build_kind=INCREMENTAL`，只编译
`Application.java`、只改变 `Application.class`，最终输出树和 diagnostics 与
独立 clean-full oracle 完全一致。A8 的三个 Worker 实例全部协作式退出。该证据
只证明当前冻结 candidate 与 fixture 的状态恢复，不证明 crash recovery、指纹失效
或长期运行稳定性。

### A9 — 重复构建稳定性

至少 100 次 method-body、constant、error/recovery、delete/restore 和 no-op
循环。每个成功 incremental build generation 等于 clean-full oracle；不能 crash、
卡住、留下 stale class family 或出现无界内存趋势。

#### A9 设计状态与拆分

本设计已经冻结并批准实现。只有实现暴露出真实矛盾时才允许修改，并且必须先记录
契约修订再改变行为。A9 拆成三个相互独立的证据通道，避免 oracle Worker 和破坏性生命周期
测试污染长驻 Worker 的内存曲线：

```text
A9-S  同一 Worker 的确定性长期稳定性 workload
A9-M  针对 A9-S 的 heap / Metaspace / `process_tree_rss_sum_bytes` 测量
A9-L  协作取消、恢复、退出与进程所有权
```

三条通道使用同一锁定 candidate 与 target-system snapshot，但使用独立私有
workspace lineage。一个通道的成功不能遮盖或修复另一个通道的失败。

#### A9-S — 确定性长驻 workload

新增一个混合纯 Java fixture，包含现有 dependency、recovery 和 class-family
形态。一个 Worker 先做 full baseline，再做一个不计入趋势的 warm-up epoch，
随后做十个 measured epoch。每个 epoch 固定包含以下 11 个操作，结束时源码树
必须回到冻结 baseline：

```text
1   叶子方法体修改
2   no-op incremental 请求
3   恢复叶子方法体
4   上游方法体修改
5   恢复上游方法体
6   compile-time constant 修改
7   恢复 compile-time constant
8   引入确定性的 unresolved-symbol 错误
9   修复错误并恢复 baseline 源码
10  删除一个 source 及其完整 class family
11  恢复该 source 与 class family
```

warm-up 之后共有 110 个 measured build request。baseline full build、11/11 个
warm-up request 和 110/110 个 measured request，都必须通过各自适用的 correctness、
diagnostic、output-family 和 oracle gate。warm-up 只是不计入资源趋势计算，绝不
豁免 correctness。所有修改必须确定、有界，并且只作用于私有源码副本；每个 epoch
开始和结束的 source-tree fingerprint 必须一致。121 个 warm-up + measured 请求
必须由同一个 Worker 和 workspace 完成；中途重建 Worker 会使 A9-S 证据失效。

所有适合增量的源码编译请求都必须报告 `actual_build_kind=INCREMENTAL`；no-op 必须
明确为 `NO_COMPILE`。删除源码属于仅资源变更：它必须报告
`actual_build_kind=null`、`build_outcome=NO_COMPILE`、无 compiled unit，并在
`deleted_classes` 中返回完整被删除 class family，最终输出必须与 clean-full oracle
一致；恢复该源码时必须重新报告 `INCREMENTAL`。错误修改必须产生预期的结构化 ERROR。源码错误属于一次已完成
的 build，而不是 infrastructure abort：它发出 `BUILD_COMPLETED`，其中
`operation_kind=INCREMENTAL`、`operation_ok=true`、`compile_ok=false`；对应 build
generation 的不可变终态是 `FAILED_COMPILE` 且不可发布，但 workspace lineage 仍
保持 `READY`，允许继续做增量源码修复。修复 build 必须保持 incremental，并获得
新的 `build_generation_id`。任何静默 full fallback、卡住、意外诊断、
stale/deleted class-family 不一致或 Worker 重启都会使 A9-S 失败。

#### Oracle 策略

每个成功源码状态都必须与 clean-full oracle 比较。为了避免对重复状态机械启动
大量 oracle Worker，只允许使用下面完整 key 缓存 oracle：

```text
candidate identity
TargetSystemLibrarySnapshot fingerprint
project_model_fingerprint
精确 source-tree fingerprint
```

`project_model_fingerprint` 必须包括有序 source roots、output roots、compile
classpath 的内容/身份、Java nature、builder 身份与顺序、resource encoding，以及
effective compiler/project options。某个 key 第一次出现时，必须创建独立私有
workspace lineage、执行真实 clean full、记录完整 class SHA tree 和 diagnostics，并
协作式关闭 oracle Worker。后续周期
只能复用该精确不可变 oracle。Oracle catalog 仅在单个 attempt 内有效，并且必须
在 measured A9-S Worker 启动前预计算完成；oracle 进程不能与 measured workload
重叠，也不能信任前一次 attempt 的报告或输出作为本次 oracle。报告 cache
hit/miss；oracle Worker 内存不计入 A9-M。no-op 必须同时等于请求前输出和当前
源码 fingerprint 对应的 oracle。故意损坏的状态与 clean-full diagnostic oracle
比较，但两边输出都绝不可发布。

#### A9-M — 资源测量

A9-S 的长驻 Worker 是唯一被测主体。Runner 以不大于 100ms 的间隔采样身份绑定
的 process tree，记录 root/child RSS、child 数、采样缺口，以及每次 build 的
observed sampled RSS peak 和 sample count。机器字段 `process_tree_rss_sum_bytes`
是 root 与所有已观察 child RSS 的算术和，可能重复计算 shared page，并不是 PSS、
USS 或 unique physical memory；Phase 1 的决策区间明确使用这个可复现工程指标。
Worker 侧有界 metrics 命令记录
heap used/committed/max、Metaspace
used/committed、存在时的 Compressed Class Space used/committed、loaded-class count、
thread count 和 uptime。`class_metadata_used_bytes` 等于 Metaspace 加存在的
Compressed Class Space，但两类 pool 仍须分别展示。不存在的 pool 明确标为
`not_applicable` 或 `unavailable`，不能填 0。每次 full/incremental build 前重置
相关 `MemoryPoolMXBean` peak counter，结束后记录每个 pool 的 peak；由于不同 pool
的 peak 可能发生在不同时刻，任何汇总值都必须标成 upper bound。这样无需在被测
Worker 内新增轮询线程。

warm-up 后和每个 measured epoch 后，在没有 build 活动时先采 pre-GC checkpoint，
再请求显式 GC，最多等待一秒 settlement 后采 after-request checkpoint。Worker
必须在请求前后记录所有可用 `GarbageCollectorMXBean` 的 collection count/time。
报告总是写 `gc_request_sent=true`；只有至少一个受支持的 collection count 增加时，
才能写 `gc_collection_observed=true` 并把样本命名为 `post_gc_checkpoint`；否则写
`gc_collection_observed=false`，样本只能叫 `after_gc_request_checkpoint`。不支持的
counter 保持明确 unavailable。最后一个 checkpoint 后再记录真正 idle 30 秒的
`process_tree_rss_sum_bytes`。`System.gc()` 只能写成“已请求”，不能宣称所有 collector 一定
运行或完成回收。只有至少采到预期 interval 的 95%，且不存在超过 500ms 的未解释
采样缺口，resource sampling 才算完整；否则 A9-M 必须进入 diagnostic rerun。

报告保留全部 raw checkpoint，并使用可比较的 after-request checkpoint 计算开头/
结尾 median；没有观察到 GC 时不得伪装成 post-GC。除已有 RSS/peak 绝对区间外，
最后三个 checkpoint 的 median 相比最先三个增加超过下列任一值时，首次运行必须
进入 diagnostic rerun：

```text
process_tree_rss_sum_bytes > max(64 MiB, 20%)
heap used         > max(32 MiB, 20%)
class metadata    > max(16 MiB, 20%)
```

或者最后五个 checkpoint 严格递增且总增长超过对应绝对阈值。thread count 的 tail
median 比 early median 多 4 个以上，或最后五个值严格递增且总增量大于 4，也触发
diagnostic rerun。loaded-class count 使用相同规则，但阈值为 `max(128, 10%)`。
这些只是重跑触发器，不是内存泄漏结论。一次噪声必须用完全相同 workload 重跑；
只有重复出现的持续增长并有 heap/native-memory 证据时，才作为增长失败阻止批准。

A9-M 只能使用下面四种 decision state：

```text
PASS
    必需测量完整、趋势稳定，绝对资源值处于 Preferred 或 Acceptable 区间
CONDITIONAL
    测量完整且稳定，但某个绝对资源值落入既定 conditional 区间
DIAGNOSTIC_RERUN_REQUIRED
    命中增长触发器、采样有噪声/不完整，或必需的 RSS、heap、class metadata、
    peak、GC checkpoint、thread/class-count 数据 unavailable
NO_GO
    超过绝对 No-Go 区间，或相同 diagnostic rerun 以证据确认持续增长
```

缺失测量绝不能填 0，一次噪声绝不能称为 leak。按操作类型记录 latency 分布，但它
不是 Phase 1 性能宣传阈值。

#### A9-L — 取消、恢复与所有权

取消使用独立 workspace lineage，并要求真正的异步 Worker 协议：一个 build 可以
后台运行，同时控制循环只接受属于该 build generation 的 `STATUS`、`CANCEL` 和
有界 shutdown。协议必须携带 command/request ID 与 `build_generation_id`，不能靠
“最后一个响应”推断所有权。
可以用确定性的测试 barrier 在 Java Builder 已开始后暂停，但它只能改变时序，
不能改变输入/输出，并且不能计入正确性或 latency 证据。

异步协议、metrics 支持和默认休眠的 barrier 都属于锁定 Worker artifact。加入这些
代码会改变 candidate identity，因此 A9 Approve 前必须在这个精确 artifact 上重跑
A1-A8。Barrier 只能由 A9-L 生命周期命令激活，不得改变 source、classpath、
compiler option、marker 或 class byte；被取消操作的任何输出都不能作为正确性证据。
所有 barrier 关闭时，A1 instrumentation parity 必须继续完全一致。

实现前冻结下面的协议状态机。workspace lineage state 与 build-generation outcome
是两条独立状态线。每个 `BUILD_COMPLETED` 都必须携带 `operation_kind`、
`operation_ok` 和 nullable `compile_ok`：

```text
workspace lineage
  READY
    BUILD_ASYNC(request_id, build_generation_id, kind)
      -> BUILDING(build_generation_id)
          STATUS(build_generation_id) -> 只读 snapshot
          CANCEL(build_generation_id) -> 接受后进入 CANCEL_REQUESTED
          STOP                        -> CLOSING 并请求取消
      -> 该 build generation 唯一 terminal event：
          BUILD_COMPLETED(
              operation_kind=CLEAN|FULL|INCREMENTAL,
              operation_ok=true,
              compile_ok=null|true|false
          )
          BUILD_CANCELLED
          BUILD_ABORTED

build generation outcome / 对 workspace 的影响
  BUILD_COMPLETED, CLEAN, operation_ok=true, compile_ok=null
      -> SUCCEEDED / workspace state 由外层 recovery transaction 决定
  BUILD_COMPLETED, FULL|INCREMENTAL, operation_ok=true, compile_ok=true
      -> SUCCEEDED / 非恢复场景回到 READY
      -> SUCCEEDED / 恢复场景保持 RECOVERING，直到 oracle equality
  BUILD_COMPLETED, FULL|INCREMENTAL, operation_ok=true, compile_ok=false
      -> FAILED_COMPILE / 非恢复场景回到 READY
      -> FAILED_COMPILE / 恢复场景进入 LINEAGE_DISCARDED
  BUILD_CANCELLED
      -> CANCELLED / RECOVERY_REQUIRED
  BUILD_ABORTED 或 Worker 异常退出
      -> ABORTED / RECOVERY_REQUIRED

RECOVERY_REQUIRED
  RECOVER(recovery_id)
      -> RECOVERING
          CLEAN_BUILD -> FULL_BUILD -> clean-full oracle 验证
      -> 只有完整事务成功才回到 READY
      -> 任一步失败则 LINEAGE_DISCARDED

LINEAGE_DISCARDED
  -> 创建新私有 workspace lineage 并执行 full build
```

`SUCCEEDED` 只表示 compiler/workspace 操作成功完成，并不自动使 build generation
可发布。对应 case 要求的全部 oracle 和 publication gate 通过前，必须保持
`generation_publishable=false`。

一个 Worker 同时只能有一个 build。Workspace mutation 只在 build thread 发生；
`STATUS` 只读 immutable/atomic snapshot，`CANCEL` 只取消精确 build monitor。每个
request response 和异步 event 都携带 request ID、`build_generation_id` 和单调递增
protocol sequence；每个接受的 build operation 只能产生一个 terminal event。
`CLEAN` 成功时 `operation_ok=true`、`compile_ok=null`；`FULL`/`INCREMENTAL`
中的 `operation_ok=true` 表示 Workspace operation 本身完成，而 `compile_ok` 表示
Java 编译是否通过。因此 Java 编译错误是 `BUILD_COMPLETED` 且
`compile_ok=false`，不是 `BUILD_ABORTED`；它不污染
workspace lineage，并允许像 A7 一样继续增量修复。`BUILD_ABORTED` 只表示 Worker、
JDT、protocol、I/O 或其他基础设施故障，使本次 build 无法形成可信的 completed
result。

唯一 terminal record 以 Runner boundary 为准，不能依赖 Worker 一定存活到发出
terminal frame。Worker 提前退出、protocol stream 中断，或在有效 terminal frame
被接受前发生 forced termination 时，Runner 必须且只能记录一次 `BUILD_ABORTED`，
并拒绝任何迟到 frame。协作取消被接受且正常 settled 时记录 `BUILD_CANCELLED`；
最终需要 force 时则记录 `BUILD_ABORTED`。

若取消被接受前 build 已完成，则 `BUILD_COMPLETED` 优先，`CANCEL` 返回
`ALREADY_FINISHED`；若取消先被接受，则 `BUILD_CANCELLED` 优先，即使 JDT 在结束前
写过文件，该操作的 class/diagnostic 也不可发布。`STOP` 遵循同一“唯一终态”规则：
若完成已经胜出，就继续正常 save/close；否则先请求取消，等待唯一 cancel/abort
terminal event 后再关闭。未知/过期 ID 必须拒绝，不能影响 active build。只有 build
thread 真正退出后 cancelled build 才算 settled；更早不得恢复或释放 ownership。

恢复是一个原子 workspace-lineage recovery transaction，不只是再发一次 full-build；
它与未来 Runtime publication transaction 不同，不会把产物提交给 HotSwap 或
restart。`RECOVERING` 期间，`CLEAN_BUILD` 和 `FULL_BUILD` 各自获得不可变
build-generation 记录，但任何
中间产物都不可发布，workspace 也不能提前回到 `READY`。只有 clean 成功、full
成功且与 clean-full oracle 完全一致，事务才提交并使 lineage 回到 `READY`；其中
clean 为 `operation_ok=true`、`compile_ok=null`，full 为 `operation_ok=true`、
`compile_ok=true`。这只表示 workspace lineage 恢复可信，Runtime publication 仍是
未来独立 gate。恢复
期间发生 cancel、编译失败、infrastructure abort 或 oracle mismatch，都必须丢弃
该 lineage；Runner 创建新的私有 workspace lineage 并建立 fresh full baseline。

至少验证：

```text
active incremental build -> CANCEL -> 有界协作取消
cancelled build generation -> 不可发布；lineage 进入 RECOVERY_REQUIRED
同一 Worker lineage      -> CLEAN + FULL 恢复事务 -> oracle exact
build deadline 到期      -> 协作取消 -> 释放 barrier ->
                            BUILD_CANCELLED -> RECOVERY_REQUIRED
build 已先完成时 STOP    -> 唯一 BUILD_COMPLETED -> clean save/close
cancel 已先接受时 STOP   -> 唯一 BUILD_CANCELLED -> 有界关闭
干净停止的 Worker       -> 离线方法体修改 -> 重开 -> incremental + oracle
Worker 异常退出          -> 缺少 clean marker，保存状态失效
无效保存状态             -> 禁止 incremental reopen，新私有 lineage + full
```

build deadline 到期后必须先请求协作取消。只有取消或 shutdown 在其 5 秒 budget
内仍未 settled，Runner 才能终止身份精确绑定的 process tree，并明确记录 forced，
不能伪装为协作成功。timeout case 使用确定性 barrier，必须观察而非推断 deadline、
取消被接受、barrier 释放、build thread 退出、terminal event 和 state transition
的完整顺序。

Workspace-lineage 复用要求 manifest 包含 `workspace_lineage_id`、最近一次 completed
的 `build_generation_id`，以及 candidate、Worker/bundle、target-system library、
`project_model_fingerprint` 和最后一次 clean save 时的源码状态指纹。只有 Runner
拥有 clean-shutdown marker。启动时必须在授予 ownership 前原子消费旧 marker；
退出时只有在 Worker `SAVE_ACK`、zero exit 且完整 identity-bound owned process
tree 已 settled 后，Runner 才能原子发布新 marker。marker 发布遵守通用生命周期
契约中的平台安全 file/replace/sync 行为；Worker 不能自证 clean。不变身份变化使
复用失效；与保存指纹相比、有界且仅涉及源码的变化记录为 offline delta，不能静默
当作配置漂移。这会把 A8 的 runner lineage label 升级为独立持久化 reuse 前置条件。

每个 owned PID 都记录 create time，避免 PID 复用误杀。Runner 持续观察 descendant，
绝不能 signal 未拥有的进程，并在 settlement 后确认全部观察到的 owned process
都消失。A9-L 不要求人为制造一个不响应取消的 JDT 故障来证明 force-kill；force
fallback 继续由单元测试覆盖，真实 A9 证据必须证明正常协作路径和异常退出失效。

#### A9 验收记录

只有同一锁定 candidate 的 A9-S、A9-L 通过且 A9-M 为 `PASS`，A9 才能通过。
`CONDITIONAL` 代表证据完整，但只能形成 Conditional phase decision；
`DIAGNOSTIC_RERUN_REQUIRED` 在完全相同重跑完成前阻止决策；`NO_GO` 使 A9 失败。

```text
baseline full build 通过 correctness/oracle gate
11/11 warm-up request 通过 correctness/oracle gate
110/110 measured 请求得到预期结果
所有成功状态与 keyed clean-full oracle 完全一致
所有 compiler-error/cancelled/aborted build generation 都不可发布
无静默 full fallback、stale class family、卡住或 Worker 重建
资源数据完整、趋势稳定且处于 A9-M PASS 区间
协作 cancel/stop 在预算内结束
cancel/abort 后只使用原子 CLEAN -> FULL -> oracle 恢复事务
异常状态被拒绝而不是被信任
全部身份绑定的 Worker/oracle process tree 无残留
```

机器报告记录 workload version、operation index/type、source fingerprint、
requested/actual kind、`operation_kind`、`operation_ok`、nullable `compile_ok`、
diagnostics/publication、oracle key 与 hit/miss、输出一致性、
timing、原始资源样本、取消时间线、进程身份、`workspace_lineage_id`、不可变
`build_generation_id` 及其终态、恢复事务、marker ownership/publication 和全部
limitation。A9 仍然只是 tiny-fixture 证据，不对公司项目、Lombok、HotSwap 或产品
发布性能作任何声明。

Resource-delta instrumentation 仍是独立的 Phase 1A Go 条件。A9 必须继续明确报告
当前 `unavailable`，不能从源码 fingerprint 推断 Java Builder delta。如果 A9 不
包含经过评审的只读 delta instrumentation，它就继续作为 pre-Go 项保留，不能被
100 轮 workload 静默视为已经解决。

### A10 — 平台和路径边界

必须在 Windows 和至少一个 POSIX 环境通过。至少一次 attempt/source 路径
包含空格与非 ASCII 字符。

## 如何证明真的增量

不能只靠 class timestamp 或耗时。必须观察：

```text
requested build kind
actual build kind
Java nature 与 configured builder identity
delta 是否可用
resource delta 摘要
compiled source units（如可观测）
created/changed/deleted class families
full-build fallback 及原因
```

Instrumentation 不得替 JDT 决定受影响源码。可使用有界 Java Builder trace、
只读 build participant，或 output-write observation + resource delta +
actual build kind。如果不能证明 Java Builder 是否真的增量，Phase 1A 未通过。

若使用 `CompilationParticipant`，它只能是 observer：

```text
modifiesEnvironment=false
createsProblems=false
aboutToBuild() 只返回 READY_FOR_BUILD
isAnnotationProcessor() 返回 false
isPostProcessor() 返回 false
不生成 source/folder
不修改 BuildContext、不记录 dependency
不增加 diagnostic
不修改 class bytes
```

只请求 `INCREMENTAL_BUILD` 不构成证据；Eclipse 可能实际 full build。Build
API 正常返回也不等于成功；接受 build generation 前必须检查 ERROR marker 与
build-path problem。

每个状态变化 case 都要和 clean-full oracle 比较完整的相对 class 集合、
每个 class SHA-256、diagnostic identity/source location 及过期输出缺失。
小型 runtime assertion 只能补充，不能替代完整比较。

## Phase 1B Lombok Fixture 与 Case

Phase 1B 只增加 Lombok `1.18.20`，不泛化 annotation processing。Fixture
至少使用 `@Data`、`@Builder`、`@Slf4j`、`@NonNull`，包含调用生成
getter/builder 的 dependent source、使用生成 `log` 字段的方法，以及效果
可观察的有界 `lombok.config`。

必须记录精确 Lombok 集成机制。若 Lombok 作为 Java agent patch
ECJ/Equinox，agent 属于 Worker identity；不能静默替换为 delombok 或 javac
class。仅能解析 annotation 不算成功，必须证明下游能调用生成成员且 `log`
字段真的存在。

Phase 1B 必须执行 clean full、普通方法体修改、改变生成 accessor 的字段修改、
改变生成 schema 的 annotation 修改、consumer 修改、`lombok.config`
新 generation/full build、生成成员 error/recovery，以及 warm-up 后至少 100
次 mixed incremental/no-op。每个成功结果都等于同一锁定栈的 clean-full。

不比较 ECJ 与 Maven/javac 字节，不宣称跨编译器等价，也不能升级 Lombok
来让实验通过。

## 测量与决策区间

必须区分配置与观测；`-Xmx` 不是 RSS。每个平台/candidate 至少记录
artifact 大小、bundle 数量、Worker JDK/JVM 参数、Xms/Xmx、process-tree、
cold start、full/no-op/leaf/dependency 时长、shutdown、显式 GC 并有界等待后
的 heap/Metaspace、再 idle 30 秒后的 `process_tree_rss_sum_bytes`、
full/incremental 的 heap 与 `process_tree_rss_sum_bytes` 峰值、100 次后的
`process_tree_rss_sum_bytes`，以及 shutdown 前中后的 child-process 数。

| 观测 | 决策 |
| --- | --- |
| Idle `process_tree_rss_sum_bytes` `<256 MiB` | 首选 |
| Idle `process_tree_rss_sum_bytes` `256–512 MiB` | 可接受 |
| Idle `process_tree_rss_sum_bytes` `512–768 MiB` | Conditional，需要收益证明 |
| Idle `process_tree_rss_sum_bytes` `>768 MiB` 或接近完整 JDT LS | 首选架构 No-Go |
| 小 fixture full-build `process_tree_rss_sum_bytes` 峰值 `>1 GiB` | 除非证明测量错误，否则 No-Go |

未来公司项目峰值属于 Phase 2。Phase 1 不规定亚秒延迟，只要求已启动 Worker
对合格普通 edit 确实避免 full build。warm-up 后 `process_tree_rss_sum_bytes`
不能单调或无界增长。

## 结构化实验报告

每次运行输出机器报告和 Markdown 摘要，至少包含：

```text
attempt_id / workspace_lineage_id / build_generation_id
git revision / dirty-worktree
OS / architecture
locked artifacts / hashes
Worker JDK / compliance / target / Lombok
target JDK 8 system-library fingerprint
system_library_discovery_method
fixture input fingerprint
requested / actual build kind
delta / compiled-unit / output-family / diagnostic
clean-full comparison
timing / resource
cancel / shutdown settlement
workspace-restart result
warnings / limitations / retained local artifact
```

绝对用户路径和原始环境值只留本地。失败/不完整测量写 unavailable，不能伪装
成零。事实、推断、产品建议必须分开。

## Phase gates

### Phase 1A Go

必须全部满足：

- 使用真正 Java Builder并证明实际 incremental；
- nature、build spec、effective build kind、delta 都是观察值；
- A1–A10 在 Windows 和至少一个 POSIX 通过；
- 每个 incremental build generation 等于 clean-full；
- dependency propagation、diagnostic recovery、delete/rename、stale cleanup
  正确；
- 同一健康 Worker 不会静默丢 state；
- cancellation/shutdown 有界且无 owned process；
- 资源在 acceptable/preferred 区间；
- 最小分发不含 JDT LS 和无关 IDE 服务。

### Phase 1B Go

精确 Lombok 1.18.20 生效；JDK 8 system library 拒绝 Java 9+ API；Lombok
full/incremental 等于 clean-full；dependent source 正确看到生成成员变化；
config/annotation 不留 stale schema；`lombok.config` 变化保守地创建新的
workspace lineage 并做 full build；生成成员 error/recovery 和 warm-up 后至少
100 次 mixed incremental/no-op 也必须通过；生命周期仍满足 Phase 1A。

### Conditional

例如 state 只能在 Worker 存活时保留、资源在 conditional 区间、Lombok 仅在
2021-03 anchor 生效、POSIX 与 Windows 不同、输出正确但不能精确观测 compiled
units。这些需要再次设计评审，不是自动 Go。

### No-Go

最多两个锁定 candidate 后，若仍无法证明真正 incremental、出现漏编/stale
class、Lombok 必须升级或改用户源码、依赖 JDT LS/M2E/Buildship/UI、Windows
cancel 不可靠、健康 Worker 反复丢 state、资源越界或 bundle 栈无法复现，就
停止首选路线。之后可以重新评估 ECJ Batch + 有界 joLink dependency graph，
但不在本 POC 并行实现。

## Phase 1A 交付物

```text
本契约
含 hash/license 的 artifact lock
最小 headless Worker source/product definition
纯 Java fixture
确定性跨平台 runner
clean-full comparison 工具
有界生命周期/资源工具
Windows + 一个 POSIX 的 Phase 1A 报告
Phase 1A Go / Conditional / No-Go 决策记录
```

## Phase 1B 追加交付物

仅在 Phase 1A Go 后追加：

```text
Lombok fixture
精确 Lombok 1.18.20 lock 与集成证据
Windows + 一个 POSIX 的 Phase 1B 报告
Phase 1B Go / Conditional / No-Go 决策记录
```

实验代码必须隔离，不能被生产 Runtime module 导入。普通构建/测试若未显式
运行实验命令，不得下载或启动 Eclipse artifact。

## Phase 1 后推进顺序

```text
Phase 2 contract
    Maven Bootstrap
    versioned BuildWorldSnapshot
    conservative invalidation
    真实公司项目 full/incremental 测量

Phase 3 contract
    JVM 从一个完整、已验证的 ECJ build generation 启动
    method-body delta → 标准 JDWP HotSwap
    schema delta → 从完整、已提交的 build generation restart
    readiness 与 HTTP 业务验证
```

没有独立兼容证据，不能在同一 Runtime artifact generation 混合 Maven/javac baseline
class 与后续 ECJ class。MapStruct、QueryDSL、Spring metadata、resource
fidelity、增强 HotSwap、MCP、生产 idle policy 和公开安装仍是未来决策。
