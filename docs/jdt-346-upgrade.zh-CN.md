# JDT 3.46 与私有 Temurin21（2026-09-13）

## 当前实现

产品切换为单一 Eclipse 4.40 / JDT3.46，不增加双引擎自动回退。

- JDT Core：3.46.0.v20260520-1003。
- Compiler Batch：3.46.0.v20260528-0407；APT、Resources、Equinox均来自同一4.40 release。
- Worker由现代javac以`--release 17`构建，class major61；不再要求JDK8构建Worker。
- 默认Worker运行时：Eclipse Temurin **21.0.12.1+1**，固定版本、平台资产和官方SHA256。
- Probe和Test Runner自身仍保持Java8字节码，项目的Maven/Gradle及应用/测试JVM不强制改成21。
- 语言级别映射8～26，现代系统库使用目标JDK的jrt-fs.jar；Java8继续使用其原系统库。
  已实测8、11、17、21；25/26、preview及完整JPMS不因此自动宣布支持。
- 当前模型仍要求相等的source/target；没有在本轮扩大非等值组合或目标平台模拟。

启动和Fast Test共用Worker运行时选择。旧Build World里缓存的build JDK不再成为
Worker运行时；它仍然供项目构建使用。只为Worker子进程设置JAVA_HOME/PATH，
不修改用户全局环境、IDE设置或项目POM。

私有JDK在`~/.cache/jolink-runtime/runtimes/temurin-21.0.12.1+1/<平台>/`安装一次，
各workspace共用。安装时校验压缩包；以后直接复用，不反复哈希JDK，也不额外启动
Java进程去重复探测官方已安装运行时。企业批准的64-bit JDK17+可用
`JOLINK_WORKER_JAVA_HOME`覆盖，覆盖路径沿用既有版本识别。

这里的“私有/内置”是由joLink管理的运行时依赖，不是把六个平台的JDK塞进通用wheel。
首次联网安装选择当前平台的包；固定元数据覆盖Windows、macOS、Linux的x64/arm64，
Linux为普通glibc发行包。本轮真实执行平台为macOS arm64，不将平台清单当成全部实测。

## 不改变现有开发循环

```text
准备固定Worker运行时（已有就直接用）
→ Probe模型/目标系统库沿用项目配置与缓存
→ 新引擎首次FULL，后续恢复持久workspace
→ 修改文件才增量
→ 原有launch / reload / Fast Test
```

更换引擎会建立新的workspace，首次FULL一次；不尝试迁移旧JDT内部state.dat。
现有无变化复用、立即SAVE、可选GC、10轮增量策略保持不变。不增加每轮哈希、
输出副本、重新审计或新的发布事务。

关闭索引适配：新版JobManager.activated是private，改为不启动索引线程、不排队，
awaitingJobsCount直接返回0。没有重新开启搜索索引。

诊断适配：新版JavaModelManager默认把部分builder trace交给DebugTrace，旧stdout
捕获会丢失项目名和部分原因。通过Eclipse DebugOptions的traceToStdOut选项接回
现有捕获通道；INFO/DEBUG才启用。日志级别、10轮边界与真实回退原因都有MCP回归。

## 在线准备与离线资源包

首次下载JDK和Eclipse依赖默认使用官方源。`JOLINK_DOWNLOAD_MIRROR=cn`选择
清华→joLink自建镜像→官方的顺序，连接或传输失败才换下一个源；也可指定自定义
镜像URL，设置为`official`则只使用官方源。不自动识别地区；当前本机清华访问仍被拦。
安装后的缓存复用、版本和SHA不变，
不会因为切换下载源而重新FULL。详见[镜像说明](runtime-download-mirror.zh-CN.md)。

在仓库已安装Python依赖的机器上：

```bash
uv run python scripts/prepare_jdt_worker.py
uv run python scripts/prepare_jdt_worker.py --offline-bundle /path/to/jolink-worker-platform.tar.gz
```

第二条会把**当前平台的JDK＋JDT依赖＋Worker配置/产物**打包，不只是打包JDK。
在同平台/架构的离线机器，把包解压到用户的`~/.cache/jolink-runtime`，保持包内目录。
Windows对应`%USERPROFILE%\.cache\jolink-runtime`。Python wheel及其Python依赖仍需
另行安装；不将这个资源包称为包含所有Python依赖的离线安装器。

本机已实际完成在线下载/校验/安装，生成约209MiB的Worker离线包；解压到新的HOME，
禁止下载后恢复Worker并编译Java17 record，使用项目JDK17运行输出正确。

JDK原有legal/LICENSE等材料保留。`--offline-bundle worker.tar.gz` 同时生成
`worker-sources.tar.gz`，包含对应版本的上游源码和构建脚本；两个包都有 `legal/`
说明。对外分发时一起提供，完全离线转交时也一起携带；运行时无需解压源码包。
首次准备源码可能额外下载约百余MiB，后续复用 `license-sources` 缓存；普通启动、
test、restart 不下载源码。许可与源码索引见[第三方声明](../THIRD_PARTY_NOTICES.md)。
仍需维护安全补丁；不要启动时追踪latest。

## 真实验证结果

| 场景 | 实际结果 |
|---|---|
| Java8产品链路 | 单/多模块启动、增量、源码布局、reload、新MCP复用通过，Worker用21，应用仍用8 |
| Maven/Gradle main8/test11、main8/test17、main11/test21 | 6组均通过，包含record/Java21 API、class major、错误恢复、重开；main不能误用更高版本API |
| 新版Petclinic，Java17 | JDT FULL和所选3项测试通过；实际应用启动ready，HTTP200，应用JVM17、Worker21 |
| MyBatis3.5.19，main8/test17 | 原样准备阶段仍有阻断，见下；仅在独立测试副本关闭format/license profile后，1316 sources编译无错误，83项所选测试通过；加RecordTypeTest后87项通过 |
| MyBatis增量 | 修改/恢复一个测试文件，均actual INCREMENTAL、1 source，Worker约559/574ms，87项通过；测试修改已恢复 |
| 持久化/GC/索引 | Java8/11单/多工程，多次无变化重开后仍增量；GC开关及索引关闭回归通过 |
| 日志/HotSwap超时 | INFO/DEBUG原因记录、10轮策略、普通更新、6秒确认、31秒unknown的8项MCP回归通过 |

上述结果不等于这些项目的所有测试均通过。MyBatis专项没有改Java源码/POM来绕过
编译器错误；profile差异明确记录。默认原样流程的失败没有删掉或计作通过。

## 兼容问题与后续处理

### 1. Lombok1.18.20：用户明确要求只记录

历史同组实测已确认JDT3.46下`@Builder(toBuilder=true)`会触发旧ECJ
`Expression#print`签名相关NoSuchMethodError，lombok.config也有兼容边界；
当时换回3.25后二者通过。记录在实验README及Phase1B历史中。

本轮不补Lombok兼容层、不自动升级/替换项目Lombok，也不按版本增加新的提前拦截。
普通注解能编译不代表上述用法已经修好。私有JDK21不能消除ECJ内部接口差异。

### 2. Checkstyle：新引擎的注解位置错误

同一SHA在3.25＋包路径适配后132项通过；本轮3.46 FULL返回5条
`Annotation types that do not specify explicit target element types cannot be applied here`。
涉及3个测试输入文件中的Nullable等注解位置；不是原6条默认包/目录错误复发。

**2026-09-13已定位，用户决定只记录、暂不处理。**

问题形态是`static <T> @Nullable T method()`：注解位于方法类型参数之后、返回类型
之前，该注解没有TYPE_USE适用范围。原始文件直接交给javac11/17可编译；
ECJ3.46在Host JDK17/21均返回同样5条错误，排除了joLink源码映射和Worker JDK21。
最小RUNTIME注解的反射对照还表明：javac把注解放在方法上，旧ECJ3.25却放在
返回类型上，因此旧版编译通过不等于注解元数据与javac一致。

另用独立Equinox应用调用原生Eclipse Java Builder（不加载joLink Worker）复现：
默认配置报错，`ignore_optional_problems=true`加`@SuppressWarnings("all")`仍报错；
声明注解移到`<T>`前、或使用真正的TYPE_USE注解则通过。未打开图形IDE窗口，
验证的是其原生Builder编译路径。

上游对应[Eclipse JDT #271](https://github.com/eclipse-jdt/eclipse.jdt.core/issues/271)，
创建于2022年，2026-09-13查询时仍Open；R4_40的
[test552082_comment_0](https://github.com/eclipse-jdt/eclipse.jdt.core/blob/R4_40/org.eclipse.jdt.core.tests.compiler/src/org/eclipse/jdt/core/tests/compiler/regression/NegativeTypeAnnotationTest.java#L4466)
仍明确期待这条错误。[OpenJDK历史解释](https://mail.openjdk.org/pipermail/type-annotations-dev/2014-March/001678.html)
认可该位置的方法声明注解。当前未找到可直接采用的Eclipse兼容开关或已合入修复。

本项保留为编译器语义兼容待办，不因Eclipse也失败就宣称joLink已支持该项目。
暂不修改用户源码、屏蔽错误、替换注解或增加编译器回退。未来重新处理时，目标是
正确区分此位置的方法声明注解与类型使用注解，并验证class元数据/反射与javac一致。
Checkstyle原文件是按文本分析的测试输入，不通过挪动原资源中的注解来制造通过结果。

### 3. Guava：泛型问题仍存在

原双测试类选择本轮3.46 FULL返回14条错误（上轮13条），主要仍是递归通配符/
类型转换，并多见ClosingFuture调用的泛型错误。升级没有自动解决这项兼容问题。

### 4. MyBatis：准备阶段插件，非Java17限制

默认format profile的OpenRewrite run要求fork生命周期，被原有准备阶段规则拒绝。
仅关闭format后，license插件长时间等待，实际线程栈停在许可证处理，尚未进入JDT；
已停止本次拥有的Maven进程。专项改为显式关闭format/license两个profile后编译和
87项测试通过。临时IDE配置已恢复；这不是默认项目全流程通过，也没扩大本轮插件处理。

### 5. APT间歇性遗漏：已定位，当前工作区修复

首轮出现的main generated类型缺失已通过多次真实MCP复现。编译当时，同一工程的
ProjectScope当前首选项为enabled，但JavaProject仍引用另一个旧首选项节点，值为
disabled；JavaBuilder因而没有初始化main处理器。不是main/test串用了Processor。
诊断还实际观察到：源码不引用生成物时，漏跑Processor也可能编译/测试通过。

该初始化顺序升级前已存在；旧JDT是否也触发尚无复现证据，不能把“升级后发现”
直接等同“升级引入”。本次直接在初始化compiler options时，根据该工程是否配置
processor path设置`CompilerOptions.OPTION_Process_Annotations`的最终值，然后
继续使用现有APT配置/加载流程。没有增加重试、sleep、逐轮检查或新的状态管理器。

Maven/Gradle回归补入了无编译期生成物引用的用例：通过反射读取生成类，验证生成
确实发生且增量修改后内容正确，覆盖冷启动、错误恢复和MCP重开。main无Processor、
test有Processor及两边独立Processor的旧用例继续保留。

修复后真实MCP/JVM回归38项通过：25项Processor/Lombok/main-test scope用例、
11项APT资源生成/持久化/重开用例、2项Processor传递依赖及资源用例。
开源MapStruct＋Lombok模块也重新走了独立缓存冷编译和新MCP复用，两次所选测试
均1/1通过，未修改该项目源码；首次FULL5个编译单元，重开没有新BUILD。
普通回归745项通过（13项因显式环境开关或平台条件跳过）。
额外20轮独立Gradle冷启动也全部通过：每轮新项目/缓存/真实MCP，使用修复后的
产品Worker（无诊断Agent或强制开关），含生成物反射验证、增量、错误恢复和重开。
这20轮未再现遗漏，不将有限轮次通过表述为对所有时序的保证。

## 内存观测

同机独立Worker对MyBatis的1316份源码做FULL，默认-Xms64m/-Xmx2048m，自动GC关闭：

- FULL约2571ms，采样峰值RSS约357.8MiB。
- 手动GC约24.5ms；1秒后RSS约282.4MiB、heap used约31.4MiB。
- 没有Java indexing线程，索引队列0。
- GC后修改私有副本，actual INCREMENTAL、1 source、约92ms。

这是本机单次观测，不是对公司项目或Windows的性能保证，也没有用它改变产品GC策略。
原始MCP JSONL、内存JSON和临时诊断均留在本地，公开记录只保留结果与条件。

## 收尾验证

普通测试745 passed / 7 skipped。最新Worker上的30项编译/处理器/持久化回归、
8项日志/HotSwap超时回归通过；此前实际执行的Maven/Gradle多模块启动等回归也已
通过。新平台语言测试没有只检查参数接受，而是执行record、Java21 API、独立
class major及Runner JDK断言，并验证错误恢复和新MCP无变化复用。

wheel/sdist构建、隔离安装后产品资产与私有JDK发现、compileall、diff检查通过。
隔离安装wheel后再经真实MCP运行新版Petclinic所选测试，3/3通过；最终Worker
离线包也在新HOME、禁止网络下载的条件下完成Java17 record编译及运行。
没有修改jdwp_adapter，也没有提交或推送本轮改动。Windows实际运行、Java25/26
完整项目和上述已记录兼容失败不计入通过。
