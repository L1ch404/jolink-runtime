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

首个 P0 只支持一个有代表性的 Java 8 模块，继续复用已通过 Phase 1 验证的
Eclipse 2021-03 / JDT 3.25 候选与其冻结的 Java 8 project model。若真实模块不是
Java 8 source/target，实验会明确拒绝，不能偷偷用 Java 8 参数编译。

## BuildWorldSnapshot v1

私有 Snapshot 冻结：

- 主源码根；
- 实际存在且包含 Java 源码的 generated source root；
- compile scope 依赖及内容指纹；
- Target JDK system libraries；
- Maven source/target/encoding；
- effective configuration 指纹；
- Annotation Processor service 身份；
- 存在 Lombok 时的版本与配置指纹；
- 源码和配置输入指纹。

可分享摘要只输出数量和 SHA-256 身份，不得输出公司目录、仓库路径、依赖坐标、
源码、Maven 日志、编译诊断原文、token/header 或 settings 内容。

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

Lombok 只复用 Phase 1B 已验证的精确机制：锁定 JDT 3.25 候选、
`-javaagent:<lombok>=ECJ` 和必要的 Worker JVM module opening。这不代表已支持
MapStruct、QueryDSL、Dagger、Hibernate enhancement、AspectJ 或任意 Processor。

## 私有工作区与隔离

所有 source root 复制到 attempt 私有 Eclipse workspace。冻结的 Worker 当前只有
一个 Java source entry，因此会在 Java package 边界合并各 root。同一路径出现不同
内容时返回 `SOURCE_ROOT_COLLISION`，不猜 Maven root 优先级。

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
