# joLink Fast Test v0.1

## 目标

Fast Test不是`mvn test`的MCP包装。Maven只在第一次或Build World失效时执行
一次`test-compile` Bootstrap；后续修改走持久JDT增量编译和独立JUnit Runner。

```text
工作源码
→ JDT main/test Incremental
→ main-bin/test-bin TestRunLease
→ 独立Test Runner JVM
→ 本地身份绑定协议
→ 结构化结果
```

测试不会修改当前Runtime Generation，也不要求业务应用存在。推荐流程：

```text
修改
→ fast test
→ 通过
→ reload
→ Runtime请求/断点验证
```

## 产品接口

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

短测试直接返回结果；较慢测试返回`status=running`和`test_run_id`，通过
`java_status(status)`观察，或通过`cancel_test`取消。

断言失败的语义是：

```json
{
  "ok": true,
  "status": "completed",
  "passed": false,
  "runtime_unchanged": true
}
```

编译、Runner协议、超时或进程收敛失败才返回`ok=false`。

`project_path`现在可指向受支持的Maven或Gradle Wrapper项目。二者先转换为同一个
`JavaTestBuildWorld`，JDT与Runner不读取POM、SourceSet或Test Task原始字段。
Gradle产品边界和证据见[Gradle G3](gradle-g3-product.zh-CN.md)。

## 实现边界

- Probe v2随wheel分发，JAR/POM/implementation identity均有SHA校验；
- Probe从Maven会话导出main/test source roots、classpath、output、resources和
  Processor事实，不修改用户POM/settings；
- 标准静态Maven Reactor优先由显式test selector唯一定位目标jar模块；
  Bootstrap使用`-pl <module> -am`，上游模块当前仍由Maven编译并作为workspace
  output进入classpath；只有目标模块常驻JDT增量模型；
- 上游模块Java源码变化会使Build World失效并触发新Maven Bootstrap，不会拿旧
  上游class继续测试；`source_files`始终相对`project_path`，目标模块源码进入
  JDT，上游源码由Maven吸收，无关/下游模块fail-closed；
- 一个`IJavaProject`使用`IClasspathAttribute.TEST`和独立`bin/test-bin`；
- main/test diagnostics分别计数，测试源码错误不会伪装成main编译错误；
- Test Runner为Java 8字节码，支持显式JUnit 4/5与TestNG Class/Class#method；
  对应API和Engine来自项目test runtime
  classpath；Surefire未把Platform Launcher放入项目classpath时，顺序固定为
  项目声明→本地精确版本→Maven解析精确版本→受限同major fallback，并返回版本、
  来源和fallback原因；禁止跨major猜测；
- stdout/stderr进入私有`test.log`，结构化结果走localhost身份绑定协议；
- 单次测试输出超过4MiB会终止Runner；最多返回8个有界失败栈并明确标记截断；
- 每个Attempt生成Java 8 pathing JAR，Manifest Class-Path指向Runner和项目依赖，
  不把数百个依赖展开进Windows命令行，同时保持Application/System ClassLoader
  语义；
- 编译失败会把working compile state标记为`failed`，在下一次成功编译前禁止
  运行旧class；
- 临时Maven settings在Probe报告读取后立即删除，不进入持久Session；
- TestAttempt取消在Bootstrap、snapshot、JDT、Tier1和Runner边界重复校验；
- compiler从FULL开始到Tier1、baseline、fingerprint和Project构造完成前始终由
  initializing事务持有，任何异常都会关闭Worker；
- Runner发送并关闭终态协议后显式`System.exit`，测试遗留非daemon线程不会造成
  假timeout；
- `System.exit`、超时、取消和残留线程只影响Runner进程树，不影响JDT Worker。

## v0.1明确不支持

- Java 17+ source/target（当前JDT 3.25 candidate只正式验证到Java 11）；
- 动态Profile控制module列表、无法由selector/source_files唯一确定目标模块的
  Reactor，以及上游模块的JDT增量编译（上游变化当前安全回退Maven Bootstrap）；
- 自定义Surefire Provider，以及任何未建模的Surefire/Failsafe VM参数、system
  properties、插件依赖或其他运行配置；无自定义运行配置的Failsafe测试类可作为
  普通显式selector执行；
- 自动impacted test选择；
- main/test不同Processor path；
- 显式Processor path、未解析discovery mode或Processor `-A` options；
- Surefire采用default-deny：除少量纯报告格式选项外，任何非空未建模运行配置
  都会拒绝；
- 删除Processor驱动注解时，JDT native incremental仍可能保留旧generated
  metadata；该边界沿用现有Processor invalidation backlog，不能把测试通过当成
  已清除旧生成物的证明；
- MapStruct 1.3.0真实探索确认：Maven冻结的generated source与JDT APT再次生成
  同名`MapperImpl`会产生duplicate type。当前正确返回
  `JDT_TEST_FULL_COMPILE_FAILED`，source-generating Processor仍未支持；
- 并行Fast Test和常驻Test Runner。
- JUnit5 uniqueId/tag/package selector；v0.1支持显式Nested Class#method、参数化
  方法、递归JUnit5组合注解，
  混合框架识别覆盖标准JUnit4/5、JUnit4 `@RunWith`与TestNG注解。
- 本轮真实产品E2E在macOS完成；404项、包含空格和中文路径的classpath-file /
  pathing JAR已通过，但发布前仍需补一次真实Windows Fast Test E2E。

JUnit 5要求项目提供Engine。Platform Launcher属于joLink Runner基础设施：优先由
同一次Maven Bootstrap解析Engine同版本Launcher；离线环境可复用同major且不旧于
Engine的本地Launcher。该选择会检查实际class内容，且不会跨major注入不兼容版本。

当前Fast Test维护自己的headless持久JDT session。若同一MCP同时启动了业务应用，
Runtime reload session与Fast Test session暂时相互独立；两者不共享可变output，
因此正确性隔离成立，但内存与重复main编译仍是后续优化项。

## 剩余边界优先级（2026-08-30）

已经确认不能用小补丁安全放开的事项按以下顺序保留：

1. **P0：Java 17+ source/target。** 当前锁定JDT 3.25正式支持到Java 15，产品只
   验证8/11。需要升级并重新跑完整A1-A9、APT、Fast Test和Runtime回归，不能只加
   一个`VERSION_17`字符串。
2. **P1：source-generating Processor。** MapStruct 1.3已稳定复现Maven冻结
   generated source与JDT APT再次生成同名类型。需要冻结“生成物由Maven拥有还是
   JDT拥有”以及删除/重命名失效语义；当前继续fail-closed。
3. **P1：main/test不同Processor path和Processor `-A` options。** Eclipse APT
   factory path/options是project级状态；只有证明两套语义可隔离，或确认main/test
   完全相同后才能放开。不得简单合并两套配置。
4. **P1：自动impacted-test选择。** 需要可靠的源码/class/测试依赖图和动态框架
   兜底。当前显式selector虽然多一项参数，但证据边界清晰。
5. **P1：真实Windows E2E。** 长classpath已通过pathing JAR与空格/中文路径
   fixture，但仍缺真实Windows进程树、Maven、Worker和Runner闭环。
6. **P2：Reactor上游模块JDT增量。** 当前上游变化会安全回退`-pl -am`
   Bootstrap；要提速需维护多project依赖图和跨module generation transaction。
7. **P2：overloaded method签名、tag/package/uniqueId selector。** 当前公共接口是
   `Class#method`；参数化的唯一同名方法可自动补签名，真正重载需要扩展selector
   contract，不能由Runner猜。
8. **P2：并行Fast Test/常驻Runner。** 当前单Attempt + 一次性Runner隔离简单且
   可取消；并发会引入共享Build World lease和测试进程资源仲裁。

## 本机实测（2026-08-29）

真实JDK8/11链路已覆盖：

```text
Maven test-compile + Probe v2 Bootstrap
JUnit4 baseline PASS
JUnit4 passed/ignored/assumption计数 PASS
20轮main+test双源码快速连续Incremental/Test PASS（同一Worker）
main API变化 → test-only compile failure
修复 → incremental recovery PASS
main方法体错误 → assertion failure
main修复 → PASS
test源码编辑 → failure/recovery
main/test resources读取 PASS；资源变化触发一次新Bootstrap PASS
JUnit5显式方法 PASS
JUnit5 `@BeforeAll` container failure → passed=false PASS
JUnit4/JUnit5混合selector（无Vintage）PASS
Lombok 1.18.20 + Spring metadata Processor + JUnit4 PASS
Processor项目双源码Incremental/Test PASS（约98ms）
Java 8 target + Maven/Worker/Runner JDK 8、11、17矩阵 PASS
Java 11 target + JDK11 jrt-fs + Spring Boot 2.5/JUnit5真实项目 PASS
Java 11方法体增量失败/恢复约14ms/10ms，产物class major 55 PASS
普通Java源码新增/删除：显式source_files同步、Worker deleted_source_units硬门禁、
增量编译与旧class删除 PASS
Spring Boot Starter未显式提供Platform Launcher的常见Surefire形态 PASS
JUnit4/5混合classpath中的JUnit5组合注解 PASS
JUnit5参数化方法（自动补全反射参数签名）、Nested Class#method、基类继承方法与
测试接口default method PASS
TestNG 7.11显式Class/Class#method、失败计数、增量失败/恢复 PASS
默认Failsafe声明不阻塞显式测试；未建模Failsafe配置fail-closed PASS
两模块Maven Reactor：`tests=app`且`source_files=lib/Value.java`仍定位app、
`-pl app -am`、workspace lib/classes、目标模块增量、上游修改/恢复自动Bootstrap PASS
System.exit隔离 PASS
非daemon残留线程由Runner主动退出清理 PASS
stdout协议隔离 PASS
timeout PASS
cancel/process-tree cleanup PASS
Surefire systemPropertyVariables未建模配置 fail-closed PASS
真实MCP test/status/cancel PASS
compile_failed后空source_files运行旧class的路径已拒绝
临时Maven settings成功/失败路径立即删除
.mvn/maven.config隐式offline + 本地Probe seed PASS
System ClassLoader直接加载项目class PASS
测试中修改resource → build_world_changes_pending + 下轮Bootstrap PASS
日志保留限制：最近8次失败+1次成功
```

在Bootstrap完成后的fixture中，新增20轮双源码压力的完整Fast Test wall time
最大约`200ms`，其中最大JDT compile约`29ms`、freshness约`1ms`、source scan
约`1ms`、Runner启动与测试约`175ms`。测试本身耗时仍由项目JUnit/Spring逻辑
决定；该数据不代表大型企业项目性能。

本机标准Maven `source=target=11` Spring Boot历史项目已从原先的结构化拒绝推进到
FULL、Incremental与Fast Test闭环PASS；该证据不扩展到JPMS、release或所有Processor。
另一个Java 8遗留项目的IDEA配置引用了已不存在的`settings.xml`，在执行Maven前
即失败关闭，没有静默改用不同仓库配置；这是当前环境完整性边界，不是JUnit/JDT
执行崩溃。
