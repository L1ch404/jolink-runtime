目前希望对这套 JDWP 实现进行一次重构，但**不是继续完善 JDWP 协议本身**，而是调整成 Jolink 的 Runtime 架构。

### 重构目标

不要把它设计成 **JDWP Client**，而是设计成 **Java Runtime**。

对于 LLM 来说，不应该暴露 JDWP 的协议细节（ThreadReference、Frame、CommandSet 等），这些都属于 Runtime 内部实现。

LLM 只应该知道有哪些 Runtime 能力。

---

### 新的类设计

建议将目前的 Debug Tool 抽象为：

```text
JavaRuntime
```

或者：

```text
java_runtime
```

Tool 只暴露 Runtime 能力。

---

### Action 设计

目前仅保留 MVP 所需要的几个 Action：

```text
run
stop
restart
status
logs
breakpoint
exception
wait_event
variables
```

说明：

* run：启动 Java 应用
* stop：停止 Java 应用
* restart：重启应用
* status：获取运行状态（是否启动、PID 等）
* logs：获取控制台日志（Console Output）
* breakpoint：断点相关（set/remove/list；remove 优先按 request_id 删除单个断点）
* exception：异常事件相关（set/remove/list；默认用于具体异常，如 NullPointerException）
* wait_event：等待断点或异常事件命中，并返回 suspension_id
* variables：读取断点处变量值（默认跳过 this，并使用浅层对象展开）

---

### Runtime 内部实现

Runtime 自己决定如何完成这些能力。

例如：

* JDWP
* jcmd
* jstack
* Spring Boot Actuator
* Attach API

这些都是 Runtime 的实现细节。

LLM 不需要知道底层到底调用了什么。

例如：

```text
LLM

↓

java_runtime(action="variables")

↓

Runtime

↓

JDWP

↓

返回变量
```

而不是：

```text
LLM

↓

JDWP Thread

↓

Frame

↓

Slot

↓

VariableTable
```

---

### 设计原则

目标不是实现一个完整的 JDWP Client。

目标是：

> 实现一个面向 Agent 的 Java Runtime。

Runtime 应该屏蔽所有 JVM 调试协议细节，对外只暴露开发者真正关心的能力。

---

### 后续扩展

未来可以逐步增加：

```text
threads
heap
gc
system_properties
command_line
thread_dump
heap_dump
```

但是目前不要实现。

保持 MVP 足够简单。

---

### 设计理念

Everything exists to reduce uncertainty for the LLM.

Java Runtime 的职责不是暴露 JVM 协议，而是向 LLM 提供稳定、统一、可预测的 Runtime Observation 能力。

所有底层实现（JDWP、jcmd、Actuator 等）都应该对 LLM 保持透明。
