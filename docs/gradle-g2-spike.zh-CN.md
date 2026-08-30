# Gradle G2：JDT Incremental + Fast Test闭环

## 目标

G2验证G1.1私有Task-native模型能否直接驱动现有编译与测试核心：

```text
Gradle Wrapper
→ classes + testClasses + private Build World
→ PersistentJdtCompileSession
→ FULL + Tier 1
→ main/test Incremental
→ 现有FastTestRunner
→ JDT clean-full oracle
```

本阶段仍不接MCP、不修改`FastTestManager`，只验证转换与运行语义。

## 转换规则

- Gradle FileCollection顺序原样进入JDT/Runner。
- 不存在的空classpath目录（例如无resources时的`build/resources/main`）保留在
  Gradle权威模型中，但转换为JDT library entry时过滤；其余entry顺序不变。
- `compileJava` classpath成为main依赖；`compileTestJava`中扣除main outputs和已存在
  main依赖后的entry成为test-only依赖。
- Gradle formal main/test outputs只用于Tier 1 oracle；Runner classpath在原位置将其
  替换为JDT `bin/test-bin`。
- Test Runner严格使用`Test.javaLauncher`的executable、working directory和有序
  runtime classpath，不使用Daemon或Compile JDK猜测。
- main/test Processor path必须完全相同；compiler args、Test environment/system
  properties/JVM args/bootstrap classpath、argument providers和非默认fork语义当前
  fail-closed。

## 真实Fixture

```text
Java source/target             11
Gradle Daemon                  JDK8或JDK17
compileJava/compileTestJava    Toolchain JDK11
Test JavaLauncher              JDK11
JUnit Platform                 本地7个真实JAR（含Launcher）
annotationProcessor            真实JDK8 Processor，main/test均执行
dependencies                   两个有顺序冲突resource的本地JAR
DSL                            Groovy / Kotlin
```

## 证据（2026-08-30）

```text
Gradle 8.10 + Groovy DSL + Daemon JDK8   PASS
Gradle 8.10 + Kotlin DSL + Daemon JDK17 PASS
Gradle 8.14 + Groovy DSL + Daemon JDK17 PASS
Gradle 8.14 + Kotlin DSL + Daemon JDK17 PASS
```

每个case均通过：

```text
Gradle main output vs JDT FULL Tier 1      compatible
Gradle test output vs JDT FULL Tier 1      compatible
真实JUnit baseline                         passed
main方法体错误                              test failed
main恢复                                   test passed
test断言错误                               test failed
test恢复                                   test passed
最终JDT incremental class SHA tree
== 独立JDT clean-full class SHA tree       exact
Worker/Runner process cleanup              settled
```

四组实测JDT增量耗时约：

```text
main edit/recovery    14–24ms
test edit/recovery    13–18ms
```

整个case包含Gradle Wrapper Bootstrap、两次JDT FULL和五次独立JUnit Runner，约
18–32秒；这不是持续增量路径的单次成本。

## 结论

G2通过，证明：

> Gradle不需要自己的JDT或测试实现。G1.1 Task-native模型可以复用现有
> PersistentJdtCompileSession和FastTestRunner，并保持Gradle的classpath顺序、
> Compile/Test Toolchain、Processor和测试运行语义。

下一阶段可以进入产品抽象：

```text
TestBuildWorldBootstrap
├── MavenTestBuildWorldBootstrap
└── GradleTestBuildWorldBootstrap

FastTestManager
→ 只消费JavaTestBuildWorld
→ 继续管理JDT、Runner、Attempt、status和cancel
```

## 仍未支持

- Gradle多Project、composite build；
- custom SourceSet、额外Test Task/JVM Test Suite；
- Kotlin/Groovy业务源码、Android、KAPT/KSP；
- source-generating Processor；
- 自定义Test JVM/system/environment/fork语义；
- Gradle 8.10/8.14之外版本；
- MCP产品接入和Runtime launch/reload。
