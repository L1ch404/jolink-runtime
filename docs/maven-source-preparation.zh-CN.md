# Maven 源码准备与生成（2026-09-12）

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
执行计划里自动注入的 packaging 默认绑定当成用户声明。完整回归曾抓到默认资源
复制被误触发，修复后保留原来的“普通项目不产生 Maven classes 输出”断言。
只执行目标及所需上游模块**显式声明**在这些阶段的执行项：

- main：generate-sources、process-sources、generate-resources、process-resources；
- test：另加 generate-test-sources、process-test-sources、generate-test-resources、process-test-resources。

没有显式声明的默认资源复制不因此被启用；compile、test、package、verify等阶段
不执行。这样既能运行生成器，也能取得 build-helper 等目录注册的真实结果。
需要 fork 生命周期的 goal 当前明确报告失败，不偷偷执行或忽略其前置链路。

生成文件写到插件本来配置的目录（通常为项目 target 下），不是去改用户源码。
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

Maven Probe 为 `0.1.0-fasttest23`。模型缓存更新一次，以导入旧缓存没有的准备信息；
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

### Checkstyle 当前结果

固定 SHA：`b54819d1f783a10ca8df13c5987ec0ac28052b3a`。
JDK17构建环境，Java11源码。没有修改源码或POM。

ANTLR生成已执行，Parser/Lexer缺失错误消失，main编译通过；本次还取得了项目
在process-resources注册的真实测试源码根。最终剩6个测试资源样本的包路径错误：
它们放在深层目录，但没有package声明，JDT Java Builder认为声明包与目录不一致。

这是现在实际可见的新兼容问题，不是生成器失败。没有改测试样本、没有压掉错误，
也没有宣称Checkstyle测试已经通过；包路径适配留待单独讨论。

旧Petclinic原样MCP回归22/22通过。原始MCP JSONL只留本机，不写进公共仓库。

收尾验证：普通测试714 passed / 7 skipped；新增3项真实MCP生成用例通过；
既有启动、Maven多模块、Processor、Lombok、Surefire和package-info回归通过，
Fast Test专项6/6通过。compileall、wheel/sdist和diff检查通过。
