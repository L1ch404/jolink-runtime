# JDT Phase 2A：真实 Maven Build World 契约

状态：已在 `experiment/jdt-incremental-worker` 实验分支实现。

Phase 2A 只回答一个问题：

```text
能否把一个有代表性的真实 Maven 模块冻结成 BuildWorldSnapshot，
并交给锁定的私有 JDT Java Builder 完成一次正确的 FULL BUILD？
```

它不实现产品 Fast Compile、增量修改、HotSwap、重启、HTTP 验证，也不新增
MCP action。

## 阶段边界

```text
Phase 2A  Maven baseline -> BuildWorldSnapshot -> 私有 JDT FULL
Phase 2B  真实修改 -> JDT incremental -> JDT clean-full oracle
Phase 2C  已提交的 changed classes -> HotSwap/restart -> Runtime evidence
```

本契约不批准 Phase 2B/2C。Phase 2A PASS 只说明值得设计 Phase 2B，不代表已经
可产品化。

## 权威输入

Maven 仍是 Build World 权威来源。实验先使用指定的 Maven、JDK、settings、
本地仓库、profile 和 reactor 选择执行 `clean compile`，然后通过受监督、可取消
的 Maven 操作获取 effective POM 与 compile-scope classpath。

原始 Maven 日志、effective POM、classpath 文件、绝对路径、源码、settings 和
凭证只保留在权限收紧的本地 attempt 目录，不能进入可分享报告。

首个 P0 只支持一个有代表性的 Java 8 模块，继续复用 Eclipse 2021-03 / JDT
3.25 技术栈与其冻结的 Java 8 project model。旧 anchor lock 保留此前 Phase 1
证据；当前 `diagnostics-v2` lock 是独立的 Worker/protocol candidate，在重新执行前
不得继承旧 A9、A10 或 Phase 1B 结论。若真实模块不是 Java 8 source/target，实验
会明确拒绝，不能偷偷用 Java 8 参数编译。

## BuildWorldSnapshot v1

私有 Snapshot 冻结：

- 主源码根；
- 实际存在且包含 Java 源码的 generated source root；
- compile scope 依赖及内容指纹；
- Maven 发现的 classpath entry 的内容验证分类；
- Target JDK system libraries；
- Maven source/target/encoding；
- effective configuration 指纹；
- Annotation Processor service 身份；
- 存在 Lombok 时的版本与配置指纹；
- 源码和配置输入指纹。

可分享摘要只输出数量和 SHA-256 身份，不得输出公司目录、仓库路径、依赖坐标、
源码、Maven 日志、编译诊断原文、token/header 或 settings 内容。

Maven 输出某个路径，不等于已经证明该路径是 Java 二进制输入。Worker 启动前必须
按以下边界分类：

```text
包含 Java class 的 archive      -> Maven type 无相反证据时加入
已知可进 classpath 的 Maven type -> 加入（包括纯资源 JAR）
class 目录                       -> 加入
其他 Reactor 模块输出            -> 带 Reactor provenance 加入
可识别的 Maven 项目描述文件       -> 记录指纹并排除
未知文件或 entry                  -> fail closed
```

仅能作为 ZIP 打开不能证明 artifact 是 Java 编译输入；没有 Maven type 支持的
sources JAR 或任意资源 ZIP 必须 fail closed。Maven 项目描述文件必须根据有大小上限、modelVersion 正确的 XML
内容识别，不能只看 `.pom` 后缀。已知非二进制 artifact 仍属于 Build World 身份和
共享计数的一部分，但绝不能进入 JDT classpath。Phase 2A 当前会组合 Maven
Dependency Plugin 的 compile-scope 路径输出、`dependency:list` 的 type/scope/path
证据以及有界的可进 classpath artifact type 白名单；以后仍应直接捕获 Maven
`compileClasspathElements`/artifact-handler 语义，在此之前不能猜测未知文件类型。

## 禁止偷吃当前模块旧 class

JDT compile classpath 严禁包含：

- 当前模块的 `target/classes`；
- 当前模块的 `target/test-classes`；
- 当前模块 `target` 下的任何 entry；
- 能确认属于当前模块的本地仓库旧 JAR；
- 当前或历史 JDT generation 的私有输出。

Reactor 中的其他上游模块是合法依赖，可以进入 Snapshot。报告必须明确：

```json
{
  "self_output_on_compile_classpath": false,
  "stale_candidate_output_on_classpath": false
}
```

无法证明这两个条件时，必须在启动 Worker 前停止。

## Generated source 分类

```text
BOOTSTRAP_GENERATED
COMPILE_TIME_AP_GENERATED
```

`BOOTSTRAP_GENERATED` 代表 Maven baseline 已生成、JDT 可以当普通源码消费的内容；
将来其生成器输入变化时必须让 Build World 失效并重新 Bootstrap。

`COMPILE_TIME_AP_GENERATED` 可以用于 Phase 2A FULL 探索，但在刷新语义验证前会
阻止 Phase 2B。同样，发现未知 compile-time Annotation Processor 时可以记录
Phase 2A 结果，但必须返回：

```text
phase2b_incremental_eligible = false
```

Lombok 只复用旧 JDT 3.25 candidate 已验证的精确机制：
`-javaagent:<lombok>=ECJ` 和必要的 Worker JVM module opening。复用机制不等于
把旧 Phase 1B 证据转移给 diagnostics-v2 Worker，也不代表已支持 MapStruct、
QueryDSL、Dagger、Hibernate enhancement、AspectJ 或任意 Processor。

## 私有工作区与隔离

所有 source root 复制到 attempt 私有 Eclipse workspace。冻结的 Worker 当前只有
一个 Java source entry，因此会在 Java package 边界合并各 root。同一路径出现不同
内容时返回 `SOURCE_ROOT_COLLISION`，不猜 Maven root 优先级。

`BuildWorldSnapshot.encoding` 必须通过 Worker 启动参数进入 Eclipse Resources
model。只设置 JDT compiler options 不够，因为 JavaBuilder 通过 `IFile` 读取源码并
使用 Resource charset。Worker READY 必须回报 raw requested、Java canonical、Eclipse
effective 和 verified 状态，Runner 确认证据闭环后才允许 FULL。编码合法性和 alias
等价关系以 Java Charset / Eclipse Resources 为权威，不允许用 Python codec registry
重新定义。encoding 改变属于 Build World identity 改变，不能复用旧 generation。
实现不得硬编码 UTF-8。

源码链接/reparse point、超限输入、配置冲突、无法忠实映射的 Lombok import 布局、
多份 Lombok artifact 都会 fail closed。JDT 输出始终位于用户项目之外。

Maven baseline 可以正常写目标模块 `target`。baseline 之后，JDT 执行全过程必须保持
target 指纹以及源码/POM/IDE 输入指纹不变。

## Maven/javac 与 JDT/ECJ 的比较

不要求、也不宣称 Maven/javac 与 ECJ/JDT 的 class 字节 SHA 完全相同。比较通过
安全 class parser 完成，不加载、更不初始化业务 class。

Tier 1 是 Phase 2A gate：

- 源码声明的顶层/成员类型集合；
- public/protected 字段和方法；
- descriptor；
- 父类和接口；
- 泛型 `Signature`；
- runtime-visible annotation 等 API 元数据；
- class major。

Tier 2 只记录，不直接失败：

- synthetic/bridge member；
- 匿名类、lambda、compiler helper；
- private compiler-generated member；
- debug metadata 和字节布局。

P0 的 source-declared 分类仍是保守启发式。发现结构差异时返回
`REVIEW_REQUIRED`，不能虚报等价。

## 结果语义

```text
phase2a_passed
    Maven baseline、Snapshot、JDT FULL、Tier 1 与隔离 gate 全部通过

phase2a_passed_with_incremental_blockers
    Phase 2A FULL 与结构 gate 通过，但 Processor/generated source 刷新语义阻止 Phase 2B

phase2a_jdt_full_failed
    JDT FULL 失败，报告只记录脱敏诊断分类和指纹

phase2a_structural_or_isolation_gap
    JDT 编译成功，但结构兼容或隔离需要 Review
```

已知实验结果会生成报告；基础设施或模型失败返回结构化错误并保留本地 attempt。
诊断原文只留在本地，分享报告只有分类数量和 message fingerprint。

Worker 诊断使用有界的 error-first 投影。协议同时返回完整的 ERROR/WARNING/INFO
计数、实际返回计数、截断状态和
`errors_first_then_warnings_then_info` 策略。先最多返回 128 条 ERROR，再使用独立的
32 条 WARNING/INFO 预算，避免大量 warning 把真正解释编译失败的 error 遮住。

跨编译器源码兼容性必须与 Build World 缺口分开。版本化的
`cross-compiler-compatibility` fixture 固化了一个 Java 8 raw `ArrayList` + 双括号
匿名类表达式：javac 会带 unchecked warning 通过，而锁定的 ECJ 3.25 会因泛型推断
拒绝。此类结果属于源码可移植性证据；joLink 不应修改业务源码，也不能把它误报成
依赖缺失。独立的 `run_cross_compiler_compatibility.py` 会验证这一预期分歧，不把它
混入普通 Phase 2A PASS fixture。

## Maven-native Probe 迁移实验

独立 Maven-native Probe Spike 的详细边界见
`maven-build-world-probe-contract.zh-CN.md`。目前已经证明：随 joLink 携带的普通
Mojo 可以在目标 Maven Session 中导出源码根、compile classpath、output 身份和
实时 Reactor output，不需要修改项目 POM；同时已通过 Maven 3.3.9/JDK 8、
`mirrorOf=*` 和显式严格离线注入。

Phase 2A 现在提供显式混合入口：传入私有 Probe 报告后，source roots、compile
classpath 和 reactor outputs 以 Probe 为权威；compiler/Processor 配置与 artifact type
仍来自 effective POM/dependency metadata。报告必须逐项标出 provider，不得把混合模型
表述成完整 Maven compiler invocation。旧链路只保留为回归/私有差分，Probe 缺失或
冲突时不能静默成为可信 fallback。

## Phase 2A Go gate

只有同时满足以下条件，才能建议进入 Phase 2B 设计：

- 精确 Maven baseline 成功；
- Snapshot 冻结成功且无敏感信息泄漏；
- 当前模块旧输出和 JDT 旧输出均不在 classpath；
- Target system libraries 与 Java 8 compiler model 已验证；
- 私有 JDT FULL 成功；
- Tier 1 结构兼容；
- JDT 执行后 Maven target 和项目输入未变化；
- Worker 退出后没有 owned process 残留。

即使 Phase 2A FULL 成功，Processor blocker 仍可能让 Phase 2B 不具备资格。
Phase 2A 输出绝不能发布给目标 JVM。

## 公司真实项目 canonical evidence

截至 2026-08-21，公司环境已经保存 canonical Phase 2A JSON。脱敏结论为：

```text
4201 Java sources
297 compile classpath entries
JDT FULL / 0 errors
Maven-vs-JDT Tier 1 compatible
status   = phase2a_passed_with_incremental_blockers
decision = PHASE2B_BLOCKED_BY_BUILD_WORLD
```

当前唯一 Phase 2B blocker 是：

```text
unknown_compile_time_annotation_processor
```

这份证据正式关闭“真实企业项目能否完成 JDT FULL”的 Phase 2A 问题，但不批准
跳过 Processor gate。详细脱敏记录见
`jdt-phase2a-company-evidence.zh-CN.md`。
