目前希望对 Runtime 架构进行一次重新设计。

这次不是优化 JDWP，也不是优化 Java，而是希望构建 Jolink 的 Runtime Framework。

## 设计目标

Runtime Framework 应该屏蔽所有语言实现细节。

LLM 永远不应该知道：

* JDWP
* jcmd
* jstack
* pdb
* gdb
* delve

这些都是 Runtime 内部实现。

LLM 只需要知道：

> 我要获取 Runtime 的什么能力。

因此，整个 Framework 应该围绕 Runtime Ability 来设计，而不是围绕某一种协议。

---

## Runtime Framework

建议整体目录结构重新设计，例如：

```text
runtime/

    base/
        runtime.py
        observation.py
        action.py
        result.py

    java/
        runtime.py
        jdwp.py
        jcmd.py
        process.py
        log.py
```

未来：

```text
runtime/python/
runtime/go/
runtime/node/
```

都可以采用同一套 Framework。

---

## Runtime Base

新增 Runtime 基类。

例如：

```python
class Runtime(ABC):

    def run(...)
    def stop(...)
    def restart(...)
    def status(...)
    def logs(...)
    def breakpoint(...)
    def variables(...)
```

所有语言 Runtime 都实现这一套接口。

例如：

```python
JavaRuntime(Runtime)

PythonRuntime(Runtime)

GoRuntime(Runtime)
```

这样 Tool 永远只依赖 Runtime，而不是具体语言。

---

## JavaRuntime

JavaRuntime 内部可以组合多个实现。

例如：

```text
JavaRuntime

├── JDWP
├── jcmd
├── Process
├── Console
```

Runtime 自己决定：

什么时候调用：

* JDWP
* jcmd
* jstack
* Spring Actuator

LLM 不需要知道。

例如：

读取变量：

Runtime：

如果：

JDWP 已开启：

↓

JDWP

否则：

返回：

Not Supported

而不是暴露 JDWP。

---

## Tool 与 Runtime 解耦

Tool 不允许直接依赖 JDWP。

例如：

不要：

```text
Tool

↓

JDWPClient
```

应该：

```text
Tool

↓

JavaRuntime

↓

JDWP
```

以后：

Runtime 内部实现可以随时替换。

---

## Runtime Ability

目前 MVP 只保留几个能力：

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

先不要继续增加更大的能力；exception / wait_event 属于 debug loop 内部的事件类型扩展。

重点先把 Runtime Framework 做稳定。

---

## Observation

Runtime 返回的不是协议数据。

而是 Observation。

例如：

不要返回：

```text
JDWP ThreadReference
FrameID
StatusCode
```

而应该返回：

```json
{
    "running": true,
    "pid": 12345,
    "threads": 98,
    "breakpoints": 2,
    "variables": {
        "user": "...",
        "request": "..."
    }
}
```

所有协议细节都留在 Runtime 内部。

---

## JDWPClient

JDWPClient 的职责应该进一步收缩。

它只负责：

* 建立连接
* Handshake
* Packet 编解码
* Command Send
* Reply Receive

不要继续提供：

* thread_name()
* thread_status()
* class_signature()

这些属于 Runtime。

JDWPClient 应该越来越像一个协议层。

---

## 设计理念

不要设计 Java Runtime。

而是设计 Runtime Framework。

Java 只是第一个 Runtime。

未来：

Python

Go

Node.js

都应该能够直接复用整个 Framework。

---

## Jolink Design Principle

Everything exists to reduce uncertainty for the LLM.

Runtime 的职责不是暴露语言协议。

Runtime 的职责是：

为 LLM 提供稳定、统一、可预测的 Runtime Observation。

LLM 看到的是 Runtime Ability。

Runtime 自己决定底层调用：

JDWP

jcmd

Actuator

Process

未来任何实现都可以替换，而不会影响 Tool 和 LLM。
