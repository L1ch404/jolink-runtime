# joLink Fast Test

Fast Test不是`mvn test`或`gradle test`的包装。构建系统只负责通过Probe导出测试
Build World；main/test编译由持久JDT workspace完成，测试由独立Runner JVM执行。

## 流程

```text
读取本地Test Build World缓存
├─ POM/父POM链或Gradle构建文件未变化 → 直接使用
└─ 没有缓存或配置变化 → 只运行Probe并保存结果
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

短测试直接返回结果；慢测试返回`running + test_run_id`，使用`java_status`观察，
或用`cancel_test`取消。断言失败仍是`ok=true, passed=false`；编译、Runner基础设施、
超时等失败返回`ok=false`。

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

- source/target支持Java 8和11；
- Runner支持显式Class或Class#method选择；
- Maven Reactor已将目标和上游模块接入持久JDT工程，支持上游main源码变化，
  也支持显式依赖上游test-jar的测试；完整流程见[多模块JDT](jdt-modules.zh-CN.md)；
- Gradle多Project使用相同的JDT模块工程；读取解析后的api/implementation/runtimeOnly
  依赖，只构建所需模块。构建配置改变时重新Probe，普通源码修改只走增量；
- protobuf/OpenAPI等必须先运行代码生成任务的项目尚未自动执行生成器；
- Runner JVM尚未保活，Spring测试的大部分后续耗时通常在Runner启动和框架初始化。
