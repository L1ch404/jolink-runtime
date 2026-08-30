# Gradle G1/G1.1：Task-native Build World Spike

## 定位

G1/G1.1回答：

> joLink能否不解析`build.gradle(.kts)`，通过项目自己的Wrapper和临时init
> plugin，让Gradle导出main/test权威Task事实，并保持顺序、JDK、身份、取消、
> 隐私和源码一致性？

本阶段不接MCP、不创建JDT session、不运行测试，也不修改用户
build/settings/source。

## 实现

```text
Python ProcessSupervisor
→ project/gradlew --offline --no-configuration-cache
→ private init.gradle
→ content-checked Java 8 Probe JAR
→ classes + testClasses
→ content-addressed jolinkExportBuildWorld_<sha>
→ 0600 private JSON
```

Probe只使用公共API：

```text
SourceSet main/test
JavaCompile compileJava/compileTestJava
Test test
JavaCompiler / JavaLauncher Toolchain metadata
JUnitOptions / JUnitPlatformOptions / TestNGOptions
```

所有classpath、source/resource roots和outputs保持Gradle FileCollection迭代顺序。
模型绑定`request_id + probe_sha256 + target_project + task_name`，额外Project、
SourceSet、Test Task和未知Test framework结构化fail-closed。

私有模型包含：source/resource roots、compile/runtime classpath、outputs、Java
level、Compiler/Test Toolchain、Processor path、compiler args，以及Test task的
classpath、working directory、JVM args、heap、bootstrap classpath、system
properties和environment overrides。非空`jvmArgumentProviders`标记为unmodeled，
由G2 fail-closed。

敏感值只写入0600私有JSON；公开报告只保留名称和数量，不输出敏感值裸SHA。
Test environment基线在Task刚创建、用户配置尚未执行时捕获，避免把Gradle
Daemon与Client的环境差异误判成用户override。

## 已有证据（2026-08-30）

同一个由真实JDK8构建的Probe（class major 52）完成：

```text
Gradle 8.10 + Groovy DSL + Daemon JDK17 + Toolchain JDK11  PASS
Gradle 8.10 + Kotlin DSL + Daemon JDK17 + Toolchain JDK11  PASS
Gradle 8.14 + Groovy DSL + Daemon JDK17 + Toolchain JDK11  PASS
Gradle 8.14 + Kotlin DSL + Daemon JDK17 + Toolchain JDK11  PASS
Gradle 8.10 + Groovy DSL + Daemon JDK8  + Toolchain JDK11  PASS

main/test SourceSet与JavaCompile事实                     PASS
Test JavaLauncher JDK11                                 PASS
本地真实JUnit 5 runtime（6 JAR）                         PASS
真实Java 8 Annotation Processor在main/test执行           PASS
两个同名resource依赖A→B保序，实际Java加载A               PASS
pre/post human source manifest一致                       PASS
0600 private model                                      PASS
system property/environment值不进入日志                  PASS
configuration-cache项目级启用被命令关闭                  PASS
Probe二次JDK8构建SHA完全一致                             PASS
```

边界门禁：

```text
多Project              → GRADLE_MULTI_PROJECT_UNSUPPORTED
额外SourceSet           → GRADLE_SOURCE_SET_UNSUPPORTED
额外Test Task           → GRADLE_TEST_TASK_UNSUPPORTED
内容地址Task冲突         → GRADLE_PROBE_TASK_CONFLICT
未知Test options         → GRADLE_TEST_FRAMEWORK_UNSUPPORTED（代码门禁）
```

取消实验：

```text
私有GRADLE_USER_HOME warm build并取得唯一Daemon PID
→ 在compileJava依赖链启动12秒可中断Gate
→ ProcessSupervisor取消Wrapper/Client
→ OperationResult.cancelled=true且timed_out=false
→ 等待超过12秒仍无late class/JSON publish
→ 精确同一Daemon PID继续存活
→ recovery复用同一PID并生成class/JSON
```

结论：CLI + init plugin可以精确取消当前Build而不杀共享Daemon，G2不需要引入
Tooling API Client。

## 已发现边界

1. “已有解压后的Gradle发行版”不等于Wrapper离线可用；Wrapper仍按
   `distributionUrl` identity查找ZIP并可能联网。产品必须结构化区分发行版未缓存
   与依赖未缓存。G1使用私有`file://` ZIP忠实验证Wrapper路径。
2. Probe使用Gradle 8.10公共API、Gradle 8.10 + JDK8构建，只对8.10/8.14获得
   证据。最低Gradle版本尚未冻结，不能据API出现时间直接宣称支持4.6。
3. 仅支持真正单Project、Java plugin、标准main/test和默认`test` Task；
   多Project、custom SourceSet/Test Task、composite build、Kotlin/Groovy业务源码、
   Android、KAPT/KSP和source-generating Processor仍不支持。
4. 当前JSON满足G2权威输入的顺序、JDK、身份、隐私和边界要求，但仍是实验私有
   模型；G2通过后才冻结最终`JavaTestBuildWorld`产品contract。

## 下一步G2

```text
Gradle private model
→ 转换为现有PersistentJdtCompileSession输入
→ JDT FULL
→ Gradle/JDT产物Tier 1
→ main/test Incremental
→ 现有JUnit4/5/TestNG Runner
```

只有G2闭环通过后，才抽取：

```text
TestBuildWorldBootstrap
├── Maven
└── Gradle
```
