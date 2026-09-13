# JDT-first 启动与快速 reload

joLink 服务于开发环境。成功建立的本地 Build World 和 JDT workspace 直接复用，
不再在每次启动或 reload 时重新审计源码、依赖和编译输出。

当前Worker运行环境与项目JDK独立。首次使用安装固定版本Temurin21，后续直接复用；
不改变项目Maven/Gradle或业务JVM的JDK。更换JDT引擎首次建立新workspace并FULL一次，
旧版本的内部增量状态不迁移。离线准备和已知兼容问题见[升级记录](jdt-346-upgrade.zh-CN.md)。

产品 direct-javac 后端、Plan 和 fallback 已移除。Maven 单模块与多模块都通过
已有 Maven-native Probe 准备模型，JDT 不可用时不会改走旧编译器。
删除范围与回归见 [direct-javac 移除记录](direct-javac-retirement.zh-CN.md)。

## 启动

```text
读取已有 Build World JSON（没有才运行模型导出）
→ 使用固定JDT3.46与私有Temurin21 Worker，复用项目目标system libraries
→ 打开 JDT workspace，不执行 Equinox -clean
→ workspace_source_changes()
   ├─ 首次：JDT FULL
   ├─ 文件大小/修改时间未变：不读取源码内容，不编译
   └─ 有变更：只同步这些源码并通知 JavaBuilder
→ JVM直接使用持久JDT workspace的bin目录，不复制class
→ 启动 JVM
```

源码镜像和原文件的大小/mtime索引持久化到workspace。启动时仍需枚举源码文件的元数据
以发现离线修改，但不再逐个读取新旧源码内容。若外部工具刻意保持mtime和大小不变，
启动扫描可能看不到该编辑；显式 `reload(source_files)` 会直接读取指定文件。

resources 作为运行classpath中的源码资源目录直接读取，不再每次reload复制整棵资源树。
上次编译成功或失败的结果随源码索引一起保存；重开workspace时直接读取，
不会把上次的编译失败包装成“没有变化，启动成功”。

## reload

```text
接受 source_files，返回 reload_started + reload_id
→ 只读取并同步指定的源码
→ 通知 Eclipse 对应文件已更改
→ JavaBuilder INCREMENTAL
→ 实际编译后保存 workspace/源码索引，按开关请求一次 GC
→ 直接取得 Eclipse output resource delta
→ 将变化class发给JDWP
→ 发布last_reload
```

已删除：

- 多轮 Build World freshness 校验和源码指纹复核；
- Worker 在 BUILD 前后对所有class计算SHA；
- Python为了计算Runtime delta和更新基线再次扫描整个输出；
- Runtime reload的resources全量复制；
- 无改动reload的额外 SAVE/checkpoint；
- 启动的多次Generation复制及复制前后哈希审计；
- 每次启动对已安装Worker依赖重新计算SHA、重复探测同一JDK。
- 首次成功后重复写入、比对Worker的classpath、编码、编译和Processor配置；
- JDT对warning/info的生成以及结果层的构造、排序和返回。

首次启动把Worker参数和Eclipse工程配置保存在workspace。以后直接使用保存的启动参数
和工程配置；Worker只打开workspace，不重新设置或逐项回读比对。全量和增量编译都将
可选warning/info设为ignore，结果仅收集ERROR。`jdt_build_ms`和`diagnostics_ms`分别
报告JDT build与错误收集耗时。

实际编译错误和JVM拒绝仍按结果返回。结构修改如果被JVM拒绝，就重新launch；
不再在发送JDWP前进行一套独立的class schema/metadata预检。HotSwap不会重新执行
静态初始化，也不代表Spring配置或已有对象被刷新，最终以新请求的实际行为为准。

`compile_ms`包含Worker BUILD、实际编译后的SAVE及可选GC；`compile_total_ms`包含指定源码同步；
`apply_ms`是JDWP应用时间；`total_ms`覆盖后台Attempt结束，不再隐藏一次后置SAVE。

## 缓存与生命周期

- Maven及Gradle单Project构建配置/依赖变更不再自动做完整freshness审计。需要重新Probe时，手动清理对应
  `project-launch` 和 `jdt-workspaces` 缓存后launch。
- 已安装的Worker按分发目录复用；新的Worker版本使用新的缓存目录。
- 正常stop/shutdown保存workspace和源码索引。
- JVM直接读取当前JDT bin；restart使用当前编译输出，不回退到首次启动副本。
- Worker仍然隶属于当前MCP进程；跨对话/跨MCP保活不在这次改动范围内。
- Maven Reactor和Gradle多Project支持目标及所需模块共用一个Worker，多模块流程见
  [多模块JDT流程](jdt-modules.zh-CN.md)。Gradle多Project会比较已导出的小构建配置，
  配置变化则重新Probe；不重验整份依赖或输出。
