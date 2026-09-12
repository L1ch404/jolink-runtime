# Maven 显式 Processor 路径接入与实测

## 2026-09-12：显式名称与 -A 参数

在已完成 main/test 分离的基础上，接入 Maven `annotationProcessors`、编译参数
`-processor` 和 `-Akey=value`。Gradle 的原生 compilerArgs 经过同一个参数映射器，
使用相同 Worker 能力；启动、Fast Test、缓存重开均传递对应配置。

- 显式名称保留声明顺序，不要求 Processor 类存在于 META-INF/services。Maven 未声明
  annotationProcessorPaths 时，保留完整编译加载路径及辅助依赖，而非仅保留有服务
  声明的 JAR。test 的 Probe 输出也补上 explicitProcessorNames，避免只接 main。
- Worker 在发现和初始化前安装所选工厂；复用 Eclipse FactoryPath、处理环境、
  轮次调度和由 Eclipse 关闭的 ClassLoader。未选中处理器的静态初始化、构造、init、
  process 都不得执行。没有修改 JDT/ECJ 编译器算法，也没有按用户项目名称做适配。
- `-A` 配置通过 AptConfig.setProcessorOptions 保存到各 JDT 工程。顺序不排序，
  重复键最后一个生效，保留空字符串、无值 flag、Unicode、空格和等号。
- 真实测试额外发现 JDT3.25 的 IdeProcessingEnvImpl.getOptions 对无值 flag 调用
  matcher(null) 导致 NPE。适配层先让 Eclipse 展开字符串选项，再在原 ProcessingEnvironment
  的选项缓存里恢复 null flag；不使用代理环境替换 Filer/Elements/Types，也不把 null
  偷换成空字符串。此处与显式选择所用的内部字段需随未来 JDT 升级一起检查。
- Worker 显式声明已在候选包中的 org.eclipse.jdt.apt.core 依赖，避免编译通过但
  Equinox 运行时找不到 AptConfig。没有新增下载依赖或独立服务。

真实 MCP 覆盖：Maven/Gradle main-only/test-only/不同路径，指定的 Processor 没有
服务声明、同路径有会在初始化时失败的未选中处理器，主测试参数隔离，生成物实际运行，
源码增量、错误恢复、缺失处理器名称及恢复、新 MCP 无改动复用。Maven 的完整加载路径
回归还验证显式名称和 -A 同时用于应用启动。

MapStruct 官方示例固定 SHA `df4dbeaae5eea82edcfad28ce31d137116af0907` 的独立副本，
临时指定 MappingProcessor 并切换 suppressGeneratorTimestamp：false 时生成源码有 date，
true 时没有 date，两次均2/2通过；新 MCP 复用仍2/2、编译文件数0。测试结束恢复POM，
没有修改业务源码。原始 MCP JSONL 与生成源码证据仅留在本机。

Maven Probe 为 `0.1.0-fasttest18`；旧启动/Test模型缓存更新一次，随后继续复用。
未扩展 processor-module-path、外部生成器、Java17 source 或 Error Prone。

以下保留此前路径接入的历史记录。

最新进展：显式路径接入后，已直接接入处理器初始化前置。原样官方 MapStruct＋Lombok
项目已通过真实 MCP FULL、Fast Test、增量修改和恢复，不再停在初始化顺序故障上。

2026-09-10，基于 `3f64d24` 后的工作区。只处理 `annotationProcessorPaths`，
不同时扩展 Processor 名称选择、`-A` 参数、main/test 独立处理器环境或 Java17 source。

## 流程

```text
Maven 读取当前模块 default-compile / default-testCompile 的有效配置
→ Maven Resolver 解析显式 Processor 及其传递依赖
→ Probe 导出完整 processorPath，并另列发现的 Processor providers
→ 启动与 Fast Test 共用路径转换
→ 现有 Eclipse Factory Path + JDT FULL / INCREMENTAL
→ 复用原有 Build World 缓存、JDT workspace 与源码索引
```

路径解析使用当前 Maven session 的仓库、镜像、认证和离线设置；没有在 Python 中
拼本地仓库文件路径。直接依赖省略版本时读取 dependencyManagement，传递依赖是否
使用管理版本由 `annotationProcessorPathsUseDepMgmt` 决定，exclusions 交给 Resolver。

`processorPath` 是**加载路径**，不是“包含 Processor 服务声明的 JAR 列表”。辅助类、
资源、SPI 协作 JAR 即使没有 `javax.annotation.processing.Processor` 声明，也保留。
旧 `processorProviderArtifactPaths` / `providers` 继续描述发现结果，不再冒充完整路径。
Eclipse 官方明确允许 Factory Path 包含辅助类和资源容器，参见
[IFactoryPath](https://help.eclipse.org/latest/topic/org.eclipse.jdt.doc.isv/reference/api/org/eclipse/jdt/apt/core/util/IFactoryPath.html)。
Maven 参数语义参见 [Compiler Plugin](https://maven.apache.org/plugins/maven-compiler-plugin/compile-mojo.html)。

Lombok 继续走已有 ECJ javaagent 集成，同时保留它在**显式** Factory Path 中的条目：
Agent 改写 AST，APT 入口的初始化还会发布协作信号，这两个职责不是二选一。
旧隐式发现路径本轮不改；不能把它之前排除 Lombok factory 的做法直接套到显式路径。
修正旧识别逻辑：仅有 `lombok/` 包不再被误当成 Agent，需要实际存在 Agent 入口类。
因此协作 JAR 会保留在 Factory Path，不会被误传给 `-javaagent`。
除此之外没有按框架名、项目名或业务包名分支。

标准 compile/testCompile execution 继承的配置由 Maven 合并，不再一概报
`EXECUTION_CONFIG_UNRESOLVED`。额外独立编译 execution 的原有未支持边界保持；
main/test 不同处理器配置仍需后续单独处理，本轮没有取并集冒充两套环境。
已有路径一致性比较保留顺序：实测已证明顺序影响处理器执行，不能再用 set 忽略它。

产品 Probe 重建为 `0.1.0-fasttest15`（Java8 字节码）；Worker 随初始化调整重新构建，
仍为 Java8 字节码，Runner 没有改动。
没有新增缓存架构、配置开关、MCP 参数，也没有改 `jdwp_adapter.py`。

## 真实验证

新增 `tests/e2e/test_maven_processor_path.py`，两组均使用真实 Maven、JDT 和 stdio MCP：

- 自定义 Processor 依赖单独的 helper JAR，从中加载辅助类和资源；helper 没有 Processor 声明。
- 原生 Maven 对照与 joLink 均生成正确代码、运行 JUnit；helper/Processor 不泄漏到应用运行 classpath。
- dependencyManagement 控制传递依赖关闭/开启分别选 helper 1/2；直接依赖版本继承和 exclusions 生效。
- FULL、无改动复用、main 增量改变生成结果、断言失败、编译错误恢复、跨 MCP 重开。
- project launch 使用生成类启动真实 JVM，TCP 请求得到正确值；产品项目没有生成 Maven target。
- 单独确认默认 testCompile 的覆盖配置能够导出；额外独立 execution 没有被误当成默认执行。

本机 macOS 验证：普通测试 707 passed / 51 skipped；路径与两个顺序的 Lombok APT、
持久化 APT 定向用例 5/5；最终真实 JVM/MCP 回归 30/30（含上述用例，以及 Maven/Gradle
多模块、Java8/11、no-op 重开和 GC）；Fast Test 产品专项 6/6。
Maven Probe 用 JDK8 重建，wheel/sdist 构建、定向 lint 和 diff 检查通过。没有把这些
结果写成 Windows 或其他未实际测试的 Maven/JDK 组合已经通过。

新增 `tests/e2e/test_explicit_lombok_apt.py`：Lombok 在前和 MapStruct 在前均通过真实
MCP 的 FULL、getter 修改后的断言失败、恢复和新 MCP 复用。
`test_jdt_persisted_configuration.py` 还覆盖两级 Processor 生成源码（生成出的源码
再触发另一个 Processor）、每个实例只初始化一次、编译诊断及持久化后增量恢复。

### 官方 MapStruct 样本

来源 [mapstruct-examples](https://github.com/mapstruct/mapstruct-examples/tree/df4dbeaae5eea82edcfad28ce31d137116af0907)，
固定 commit `df4dbeaae5eea82edcfad28ce31d137116af0907`。JDK8，Maven3.9.11，
当前 Eclipse/JDT candidate。原生 Maven 与 joLink 使用独立 clone，产品 clone 无 target，
不修改源码或 POM 绕过问题。

| 样本 | 原生 Maven | 当前 joLink |
|---|---|---|
| mapstruct-field-mapping / MapStruct1.6.3 | 2/2 | 真实 MCP JDT FULL + Fast Test 2/2，冷调用约5.1秒 |
| mapstruct-lombok / MapStruct1.6.3 + Lombok1.18.30 + binding0.2.0 | 1/1 | 初始化调整后真实 MCP FULL + Fast Test 1/1，冷调用约5.9秒；POM/源码保持原样 |

同一官方项目完成开发循环：无改动约0.22秒；getter 修改后如期断言失败约0.77秒；
恢复约0.18秒；编译错误如实报告，修复后通过；新 MCP 复用约1.62秒。耗时是本机
单次观察，不作为跨项目性能承诺。临时源码修改均已恢复。

## 修复前定位：MapStruct 与 Lombok binding 的 ECJ 协作

原样配置报错：`No implementation was created ... erroneous element java.util.ArrayList`。
已确认完整路径存在，Lombok 编译出的 Source.class 也确实有 getter/setter。
后续参考 Eclipse 并在临时 Worker 加入只读诊断 Processor，获得更具体的证据：

| Factory Path / 操作 | 观察 |
|---|---|
| 保留全部 JAR，原顺序 MapStruct → Lombok → binding | FULL 失败 |
| 原顺序，观察者放最前 | 首轮开始 `lombokInvoked=false`；最后一轮为 `true`，但 Mapper 未生成 |
| 原顺序，观察者放最后 | Lombok 初始化后已是 `true`；没有新的普通处理轮次，直接到最后一轮 |
| 相同 JAR，Lombok → MapStruct → binding | FULL 通过，出现生成源码的下一普通轮次 |
| 去掉 Lombok factory、只保留 Agent | 不能解决 |
| 临时再去掉 binding | FULL 通过，但这不是产品方案 |

binding 的[官方实现](https://github.com/projectlombok/lombok/blob/master/src/bindings/mapstruct/lombok/mapstruct/NotifierHider.java)
会读取 Lombok 的 `lombokInvoked` 信号，而 Lombok 的
[APT 初始化](https://github.com/projectlombok/lombok/blob/v1.18.30/src/launch/lombok/launch/AnnotationProcessor.java)
设置该值。当前 JDT 的
[IdeAnnotationProcessorManager](https://github.com/eclipse-jdt/eclipse.jdt.core/blob/R4_19/org.eclipse.jdt.apt.pluggable.core/src/org/eclipse/jdt/internal/apt/pluggable/core/dispatch/IdeAnnotationProcessorManager.java)
按 Factory Path 顺序逐个发现、初始化、交给处理轮次执行，未事先初始化所有 Processor。
所以 MapStruct 先看到 false 后推迟处理，Lombok 随后设置 true，但 ECJ 没有新的普通
round；MapStruct 在最后一轮报告未生成。这里的 ArrayList 不是实际缺失依赖。

也检查了 m2e commit `19318e846c191b7521fe57cf41f1fadb7b966562` 的
[Factory Path 配置](https://github.com/eclipse-m2e/m2e-core/blob/19318e846c191b7521fe57cf41f1fadb7b966562/org.eclipse.m2e.apt.core/src/org/eclipse/m2e/apt/internal/AbstractAptConfiguratorDelegate.java)：
完整解析后保留 JAR 顺序；反向调用 addExternalJar 只是因为该 API 插到列表头部。
没有看到按 Lombok/MapStruct 名称重排。本轮是源码对照和真实 headless Eclipse
Builder 观察，没有声称运行了完整 Eclipse IDE/m2e 导入流程。

## 已接入：初始化完成后再执行处理

`InitializedAnnotationProcessorManager` 继承 Eclipse 的
`IdeAnnotationProcessorManager`，第一次发现 Processor 时先通过父类完成全部配置
工厂的实例化、ProcessingEnvironment 注入和 init，再按原顺序返回给 Eclipse 的
RoundDispatcher。后续 process、注解匹配/认领、生成文件及多轮处理仍由 Eclipse 执行。

不删除 binding、不改 lombokInvoked、不按框架名称重排，也不额外制造处理轮次。
所有配置工厂的初始化会前置；不声称 init 就代表它们的生成工作已经完成。

通过 Eclipse 的 annotationProcessorManager 扩展点注册子类。JDT3.25 默认只选
第一个注册者、没有优先级参数，因此 Worker 启动时设置其已有 manager factory 字段，
明确选用该扩展；升级 JDT 时需同步检查这处内部接线。没有复制 Eclipse 的调度器，
没有改第三方 JAR，也没有新增版本检查或回退状态机。

变动集中在一个约40行的管理器子类、plugin.xml、现有 bundle 依赖声明以及 Worker
启动入口的一行安装调用。已经直接接入产品并重新打包，没有另建实验分支。
