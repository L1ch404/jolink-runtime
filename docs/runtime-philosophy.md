# Runtime 启动成功的定义

> joLink 的长期产品泛用性与Fast Path覆盖原则见
> [product-generality-principles.zh-CN.md](product-generality-principles.zh-CN.md)。
> 安全拒绝只是底线，不能替代对陌生主流Java项目的真实支持。

> **状态：Accepted for MCP v0.1**
>
> 当前采用可选的本地 TCP readiness：Runtime 仍只返回可验证事实，不把
> TCP 端口开放解释为完整业务健康。

## 2026-07 更新：当前采用的折中方案

Dogfood 证明，仅返回 Process/JDWP Ready 会让 Agent 在大型应用仍处于
初始化阶段时过早触发业务请求。让 Agent 通过日志猜测 readiness 也不够
确定。

因此 MCP v0.1 增加两个可选参数：

```text
ready_port
startup_wait_timeout_seconds
```

当调用方明确提供 `ready_port` 时：

```text
进程存活 + 端口尚未接受连接
→ startup_state=starting

端口接受本地 TCP 连接
→ startup_state=ready

进程退出
→ startup_state=failed
```

同步等待超时不会终止进程。Runtime 返回 `starting`，后续 `status` 使用
同一端口继续观察。没有配置端口时返回 `unverified`，绝不把 Process/JDWP
Ready 冒充 Application Ready。

这不是方案 B 中的完整 Application Ready。Runtime 没有判断数据库、缓存、
业务接口或框架生命周期，只陈述“指定端口是否接受了 TCP 连接”这一事实。
因此当前实现仍遵循 Fact Provider 的边界。

---

## 背景

在实现 `Runtime.run()` 时，遇到了一个核心问题：

**什么才算启动成功（Startup Success）？**

表面上看只是一个实现细节，但实际上它决定了 Runtime 的职责边界，因此属于架构层面的设计问题。

---

# 当前发现的问题

目前存在几个不同层级的"启动成功"：

### Process Ready

进程已经启动。

例如：

* `subprocess.Popen()` 成功
* PID 存在
* Process Alive

但是：

* JVM 可能马上退出
* Main 方法可能抛异常
* 应用还没有真正开始工作

因此：

**Process Ready ≠ Startup Success**

---

### Debug Ready

JDWP 已经可以连接。

例如：

* JDWP Handshake 成功
* 可以设置断点
* 可以读取变量

但是：

Spring Boot 可能还在：

* 初始化 Bean
* 建立数据库连接
* 初始化 Redis
* 初始化 MQ

因此：

**JDWP Ready ≠ Application Ready**

---

### Application Ready

应用已经真正能够提供服务。

例如：

Spring Boot：

* `ApplicationReadyEvent`
* `Started xxxApplication`
* `/actuator/health == UP`

但是：

并不是所有 Runtime 都有：

* Spring
* HTTP
* Tomcat

因此：

Application Ready 并不具备通用性。

---

## 当前存在的两种设计思路

### 方案 A：Runtime Ready（偏底层）

`run()` 保证：

* Process 已启动
* Runtime 已接管
* Debug 能力可用

例如：

```text
run()

↓

启动 JVM

↓

JDWP Ready

↓

Runtime 接管成功

↓

return
```

随后由 LLM 自己继续：

* 调接口
* 观察日志
* 判断应用是否真正 Ready

### 优点

* Runtime 只提供事实（Facts）
* 不绑定 Spring Boot
* 不绑定 HTTP
* 天然支持多语言 Runtime

### 缺点

LLM 需要多进行一次观察。

---

### 方案 B：Application Ready（偏高级）

`run()` 一直阻塞。

直到：

Runtime 能够确认：

应用真正可用。

例如：

* HTTP Health Check
* 指定日志
* 自定义 Ready Strategy

例如：

```text
run()

↓

启动 JVM

↓

等待

↓

Health Check

↓

Ready

↓

return
```

### 优点

* Action 原子性更强
* LLM 使用更简单

### 缺点

Runtime 开始理解业务。

例如：

* Spring
* HTTP
* Tomcat

降低通用性。

---

# 此前倾向（保留为设计背景）

最初更倾向于：

> **Runtime 负责提供确定性的事实（Facts），而不是替 LLM 做业务判断。**

例如：

Runtime 可以确认：

* Process Alive
* JDWP Connected
* Uptime
* PID
* Log Path

这些都是 Runtime 能够确定的事实。

但 Dogfood 证明，让 LLM 仅依赖日志自行判断 readiness 容易过早触发。
当前方案因此增加了显式、结构化的 TCP readiness，同时仍不替 LLM 判断
完整业务健康。

---

# Runtime 的设计目标

Runtime 的核心目标应该包括：

## 1. 原子性（Atomic）

一个 Action：

要么：

* 成功完成

要么：

* 明确失败

不能返回半完成状态。

---

## 2. 确定性（Deterministic）

对于 Runtime 能确认的信息：

必须返回确定结果。

不能猜测。

---

## 3. 可观测性（Observable）

Runtime 应尽可能提供事实。

例如：

* Process 状态
* Debug 状态
* 日志
* PID
* Exit Code
* Uptime

让 LLM 基于事实进行决策。

---

# 当前决定

当前采用：

* Process/JDWP 状态始终返回；
* 应用 readiness 必须由调用方显式配置；
* 第一版只支持本地 TCP 端口事实；
* 未配置返回 `unverified`；
* 同步等待超时返回 `starting` 并保留进程；
* `status` 负责结构化复查，不依赖日志推断；
* 不解析 Spring 日志，不自动扫描端口，不推断业务健康。

---

# TODO

未来验证：

* 是否需要在 TCP 之外增加显式 HTTP Health Ready Strategy？
* TCP readiness 对真实项目是否已经足够？
* 多语言 Runtime 下，这套设计是否仍然成立？

---

> **一句话总结：**

> Runtime 负责执行受控生命周期动作并提供可验证事实；它不把端口、日志或
> 变量值自动解释成完整的业务结论。
