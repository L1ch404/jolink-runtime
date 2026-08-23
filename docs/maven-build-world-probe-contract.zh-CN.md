# Maven 原生 Build World Probe 契约

状态：已在 `experiment/jdt-incremental-worker` 分支完成实验性 Spike，并提供显式
`Probe 私有报告 -> Phase 2A JDT FULL` 混合入口。它不是公开的 Runtime/MCP action。

## 这个 Spike 回答什么问题

Probe 验证的是一条比“Python 侧复刻 Maven”更窄的路线：

```text
项目自己的 Maven 调用
        ↓
同一 Reactor / Session 中的普通 joLink Maven Mojo
        ↓
MavenProject / MavenSession 已解析事实
        ↓
私有、版本化的 Build World 输入
```

目标是不再让 joLink 逐个理解各种 POM 写法和插件习惯。Model 构建、继承、
Profile、Artifact Handler、依赖解析、Reactor 顺序和生命周期仍由 Maven 负责；
Probe 只导出 Maven 已经解析好的、有界事实。

当前 schema 仍然很小，还不能替代完整 `BuildWorldSnapshot`，因此这个 Spike 不
宣称已经捕获所有 Maven compile 语义。

## 注入契约

Probe 是普通 Maven Plugin/Mojo，不是 Maven Core Extension。它通过完整坐标调用：

```text
mvn compile io.jolink:jolink-maven-probe:<version>:export-build-world
```

正式形态会把预编译 Probe 随 joLink 一起分发。当前 Spike 只为验证 artifact 才从
源码构建；源码构建使用选定的 Maven user settings，使公司 mirror 能解析固定版本的
Probe build plugin，但不会转发命令行 target profile。日志和 settings 均为私有证据。

默认注入路径：

```text
joLink 自带的 Probe JAR + POM
        ↓
joLink cache 下内容寻址的 Maven2 file repository
        ↓
attempt 私有 settings.xml 增加一个 pluginRepository
        ↓
调用完整 Probe goal
```

目标项目不会被修改：

- 不改 POM；
- 不改 `.mvn`；
- 不改源码；
- 不改用户原始 settings；
- 调用前后验证整棵 POM 指纹。

如果原 settings 使用 `mirrorOf=*`，joLink 只在 attempt 私有副本中加入 Probe
repository 排除项：

```text
*,!jolink-local-probe-<jar-sha-prefix>
```

其他仓库仍走用户 mirror，Probe 则从 joLink 的本地 `file://` 仓库解析。原 settings
字节保持不变；临时 settings 属于私有证据，不能进入可分享报告。没有显式 settings
时，runner 会保留 Maven 默认 `~/.m2/settings.xml` 语义。包含凭证的临时副本在 Maven
返回后立即删除，即使使用 `--keep-attempt` 也不会保留。

### 严格离线语义

如果 artifact 从未缓存，Maven `--offline` 不会访问 `file://` remote/plugin
repository。因此严格离线使用一个明确兜底：

```text
joLink 自带 Probe
        ↓
验证内容指纹与坐标冲突
        ↓
把 io/jolink/jolink-maven-probe 写入明确选择的 localRepository
        ↓
执行 Maven --offline
```

这是一次有界的 Maven 缓存写入，不是“零痕迹”。结果必须报告
`offline_probe_seeded=true`。Spike 在离线模式强制要求显式
`--local-repository`，避免 joLink 猜测并修改错误的用户仓库；坐标已有不同内容时
会 fail closed。

默认在线/file-repository 路径中，Maven 自己也可能像缓存普通插件一样，把 Probe
缓存进所选 localRepository。因此未来产品文档不能承诺 Maven 本地仓库完全不变。

`-Dmaven.ext.class.path` 暂不采用。它会把 Probe 升级成 Maven Core Extension，扩大
Maven 版本、Core API 和 ClassLoader 风险，却没有带来本实验需要的额外证据。

## Probe 依赖边界

运行时 Probe artifact 不捆绑第三方库。它只使用 `provided` 的 Maven API，并内置
一个很小的 JSON writer。首个兼容下限是 Maven 3.3.9 和 Java 8 bytecode。

源码构建只显式执行 compiler、plugin descriptor 和 jar 三个 goal，不走完整
`package` 生命周期，避免 Maven 3.3.9 的 Super POM 自动引入旧 resources/Surefire
默认插件。`--offline` 现在也传递给Probe源码bootstrap，避免目标调用离线但Probe构建
仍访问仓库。这只影响Spike的开发构建；未来发布包直接携带预编译JAR。

## v1 导出内容

Reactor 中每个项目产生一个私有 JSON：

```text
schema / Probe 版本 / Probe implementation identity
项目坐标、packaging、base directory
本次 Maven goals
compile source roots
compile classpath elements
正式 output directory
Reactor 项目身份及其 output directory
Annotation Processing discovery mode
隐式 compile-classpath 中声明 Processor service 的 artifact path
Processor provider names
`-A` Processor options 与显式 Processor names
显式 annotationProcessorPaths declaration count
```

Processor-aware初版使用历史 `spike2`；收紧Provider/runtime path和fail-closed边界的
当前收紧effective Factory Path和legacy option边界的可重复构建版本使用独立
`0.1.0-spike6` 坐标。当前已验证：

```text
discoveryMode = IMPLICIT_COMPILE_CLASSPATH
compileClasspathDiscovery = true
processorProviderArtifactPaths / providers / options
```

`processorProviderArtifactPaths`只证明artifact声明了Processor provider，不宣称它已经
包含Processor运行所需的完整dependency closure。非空options、显式Processor name、
execution-level Processor配置、plugin-level legacy `<compilerArguments><A...>`、
`maven.compiler.proc` property、raw processor compiler args、`proc=only`和目录型Provider
目前均fail closed。Provider artifact顺序保留Maven compile classpath原始顺序。

如果 effective compiler config 声明显式 `annotationProcessorPaths`，Probe当前返回
`EXPLICIT_DECLARED_UNRESOLVED`，不能伪装成已解析路径。

这些都是私有事实。绝对路径、坐标和 settings 副本不会进入可分享报告。每个 snapshot
必须回显从 Probe 源码/POM 计算出的 implementation identity，防止固定 GAV 静默命中
本地旧插件。当前可分享报告只包含数量、artifact/implementation/JDK/Maven 指纹、
耗时、mirror 调整数、离线 seed 状态和项目未修改 gate。

Probe 成为唯一BuildWorld authority前还要继续补：完整compiler options、显式Processor
artifact解析、artifact-handler provenance、generated-source provenance、resources、
toolchains和精确配置指纹。

## 权威与回退规则

当前迁移阶段：

- Maven-native Probe 输出只是实验性证据；
- 显式传入 Probe 私有报告时，Phase 2A 已逐项切换 source roots、compile classpath 和
  reactor outputs 的权威来源；
- compiler/processor 配置和 artifact-type provenance 暂时仍来自 effective POM 与
  dependency metadata，报告必须标记 `hybrid_model=true`；
- 不传 Probe 报告时保留历史 Phase 2A 入口，只用于回归/差分，不作为本轮公司验证证据；
- 旧链路未来可以作为对照证据，但 Probe 缺失或冲突时不能静默作为可信 fallback；
- Probe 文档缺失/损坏、仓库碰撞、不支持的 settings、Maven 失败都返回结构化失败；
- Probe 成功不批准 JDT 发布、HotSwap 或 Phase 2B。

未来迁移 gate：

```text
冻结相同 Maven/JDK/settings/profile/localRepository 输入
        ↓
私下比较 Probe 与旧链路事实
        ↓
解释并分类所有差异
        ↓
再逐项把权威切换到 Probe
```

## 已获得的真实证据

2026-08-16 已验证：

```text
Maven 3.9.11 + 新版宿主 JDK
  单模块                             PASS
  双模块 Reactor                     PASS
  mirrorOf=* settings                PASS
  第一次严格离线                     显式 seed local repo 后 PASS

Maven 3.3.9 + JDK 8u332
  单模块                             PASS
  双模块 Reactor                     PASS
```

Reactor fixture 故意没有把上游 SNAPSHOT 安装进本地仓库。app 的 Probe 文档仍包含
兄弟模块当前 `target/classes`，证明导出的 compile classpath 来自正在执行的 Reactor，
不是依赖旧的本地仓库 JAR。

所有执行都保持 POM 指纹不变；普通 Mojo 已能在目标 Maven Session 中成功输出并被
joLink 消费。

## 产品化前剩余验证

至少还要覆盖：

- Windows 空格和中文路径；
- 公司 Maven 3.3.9/JDK 8/settings/localRepository 环境；
- 认证 mirror、proxy、加密凭证和复杂 `mirrorOf`；
- Profile 与局部 Reactor 命令（`-pl/-am`）；
- Maven Toolchains 和 Maven Wrapper；
- Maven 4；
- 慢构建下的取消、进程树清理和有界输出；
- compiler/processor schema 扩展后，与现有 Phase 2A 发现链路继续做私有差分验证。

本契约不批准新增公开 action、自动 fallback 或产品能力声明。
