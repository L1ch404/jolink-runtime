# joLink Headless JDT 增量编译 POC 契约（中文版）

契约版本：`0.1`

设计状态：`已批准进入 Phase 1A 实验`

实现状态：`A1-A8 部分证据已通过；A9-A10 待验证`

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
- `Generation`：一个私有 workspace、编译器身份、项目配置、源码快照、
  输出树及 build state。不同 generation 的结果不能混用。
- `Publication transaction`：未来产品接入时的发布边界。Runtime 在 candidate
  generation 编译和验证期间继续使用已提交的 last-good generation。
  `generation_publishable` 目前只是逻辑发布门禁，并不会把 JDT 可变的 `bin`
  目录变成物理 last-good 存储。
- `Clean-full oracle`：从同一冻结输入、使用同一锁定 JDT 栈创建的新私有
  generation，是判断增量结果正确性的基准。
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
bootstrap placeholder 的缺失项，则视为 unresolved 并使 generation 失效。
`PRESENT` 与 `ABSENT` 的双向状态变化、内容指纹变化，或 present compiler
entry 不可读，也会使 generation 失效。所有 present javac platform entries
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
- cancelled/timed-out/crashed generation 不可发布，下次只能 clean/full；
- 不遗留 Worker、Equinox、compiler 或 fixture application 进程；
- Phase 1 不 attach JDWP、不启动 fixture 应用；
- 只有 runner 明确要求时才保留失败 attempt。

若用 stdio 控制 Worker，stdout 只能有协议帧，诊断写 stderr 或私有日志。
公开结果不得包含原始环境值、用户 home、仓库凭据或无限编译输出。

优雅 shutdown 前必须请求 Workspace save。保存的 generation 要有
clean-shutdown marker，并指纹化 Worker、bundle、编译选项、system library、
源码和 classpath。marker 缺失、save 失败、异常退出或指纹变化都会使 state
失效。一个 workspace 同时只能被一个 Worker 拥有。空闲退出先给 5 秒
settlement budget，之后只能终止身份已验证的 owned process tree。

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
不能宣布 generation 可发布，也不能把过期 class 当成功输出。修复后除非
Java Builder 自己要求 full，否则应在同一 Worker 恢复。

首次 macOS A7 证据在一个原本可编译的 class 中引入 unresolved symbol。
同一 Worker 返回包含 resource、line、character range、severity 和 message 的
有界结构化 ERROR 诊断，明确标记 generation 不可发布，并且不暴露任何
可发布 changed class。将错误修复为另一个有效实现后，同一 Worker 完成
增量恢复、清空全部 diagnostics，并产生与独立 clean-full oracle 完全相同的
可发布 class tree。失败构建期间生成或保留的 class 只作为证据记录，绝不作为
可发布输出对外呈现。

#### 未来 Runtime 发布边界（仅记录，Phase 1 不实现）

JDT 会直接写入私有 generation 的可变输出树；失败 build 可能保留、删除或
替换其中的 class。因此 `generation_publishable=false` 是逻辑安全门禁，不是
物理 last-good output 隔离。产品 Runtime 接入时绝不能扫描当前 `bin` 后把
其中所有 class 直接 HotSwap，而必须把编译视为 publication transaction：

```text
已提交的 last-good generation N 继续服务 Runtime
    candidate N+1 编译失败 -> ABORT；N+1 的 class 一个也不发布
    candidate N+1 编译成功 -> COMMIT 明确验证过的输出集合
                              然后才允许 HotSwap 或 restart
```

物理实现可以是独立输出根、不可变快照、copy-on-commit 或以后评审确定的其他
方案，当前故意留到产品化阶段再决定。Phase 1 只记录编译器现实并执行逻辑门禁，
不宣称已经具备事务化输出存储。

### A8 — Workspace restart

成功 build 后优雅保存、停止、重启同一 generation，再做普通方法体修改。
结果是 incremental 或 required full 都必须显式且正确，禁止静默 fallback。

首次 macOS A8 证据先完成 full build，收到明确的 Workspace save 确认并协作式
关闭 Worker，然后用同一 configuration area、workspace 和 generation 启动新的
Worker 进程。重开的 Worker 接收普通方法体修改和 requested incremental build；
真实 Java Builder 报告 `actual_build_kind=INCREMENTAL`，只编译
`Application.java`、只改变 `Application.class`，最终输出树和 diagnostics 与
独立 clean-full oracle 完全一致。A8 的三个 Worker 实例全部协作式退出。该证据
只证明当前冻结 candidate 与 fixture 的状态恢复，不证明 crash recovery、指纹失效
或长期运行稳定性。

### A9 — 重复构建稳定性

至少 100 次 method-body、constant、error/recovery、delete/restore 和 no-op
循环。每个成功 incremental generation 等于 clean-full oracle；不能 crash、
卡住、留下 stale class family 或出现无界内存趋势。

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
API 正常返回也不等于成功；接受 generation 前必须检查 ERROR marker 与
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
的 heap/Metaspace、再 idle 30 秒后的 RSS、full/incremental 峰值、100 次后
RSS 和 shutdown 前中后的 child-process 数。

| 观测 | 决策 |
| --- | --- |
| Idle RSS `<256 MiB` | 首选 |
| Idle RSS `256–512 MiB` | 可接受 |
| Idle RSS `512–768 MiB` | Conditional，需要收益证明 |
| Idle RSS `>768 MiB` 或接近完整 JDT LS | 首选架构 No-Go |
| 小 fixture full-build 峰值 `>1 GiB` | 除非证明测量错误，否则 No-Go |

未来公司项目峰值属于 Phase 2。Phase 1 不规定亚秒延迟，只要求已启动 Worker
对合格普通 edit 确实避免 full build。warm-up 后 RSS 不能单调或无界增长。

## 结构化实验报告

每次运行输出机器报告和 Markdown 摘要，至少包含：

```text
attempt_id / generation_id
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
- 每个 incremental generation 等于 clean-full；
- dependency propagation、diagnostic recovery、delete/rename、stale cleanup
  正确；
- 同一健康 Worker 不会静默丢 state；
- cancellation/shutdown 有界且无 owned process；
- 资源在 acceptable/preferred 区间；
- 最小分发不含 JDT LS 和无关 IDE 服务。

### Phase 1B Go

精确 Lombok 1.18.20 生效；JDK 8 system library 拒绝 Java 9+ API；Lombok
full/incremental 等于 clean-full；dependent source 正确看到生成成员变化；
config/annotation 不留 stale schema；生命周期仍满足 Phase 1A。

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
    JVM 从一个完整 ECJ generation 启动
    method-body delta → 标准 JDWP HotSwap
    schema delta → 从完整私有 generation restart
    readiness 与 HTTP 业务验证
```

没有独立兼容证据，不能在同一 runtime generation 混合 Maven/javac baseline
class 与后续 ECJ class。MapStruct、QueryDSL、Spring metadata、resource
fidelity、增强 HotSwap、MCP、生产 idle policy 和公开安装仍是未来决策。
