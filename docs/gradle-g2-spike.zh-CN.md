# Gradle G2/G2.1：JDT Incremental + Authority冻结

## 目标

G2验证G1.1私有Task-native模型能否直接驱动现有编译与测试核心；G2.1把
标准Fixture中隐含成立的条件冻结为显式authority门禁：

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
- 缺失entry只允许已知为空的formal resource output；缺失依赖JAR或未知目录返回
  `GRADLE_CLASSPATH_ENTRY_UNAVAILABLE`。
- 去除formal outputs后，`compileJava.classpath`必须是
  `compileTestJava.classpath`的有序前缀；否则返回
  `GRADLE_TEST_CLASSPATH_ORDER_UNMODELED`，不允许重新排序。
- Gradle formal main/test outputs只用于Tier 1 oracle；Runner classpath在原位置将其
  替换为JDT `bin/test-bin`。
- Target系统库严格来自`compileJava/compileTestJava`共同的Compiler Toolchain；
  source/target/encoding/release无法由同一JDT Project表达时fail-closed。
- Test Runner严格使用`Test.javaLauncher`的executable、working directory和有序
  runtime classpath，不使用Daemon或Compile JDK猜测。
- main/test各自必须只有一个classes output，且formal outputs必须真实出现在
  Test.classpath中原位置；不再自动插入JDT bin/test-bin。
- main/test Processor path必须完全相同；Lombok当前明确返回
  `GRADLE_LOMBOK_UNMODELED`。compiler args、Test environment/system
  properties/JVM args/bootstrap classpath、argument providers和非默认fork语义当前
  fail-closed。
- v0.1要求标准`src/main|test/java|resources`且无include/exclude pattern；Python
  pre、Gradle post、private snapshot三方manifest必须完全一致。
- JDT FULL后、formal resource overlay前捕获native resource manifest；每个JDT
  Processor资源必须与Gradle formal output同路径同hash。

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
JDT native Processor resource vs Gradle     exact
overlay后完整class+resource tree
== 独立JDT clean-full完整tree               exact
pre/post/snapshot input manifest             exact
authority故障注入                           11/11 rejected
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

G2.1通过，证明：

> Gradle不需要自己的JDT或测试实现。G1.1 Task-native模型可以无损复用现有
> PersistentJdtCompileSession和FastTestRunner，并保持Gradle的classpath顺序、
> Compile/Test Toolchain、Processor和测试运行语义。

故障注入覆盖：classpath重排、错误Toolchain、release、自定义source、Test
assertions/tags、多output、缺失依赖、runtime缺formal output、Lombok和Processor
资源差异。

下一阶段允许进入产品抽象：

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
