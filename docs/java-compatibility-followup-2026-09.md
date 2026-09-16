# 第一批开源兼容性：小范围修复与待讨论项

## 最新汇总（2026-09-16，优先于下方历史记录）

### 已提交基线与本轮范围

JDT3.46＋私有Temurin21升级与APT修复已提交为`a168058`，不再是待review工作区。
运行时后台下载准备已提交为`bb5ae70`；launch/test同步等待及统一timeout已提交为
`3b2e2bd`。上一轮配置缓存修复已提交为`eea26fe`，但Review确认其生产接线仍有遗漏。
本轮只修Review第1/2/3/6项；第4/5项按用户决定只记录，Gradle引用脚本也继续暂缓。
不将这些剩余问题写成已解决。Java17语言限制已在新引擎
消除，main8/test17与main11/test21的Maven/Gradle真实回归通过；新版Petclinic的
所选测试、启动和HTTP通过。MyBatis在测试副本关闭format/license profile后87项
通过，原样准备阶段仍阻断。详见[升级实现、离线准备、性能与剩余问题](jdt-346-upgrade.zh-CN.md)。

新实测也暴露了回归，未用忽略错误或修改业务源码掩盖：Checkstyle5条注解位置错误，
Guava14条泛型错误。隐式APT遗漏已定位到JavaProject缓存的旧首选项节点仍关闭APT，
`a168058`已改为初始化编译选项时直接写入最终开关，U7已关闭；详见升级文档第5项。
Lombok1.18.20历史已知问题按用户要求只记录，不修、不自动换依赖、不增加版本拦截。

源码生成改动已提交为 `24df38e`。以下将已解决的入口问题、当前确认的项目阻断、
尚未覆盖的能力分开，不把同一问题重复记账，也不把越过入口当作项目全流程通过。

2026-09-13后续：U1的私有源码包路径适配已提交为 `05ea849`。Checkstyle原SHA
通过真实MCP完成4079份源码FULL，0 errors；6个相关测试类共132项通过。
新MCP无变更重开不编译；临时修改一个原默认包样本后，实际INCREMENTAL只编译
1个文件，相关15项测试通过，恢复后再次通过，原项目git状态干净。
这解决的是U1及所选测试，不代表Checkstyle全部测试套件已验收。
映射持久化、性能和通用回归见[源码布局适配](jdt-source-layout.zh-CN.md)。

### 尚未解决的功能兼容项

| 编号 | 当前问题 | 最新事实 / 与旧问题的关系 | 下一步 |
|---|---|---|---|
| U2 | Guava 泛型错误 | 新3.46原选择器FULL仍失败，14条诊断（旧版本13条），升级没有自动解决 | 缩小表达式并对齐输入，区分编译器差异 |
| U4 | Error Prone等javac专属检查 | TestNG/Mockito相关配置仍未转成“明确不执行检查但继续编译”的行为。Gradle构建逻辑、动态参数导出、main/test分离已解决，不能再用这些旧原因解释它们。TestNG框架本身已通过测试 | 当前只记录、不实现检查；放行项目并说明检查未执行的策略需另行接入、复跑项目 |
| U5 | Checkstyle新引擎注解位置错误 | 已定位为泛型方法类型参数后的声明/类型注解归属差异；独立ECJ及原生Eclipse Java Builder均复现，不是U1目录映射复发 | 2026-09-13用户决定只记录、暂不处理；保留原错误，不修改输入或屏蔽诊断，详见升级文档第2项 |
| U6 | MyBatis准备阶段 | format profile中OpenRewrite fork被拒；关闭format后license处理等待。关闭两者后main8/test17编译和87项通过 | 后续讨论准备步骤；不把专项结果当成原样全通过 |
| U8 | 旧Lombok1.18.20兼容 | 部分用法依赖旧ECJ内部接口，`@Builder(toBuilder=true)`等有已知失败；不是所有注解都不能使用 | 用户决定暂缓，不自动换项目依赖、不增加版本拦截 |
| U9 | Reload遇到未加载类 | 同一源码的变更class中有未加载类时，整轮要求重新launch，连已加载外部类也不更新 | 用户决定暂缓，详细证据见下文 |
| U10 | Gradle引用脚本漏入配置清单 | 真实MCP：`apply from: 'config/resources.gradle'`不在已记录输入内；只改脚本后，launch和Test仍复用旧模型 | 2026-09-15用户决定先记录，不实现脚本收集；不能用正则支持一个写法就宣称完整支持动态脚本 |
| U11 | IDEA切换构建选择/启动意图仍复用旧计划（Review第4项） | 真实MCP：只把选中的settings从A切到B，两文件内容不变，仍返回旧HTTP内容；生产load也忽略新的主类/参数/构建偏好。读取新BuildPreferences发生在缓存命中之后 | 2026-09-16用户决定先记录。不把“同一settings文件内容变化可感知”当成“切换settings/Profile也已支持”；后续区分构建选择与纯启动参数 |
| U12 | Probe执行期间改配置的基线窗口（Review第5项） | 生产方法受控复现：Probe返回旧UTF-8模型，但返回前POM已改US-ASCII；之后_stabilize拍到新文件，误认为旧模型仍有效。尚未跑真实Maven并发编辑窗口 | 2026-09-16用户决定先记录。本轮仍在Probe结束、JDT开始前取快照；不增加Probe重试/事务或业务源码快照。临时避免在Probe运行时编辑构建配置 |

U4与U6可合并讨论快速流程中构建步骤的边界：必要源码生成（如ANTLR）继续执行；
格式化、license检查及javac专属质量检查是否留给正式构建，需要明确接线和说明。
不能一概执行，也不能把所有准备步骤一起跳过。Guava的14条诊断不是14个独立根因。

### 2026-09-16：Review第1/2/3/6项收口（工作区，待review）

本地已核对Review包的两份完整源码哈希及9个方法AST。6项问题均复现，其中1/2/4/6
补了真实MCP验证；3/5当时为完整生产Python方法的受控验证，不混称完整JVM验收。

本轮实现：

1. 启动配置输入合并`load_module_worlds()`实际导出的模块POM，再交给现有父链收集。
   旧启动缓存若已有这些模块却漏了其输入，会未命中一次；普通完整缓存不因此全部失效。
2. Maven Fast Test在Probe→World转换处只登记持久原始配置，包括实际settings/default候选路径、
   入口及导出模块POM、.mvn配置；不将临时effective POM或资源内容放入配置快照。
   Fast Test磁盘格式升至v5，缺失settings来源的旧缓存重新Probe；不清空JDK/JDT资产。
3. 活跃`_FastTestProject`持有自己的配置快照，复用它时不读取磁盘模型为它背书。
   文件、环境及生成输入均按该对象的基线比较；过期后才尝试磁盘恢复或Probe。
   磁盘与活跃模型共用比较函数，不添加跨窗口调度或新锁机制。
4. direct JAR/classpath同步等待结束后，ready/unverified清除旧status/starting提示和等待超时标记；
   失败给出logs建议，真正仍starting时保留等待提示。不改原有初始JDWP握手例外。

纠正上一轮验收表述：Profile单测曾手工给plan补上模块POM，只证明收集函数能追父链，
没有证明生产入口会提供路径。旧direct测试也只看ready，未检查残留提示。本轮测试通过
实际_prepare_maven_probe与_finish_build_world接线，并检查最终返回状态与指导一致。
不以测试数量代替未覆盖场景的证据。

新增真实MCP已验证：

- Profile才激活的lib具有独立本地父POM，改父POM的参数名生成开关后，重新launch的HTTP
  反射结果由false变true；不能再继续用旧编译配置。
- 同一MCP中只改settings，Test实际读到新系统属性和资源；不变时及重开后继续复用。
- 两个实际MCP先后测试不同模块，共享根项目磁盘模型，但各自保留Worker：B更新配置后，
  A会刷新自己的旧模型；A替换磁盘模型后，仍有效的B无需重新建模。测试没有并发执行用例。
  此证据不等于所有同目录多Worker并发场景都已验收。
- 延迟开放端口的直接启动最终ready后，不再残留要求继续查status的提示。

本轮验收（macOS，未提交）：

- 新增18项单元用例/参数组合，重点经过实际生产转换和发布方法，而非由测试提前补齐输入。
  普通回归867 passed / 13 skipped，另外89项需显式开启的MCP/JVM测试未混入普通结果。
- 4组定向MCP通过：Profile父POM、settings刷新与重开、两个活跃MCP的模型隔离、直接启动最终提示。
  双MCP测试额外断言同一磁盘文件先被B、再被A覆盖，A旧Worker在比较前仍ready，避免场景未成立也绿。
- 7组已有MCP回归通过：Maven单/多模块和Gradle配置缓存、Gradle Kotlin多模块、模板生成、
  ANTLR单模块/Reactor。ANTLR初次因缺少JDK11测试变量跳过，补齐本机JDK11后两项实际通过。
- 定向lint、compileall、git diff --check、离线wheel/sdist构建通过。

U11/U12只记录，未改代码；U10亦未处理。没有修改jdwp_adapter、Worker/JDK交付或增加新工具。
旧Fast Test v4缓存首次会重新Probe以补齐配置来源，编译状态是否复用仍走原有规则；不清空
JDK/JDT下载缓存。本轮不宣称完整Windows验证，也不将同一Eclipse workspace的任意并发使用
算作已覆盖。

### 上一轮：构建配置缓存感知（2026-09-15，已提交eea26fe）

修复前的真实MCP对照只改资源目录配置，Java源码保持不变：

| 修改位置 | stop后launch | Fast Test | 原因 |
|---|---|---|---|
| Maven pom.xml：resourcesA→resourcesB | 仍命中Probe缓存，HTTP返回A | 新资源B与新的测试参数生效，测试通过 | Maven启动缓存没有比较POM配置 |
| Gradle build.gradle：同样修改 | 重新Probe，HTTP返回B | 新配置生效，测试通过 | 当前Probe记录的常规构建文件有比较 |
| Gradle config/resources.gradle（apply from引用） | 仍命中缓存，HTTP返回A | 缓存仍是resourcesA及-Dexpected=A，按旧配置通过 | 引用脚本未进入配置输入清单，U10继续保留 |

两种构建系统在配置修改后，对未改Java执行reload都曾返回`no_changes/applied=true`；
restart也继续返回旧内容A。restart复用现有编译产物是既有语义，缺的是配置过期提示。
另有缓存级复现：Profile导出的额外模块只记录了自身POM，未继续追踪其独立本地父POM。

历史原因已追到`91ab10e`：移除重扫描时把整个旧is_fresh调用移除了，轻量POM检查
也一起丢失；旧单测甚至要求“改了POM仍命中”。这不是JDT3.46或同步等待造成的。
测试必须约束用户需要的行为，不能只固定当前实现的返回值。

本轮改动范围：

- 启动与Fast Test共用小范围Maven配置输入收集；入口/Probe模块/本地父链都参与，
  包含Profile额外模块的父链；不扫描业务源码、class和依赖JAR。
- 启动的settings输入按实际选择登记：显式自定义文件不再额外跟踪未使用的
  `~/.m2/settings.xml`；使用默认路径时，即使文件不存在也记录，之后新增能触发重新Probe。
- 配置快照随构建模型保存；启动时配置变化重新Probe。旧启动缓存没有快照时只重取
  模型，不删整个.cache，也不重新下载Worker/JDK。
- reload/restart按当前运行模型的快照检查，变化时返回`BUILD_CONFIGURATION_CHANGED`，
  提示显式stop后launch；不停止当前JVM，不悄悄重编整个项目，不假报no_changes。
- 快照在JDT编译前固定，不能在漫长FULL之后把中途改过的POM记成旧模型的新基线。
- U10不在本轮实现范围；生成器隐藏/远端输入、未导出的仓库父POM等仍不能据此宣称
  全部覆盖。只按配置文件内容比较，包括注释变化；不新增POM语义差异引擎。

测试已覆盖：配置不变复用、只改源码/产物不重新Probe、POM增删改/无效XML、
同mtime/size不同内容、父链/子模块/Profile模块、缺失文件随后出现、旧缓存、
编译前快照及运行模型与后来磁盘缓存隔离。真实MCP验证新配置实际生效，而不只检查
命令行或缓存标志。

当轮macOS验收（只代表当时已覆盖用例，不替代上面的Review结论）：

- 新增49项配置边界单测；与原有缓存测试合计58项通过。后续又补4项settings选择回归，
  覆盖显式/默认及默认文件存在/不存在，并经过Probe参数生成、模型整理及缓存读取。
  测试直接经过cache.save/load，
  不只断言输入函数；明确禁止扫描/读取业务源码、class及依赖JAR来判断POM新旧。
- settings调整后普通回归849 passed / 13 skipped；跳过项为未启用的其他E2E及平台条件，不是代码失败。
- 新增3组真实MCP：Maven单模块、Maven父/子模块、Gradle常规配置。改配置后Test读到新值；
  旧Runtime的reload/restart被明确提示且PID/HTTP旧行为保留；stop后launch实际读到新值。
  配置不变跨MCP重开仍复用Probe，随后只改Java也复用Probe并实际运行新代码。
  后续真实MCP还验证了IDEA指定自定义settings：缓存重开正常，单独修改该文件后重新Probe，
  应用仍正确启动并返回预期HTTP内容。
- 已有4组MCP回归通过：JDT持久化/reload、Maven三模块（标准/自定义测试根）、Gradle
  Kotlin DSL多模块（包括增量、测试、取消、重开与Runtime-only依赖隔离）。
- compileall、定向lint、git diff --check及离线wheel/sdist构建通过。

新版旧缓存首次可能需要重新Probe；JDT是否重新FULL仍由模型身份和现有复用流程决定。
没有为了通过测试手工删除用户缓存。本轮真实运行不等同于Windows公司项目已验收。

### 下载体验与平台验证

固定资产、国内镜像选择与现有Worker JDK覆盖均已实现。`bb5ae70`进一步修复了
JAR逐文件留存、有限网络重试、底层错误日志、Windows新目录权限，并加入MCP启动后的
后台准备与临时进度。不能再把这些当作尚未实现；详见[下载与镜像](runtime-download-mirror.zh-CN.md)。

仍需区分网络条件与产品故障：清华部分环境异常请求/403、自建源公网带宽较低，
不能保证所有国内网络都快。公司Windows曾在重置目录后恢复，但不等于新版所有
Windows/Linux组合已验收；历史异常ACL不会由新建目录修复逻辑自动递归重置。

### Reload待办：未加载类（2026-09-13，用户决定先记录、不修改）

真实MCP测试中，`Foo.java`同时产生`Foo.class`和`Foo$Nested.class`。只修改外部类
方法体时，JDT输出变更也包含内部类；若`Foo`已加载而`Foo$Nested`未加载，当前
JdtReloadService在发送RedefineClasses**之前**返回
`RELOAD_REQUIRES_RELAUNCH / CLASS_NOT_LOADED`，连外部类也不更新。
测试让应用正常加载内部类后，再次reload可成功。

这是joLink把所有变更class都要求为“唯一已加载定义”的策略，不是JVM规定未加载
内部类必须重启。后续可讨论：对从当前JDT输出目录加载的应用，已加载类HotSwap，
未加载类保留新磁盘产物待正常加载；规则应通用于所有类，不按内部类或仓库特判。
本轮不放开这项判断，也不强制加载类。它与等待确认超时的HOT_SWAP_OUTCOME_UNKNOWN
不同，后者命令已经开始发送，不能断言JVM未更新。

### Reload确认等待：独立30秒（2026-09-13）

本机真实MCP/JVM复现：只改一行return值，测试Agent在类重定义回调中延迟6秒；
旧5秒socket读取超时返回HOT_SWAP_OUTCOME_UNKNOWN，之后HTTP却返回新值。
这证明该等待策略可以造成同类现象，不等于已经证明公司当次也是超时。

本轮仅给VirtualMachine/RedefineClasses设置30秒的回复等待，普通连接/命令仍沿用
原超时。等待从命令发送完成后计算一个deadline，穿插事件/其他回复不重置预算，
收到确认立即返回；不固定等待30秒，也不自动重发更新。每次读包沿用已有机制恢复
socket超时，不让后续普通命令继承30秒。真正超时/失联仍保留unknown语义。
没有修改未加载内部类策略、编译流程、Worker或增加环境变量。

回归沉淀在tests/e2e/test_reload_reply_timeout.py：正常更新、测试Agent延迟6秒仍
确认成功、延迟31秒仍报unknown且之后真实HTTP可能已更新。单元测试覆盖回复成功、
明确拒绝、超时、断连之后原socket超时恢复，以及多包共享一次等待预算。
本机验收：3项真实MCP超时回归通过；2项源码布局/启动/reload MCP回归通过，包含
未加载内部类仍被原策略拒绝的断言。普通测试738 passed / 7 skipped，compileall、
新测试lint和diff检查通过。

### 已知能力边界（不冒充本轮新发现的失败）

| 类别 | 仍未完成的部分 | 当前边界 |
|---|---|---|
| 生成任务 | Gradle生成任务、依赖其他生命周期/fork的Maven生成链、具体protobuf/OpenAPI项目验收 | Maven显式准备阶段及ANTLR/模板生成已支持，不应再统称“生成源码不支持”；APT生成也已经支持 |
| 生成输入缓存 | 远端、插件隐含输入、仅以整个项目根寻找输入等情况 | 现有本地配置路径跟踪不能保证覆盖这些变化，存在缓存过时风险；需补输入表达，不能当作已完整支持 |
| 测试运行配置 | 部分tags/engines/groups过滤、并行、重试、多fork及其他排序策略 | 字母正序/倒序、明确类/方法选择、已有运行参数已支持；剩余选项按实际项目补，不重造完整Surefire |
| 其他编译/模块配置 | 未映射编译参数、processor-module-path等未覆盖组合 | 名称选择、-A参数和-Xpkginfo:always已解决，不再混在此项；其他组合需实际验证 |
| 平台与JDK矩阵 | 新版Windows/Linux、更多JDK/Processor组合、完整JPMS | 已实测Java8/11/17/21；不能把Worker运行于21等同于所有目标版本已测，macOS证据不替代其他平台 |
| 进程保活 | Worker跨MCP常驻、Test Runner保活 | 尚未实现，属于后续能力；已有磁盘缓存和Worker重开恢复不是跨MCP常驻 |

### 已解决，不再作为待办

- 旧Petclinic启动中的direct-javac遗留路径；已有启动、HTTP、reload/restart和测试证据。
- Maven真实源码根/模块定位、Gradle build-logic加载与ArgumentProvider求值。
- main/test各自的语言级别、依赖、Processor与参数表达；Java17语言能力也已在新引擎实测。
- Processor加载路径、显式名称、-A参数、Lombok/MapStruct初始化，以及无值flag适配。
- Surefire字母排序、MyBatis的useIncrementalCompilation拦截、Checkstyle的-Xpkginfo:always。
- Checkstyle所需ANTLR生成及Maven源码准备；U1包路径适配已实现，132项所选测试通过。
- U3：新引擎已支持Java17编译；旧JDT3.25的限制留在下方历史记录。Java25/26等未实测组合不宣称已验证。
- U7：APT旧首选项节点导致偶发漏跑/假成功，已随`a168058`修复并回归，不再列为未解决。
- 已复现的HotSwap确认5秒超时已由`45beb4f`改为独立30秒等待；真正失联或超过30秒仍可unknown，不能混为同一个已修问题。

已有项目通过证据包括旧Petclinic的22项、Commons Lang的12项所选测试、MapStruct
示例的所选测试。它们不是每个项目全部测试套件的验收，也不是本次重新全量跑分。

### U1 初步定位证据（修复前，保留原因链）

Checkstyle通过build-helper把src/test/resources等目录加入test source roots，这是
项目真实配置。相关样本和测试用例明确覆盖“没有package声明”的情况，不能给文件
补package来让编译通过，否则改变了被测输入。

本机对照使用原6个文件，并补入原项目的InputRedundantImportCheckClearState.java
满足其中一个文件的通配符导入。没有补假类、没有修改这些源码：

| 路径 | 结果 |
|---|---|
| javac11，显式文件输入，--release11 | 成功；6个顶层class均位于默认包 |
| 产品中相同JDT3.25的ECJ batch，-11 | 成功；6个顶层class均位于默认包 |
| 真实MCP：一个原样文件放在临时工程src/main/java/fixtures/deep下 | JDT_TEST_FULL_COMPILE_FAILED，声明包与fixtures.deep不符 |
| 仅将临时文件移至src/main/java根，字节内容不变 | 1项反射加载测试通过，类名为原默认包类名 |
| 移回深层目录 | 增量编译再次返回相同包名诊断（JDT_TEST_COMPILE_FAILED） |

产品接线位置：jdt_modules.py的_materialize_sources和_private_path_for_workspace_source
保留相对物理目录，Worker通过JavaCore.newSourceEntry建立JDT源目录。JDT的
[SourceFile](https://github.com/eclipse-jdt/eclipse.jdt.core/blob/R4_19/org.eclipse.jdt.core/model/org/eclipse/jdt/internal/core/builder/SourceFile.java)
据此推导expected package；
[CompilationUnitScope](https://github.com/eclipse-jdt/eclipse.jdt.core/blob/R4_19/org.eclipse.jdt.core/compiler/org/eclipse/jdt/internal/compiler/lookup/CompilationUnitScope.java)
发现不一致后报错，错误恢复还会采用expected package。因此不是把错误降级就能
保证语义正确。

当时的初步方向：保留用户文件/资源路径，在私有JDT视图中按实际声明包安排源码或调整
source entries，复用现有source map。真正实施时需验证文件增删、package变更、
同名映射冲突与重开恢复；当时尚未实施。后续实现及验收见本节顶部更新。

原始诊断JSON和MCP调用记录只留在本机。此次javac仅作诊断对照，没有恢复产品
direct-javac路线。

---

## 历史记录（以下保留当时状态，不作为当前待办清单）

后续更新：用户已确认移除产品 direct-javac 路线。下面保留第一轮状态；旧 Petclinic
启动阻断现已消除，真实 MCP 启动、HTTP、reload、restart 已通过，见
[移除记录](direct-javac-retirement.zh-CN.md)。其他待讨论项不因此视为已解决。

2026-09-10，基于 `5d4877b` 后的工作区。原报告固定基线为 `e5b139e`；本轮不改写
原报告，也不把越过一个入口检查算作项目全流程通过。原始请求/响应和诊断留在本地。

第二项 Processor 后续进展：显式 Maven Processor 加载路径已接入，普通 MapStruct
样本真实 MCP 测试通过；后续接入初始化前置后，原样 MapStruct/Lombok binding 组合
也通过了真实 MCP FULL、测试及增量恢复。详见
[本轮实现、通用回归和剩余兼容问题](maven-processor-path.zh-CN.md)。

### 2026-09-11：MyBatis 的 Maven 增量参数拦截

已移除 `testCompile.useIncrementalCompilation` 对 JDT Fast Test 的拦截。
该字段是 Maven Compiler Plugin 自己的源码选择策略，不控制 JDT 的依赖分析；
保留 Probe 原始信息，产品继续按自己的持久源码索引与 JDT 状态复用/增量编译。
没有修改 MyBatis POM、语言级别、Processor、Worker 或其他参数规则。

真实 stdio MCP 使用 MyBatis 3.5.19 原 SHA `ee0d4f4831ffdd311b0183c202f4ad6492a3f404`
的独立副本、JDK17，执行 MetaObjectTest / SqlSessionTest。两次均越过旧配置拒绝，
Worker ready 后实际发起 FULL，随后返回 `JDT_BUILD_ABORTED`。保留的 Worker stderr
显示 `ResourceException` 包含 `AbortIncrementalBuildException`，发生在
`NameEnvironment.findClass` / 类型解析阶段。后续只读 jdb 定位到 record_type.Property，
源码是 Java record，而实际 JDT source/compliance/target 全为1.8；MyBatis 要求
testRelease17。这属于已知 main/test 编译级别未分开及 Java17 支持缺口，本轮未继续修。
不能把异常类名当作本次请求了 INCREMENTAL；外部请求明确为 FULL。
JUnit 尚未执行，不能写成 MyBatis 已完整通过。

新增 true/false 两组真实 MCP 回归均通过：冷编译、无改动复用、修改后预期断言失败、
恢复及新 MCP 无改动复用；普通测试 707 passed / 52 skipped。原始回归报告保持不变，
新的 MCP JSONL、mcp.log 和异常栈仅保留在本地。

## 本轮处理

### 2026-09-12：Maven 源码准备

上一轮名称/参数改动已提交为 `53ba2f4`。本轮接入 Maven 声明在源码/资源准备阶段
的执行项，沿用原插件和Maven执行顺序，不运行compile/test阶段。配置及准备输入
未变化时复用；变化后生成结果交给已有JDT workspace增量编译。

Checkstyle原SHA的ANTLR生成和main编译已通过，原Parser/Lexer缺失消失。新增实际
阻断是6个无package声明、却位于深层目录的测试资源样本。未修改样本或屏蔽错误，
Checkstyle整体测试仍未通过。该问题与源码目录/编译器环境映射有关，留待下一轮。
实现范围、缓存规则与真实回归见[源码准备记录](maven-source-preparation.zh-CN.md)。

### 2026-09-12 后续：Processor 名称/参数与 Checkstyle 参数

前一轮已提交为 `21c8fe7`；本轮按用户指定的两项继续，不接入外部生成任务、
不升级 JDT，也不静默关闭 Error Prone。

- Processor 名称和 `-A` 已接入启动及 Fast Test，复用 Eclipse 加载和 APT配置。
  显式名称不依赖服务声明，未选中处理器不初始化；main/test 配置各自保存与复用。
  详情、JDT3.25 无值 flag 的实际 NPE 及适配见[Processor 记录](maven-processor-path.zh-CN.md)。
- Checkstyle 的 `-Xpkginfo:always` 经实际输出验证，当前 JDT 默认已实现相同行为，
  因此只补共享参数分类，不加编译器补丁、不统一放开其他参数。Maven/javac 对照与
  真实 MCP 均验证空说明、SOURCE/RUNTIME 包注解、增量修改/删除/恢复、跨 MCP复用。
- Checkstyle 原 SHA `b54819d1f783a10ca8df13c5987ec0ac28052b3a`、JDK17运行环境、
  Java11源码，原样执行 CommonUtilTest，已经越过参数拦截并发起 JDT FULL。
  本次实际错误为缺少 grammar.java / grammar.javadoc 下 ANTLR 生成类（如
  JavaLanguageParserBaseVisitor、JavaLanguageParser、JavadocParser），不是原参数拒绝。
  生成任务按约定未执行，所以不能写成 Checkstyle 测试通过。源码/POM未修改。

本机收尾：普通测试711 passed / 7 skipped；相关真实 JVM/MCP 回归40项通过，
Fast Test专项6项通过；wheel/sdist、compileall、diff检查通过。新增场景包括没有服务
声明的指定处理器、未选中处理器哨兵、无值/空字符串/Unicode参数、缺失名称与恢复、
配置缓存重开，以及参数同时用于应用启动。临时MapStruct配置已恢复。

### 2026-09-12：测试排序与 main/test 编译配置分离

本轮只处理已确认的排序与配置表达，不升级 JDT，不接入 javac 专属质量检查。

- Surefire alphabetical/reversealphabetical 排序交给 Runner 实际按类执行；
  JUnit4/5、TestNG 均有正序、逆序和跨 MCP 回归，TestNG suite 生命周期保留。
- Maven 与 Gradle 均使用同一个 Worker 内的独立 main/test JDT 工程。配置分别保存
  在原有模块模型中；test 依赖 main，持久化、增量和上游传播复用现有多工程机制。
- 已实测 main Java8 / test Java11、不同参数元数据，以及 test-only Processor、
  main/test 各自不同 Processor（两个 JAR 可含相同处理器类名），生成物实际运行、
  main/test 增量、编译错误恢复和跨 MCP 无改动复用。main 看不到 test 生成的类型。
- Commons Lang 原固定版本、JDK8、未修改源码/POM，通过真实 MCP 完成 JDT编译，
  StringUtilsEmptyBlankTest 12/12通过。原 test-only JMH Processor 路径阻断已消除；
  新 MCP 重开后仍12/12，编译文件数0，总耗时约1.8秒。
- Guava 原双类选择器已越过 runOrder 阻断，仍在 JDT FULL 返回13项泛型错误；
  这是之前已观察到的编译兼容问题，不能计为 Guava 测试通过。
- Java17 本机直接执行打包的 JDT3.25：`--release 17 -version` 返回
  `release version 17 is not supported`。不是仅有产品限制；不放开校验伪装支持。
  MyBatis 原 SHA 真实 MCP 已识别 test Java17，返回 `JDT_TARGET_PLATFORM_UNSUPPORTED`，
  不再套用 main Java8 编译 record，也不把编译器限制误报成未安装 JDK17。
- javac 专属检查决定先不支持并记录。后续可明确把这类质量检查留给正式构建/CI；
  本轮没有修改 Error Prone 参数处理，TestNG/Mockito 仍需后续处理这个入口。

旧 main/test 共用配置缓存不能表达新布局，Test缓存schema更新一次。新workspace
建立后按原流程保存与复用，不增加每次构建的全量检查。

本轮收尾：普通测试709 passed / 7 skipped（另60项显式E2E未在普通命令执行）；
单独开启的相关真实 JVM/MCP 用例35项通过，其中新增排序、配置分离和处理器隔离12项；
Fast Test专项6项全部通过。既有Lombok/MapStruct“main/test路径顺序不同时应拒绝”
断言已更新为实际执行成功及无改动复用。wheel/sdist、compileall、diff检查通过。
Guava13项泛型错误、Java17及javac专属检查仍不计为支持。

### 2026-09-11 后续：真实源码目录与 Gradle 构建逻辑

- Maven 冷测试入口移除 Python 静态选模块和固定 `src/test/java` 查找。现有 aggregator
  Probe 在 Maven 已解析的项目中按真实 test source roots 选模块，只解析目标及所需
  上游依赖，并一起输出 effective model；选模块和导出共用一次 Probe 调用，不增加
  第二次 Maven 模型加载，不执行业务编译。
  同名测试无法唯一定位时保留明确错误，不按目录顺序随意挑选。旧静态选模块测试由
  真实 MCP 的标准目录/父 POM 属性继承自定义目录、同名冲突回归替代。
- Gradle 的启动和 Fast Test 两处 buildSrc/build-logic 目录拦截均移除。沿用 Wrapper＋
  原生 Probe，不新增 Buildship/JDT LS 服务；读取的是 Gradle 配置后的对象。
  单项目也使用现有模块模型，真实源码/资源目录不再落到旧单项目固定布局检查。
- 各实际加载的 Gradle build 输出配置输入，包含任意名称、嵌套 included build 的
  原生 SourceSet 源码/资源目录。现有启动/Test 缓存共用文件/目录输入处理，覆盖新增、
  修改、删除。业务源码不列为构建配置输入。修正脚本 glob 会匹配 `.gradle` 目录的
  情况，避免把瞬时缓存写入误当成配置变化。
- compilerArgumentProviders 由 Gradle 求值并导出实际参数，与直接 compilerArgs 合并。
  没有按 Provider 类名分支，也没有静默删除 Error Prone 等参数。

Maven Probe 为 `0.1.0-fasttest16`，Gradle Probe 为 `0.1.0-modules2`；Worker、Runner
和 jdwp_adapter 未修改。没有新增用户配置目录、MCP action 或独立缓存服务。

真实 MCP 回归：Maven 标准/自定义继承目录、多模块上游修改、启动/reload、测试和
跨 MCP；Gradle Groovy/Kotlin 多模块，以及 buildSrc、任意命名 included build 加
嵌套构建逻辑。后者验证：构建插件改变源码目录会刷新配置并改变测试结果；普通源码
修改不重新运行 Probe；动态 `-parameters` 通过反射确认生效；启动和测试跨 MCP 复用。
测试的业务 Gradle compile/test task 带失败哨兵，确认产品没有执行它们。

真实开源项目仍按实际可达阶段报告：

| 项目 | 本轮已确认 | 后续仍未解决 |
|---|---|---|
| Guava / JDK8 / 原 SHA | Probe 选中 guava-tests，真实 test root 为 guava-tests/test，并导出 guava、guava-testlib 所需源码根；原模块定位错误消失 | 原双类选择器仍被 Surefire runOrder 阻断。补充单方法选择通过既有单方法路径进入 JDT 后，报告 TypeTokenSubtypeTest 和 ClassToInstanceMap 的泛型类型错误；未修改源码或宣称全套测试通过 |
| TestNG asserts、core / JDK17 / 原 SHA | build-logic、build-logic-commons 正常加载、原生模型导出成功；目录拒绝与“Provider 尚未求值”不再阻断 | main/test 的 compilerArgs 不同：都含 Error Prone，test 多 XepCompilingTestOnlyCode；当前停在 GRADLE_TEST_COMPILER_CONFIGURATION_UNMODELED。两边 release 均为11，不是 Java17 源码问题 |

TestNG 首先遇到官方仓库 TLS/离线缓存缺失；沿用上轮记录的本机仓库 URL 标识后离线
完成模型导出，没有启动新转发器或跳过远端证书校验。仅在独立测试副本调整仓库配置，
并通过 --no-scan 禁止 Build Scan 发布；测试结束恢复这些修改。原始报告不改写，
本轮 MCP JSONL、原生模型及环境记录仅保留本地。后续错误不在本轮继续修复。

本机收尾：普通测试 706 passed / 55 skipped；相关真实 JVM/MCP 用例去重23项通过，
Fast Test 专项6项通过；wheel/sdist 构建和 diff 检查通过。未验证的平台不计入通过。

以下为前一轮历史记录，不覆盖上述最新状态。

1. **显式选类不再被测试发现过滤拦住。** Fast Test 总是明确提供 Class/Class#method，
   按 Maven `-Dtest` 的语义覆盖 Surefire `includes/excludes`；不只对单方法处理。
   Failsafe 的发现过滤也不用于这一显式 Surefire/Fast Test 选择。
   其他尚未实现的排序、并行、工具链及 Failsafe 运行配置没有统一放开。
2. **不执行的配置不再作为已执行配置检查。** Maven Probe 不收集没有 goal 的
   Surefire/Failsafe execution 配置。Commons Lang 的 `plain` 配置就是这种情况；
   原生基线实际运行的是 `default-test`，不能因为 `plain` 写了随机排序就拒绝它。
3. **额外运行 classpath 正式接入。** 从 effective POM 读取
   `additionalClasspathElements`，相对路径以目标模块为基准，追加到 Runner
   classpath。单模块、Reactor 和缓存复用共用；不会把这些路径加到编译 classpath。
   共用 Surefire 配置读取，保留未被 execution 覆盖的 plugin 字段。
4. **允许未选中的 Gradle Test 任务存在。** Probe 仍导出默认 `test` 任务，
   但不再要求它是唯一 Test 任务。`retryTest` 等额外任务既不阻断，也不会被执行。
   本轮没有增加任意自定义 Test 任务选择或额外 SourceSet 支持。
5. **启动错误不再丢失。** 单模块 JDT-first 启动不再要求 IDEA Make 提供正式
   Maven 编译基线。构造 JDT Plan 的实际异常直接返回，不再吞掉后只报
   `JDT_BUILD_WORLD_UNAVAILABLE`。隐式插件 goal 无法建模时附带 plugin/goals/reason。
   Gradle 模块转换异常同样保留自身错误码；main/test Processor 差异列出 artifact 名称。

过滤语义依据：[Surefire test 参数](https://maven.apache.org/surefire/maven-surefire-plugin/test-mojo.html#test)。
Maven Probe 已重新构建为 `0.1.0-fasttest12`，避免同一坐标覆盖旧内容；Gradle Probe
同步重新打包。没有修改 Worker、升级 JDT、删除项目插件或改业务源码绕过限制。

## 同版本开源项目实测

使用原报告对应 SHA 的独立 clone、真实 stdio MCP 和独立缓存，保留原生依赖/语言级别。

| 样本 | 本轮结果 | 仍未完成 |
|---|---|---|
| Petclinic 旧版 / Java 8 | 冷 JDT + Fast Test 22/22；无改动复用；改 Controller 重定向后出现预期 2 项失败，恢复 22/22；编译错误及恢复；跨 MCP 重开通过 | project launch 仍被旧插件检查阻断，未验证 HTTP/reload |
| Commons Lang 3.12.0 / Java 8 | 已越过 Surefire 拒绝；未删除 `plain` 配置 | test-only `jmh-generator-annprocess-1.27.jar` 与 main Processor 路径不同，尚未进入 JDT |
| Mockito 5.14.2 / Java 17 | 已越过额外 `retryTest` 拒绝，实际到达 Gradle→JDT 模块转换 | `GRADLE_COMPILE_CONFIGURATION_UNMODELED`，仍未编译/运行所选测试 |
| Checkstyle 固定快照 / Java 17 | 已越过 Surefire 额外 classpath/过滤和 Failsafe 发现过滤拒绝 | `FAST_TEST_COMPILER_ARGUMENT_UNSUPPORTED`，4 处 compiler_extension 参数，未进入 JDT |
| Petclinic 新版固定快照 / Java 17 | 启动明确报告只支持 Java 8/11 source/target，不再混成 Plan 缺失 | Java17 源码级别仍不支持 |

Mockito 首次离线尝试因原仓库 URL 对应缓存不全失败；复用了上次报告的本机镜像
仓库 URL 后，在离线模式下到达产品模块转换。没有启动新的下载转发器或降低远端 TLS
校验，执行前关闭了 Build Scan 发布。这个环境调整不是产品修复。

## 先记录，单独讨论

### 启动仍保留旧的单模块路线

Petclinic 旧版真实诊断定位到 `consume_jdt_build_world_plan()` 调用的旧
`_ensure_no_unverified_build_transforms()`。实际触发点是
`org.codehaus.mojo:cobertura-maven-plugin` 的 `clean/check` 隐式 goal。
本地官方插件 2.7 的 descriptor 显示 `check` 默认在 verify，且会 fork 到
Cobertura 的 test lifecycle；不能只根据名称断言它没有编译副作用。

需要讨论的是：单模块启动是否统一复用已有 Maven-native Probe/模块 Build World 路线，
从 JDT 启动路径移除为旧 direct-javac 编写的前置模型，而不是继续逐插件加白名单。
这会涉及启动输入、缓存和生成资源的边界，本轮没有顺带替换整条流程。

### Processor 不能仅靠删除拒绝条件放开

- 原报告 C02 的显式 processor path/name/options 需要准确传入 JDT APT。
- 本轮又实际遇到 Commons Lang 的 test-only JMH Processor。当前一个 JDT 工程共用
  main/test Processor 配置；不能不加区分地取并集，然后声称忠实复现了两套编译。
- 下一轮应一起讨论 main/test 的处理器配置如何表达与执行，不能仅加一个允许的枚举值。

### 非标准测试目录与模块选择（原报告 C04）

Guava 测试位于 `guava-tests/test`。当前冷启动选择模块早于 Probe，还按
`src/test/java` 查找。应以 Maven 导出的实际 test source roots 选择模块，而非按仓库名
或追加一个固定 `test/` 目录特例。本轮未改变 Probe/模块选择时序，也未宣称 Guava 通过。
后续还需处理它实际启用的 Surefire alphabetical 排序及可能的其他编译边界。

### Java17、编译扩展和 build-logic

- Java17 需要升级/验证 JDT candidate 及对应 Worker JDK/Lombok/APT 组合，
  不是安装一个新 JDK 就结束。
- Mockito 配置了 Error Prone，当前拒绝发生在 compiler args/argument provider
  映射处。不能直接丢弃编译扩展；需要进一步明确实际导出信息与 JDT 支持方式。
- Checkstyle 的编译扩展及 ANTLR 生成源码应分别验证；本轮尚未到生成源码编译阶段，
  不把预期的生成器问题写成已观察到的错误。
- TestNG 的 buildSrc/build-logic 支持涉及构建模型获取，不在本轮放开目录检查。

## 回归入口

- `tests/e2e/test_maven_surefire_compatibility.py`：原生 Maven 与 MCP 同一选择器对照；
  显式选类覆盖发现过滤、未绑定 execution 不生效、额外资源 classpath、修改/恢复、缓存重开。
- `tests/e2e/test_maven_modules.py`：额外 Surefire 运行路径在 Reactor 转换后仍可见。
- `tests/e2e/test_gradle_modules.py`：Groovy/Kotlin 均声明额外 retryTest，默认流程正常，
  retryTest 的失败哨兵不执行。
- `tests/e2e/test_stdio_mcp_java.py`：Make disabled 下仍有冷编译、增量启动、reload、
  restart 和跨 MCP 恢复；不复活旧 class 输出复用策略。
- 既有完整 Fast Test 和普通测试继续回归，不能用以上局部通过代替全部项目验收。

本机本轮结果：普通测试 `801 passed, 47 skipped`；对应开关开启的真实 MCP/启动/
Maven、Gradle 模块及新 Surefire 对照 `15 passed`；完整 Fast Test 回归 `6 passed`。
skip 不作为通过。wheel/sdist、compileall、git diff --check 通过。
原报告未修改；独立 clone 中的临时业务改动、启动配置及仓库/Build Scan 配置均已恢复。
