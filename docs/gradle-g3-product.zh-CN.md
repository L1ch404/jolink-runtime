# Gradle G3：Fast Test产品接入

## 产品结构

G3将Maven和Gradle统一到同一个不可变authority模型：

```text
Maven Probe ─┐
             ├→ JavaTestBuildWorld
Gradle Probe ┘
                    ↓
          PersistentJdtCompileSession
                    ↓
              FastTestRunner
```

`FastTestManager`仍负责Attempt、status、cancel、JDT/Runner生命周期和结果；Build
System只负责Bootstrap与`JavaTestBuildWorld`验证。产品层现在只有一套Gradle
authority转换器，实验Runner也复用它；Maven/Gradle通过实际Bootstrap provider
分派，不再在Manager中写死两条检测分支。没有新增MCP Tool或Action。

## Gradle调用

```json
{
  "action": "test",
  "project_path": "/workspace/gradle-project",
  "source_files": ["src/main/java/example/Service.java"],
  "tests": ["example.ServiceTest#works"],
  "timeout": 60
}
```

项目必须提交：

```text
gradlew / gradlew.bat
gradle/wrapper/gradle-wrapper.properties
build.gradle 或 build.gradle.kts
```

第一轮调用执行Gradle `classes/testClasses`与内容校验Probe；后续调用只同步显式
Java源码并使用持久JDT增量编译。

## v0.1边界

```text
Gradle                    8.10 / 8.14
Project                   单Project
Plugin                    java / java-library产生的标准Java模型
SourceSet                 仅main/test标准目录，无include/exclude
Java                      source=target 8或11，release为空
Processor                 main/test path完全相同；Lombok暂拒绝
Resources                 main/test resource roots必须为空
Test Task                 仅默认test，默认fork/assert/filter/JVM配置
Framework                 当前仅JUnit Platform（强制选择JUnit5 Runner）
```

Gradle Daemon JDK、Compiler Toolchain、Test JavaLauncher和JDT Worker JDK互相独立；
Target系统库来自Compiler Toolchain，Runner使用Test JavaLauncher。

Gradle Probe导出的`sourceFiles`、`Test.testClassesDirectories`、Test工作目录和
Compiler编码均作为authority验证；缺失编码、JPMS、生成Java源码、未知Test
framework会fail closed。Runner继承本次Gradle Build World使用的Java环境。

freshness同时覆盖build/settings脚本、`gradle.properties`、version catalog、
Wrapper properties/JAR、用户Gradle配置及init script、相关环境变量，以及
compile/test/runtime三段依赖。配置文件即使Bootstrap时不存在也会进入指纹，创建
后会使旧Build World失效。`GRADLE_ARGS=-o/--offline`会真实传给Wrapper，而不是只
作为joLink侧标记。

## 产品证据（2026-08-30）

```text
统一JavaTestBuildWorld Maven定向回归       PASS
Maven Java11真实Fast Test                 PASS
Gradle 8.14产品FastTest baseline           PASS
Gradle 8.10产品FastTest baseline           PASS
Gradle 8.14 strict offline产品FastTest       PASS
Gradle main源码错误/恢复                   PASS
Gradle test源码错误/恢复                   PASS
Maven+Gradle同文件6个真实Fast Test E2E      PASS
普通测试                                   588 passed, 14 skipped
原Runtime MCP/JVM E2E                     10 passed
uv build --no-sources                     PASS
隔离wheel Gradle资产及完整Fast Test         PASS
Worker/Runner残留进程                      0
```

## 当前不做

- Gradle Runtime launch/reload；G3只接Fast Test；
- Gradle多Project、custom SourceSet/Test Suite、composite build；
- Gradle资源overlay、Lombok、source-generating Processor；
- 自定义Test system properties/environment/JVM args/heap/filter/tags/engines；
- Gradle JUnit4/TestNG产品执行；G2实验覆盖不等于G3产品承诺；
- Gradle 8.10/8.14以外版本；
- configuration cache；产品命令显式关闭。
