# Gradle G1：Task-native Build World Spike

## 定位

G1只回答一个问题：

> joLink能否不解析`build.gradle(.kts)`，通过项目自己的Wrapper和临时init
> plugin，让Gradle导出main/test权威Task事实，并保持取消、隐私和源码一致性？

G1不接MCP、不创建JDT session、不运行测试，也不修改用户build/settings/source。

## 实现

```text
Python ProcessSupervisor
→ project/gradlew --offline
→ private init.gradle
→ content-checked Java 8 Probe JAR
→ classes + testClasses
→ jolinkExportBuildWorld
→ 0600 private JSON
```

Probe只使用公共API：

```text
SourceSet main/test
JavaCompile compileJava/compileTestJava
Test test
JavaCompiler Toolchain metadata
```

私有模型包含：source/resource roots、compile/runtime classpath、outputs、Java
level、build JDK、Processor path、compiler args，以及Test task的classpath、
working directory、JVM args、system properties和environment overrides。

敏感值只写入0600私有JSON；公开报告只保留名称、数量和SHA identity，Gradle日志
不得回显值。Test environment基线在Task刚创建、用户配置尚未执行时通过公共API
捕获，避免把Gradle Daemon与Client之间的环境差异误判成用户override。

## 已有证据（2026-08-30）

同一个Java 8字节码Probe（class major 52）完成：

```text
Gradle 8.10 + Groovy DSL + Java 11       PASS
Gradle 8.10 + Kotlin DSL + Java 11       PASS
Gradle 8.14 + Groovy DSL + Java 11       PASS
Gradle 8.14 + Kotlin DSL + Java 11       PASS

main/test SourceSet与JavaCompile事实       PASS
JUnit Platform Test task识别              PASS
pre/post human source manifest一致         PASS
0600 private model                        PASS
system property/environment值不进入日志    PASS
Probe二次构建SHA完全一致                   PASS
```

取消实验：

```text
启动12秒阻塞的Export Task
→ ProcessSupervisor取消Wrapper/Client
→ client/process tree有界退出
→ 等待超过12秒仍无late JSON publish
→ 原Gradle Daemon PID继续存活
→ 同一Daemon后续Export成功
```

结论：当前CLI + init plugin路径可以做到“取消当前Build但不杀共享Daemon”，G1
暂时不需要引入Tooling API Client。

## 已发现边界

1. 本机已有“解压后的Gradle发行版”不等于Wrapper离线可用；Wrapper仍按
   `distributionUrl`的identity查找ZIP并可能尝试联网。产品必须预检Wrapper
   distribution，结构化区分“发行版未缓存”和“依赖未缓存”。G1使用私有
   `file://` ZIP忠实验证Wrapper路径。
2. Probe当前用Gradle 8.10公共API编译，只对8.10/8.14获得证据。最低Gradle版本
   尚未冻结；不能据API出现时间直接宣称支持4.6。
3. 仅支持真正单Project、Java plugin、标准main/test和默认`test` Task；尚未验证
   多Project、custom SourceSet/Test Task、composite build、Kotlin/Groovy业务源码、
   Android、KAPT/KSP或source-generating Processor。
4. 当前JSON是实验私有模型，不是最终`JavaTestBuildWorld`产品contract。

## 下一步G2

```text
Gradle private model
→ 转换为现有PersistentJdtCompileSession输入
→ JDT FULL
→ Maven/Gradle产物Tier 1
→ main/test Incremental
→ 现有JUnit4/5/TestNG Runner
```

只有G2闭环通过后，才抽取：

```text
TestBuildWorldBootstrap
├── Maven
└── Gradle
```
