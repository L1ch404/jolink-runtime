# Runtime 启动成功的定义（待思考）

> **状态：Draft**
>
> 当前阶段不做最终设计，仅记录思考，等待 Runtime 支持更多语言后再回顾。

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

# 当前倾向

目前更倾向于：

> **Runtime 负责提供确定性的事实（Facts），而不是替 LLM 做业务判断。**

例如：

Runtime 可以确认：

* Process Alive
* JDWP Connected
* Uptime
* PID
* Log Path

这些都是 Runtime 能够确定的事实。

至于：

应用是否真正 Ready：

交给 LLM 根据 Runtime 提供的信息自行判断。

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

**暂不确定最终方案。**

目前 MVP：

优先保证：

* Java Runtime 可用
* Debug 能力稳定
* Runtime 接口稳定

待未来支持：

* Java
* Python
* Go

之后，再重新评估 Runtime 的职责边界。

---

# TODO

未来验证：

* Runtime 是否应该提供 Ready Strategy？
* Ready 是否应该可配置？
* LLM 是否能够仅依赖 Observation 完成 Ready 判断？
* 多语言 Runtime 下，这套设计是否仍然成立？

---

> **一句话总结：**

> Runtime 的职责，到底是**执行任务（Task Executor）**，还是**提供事实（Fact Provider）**，这是一个值得持续思考的问题，而不是当前阶段必须立刻解决的问题。
