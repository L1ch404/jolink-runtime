# Gradle 多模块：启动、reload 和 Fast Test

Gradle 多模块使用已有 `ModuleCompileSession` / `ModuleWorkspace`，没有第二套
编译管理器，没有改 `jdwp_adapter.py`，没有新增 MCP Tool 或 Action。

## 流程

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

同时有 Maven 和 Gradle 构建的项目，launch 和 test 都可以显式指定
`"build_system": "gradle"`，避免猜测使用哪套构建。

## 代码职责

- `GradleModuleGraph.java`：从 resolved project artifacts 收集所需模块及产物归属。
- `JoLinkGradleInitPlugin.java`：目标选择、每模块导出；处理 Gradle 默认 JDK/
  Test executable，补齐独立 Runner 所需的同版本 JUnit Platform launcher。
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

- composite / included build、testFixtures 或自定义 classifier 产物映射。
- 需要先运行代码生成任务的源码；不会偷偷执行正式 Gradle 编译。
- 一个模块 main/test 需要不同 compiler/Processor 配置时，目前不能在同一个
  JDT 工程中分别表达。现有明确的 Test filters 等配置也没有在本轮扩展。
- Gradle 单Project此前的部分限制仍在；这轮接的是多Project公共模型。
- 多模块真实 Windows 尚未执行；本轮是 macOS 实测，不声称 Windows 已验证。

这些是后续支持缺口，不作为“已经支持这些项目”的依据。
