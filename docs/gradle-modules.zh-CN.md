# Gradle 多模块：启动、reload 和 Fast Test

Gradle 6.9.4 使用 `org.gradle.jvmargs` 启动一次性 daemon 时，初始化脚本优先从
`StartParameter` 读取本轮 Probe 的参数。无需用户删除 JVM 参数或手工指定 JAR。
此场景在 JDK 11 的多模块 Fast Test、应用启动、restart 及重开缓存上有真实 MCP 回归。

JDT 镜像同时遵循已声明的 SourceSet 和 JavaCompile 字符串过滤：两层 includes 必须
各自命中，任一层 exclude 命中就排除。RoaringBitmap 1.0.0 用 Gradle 6.9.4
将 module-info.java 排除在普通 Java 8 编译之外。对 `org.roaringbitmap.TestUtil`
实测冷、暖均为 18/18，暖跑无编译；改源码后一个断言变红、更新预期后全绿，
重开也复用。这是所选测试结果，不代表其多版本 JAR 的发布流程已经验收。

Gradle 项目通过同一个 Probe 和同一套 MCP 工具使用两条编译路径。普通 Java 使用
持久 JDT；选定范围需要混编、通过 `SourceSet.output.dir(...)` 声明额外输出，或定制了 Java 编译动作时，
Gradle 执行自己的任务。无需指定后端。

如果项目传了当前 JDT 模型不能表达的 javac 参数，也由项目自己的 Gradle 执行原编译任务，
而不默默删除参数。对于从声明的文件输入动态计算参数的 Gradle Provider，不能把一次读到的
参数认作永久配置；沿用原生后端及文件变化跟踪。只在 test 配置的参数不会改动应用 main 的编译后端。
原生编译失败时保留 Gradle 诊断，不发布部分输出。此路径不表示所有 JPMS、隐藏环境输入都已支持。

## 混合源码：编译交给谁

- 看目标及依赖模块的源码集和输出，不看仓库名，也不把 `build.gradle.kts` 当成 Kotlin 业务源码。
  应用只看 main，Fast Test 还看目标的 test。仅测试使用 Groovy/Kotlin 的项目，应用可以继续用 JDT。
  配了额外语言输出但没有对应源码时，保留 Java 快路径；新增首份语言源码后重新选择。
- 混编范围里，Probe 导出源根、classes 任务路径、各个 class/资源输出目录；由项目 Wrapper
  调用该目标及必要依赖的 classes/testClasses 任务。执行次序、工具链、APT、编译器插件和
  增量判断归 Gradle。不会另让 JDT 重编这批 Java，也不运行 clean、Gradle test 或完整 build。
  自定义任务若是 classes 的依赖，依然会被执行。
- Gradle 写项目原有 build 目录，复用自己的编译缓存。**应用 JVM 和 joLink Test Runner 使用
  两份独立的持久输出**：仅在各自请求的构建成功后同步变更（包括删除），不直接从对方
  正在编译的 class 目录加载。失败不发布部分编译结果。外部依赖 JAR 仍用原路径。
- 无源码/资源变化时直接复用，不为测试另外启动 Gradle。发生修改时由 Gradle 判断各任务
  是否 up-to-date、是否全编。返回 `compile_backend=gradle_native`；实际执行 Gradle 时
  没有精确计数就不返回 `compiled_source_count`，确认跳过编译时返回 0。
  可在运行结果详情中查看 `native_compile_log`，编译失败时查看诊断及 `compile_log`。
- 应用仍使用原有 HotSwap / restart 判断；测试仍通过独立 Runner 只运行请求的选择器。
  `gradle_native` 不是完整 CI 的替代。当前针对普通 JVM main/test 和 classpath 应用；
  混编 JPMS、Android、自定义变体和所有隐式生成输入并未验证。文件变更检测沿用本地
  mtime/size 约定，不承诺跨进程同时编辑/构建的原子快照。

回归入口：[联合编译与测试隔离](../tests/e2e/test_gradle_joint_compile.py)。

### 已声明的额外输出

当解析到的项目依赖属于另一个 SourceSet（例如 `testFixtures(project(":lib"))`），
Probe 按实际 artifact 找到生产者，沿该 SourceSet 的依赖继续解析，交给 Gradle
构建对应 classes 及已选择的 Jar。若运行路径选中 JAR，使用原生任务生成的实际 JAR
的私有副本，不用 main classes 或手工拼出的目录冒充；仅测试使用 fixture 不会改变
应用的编译后端。未选中的 SourceSet 不要求编译。

有些项目需要在编译之后再复制或生成文件供类加载器读取。Probe 读取 Gradle
登记的输出目录和任务依赖；既支持显式 `builtBy`，也支持任务挂在 `classes.dependsOn`
且实际写入注册目录的子目录。下游依赖上游的普通项目 JAR 时，运行路径也保留这些输出。
只有 test 声明时，应用 main 仍使用原来的后端。

重跑时检查已声明的文件输入，包括原本为空的文件树和注册目录中不归生成任务所有的文件。
文件树沿用 Gradle 导出的 includes、excludes 与大小写设置，而不是从一次构建中列出已有文件。
无变化不启动 Gradle；有变化则由 Gradle 决定哪些任务实际重做。
回归入口：[Fast Test 与真实 HTTP 启动 / restart](../tests/e2e/test_gradle_declared_outputs.py)。

### 原地改写已有 class，不声明额外输出

项目或插件可以给 `compileJava` / `compileTestJava` 追加 `doFirst`、`doLast`。
Probe 比较配置完成后的任务类型和动作列表，与**当前 Gradle 发行版**给标准 JavaCompile
安装的动作类型和顺序。不同就由 Gradle 执行整个任务；不尝试从回调内容猜测它是改 class
还是只打印日志。只定制 test 不影响应用 main；上游 main 的定制会影响使用它的下游。
回调显式注册的本地文件、目录与文件树进入同一套变更检测。若某个直接依赖的任务声明
自己生产该输入，Probe 沿这条文件依赖链追溯原始输入；暖缓存比较原始文件，不把中间产物当成新改动。
没有改动依然跳过 Gradle，
所以这不适合依赖未声明的外部输入或要求每次调用都产生副作用的回调。
回归入口：[测试与应用实际读取改写后的字节码](../tests/e2e/test_gradle_compile_actions.py)。

## 纯 Java 流程

1. 从工程根目录或子项目目录找到 Wrapper。Probe 根据主类或测试选择器确定目标。
2. 首次由 Gradle 配置工程并解析依赖，只执行导出任务，不执行 Java 编译或打包。
   Probe 读取 Gradle 解析后的项目 artifact，而不是自己猜 `project()` 或版本。
   只收集目标需要的模块，含 runtimeOnly 模块；无关模块不交给 JDT。
3. 将模块的源码、依赖、Processor、Java level、编码与输出转换成公共 modules 模型。
   本地 jar/classes 依赖映射为 JDT project reference，外部依赖保留 Gradle 解析结果。
   `api`、`implementation` 和 `runtimeOnly` 的可见性跟随各个已解析的 classpath。
4. 打开本地持久 workspace：首次 FULL；已有状态则检测源码变化，没变化不编译，
   有变化交给 JavaBuilder 做增量及下游传播。一个 Worker JVM 管理多个工程。
5. 启动应用或 Test Runner 时使用当前 JDT 输出；资源直接从源码资源目录读取。
   reload 复用现有异步 Attempt 和 JDWP HotSwap。

Build World 存在本地 JSON 中。多模块构建脚本、settings、已有 init scripts、
Wrapper 配置和相关 Gradle 环境有变化时重新 Probe；普通 Java 修改不会触发 Probe。
这里只比较小配置文件，不重新扫描依赖 JAR、哈希整棵 class 树或复制整份输出。
关闭 MCP 后 workspace/源码索引保留，下次直接打开。

2026-09-27补充：Probe会记录Gradle实际应用的本地Groovy/Kotlin脚本，包括嵌套、
变量路径和项目外引用，并入上述配置清单。不是解析`apply from`文本，也不扫描整个
业务仓库；没有变化时不为检查脚本而重启Gradle。远端内容及未执行分支的隐藏输入尚未
完整覆盖，验收和范围见[U10跟进记录](java-compatibility-followup-2026-09.md)。

同时有 Maven 和 Gradle 构建的项目，launch 和 test 都可以显式指定
`"build_system": "gradle"`，避免猜测使用哪套构建。

## 代码职责

- `GradleModuleGraph.java`：从 resolved project artifacts 收集所需模块及产物归属。
- `JoLinkGradleInitPlugin.java`：目标选择、每模块导出；处理 Gradle 默认 JDK/
  Test executable，补齐独立 Runner 所需的同版本 JUnit Platform launcher。
- `GradleConfigurationInputs.java`：收集构建逻辑输入和实际应用的本地脚本；仍由
  公共配置快照完成后续缓存比较。
- `gradle_module_world.py`：Gradle facts 转换成公共 modules 数据。
- 现有 Runtime/Test 转换层及缓存：接线、保存配置；后续仍走原有 JDT 服务。
- `jdt_modules.py` / `ModuleWorkspace.java`：直接复用上一轮 Maven 多模块实现。

不再因为 Gradle 版本没有出现在旧实验枚举中而提前拒绝；版本兼容性以实际执行为准。
本轮实测版本是 7.4.2、8.10、8.14，不能据此宣称所有 Gradle 版本都已验证。

## 真实 MCP 验证

### Groovy / Kotlin DSL fixture

三层 `base → core → app`、独立 runtimeOnly 模块、一个故意含错误的无关模块。
覆盖：

- 冷目录无旧 classes/JAR，直接 JDT 编译并启动。
- 上游常量修改、编译错误及恢复，实际下游测试和 HotSwap 结果。
- `api` 改为 `implementation` 后，下游非法直接引用产生编译错误。
- runtimeOnly 模块运行时可加载，编译时不可见。
- 上游资源读取、测试系统属性和环境变量。
- 上游模块使用 Lombok，实际调用生成的 getter。
- 连续三个 MCP 进程之间的缓存/增量状态复用。
- 项目路径别名与旧缓存中的别名统一规范化；成功 HotSwap 后的记录错误不会
  再伪装成 `applied=false`，而是保留应用事实并返回 post-apply warning。
- 主动取消 Test Runner 返回 `TEST_CANCELLED`；未取消时异常退出仍返回 Runner failure。
- Groovy、Kotlin DSL，中文及空格路径，全程无模块 `build/classes` 输出。

测试入口：`tests/e2e/test_gradle_modules.py`。Fixture launcher 调用本机已安装的
真实 Gradle；下面两个公开项目使用仓库原有 Wrapper。

### JsonPath 2.9.0

[仓库版本](https://github.com/json-path/JsonPath/tree/af7e516c69df680a6584fca7180ef082eb67c96c)，
Gradle 7.4.2 / JDK 8。没有修改构建文件或依赖版本。

- 只编译 `json-path-assert` 和依赖的 `json-path`：116 个 main Java 源码、12 个 test 源码。
- 下游 9 个测试类：96 项中 85 passed、11 skipped、0 failed。
- 同一 MCP 中无修改重复测试约 0.40 秒；新 MCP 打开持久 workspace 后约 1.10 秒。
- 将上游 `JsonPath.parse(String)` 临时改为解析空对象，下游 20 项中 14 项实际失败；
  不是只看编译返回值。同一 MCP 中只重编这一个上游文件，编译约 46ms；
  恢复后增量约 38ms，20 项全部通过。测试后源码已恢复。

### Spring 多模块指南

[仓库版本](https://github.com/spring-guides/gs-multi-module/tree/ef45093909f4997d25c7a25ce0300745cef7b8c0)，
Spring Boot 2.7.1 / Gradle 7.4.2 / JDK 8。

- 原始 main 源码由 JDT 编译，应用 TCP ready，真实 HTTP 返回 `Hello, World`。
- 修改 library 的 `MyService.message()`，reload 后 HTTP 返回 `jolink-updated`；
  恢复源码再 reload，返回原值。新 MCP 复用 workspace，再次完成相同闭环。
- 实测 reload 总耗时约 40–212ms，单文件编译约 10–181ms；不是企业项目性能保证。
- 该历史版本测试源码用 JUnit 5，但 Gradle 未调用 `useJUnitPlatform()`。
  原配置测试失败已如实保留；通过本次测试专用 init script 显式补该配置后，
  Spring 上下文测试通过，warm 约 2.16 秒（主要是独立 Runner 中的 Spring 启动）。
  不把修正后的测试结果当作原配置无条件通过。

两个公开项目遇到本机 JVM HTTPS 下载失败，测试使用了临时本地下载转发器，远端
仍通过正常 HTTPS 获取依赖；没有关闭证书验证，也没有修改依赖版本。网络绕行代码
没有放进产品。上述耗时不包含第一次下载全部依赖的时间。

可重复执行已有项目：

```bash
uv run python scripts/validate_gradle_modules_project.py /path/to/project \
  example.SomeTest --java-home /path/to/jdk --report /tmp/gradle-test-report.json
```

## 当前尚未完成

- 作为业务依赖的 composite / included build，以及无法从当前构建的 SourceSet 输出或对应 Jar 任务识别的自定义 artifact。
  用于构建逻辑的included build已支持，不应与业务产物映射混为一项。
- 需要先运行代码生成任务的源码；不会偷偷执行正式 Gradle 编译。
- main/test不同compiler/Processor配置已通过分工程接入，不再作为未支持项；
  其余Test filters等配置仍按[当前跟进清单](java-compatibility-followup-2026-09.md)推进。
- Gradle 单Project此前的部分限制仍在；这轮接的是多Project公共模型。
- 多模块真实 Windows 尚未执行；本轮是 macOS 实测，不声称 Windows 已验证。

这些是后续支持缺口，不作为“已经支持这些项目”的依据。
