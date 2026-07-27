# Case-005：Runtime Operations 支撑长流程增量分析 E2E

> **数据说明**
>
> 本文整理自一次真实 joLink dogfood，但业务名称、服务名、JAR 路径、
> 配置键、日志文本、任务 ID、数据数量、表结构和测试数据均已替换为
> 虚构或泛化内容。
>
> 文中使用的“消息分析”“文档数据源”“关系结果库”和“外部分析模型”
> 只用于表达通用执行关系，不对应原系统的真实命名或可用接口。
> 匿名化保留的是 Runtime 使用方式、证据边界和产品教训，而不是原始
> 业务设计。

## 一、Case 概要

本次 Case 验证一个持续十余分钟、需要多次修改数据并反复启停 Java 服务的
增量分析流程。

业务链路经过匿名化后表示为：

```text
文档数据源中的模拟消息
→ Java 定时分析服务
→ 外部分析模型
→ 关系结果库中的任务、历史和分析结果
```

本次使用的 joLink Action 为：

```text
run
status
logs
stop
```

没有使用：

```text
breakpoint
exception
wait_event
stack
variables
resume
```

因此，这不是一次深度调试能力验证，而是一次 Runtime Operations 和日志
观察能力验证。

Agent 借助 joLink 完成了：

```text
准备测试数据
→ 启动 Java 服务
→ 等待定时任务执行
→ 读取运行日志
→ 查询下游数据
→ 停止服务
→ 修改输入或启动参数
→ 再次启动并继续验证
→ 最终停止服务并清理测试数据
```

这个 Case 说明：joLink 即使不进入断点调试，也能降低 Coding Agent 执行
真实 Java E2E 的进程编排成本。

## 二、匿名化测试背景

被测功能是一个通用的增量消息分析任务。

由于测试环境无法稳定产生真实上游消息，测试通过向一次性文档数据源写入
模拟消息，验证以下下游行为：

```text
首次分析
范围内的新消息触发覆盖重算
重算读取有效范围内的全部消息
旧的自动分析结果被软删除
人工确认结果被保留
范围外消息不触发指定范围任务
“全部历史”任务扫描完整历史
延迟到达的历史消息触发重新分析
```

测试标准明确要求不能只看到：

```text
Spring Boot 启动成功
PID 仍然存在
```

就宣布业务通过。

完整证据需要来自多个来源：

```text
joLink status
joLink logs
文档数据源查询
关系结果库查询
外部分析结果
测试数据清理结果
```

## 三、为什么这个流程需要 Runtime Operations

本次测试不是一次短命令，而是一个有多个等待和重启阶段的状态机：

```text
启动服务
→ 等待应用初始化
→ 等待定时调度
→ 等待外部模型
→ 检查数据
→ 停止服务
→ 修改数据
→ 使用不同参数重新启动
→ 再次等待调度和模型
→ 验证结果
→ 清理
```

如果不使用统一的 Runtime 工具，Agent 需要自行维护：

```text
Java 启动命令
后台进程
PID
JDWP 参数
日志重定向
日志文件位置
进程存活检查
Windows 进程终止
旧进程和端口清理
```

这些动作本身并不属于业务验证，却容易让长流程在中途失败。

joLink 将它们收敛为少量有状态 Action，使 Agent 可以把注意力放在：

```text
当前测试进行到哪个阶段
下一步应该修改什么输入
需要观察什么证据
哪些结论仍未验证
```

## 四、实际执行流程

### 4.1 首次启动

匿名化后的启动请求为：

```yaml
action: run
jar_path: C:\demo\sample-analysis-worker\target\worker.jar
app_args:
  - --spring.profiles.active={{TEST_PROFILE}}
```

joLink 完成：

```text
使用 java -jar 启动服务
启用 JDWP
记录 Runtime-owned PID
创建并返回日志文件
等待 JDWP 就绪
返回实际使用的 JDWP 端口
```

需要注意：

> joLink 当前不会自动搜索并分配空闲 JDWP 端口。

如果调用方没有显式传入 `jdwp_port`，当前版本使用 Schema/Runtime 的默认
端口。返回该端口不等于动态分配了一个新端口。

本次 Case 未遇到端口冲突，但这只能证明该端口在本次执行环境中可用。

### 4.2 等待真实业务执行

服务启动后，Agent 多次调用 `logs`，观察匿名化后的关键阶段：

```text
analysis task started
task=sample-task-42 pending-session-count=1
message-field-sample session-type=DIRECT
processing message id=sample-message-101
calling external analyzer
task=sample-task-42 completed
```

这些日志能够证明：

```text
定时任务执行到了对应阶段
数据查询至少命中了一个待处理对象
代码进入了外部分析调用路径
应用记录了任务完成事件
```

但仅凭日志不能独立证明：

```text
外部分析模型返回内容完全正确
关系结果库写入的每个字段都正确
所有消息分支都已覆盖
```

这些结论需要继续结合下游数据查询。

Agent 没有因为第一次 `logs` 尚未出现完成消息就立刻判定失败，而是根据
异步任务和外部调用的性质继续等待，并读取后续日志快照。

### 4.3 停止服务并修改测试数据

首次分析完成后，Agent 调用：

```yaml
action: stop
```

随后在一次性测试数据中加入：

```text
一条旧的自动结果哨兵
一条人工确认结果哨兵
一条位于任务范围内的新消息
```

本次执行中，Runtime-owned 服务被成功停止，没有观察到旧进程继续执行后续
调度的现象。

这是对本次执行实例的观察，不代表所有 Windows/JDK/应用退出路径已经得到
证明。

### 4.4 使用不同参数重新启动

为了避免在专项测试中等待正常的长调度周期，Agent 在下一次 `run` 中传入
匿名化的测试覆盖参数：

```yaml
action: run
jar_path: C:\demo\sample-analysis-worker\target\worker.jar
app_args:
  - --spring.profiles.active={{TEST_PROFILE}}
  - --analysis.change-detect-initial-delay-ms=10000
  - --analysis.change-detect-delay-ms=7200000
```

重新启动后，Agent 观察到匿名化日志：

```text
in-range message change detected; task requeued
old-count=3
new-count=4
task=sample-task-42 pending-session-count=1
calling external analyzer
task=sample-task-42 completed
```

这一阶段证明：

```text
run 可以为同一个 JAR 传入不同 app_args
新的 Runtime-owned 进程使用了本轮参数
应用实际执行了增量变化检测路径
```

下游数据查询进一步确认：

```text
旧的自动结果被软删除
人工确认结果仍然存在
新的自动结果已经生成
```

### 4.5 验证范围外消息

下一阶段的匿名化流程为：

```text
stop
→ 写入范围外消息
→ run
→ logs
→ 查询任务与结果
```

观察结果为：

```text
本轮没有出现指定任务重新排队日志
指定任务状态没有发生预期外变化
结果库没有新增对应自动结果
```

“没有看到某条日志”本身不是强证据，因此该结论同时依赖：

```text
足够长的调度观察窗口
任务状态查询
结果数量查询
输入数据范围检查
```

### 4.6 验证全部历史和延迟消息

最后两个阶段验证：

```text
创建“全部历史”分析任务
→ run
→ logs
→ 验证完整历史消息数量
```

以及：

```text
stop
→ 写入一条延迟到达的更早消息
→ run
→ logs
→ 验证消息数量增加并重新分析
```

匿名化后的观察为：

```text
历史任务首次读取 5 条消息
写入延迟消息后读取 6 条消息
任务重新进入分析流程
下游结果与新的输入集合对应
```

### 4.7 最终清理

测试结束后，Agent：

```text
调用 stop
确认 Runtime-owned 服务已停止
删除文档数据源中的模拟消息
删除关系结果库中的测试任务和结果
检查清理后的测试标识不存在
```

本次没有创建 breakpoint 或 exception watch，因此不涉及 suspension 的
`resume` 或 `cleanup_debug_state`。

如果后续 Case 进入断点流程，必须先恢复或清理 suspension，再结束服务。

## 五、不同证据来源分别证明了什么

### 5.1 joLink `run/status/stop`

直接证明：

```text
joLink 启动了一个 Runtime-owned Java 进程
该进程对应当前返回的 PID
Runtime 跟踪了它的生命周期
stop 对该 Runtime-owned 目标完成了停止
```

不能单独证明：

```text
HTTP 服务端口已经可用
Spring 所有 Bean 已完成初始化
定时任务已经运行
业务数据一定正确
```

JDWP 就绪只代表调试连接就绪，不代表完整业务 readiness。

### 5.2 joLink `logs`

直接证明：

```text
当前 Runtime-owned 进程的 stdout/stderr 中出现了对应文本
应用记录了某些执行阶段和计数
```

不能单独证明：

```text
日志文本的业务语义一定与 Agent 理解一致
日志之后的数据库事务一定提交
外部系统一定接受了请求
未出现的日志对应代码一定没有执行
```

### 5.3 数据源和结果库查询

直接证明：

```text
测试时点可读取到的原始输入
测试时点可读取到的任务状态和持久化结果
自动结果与人工结果的保留/删除状态
```

不能单独证明：

```text
具体是哪一条 Java 执行路径写入了这些数据
中间是否发生过失败重试
一次查询代表所有并发时序
```

### 5.4 组合证据

本次结论来自：

```text
Runtime 生命周期
+ 应用日志
+ 输入数据查询
+ 结果数据查询
+ 测试清理检查
```

这比“服务启动成功”或“日志中出现完成”提供了更完整的 E2E 证据。

## 六、joLink 在本次 Case 中的实际价值

### 6.1 降低 Java 服务控制成本

Agent 不需要在每个阶段重新处理：

```text
java.exe PID
java -jar 命令
后台运行
日志重定向
停止命令
```

它只需表达：

```text
运行这个 JAR 和参数
查看当前进程及日志
停止这个 Runtime-owned 进程
```

### 6.2 为长流程提供稳定执行面

本次 Case 的复杂度主要来自多阶段状态：

```text
调度等待
外部分析等待
数据修改
重新启动
再次验证
最终清理
```

joLink 减少了 Agent 丢失 PID、操作错误进程或忘记最终停止服务的概率。

在本次受控环境中，同一个服务被反复启停多次，没有观察到：

```text
旧进程残留
JDWP 端口冲突
日志文件不可读
停止后继续执行业务调度
```

这是一个有价值的稳定性样本，但仍是单一项目和环境中的有限观察。

### 6.3 日志是较低成本的 Runtime Evidence

本次主要问题可以由日志和数据查询回答：

```text
任务是否执行
输入是否命中
变化检测是否发现计数变化
外部分析路径是否被调用
任务是否记录完成
```

因此 Agent 没有强行设置断点。

合理的观察升级路径是：

```text
status 和 logs 足够
→ 完成验证

status/logs 无法区分竞争性假设
→ 设置 breakpoint 或 exception watch
→ wait_event
→ stack / variables
→ resume
```

### 6.4 app_args 支持阶段性测试配置

同一 JAR 可以在不同阶段传入不同应用参数，使 Agent 能在一次性测试环境中
缩短调度周期，而不必修改生产默认配置。

这种能力同样存在风险：

```text
Agent 必须确认参数仅用于当前测试实例
不能把一次性测试参数误写入共享配置
不能在不受控环境中缩短高成本任务周期
```

## 七、暴露出的体验问题

### 7.1 日志需要重复读取

`logs` 当前返回调用时刻的快照。

长流程中，Agent 需要：

```text
logs
→ 尚未完成
→ 等待
→ logs
→ 继续等待
→ logs
```

当前 `logs` 使用固定文件大小的有界 tail 快照。小日志完整扫描时，
`total_lines_exact=true` 且 `total_lines` 为精确值；大日志只读取尾部时，
`total_lines_exact=false` 且 `total_lines=null`，同时通过 `scanned_bytes`、
`has_more_before` 和 `truncated` 说明证据边界。重复读取还会返回
`growth_state` 和 `new_bytes_since_previous_read`，可以判断日志是否继续增长，
不需要为了比较进度重新统计整个文件。

输入仍然没有增量 cursor。后续可以继续观察是否有更多真实 Case 需要：

```text
从上次位置读取新增日志
等待有限时间直到出现新增日志
```

本 Case 只记录这一 UX 信号，不据此立即扩大公开 Schema。

### 7.2 缺少服务端日志过滤

Agent 为查找匿名化的计数和完成标识，额外使用了 Shell 文本搜索。

这说明可能存在以下需求：

```text
普通字符串包含过滤
排除已知噪声级别
只返回匹配行附近的小范围上下文
```

过滤可以减少 Token 消耗，但它也会隐藏未匹配的异常上下文。因此在设计前
需要确认：

```text
过滤结果是否明确标记为子集
是否保留总行数和扫描范围
如何避免 Agent 把“未匹配”解释为“未发生”
```

### 7.3 中文日志曾出现乱码

本次 Agent 报告部分中文日志显示异常。

当前只能记录以下事实：

```text
Agent 最终看到的部分中文文本不可读
英文标识和数字字段仍然可用
```

尚未验证乱码发生在哪一层：

```text
业务字符串进入日志前
JVM 默认字符集
日志框架编码
Java stdout 字节
joLink 日志文件
joLink UTF-8 解码
MCP 客户端渲染
```

joLink 当前让 Java 子进程直接向二进制文件描述符写入 stdout/stderr，再按
UTF-8 读取并以替换模式处理非法字节。

因此，在检查原始日志字节前，不能直接宣布根因是 joLink，也不能仅凭本次
现象决定增加 `log_encoding` 参数。

更合适的后续验证是：

```text
读取原始日志字节
→ 判断文件实际编码
→ 对比 Java 进程的 file.encoding / stdout.encoding
→ 检查应用日志框架配置
→ 对比 MCP 返回文本
```

## 八、本次业务验证范围

通过的匿名化范围：

```text
模拟消息读取
首次外部分析
范围内新增消息触发覆盖重算
重算读取有效范围内的全部消息
旧自动结果软删除
人工确认结果保留
范围外消息不触发指定范围任务
全部历史任务扫描历史消息
延迟到达消息触发重新分析
输入和结果测试数据清理
Java 服务最终停止
```

未验证范围：

```text
上游真实消息采集链路
面向用户的查询接口一致性
非文本输入边界
外部模型失败时的旧结果保留策略
并发写入和多实例调度
所有支持的 Windows/JDK 组合
```

因此严格结论是：

> 在本次匿名化代表的受控测试环境中，文档数据源 → Java 分析服务 →
> 外部分析模型 → 关系结果库的核心下游链路通过；上游采集、查询接口、
> 失败边界和并发场景未验证，完整业务 E2E 只能判定为部分通过。

## 九、对 joLink 产品定位的启示

本次 Case 展示了两层不同价值。

### 高频基础层：Runtime Operations

```text
run
status
logs
stop
restart
attach
```

作用是：

> 让 Agent 可靠地运行、观察和控制本地 Java 应用。

### 低频深度层：Runtime Debugging

```text
breakpoint
exception
wait_event
stack
variables
resume
cleanup_debug_state
```

作用是：

> 当生命周期、日志和外部结果不足以解释行为时，让 Agent 进入 JVM
> 获取更细粒度的真实执行状态。

这两层不是互相替代，而是成本不同的观察梯度：

```text
生命周期与日志
→ 足够时直接完成
→ 不足时升级到断点和变量
```

本次只验证了第一层，却仍然显著减少了 Agent 的低层进程编排工作。

因此更准确的定位是：

> **joLink 让 Coding Agent 运行、观察和调试本地 Java 应用。**

但本 Case 只能支持较窄的结论：

> 在一次受控的本地 Java 长流程 E2E 中，joLink Runtime Operations
> 提供了稳定且有用的执行面。

它不能单独证明 joLink 已适用于：

```text
远程生产环境
任意构建系统
所有 Java 版本
所有日志编码
无人值守的长期任务
```

## 十、核心教训

### 1. joLink 的价值不依赖每次都设置断点

如果日志和数据查询已经能够区分当前假设，继续进入断点只会增加暂停管理和
Agent 编排成本。

### 2. Runtime lifecycle 是业务 E2E 的基础证据，不是完整业务证据

```text
running=true
```

不能证明业务 ready 或测试通过。

### 3. 日志是观察事实，但仍需要语义和持久化验证

```text
日志出现“completed”
```

只能证明应用写出了这条日志，不能替代结果库检查。

### 4. 长流程需要显式维护阶段和清理责任

Agent 应持续记录：

```text
当前 Runtime-owned PID
当前测试阶段
当前输入集合
已经确认的证据
仍未验证的范围
最终 stop 和数据清理是否完成
```

### 5. 一次成功的稳定性样本不能无限外推

反复启停多次没有残留是积极证据，但仍然受限于：

```text
当前项目
当前操作系统
当前 JDK
当前启动参数
当前测试时长
```

## 十一、最终总结

本次 Dogfood 没有使用 joLink 最复杂的调试能力，却证明了一个重要的产品
价值：

```text
Agent 可以通过少量稳定 Action
持续控制同一个 Java 服务的多轮启动、观察和停止
并把进程管理从业务验证流程中抽离出来
```

joLink 在本次流程中直接提供的是：

```text
Runtime-owned Java 生命周期
当前运行状态
当前实例的 stdout/stderr 快照
可重复的 run/stop 控制面
```

业务结论则由：

```text
joLink Runtime Evidence
+ 输入数据
+ 下游持久化结果
+ 外部分析结果
+ 最终清理检查
```

共同支持。

最终结论：

> **在本次受控的本地 Java 长流程 E2E 中，joLink 即使只使用
> `run`、`status`、`logs` 和 `stop`，也明显降低了 Coding Agent 的
> 进程编排成本，并提供了可复用的 Runtime Operations 执行面。**
