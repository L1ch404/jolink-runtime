# 启动与Fast Test：执行环境选择与原生资源准备

2026-09-29，已完成review，随本轮提交；这组功能尚未发布。

## 为什么要改

公司两个公开项目试用分别暴露：Agent找到了JDK17、Maven和可用镜像，却无法把
它们传给Fast Test；java-diff-utils的测试用磁盘路径读取`target/test-classes`，
原先只把资源源码目录放进classpath，不能满足这个路径。

实现不按项目名分支。Maven/Gradle继续负责配置和资源处理，Java编译继续交给JDT。

## 执行环境

`java_fast_test(action="run")`新增三个可选参数：

- `java_home`：构建准备使用的JDK，以及未另设测试JVM时的默认测试JDK；不修改项目的source/target。
- `maven_home`：已有Maven安装目录，显式指定时不回退到Wrapper/PATH中的另一版本。
- `maven_settings`：已有用户settings.xml，沿用镜像、代理、认证等配置；不修改原文件。

Fast Test参数只作用于本次调用，不改机器环境，不要求重启MCP，也不黏附成下一次请求的隐含参数。
同样的选择进入现有活跃模型/磁盘缓存比较；换选择重新准备，不换继续复用。
项目已有明确的Surefire `jvm`或Gradle测试JavaLauncher仍使用其配置。
这不是完整补齐Maven `jdkToolchain`或所有CLI参数的支持。

本地仓库选择保留IDEA明确配置；否则读所选用户settings、已知Maven安装的全局settings，
最后用`~/.m2/repository`。支持路径里的`${user.home}`和`${env.NAME}`，不会为了注入
Probe把settings指定的仓库强行改回默认目录。

### 项目启动也使用相同的构建选择

`java_application(action="launch", project_path=...)`同样接受`maven_home`、
`maven_settings`，另外用`build_java_home`指定项目准备的JDK。原有`java_home`
仍只选择应用JVM，不混为一个含义，也不因为只换应用JDK就使构建模型失效。

```json
{
  "action": "launch",
  "project_path": "/work/app",
  "main_class": "example.Application",
  "java_home": "/jdks/8",
  "build_java_home": "/jdks/17",
  "maven_home": "/tools/maven",
  "maven_settings": "/work/settings.xml",
  "ready_port": 8080
}
```

环境覆盖、Maven显式选择和仓库解析由启动/Test共用`build_environment.py`与
既有ToolchainResolver，未复制第二套实现。restart保存原request，在配置比较和
需要刷新模型时都重新应用该request的选择；不把显式选择与未覆盖的IDE默认值比较。
只检查已有配置与选择，不增加构建状态机。新的launch需要再次提供所需的非默认选择。

定向用例在`tests/e2e/test_launch_build_environment_mcp.py`，包括Maven/Gradle
单/多模块、构建17＋应用8、HotSwap、强制重启、settings/properties修改后刷新、
stop后缓存命中、新MCP复用、只换应用JDK，以及显式Maven不执行失败Wrapper。
过滤后的HTTP响应同时报告构建JDK和真实应用JDK，不只检查返回标志。

启动参数补齐后的验收：常规测试926项通过；新增启动环境/既有启动JDK/Fast Test
真实MCP回归13组通过，既有settings/Profile选择回归12组通过。已验证只换应用
JDK命中编译缓存，换构建JDK重新Probe，原生过滤资源与HTTP中的Java版本同步变化。
独立安装wheel后，Maven/Gradle两组启动环境场景通过；另外在Petclinic干净副本
完成启动、HTTP200、强制重启换PID和stop后缓存复用，使用的是安装包的MCP服务。
包中只保留`build_environment.py`，已删除未发布的旧`test_environment.py`实现。

测试中发现Gradle过滤变量未声明为task input时，原生Gradle本身也会保留旧资源；
用例补齐`inputs.property`后通过，没有用强制rerun或项目专用分支掩盖任务输入问题。
公司Windows环境仍需实测，这组功能尚未发布。

## 资源准备

`process-test-resources`是生命周期阶段，直接执行会先经过main的compile。
这里没有追加这条命令，也没有调用`mvn test`、Gradle `classes/testClasses`。

### Maven

现有Probe取得Maven计算的执行计划，保留原来显式源码/资源准备goal的顺序，并接入
默认的`resources:resources`和`resources:testResources`；同阶段声明的
`resources:copy-resources`也沿用其实际配置。不强行调用未绑定在准备阶段的执行项。

插件版本、过滤变量、filter文件、includes/excludes、编码、targetPath及输出目录
交给项目自己的Resources Plugin处理，不在Python里重写这些规则。

Resources Plugin输出到本轮临时资源目录，再同步到按模块/目标目录标识的持久资源目录和原生
输出位置，满足`new File("target/test-classes/...")`。Runner读取这份仅含资源的
处理结果，不把混有旧javac class的整个target/classes作为补充代码入口。
确实作为资源复制的`.class`测试数据仍保留；不是按扩展名一律删掉。

资源输出清单是一个本地文件。资源删除后只删除此前由这条资源准备路径生成的文件，
不clean整个target，也不删除原生输出目录中的无关class。失败不更新已成功输出清单，
下次恢复仍能清理已删除资源。没有复制JDT class树或创建新的产物发布机制。

2026-10-08：同步时仅覆盖内容变化的文件，相同资源保留mtime，避免冷launch使已有
Fast Test模型无端失效。比较只在实际Maven准备时发生，使用Java8流式读取，不增加
普通测试请求的哈希扫描。临时资源目录在成功/异常结束时清理。
多个goal先后覆盖同一文件时，仍可能因中间覆盖而改变mtime；此窄边界已实际复现并
保留跟进，详见[资源复用记录](java-compatibility-followup-2026-09.md)。

main/test资源保持独立；测试资源先于main资源。上游模块的资源随已有模块依赖映射使用。

### Gradle

Probe导出任务依赖所需模块的实际`processResources`/`processTestResources`任务，
由Gradle执行CopySpec、filter、rename及生成依赖，不翻译Groovy/Kotlin脚本。
依赖解析在任务依赖计算中进行，不放在全局projectsEvaluated回调中。
导出实际处理输出供Runner/应用使用；配置和资源输入进入现有准备缓存。

Probe使用`--no-daemon`：已有进程Supervisor会清理每次构建产生的子进程，共享
Gradle daemon会在并发调用时产生生命周期冲突。使用独立构建生命周期，不添加daemon
管理服务；无改动与普通Java修改本来就不调用Gradle，因此不影响这两条快路径。

普通Java项目不会因此执行compileJava/compileTestJava/test；若项目自定义资源任务
本身依赖编译或其他任务，其任务图语义不被擅自剪掉，不能保证任意自定义任务图都无编译。

## 快路径与缓存

- 首次、相关资源/过滤文件/配置变化、输出缺失：重新准备。
- 仅Java源码变化：JDT增量，复用资源，不重新启动构建工具。
- 无改动：复用编译与资源结果，直接运行Runner。
- 文件检查复用已有mtime/size与文件集合机制，不新增全仓库hash扫描。
- 资源准备信息不参与JDT编译配置身份，不因仅资源内容变化主动FULL。

Fast Test模型升级到v7，项目启动模型到v4：旧模型重新Probe一次，下载缓存不删除。
编译workspace继续按实际编译配置决定是否复用，不承诺所有旧布局无需一次FULL。

## 回归入口

- `tests/e2e/test_fast_test_environment_resources.py`：真实stdio MCP的显式环境、
  单/多模块、过滤、同名覆盖、二进制、class测试数据、资源删除/输出缺失恢复、
  Java-only、无改动及跨MCP复用；检查Worker实际FULL次数，避免只凭编译数量猜测。
- `tests/e2e/test_fast_test_public_resources.py`：从本机固定公开项目checkout复制
  不含target/build的干净副本，直接运行java-diff-utils/Petclinic，原项目不改动。
- `tests/unit/test_fast_test_environment.py`：参数传递、选择比较、路径和仓库优先级。

## 本轮实际结果（macOS）

- 常规回归919 passed；需显式开启的真实MCP/JVM测试单独执行。
- 环境/资源、Maven生成器、项目JDK、Processor定向真实MCP：13组通过。
  包含资源缺失报错后恢复、删除资源清理、输出删除重建、资源修改不触发实际FULL。
- Gradle8.14：多模块、构建逻辑、引用脚本的既有回归12组通过；
  两个独立MCP同时冷准备Gradle资源的回归另行通过。
- Gradle7.4.2本轮资源/显式环境和多模块回归8组通过。
- 公开项目干净副本回归2组通过，每组含同MCP复用和新MCP重开：
  java-diff-utils `0fd3bd8`的DiffRowGeneratorTest 41项；
  Petclinic `818c413`的VetControllerTests/ClinicServiceTests 14项。
  MCP继承JDK8，Petclinic调用显式选择JDK17与Maven。
- 另行通过Spring `gs-testing-web` `0fa8dd2`（Gradle9.3.1）的HttpRequestTest与
  TestingWebApplicationTest，2项通过。使用临时Gradle用户目录中的镜像配置，
  未修改项目脚本、依赖或源码。不是整个Gradle9生态的兼容性承诺。
- java-diff-utils第二次约0.46秒；Petclinic第二次约4.59秒；Gradle Web第二次约2.90秒。
  这些运行均无bootstrap，所选测试通过；不是企业项目性能保证。
- wheel/sdist构建成功。独立安装wheel后，从site-packages运行真实MCP，
  三组Maven资源与显式环境用例通过。新增测试lint、compileall和diff检查通过。

过程中修正了Gradle9全局回调中解析依赖的时机问题；并发回归暴露共享daemon
生命周期冲突，采用前述`--no-daemon`。旧测试中“不生成任何build目录”改为
“不执行业务编译/JAR任务，但原生资源输出必须正确”，没有删除业务行为断言。
早期失败记录不计入上述通过结果。公司Windows环境尚未执行这一轮新版本。

资源型Gradle上游模块还暴露了不存在默认Java目录时的路径异常：保留可缺省的源码根，
空模块可用，后续新建Java源码仍交给既有增量索引。没有要求用户创建空目录。

隐藏文件读取、远端输入、复杂自定义任务和完整Maven Toolchains仍不能据此宣称覆盖。
本轮没有改U4/U6/U12/U13，也没有放开其他被暂缓的编译边界。
