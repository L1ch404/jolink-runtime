# JDT APT Spike：Eclipse 2021-03 标准 JSR 269 探索

状态：探索 PASS；尚未进入 production BuildWorld 或 MCP/Runtime。

日期：2026-08-23

## 一、目标

验证最小 Headless Eclipse/JDT Worker 加入标准 APT 能力后，能否：

```text
FULL 执行 Spring Configuration Processor
生成 META-INF/spring-configuration-metadata.json
字段新增时真实 INCREMENTAL 更新 resource
字段删除时清理 stale metadata
与相同 APT candidate 的独立 clean FULL oracle 一致
```

同时记录 candidate 大小、启动时间和 RSS。此实验不修改现有证据 candidate，使用独立：

```text
eclipse-2021-03-no-apt-spike
eclipse-2021-03-apt-spike
```

## 二、最小 APT bundle closure

APT candidate 在原 stripped closure 上增加 4 个 root units：

```text
org.eclipse.jdt.apt.core
org.eclipse.jdt.apt.pluggable.core
org.eclipse.jdt.compiler.apt
org.eclipse.jdt.compiler.tool
```

p2 mandatory dependency resolution 额外带入 `org.apache.ant`，所以最终：

```text
baseline candidate bundles  18
APT candidate bundles       23
bundle delta                 +5
candidate bytes delta        +3,994,134 bytes
```

Closure 中没有：

```text
org.eclipse.jdt.apt.ui
org.eclipse.jdt.ui
org.eclipse.ui
SWT
m2e
jdt.ls
```

因此方案 A 不需要引入 Eclipse UI 或语言服务器。

## 三、Worker APT 配置

APT 是可选能力。没有 `--apt-processors-file` 时，原 Worker 行为不变。

APT spike 通过 Eclipse APT bundle 自身 classloader调用公开 API：

```text
AptConfig.initialize()
AptConfig.setGenSrcDir(...)
AptConfig.setProcessDuringReconcile(false)
AptConfig.getDefaultFactoryPath(...)
IFactoryPath.addExternalJar(...)
AptConfig.setFactoryPath(...)
AptConfig.setEnabled(true)
```

READY 回报：

```text
apt_enabled
apt_factory_path_requested/effective_count
apt_factory_path_requested/effective_identity
apt_factory_path_verified
apt_generated_source_requested/effective
apt_generated_source_verified
```

以上字段来自 `AptConfig.getFactoryPath()`、`getGenSrcDir()` 和 `isEnabled()` 的
Eclipse effective-state readback，不只是joLink请求值。

Factory Path按Maven Provider artifact原始顺序保存。由于`addExternalJar()`插入path
头部，Worker按逆序调用，从而使Eclipse effective order与Maven requested order一致；
requested/effective identity均按有序sequence计算，不通过排序掩盖顺序差异。

基础 Worker manifest 没有增加 APT hard dependency；只有 APT candidate 且传入 Processor
path 时才加载和配置该子系统。

## 四、最小 Spring Configuration Processor fixture

Fixture 只有一个 Java 8 类型：

```text
@ConfigurationProperties(prefix = "demo")
DemoProperties.name
```

Processor：

```text
org.springframework.boot.configurationprocessor.
ConfigurationMetadataAnnotationProcessor
```

Factory path 只包含 Processor JAR；普通 Spring compile dependencies 只进入 Java
classpath，不进入 factory path。

### 4.1 FULL

```text
compile_ok                   true
errors                       0
duration                     182.1 ms
APT enabled                  true
factory path entries         1
generated properties         demo.name
class count                  1
```

当前 fixture 中 Maven/javac 与 JDT 生成的关键 configuration property name/type 集合
一致；本轮没有声明 hints、default、description、deprecation 等完整 metadata 语义已
全部规范化。

### 4.2 新增字段 Incremental

临时增加 `timeout` 字段和 getter/setter：

```text
actual build kind            INCREMENTAL
compiled source units        DemoProperties.java only
duration                     205.4 ms
demo.timeout present         true
class oracle equal           true
metadata oracle equal        true
```

### 4.3 删除字段 Incremental

恢复原源码：

```text
actual build kind            INCREMENTAL
compiled source units        DemoProperties.java only
duration                     210.3 ms
demo.timeout absent          true
class oracle equal           true
metadata oracle equal        true
```

旧 metadata entry 没有残留。

### 4.4 删除 annotation/source

扩展 lifecycle case 后发现：

```text
删除 @ConfigurationProperties annotation
    Native INCREMENTAL 保留旧 metadata resource

删除整个 source
    class 正确删除
    Native INCREMENTAL 仍保留旧 metadata resource
```

两种情况下：

```text
native_incremental_stale            true
CLEAN + FULL fallback oracle equal  true
```

因此 Spring Processor 的当前安全策略是：

```text
配置字段新增/删除      Native APT INCREMENTAL
annotation/source删除  CLEAN + FULL fallback
```

不能宣称 Native Incremental 已独立解决所有 generated-resource stale cleanup。

## 五、最小 fixture footprint

同一台 Mac 的单次探索数据：

```text
                               baseline      APT passive    APT active
candidate size                 11,778,517    15,772,651     15,772,651 bytes
Worker startup                 757.2 ms      649.5 ms       640.6 ms
READY process-tree RSS         138,985,472   141,688,832    144,965,632 bytes
```

相对 baseline：

```text
APT bundles installed but inactive READY RSS  +2,703,360 bytes
Spring Processor active READY RSS              +5,980,160 bytes
FULL peak RSS                                  152,993,792 bytes
add-field incremental peak                     145,162,240 bytes
delete-field incremental peak                  143,949,824 bytes
```

启动时间差异处于单次进程噪声范围，没有观察到秒级退化。最小 fixture 表明 APT capability
本身的常驻成本较低。

## 六、423-source旧企业模块验证

项目名、包名、仓库和源码均脱敏。沿用之前严格离线 Maven/Probe 取得的 BuildWorld：

```text
Java sources                     423
compile dependencies             245
Lombok                           1.18.24 / ECJ agent
standard Processor factory path  2 JARs
```

Factory path 只包含实际声明非 Lombok Processor service 的：

```text
Spring Configuration Processor
MapStruct Processor
```

Lombok 继续走 `ECJ_AGENT_TRANSFORM`，不重复加入标准 factory path。

Probe保留Maven compile classpath中Provider artifact的原始顺序；423-source回归实际请求：

```text
Spring Configuration Processor
MapStruct Processor
```

Worker通过逆序`addExternalJar()`抵消Factory Path头插语义，Eclipse effective order与
requested order的有序identity完全一致，且unexpected非EXTJAR container数量为0。

### 6.1 FULL

```text
compile_ok                       true
compiled source units            423 / 423
errors                             0
warnings                         324
duration                         4,160.4 ms
APT enabled                      true
factory path entries               2
Spring metadata present          true
metadata bytes                   1,433
metadata properties                  6
generated Java sources               0
Worker shutdown                  settled
```

### 6.2 新增配置字段 Incremental

只在 private workspace 中临时给一个 `@ConfigurationProperties` 类型增加字段：

```text
actual build kind                INCREMENTAL
compiled source units            2
errors                            0
duration                         650.5 ms
metadata property count          7
new property present             true
class oracle equal               true
metadata oracle equal            true
```

### 6.3 删除配置字段 Incremental

恢复原源码：

```text
actual build kind                INCREMENTAL
compiled source units            2
errors                            0
duration                         606.2 ms
metadata property count          6
temporary property absent        true
class oracle equal               true
metadata oracle equal            true
```

这证明标准 Eclipse APT 在当前 stripped Worker 中能对真实企业模块维护 generated
resource 的增量更新和字段级 stale cleanup。

## 七、企业模块 footprint

同一 BuildWorld 的单次对照：

```text
                         no APT FULL    APT FULL / 2 Processor JARs
Worker READY RSS         189,857,792    154,615,808 bytes
FULL peak RSS            605,290,496    692,043,776 bytes
FULL duration            4,175.7 ms     4,160.4 ms
```

READY RSS 受进程/GC时机影响较大，不能根据单次较低数值宣称 APT 节省内存。FULL peak
相对本次 baseline 增加约 87 MB；FULL duration 基本相同。

一次把全部 244 个 dependency 都放进 factory path 的错误对照达到约 770 MB peak，说明：

> factory path 必须是 Processor及其真正运行依赖的最小集合，不能直接等同 compile
> classpath。

## 八、当前结论

这次探索已经证明：

```text
Native Eclipse APT minimal closure             可控
Spring resource-generating Processor FULL      PASS
真实 APT Incremental                           PASS
generated resource refresh                     PASS
field deletion stale cleanup                   PASS
same-candidate clean FULL oracle                PASS
423-source企业模块                              PASS
```

因此，旧报告中的：

```text
unknown_compile_time_annotation_processor
```

已经不再等于“stripped Worker完全没有标准APT能力”。对 Spring Configuration
Processor，方案 A 已经获得正向证据。

Processor provider artifact已由收紧最终边界的 `0.1.0-spike6` Maven Probe以：

```text
discoveryMode = IMPLICIT_COMPILE_CLASSPATH
processorProviderArtifactPaths
providers
options
```

形式严格离线导出，APT runner 可直接使用Probe snapshot，不再必须依靠人工路径。该字段
只证明Provider所在artifact；通用Processor runtime dependency closure仍未宣称解决。

spike4还冻结以下fail-closed边界：

```text
非空 -A options                         reject
显式 Processor names                   reject
execution-level Processor config       EXECUTION_CONFIG_UNRESOLVED / reject
explicit annotationProcessorPaths      EXPLICIT_DECLARED_UNRESOLVED / reject
plugin-level legacy A... options       reject
maven.compiler.proc property           reject
raw processor compiler args            reject
proc=only                               reject
directory型Provider                    reject
```

Probe JAR固定`project.build.outputTimestamp`；相同源码连续两次严格离线构建并写入同一
本地坐标成功，没有再触发artifact byte collision。

## 九、尚未产品化的边界

- 当前只支持 Probe 的隐式 compile-classpath Processor发现；显式
  `annotationProcessorPaths` 仍标记为 unresolved/fail-closed；
- Processor activity/output model 尚未接入 BuildWorldSnapshot；
- generated resources 尚未纳入 build-generation publication manifest；
- annotation/source删除必须使用CLEAN+FULL fallback，尚未产品化；
- MapStruct 在企业模块中仍然没有输入和 generated source，不能据此宣布支持；
- 未验证公司 4201-source 项目的 Processor身份和 Incremental；
- footprint 是单机单轮探索数据，不是性能承诺；
- APT spike candidate 不继承原 candidate 的 A1-A10/A9/Phase 1B 证据。

## 十、下一步最小事项

1. 把 Probe Processor facts 接入正式 BuildWorldSnapshot；
2. 把 generated resource change/delete 加入candidate generation/output manifest；
3. 将annotation/source删除映射到CLEAN+FULL fallback；
4. 解析显式 `annotationProcessorPaths` 和 `-A` options；
5. 单独建立TypeAnnotation order oracle fixture；
6. 再把Spring Processor从unknown gate提升为verified APT fast path。
