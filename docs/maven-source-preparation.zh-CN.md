# Maven 源码准备与生成（2026-09-12）

2026-09-29更新：默认main/test资源处理已纳入准备阶段，见
[执行环境与资源准备](fast-test-environment-resources.zh-CN.md)。下方生成器实测为历史记录。

2026-10-10更新：准备阶段读取有效POM及插件JAR自带的m2e执行映射，明确ignore的
步骤不运行；未匹配的生成步骤保持原流程。MyBatis默认配置已通过，详见
[U6与实际边界](java-compatibility-followup-2026-09.md)。

## 流程

```text
启动 / Fast Test
  → 构建配置、准备步骤输入未变化：使用缓存
  → 没有缓存或输入变化：同一次 Maven Probe session 执行项目声明的准备 goal
  → 导出更新后的真实 source roots / classpath
  → 打开已有 JDT workspace，编译实际变化的 Java 文件
  → 启动应用或运行测试
```

这不是恢复 `mvn compile` / `mvn test`，也没有按 ANTLR、Checkstyle 仓库名添加特例。
生成工具使用项目的 Maven 构建 JDK；例如 ANTLR4.13.1 的 Tool 需要 Java11，即使
它生成的业务代码仍以 Java8 为目标。Worker 与目标平台继续沿用原来的选择机制。

## 执行哪些步骤

从 Maven 自己计算的执行计划取得 phase、goal、execution 配置，保持 Maven 顺序。
使用 Maven 保留的 POM 声明位置识别执行项（包含继承/合并后的信息），而不是把
执行计划里自动注入的 packaging 默认绑定当成用户声明。现在除默认资源处理外，
继续选择目标及所需上游模块**显式声明**在这些阶段的执行项，再应用执行映射：

- main：generate-sources、process-sources、generate-resources、process-resources；
- test：另加 generate-test-sources、process-test-sources、generate-test-resources、process-test-resources。

默认resources/testResources已经启用，compile、test、package、verify等阶段仍不执行。
这样既能运行生成器/资源处理，也能取得 build-helper 等目录注册的真实结果。
声明ignore的goal记录为`skipped_build_steps`，不执行其fork链路；仍需执行且要求
fork生命周期的goal继续明确报告失败，不把所有fork或未知生成器一律跳过。

映射优先级是有效POM中的m2e配置、插件JAR中的m2e元数据、joLink默认映射。
支持插件坐标、版本范围、goal和参数匹配；明确execute可以覆盖ignore。当前默认映射
只补充OpenRewrite的重写/检查goal，其余使用已有声明或原执行流程。不激活额外profile，
不运行Eclipse configurator，不宣称已复刻m2e的全部映射来源。

生成器按项目配置输出文件，通常位于target下；实际写入范围仍由执行的插件决定，
明确execute的源码整理插件仍可能修改源码，并非所有插件都被限制在target内。
最终 Java 编译仍由 JDT 完成。生成产物如何清理由原插件负责，本轮不额外增加
文件删除、回滚或发布机制。

## 缓存

- 从已解析的 goal 配置中收集实际本地文件/目录路径，包含 XML 属性里的文件路径。
  只比较 mtime/size、文件集合和存在性，不对这些内容反复做 SHA，也不扫描整个仓库。
- 对只涉及新增源码根注册的步骤，记录目录存在性，目录内普通 Java 修改交给 JDT。
- 语法/模板输入或记录的生成产物变化时，重新执行准备步骤并导出模型；普通 Java
  修改不重新跑 Maven，原有无改动复用路径保留。
- 准备步骤的路径/执行记录不参与 JDT 编译配置 identity，避免仅增加准备元数据就
  让已有编译状态退成 FULL。真正的 source roots、classpath或编译选项变化仍照常处理。
- 远端输入、插件内部未暴露在配置中的输入不在本轮自动变化检测范围内；项目根及
  父目录只作为上下文，不递归扫描。生成器若仅用整个项目根隐式寻找输入，目前还
  不能保证自动检测完整，需要单独补充输入表达，不能将其计为已完整支持。

当前Maven Probe为`0.1.0-fasttest28`，Test模型v10、启动模型v7。旧模型更新一次，
以应用执行映射并保存跳过说明；
编译配置未变化时仍可复用现有 JDT workspace。没有新增 MCP action 或独立缓存服务。
Gradle 生成任务不在本轮接入范围。

## 实测

真实 MCP 回归覆盖单模块、上游生成源码的两模块项目，以及 Ant 模板生成：

- 首次生成、JDT编译、测试和应用启动；
- 普通 main/test Java 修改不重新 Probe；
- 语法变更产生预期测试失败，恢复后通过，JDT继续增量而非FULL；
- 生成器语法错误不运行旧测试产物；
- 删除生成目录后重建；
- 新 MCP 无改动复用；
- compile/test阶段放置失败哨兵，确认没有执行业务 Maven 编译/测试。

### Checkstyle 生成步骤结果及后续

2026-09-13后续：以下生成阶段遇到的6项包路径错误，现已由私有JDT源码布局适配
解决；真实MCP编译4079份源码无错误，6个相关测试类132项通过，无变更重开不编译。
原项目源码/POM保持不变。详见[源码布局适配](jdt-source-layout.zh-CN.md)。
下面保留生成阶段首次暴露问题时的记录，不再作为当前未解决项。

固定 SHA：`b54819d1f783a10ca8df13c5987ec0ac28052b3a`。
JDK17构建环境，Java11源码。没有修改源码或POM。

ANTLR生成已执行，Parser/Lexer缺失错误消失，main编译通过；本次还取得了项目
在process-resources注册的真实测试源码根。最终剩6个测试资源样本的包路径错误：
它们放在深层目录，但没有package声明，JDT Java Builder认为声明包与目录不一致。

这是现在实际可见的新兼容问题，不是生成器失败。没有改测试样本、没有压掉错误，
也没有宣称Checkstyle测试已经通过；包路径适配留待单独讨论。

2026-09-13补充定位：原6个文件在javac11及产品同版本ECJ batch下都能编译成默认包
class（补齐原有同包导入依赖）；真实MCP中只移动临时副本、不改内容，就可使其中
一个样本由失败变为通过，移回后再次失败。问题已收敛到JDT工程的目录/包名映射，
不是ANTLR生成失败，也不需要先靠升级JDT判断。详细证据和统一待办见
[最新兼容性汇总](java-compatibility-followup-2026-09.md#最新汇总2026-09-13优先于下方历史记录)。

旧Petclinic原样MCP回归22/22通过。原始MCP JSONL只留本机，不写进公共仓库。

收尾验证：普通测试714 passed / 7 skipped；新增3项真实MCP生成用例通过；
既有启动、Maven多模块、Processor、Lombok、Surefire和package-info回归通过，
Fast Test专项6/6通过。compileall、wheel/sdist和diff检查通过。
