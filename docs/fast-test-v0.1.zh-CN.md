# joLink Fast Test

Fast Test不是`mvn test`或`gradle test`的包装。构建系统只负责通过Probe导出测试
Build World；main/test编译由持久JDT workspace完成，测试由独立Runner JVM执行。

## 测试进程的运行配置

不需要新增MCP参数，直接沿用项目自己的配置：

- Maven Surefire/Failsafe的`environmentVariables`、`excludedEnvironmentVariables`、
  `workingDirectory`、`enableAssertions`由Probe读取Maven合并后的goal配置，并通过
  Maven表达式求值器解析。父POM、Profile、execution配置使用同一条路径。
- Gradle直接使用Probe已经导出的Test任务环境、工作目录和断言开关；不解析DSL文本，
  不按项目名或JUnit/TestNG类型分支。
- Runner继承当前进程环境，再应用配置的覆盖/删除；空字符串与删除是两回事。
  Maven同名变量同时出现在排除列表和显式配置中时，显式配置优先。
- 工作目录不存在时Runner会创建；路径是普通文件或无法创建时，返回
  `TEST_WORKING_DIRECTORY_UNAVAILABLE`。Maven无法解析配置中的目录表达式时同样报告
  配置错误，不创建名为`null`的目录，也不因此阻止不使用该测试配置的应用启动。
- 默认启用Java断言；显式关闭和已有JVM断言参数可以生效，不再被Runner末尾的`-ea`
  覆盖。这只影响Java的`assert`，不禁用JUnit/TestNG的断言方法。

只持久化明确的环境覆盖与删除，不缓存整个宿主环境。重开MCP后，普通继承变量来自
新进程；Maven已知POM/父POM/settings等配置中的`${env.NAME}`引用纳入现有缓存输入。
运行配置变化会刷新模型，但不加入JDT编译身份；编译配置和源码未变则复用class。
旧Test模型升为v11后首次重新Probe，不删除JDT workspace。

Gradle仍沿用已有配置输入追踪；任意脚本隐式读取其他环境变量/外部文件的完整失效追踪
未因此解决。当前测试执行配置与开发调度策略如下，不等同于完整Maven CI生命周期。
实际验收见[兼容性跟进](java-compatibility-followup-2026-09.md)。

### 按所选测试使用对应运行环境

Probe保存Surefire `test`、Failsafe `integration-test`执行项的有效配置，不把两者
所有字段合在一起作为项目拒绝条件。选择器按导出的include/exclude规则匹配首个执行项；
没有匹配时使用首个配置作为显式开发测试的默认环境，不因此丢弃调用者点名的测试。
目前支持常见Ant、类/方法模式和普通`%regex`，不是完整移植Surefire/Java正则引擎。

同一次请求中需要不同执行配置的测试按组顺序启动Runner，计数/失败结果合并；所有组
共用一次JDT编译和现有执行时间预算。换测试选择不重建编译模型，跨MCP仍可从缓存恢复。
结果的`test_execution_ids`说明使用了哪些配置，不返回完整环境或JVM参数。

支持常见`argLine`与`@{...}`迟绑定、系统属性（含properties文件）、额外运行classpath、
测试JVM和已有运行字段；properties/include/exclude文件纳入配置输入。JUnit4 category、
JUnit5 tag/engine/configurationParameters及TestNG group通过框架API执行；过滤后完全
没有匹配测试仍报告失败。Gradle已导出的JUnit Platform tags/engines复用同一Runner接线。

Maven层的parallel/threadCount/fork/retry等不再阻断显式测试。每个配置组使用一个临时
Runner，未复现的策略返回`not_applied_test_settings`；这不代表并发/重试行为已经验证。
不关闭测试框架自身的配置或注解行为，例如JUnit5配置参数仍可启用框架并行。
报告格式使用joLink自己的输出。不同测试数量不再触发“单方法放行、多类拒绝”的分支。

没有执行Maven pre/post-integration-test服务编排，也没有遍历同一个测试的全部CI执行
矩阵。复杂suite XML、自定义provider/classloader、未映射运行字段仍按所选执行项报告，
不会因另一份无关Failsafe配置有问题而拒绝所有普通单测。

## 状态摘要与结果详情

`java_fast_test` 支持 `run`（默认）、`cancel` 和 `result`。
`java_status(status).fast_test` 始终是摘要：任务 ID、阶段、结果、计数和耗时。
`run` 通常也返回摘要；如果本次回复时已经编译失败，会直接返回编译诊断（文件、行号、
错误信息、错误总数和截断标记），无需再调用 `result`。覆盖首次全量编译、增量编译，
以及尚未修复的已有编译错误，不输出完整编译文件清单。

若调用已因 `timeout` 返回后台任务，之后才发生的错误仍通过 `result` 读取；不会补发
或改写已经返回的响应。其他失败摘要提供可直接调用的 `next_action`。读取详情用
`java_fast_test(action="result", test_run_id=...)`，不编译、不启动 Runner、不消费结果。
活动任务尚未结束时返回当前观察；完成后返回已有诊断和失败详情。
只复用当前 MCP 进程已有的活动/最近完成任务，不增加历史数据库或对话已读状态；
已不保留的 ID 返回 `TEST_RUN_NOT_FOUND`，不会误读另一次测试。
所有测试输出保留 `compiled_source_count`，不再默认输出完整编译文件清单。

## 流程

```text
读取本地Test Build World缓存
├─ POM/父POM链或Gradle构建文件未变化 → 直接使用
└─ 没有缓存、配置或生成输入变化 → Probe执行已声明的源码准备步骤并保存结果
                ↓
打开持久Test JDT workspace
├─ 没有编译结果 → FULL编译main/test
├─ 源码没有变化 → 不编译
└─ main/test源码有变化 → INCREMENTAL编译实际变化文件
                ↓
实际编译完成 → 保存JDT构建状态和源码索引（无改动时跳过）
                ↓
启动一次Test Runner JVM
                ↓
返回结构化结果
```

这条路径不执行Maven`test-compile`、Gradle`classes/testClasses`，不创建源码或
resource快照，不比较Maven/JDT class输出，也不在测试前后全量哈希Build World。
main/test资源由原生资源步骤处理，Runner读取处理后的资源；同名时test优先。
Maven同时保留原生资源输出路径，支持通过文件路径读取测试资源。资源未变则复用，
普通Java修改不重新运行资源准备。详见[执行环境与资源准备](fast-test-environment-resources.zh-CN.md)。

Maven 项目声明了源码/资源准备执行项时，Probe 使用 Maven 的实际执行计划运行
这些 goal，并包含默认main/test资源处理，再导出 source roots；不进入 compile/test 生命周期。语法/模板输入变化
会更新生成结果，普通 Java 变更仍直接增量，见[源码准备实测](maven-source-preparation.zh-CN.md)。

同一个 Worker 内，每个参与测试的模块分别使用 main / test 两个持久 JDT 工程。
test 工程依赖 main；语言级别、编码、`-parameters` 和 Processor 路径各自配置，
不再要求两边相同。依赖构建顺序与增量传播仍交给 Eclipse Java Builder，一轮
workspace build 后保存一次，不为每个工程启动 Worker。

Maven Surefire 的 `runOrder=alphabetical/reversealphabetical` 已映射到 Runner 的
实际测试类执行顺序，不只排列发现列表。JUnit4/5 和 TestNG 均有真实 MCP 回归；
TestNG 保持同一次 suite 执行，不因排序重复运行 BeforeSuite/AfterSuite。
其他随机、历史耗时等排序模式本轮不扩展。

Maven 冷入口由 Probe 在同一次 Maven session 中，按各模块解析后的真实测试源码根
定位测试类，再导出目标及所需上游模块；不再先按 `src/test/java` 猜模块，也不额外
调用 help:effective-pom。普通项目与自定义目录、父 POM 继承的目录使用同一条路径。

直接给出Reactor子模块的`project_path`时，joLink沿模块声明找到外层聚合根，使用根的
Wrapper/POM运行Probe；用户选定的子模块仍是测试目标，Runner工作目录不变。所需上游
继续走已有JDT依赖工程，不要求先`mvn install`。根POM和`.mvn`配置也纳入缓存，
根配置未变、源码未变则继续复用。Profile仍需由Maven的实际配置激活，不自动开Profile。

Maven的main `includes/excludes`与test `testIncludes/testExcludes`分别作用于各自的
原始源码根，使用Ant风格的相对路径规则；先筛选、再归一化package目录。FULL、增量和
显式`source_files`使用相同规则；被排除源码变化不触发编译，过滤规则本身改变则更新模型。
普通WAR项目里的Java测试也走同一条JDT/Runner流程，不要求packaging为jar；这不包含
WAR部署或外部Servlet容器管理。当前验收与剩余边界见
[m2e对齐记录](java-compatibility-followup-2026-09.md#m2e对齐第一批源码过滤与war测试)。

Gradle 正常加载 buildSrc 和 included builds 后，Probe 读取配置完成的 SourceSet/
JavaCompile/Test 对象。单项目与多项目共用现有模块转换；构建逻辑目录不再触发拒绝。
编译 ArgumentProvider 通过其 asArguments() 导出实际参数，不因 Provider 的存在而
笼统拒绝；具体 javac 专用参数仍按实际能力处理。

Gradle 加载配置时可能编译构建插件本身，这不等于执行业务 compileJava/test 任务。
构建文件、构建逻辑实际源码/资源目录参与现有缓存；业务源码改动仍只走 JDT。
目录中的新增、修改、删除会刷新模型；`.gradle` 临时缓存不作为脚本目录纳入。

Maven 的 `useIncrementalCompilation` 只控制 Maven Compiler Plugin 的源码选择策略，
不配置 JDT。无论它是 `true` 还是 `false`，Fast Test 都继续使用持久 JDT workspace
和实际源码变化进行复用/增量编译，不因该参数拒绝项目。

Test Build World和JDT workspace都保存在joLink本地缓存。MCP关闭后，下一个MCP
进程可以直接打开；配置未变且状态可复用时不需要重新Probe或主动FULL。
JDT自身仍可能要求FULL，实际build kind和原因以日志为准。构建配置缓存只检查小型配置文件：Maven
的当前POM、本地父POM链和`.mvn`配置，或Gradle的build/settings/properties和Wrapper
配置。依赖目录和源码树不做内容哈希。

FULL/增量完成时立即请求Worker保存完整构建状态，而不是只写joLink自己的
`state.json`、等Worker退出才保存JDT状态。实现和暂缓处理项见
[JDT状态持久化记录](jdt-workspace-persistence.zh-CN.md)。

可选设置 `JOLINK_JDT_GC_AFTER_BUILD=1`：每轮实际 FULL/增量保存完成后，向编译
Worker 请求一次 GC，再启动 Runner。正常返回编译错误也请求，无改动复用不请求；
默认关闭。GC 耗时包含在相应编译总耗时中，细节见
[Worker 内存与 GC](jdt-worker-memory.zh-CN.md#可选的编译后-gc2026-09-10)。

## 调用

调用独立工具`java_fast_test`，不需要先启动业务应用或配置IDEA启动项。
省略`action`默认执行`run`；显式写`"action":"run"`也可以。旧的
`java_application(action="test"/"cancel_test")`公开入口已移除，不保留双入口。

```json
{
  "action": "run",
  "project_path": "/workspace/project",
  "source_files": [
    "src/main/java/example/Service.java",
    "src/test/java/example/ServiceTest.java"
  ],
  "tests": ["example.ServiceTest#works"],
  "timeout": 60
}
```

`source_files`可以省略。joLink会用持久源码mtime/size索引自动发现main/test变化，
调用方显式提供的文件与实际变化文件合并后一次增量编译。

可选`java_home`指定本次项目准备和默认测试JDK；`maven_home`指定Maven安装，
`maven_settings`指定用户settings文件。无需修改全局环境或重连MCP。
项目明确配置的测试JVM仍按项目配置，编译source/target不会因此被改写。

`timeout`现在与启动统一表示本次工具调用的同步等待时间，默认30秒；大于30按30秒等，
不报错；0表示提交后立即返回。该时间包含下载、准备和编译，不是每阶段各等一次。
旧的启动等待参数已删除，不再兼容接收。

短测试直接返回结果；仍未完成的测试返回当前状态和原`test_run_id`，使用`java_status`观察，
或用`java_fast_test(action="cancel", test_run_id="...")`取消。断言失败仍是`ok=true, passed=false`；编译、Runner基础设施、
超时等失败返回`ok=false`。

同步等待到期不停止测试，Runner使用独立的内部300秒执行上限（Bootstrap上限不变）。
下一步建议提示LLM自行评估等待间隔，可用sleep或PowerShell的Start-Sleep等待后查询，
不固定建议秒数，也不生成终端命令。取消MCP等待本身不取消后台任务；要取消任务仍使用
`java_fast_test(action="cancel")`并传入原test_run_id。MCP退出依然通过原有生命周期清理任务。

状态继续由`java_status(action="status")`返回，不新增测试状态系统。新入口复用原Manager、
JDT Worker、Probe模型及编译缓存；工具改名不改变缓存身份。重连MCP才能获取更新后的工具列表。
首次准备可能需要数分钟，Fast指重复开发验证时的增量复用，不承诺完整替代原生构建生命周期。

### 2026-09-16 独立工具入口验收

- 产品代码只改工具Schema、分发和MCP接线；沿用原FastTestManager、Worker、缓存、Runner和结果结构。
  `java_application`、`java_debugger`不再暴露测试参数，公开测试入口只有`java_fast_test`。
- 普通回归879 passed / 13 skipped；另行开启原有Fast Test产品回归6组全部通过，包含
  JUnit4/5、Lombok、Reactor、TestNG、Gradle以及增量/失败恢复等路径。
- 27组真实stdio MCP用例通过，覆盖无IDEA/无业务应用的JUnit4与TestNG、编译错误、断言失败、
  缓存重开、32秒测试超过30秒回复预算后继续完成、等待期间取消、下载进度和后台恢复、
  Profile/settings/双MCP模型回归、Gradle Groovy/Kotlin多模块、排序和main/test跨JDK编译级别。
  测试已经改为调用新工具，不依赖旧公开入口的兼容转发。
- 实际开源项目通过新入口复测：MapStruct＋Lombok所选1项、现代Petclinic所选3项均通过；
  两者重复运行编译数量均为0。未修改这些项目源码。双构建配置的项目显式选择Maven。
- 初次Gradle产品脚本缺JUnit依赖变量，补变量时曾误配JUnit4；改为该样本实际要求的
  JUnit5.11.4/Platform1.11.4后通过。Java17矩阵的缺失环境变量也已补齐重跑，不把跳过当通过。

本轮验证工具可发现、可调用及原有行为保持，不等于已量化GLM/CodeBuddy的自主选择率。
后续应重连MCP确认新工具列表，再用不提示joLink的新会话观察模型是否主动获取说明和调用。

每次测试仍启动独立Runner JVM，避免测试之间共享静态状态。当前时间字段：

- `bootstrap_ms`：缓存读取/Probe、Worker启动以及首次FULL；
- `source_scan_ms`：通过mtime/size查找变化源码；
- `compile_ms`：JDT增量编译；
- `runner_ms`：Runner JVM启动和测试执行；
- `total_ms`：完整调用。

## 已验证

- Maven JUnit4/5、TestNG、Lombok和Spring Configuration Processor；
- Gradle 8.10/8.14 JUnit5；Gradle 7.4.2真实多模块，JUnit4/Vintage/5，
  上游源码修改、失败恢复及持久workspace，见[Gradle多模块实测](gradle-modules.zh-CN.md)；
- 编译失败、断言失败、恢复、超时、取消和进程隔离；
- MCP进程退出后复用Test Build World与JDT workspace；
- 本地业务服务样本（项目标识已脱敏）：423个main源码、34个test源码，首次约10.3秒；同MCP后续约
  1.52秒；新MCP复用约2.33秒；单main源码增量编译约36～43ms。

## 当前边界

- Maven 显式 `annotationProcessorPaths` 已接入：Maven 解析完整加载路径（含传递依赖、
  辅助类和资源 JAR），缓存后交给 Eclipse Factory Path。main/test 不同路径、显式
  Processor 名称和 `-A` 参数均已接入。指定名称时只加载所选处理器，不要求服务声明；
  Maven 未指定 Processor 路径时使用其编译 classpath。普通 MapStruct、MapStruct＋Lombok binding
  样本均通过；Worker 先完成处理器初始化再按原顺序执行，见[路径接入与实测](maven-processor-path.zh-CN.md)。
- 当前产品已升级JDT3.46，支持Java8～26的source/target；已实测8/11/17/21，
  包括main8/test17和main11/test21。目标系统库、Runner JDK仍按项目配置选择。
  Worker默认使用joLink管理的Temurin21，旧Lombok1.18.20问题暂不处理。
  详见[升级与实际结果](jdt-346-upgrade.zh-CN.md)，不将编译器支持范围等同于所有项目通过；
- 标准 Error Prone 检查模式不在JDT中执行：Fast Test继续编译和运行测试，结果显式
  返回`skipped_compiler_checks: ["Error Prone"]`，这些检查仍需原生构建/CI执行。
  真正的APT、Processor选择和`-A`参数保持原样；不是删除整个Processor Path。
  自动改源码的patch模式、argfile和未知javac插件没有因此获得支持。详见
  [U4接线与实测](java-compatibility-followup-2026-09.md)。
- Maven准备阶段支持有效POM/插件JAR的m2e执行映射，被忽略的步骤返回
  `skipped_build_steps`；必要生成器和资源处理保留。MyBatis默认profile的87项所选
  测试已通过，未要求关闭format/license；见[执行映射与准备边界](maven-source-preparation.zh-CN.md)。
- Runner支持显式Class或Class#method选择；
- `-Xpkginfo:always` 已映射为 JDT 默认输出行为：空包说明、SOURCE/RUNTIME 注解均有
  package-info.class，并验证了增量修改、删除、恢复；没有统一忽略其他 javac 参数；
- Maven Reactor已将目标和上游模块接入持久JDT工程，支持上游main源码变化，
  也支持显式依赖上游test-jar的测试；完整流程见[多模块JDT](jdt-modules.zh-CN.md)；
- Gradle多Project使用相同的JDT模块工程；读取解析后的api/implementation/runtimeOnly
  依赖，只构建所需模块。构建配置改变时重新Probe，普通源码修改只走增量；
- 已接入 Maven 显式声明的源码准备阶段，验证了 ANTLR 与模板生成；protobuf/OpenAPI
  等具体项目仍需实测，依赖额外生命周期或未导出输入的生成链没有因此宣称支持；
- Runner JVM尚未保活，Spring测试的大部分后续耗时通常在Runner启动和框架初始化。

此前拆分 main/test 工程时，旧 Test Build World 需重新导出并建立一次新 workspace；
新布局建立后继续复用。不是每次 test 都重建，也不是每次模型重新Probe都会FULL。

2026-09-12 后续的名称/参数接入同时更新启动与 Test 缓存模型；旧缓存需要重新导出一次，
避免复用之前未保存显式名称的模型。实现及真实结果见[Processor 配置](maven-processor-path.zh-CN.md)。
