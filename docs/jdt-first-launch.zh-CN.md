# JDT-first 单模块启动

当前项目启动不再执行 Maven `compile` 或 Gradle
`compileJava/processResources/classes`。Maven/Gradle只导出依赖和编译配置；JDT在JVM
启动前完成编译。

## 实际顺序

```text
java_application(launch)
  → 读取IDEA启动配置
  → 读取本地Build World JSON
      ├─ 配置/依赖/JDK身份仍匹配：跳过构建工具
      └─ 没有缓存或已失效：只执行模型导出并保存结果
  → 打开持久JDT workspace和旧源码镜像
      ├─ 首次workspace：JDT FULL
      └─ 已有workspace：workspace_source_changes()
          ├─ 无变化：不调用JavaBuilder，直接使用现有输出
          └─ 有变化：JDT INCREMENTAL编译变化源码
  → 同步当前resources
  → 封存本次启动输出
  → 启动JVM并等待ready_port
```

`launch`仍立即返回`project_launch_started`，通过`java_status(status)`观察后台进度。
现在`compile_ready=true`可能先于`runtime_active`出现，调用方仍须等待应用ready。

## 本地持久化

```text
<joLink cache>/project-launch/<project+launch hash>/build-world.json
<joLink cache>/jdt-workspaces/<project+module hash>/state.json
<joLink cache>/jdt-workspaces/<project+module hash>/workspace/
```

Build World JSON保存Probe得到的classpath、source roots、JDK、Processor和启动参数。
源码镜像、JDT输出及JavaBuilder状态保存在workspace中。源码变化不会使Build World
缓存失效；它由`workspace_source_changes()`处理。

状态中可观察：

```text
probe_cache_reused=true       本次未调用Maven/Gradle
jdt_bootstrap_reused=true     恢复了已有JDT workspace
jdt_bootstrap_build_kind=null 源码无变化，没有执行编译
jdt_bootstrap_build_kind=INCREMENTAL
jdt_bootstrap_build_kind=FULL
```

## 当前范围

- 单模块Maven jar项目、单Project Gradle Java项目。
- Java 8或11目标平台，继续使用现有JDT/Lombok/APT能力。
- 不再把javac输出作为启动基线，也不再做启动时javac/JDT Tier1比较。
- resources先按普通文件同步，不复制Maven/Gradle自定义资源处理行为。
- Maven Reactor/Gradle多Project的跨模块JDT构建暂不实现，后续单独推进。
- Fast Test的Bootstrap不在本次改动范围内。

`reload`仍然只做HotSwap，不自动restart；结构或资源变化需要`stop → launch`。
这个新launch会使用JDT增量结果，不会重新执行正式Maven/Gradle编译。
