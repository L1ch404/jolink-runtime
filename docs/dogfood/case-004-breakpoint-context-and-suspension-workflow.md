# Case-004：断点位置与上下文保留——一次层级校验误判

> **数据说明**
>
> 本文来自一次真实 dogfood 排查，但接口路径、业务类名、方法名、字段名、
> 类型编码、节点名称、节点 ID、源码行号和错误文案均已替换为虚构或泛化
> 内容。示例请求不是原系统的可用请求，也不能用于识别原项目。
> 匿名化保留的是执行关系、证据边界和调试教训，而不是原始业务数据。

## 一、Case 概要

用户在一个“扩展工作区”下面继续创建普通页面时，接口返回了层级超限错误。

多个分析窗口都找到了直接抛出异常的校验方法：

```text
NavigationTreeService.validateDepth(Integer depth, Integer nodeType)
```

第一轮 Runtime 观察只看到：

```text
depth = 7
nodeType = 20
MAX_STANDARD_DEPTH = 6
```

它足以解释代码为什么抛异常，却不能解释为什么当前场景不应该应用普通
导航树的层级限制。

第二轮将断点移到更上层、业务对象仍然完整的位置，观察到当前节点的直接
父节点是目录，并保留了父节点继续指向上层“扩展工作区”的关系。结合后续
只读数据验证，证据链最终确认：

```text
扩展工作区 type=90
└── 工作区目录 type=10
    └── 当前创建页面 type=20
```

本 Case 暴露了三个相互关联的问题：

1. 层级校验只判断当前节点的深度和类型，遗漏了祖先关系；
2. 断点位置决定 Runtime Evidence 能保留多少业务上下文；
3. 触发请求命中断点后可能被挂起，Agent 不能先等待 HTTP 正常返回再
   调用 `await`。

## 二、匿名化业务场景

### 1. 接口和请求

本文使用虚构接口：

```text
POST /api/navigation/nodes/upsert
```

匿名化请求：

```json
{
  "depth": 7,
  "nodeType": 20,
  "displayName": "示例页面",
  "parentNodeId": 6208
}
```

节点类型经过替换：

```text
type=10：目录
type=20：普通页面
type=90：扩展工作区根节点
```

系统返回的匿名化错误为：

```text
导航节点已达到最大层级
```

### 2. 已确认的业务规则

本次排查使用的业务规则是：

> 只要节点位于扩展工作区的祖先链下面，就不受普通导航树的最大层级限制。

当前创建节点本身仍然可以是普通页面。是否限制层级不能只判断当前节点的
`nodeType`，还需要判断祖先链中是否存在扩展工作区节点。

该规则来自需求确认，不是 Runtime 根据变量值推导出来的结论。

## 三、第一次 Runtime 观察：直接触发条件

第一轮断点设置在：

```text
NavigationTreeService.validateDepth
```

调试流程为：

```text
breakpoint(set)
→ wait_event(wait_mode="arm")
→ receive status="armed"
→ trigger scenario
→ wait_event(wait_mode="await", wait_handle=...)
→ variables
→ resume
→ cleanup_debug_state
```

### Runtime 直接观察事实

| 变量 | 运行时值 |
| --- | ---: |
| `depth` | `7` |
| `nodeType` | `20` |
| `MAX_STANDARD_DEPTH` | `6` |

这组证据可以证明：

1. 当前请求实际执行到了层级校验；
2. 当前节点按运行时参数被视为普通页面；
3. 当前计算深度为 7；
4. 普通导航树的内部限制值为 6；
5. 异常由 `depth > MAX_STANDARD_DEPTH` 触发。

直接执行链路为：

```text
父节点深度为 6
→ 新节点深度被计算为 7
→ validateDepth(7, 20)
→ 按普通页面规则校验
→ 7 > 6
→ 抛出异常
```

这解释了：

> 当前代码为什么抛出异常。

但它没有解释：

> 当前请求为什么不应该应用这条普通导航树规则。

## 四、Context-Loss Boundary

在上层业务方法中，程序仍然拥有：

```text
当前节点对象
直接父节点对象
父节点类型和名称
父节点的 parentId
节点之间的层级关系
```

进入：

```java
validateDepth(Integer depth, Integer nodeType)
```

之后只剩：

```text
depth
nodeType
```

完整业务对象被压缩为两个标量参数，祖先链信息在这里丢失。本文将这种位置
称为：

```text
Context-Loss Boundary
```

因此，离异常最近的断点并不一定最有解释力。异常附近适合观察直接触发
条件，但业务对象可能已经被拆解，无法再回答“为什么本场景不应应用这条
规则”。

选择断点时应额外考虑：

```text
解释问题需要哪些对象和关系？
这些上下文在哪一步会被转换、拆解或压缩？
能否在该边界之前观察完整对象？
```

## 五、第二次 Runtime 观察：在上下文丢失前取证

第二轮将断点移到匿名化的上层方法：

```text
NavigationTreeService.upsertNode
```

该位置仍然保留 `parentNode` 对象。

### Runtime 直接观察事实

```text
parentNode.displayName = "扩展工作区目录"
parentNode.nodeType = 10
parentNode.parentId = 4102
```

这可以直接证明：

```text
当前页面的直接父节点是目录 type=10
该目录仍然指向上一级节点 4102
```

但仅凭上述变量，还不能证明节点 `4102` 的类型。

### 后续只读数据验证

后续证据确认：

```text
nodeId = 4102
nodeType = 90
```

因此，组合后的祖先链为：

```text
扩展工作区 type=90
└── 工作区目录 type=10
    └── 当前创建页面 type=20
```

这里必须区分两个证据来源：

```text
Runtime Evidence
→ 证明当前执行中的 parentNode 内容和 parentId

只读数据验证
→ 证明 parentId 指向的上级节点类型
```

Runtime 与后续数据证据共同确认了完整祖先关系。不能把 `parentId` 本身
误写成“Runtime 已经证明上级节点是扩展工作区”。

## 六、完整根因

结合业务规则、Runtime 观察、只读数据验证和静态代码分析，证据链如下：

```text
当前创建节点是普通页面 type=20
→ 直接父节点是目录 type=10
→ 目录的上级节点是扩展工作区 type=90
→ 当前节点实际位于扩展工作区的祖先链下
→ 该场景不应受到普通导航树最大层级限制
→ 现有 validateDepth 只接收 depth 和 nodeType
→ 校验逻辑无法判断祖先链
→ 当前节点被错误地按普通导航树规则校验
→ depth=7 超过内部限制值 6
→ 抛出异常
```

问题不是请求把当前节点类型传错了。当前对象确实是普通页面，因此
`nodeType=20` 是合理的。

问题也不能简单归结为限制值太小。真正的问题是：

> **层级校验只判断当前节点自身的深度和类型，没有获得判断其所属导航树
> 所需的祖先上下文。**

从方法设计上看：

```java
validateDepth(Integer depth, Integer nodeType)
```

其参数不足以独立完成“是否位于扩展工作区下面”的业务判断。

## 七、静态分析提供的横向覆盖

静态分析除了找到当前校验路径，还发现了两个需要继续评估的线索。

### 1. 相邻的深度刷新路径

匿名化后的相邻方法为：

```text
recalculateDescendantDepths
```

它可能在移动节点、修改父节点或递归刷新子节点深度时应用相同规则。因此它
是需要审查的同类路径，但在没有运行时或测试证据前，只能标记为：

```text
潜在受影响路径
```

不能直接宣布它已经在当前场景触发。

### 2. 已有祖先判断辅助方法

静态分析还发现了匿名化辅助方法：

```text
isUnderExtendedRoot()
```

方法名表明它可能用于判断祖先关系，但仍需检查：

```text
是否遍历完整祖先链
是否只检查直接父节点
是否适用于创建和移动路径
是否处理循环或缺失父节点
是否可以安全复用
```

发现一个名称相符的方法只是修复线索，不是其正确性证明。

## 八、不同观察方式的覆盖范围

### 静态分析：Structural Coverage

静态分析更适合：

```text
查找校验方法的所有调用方
发现相邻刷新和移动路径
发现已有辅助方法
评估修改的整体影响范围
检查当前请求未执行到的代码
```

### Runtime：Execution-Path Coverage

Runtime 更适合：

```text
确认当前请求实际进入的分支
确认当前对象和参数的真实值
确认当前父节点对象
确认直接异常触发条件
把候选业务结构与本次真实请求关联起来
```

更合理的组合顺序是：

```text
静态分析定位候选路径和业务规则
→ 找到 Context-Loss Boundary
→ 在边界前选择断点
→ Runtime 验证当前请求的真实对象和执行路径
→ 使用只读数据补齐跨对象关系
→ 静态检查相邻调用路径
→ 修改代码
→ Runtime 再次验证真实场景
```

## 九、Suspension Workflow Misuse

本次测试还暴露了一个独立的 Runtime 使用问题。

其中一个 Agent 采用了错误顺序：

```text
breakpoint
→ arm
→ 同步调用 HTTP 接口
→ 等待接口正常返回
→ 准备在返回后调用 await
```

断点命中后，处理该 HTTP 请求的 Java 线程会进入 suspended 状态：

```text
Agent 等待 HTTP 返回
→ HTTP 等待 Java 请求线程继续执行
→ Java 请求线程等待 Agent inspect 和 resume
→ 形成循环等待
```

为 HTTP 客户端设置超时可以防止客户端无限阻塞，但它不会恢复 Java 线程，
也会占用 joLink 等待 Agent 领取 suspension 的安全窗口。因此客户端超时只应
作为保护措施，不应作为主要同步机制。

### 推荐流程

简单的本地 HTTP 场景可以由 joLink 负责关键顺序：

```text
1. 设置 breakpoint 或 exception watch；
2. wait_event(wait_mode="arm", http_trigger=...)；
3. 等待 status="armed"；
4. 按 required_next_action 立即 await(wait_handle=...)；
5. 读取 stack 和 variables；
6. resume(suspension_id=...)；
7. 调试结束后 cleanup_debug_state。
```

`http_trigger` 只会在 JDWP 完成 arming 后异步启动，所以 `arm` 不会等待
可能被断点挂起的 HTTP 响应。Agent 不应再次发送同一个请求。

无法使用内置本地 HTTP trigger 时，继续使用外部非阻塞流程：

```text
1. 设置 breakpoint 或 exception watch；
2. wait_event(wait_mode="arm")；
3. 等待 status="armed"；
4. 在独立终端或后台启动目标请求，不等待它完成；
5. 立即 wait_event(wait_mode="await", wait_handle=...)；
6. 读取 stack 和 variables；
7. resume(suspension_id=...)；
8. 检查目标请求的最终结果；
9. 调试结束后 cleanup_debug_state。
```

简化为：

```text
breakpoint
→ arm
→ trigger without waiting
→ await immediately
→ inspect
→ resume
→ verify
→ cleanup
```

客户端超时的正确定位是：

```text
避免外部触发工具永久等待
≠
替代 await、resume 或 cleanup_debug_state
```

## 十、对 joLink 的产品启示

### 1. 关键顺序应由协议行为保证，动态提示负责解释

仅靠 `suggested_next_step` 不能保证模型不会同步等待 HTTP 响应。对于简单的
本地 HTTP 场景，`arm(http_trigger=...)` 现在把以下顺序收进 MCP 边界：

```text
JDWP armed
→ 异步启动 HTTP
→ 返回 wait_handle 和 required_next_action=await
```

动态结果仍需解释“不要重复发送请求”和“检查后必须 resume 或 cleanup”。
其他触发类型继续使用外部后台任务，不扩展为通用场景编排器。

### 2. 断点选择应围绕待验证的不确定性

Agent 不应只问“异常在哪一行”，还应先确定：

```text
直接触发条件是否未知？
业务对象的来源是否未知？
对象关系是否会在下层方法中丢失？
哪个位置仍保留区分竞争性假设所需的信息？
```

### 3. Runtime 不负责自动解释业务语义

Runtime 能证明当前变量和路径，却不能仅凭 `parentId` 自动证明上级节点的
类型，也不能从 `nodeType` 自动推导完整业务规则。必要时仍需结合字段血缘、
只读数据和需求确认。

## 十一、`0.1.0a2` 使用后的模型评价

完成本 Case 后，实际使用 joLink 的模型对当时版本给出了如下总体评价：

> 核心想法和设计方向正确，Java 调试原语已经能够组成完整闭环，但整体仍更像
> 一个能够跑通的 Debug MVP；主要问题集中在稳定性、等待时序和 Agent 编排成本。

模型认可的能力包括：

```text
attach / breakpoint / stack / variables / resume 原语完整
MCP 形式适合 Coding Agent 直接操作本地 JVM
arm / await 两阶段等待方向正确
java_processes 能降低进程发现成本
```

它实际感受到的主要问题包括：

```text
arm 后由外部工具触发 HTTP，再 await，跨多次工具调用且时序容易出错
同步等待 HTTP 响应时，不理解请求线程可能已经被断点挂起
未及时领取 suspension 时遇到 WAIT_RESULT_EXPIRED
suggested_next_step 不能稳定约束模型行为
cleanup 后仍被提示额外调用 status，增加了一次编排步骤
过期结果对事件历史和自动恢复过程解释得不够直观
```

### 这份评价的版本边界

这份评价来自 `0.1.0a2`，产生于内置 HTTP Trigger 实现之前。因此它应作为
`0.1.0a3` 的基线体验，而不能直接视为新版本仍然存在的结论。

本次反馈推动了以下协议级改动：

```text
arm(http_trigger=...)
→ joLink 等待 JDWP armed
→ joLink 异步启动本地 HTTP 请求
→ 返回 required_next_action=await
→ Agent 直接领取 Runtime Event
```

后续应使用相同或等价场景重新测试，并比较模型是否仍会：

```text
重复发送 HTTP 请求
同步等待被断点挂起的响应
遗漏 await
在领取窗口结束后才请求结果
```

如果这些问题消失，就能形成一条完整的 dogfood 改进证据链：

```text
真实 Agent 使用反馈
→ 识别编排缺陷
→ 协议级修复
→ 相同场景回归验证
```

### 关于 `WAIT_RESULT_EXPIRED` 的技术校正

模型将该错误理解成“30 秒内断点没有命中”，但当前两种状态实际已经分开：

```text
事件等待窗口内没有命中
→ status="timeout"

事件已经命中并产生 suspension，但未在结果领取安全窗口内被 await
→ WAIT_RESULT_EXPIRED
→ Runtime 自动尝试 resume
→ 原 suspension_id 失效
```

真实 UX 问题不是两种结果使用了同一个错误码，而是
`WAIT_RESULT_EXPIRED` 仍要求模型从 `invalidated_suspension_id` 等字段自行推断
事件历史。后续可以考虑显式返回：

```text
event_was_observed
event_observed_at
result_retention_seconds
expired_at
suspension_auto_resumed
auto_resume_result
```

这属于结果可解释性优化，不应和普通事件 timeout 混为一谈。

## 十二、构建上下文缺失导致的 `run` 使用困难

本 Case 还暴露了一个发生在 Runtime 调试之前的问题：Agent 不一定能使用项目
真实的 Maven 环境完成打包，因此 `java_runtime(run)` 很难获得可启动的新产物。

### 实际观察

模型使用了类似下面的通用命令：

```bat
cd C:\workspace\sample-service && mvn compile package -DskipTests -Dmaven.test.skip=true
```

而开发者在 IDE 中实际使用的是一套明确配置过的 Maven 环境。脱敏后，真正影响
构建结果的部分近似为：

```bat
D:\tools\apache-maven\bin\mvn.cmd ^
  -s D:\tools\apache-maven\conf\settings.xml ^
  -Dmaven.repo.local=D:\maven-repository ^
  -DskipTests=true ^
  -f pom.xml ^
  package
```

IDE 额外添加的版本标识、事件监听器、彩色输出等参数主要用于 IDE 集成和显示，
通常不是命令行构建的必要条件。

### 通用命令存在的问题

1. `package` 本身已经包含 `compile` 生命周期阶段，`compile package` 没有必要。
2. `-DskipTests` 与 `-Dmaven.test.skip=true` 语义不同，但同时使用通常是重复的，
   Agent 应根据是否需要编译测试代码选择其中一个。
3. 直接调用 `mvn` 假设 Maven 已正确加入 `PATH`，并可能使用与 IDE 不同的版本。
4. 没有复用项目真实的 `settings.xml`，可能无法访问公司镜像、私服或 Profile。
5. 没有复用真实本地仓库，可能重新下载大量依赖，或找不到内部构件。
6. 没有确认实际使用的 JDK、工作目录、激活 Profile 和最终产物位置。
7. 对多模块项目，如果只在子模块执行 `package`，可能缺少尚未构建的兄弟模块。

在脱敏示例环境中，更接近真实构建上下文的命令是：

```bat
cd /d C:\workspace\sample-service && D:\tools\apache-maven\bin\mvn.cmd -s D:\tools\apache-maven\conf\settings.xml -Dmaven.repo.local=D:\maven-repository -DskipTests=true -f pom.xml package
```

Windows CMD 使用 `cd /d`，可以同时切换盘符和目录。

如果目标是聚合工程中的一个模块，可能需要在父工程目录使用：

```bat
mvn.cmd -pl sample-service -am -DskipTests=true package
```

具体命令仍应由项目真实结构决定，不能机械套用。

### 对 Agent 的构建上下文要求

Agent 在打包前至少应确认：

```text
项目是否提供 mvnw / mvnw.cmd
实际 Maven 可执行文件
实际 JDK / JAVA_HOME
settings.xml
本地仓库位置
单模块还是多模块
需要激活的 Maven Profile
跳过测试的真实意图
最终可执行 JAR 或 classpath 位置
```

推荐的发现顺序是：

```text
项目 Maven Wrapper
→ 项目构建脚本、README 和 CI 命令
→ 已知的 IDE / 用户 Maven 配置
→ 系统 PATH 中的 mvn
```

### 对 joLink 产品边界的启示

`java_runtime(run)` 当前负责启动一个已经存在的 JAR 或 classpath 产物，不应
在没有充分设计前直接变成 Maven/Gradle 构建器。构建和运行具有不同的失败语义：

```text
构建失败
→ 依赖、编译、测试、插件、私服或 Profile 问题

运行失败
→ JVM 参数、启动方式、端口、应用配置或 JDWP 问题
```

第一阶段更合理的方向是让 Agent 在调用 `run` 前发现并复用真实构建上下文，
而不是让 Runtime 猜测一个通用 `mvn package`。是否需要提供结构化的 build/run
协作能力，应结合更多 dogfood 结果单独设计，不在本 Case 中直接决定。

完整的验证闭环因此是：

```text
修改代码
→ 使用项目真实构建配置生成新产物
→ java_runtime(run / restart)
→ arm(http_trigger=...)
→ await
→ inspect
→ resume
→ 验证请求结果
```

这个问题和 HTTP Trigger 的共同点是：

> Agent 能生成一个通用动作，但通用动作缺少真实环境上下文；关键环境证据不能只靠
> 模型猜测。

## 十三、Case 分类

本 Case 同时属于：

```text
Runtime Confirmation Case
Breakpoint Placement Case
Context Preservation Case
Static-and-Runtime Complementarity Case
Suspension Workflow Misuse Case
Agent Workflow Feedback Case
Build Context Discovery Case
```

评估结果：

| 维度 | 结论 |
| --- | --- |
| 静态分析能否找到直接校验问题 | 可以 |
| Runtime 能否确认直接异常条件 | 可以 |
| Runtime 能否提供额外业务上下文 | 取决于断点位置 |
| 是否需要额外数据验证祖先链 | 需要 |
| 静态分析能否发现相邻路径 | 可以，但仍需验证 |
| Runtime 验证价值 | 高 |
| 是否暴露 suspension 使用问题 | 是 |
| 是否暴露构建环境发现问题 | 是 |
| 是否形成版本改进基线 | 是，`0.1.0a2` → `0.1.0a3` |

## 十四、最终结论

本 Case 最重要的结论有六点：

1. **离异常最近的断点不一定最有解释力。** 低层断点适合确认直接触发
   条件，上层断点更可能保留业务对象和关系。
2. **Agent 应在 Context-Loss Boundary 之前取证。** 完整对象一旦被压缩为
   标量参数，Runtime 无法恢复已经丢失的上下文。
3. **触发 suspended 请求必须采用非阻塞编排。** `arm` 后应后台触发并立即
   `await`，不能先等待 HTTP 正常返回。
4. **静态分析与 Runtime 提供不同覆盖。** 静态分析负责横向结构覆盖，
   Runtime 负责纵向真实路径和对象证据，完整根因需要两者形成闭环。
5. **真实构建环境也是运行时验证的前置证据。** Agent 不能用通用
   `mvn package` 代替项目真实的 Maven、JDK、settings、仓库、Profile 和
   多模块上下文。
6. **dogfood 评价必须标注版本边界。** `0.1.0a2` 暴露的外部 HTTP 编排问题
   是 `0.1.0a3` HTTP Trigger 的需求基线，应通过相同场景回归验证改进效果。

可以将本 Case 抽象为：

```text
Runtime Evidence Effectiveness
= Tool Capability
× Breakpoint Placement
× Context Preservation
× Trigger Timing
× Suspension Management
× Build Context Fidelity
× Evidence Interpretation
```

joLink 的长期价值不只是让 Agent 拥有断点，而是帮助它逐渐学会：应该在
哪里观察、需要保留哪些上下文、如何复用真实构建环境、如何触发被观察场景，
以及如何区分直接事实、补充证据和尚未验证的推断。
