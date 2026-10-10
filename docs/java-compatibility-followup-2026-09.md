# joLink 产品边界与兼容性跟进

## 最新汇总（2026-10-10，优先于下方历史记录）

`eec9429`的0.1.0a8已经发布。源码过滤/WAR入口`1d3c772`与Reactor子模块入口
`9dead95`均已推送main。资源复用修复已提交为`59360e4`，尚未推送；未发布新PyPI版本。
测试进程环境、工作目录和断言配置已提交为`d7fd335`，尚未推送。
本轮U4的检查跳过接线及真实项目回归已完成用户review，纳入本次提交，尚未推送。
同文件多goal覆盖的资源复用边界及其他已暂缓事项没有一并改动。

### U4：检查与编译分开，共用参数映射（2026-10-10，已review）

Maven/Gradle继续由原生Probe求值配置，共用`compiler_profile.py`分类，不按项目名称、
目录或Gradle Provider实现类分支。标准`-Xplugin:ErrorProne`检查模式及其检查选项、
javac执行策略参数不交给ECJ；`-processor`、Processor Path、`-A`和`-parameters`保留。
不能删除整条annotationProcessorPath：同一加载路径上可以同时存在真正的代码生成器。
本轮没有扩成任意javac插件执行器，也没有修改Worker、Java Probe或`jdwp_adapter.py`。

- Fast Test完成结果和精简摘要返回`skipped_compiler_checks: ["Error Prone"]`，提示正式
  构建/CI仍需执行检查；launch的warnings返回同一说明。没有使用这类检查时不增加字段。
- 信息写入现有Build World缓存，重开MCP仍可见；仅改变检查严重级别不会改写JDT class。
  启动模型缓存从v5升为v6，让旧的静默跳过模型重新Probe一次，不删除已有JDT产物。
- `-XepPatch...`自动改源码模式、`@argfile`、未知插件/未知选项不认作“仅检查”。它们
  仍留在公共分类器的unresolved结果中；Fast Test与Gradle入口沿用原有拒绝逻辑。
  Maven Runtime原有路径对部分未映射参数并没有统一拒绝，本轮未另加通用拦截层，
  因此不能把本项写成“所有入口已验证任意编译插件语义”。
- TestNG真实冷启动同时暴露一个简单共用bug：test-only上游工程尚未输出class，
  Python先`resolve(strict=True)`而报FileNotFoundError。改为保留路径，交给既有JDT
  工程依赖解析，不建立假目录、不吞掉真正的缺失库诊断。补充仅测试依赖上游的
  Groovy/Kotlin DSL多模块MCP回归。

实测依据：原生javac运行Error Prone因`DeadException`失败；同一fixture经joLink可
完成Maven/Gradle测试和HTTP启动，并明确说明检查未执行。共存的真实APT、生成代码、
`-A`、反射可见参数名、增量更新、无变化复用、配置重导出和跨MCP缓存重开均通过。
patch模式仍被Fast Test拒绝，源码未被改写。常规回归990 passed；最终新增Maven/Gradle
用例与补强后的Gradle模块回归共5组通过；前一轮含既有Maven模块共9组通过。
独立安装wheel后新增的Maven/Gradle两组再次通过；wheel/sdist、compileall及diff检查通过。
回归入口：[检查跳过MCP](../tests/e2e/test_skipped_compiler_checks.py)、
[Gradle模块MCP](../tests/e2e/test_gradle_modules.py)、
[参数分类](../tests/unit/test_compiler_profile.py)、
[缓存恢复](../tests/unit/test_build_configuration.py)。

原始开源项目复测（JDK17、业务源码与依赖版本未修改）：

| 项目与版本 | 修复前真实MCP | 本轮真实MCP |
|---|---|---|
| TestNG `9665805`，asserts | `GRADLE_COMPILE_CONFIGURATION_UNMODELED` | 所选70项，69通过、1跳过；跨MCP恢复也通过。原生Gradle同为69通过、1跳过 |
| TestNG `9665805`，core | 同上 | 干净副本已进入JDT FULL，6条诊断指向未生成的Kotlin/Groovy输出；原生Gradle所选16项通过。见U14 |
| Mockito `7834859`，core | 同上 | 干净副本JDT FULL有2条build path诊断，根因是未生成`build/generated/resources/inline`。原生生成资源后再跑MCP，所选23项21通过、2跳过，与原生一致；不计为干净项目完整支持。见U13补充 |

测试使用独立副本，离线缓存缺失的依赖先按原坐标补齐。仅调整测试副本的仓库地址，
让Gradle读取本机依赖，禁止Build Scan上传；不改变Processor/Java版本/业务代码以求通过。
Mockito副本缺少Git tag，原生构建派生版本为`0.0.1-SNAPSHOT`，源码仍为上述固定SHA。
修复前对照使用`d7fd335`源码，不能把此前依赖下载失败算作产品拒绝证据。

语义依据：[Error Prone flags](https://errorprone.info/docs/flags)、
[安装方式](https://errorprone.info/docs/installation)、
[patching模式](https://errorprone.info/docs/patching)。支持的是通用的参数语义接线；
目前已确认并实测的javac检查后端是Error Prone，不声称未知插件一概可跳过。

### 测试运行配置：复用同一个Build World与Runner（已提交d7fd335）

修复前真实MCP拒绝Maven的这组Surefire配置。代码链路还存在两个共用问题：Runner
在调用者JVM参数后强行加`-ea`，覆盖关闭断言的意图；Gradle的环境删除在转换时丢失，
随后启动进程又继承父环境。不是JUnit/TestNG本身无法使用这些配置。

本轮没有新增tool/action/参数，没有修改`jdwp_adapter.py`：

- Maven Probe导出`workingDirectory`、`enableAssertions`和私有环境覆盖字典，使用
  Maven自身的goal配置合并及表达式求值。Gradle复用原有Test任务导出，两边都接入
  现有`JavaTestBuildWorld`、缓存和公共Runner。
- 环境覆盖字典用null表示删除，用空字符串表示设置为空。公共ProcessSupervisor
  执行这种差量合并，不修改joLink自身环境，不把null转换为字符串`None`。
- 缓存只保存明确覆盖，不保存整个宿主环境；构建JDK的基础PATH/JAVA_HOME在恢复
  Runner配置时重建。Maven配置文件中的环境引用记录名称及摘要；只改普通继承变量
  不会因此重新Probe。Gradle已有输入跟踪边界未扩成任意脚本依赖分析。
- 断言默认在前、项目JVM参数在后，`enableAssertions=false`映射到`-da`；保留JVM
  原生的类/包级断言参数顺序，不为框架新增分支。
- 缺失工作目录由Runner创建；已有普通文件/不可创建目录返回结构化错误。还对照
  原生Surefire确认：显式目录表达式无法解析时应报配置错误，不能偷偷选择默认目录。
  仅测试配置无效不影响应用的main启动。

运行字段不加入编译身份；配置修改和跨MCP恢复时，回归直接比较JDT class文件mtime，
确认没有重新编译。Test模型缓存升为v9补齐这份运行配置；Probe内部坐标升为fasttest27，
仍为Java8字节码。JDT Worker与Java Test Runner JAR本身未改。

本机验收：常规973 passed；运行配置矩阵6组（Maven单模块/Reactor、Gradle、JUnit4/5/
TestNG、直接/execution/Profile配置）先通过原生构建，再通过MCP。独立安装最终wheel后
7组通过，含目录配置错误及应用启动不受影响的回归。Maven3.3.9+JDK8的5组正向配置和
目录错误用例通过；测试依赖/原生测试provider先准备到对应本地仓库，之后执行离线回归。
既有Maven/Spring/Processor回归16组、main/test与Runner排序12组、Gradle模块/资源/
JDK共享流程12组通过。公开java-diff-utils的核心子模块41项、jgit子模块3项也通过真实
MCP的冷启动、复用与重开。本轮为macOS证据，不替代Windows实际运行。

通用验收包含：覆盖/删除/空值、同名排除与显式赋值、普通继承、环境表达式引用变化、
中文/空格与相对工作目录、false→true断言、argLine的显式`-da`、配置错误后恢复、
运行配置改变但class不变、跨MCP缓存重开。先用原生Maven/Gradle执行同一批独立断言，
再用真实stdio MCP。Gradle原生Test要求目录已存在，原生对照先建目录；MCP侧将其删除，
另验证Runner创建目录的行为。没有用仅检查JSON字段来代替实际测试结果。

回归入口：[运行配置真实MCP](../tests/e2e/test_test_runtime_configuration.py)、
[缓存单测](../tests/unit/test_build_configuration.py)、
[真实子进程环境单测](../tests/unit/test_process_supervisor.py)、
[Gradle模型单测](../tests/unit/test_gradle_test_build_world.py)。
原生语义依据：[Surefire参数](https://maven.apache.org/surefire/maven-surefire-plugin/test-mojo.html)、
[Surefire3.2.5进程配置源码](https://github.com/apache/maven-surefire/blob/surefire-3.2.5/maven-surefire-common/src/main/java/org/apache/maven/plugin/surefire/booterclient/DefaultForkConfiguration.java)。

迟绑定argLine、多个Surefire/Failsafe执行项的完整调度、并行/重试/多fork不在本轮；
不能将本轮三个运行字段的接通表述成“完整复刻Surefire”。U4/U6等后续顺序不变。

### 资源准备：不改写相同输出（本轮修复，仍有一个窄边界）

真实MCP复现：Test已经编译并复用 → 冷启动应用 → 不修改任何源码/资源再跑Test，
却多出约2316ms的Bootstrap。根因不是Java改动，而是`ResourcePreparation`每次清空
持久资源目录，再无条件复制到原生输出；两份模型观察到的mtime因此互相失效。

本轮让Resources Plugin先输出到本轮临时资源目录，再同步到原有持久目录和原生
target输出：内容相同不覆盖，内容不同才复制，已删除资源按现有输出清单清理。
比较使用Java8流式读取，缓冲区固定，不引入额外依赖或把整个资源读进内存。
成功和异常退出都清理本轮临时目录；没有复制JDT class树、增加发布事务或共享模型服务。
后续准备goal仍能读取原生资源路径，原Maven执行顺序不变。

这不是合并launch/Test的Build World：两个入口首次仍分别取得所需模型。修的是一方
准备相同资源后，另一方被迫重复准备。内容比较只在实际Maven准备阶段执行，普通无改动
测试仍使用原stat检查，不新增每次请求的内容哈希或扫描。Python缓存规则未放宽。

本机证据：

- 先Test后launch、先launch后Test，单/多模块4组：连续互相调用与MCP重开后，输出
  字节及mtime保持不变；热Test不再Bootstrap，重新launch复用Probe模型。
- 仅触碰资源源码mtime后，重新准备仍不改写相同输出。真正修改内容（包括同大小替换、
  大于16KiB的二进制末尾变化）能到达原生文件和HTTP响应；空文件、class测试数据保留。
- 资源过滤、删除、输出缺失恢复、准备失败后恢复、ANTLR/模板生成5组通过；过滤文件
  缺失时不清空既有成功资源，本轮临时目录正常清理。不据此宣称任意准备失败都有回滚。
- 更新的资源复用/源码过滤合计11组通过；Gradle资源及显式JDK选择4组通过。
- Maven3.3.9+JDK8的资源复用/资源行为6组通过；Maven3.9.11独立安装wheel后同样6组
  通过。Probe仍为Java8字节码，内部分发坐标升为fasttest26，未改公开版本号/协议。
- 常规回归967项通过，wheel/sdist构建通过。以上是本机macOS，不替代Windows实测。

**剩余实测边界**：`resources`先写A、后续`copy-resources`覆盖同一个文件为B。
下一次准备仍要经历A→B，最终内容相同但mtime改变，仍会重复Probe（约2064ms）。
测试和HTTP结果均为正确的B；这是性能/复用缺口，不是编译失败或错误业务输出。
不能直接删掉中间同步，因为后续goal可能读取原生输出。暂未增加中间文件版本管理，
已保留仅针对这一已确认现象的xfail；其他错误仍正常报失败。

回归入口：[资源复用真实MCP](../tests/e2e/test_maven_resource_reuse.py)、
[资源行为/异常清理](../tests/e2e/test_fast_test_environment_resources.py)。旧源码过滤
回归中“先额外跑一次Test预热”的绕行已改为直接断言冷launch不会使Test重复Bootstrap。

### 子模块入口：区分Maven构建根与目标模块（本轮修复）

原复现是`project_path=reactor/app`：joLink以`app/pom.xml`开始整个Maven session，
同级library未进入reactor，导致缺少本地上游依赖或使用仓库里的旧JAR。根目录入口能通过。
启动有同类入口问题，本轮与Fast Test共用修复，不只改测试路径。

现在的流程：沿祖先POM中的模块声明找到聚合根 → 从根选择Wrapper并运行原Probe →
仍将用户选中的app作为target → 复用Probe已有的依赖解析及JDT多工程编译。
不执行Maven compile/install，不把所有模块都编译，也不修改用户POM或源码。

- 共用一个`maven_build_root()`；不会因为存在上层POM或继承parent，就把无关工程当成
  聚合根。支持字面模块路径、跳层目录、嵌套聚合和Profile里的模块声明。
- Profile是否激活、依赖关系与编译模型仍由Maven处理。这里不自动启用Profile，
  不实现属性表达式求值；依赖动态module路径或不在祖先目录的聚合器仍未覆盖。
- `project_path`、目标模块、Runner/应用工作目录的原语义不变，只调整Probe构建入口。
  启动仍使用调用者的主类/启动配置，不新增MCP参数，也不改JDWP adapter或Worker。
- 根POM及根`.mvn/maven.config`、`jvm.config`、`extensions.xml`进入原缓存输入。
  子模块入口能感知根配置修改；旧缓存缺少这些记录时重新Probe。没有新增缓存格式、
  清空编译目录或加入源码/JAR全量扫描。

实际回归覆盖根/子模块入口、三模块传递依赖、上游test-jar、错误恢复、增删源码、
restart实际输出、MCP重开、嵌套聚合、根Wrapper、Profile激活与根配置A→B刷新。
无关模块故意放置坏源码，确认没有顺便编译它；旧WAR子模块预期失败已转为正常验收，
不再保留xfail。测试均使用新的真实stdio MCP进程。

另从官方源码重新下载java-diff-utils 4.12（`0fd3bd8e061eed09dbb937c8ab9ba0969ba12264`）：
直接选择核心子模块，所选41项通过；直接选择依赖该模块的jgit子模块，所选3项通过，
均验证冷编译、无改动复用和MCP重开。jgit同样3项在原生Maven reactor中通过。
MCP用例使用不含target的源码副本，未改业务代码/依赖，没有先install上游；
本机只通过显式临时settings选择依赖镜像。

本机常规回归967 passed，158项需显式启用的集成测试未混入这个数值。wheel/sdist
构建成功；独立安装wheel后，嵌套/Profile根配置与WAR子模块的4组真实MCP回归通过，
包括此前失败的未安装上游依赖路径。单元测试还覆盖未聚合的继承parent、无关上层POM、
根配置新增/修改及旧缓存刷新。Python编译、新增测试lint与diff检查通过。
最终源码环境回归：Maven模块/源码过滤/根配置/配置刷新15组通过，Gradle模块和
Maven/Gradle应用JDK共享流程6组通过；没有跳过此前的子模块失败来获得绿色结果。

回归入口：[发现与配置单测](../tests/unit/test_maven_reactor.py)、
[实际缓存加载单测](../tests/unit/test_build_configuration.py)、
[根配置/嵌套真实MCP](../tests/e2e/test_maven_child_entry.py)、
[三模块真实MCP](../tests/e2e/test_maven_modules.py)、
[公开项目真实MCP](../tests/e2e/test_fast_test_public_resources.py)。

下节的源码过滤/WAR记录属于上一轮；其中子模块入口的xfail是修复前证据，不代表
当前仍未修复。资源时间戳的普通场景已在2026-10-08继续修复；同目标多goal覆盖的
剩余复用缺口见上节。

### m2e对齐第一批：源码过滤与WAR测试

- Maven Probe已经导出Maven解析后的effective model。本轮从现有main/test scoped
  model分别读取`includes/excludes`和`testIncludes/testExcludes`，保留父POM、profile、
  execution及属性插值后的值。不把main规则误用到test，也不另写一套POM发现机制。
- 普通Ant路径规则（`*`、`?`、`**`）按**各原始source root下的相对路径**匹配，再做
  joLink已有的package目录映射。FULL、增量扫描、显式source hint、持久化恢复共用一个
  选择器；被排除文件不读取内容、不进入私有源码镜像。新增/删除规则走既有配置失效流程。
- 无规则的模块不增加空规则字段，保持原来的编译身份。Test模型缓存升为v8、启动缓存
  升为v5，旧模型首次重新Probe以补齐字段；不删除JDT缓存或下载缓存。实测普通无过滤
  项目的旧schema重新Probe后，仍使用原class文件，没有重新编译；另将a8真实jar Reactor
  缓存交给本轮代码，原有3个class的路径与mtime也均保持不变。
- Probe选测试模块时不再只接受`jar`。WAR里的JUnit测试使用相同的JDT和Runner路径；
  并非新增WAR部署功能，不负责打WAR包、部署外部Tomcat或执行容器部署插件。
  本轮未宣称所有自定义packaging、多个不同compile execution或Maven特殊regex规则均已验证。

对照依据：[m2e源码固定版本](https://github.com/eclipse-m2e/m2e-core/blob/66500f4ff361757de9f244a03ed76c73284cb6d2/org.eclipse.m2e.jdt/src/org/eclipse/m2e/jdt/internal/AbstractJavaProjectConfigurator.java)。
m2e给JDT source entry配置main/test各自的inclusion/exclusion；joLink因为有package布局
归一化，在原始目录筛选后再映射到自己的JDT工程，并未照搬IDE/UI或新增Worker协议。

本轮macOS证据：

- 原生Maven能通过、旧joLink会编译到被排除坏文件的最小复现，现在通过真实stdio MCP。
- 4组单模块/Reactor、jar/WAR、公共配置/execution/profile场景，覆盖父POM继承、
  main/test独立、增删文件、无效编码的排除文件、显式hint、规则切换、启动HTTP、restart
  新内容和跨MCP重开；另测两个WAR模块同名测试的歧义与独立模块显式选择。
- [Spring Framework Petclinic固定版本](https://github.com/spring-petclinic/spring-framework-petclinic/tree/09351b3ee0bd5aec2d480c0280e84700978f56d3)
  是真实WAR项目，Java17/Spring7/JUnit6。原生Maven全部75项通过；joLink从不含target的
  源码副本FULL后，同样75项通过，并验证无改动复用与MCP重开。覆盖MVC、JDBC/JPA、
  Spring Data及H2，不修改项目源码/POM/依赖；本机使用显式临时Maven镜像配置。
- 既有Maven模块/源码生成/布局/Processor回归20组通过；Gradle模块/构建逻辑、资源与
  JDK选择等共享流程16组通过。新回归已沉淀，不只保留一次手工日志。
- 常规测试948 passed，153项需显式开启的集成用例未计入；Python编译、新增测试lint
  及diff检查通过。wheel/sdist构建成功，独立安装wheel后，公开WAR的75项测试及
  冷/热/跨MCP复用再次通过；未用工作区的editable安装代替发布包验证。
  独立安装后的本轮过滤/模块选择/schema刷新用例6 passed、1 xfailed，后者就是下述
  尚未修复的Reactor子模块入口问题，不计作通过。

入口：[过滤与模块选择真实MCP](../tests/e2e/test_maven_source_filters.py)、
[匹配与恢复单测](../tests/unit/test_source_selection.py)、
[公开项目真实MCP](../tests/e2e/test_fast_test_public_resources.py)。
公开WAR用例通过`JOLINK_TEST_WAR_PETCLINIC`指定本地checkout；不把第三方源码放入仓库。

上一轮额外发现（子模块入口已在2026-10-07后续修复）：

1. **显式Reactor子模块入口遗漏上游**：从聚合根选择测试能够导出上游，但直接把
   `project_path`指到子模块时，Maven session可能只有该子模块；未安装到本地仓库的
   sibling依赖随即解析失败。保留真实MCP预期失败用例，仅对已确认的缺少上游依赖
   错误标记xfail，其他错误仍失败。后续应分开“聚合build root”和“目标module”，
   不是要求用户先`mvn install`，也不是WAR编译器问题。另用`eec9429`原代码/Probe24
   的jar Reactor复现同样现象：根入口通过，子模块入口缺少`example:lib:jar:1`；确认
   不是本轮移除jar限制引入的回归。
2. **启动/Test共享资源准备的mtime**：冷启动重新准备资源后，已缓存的Fast Test可能
   因处理后资源时间戳变化而再次Probe。实测预热到同一资源状态后，仅改排除Java文件
   不再Probe/编译。此为已有资源缓存协调问题，不把它归因于源码过滤。

测试运行配置中的环境变量、工作目录和assertions已在2026-10-09接入。后续m2e对齐
仍待讨论：延迟argLine及更多测试运行策略、
生命周期准备策略（与U4/U6相交）、Toolchains/自动JDK选择、JPMS/preview/multi-release及
多个compile execution、插件增量输入，以及独立工程之间的依赖与显式刷新。没有将这些
项目作为本轮新增拦截，也不把m2e的IDE功能列表全部当成joLink必须实现的待办。

### 0.1.0a8基线（2026-09-29）

此前 `main` 的 `4dc7a70` 已包含U11与U10，且已推送。本轮提交补齐
启动/Fast Test的执行环境选择及原生资源准备，已完成review，详见下节。
没有重新跑完整开源项目矩阵。
“已提交”不自动等同于用户已经安装到该修复或公司原项目已经完成验收。

继续沿用原 U 编号，区分：仍阻断使用、用户暂缓的兼容项、已实现但待专项回归、
尚未取证的反馈。常见项目被拒绝仍是产品缺口，不能以“安全拒绝”代替支持。
U11和本轮选定的U10分别记录在下方，不改变其他事项的暂缓决定。

### 本轮：启动/Fast Test执行环境与测试资源（已修复）

公司Agent已经主动调用joLink，但Petclinic被JDK/Maven/镜像选择挡住；
java-diff-utils要先手工准备target/test-classes，才能通过读取磁盘资源的测试。
这两项属于产品接线缺口，不是继续加重Agent提示词，也不是要求业务项目改测试。

本轮新增`java_home`、`maven_home`、`maven_settings`，选择贯穿执行和现有缓存。
启动入口也接入`maven_home`、`maven_settings`与独立的`build_java_home`，restart继承；
启动的`java_home`仍仅选择应用JVM，不改变原语义。
Maven/Gradle执行实际资源处理，保留过滤、目录映射及原生文件路径，Runner读取
处理后的资源；Java-only继续JDT增量，不重新准备。实现、回归入口与剩余边界见
[执行环境与资源准备](fast-test-environment-resources.zh-CN.md)。

目前公开项目证据：原版java-diff-utils 4.12所选41项通过，原版Petclinic
`818c413`所选14项通过，Spring gs-testing-web的Gradle9.3.1所选2项通过。
均通过真实stdio MCP；本机中央仓库403，使用临时镜像配置后完成，没有升级项目依赖。
不是公司修改后的51/17项测试，也不代表完整项目所有任务均已验证。

### 当前待办

| 编号 | 当前状态 / 问题 | 已有证据与边界 | 后续落点 |
|---|---|---|---|
| U2 | 待定位：Guava 泛型错误 | JDT3.46在原选择器上仍有14条诊断（旧版本13条）；未拆清源码兼容差异与编译输入问题。这不是14个独立根因 | 沿固定Guava样本缩小表达式，先对齐原生构建与JDT输入，再决定修输入映射还是处理编译器差异 |
| U4 | 已接线、已review：Error Prone检查跳过 | Maven/Gradle共用参数分类，保留真正APT，启动/测试明确说明检查未执行；TestNG asserts的70项所选测试通过。Mockito/TestNG core已越过参数拦截，后续问题见U13/U14 | 不执行javac检查本身；正式构建/CI继续负责。其他未知插件未一概忽略 |
| U5 | 用户暂缓：Checkstyle注解位置差异 | 新ECJ的5条错误已在独立ECJ、原生Eclipse Java Builder复现，涉及泛型方法类型参数后的注解归属；不是已修复的源码目录映射问题 | 保留最小样例与原诊断，详见[升级记录](jdt-346-upgrade.zh-CN.md)；本轮不改源码、不屏蔽错误 |
| U6 | 待讨论：MyBatis准备步骤 | 默认format profile的OpenRewrite fork被拒；关闭format后license处理等待。在副本中关闭两者后，main8/test17及87项所选测试通过 | 区分必要生成步骤与格式化/license检查，接入合理的快速准备流程后，用原配置验证；不能把关闭profile的结果算成原样通过 |
| U8 | 用户暂缓：Lombok1.18.20兼容 | `@Builder(toBuilder=true)`等部分用法依赖旧ECJ内部接口；不代表所有Lombok注解都失败 | 后续单独研究旧版本支持；不自动升级业务依赖、不增加整版本拦截 |
| U9 | 功能已有替代路径，优化暂缓：未加载类的HotSwap | `05a8bf0`后公开restart会增量编译，并在`CLASS_NOT_LOADED`时使用本轮产物自动重启；尚不支持已加载类热更、未加载类留待正常加载 | 待讨论的是减少不必要重启，不再记成“restart无法应用变更”。见[restart流程](project-restart.zh-CN.md) |
| U12 | 用户暂缓：Probe执行期间改配置的基线窗口 | 生产方法受控复现：Probe返回旧编码模型，返回前POM已修改，随后`_stabilize()`给旧模型记上新文件快照。尚未完成真实Maven并发编辑窗口复现 | 先保留问题；临时避免在Probe运行期间修改构建配置。现有“JDT编译前固定快照”不等于已解决此窗口 |
| U13 | 用户暂缓：Gradle编译后处理未接入 | 依赖`compileJava.doLast`修改class的效果未进入JDT产物；Gradle8.10/8.14两组验证属于同一边界。应用ready不能证明后处理已执行 | 区分普通回调与产物变换，再讨论通用接入；不见到doLast就一律拒绝，也不删除仍失败的验收断言 |
| U14 | 新实测：Gradle Java/Kotlin/Groovy混合源码 | TestNG core导出的classpath包含Kotlin/Groovy输出，干净副本没有这些产物，JDT FULL报告缺失库；项目确有其他语言源码，不是只有空目录 | 后续讨论如何执行原生语言任务并跟踪其产物；不靠建立空目录、丢classpath或只选Java测试宣称支持 |

U11、U10均已提交，不再作为待实现项。U4已完成review并纳入本次提交。
后续可讨论U6/U13的构建步骤、U14的语言任务、U2的编译兼容性。
必要源码生成（如ANTLR）与可留给正式构建的质量检查不能混为一类。用户已明确暂缓的
事项继续保留，不因这次更新文档就自动开展实现。

U13本轮补充：Mockito的`copyMockMethodDispatcher`依赖`compileJava`，把编译后的
`MockMethodDispatcher.class`复制/重命名为`.raw`，写入额外SourceSet输出目录。
当前Probe准备`processResources`，不会执行这条编译后资源链；新副本因此缺少目录，
JDT test工程报告build path错误。不能简单把所有额外输出当普通资源忽略；这个文件是
Mockito inline mock的实际运行输入。本轮只定位记录，不扩展编译后任务调度。

### U10：本地Gradle引用脚本跟踪（2026-09-28，已修复）

修复前通过真实MCP再次复现：根build文件、Java源码和测试均不修改，只改嵌套引用
脚本的资源目录、测试属性与`-parameters`设置，Fast Test仍使用旧模型，独立预期为B
的测试失败。原因是这些实际应用的脚本没有进入Probe导出的配置文件清单。

本轮在Gradle Probe中监听`ApplyScriptPluginBuildOperationType`事件，读取Gradle已经
解析出的本地文件路径，合并到现有`configurationFiles`。不解析`apply from`字符串，
不靠扩展名或全仓库扫描找脚本，不增加MCP参数。使用的是Gradle内部构建事件接口，
已实际验证7.4.2、8.10、8.14；后续Gradle升级要继续跑这些回归，不能据此保证所有版本。
对应[上游事件实现](https://github.com/gradle/gradle/blob/v8.14.0/subprojects/core/src/main/java/org/gradle/configuration/BuildOperationScriptPlugin.java)。

- Groovy/Kotlin DSL、变量拼接、嵌套引用、项目外文件、空脚本和非标准扩展名均有回归。
  空Groovy脚本不会产生普通脚本执行监听事件，实测确认选用的“应用脚本”事件能记录它。
- 清单随现有Build World保存，launch、运行中restart和Fast Test共用原配置比较。
  配置不变不启动Gradle；也不新增源码、class或依赖JAR扫描。
- 新增/切换脚本引用后重新记录清单；旧引用移除后不继续影响缓存。测试中的未应用脚本
  位于既有固定配置目录之外，不被新增跟踪机制纳入；原有固定配置目录跟踪保持不变。
- 排除joLink自己的临时init脚本，构建结束移除监听器；成功、脚本删除失败及恢复路径
  均经过真实Gradle，避免临时文件或上一轮监听数据导致反复重新Probe。
- `buildSrc`和included build的构建逻辑脚本也有行为验证。这里不代表已经支持
  composite业务依赖产物映射，也不代表U13的编译后处理已经接入。

旧Gradle模型已跟踪Probe lock文件；分发产物更新后会按原机制重新Probe，补齐实际脚本
清单。不删除缓存目录、不升级缓存格式；JDT workspace是否需要FULL仍由原身份决定。

本轮macOS验收：

- 新增5类真实MCP用例，在Gradle7.4.2、8.10、8.14分别通过，共15组，包含HTTP内容、
  Java反射编译参数、Fast Test结果、文件删除/恢复、引用切换、跨MCP重开与不变复用。
- 既有Gradle构建逻辑和多模块回归5组通过；另外2组构建逻辑引用外部脚本的用例通过。
- 单元测试覆盖启动/Test缓存的内容修改、同mtime/size修改、删除、恢复和活跃模型基线；
  常规回归912 passed，132项需显式开启的MCP/JVM用例未混入此结果。
- wheel/sdist构建成功；独立安装wheel后，真实MCP的嵌套脚本修改/重启/Test用例通过，
  确认实际使用的是site-packages内的新版Probe产物，而非源码目录。
- 当前客户端连接的joLink MCP也完成A→B与无改动复用：仅修改被引用脚本后，测试
  读到新的资源/系统属性；无改动再跑约150ms，freshness约1.4ms。仅为本机小样例，
  不作为企业项目性能保证。新增/修改测试lint、Python编译及diff检查通过。

回归文件：[真实MCP脚本用例](../tests/e2e/test_gradle_script_inputs.py)、
[构建逻辑用例](../tests/e2e/test_gradle_build_logic.py)、
[缓存单测](../tests/unit/test_gradle_script_input_cache.py)。

仍未覆盖：远端URL内容变化、脚本通过普通文件读取/目录条件间接依赖的其他输入，
以及首次未执行某个`apply`分支、之后仅新增条件文件的情况。它们继续属于“隐藏/远端
配置输入”边界；本轮没有把任意Gradle配置依赖宣称为完整跟踪。U12的Probe中途编辑
窗口、U13的`compileJava.doLast`均未处理，也未改变它们的原有验收结论。

### U11：已修复（2026-09-27）

`3102b49`已经修了应用JDK、启动参数和主类/模块变化。本轮补齐的是构建选择的
缓存与运行路径，不重新实现启动流程。

修复前用真实stdio MCP再次确认了两层问题：

1. settings-A/B、POM和Java源码内容全部不变，只改IDEA选项：启动仍返回A，
   Fast Test仍使用A的系统属性/资源，独立预期为B的测试失败。
2. 补上Probe缓存比较后，只切Profile，资源已经变B，但新的`-parameters`编译设置
   没有使旧JDT workspace失效。HTTP反射返回`B|false`，预期为`B|true`。

本轮实现：

- 启动与Fast Test共用`build_preferences_key()`，比较Maven/settings/local repository/
  Profile选择和实际影响构建的SDK引用、对应目录。忽略应用启动JRE/参数、无关SDK及警告。
  Gradle不因无关Maven settings/Profile变化而重新准备。
- 如果Maven/Gradle构建本身选用了IDEA项目SDK，改变该SDK也是构建变化，应重新Probe；
  单纯修改`java_home`、启动配置的JRE或启动参数不因此使编译缓存失效。
- 启动缓存命中前比较选择；重新Probe时，把同一构建选择纳入已有workspace身份，避免
  新Profile模型继续使用旧编译选项。配置不变仍走原缓存，不增加源码/class扫描。
- Fast Test把Probe所用选择随模型快照保存；活跃模型与磁盘模型分别比较自己的基线，
  不借另一个会话写入的新磁盘记录为旧Worker背书。
- 活跃应用的restart也复用现有配置变化检查，构建选择变了就走已有重新启动/Probe路径；
  不继续在旧模型上编译。`jdwp_adapter.py`只多传一份快照字段，未增加构建编排逻辑。

缓存升级：启动缓存继续读取已有选择字段，不全量作废。旧Fast Test v5没有保存这份
基线，升为v6后首次重新Probe；JDT workspace和下载缓存不删除，是否重新编译由现有
workspace身份/持久状态决定，不承诺任意模型变化都无需FULL。

回归入口：[单元测试](../tests/unit/test_build_preferences.py)、
[settings/Profile真实MCP](../tests/e2e/test_build_preferences_mcp.py)、
[应用JDK回归](../tests/e2e/test_project_launch_jdk_mcp.py)。已覆盖单模块/Reactor、
launch/restart/Fast Test、A→B→A、跨MCP切换与不变重开，以及真实HTTP/测试结果和
编译参数反射；不是仅断言`probe_cache_reused`标志。

本轮提交前本机验收（macOS）：

- 常规测试899 passed；125项需显式开启的MCP/JVM测试未混入此结果。
- 最终定向真实MCP：12组U11场景＋3组既有Maven/Gradle应用JDK场景，15 passed。
  另有既有settings内容刷新、Profile模块父POM、活跃模型隔离回归通过。
- 20轮Fast Test开发循环通过，包含增量、错误恢复、混合JUnit、取消及进程隔离。
- wheel/sdist构建成功；独立安装wheel后的实际MCP启动、HotSwap、restart、Fast Test、
  调试及清理验证通过。新增测试文件lint、Python编译检查及diff检查通过。

U11提交当时未包含U10，后续的U10修复见上节。U12的Probe中途编辑窗口、任意
环境变量或远端配置输入不在U11范围内。Gradle既有启动/JDK/Fast Test回归通过；
公司原项目仍待实测。

### 最近已修复：不再作为待实现项

| 事项 | 提交 / 实际变化 | 验收范围与保留边界 |
|---|---|---|
| 配置缓存Review第1/2/3/6项 | `068ec6d`：模块/父POM输入、Fast Test settings来源、活跃模型独立快照、同步等待后的状态指导 | 已有真实MCP及生产接线单测；不是U10、U11剩余部分或U12全部解决 |
| 应用JDK与无IDEA启动 | `3102b49`：当前应用JDK及参数不被旧缓存覆盖；默认按项目目标平台选应用JDK，显式选择仍可覆盖 | Maven/Gradle相关单测、真实MCP；仅改应用JDK/参数仍可复用编译产物。U11剩余构建选择由上节本轮改动修复 |
| restart接口与未就绪提示 | `607ba14`：移除`java_application`的`source_files`，自动扫描变化；保留同步等待最终结果的readiness指导 | 未配置`ready_port`仍可启动，但`unverified`不等于HTTP服务已就绪；没有因此解决所有“请求返回旧内容”的反馈 |
| Fast Test直接返回编译错误 | `7d97321`：`run`返回前已发生编译错误时，直接带诊断；状态查询保持精简 | 超过同步等待后才完成的后台错误仍通过`result(test_run_id)`读取；不是让单次MCP无限等待 |
| Spring/JUnit框架误判 | `467e182`：不把`@SpringBootTest`包含的Jupiter辅助扩展算成JUnit5测试入口；使用`@Testable`及嵌套测试容器识别 | 真实MCP的JUnit4 Spring随机端口/MockBean/HTTP用例在JDK8、17通过；类/方法选择、缓存重开、混合JUnit以及独立安装wheel通过。公司原项目仍待回归 |

Spring回归已沉淀在[test_fast_test_spring.py](../tests/e2e/test_fast_test_spring.py)。
当时865项常规测试与3组既有JUnit4/5、TestNG完整流程通过；其后的规则文档提交
`36cd3c6`对应870项常规测试通过。这些是已有验收记录，不是本次重新执行的结果，
也不覆盖下表中所有高级测试配置。

### 待取证的现场反馈：restart之后仍看到旧内容

用户曾报告修改少量业务代码后，restart返回`applied=true`、`apply_method=restart`、
`startup_state=unverified`，之后请求仍见旧内容。当时没有带回完整MCP日志及端口PID
证据；请求目标已确认是本机`127.0.0.1`。

本机已有对照中，旧受管进程退出、新JVM从JDT输出运行、就绪后HTTP读取新内容均正常。
目前只能确认未就绪指导曾被丢失，相关提示已修；**不能把“旧JVM仍在服务”或“JDT产物
没落盘”写成已确认根因，也不能把它们写成已修复**。此项不与U9的未加载类优化合并。
无readiness配置时几秒的启动观察值不能证明需要更长初始化的业务服务已就绪。

若再出现，优先带回同一轮请求/返回、joLink版本、MCP/应用日志和时间点对应的端口PID，
再判断停旧进程、运行classpath、编译产物或验证时机哪一段出了问题；无需提供公司源码。

### 历史修复记录：2026-09-16 Review第1/2/3/6项（已提交068ec6d）

以下记录描述当时的实现与验收；U11此后的修复以上方最新汇总为准。

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

该轮验收记录（macOS，已随068ec6d提交；以下数字为当时结果）：

- 新增18项单元用例/参数组合，重点经过实际生产转换和发布方法，而非由测试提前补齐输入。
  普通回归867 passed / 13 skipped，另外89项需显式开启的MCP/JVM测试未混入普通结果。
- 4组定向MCP通过：Profile父POM、settings刷新与重开、两个活跃MCP的模型隔离、直接启动最终提示。
  双MCP测试额外断言同一磁盘文件先被B、再被A覆盖，A旧Worker在比较前仍ready，避免场景未成立也绿。
- 7组已有MCP回归通过：Maven单/多模块和Gradle配置缓存、Gradle Kotlin多模块、模板生成、
  ANTLR单模块/Reactor。ANTLR初次因缺少JDK11测试变量跳过，补齐本机JDK11后两项实际通过。
- 定向lint、compileall、git diff --check、离线wheel/sdist构建通过。

该轮U11/U12只记录，U10亦未处理；U11后来已有部分修复，见最新汇总。
该轮没有修改jdwp_adapter、Worker/JDK交付或增加新工具。
旧Fast Test v4缓存首次会重新Probe以补齐配置来源，编译状态是否复用仍走原有规则；不清空
JDK/JDT下载缓存。本轮不宣称完整Windows验证，也不将同一Eclipse workspace的任意并发使用
算作已覆盖。

### 历史修复记录：构建配置缓存感知（2026-09-15，已提交eea26fe）

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

### Gradle compileJava.doLast：额外回归发现（2026-09-17）

`tests/e2e/test_gradle_runtime_product.py` 的 Gradle 8.14 / Java11 路径已通过
启动、缓存重开、增量、HotSwap、结构变化自动重启、资源读取；末尾附加用例
给 `compileJava` 增加 `doLast`，预期 `GRADLE_BYTECODE_TRANSFORM_UNMODELED`，
实际却到达 `runtime_active/ready`，旧脚本因此等待失败状态超时。

另外提取未修改的 `e5bad1d` 源码，以独立真实MCP和Gradle 8.14重新执行同类
`compileJava.doLast { target << (byte)0 }` 项目，仍为 `runtime_active/ready`。
因此不是本轮restart造成的回归。本轮没有改Gradle Probe及模型转换，也没有为了
消除失败而删除该断言或新增白名单。仍需定位原生Probe实际导出的任务action信息，
并区分“快速路径不执行正式编译任务”与“项目依赖的字节码变换已经被忠实执行”。
它与U4的构建步骤取舍相关，不能把这轮启动成功写成任意doLast语义受支持。

2026-09-21 主分支整理前的完整 E2E 又在 Gradle 8.10 / target 8 和
Gradle 8.14 / target 11 两组复现相同问题：前面的启动、增量、HotSwap、
结构变化重启等步骤通过，末尾 `doLast` 用例等待预期拒绝而超时。
本轮仅迁移测试辅助代码，Probe 的 Java 源码及分发 JAR 均保持原样；
仍保留失败断言，没有改成跳过或放宽断言来获得全绿。

#### 产品边界确认（2026-09-21，用户决定先记录）

- **分类**：尚未支持的Gradle编译后处理能力，不是本轮目录整理导致的编译或启动回归。
- **实际差别**：Gradle正式执行`compileJava`时会执行其`doLast`动作；joLink由JDT
  编译并直接使用输出，没有执行这段动作，也没有按旧测试约定返回未建模错误。
- **失败含义**：测试一直等待“应拒绝启动”的结果，而应用已经ready，因此测试超时。
  Gradle 8.10和8.14的两项失败是同一边界的两组验证，不是两个独立故障。
- **影响范围**：真正依赖编译后字节码增强等处理的项目，快速路径产物可能与正式构建
  不同；不能用普通启动、增量、HotSwap通过来宣称这些处理已受支持。
- **后续原则**：`doLast`只是通用回调，也可能只打印日志，不能见到它就一律拒绝。
  后续结合实际项目区分是否影响产物，再讨论通用接入方式，不按项目或插件名字特判。
- **本次决定**：仅登记为产品边界推进项；不改实现、不增加拒绝策略，也不删除或放宽
  现有失败断言来获得全绿。

### Reload待办：未加载类（2026-09-13，用户决定先记录、不修改）

2026-09-17 更新：公开reload已合入restart，默认先增量编译并尝试HotSwap；
遇到未加载类会直接用本轮产物重启，不再要求LLM另发stop/launch。以下是原问题
及仍暂缓的“部分HotSwap”优化背景，不代表新入口还会停在相同拒绝上。

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
| 源码变化发现的工程取舍 | 外部操作保持文件大小和mtime不变 | 当前元数据扫描可能不发现该编辑，见[JDT-first启动](jdt-first-launch.zh-CN.md)；不是已经复现的日常git切分支必然失效，也不在本轮恢复每次全量哈希 |

### 已解决，不再作为待办

- 旧Petclinic启动中的direct-javac遗留路径；已有启动、HTTP、reload/restart和测试证据。
- Maven真实源码根/模块定位、Gradle build-logic加载与ArgumentProvider求值。
- main/test各自的语言级别、依赖、Processor与参数表达；Java17语言能力也已在新引擎实测。
  main8/test17、main11/test21的Maven/Gradle回归，以及新版Petclinic所选测试、启动和HTTP
  已通过；不代表所有更高Java版本或所有JDK/Processor组合都已验证。
- Processor加载路径、显式名称、-A参数、Lombok/MapStruct初始化，以及无值flag适配。
- Surefire字母排序、MyBatis的useIncrementalCompilation拦截、Checkstyle的-Xpkginfo:always。
- Checkstyle所需ANTLR生成及Maven源码准备已随`24df38e`接入；U1包路径适配由`05ea849`
  实现。旧candidate下真实MCP完成4079份源码FULL、0 errors和132项所选测试；无改动
  重开不编译，修改一个默认包样本后增量编译1个文件、15项相关测试通过。详见
  [源码布局适配](jdt-source-layout.zh-CN.md)。后来JDT3.46的U5注解差异仍单独保留，
  不能把旧candidate的通过结果当作当前Checkstyle全量编译已通过。
- U3：新引擎已支持Java17编译；旧JDT3.25的限制留在下方历史记录。Java25/26等未实测组合不宣称已验证。
- U7：APT旧首选项节点导致偶发漏跑/假成功，已随`a168058`修复并回归，不再列为未解决。
- 已复现的HotSwap确认5秒超时已由`45beb4f`改为独立30秒等待；真正失联或超过30秒仍可unknown，不能混为同一个已修问题。
- 配置缓存的生产接线修复、当前应用JDK/参数刷新、readiness提示、Fast Test编译错误
  直接返回和Spring/JUnit误判，分别见上方“最近已修复”表；U11剩余构建选择已在本轮
  修复并提交，见上方专项记录。

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
