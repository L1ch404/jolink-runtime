# joLink Fast Test

Fast Test不是`mvn test`或`gradle test`的包装。构建系统只负责通过Probe导出测试
Build World；main/test编译由持久JDT workspace完成，测试由独立Runner JVM执行。

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
main/test resource源码目录直接加入Runner classpath。

Maven 项目声明了源码/资源准备执行项时，Probe 使用 Maven 的实际执行计划运行
这些 goal，再导出 source roots；不进入 compile/test 生命周期。语法/模板输入变化
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
进程可以直接打开；不会重新Probe或FULL。构建配置缓存只检查小型配置文件：Maven
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

```json
{
  "action": "test",
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

`timeout`现在与启动统一表示本次工具调用的同步等待时间，默认30秒；大于30按30秒等，
不报错；0表示提交后立即返回。该时间包含下载、准备和编译，不是每阶段各等一次。
旧的启动等待参数已删除，不再兼容接收。

短测试直接返回结果；仍未完成的测试返回当前状态和原`test_run_id`，使用`java_status`观察，
或用`cancel_test`取消。断言失败仍是`ok=true, passed=false`；编译、Runner基础设施、
超时等失败返回`ok=false`。

同步等待到期不停止测试，Runner使用独立的内部300秒执行上限（Bootstrap上限不变）。
下一步建议提示LLM自行评估等待间隔，可用sleep或PowerShell的Start-Sleep等待后查询，
不固定建议秒数，也不生成终端命令。取消MCP等待本身不取消后台任务；要取消任务仍使用
`cancel_test`。MCP退出依然通过原有生命周期清理任务。

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
- Error Prone 等 javac 专属插件不在本轮接入。后续方向是快速流程不执行这些质量
  检查、正式构建/CI保留；本轮只记录，尚未删除对应参数拒绝，也不静默忽略未知参数。
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

本轮 main/test 工程布局与缓存模型发生变化：旧 Test Build World 会重新导出并建立
一次新 workspace；新布局建立后继续复用。不是每次 test 都重建。

2026-09-12 后续的名称/参数接入同时更新启动与 Test 缓存模型；旧缓存需要重新导出一次，
避免复用之前未保存显式名称的模型。实现及真实结果见[Processor 配置](maven-processor-path.zh-CN.md)。
