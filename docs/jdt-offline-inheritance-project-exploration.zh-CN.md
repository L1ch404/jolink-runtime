# JDT Mac 探索报告：从 Spring Boot/H2 基线到旧企业 Maven 继承项目

状态：探索完成，未修改 joLink 实现，未批准 Phase 2B。

日期：2026-08-23

本报告串联同一台 Mac 上的两次探索：先用公开 Spring Boot/H2 小项目建立 FULL、
Incremental 与实际运行正向基线，再用本地旧企业 Maven 继承项目扩大源码、classpath
和 Processor 复杂度。旧企业项目名、包名、仓库地址、源码内容和绝对路径均已脱敏。
原始日志与临时 Build World 只保留在测试机器的临时目录中。

## 一、测试目的

本次想回答：

```text
1. 本地仓库能否支撑目标服务严格离线 Maven compile？
2. 父 POM 继承和兄弟模块依赖在 Maven 实际模型中如何体现？
3. Maven-native Probe 能否严格离线导出 Build World？
4. 现有 Headless JDT Worker 能否完成该模块 FULL BUILD？
5. unknown annotation processor 具体由什么组成？
```

本轮不修改 joLink 代码，不为项目增加特例，也不通过删除依赖或放宽 gate 强行进入
Incremental。

## 二、前置正向基线：Spring Boot + H2

在进入旧企业项目之前，先使用：

```text
spring-boot-rest-api-h2-database-main
```

验证基础链路。项目 POM 实际配置 Java 11；由于当前 JDT project model 只冻结 Java 8，
正向实验使用不修改原项目的 `/tmp` 隔离镜像，仅把临时镜像 compiler level 调整为
Java 8。同一份业务源码没有其他修改。

### 2.1 Maven Probe 与 JDT FULL

```text
Java sources                         6
Probe source roots                   2
Probe compile classpath entries     60
BuildWorld effective source roots    1
BuildWorld dependencies             59
annotation processor artifacts       0

JDT actual build kind              FULL
JDT errors / warnings              0 / 0
JDT FULL duration                  238.7 ms
Maven/JDT class count              6 / 6
source-declared type sets equal    true
Tier 1 status                      compatible
phase2b_incremental_eligible       true
```

Encoding 证据闭环：

```text
requested            UTF-8
requested canonical  UTF-8
Eclipse effective    UTF-8
verified             true
```

### 2.2 真实 JDT Incremental

在 private workspace 中只修改一个普通 service method body：

```text
requested build kind       INCREMENTAL
actual build kind          INCREMENTAL
compiled source units      1
changed class files        1
errors / warnings          0 / 0
incremental duration       248.1 ms
independent clean FULL     213.3 ms
clean-full oracle equal    true
```

该项目很小，因此 clean FULL 与 Incremental 的耗时没有产品性能代表性；价值在于实际
compiled source/class 集合正确，而且完整输出与同一 JDT candidate 的独立 clean FULL
逐 class 精确一致。

### 2.3 实际运行验证

使用 JDT clean-full oracle 的 class 输出、项目 resources 和 Maven runtime classpath
直接启动 Spring Boot：

```text
application ready           true
HTTP create request         success
HTTP delete request         success
modified method behavior    observed
application stopped         true
```

这证明 JDT 生成的 class 不只是 hash/oracle 可比较，而是能够被真实 Spring Boot JVM
加载并执行修改后的逻辑。

本轮没有执行 JDWP HotSwap，也没有证明当前 Worker 原生支持项目 POM 的 Java 11；它
证明的是同一源码在冻结 Java 8 Build World 中的：

```text
Maven Probe
-> JDT FULL
-> JDT Incremental
-> clean-full oracle
-> Spring Boot runtime behavior
```

完整闭环。

### 2.4 两次探索的关系

```text
Spring Boot/H2 小项目
    -> 无 Processor blocker
    -> FULL / Incremental / runtime 全部通过

旧企业 Maven 继承项目
    -> FULL 通过
    -> Processor/resource generation 阻止 Incremental
    -> 暴露 Probe offline 与 structural oracle 边界
```

因此第二个项目的失败边界不能被解释成“JDT 基础链路不可行”；前一个项目已经证明基础
链路成立，后一个项目是在增加真实 Build World 复杂度后定位缺失语义。

## 三、旧企业目标工程形态

脱敏后的项目量级：

```text
main Java sources  423
test Java sources   34
target Java           8
```

目录中存在父 POM 和多个兄弟模块，但 Maven 模型不是标准聚合 reactor：

- 目标模块继承上层父 POM；
- 父 POM 的 `<modules>` 数量为 0；
- 目标模块使用 Maven 默认父 POM relative path；
- 因此不能从父目录使用 `-pl/-am` 构建整个兄弟模块集合；
- 兄弟模块依赖在本次 Maven compile 中来自本地仓库，而不是兄弟目录的
  `target/classes`。

这是一种“父 POM 继承 + 多个逻辑兄弟模块 + 本地仓库 SNAPSHOT”的工程形态，不能把
它误称为已验证 Maven reactor output 映射。

## 四、严格离线 Maven compile

执行环境：

```text
JDK       8
Maven    3.9.6
mode     --offline
tests    skipped
target   隔离副本，运行前不存在
```

结果：

```text
BUILD SUCCESS
compiled source files  423
class files            450
generated Java files     0
duration               6.738 s
```

结论：本地仓库已经包含目标模块普通 Maven compile 所需的父 POM、依赖与构建插件。
本轮没有因为无法访问历史仓库而失败。

Maven 同时报告两个非阻塞模型警告：

- Lombok dependency 重复声明；
- 一个第三方 dependency POM 无效，其传递依赖不可用。

它们没有阻止本次离线编译，但属于项目模型质量问题。

## 五、Probe 的离线边界

### 5.1 `--offline` 当前不是端到端离线

直接执行：

```text
run_maven_probe_spike.py --offline
```

时发现：

```text
目标项目 Probe invocation  接收 --offline
Probe 插件自身 bootstrap   固定 offline=False
```

因此 Probe 插件源码构建阶段仍尝试访问 Maven mirror。日志中该阶段约耗时 29.953 秒，
随后因为本地同坐标 Probe artifact 字节不一致返回：

```text
MAVEN_PROBE_REPOSITORY_COLLISION
```

这说明当前 source-tree Probe runner 的 `--offline` 语义不完整。问题不在目标项目，
而在 Probe bootstrap/distribution。

### 5.2 严格离线 Probe goal 可以工作

本地仓库中已经存在一个 Probe artifact，其 embedded implementation ID 与当前源码
完全一致。复用该 artifact，并在临时 settings 中加入它原先的 repository ID 后，
手工执行同一个 Maven goal：

```text
mvn --offline compile
    io.jolink:jolink-maven-probe:...:export-build-world
```

结果：

```text
BUILD SUCCESS
duration                              7.162 s
snapshot count                        1
Probe compile source roots            2
Probe compile classpath entries     246
entries from local Maven repository 245
reactor projects                      1
sibling workspace output references  0
```

Probe 报告中的第二个 source root 是 Maven 提供的 generated-source location；由于
其中没有 Java 文件，BuildWorldSnapshot 最终只保留 1 个有效 source root。

结论：Probe goal 本身可以严格离线；需要修的是 Probe artifact 的 bootstrap、预构建
分发和 Maven Resolver repository provenance。

## 六、JDT FULL BUILD

由于正式 Phase 2A runner 目前没有 offline 参数，本轮没有修改 runner，而是直接消费
严格离线 Probe snapshot，复用现有 BuildWorld、TargetSystemLibrarySnapshot 和 Worker
组件完成 private JDT FULL。

结果：

```text
actual build kind       FULL
compiled source units   423 / 423
compile_ok              true
error count             0
warning count           321
returned warnings        32
diagnostics truncated   true
JDT FULL duration       3.929 s
source encoding         UTF-8 / verified
Worker shutdown         settled
owned process tree      absent
```

3.929 秒只包含已经启动 Worker 后的 JavaBuilder FULL，不包含 Maven model、依赖解析、
resources、Probe、Worker bootstrap 或进程启动。

## 七、Maven/javac 与 JDT 结构比较

```text
Maven class count                  450
JDT class count                    442
source-declared type sets equal    true
missing declared types               0
extra declared types                 0
class-major mismatches                0
Tier 1 API mismatches                 1

Maven compiler-generated Tier 2      11
JDT compiler-generated Tier 2         3
```

唯一 Tier 1 mismatch 已定位：

- 同一个 getter/setter 上存在两种 type-use annotation；
- javac 与 JDT 输出的 `RuntimeVisibleTypeAnnotations` 内容一致；
- 两者只是在 attribute 内部的 annotation 枚举顺序不同；
- 当前 oracle 只规范化顶层 API attribute 顺序，没有规范化该嵌套列表。

因此这更接近 structural oracle normalization gap，而不是源码类型缺失、JDT 编译失败
或真实 API 语义变化。当前报告仍按既有 gate 如实记录为 1 个 mismatch，没有修改 joLink
让它通过。

Tier 2 数量差异继续保持 `recorded_not_gate`，符合 javac/ECJ 对匿名类和 compiler helper
不要求集合完全一致的现有契约。

## 八、Annotation Processor 调查

Compile classpath 中有 3 个声明 Processor service 的 artifact，共 4 个 provider。

### 8.1 Lombok 1.18.24

```text
lombok.launch.AnnotationProcessorHider$AnnotationProcessor
lombok.launch.AnnotationProcessorHider$ClaimingProcessor
```

本模块有 118 个源码文件导入 Lombok。joLink 已将 `lombok.*` 识别为已知 Processor，
并使用锁定的 `-javaagent:<lombok>=ECJ` 路径。本次 JDT FULL 成功，因此 Lombok 不是
`unknown_compile_time_annotation_processor` 的来源。

### 8.2 Spring Boot Configuration Processor 2.2.5

```text
org.springframework.boot.configurationprocessor.ConfigurationMetadataAnnotationProcessor
```

本模块存在 2 个 `@ConfigurationProperties` 类型。Maven/javac 实际生成：

```text
META-INF/spring-configuration-metadata.json
size  1458 bytes
```

JDT private class output中没有该 resource。这是一个真实输出差异：当前 JDT Worker 能
编译 class，但尚未复现或刷新该 Processor 生成的 resource。

该 metadata 通常服务于配置提示和工具，但对于完整、可发布的 build generation，不能
静默缺失或在增量修改后保持旧内容。

### 8.3 MapStruct Processor 1.3.0

```text
org.mapstruct.ap.MappingProcessor
```

本模块当前：

```text
org.mapstruct imports        0
MapStruct generated sources  0
```

源码中虽然存在大量 `@Mapper`，但不是 MapStruct import，符合 MyBatis Mapper 使用
形态。因此该 Processor 当前大概率被 ServiceLoader 发现但没有处理目标，也没有生成
输出。

joLink 目前只能证明 artifact/provider 存在，尚不能证明它在当前 source set 中 inactive，
所以继续 fail closed 是合理的。

### 8.4 最终 Processor gate

```text
annotation_processor_artifact_count  3
declared_processor_count             0
phase2b_incremental_eligible         false
phase2b_blockers:
  - unknown_compile_time_annotation_processor
```

`declared_processor_count=0` 是因为 POM 没有使用 `annotationProcessorPaths` 或显式
processor name；这些 Processor 作为普通 compile dependencies 进入 classpath，再由
ServiceLoader 隐式发现。

当前 blocker 的准确组成是：

```text
Lombok                    已知并已适配
Spring Config Processor   实际生成 resource，尚未被 JDT generation 建模
MapStruct Processor       当前无输出，但尚未形成 verified-inactive 证据
```

## 九、为什么没有继续 Incremental

虽然 FULL `compile_ok=true`，但本轮明确检测到 unknown Processor，并确认 Spring
Configuration Processor 的 Maven/JDT resource 输出不同。

因此没有删除 gate 或强行执行 Phase 2B。继续 Incremental 前至少需要定义：

```text
artifact present
processor discovered
processor active
processor produced source/class/resource
processor inputs changed
generated output refreshed/stale
```

## 十、本轮发现的 joLink 待评估项

这些是探索结论，不是本轮代码修改：

1. `run_maven_probe_spike --offline` 没有覆盖 Probe 插件自身 bootstrap；
2. Probe artifact 的 `_remote.repositories` provenance 会让已存在 artifact 在 offline
   模式下被 Maven 判为 unavailable；
3. 正式 Phase 2A runner 没有 offline invocation identity；
4. Processor model 需要区分 present / active / output-producing；
5. Spring configuration metadata resource 需要 generation/invalidation 策略；
6. 当前 Tier 1 oracle 没有规范化嵌套 `RuntimeVisibleTypeAnnotations` 枚举顺序；
7. 当前工程没有 Maven aggregator，尚未验证真正的 reactor sibling output。

## 十一、证据边界与清理

- 原目标项目源码、POM 和已有用户改动未变化；
- 所有编译和 JDT 输出均位于隔离临时目录；
- 没有修改 joLink 代码或 candidate contract；
- 凭证相关临时 settings 已删除；
- Maven、Probe、JDT Worker 和业务 JVM 均无残留；
- 这份报告不是 Phase 2B PASS，也不是 Maven/JDT 完全等价声明。

## 十二、阶段性结论

两次探索形成了递进证据：

> 公开 Spring Boot/H2 项目先证明 JDT FULL、真实 Incremental、clean-full oracle 和
> Spring Boot 运行行为能够形成闭环；随后旧企业模块证明同一 Worker 可以把规模扩大到
> 423 个 source units 和 245 个本地仓库 dependency，继续保持 FULL 0 errors。

旧企业项目本轮最强正向证据是：

> 一个 423-source、父 POM 继承、245 个本地仓库 classpath dependency、Lombok 1.18.24
> 的旧企业 Spring Boot 模块，在严格离线 Maven baseline 成功后，也能由现有 Headless
> JDT Worker 对全部 423 个 source units 完成 FULL BUILD，达到 0 compiler errors。

当前阻止继续 Incremental 的核心不是 Java source 编译能力，而是 Processor/resource
generation 的完整性与失效语义。

## 十三、后续 APT spike 更新

本报告完成后，独立 `eclipse-2021-03-apt-spike` candidate 已加入最小标准 Eclipse APT
closure，并在最小 Spring fixture 与本报告中的 423-source 模块上获得正向结果：

```text
Spring metadata FULL generation        PASS
configuration field add INCREMENTAL    PASS
configuration field delete cleanup     PASS
class clean-full oracle                PASS
metadata clean-full oracle             PASS
```

后续扩展还确认：删除整个annotation/source时，Native Incremental会保留旧metadata；
通过CLEAN+FULL fallback可以恢复到clean-full oracle。Processor artifact现已由严格离线
Maven Probe spike6导出有序Provider artifact facts，factory path/generated-source dir也完成
effective-state readback；通用Processor runtime dependency closure仍保持未验证。

因此本报告中 `unknown_compile_time_annotation_processor` 的结论应理解为当时准确的
fail-closed状态，不再代表 Spring Configuration Processor 技术上无法由 stripped Worker
执行。最新完整证据见 `jdt-apt-spike-eclipse-2021-03.zh-CN.md`。
