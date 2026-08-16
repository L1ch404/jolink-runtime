# JDT Phase 2A：开源项目阶梯验证计划

状态：测试路线已冻结，具体项目清单待筛选。

## 目的

公司项目已经证明 Phase 2A 能进入真实企业 Maven Build World，也暴露了 classpath
非二进制 artifact、诊断截断、跨编译器源码兼容和未知 Processor 等真实边界。但它
同时包含 4200+ 源码、数百依赖、旧版 Lombok、未知 Processor 和公司 Maven 配置，
不适合继续承担底层问题隔离。

接下来采用：

```text
开源项目 -> 控制变量、定位基础问题
公司项目 -> 最终 enterprise acceptance
```

本计划不把开源项目特例写入 Build World 核心。每个失败必须先归因到通用语义，才能
修改实现。

## 所有 Level 的统一基线

每个项目都固定 commit，并记录许可证、JDK、Maven Wrapper/Maven 版本、激活 profile、
模块选择和环境身份。测试时不修改原项目正式输出：

```text
Maven clean compile
-> 冻结 BuildWorldSnapshot
-> 私有 JDT FULL
-> 脱敏 diagnostics
-> Maven/javac 与 JDT 的 Tier 1 结构比较
```

通过条件：

- Maven baseline 成功；
- classpath entry 全部被明确分类，未知类型 fail closed；
- 当前模块旧 output 不进入 JDT classpath；
- JDT FULL 无 ERROR；
- Tier 1 结构兼容；
- 项目输入与 Maven `target` 在 JDT 阶段保持不变；
- owned Worker 完整退出；
- unknown Processor 或 generated-source 刷新问题即使不阻止 Phase 2A，也必须阻止
  Phase 2B。

Maven/javac 与 ECJ/JDT 不要求 class SHA 相同。进入 incremental 后，正确性 oracle
仍然是：

```text
同一 JDT candidate incremental output
==
独立 JDT clean-full output
```

## Level 1：普通 Spring Boot + Lombok

选择条件：

- 单模块 Maven；
- Java 8；
- Spring Boot；
- Lombok；
- 约 100～500 个 Java 文件；
- 无 MapStruct、QueryDSL、protobuf、OpenAPI 等额外代码生成器；
- 无 AspectJ、Hibernate enhancement 或其他字节码变换。

目标：证明常见 Spring Boot + Lombok Build World 能通过 Maven baseline、JDT FULL
和 Tier 1。

## Level 2：中型单模块

在 Level 1 基础上只增加规模和普通编译复杂度：

- 约 500～1500 个 Java 文件；
- 更多 compile/provided 依赖；
- 常见 `compilerArgs`、resources 和复杂泛型；
- 仍然没有 generated source 和额外 Processor。

目标：观察 FULL 构建时间、Worker 峰值内存、diagnostics 规模与 classpath 分类是否随
规模稳定，不混入 Reactor/Processor 变量。

## Level 3：Maven Reactor

选择一个 parent + 2～4 个模块的项目，包含明确的模块依赖链。专门验证：

```text
上游 workspace 当前 target/classes
-> 作为 reactor_output 进入下游 JDT classpath

本地仓库 stale SNAPSHOT JAR
-> 不得替代 workspace 当前输出
```

这一级单独形成 `Phase2A-Reactor` gate。

## Level 4：单一 generated-source / Processor 变量

分别选择项目，每个项目尽量只引入一种机制：

- MapStruct；
- QueryDSL；
- protobuf；
- OpenAPI。

目标不是一次支持全部 Processor，而是逐个确认：

- Maven bootstrap 生成了什么；
- generated source root 的 provenance；
- Processor 输入、输出及刷新边界；
- 哪些变化必须让 Build World 失效并重新运行 Maven；
- 在增量语义未经证明前是否正确 fail closed。

## Level 5：大型项目

最后才选择 2000+、4000+ 源码项目，观察：

- JDT FULL 稳定性；
- Worker 启动、构建和退出时延；
- 常驻与峰值内存；
- diagnostics 大规模投影；
- Lombok 全项目交互；
- 单文件成功但全量失败时的 source-set bisection 可用性。

大型项目不会用于修复 Level 1～4 尚未通过的基础问题。

## 失败分类

每个失败必须归入以下之一，并保留最小复现：

```text
BUILD_WORLD_CLASSPATH_GAP
SOURCE_ROOT_OR_GENERATED_SOURCE_GAP
PROCESSOR_MODEL_UNVERIFIED
BYTECODE_TRANSFORM_UNVERIFIED
CROSS_COMPILER_SOURCE_COMPATIBILITY
JDT_PROJECT_MODEL_GAP
JDT_SCALE_OR_LIFECYCLE_FAILURE
PROJECT_SPECIFIC_HIGH_ORDER_BOUNDARY
```

其中 `CROSS_COMPILER_SOURCE_COMPATIBILITY` 不允许通过修改项目源码或放宽 Build
World 可信边界来“修复”。未知 Processor 继续 fail closed。

## 回到公司项目的条件

至少满足以下条件后，公司项目才重新作为 enterprise acceptance：

- Level 1 普通 Lombok 项目通过；
- Level 2 中型单模块通过；
- Reactor 基础语义通过或明确不适用于该公司模块；
- diagnostics 能稳定返回 ERROR-first 证据；
- 已知跨编译器 fixture 能被正确分类；
- 公司项目实际使用的 generated source/Processor 要么已验证，要么明确阻止
  Phase 2B。

回归公司项目后，不再从 260 条错误逐条打补丁，而是只处理无法由已有 Level 解释的
企业级高阶边界。

## 2026-08-14 首轮候选筛选

以下 commit 均通过 GitHub 公共仓库浅克隆后本地检查；不依赖浮动分支作为证据。

### 零号门：已通过

```text
repo: https://github.com/murraco/spring-boot-jwt
commit: e9186360be0614873ae6d8e69c8e4d0948e09faa
shape: Java 8 / Spring Boot 2.5.4 / Lombok 1.18.20 / 单模块 / 16 main sources
```

真实结果：

```text
Maven clean compile                   PASS
BuildWorldSnapshot                    PASS
JDT FULL                              PASS (0 error, 4 warnings)
Tier 1                                compatible
Phase 2B eligibility                  true
Maven target / project input isolation PASS
```

这只是零号门，不替代 100～500 源码的正式 Level 1；它证明当前 Java 8 + Spring Boot
+ Lombok 1.18.20 主链可以在公开源码上闭环。

2026-08-15 在独立的
`eclipse-2021-03-lombok-anchor-diagnostics-v2` Worker/protocol candidate 上重新执行
同一固定 commit，结果仍为 `phase2a_passed`、Tier 1 compatible、Phase 2B eligible。
本次是 dirty-worktree 功能回归，不继承旧 candidate 的 A9/A10/Phase 1B，也不冒充
clean-worktree canonical evidence。

### 被 Maven baseline 淘汰

```text
repo: https://github.com/Romeh/spring-boot-sample-app
commit: 6f4168d091228891e74d3a4894acec38b9008f77
```

项目声明 Spring Boot 2.2.2 依赖，但未锁定 `spring-boot-maven-plugin` 版本；当前 Maven
解析到需要 Java 17 的 4.1.0，导致 Java 8 baseline 失败。JDT 未启动。此项目不能作为
当前 Phase 2A correctness fixture，joLink 也不会修改其 POM 强行通过。

### Reactor 候选：发现独立 metadata gap

```text
repo: https://github.com/alexmarqs/springboot-multimodule-example
commit: 65d5a4f7f6975607443883ccb50058bf7319f4d7
shape: Java 8 / 5 个子模块 / web 为目标模块
```

Maven baseline 成功，但当前独立 `maven_compile_classpath` metadata 操作在 Reactor
中无法解析尚未安装到本地仓库的上游 SNAPSHOT。该问题属于 Phase2A-Reactor
discovery gap，不与本轮 classpath 非二进制分类混修；保留为 Level 3 的第一个回归
候选。

### Level 4 候选：暂不提前运行

```text
repo: https://github.com/elenamountz/spring-boot-rest-api-warehouse
commit: 0e005a1000ef17c3e809b15b40371b6133c446db
shape: Java 8 / Spring Boot 2.3.4 / Lombok / QueryDSL APT / 76 main sources
```

该项目同时引入 QueryDSL Processor 和 generated source，明确归入 Level 4，不用于
替代普通 Lombok Level 1。

### 明确不进入当前 Java 8 阶梯

- `Genc/spring-boot-boilerplate@ca460039...`：Java 21 + MapStruct；
- `OKaluzny/spring-boot-rest-api-postgresql@65f00021...`：Java 17；
- `murraco/spring-boot-jwt` 当前主分支：Java 17，故固定使用上面的 2021 commit。

正式 Level 1 和 Level 2 仍需继续筛选；不能为了填满表格而降低“Java 8、单模块、无
额外 Processor、规模逐级增加”的控制变量要求。
