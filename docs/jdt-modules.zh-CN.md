# Maven / Gradle 多模块 JDT

启动和 Fast Test 共用模块模型。一个编译 workspace 中只有一个 Worker JVM，
每个参与编译的 Java 模块对应一个 JDT 工程。Maven 和 Gradle 共用这个编译链路。

## 实际流程

1. 根据启动类或测试选择器选定目标模块。
2. 没有缓存时运行对应的 Probe。Maven / Gradle 解析版本和依赖，Probe 只导出目标
   及它需要的模块，不执行 compile、test-compile、classes、testClasses 或 package。
3. 每个模块保留自己的源码目录、编码、Java level、依赖、Processor 和输出。
   本地模块依赖转换成 JDT project reference，外部依赖继续使用已解析的 JAR。
4. 打开持久 workspace：首次 FULL；以后通过源码mtime/size找变化，通知
   JavaBuilder做INCREMENTAL。下游源码是否需要重编由JavaBuilder决定。
5. launch/restart使用当前各模块输出；reload把发生变化的已加载class发给JDWP；
   test启动独立Runner使用测试模块及上游输出。

正常关闭后保留workspace与源码索引，新MCP可以复用。Runtime和Fast Test仍分别拥有
各自的编译workspace，避免测试编译与当前应用的工作输出互相影响。

## 这轮实现

- `ExportReactorWorldMojo`在Maven会话中解析模块依赖。WorkspaceReader将本地
  GAV绑定到模块输出位置；即使没有旧JAR或target，仍可以解析依赖模型。
- `maven_module_world.py`把Probe导出转换成共享模块配置。
- `jdt_modules.py`同步各模块变化源码、保存索引、准备Worker参数、映射Runtime输出。
- `ModuleWorkspace.java`在同一个Eclipse workspace中创建project引用并调用JavaBuilder。
- 普通classpath引用不暴露上游测试代码；依赖test-jar时才包含上游test输出。
- 无关模块的源码不加入workspace。它有编译错误也不会阻止当前目标。
- 没有新增MCP Tool或Action，也没有往`jdwp_adapter.py`加入逻辑。

## 验证证据

真实stdio MCP fixture：三层`base → core → app`加一个无关的坏模块；验证首次启动、
上游常量变化的下游重编、JDWP HotSwap实际返回值、test-jar、main/test可见性、
源码新增/删除、API变化后的编译错误、错误恢复、两次MCP进程之间的workspace复用。
所有模块均未产生Maven target输出。

旧JAR对照：先安装旧shared JAR，然后在上游源码新增方法。Runtime成功执行新方法，
证明编译和启动使用当前JDT输出。

本机业务项目隔离副本：71个上游源码和423个服务源码、34个原有测试源码。
给服务模块补一个本地断言，验证上游已有方法的实际结果。首次约10.75秒；
上游单文件修改使下游断言失败，增量编译35ms；恢复后通过，增量24.8ms。
重新打开持久workspace后测试约2.04秒。

开源项目：[Apache Commons Numbers 1.2](https://github.com/apache/commons-numbers/tree/rel/commons-numbers-1.2)，
commit `77f715769e4039acb4babd9add80b78a06ca7279`。
目标fraction只编译自身和core（包含依赖的core test-jar），6个测试类共156项全部通过，
warm测试约783ms。上游`ArithmeticUtils.gcd`临时修改使UserGuide的4项测试失败，
恢复后9项全部通过。未修改项目POM，未执行正式编译。

开源验证中修复了可选Profile模块导致的整仓拒绝、Surefire可选jvm属性和纯告警
`-Xlint`选项。网络下载时间不计作warm编译性能。

## 尚未覆盖

- Gradle多Project已接入；见 [Gradle多模块流程与实测](gradle-modules.zh-CN.md)。
- Profile中才出现的目标模块仍需进一步打通发现入口；普通模块不再因为仓库存在
  可选Profile模块而整体拒绝，实际Maven会话仍决定激活哪些模块。
- 需要先执行protobuf/OpenAPI等生成任务的项目仍需要已有生成源码或后续专门支持。
- 多模块JPMS、特殊classifier产物、混用不同Lombok版本尚未完成实测。
- 本轮在macOS验证；包含中文和空格路径，未宣称Windows多模块已通过。
