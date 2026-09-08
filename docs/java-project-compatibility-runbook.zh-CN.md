# joLink 开源 Java 项目兼容性测试手册：第一批

状态：待执行的测试计划，不是通过报告。只测试、定位、记录，不修复产品。
版本清单于 2026-09-08 核对。

## 1. 交给执行模型的任务

通过真实 GitHub 项目和真实 MCP，确认 joLink 的编译、启动/重启、reload、Fast Test、
缓存与跨 MCP 恢复有哪些尚未覆盖的边界。joLink 面向普通 Java 开发者，不只面向已有
Demo 或某个公司项目；常见项目被拒绝同样是产品缺口，不能把“安全拒绝”算作支持。

本轮不解 benchmark 题、不修改 joLink、不自行升级依赖或降低项目 Java 级别来追求全绿。
允许在一次性项目副本中做有明确预期的源码修改、增加测试用例和最小启动配置，以验证
增量和实际执行结果；结束后恢复自己的修改。可以写临时测试驱动和最小复现，但不要
放进 joLink 产品代码，也不要提交、推送或发布任何改动。

只使用以下产品入口作为主验收链路：

- `java_application`：`launch`、`reload`、`restart`、`stop`、`test`、`cancel_test`。
- `java_status`：`status`、`logs`、`processes`。

暂不专项测试 `java_debugger`、断点、wait_event、变量、异常监听。reload 涉及的代码
更新用实际 HTTP/CLI 行为验证，不为本轮额外引入断点。库项目没有应用入口时，启动项
记为“不适用”，重点跑编译与 Fast Test；不要给每个库强造一个 Web 服务。

范围是 9 个仓库、10 个版本样本。先串行执行；一个项目失败后记录问题，继续其他独立
项目，不要因为一个难点把整批测试停住。正式写法以本手册为准，不沿用历史 Phase 2A
实验的门禁、Tier 1 或 class SHA 等价要求。

## 2. 固定版本与执行顺序

joLink 初始代码基线：`feature/jdt-productization`，提交
`e5b139e5371bbfd7e6738f06cfc99fa44ccb697d`，包含“编译后立即保存 JDT 状态”修复。
不要使用同名但较旧的 PyPI `0.1.0a3` 来代替该产品分支。若用户指定更新提交，以指定
提交为准，并在报告中记录；执行过程中不自动拉取更新。

下表 SHA 是 commit，不是 annotated tag 对象。日期是提交日期，不作为发行日期。
P03B、P05 已固定历史快照，执行时不要改用当时最新的 main。

| ID | GitHub 仓库／版本参考 | 固定 commit | 提交日期 |
|---|---|---|---|
| P01 | [apache/commons-lang，3.12.0](https://github.com/apache/commons-lang/tree/105a350b154eac7c3f3a6bac94ec2cfb0fbe232b) | `105a350b154eac7c3f3a6bac94ec2cfb0fbe232b` | 2021-02-26 |
| P02 | [json-path/JsonPath，2.9.0](https://github.com/json-path/JsonPath/tree/af7e516c69df680a6584fca7180ef082eb67c96c) | `af7e516c69df680a6584fca7180ef082eb67c96c` | 2024-01-20 |
| P03A | [spring-projects/spring-petclinic，1.5.x](https://github.com/spring-projects/spring-petclinic/tree/c36452a2c34443ae26b4ecbba4f149906af14717) | `c36452a2c34443ae26b4ecbba4f149906af14717` | 2017-11-03 |
| P03B | [spring-projects/spring-petclinic，新版快照](https://github.com/spring-projects/spring-petclinic/tree/818c4136ea971c21674525f9053de0d9c7ad8cfe) | `818c4136ea971c21674525f9053de0d9c7ad8cfe` | 2026-08-26 |
| P04 | [mybatis/mybatis-3，3.5.19](https://github.com/mybatis/mybatis-3/tree/ee0d4f4831ffdd311b0183c202f4ad6492a3f404) | `ee0d4f4831ffdd311b0183c202f4ad6492a3f404` | 2025-01-02 |
| P05 | [mapstruct/mapstruct-examples，MapStruct 1.6.3 快照](https://github.com/mapstruct/mapstruct-examples/tree/df4dbeaae5eea82edcfad28ce31d137116af0907) | `df4dbeaae5eea82edcfad28ce31d137116af0907` | 2025-05-11 |
| P06 | [google/guava，31.1](https://github.com/google/guava/tree/0a17f4a429323589396c38d8ce75ca058faa6c64) | `0a17f4a429323589396c38d8ce75ca058faa6c64` | 2022-02-28 |
| P07 | [mockito/mockito，5.14.2](https://github.com/mockito/mockito/tree/78348597236d54a84843f454936b8440b006f773) | `78348597236d54a84843f454936b8440b006f773` | 2024-10-15 |
| P08 | [testng-team/testng，7.10.2](https://github.com/testng-team/testng/tree/9665805ac267d8016fdcededd855d4b5fa1e588d) | `9665805ac267d8016fdcededd855d4b5fa1e588d` | 2024-04-28 |
| P09 | [checkstyle/checkstyle，10.12.7](https://github.com/checkstyle/checkstyle/tree/b54819d1f783a10ca8df13c5987ec0ac28052b3a) | `b54819d1f783a10ca8df13c5987ec0ac28052b3a` | 2023-12-31 |

按表顺序先完成每个项目的主组合，再补 JDK 对照；P03A/P03B 分开报告。

### 每个项目从哪里开始

下面是已检查到的源码入口，不是已运行通过的测试集合。执行前阅读对应测试，确认
真实包名、测试框架、外部服务要求和原生构建选择方式。先跑少量代表类，再扩大到
一个目标模块的普通单元测试；按模块、按类分批，不把“两个类通过”写成“整个仓库通过”。

| ID | 构建与 project_path | 首组选取的测试类 | 重点 |
|---|---|---|---|
| P01 | Maven，仓库根 | `org.apache.commons.lang3.StringUtilsTest`、`org.apache.commons.lang3.ArrayUtilsTest`、`org.apache.commons.lang3.math.NumberUtilsTest` | Java 8 库、JUnit 5、方法选择、纯函数修改与恢复 |
| P02 | Gradle，仓库根，先选 `json-path-assert` | `com.jayway.jsonassert.JsonAssertTest`、`com.jayway.jsonpath.matchers.HasNoJsonPathTest` | JUnit 4/Vintage/Platform、多模块、对上游 json-path 的依赖；已有历史通过结果只作对照，不代替本轮 |
| P03A/B | Maven，仓库根，显式 `build_system=maven` | `org.springframework.samples.petclinic.service.ClinicServiceTests`、`org.springframework.samples.petclinic.owner.OwnerControllerTests` | Spring 上下文、数据库/资源、真实应用启动；优先项目自带本地数据库方案 |
| P04 | Maven，仓库根 | `org.apache.ibatis.reflection.MetaObjectTest`、`org.apache.ibatis.session.SqlSessionTest` | 反射、XML 资源、内嵌数据库；与需外部数据库或容器的测试分组 |
| P05 | Maven，`<仓库>/mapstruct-lombok`，显式 `build_system=maven` | `com.mycompany.mapper.SourceTargetMapperTest` | MapStruct 1.6.3、Lombok 1.18.30、binding 0.2.0、生成源码和增量；这是专项样例，不作为大型项目证据 |
| P06 | Maven，仓库根；目标 `guava-tests`，包含所需上游 | `com.google.common.collect.ListsTest`、`com.google.common.collect.ImmutableListTest` | Reactor、guava-testlib、非标准 `test/` 源目录、传统测试套件；首轮不含 Android/GWT 构建 |
| P07 | Gradle，仓库根；目标 `mockito-core` | `org.mockito.MockitoTest`、`org.mockitousage.annotation.CaptorAnnotationBasicTest` | Mockito inline/字节码增强、Agent 参数、测试 JVM 隔离；Wrapper 为 8.5 |
| P08 | Gradle，仓库根；先 `testng-asserts`，再单独探 `testng-core` | `org.testng.AssertTest`、`test.assertion.SoftAssertTest`；core 可从 `test.annotationtransformer.SimpleTest` 调查 | TestNG、分组/监听器、构建逻辑和跨模块；Wrapper 为 8.7；core 的样例类可能是嵌套测试数据，不能直接把样例算作顶层套件 |
| P09 | Maven，仓库根 | `com.puppycrawl.tools.checkstyle.DefaultConfigurationTest`、`com.puppycrawl.tools.checkstyle.CheckerTest` | ANTLR 生成源码、Java 11、测试资源和复杂编译选项；`.java` 测试输入不一定是编译源文件 |

Petclinic 两版主类均为 `org.springframework.samples.petclinic.PetClinicApplication`。
新版含 Maven/Gradle 两套构建，本轮先测 Maven，不顺便加第二套全矩阵。

项目定义以固定 SHA 的 POM、Gradle 脚本、CI、README 为准。特别是 main 的目标 Java
版本与 test 依赖的最低 Java 版本可能不同：例如 MyBatis 的 main 声明 Java 8，但
其 Mockito 5 测试依赖要求更高的运行时，不能据 main 配置就把整个测试过程放到 JDK 8。

## 3. JDK：主组合先跑，再做对照

第一批覆盖 JDK 8、11、17、21。下面是执行起点，不是已认证兼容矩阵；若固定项目的
原生构建需要额外 toolchain，正常配置并记录，不删除该要求。暂不做所有项目×所有
JDK×所有操作的笛卡尔积，也不把 JDK 25 加进首轮。

| ID | 首先尝试的构建/Test Runner JDK | 对照组合 |
|---|---:|---:|
| P01 | 8 | 17 |
| P02 | 11 | 17 |
| P03A | 8 | 11，观察旧 Spring 生态迁移边界 |
| P03B | 17 | 21 |
| P04 | 17 | 21 |
| P05 | 8 | 17 |
| P06 | 8 | 17 |
| P07 | 17 | 21 |
| P08 | 17 | 21 |
| P09 | 17，main 目标仍保留 11 | 21 |

每组分别记录，而不是只填一个“JDK 版本”：

1. Maven/Gradle 实际 JVM、Wrapper 版本及额外编译 toolchain。
2. main/test 的 source、target、release。
3. joLink Worker 的实际 java 路径、版本、PID。
4. 应用或 Test Runner 的实际 java 路径、版本及必要 JVM 参数。

需要的目标平台 JDK 也应安装并记录。例如用 JDK 17 Worker 编译 Java 8 项目时，
不要把目标 JDK 8 系统库缺失混同于业务源码不兼容；保留所需 JDK 安装，不只修改
一个 JAVA_HOME 就假定所有角色和目标平台都已准备好。

`JAVA_HOME=21` 不证明 Worker 或 Runner 就是 21；IDEA 配置、工具链和旧缓存可能
选择别的 JDK。以实际进程/构建日志为证据，无法取得就写“未观察”。没有可操作入口
指定所需组合，也要记录为工具链控制缺口，不能暗中改缓存 JSON 实现。

切换 JDK 组合用独立缓存目录和新 MCP；测试跨 MCP 持久化时则必须复用同一组缓存。
项目本身不能在某 JDK 原生构建/测试，与 joLink 不能执行这个组合是两种情况，分开记。
当前 joLink 已知主要支持 Java 8/11 目标，这不是跳过 P03B 的理由：真实尝试后记录
阻塞阶段，不降低它的 Java 级别。无法到达的后续阶段写 blocked，不臆测更多失败。

## 4. 准备：两个工作目录，避免预编译掩盖问题

选择一个专用的绝对路径 `RUN_ROOT`，位于 joLink 仓库和用户日常项目之外。按项目、
版本、JDK 组合建目录，建议：

```text
RUN_ROOT/P01/jdk8/
  product/          joLink 使用的原始源码检出
  baseline/         相同 commit，原生 Maven/Gradle 对照
  cache/            这组的 joLink workspace/Build World
  evidence/         MCP JSON、日志、环境、临时修改 patch
  report.md
RUN_ROOT/index.md
RUN_ROOT/issues.md
```

在专用目录中 clone 并 checkout 表中完整 SHA；记录 `git rev-parse HEAD` 和
`git status --short`。可使用独立 worktree 建 baseline，但不能复制其 target/build
或生成源码到 product。共享正常依赖下载缓存可以；“冷”在这里指没有 joLink Build
World/JDT workspace，没有提前生成的项目 class，不要求重复下载全部依赖。

原生基线只在 baseline 目录执行。先按项目 README/CI 选择正确命令，例如：

```text
Maven 单模块：mvn -B -Dtest=<已确认的类名> test
Maven Reactor：mvn -B -pl guava-tests -am -Dtest=ListsTest -Dsurefire.failIfNoSpecifiedTests=false test
Gradle：./gradlew :json-path-assert:test --tests com.jayway.jsonassert.JsonAssertTest
Windows Wrapper：使用 mvnw.cmd / gradlew.bat 的对应命令
```

这些是命令结构，不是所有项目可原样套用的万能命令。优先原仓库 Wrapper，不自行
升级；Maven 没有 Wrapper 时记录所选版本。Reactor 的“不存在指定测试不失败”参数
只用于上游模块，目标模块仍必须实际执行测试且数量非零。记录各目标模块原生结果，
不要只看 shell exit code，也不要用 skipTests 把测试基线跑成空操作。

环境问题允许修正正常配置，例如 Maven 路径、镜像、代理、离线设置、本地数据库。
不得通过关闭 TLS 校验、删除 Processor/生成器、改测试框架或改编译级别来把 joLink
包装成通过。若使用辅助步骤才能运行，单独写“辅助后通过”，不能覆盖原始失败。
依赖预热/原生安装产生的本地上游 JAR 也要注明；跨模块修改仍应看到当前源码效果。

测试生成源码冷启动时尤其注意：baseline 中生成了文件，不代表 product 中已经有。
如果 joLink 必须先手动运行生成任务，记录这个产品前置依赖；可在另一个辅助场景执行
仓库原生生成任务继续定位，不把它算作原始 cold-start 通过。

## 5. 必须连到实际被测 MCP

从固定 joLink 源码检出执行 `uv sync`，连接该检出的服务，而不是旧 PyPI 包。通用
MCP 客户端配置示例（所有占位路径都替换成当前机器的实际绝对路径）：

```json
{
  "mcpServers": {
    "jolink-runtime": {
      "command": "uv",
      "args": ["--directory", "/absolute/jolink-runtime", "run", "jolink-runtime"],
      "env": {
        "JAVA_HOME": "/absolute/jdk8",
        "PATH": "/absolute/jdk8/bin:/actual/original/PATH",
        "XDG_CACHE_HOME": "/absolute/RUN_ROOT/P01/jdk8/cache"
      }
    }
  }
}
```

Windows 使用实际 Windows 路径和分号 PATH；缓存隔离设该 MCP 进程的 `LOCALAPPDATA`
而不是 XDG_CACHE_HOME。只设置当前服务进程，不改系统全局环境。Worker 下载缓存仍
可能共用，不要为本轮删除全局 `~/.cache/jolink-runtime` 或用户 `.m2/.gradle`。

若用 Python MCP SDK 写临时驱动，必须显式传 `StdioServerParameters(..., env=env)`，
例如 `env={**os.environ, "JAVA_HOME": ..., "XDG_CACHE_HOME": ...}`。SDK 默认只继承
部分环境变量；现有 `scripts/jolink_mcp_dev_client.py` 没有显式转发完整 env，不能只
在外层 shell 设置 JAVA_HOME/XDG_CACHE_HOME，就假定它完成了 JDK/缓存隔离。
可参考该脚本的真实 stdio 会话写法，在 RUN_ROOT 写驱动，不修改 joLink 脚本。

初始化后检查 tools/list，确认有 `java_application`、`java_status`。记录 joLink
commit、dirty 状态、Python 路径、启动命令、stderr 路径；检查 `java_status(status)`
里的 `server_diagnostics.log_file` 是否位于预期缓存。若服务旧/路径错误，先修正连接，
不得把内存里旧版本的结果归给当前提交。

以下 JSON 都是工具的 arguments；使用宿主 UI 调工具，或 SDK 的
`session.call_tool("java_application", arguments)`。通过直接实例化 Python Manager
绕过 MCP 的结果只能作辅助定位，不能替代产品流程验收。

### Fast Test 调用与轮询

```json
{
  "action": "test",
  "project_path": "/absolute/RUN_ROOT/P01/jdk8/product",
  "build_system": "maven",
  "tests": ["org.apache.commons.lang3.StringUtilsTest"],
  "timeout": 120
}
```

用 `java_status` 调用 `{"action":"status"}`。Fast Test 状态在返回的 `fast_test`
对象内；按 test_run_id 关联本次，不拿上一轮结果充数。`starting/bootstrapping/compiling/
running` 都还未结束。每 1～2 秒观察一次；`completed` 后还必须检查 `passed`、实际
tests/failed_count/skipped_count。`ok=true, passed=false` 是断言失败，不是 MCP 崩溃。

`tests` 当前最多 64 个 Class 或 Class#method 选择器，按目标模块分批；不要混合多个
目标模块的选择器来假设工具支持整仓库一条命令。测试数为零或全部跳过，不算执行通过。
首次 baseline 省略 `source_files`；修改时至少一次也省略它，验证自动发现改动。
另一次显式提供，路径相对 project_path，包含模块前缀，单次最多 16 个文件。

不要把字段 `timeout` 写成 `timeout_seconds`。它控制测试运行，不是完整 Bootstrap
期限；状态里单独记录的 bootstrap 超时也要保留。工具短暂返回、HTTP 尚未 ready 或
等待超时，不等于服务/编译失败；观察实际终态。长构建有进展就继续等待，不因慢而停止。

## 6. 通用测试清单

先在每个主 JDK 组合执行；对照 JDK 至少重复基线、无改动、main 修改、恢复、跨 MCP。
不要每一项都新建缓存；同一组合尽量共享一次 Probe/FULL，这正是要验的产品流程。

| 编号 | 实际操作 | 要证明的事实 |
|---|---|---|
| T01 冷 Fast Test | product 未经原生编译、独立 joLink 缓存，运行首组已有测试 | Probe、JDT main/test、Runner 真能连起来；记录实际测试数，与 baseline 对照 |
| T02 热复用 | 同一 MCP、无源码/构建配置变化，原样重复 3 次 | 不重复 Probe/FULL；记录 Worker 与每次 Runner 的区别及耗时，不把新 Runner 当 Worker 重启 |
| T03 main 修改 | 选一个被这些测试调用的普通方法，改变可预测返回值/分支 | 测试出现预期差异；恢复原源码后恢复原结果，不靠追加注释证明业务更新 |
| T04 test 修改 | 临时让一个已执行的断言必然失败，再恢复 | 测试源码被重新编译并执行；不能一直跑旧 test class |
| T05 编译错误 | 在一次性源码副本中制造确定语法错误，运行并在不改源码时再运行一次，随后恢复 | 两次都应报告编译失败，不能运行旧输出后宣称通过；恢复后可继续 |
| T06 增删源码 | 添加小 helper 并让一个测试引用它；撤掉引用并删除 helper | 新文件可用，删除不留下旧类误导执行；原生基线接受相同源码变更 |
| T07 上游变化 | 多模块项目修改被下游测试使用的上游方法，再选一次公开常量或签名变化 | 实际下游行为/编译结果变化；不读取本地仓库旧 JAR，也不只编显式列出的文件 |
| T08 资源/Processor | 修改测试读取的 resource；Processor 项目修改注解/映射或 DTO 字段 | 新 Runner 读到新资源，生成代码确实更新；不直接改生成文件假装 Processor 工作了 |
| T09 跨 MCP | 按下一节执行正常重开和“任务完成后换窗口”两条路径 | 缓存恢复与有变化时的实际增量状态成立，不只检查 reusable=true |
| T10 取消/超时 | 在一个代表项目的临时测试中加入可控等待；确认 running 后取消，另测短 timeout，恢复测试后重跑 | 测试终止、后续可用；不杀无关 JVM、不让测试污染应用；首轮只需 P01 主组合完成 |

T03 的改动应与断言建立明确关系，必要时将同样改动放到 baseline 对照。T04/T05 中
预期的红色结果意味着这个控制用例通过；不能统计成 joLink 的兼容失败。反之，预期
失败却仍全绿是重点问题。保存最小源码 diff，结束恢复，不能删除测试或修改预期来掩盖故障。

T06/T07 非所有项目适用；P06 优先做上游链，P02 补 Gradle 模块。P05 优先做生成链：
`SourceTargetMapperTest` 原本把字符串 5 映射为数值 5，可临时改变 Mapper 的映射常量
产生预期断言失败，再恢复；另外测一次 Lombok 字段/访问器变化，不手写生成的 getter
或 MapperImpl。若生成链不支持，定位到输入/Processor/输出阶段后记录并继续下一项目。

P09 大量 `.java` 位于测试输入/资源目录，不应一股脑计入编译源码。源码数以实际 source
roots 为准，仓库 `rg --files '*.java'` 的数量只是文件清点，不是 JDT 编译数量。

### T09：两条都要测

**A：正常关闭后恢复。** 完成一次测试，正常结束 MCP 服务，启动新 MCP，保持同一
project_path、JDK、缓存目录。先无改动运行，再修改一个业务文件运行并恢复。

**B：任务结束，但旧 Worker 仍闲置。** 至少在 P01 和 P05 主组合完成：

```text
MCP A：首次测试完全结束
→ 修改一个业务文件，不改 POM/Gradle/JDK/joLink
→ 保留 A 闲置，新开 MCP B，使用同一个缓存
→ B 运行相同测试，验证修改生效与实际编译范围
→ B 完成一次增量后，再验证其状态已经保存，可被下一次恢复使用
```

不是让两个 BUILD 同时执行。库的 Fast Test 不占服务端口，优先用它做这个子用例。
清理时先关闭较旧的闲置会话，再关闭最新会话；旧 Worker 晚退出写回旧状态是已记录的
另一边界，不要在每个项目里额外扩成并发实验。若自然遇到，仍须保留证据和报告。

不能只看 reusable、bootstrapping 或耗时猜 FULL。保存实际构建类型/编译单元字段；
公开结果没有提供时明确写 unavailable，可通过只记录现有 Worker 响应的临时旁路
trace 定位，不改算法、不增加 SAVE、不预先调用内部初始化。计时是线索，不替代事实。
当前 Fast Test 初始化阶段的 compiled_source_count 可能不代表 FULL 实际编译数，
不要看到 0 就宣称没编译。参考[已定位的持久化问题](jdt-workspace-persistence.zh-CN.md)。

## 7. 应用启动/重启/reload：主要使用 P03A、P03B

若仓库已有正确的 IDEA Application/Spring Boot 配置，直接复用。否则可以在 product
副本增加最小 `.run/CompatPetclinic.xml`；这是显式记录的启动适配文件，不改 POM：

```xml
<component name="ProjectRunConfigurationManager">
  <configuration name="CompatPetclinic" type="Application" factoryName="Application">
    <option name="MAIN_CLASS_NAME" value="org.springframework.samples.petclinic.PetClinicApplication" />
    <option name="WORKING_DIRECTORY" value="$PROJECT_DIR$" />
    <option name="PROGRAM_PARAMETERS" value="--server.port=18080" />
    <method v="2"><option name="Make" enabled="true" /></method>
  </configuration>
</component>
```

18080/15005 只是示例；先检查空闲，冲突就改这份测试配置和调用参数，不能杀用户进程。
沿用所选版本默认的本地数据库配置；不要注入公司数据库、账号或服务端点。

调用 `java_application`：

```json
{
  "action": "launch",
  "project_path": "/absolute/RUN_ROOT/P03A/jdk8/product",
  "build_system": "maven",
  "launch_name": "CompatPetclinic",
  "jdwp_port": 15005,
  "ready_port": 18080,
  "startup_wait_timeout_seconds": 30
}
```

不要同时传 project_path 与 main_class/classpath/jar_path/app_args/vm_args。参数放到
上述启动配置里。轮询 java_status(status)，等待实际 ready/runtime_active，检查
launch_error；TCP ready 后再用普通 HTTP 客户端访问 `/`、`/owners/find` 等该版本
已有路径，记录 HTTP 内容/状态。不能仅凭 PID 存在或 ready=true 宣称业务工作正常。

完成下面几步，依次保存事实：

1. 冷 launch：product 未预先 Maven 编译；ready 后 HTTP 有效，日志可读。
2. 无改动 stop→launch：复用缓存；再测一次新 MCP 的正常关闭后 launch。
3. 同一会话 `restart`：新应用 PID，HTTP 行为不变。当前 restart 复用已编译输出，
   不要指望它读取尚未编译的源码。
4. 选择一个已被请求执行的 Controller/Service 普通方法，只改方法体，使 HTTP 输出
   有可预测差异。调用 `reload`，source_files 使用实际路径；返回 reload_started 后，
   按 reload_id 轮询 status.last_reload 直到终态，再发送新请求验证。
5. 恢复源码，再 reload 和 HTTP 验证；重启后行为应仍与当前编译结果一致。
6. 在应用运行期间执行已有 Spring/普通 Fast Test，确认测试结果和应用仍可访问。
7. 最终 stop，确认受管应用退出、业务端口释放。

```json
{
  "action": "reload",
  "source_files": ["src/main/java/实际包名/实际类名.java"]
}
```

该占位路径执行前必须替换；reload 不传 project_path，不使用 debugger 的 http_trigger。
如果结构变化返回需要 relaunch，记录该能力边界，可明确执行 stop→launch 继续验证，
但不能把它记成 reload 成功。主用例只改普通方法体，避免一开始混入结构变更。

启动和 Fast Test 使用不同 JDT workspace；测试更新了 test workspace 不能证明
运行中的应用已更新，反过来也一样。两边各自用实际输出验证。

如果某版本在 Probe/编译阶段就失败，后续启动/reload/HTTP 项标为 blocked，不生成
“readiness、reload、HTTP 全失败”的重复问题。不得改低 Java 级别后称原项目已支持。

## 8. 定位与停止范围

每个新问题优先做一次受控复现：固定版本和 JDK，保持其他条件不变，定位最早失败
阶段。读取源码、配置、MCP 原始 JSON、stderr、Worker 日志、生成结果和进程参数。
可提取小复现，但不能用小样例通过替代原项目失败，也不自行修复 joLink。

网络/依赖受阻可检查一次代理、镜像、Wrapper、离线缓存与官方配置，并做一次合理
重试。记录是原生与 joLink 都失败，还是原生成功、joLink Probe 的仓库/设置传递失败。
后者可能是产品问题，不能一概丢进“环境故障”。不无限重试远端仓库，不改 TLS 校验。

测试失败按实际情况分，不要只有“支持/不支持”：

| 分类 | 依据 |
|---|---|
| PRODUCT_BUG | 实际流程/结果错误，例如修改没生效、漏编、错模块、缓存恢复退化、配置没传到 Runner |
| CAPABILITY_GAP | 原生路径成立，但 joLink 明确缺少语言级别、生成任务、Processor、测试配置等能力；拒绝不是通过 |
| ENVIRONMENT_BLOCKED | 缺 JDK/依赖/数据库/容器或网络问题使有效对照无法完成；记未验证 |
| UPSTREAM_OR_COMBINATION_FAILURE | 原项目在所选版本/JDK/配置下也实际失败；保留对照，不归咎 joLink |
| INCONCLUSIVE | 证据尚不足以归因，列出下一条最小验证，不强行宣布根因 |

一个项目碰到问题，记录之后继续其他可独立执行的阶段或项目。高优先级是会阻断常见
启动/测试，或会“看似成功但执行旧代码”的问题；按实测影响排序，不按错误文案是否
显得安全来排序。不凭一次异常推断发生频率，也不把现有文档的支持边界当作免责理由。

T10 取消使用 `java_application` 的 `{"action":"cancel_test","test_run_id":"实际ID"}`。
只取消本轮测试、停止本轮受管应用。结束 MCP 后检查本轮 Worker/Runner 是否退出；
MCP 尚开着时 Worker 保活是正常现象。不要结束机器上所有 java/Gradle/IDE 进程。

## 9. 每组报告与证据

每个“项目版本＋JDK 组合”至少留下：

```text
report.md                 人可读结论
environment.json          OS/架构、各 JDK、构建工具、joLink/项目 SHA
mcp-calls.jsonl           实际参数与完整 structuredContent，带时间和请求/Attempt ID
native-command.txt        cwd、命令和非敏感配置来源
native-result/            目标模块测试报告、stdout/stderr
mutation.patch            每次有预期的临时源码修改与恢复记录
logs/                     本轮 MCP/Worker/Runner 相关日志
```

不要把公司 Maven settings 内容、环境变量全集、token、用户目录中的无关文件打包。
日志中的认证和个人路径对外分享前脱敏。报告先留在 RUN_ROOT，不自动上传、推送。

报告模板：

```markdown
# Pxx / 版本 / JDK 组合

## 输入
- 项目 URL / commit：
- joLink commit / dirty 状态 / MCP 连接方式：
- OS / CPU 架构：
- 原生构建 JDK / toolchain / main-test source-target-release：
- Worker JDK / 应用或 Runner JDK（实测，不可见写 unknown）：
- 构建工具、profile、环境适配、实际 project_path / 目标模块：
- 原生 baseline 与 product 是否独立，是否有预编译/生成文件辅助：

## 实测
| 用例 | 调用/证据路径 | 原生对照 | joLink 实际结果 | 结论 |
|---|---|---|---|---|
| T01 | | | | |

- 测试选择器、实际 tests/passed/failed/skipped：
- 源码改动、预期差异、实际差异、恢复后结果：
- requested/actual build kind、实际编译范围（未观察写 unavailable）：
- 冷/热/跨 MCP：总耗时、bootstrap/freshness/source_scan/compile/runner：
- 应用启动到 ready、HTTP、reload 的实际行为（库项目写 N/A）：
- 退出与源码恢复情况：

## 问题
- issue_id / 分类 / 优先级：
- 已观察事实：
- 最早失败阶段、错误码和最小复现步骤：
- 与原生对照的差异：
- 当前原因判断及证据强度：
- 哪些尚未验证，下一条最小验证是什么：
- 未修改 joLink；是否使用辅助配置/步骤：

## 范围结论
- 哪些模块、测试和操作已通过：
- 哪些部分受阻/未测/不适用：
- 不能从本次结果推导出的结论：
```

计时不要重复相加：runner_ms 包含 Runner 生命周期和测试执行；test_ms 也不能直接
等同于纯测试方法时间。bootstrap_ms 可包含磁盘恢复，不等于 FULL；restart 可能沿用
首次 bootstrap 摘要。必要时用旁路响应/进程证据确认，不从几个字段推断“缓存没用”。

## 10. 最终交付

更新 RUN_ROOT 下两个总表，不只输出一段“全部完成”：

1. `index.md`：10 个版本样本×实际 JDK 组合，每行列出原生基线、冷编译、热复用、
   修改生效、错误恢复、跨 MCP、Fast Test、启动/reload、已测范围与报告链接。
2. `issues.md`：合并同根因问题，但保留受影响项目/版本/JDK；按实际阻断范围和错误
   结果风险排序，给出复现证据、归因置信度和建议下一步，不实施修复。

每项使用 PASS / FAIL / BLOCKED / NOT_RUN / N/A；解释预期失败的控制用例为何通过。
不能把 BLOCKED、明确不支持、全部跳过合并成 PASS；不能把“未发现 bug”写成“普遍支持”。
完成主组合后再补对照 JDK，执行受中断时先更新进度，下一模型从未完成项继续，不重跑
已经完成的所有冷启动。新增第十个仓库、JDK 25、大型并发压力或 debug 专项留待下一批。

相关产品说明：[Fast Test](fast-test-v0.1.zh-CN.md)、[JDT 多模块](jdt-modules.zh-CN.md)、
[Gradle 多模块](gradle-modules.zh-CN.md)、[持久化记录及暂缓问题](jdt-workspace-persistence.zh-CN.md)。
