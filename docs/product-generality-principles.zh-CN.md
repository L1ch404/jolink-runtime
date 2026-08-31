# joLink 产品泛用性原则

> 状态：长期产品决策（Accepted）

## 第一原则：用真实运行证据消除不确定性

joLink存在的价值，是让Agent通过真实运行事实减少对程序行为的猜测。开发joLink
本身也必须遵守同一原则：

> **优先在真实项目、真实构建工具、真实JDK/JVM、真实MCP stdio、真实文件与
> 进程生命周期中验证结论，而不是依赖静态推理、mock或契约自证正确。**

证据优先级固定为：

```text
真实用户/真实项目复现
→ 真实跨边界E2E
→ 契约与不变量测试
→ 定向单元测试和mock
→ 静态推理只作为辅助证据
```

具体要求：

1. 构建、测试、reload、debug、分发、跨平台、编码、进程清理等跨边界能力，
   必须尽可能运行真实Maven/Gradle、JDK、JVM和MCP子进程；
2. mock适合验证局部不变量、确定性竞态和故障注入，不能替代产品验收；
3. 在宣布能力可用前，必须从真实输入走到用户可见结果，而不只是构造一个符合
   预期形状的fixture；
4. 当前环境无法提供真实验证时，必须明确记录`evidence gap`，不能用绿色mock、
   推理结论或新增边界替代缺失证据；
5. 所有结论必须区分已观察事实、推断和未知；
6. 安全拒绝只证明没有假成功，不证明兼容性和泛用性；
7. benchmark和dogfood用于主动发现兼容性缺口。有效主流项目未进入Fast Path时，
   必须继续调查原因，不能把结构化`UNSUPPORTED`当作成功。

这条原则高于“让测试变绿”和“让Contract完整”。如果真实运行结果与设计、mock或
文档冲突，以真实运行证据为准，并修正实现和结论。

## 定位

joLink 是面向 Coding Agent 的开源 Java Runtime Interface，不是只服务某个
公司项目的定制工具。

目标用户应当是一个使用陌生、普通 Java 仓库的人。他不需要：

```text
joLink维护者陪同排查
修改项目POM或Gradle配置
了解joLink内部JDT/JDWP实现
使用针对特定仓库的兼容分支
```

在明确冻结的主流 Java 目标范围内，joLink 的核心产品承诺是：

```text
持久JDT增量编译
Fast Test
可靠reload/restart
运行时观察与调试
```

Maven和Gradle负责导出权威项目模型、首次基线和必要恢复。它们不是遇到常见
配置时随手降级的默认日常编译路径。

## 安全拒绝不是完成

结构化fail-closed能够避免假成功，是必须保留的安全底线，但它不代表产品已经
支持该项目。

以下结论不再被视为“工作完成”：

```text
发现真实项目无法进入Fast Path
→ 增加一个UNSUPPORTED错误码
→ 补一条契约和测试
→ 宣布一切尽在掌握
```

如果一个常见主流配置导致：

```text
UNSUPPORTED
UNVERIFIED
formal Maven/Gradle fallback
```

它应当进入产品兼容性缺口队列，继续调查和解决，而不是因为拒绝过程安全就关闭。

## 主流目标范围

具体版本会随产品演进，但目标范围至少应显式覆盖：

```text
Maven与Gradle Java项目
Java 8 / 11 / 17
单模块与常规多模块/Reactor
普通resources
常见compilerArgs
主流Annotation Processor
JUnit 4 / JUnit 5 / TestNG
常见Surefire/Failsafe/Gradle Test配置
常规启动、测试、reload与restart
```

Kotlin、Android、AspectJ、私有javac插件、特殊字节码织入等能力可以单独定义
阶段和分母，不能用它们模糊主流 Java 范围的覆盖率。

## 新边界的处理流程

每次遇到新的边界必须按以下顺序处理：

1. 判断它在主流 Java 项目中是否常见；
2. 检查 Maven、Gradle、javac、JDT、IDE/m2e/Buildship 或测试框架的真实语义；
3. 抽象成可复用的 CompilerProfile、Build World、Test Runtime 或 Runtime
   生命周期能力；
4. 禁止按公司、仓库名或某个精确 POM 形状增加特判；
5. 使用公司dogfood、公开仓库和benchmark共同回归；
6. 只有确认属于目标范围外，才可以作为长期hard boundary。

`compilerArgs`等参数容器必须解析具体参数和作用域，不能只因字段非空就整体拒绝。
未知参数也不能静默忽略后依靠浅层产物比较宣布成功。

## Fallback的地位

正式Maven/Gradle fallback可以保留，用于：

```text
明确超出目标范围的定制编译体系
灾难恢复
用户主动要求正式构建复核
JDT Worker损坏后的安全恢复
```

但它只是安全带，不是产品发动机。

以下常见情况进入fallback时，必须计为Fast Path覆盖缺口：

```text
普通compilerArgs
主流APT
常规Maven Reactor
标准Gradle SourceSet
常见JUnit/Surefire配置
普通resources
Java 8/11/17标准编译
```

fallback成功不得计入Fast Path支持率，也不得用来粉饰产品泛用性。

## 指标

报告必须分别展示三类指标：

### 安全性

```text
错误宣称成功 = 0
Generation/产物污染 = 0
残留进程/暂停线程 = 0
失效Runtime Evidence = 0
```

### 可用性

```text
陌生用户无需维护者陪同
无需修改项目构建配置
无需猜测Maven/JDK/settings/module
错误能够指导用户完成下一步
```

### Fast Path覆盖率

```text
Fast Path Coverage
= 真正进入Persistent JDT/Fast Test/reload的有效项目数
  / 冻结目标范围内的有效项目总数
```

目标是在明确、固定且有证据的主流 Java 分母内达到至少 98% Fast Path
Coverage。

以下项目不能进入分子或分母来美化结果：

```text
镜像/网络未准备导致的环境失败
base checkout本身无法编译的无效benchmark
formal build fallback
仓库特判后才能运行的项目
```

## Benchmark和公司项目的作用

公司项目是高复杂度真实验收语料，不是产品逻辑的特判对象。

SWE-PolyBench、SWE-bench-Live和公开仓库的用途是：

```text
主动发现陌生项目兼容性问题
统计常见构建配置分布
验证通用修复
在正式Agent A/B前清除joLink自身障碍
衡量真实Fast Path覆盖率
```

跑完题目、安全拒绝所有未知配置，不等于完成验证。只要大量有效项目仍未进入
Fast Path，就必须继续分析原因和提升泛用性。

## 对外结论的表达标准

结论必须如实区分：

```text
安全拒绝成立
产品可用覆盖率不足
Fast Path覆盖率不足
真正完整通过
```

禁止用“没有发现确定产品Bug”掩盖“没有样本真正进入Fast Test”，也禁止因为
状态机和错误码正确就宣称产品已广泛可用。

这条原则优先于为了让单个Review或测试报告变绿而继续缩小产品边界。
