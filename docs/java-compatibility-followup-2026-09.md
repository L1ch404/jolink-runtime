# 第一批开源兼容性：小范围修复与待讨论项

后续更新：用户已确认移除产品 direct-javac 路线。下面保留第一轮状态；旧 Petclinic
启动阻断现已消除，真实 MCP 启动、HTTP、reload、restart 已通过，见
[移除记录](direct-javac-retirement.zh-CN.md)。其他待讨论项不因此视为已解决。

2026-09-10，基于 `5d4877b` 后的工作区。原报告固定基线为 `e5b139e`；本轮不改写
原报告，也不把越过一个入口检查算作项目全流程通过。原始请求/响应和诊断留在本地。

第二项 Processor 后续进展：显式 Maven Processor 加载路径已接入，普通 MapStruct
样本真实 MCP 测试通过；后续接入初始化前置后，原样 MapStruct/Lombok binding 组合
也通过了真实 MCP FULL、测试及增量恢复。详见
[本轮实现、通用回归和剩余兼容问题](maven-processor-path.zh-CN.md)。

## 本轮处理

1. **显式选类不再被测试发现过滤拦住。** Fast Test 总是明确提供 Class/Class#method，
   按 Maven `-Dtest` 的语义覆盖 Surefire `includes/excludes`；不只对单方法处理。
   Failsafe 的发现过滤也不用于这一显式 Surefire/Fast Test 选择。
   其他尚未实现的排序、并行、工具链及 Failsafe 运行配置没有统一放开。
2. **不执行的配置不再作为已执行配置检查。** Maven Probe 不收集没有 goal 的
   Surefire/Failsafe execution 配置。Commons Lang 的 `plain` 配置就是这种情况；
   原生基线实际运行的是 `default-test`，不能因为 `plain` 写了随机排序就拒绝它。
3. **额外运行 classpath 正式接入。** 从 effective POM 读取
   `additionalClasspathElements`，相对路径以目标模块为基准，追加到 Runner
   classpath。单模块、Reactor 和缓存复用共用；不会把这些路径加到编译 classpath。
   共用 Surefire 配置读取，保留未被 execution 覆盖的 plugin 字段。
4. **允许未选中的 Gradle Test 任务存在。** Probe 仍导出默认 `test` 任务，
   但不再要求它是唯一 Test 任务。`retryTest` 等额外任务既不阻断，也不会被执行。
   本轮没有增加任意自定义 Test 任务选择或额外 SourceSet 支持。
5. **启动错误不再丢失。** 单模块 JDT-first 启动不再要求 IDEA Make 提供正式
   Maven 编译基线。构造 JDT Plan 的实际异常直接返回，不再吞掉后只报
   `JDT_BUILD_WORLD_UNAVAILABLE`。隐式插件 goal 无法建模时附带 plugin/goals/reason。
   Gradle 模块转换异常同样保留自身错误码；main/test Processor 差异列出 artifact 名称。

过滤语义依据：[Surefire test 参数](https://maven.apache.org/surefire/maven-surefire-plugin/test-mojo.html#test)。
Maven Probe 已重新构建为 `0.1.0-fasttest12`，避免同一坐标覆盖旧内容；Gradle Probe
同步重新打包。没有修改 Worker、升级 JDT、删除项目插件或改业务源码绕过限制。

## 同版本开源项目实测

使用原报告对应 SHA 的独立 clone、真实 stdio MCP 和独立缓存，保留原生依赖/语言级别。

| 样本 | 本轮结果 | 仍未完成 |
|---|---|---|
| Petclinic 旧版 / Java 8 | 冷 JDT + Fast Test 22/22；无改动复用；改 Controller 重定向后出现预期 2 项失败，恢复 22/22；编译错误及恢复；跨 MCP 重开通过 | project launch 仍被旧插件检查阻断，未验证 HTTP/reload |
| Commons Lang 3.12.0 / Java 8 | 已越过 Surefire 拒绝；未删除 `plain` 配置 | test-only `jmh-generator-annprocess-1.27.jar` 与 main Processor 路径不同，尚未进入 JDT |
| Mockito 5.14.2 / Java 17 | 已越过额外 `retryTest` 拒绝，实际到达 Gradle→JDT 模块转换 | `GRADLE_COMPILE_CONFIGURATION_UNMODELED`，仍未编译/运行所选测试 |
| Checkstyle 固定快照 / Java 17 | 已越过 Surefire 额外 classpath/过滤和 Failsafe 发现过滤拒绝 | `FAST_TEST_COMPILER_ARGUMENT_UNSUPPORTED`，4 处 compiler_extension 参数，未进入 JDT |
| Petclinic 新版固定快照 / Java 17 | 启动明确报告只支持 Java 8/11 source/target，不再混成 Plan 缺失 | Java17 源码级别仍不支持 |

Mockito 首次离线尝试因原仓库 URL 对应缓存不全失败；复用了上次报告的本机镜像
仓库 URL 后，在离线模式下到达产品模块转换。没有启动新的下载转发器或降低远端 TLS
校验，执行前关闭了 Build Scan 发布。这个环境调整不是产品修复。

## 先记录，单独讨论

### 启动仍保留旧的单模块路线

Petclinic 旧版真实诊断定位到 `consume_jdt_build_world_plan()` 调用的旧
`_ensure_no_unverified_build_transforms()`。实际触发点是
`org.codehaus.mojo:cobertura-maven-plugin` 的 `clean/check` 隐式 goal。
本地官方插件 2.7 的 descriptor 显示 `check` 默认在 verify，且会 fork 到
Cobertura 的 test lifecycle；不能只根据名称断言它没有编译副作用。

需要讨论的是：单模块启动是否统一复用已有 Maven-native Probe/模块 Build World 路线，
从 JDT 启动路径移除为旧 direct-javac 编写的前置模型，而不是继续逐插件加白名单。
这会涉及启动输入、缓存和生成资源的边界，本轮没有顺带替换整条流程。

### Processor 不能仅靠删除拒绝条件放开

- 原报告 C02 的显式 processor path/name/options 需要准确传入 JDT APT。
- 本轮又实际遇到 Commons Lang 的 test-only JMH Processor。当前一个 JDT 工程共用
  main/test Processor 配置；不能不加区分地取并集，然后声称忠实复现了两套编译。
- 下一轮应一起讨论 main/test 的处理器配置如何表达与执行，不能仅加一个允许的枚举值。

### 非标准测试目录与模块选择（原报告 C04）

Guava 测试位于 `guava-tests/test`。当前冷启动选择模块早于 Probe，还按
`src/test/java` 查找。应以 Maven 导出的实际 test source roots 选择模块，而非按仓库名
或追加一个固定 `test/` 目录特例。本轮未改变 Probe/模块选择时序，也未宣称 Guava 通过。
后续还需处理它实际启用的 Surefire alphabetical 排序及可能的其他编译边界。

### Java17、编译扩展和 build-logic

- Java17 需要升级/验证 JDT candidate 及对应 Worker JDK/Lombok/APT 组合，
  不是安装一个新 JDK 就结束。
- Mockito 配置了 Error Prone，当前拒绝发生在 compiler args/argument provider
  映射处。不能直接丢弃编译扩展；需要进一步明确实际导出信息与 JDT 支持方式。
- Checkstyle 的编译扩展及 ANTLR 生成源码应分别验证；本轮尚未到生成源码编译阶段，
  不把预期的生成器问题写成已观察到的错误。
- TestNG 的 buildSrc/build-logic 支持涉及构建模型获取，不在本轮放开目录检查。

## 回归入口

- `tests/e2e/test_maven_surefire_compatibility.py`：原生 Maven 与 MCP 同一选择器对照；
  显式选类覆盖发现过滤、未绑定 execution 不生效、额外资源 classpath、修改/恢复、缓存重开。
- `tests/e2e/test_maven_modules.py`：额外 Surefire 运行路径在 Reactor 转换后仍可见。
- `tests/e2e/test_gradle_modules.py`：Groovy/Kotlin 均声明额外 retryTest，默认流程正常，
  retryTest 的失败哨兵不执行。
- `tests/e2e/test_stdio_mcp_java.py`：Make disabled 下仍有冷编译、增量启动、reload、
  restart 和跨 MCP 恢复；不复活旧 class 输出复用策略。
- 既有完整 Fast Test 和普通测试继续回归，不能用以上局部通过代替全部项目验收。

本机本轮结果：普通测试 `801 passed, 47 skipped`；对应开关开启的真实 MCP/启动/
Maven、Gradle 模块及新 Surefire 对照 `15 passed`；完整 Fast Test 回归 `6 passed`。
skip 不作为通过。wheel/sdist、compileall、git diff --check 通过。
原报告未修改；独立 clone 中的临时业务改动、启动配置及仓库/Build Scan 配置均已恢复。
