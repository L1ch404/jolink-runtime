# SWE-PolyBench Verified Java 稳定性预检（2026-08-31）

## 目的

本轮不是模型解题跑分，也没有应用 gold patch。它回答两个问题：

1. joLink Fast Test 在真实 Java benchmark 仓库中能否稳定启动、测试、增量编译、回滚并清理；
2. 在后续 DeepSeek A/B 之前，哪些题目环境、构建边界或 runner 条件必须先处理。

## 固定输入

```text
SWE-PolyBench harness:
9c836c5d7f3cb991934132b77d29e6941d912a07

SWE-PolyBench Verified dataset:
b3fca77b637379f0c01ad86d18753a7ac1998b53

dataset CSV SHA256:
0c8138e73c34fa29a5276b675b146b72d78ce001fcc4560d76302c908b4808a5

joLink product RC5:
1939bb4dd7fe16648e801adde2946c86ae482824

runner:
1a733957ed07e1c4b9fe26f6363a9396c2ac604b

wheel SHA256:
0ffe9b7e50b8aaad4c081e50ec7ea42ba9d4741a652a304bf41bec8019daaae9
```

数据集共有 382 条，其中 Java 69 条：

```text
Gson       19
Guava       2
Dubbo      15
RocketMQ   18
Apollo      4
Trino      11
```

runner 在载入记录后立即丢弃：

```text
patch
modified_nodes
problem_statement
hints_text
```

只应用 `test_patch`，不读取或应用答案。每题使用官方镜像、官方 JDK 和官方测试命令，随后才运行 joLink。Mac 为 arm64，官方镜像为 amd64，通过 Docker Desktop Rosetta 串行执行，`max_workers=1`。

## 验证链路

对可进入 joLink 的题目，runner 计划验证：

```text
官方 test command
  ↓
选择一个 P2P；没有 P2P 时选择 F2P
  ↓
Fast Test baseline
  ↓
向目标测试源码追加无语义注释
  ↓
Fast Test forward incremental
  ↓
恢复原始源码
  ↓
Fast Test reverse incremental
  ↓
校验 public compiled_source_units
  ↓
校验测试结果往返一致
  ↓
关闭 session，检查 Worker/Runner 残留
```

## 首轮结果

69/69 均产生了独立原始结果，runner 正常退出。原始控制台摘要为：

```text
DATASET_OR_ENVIRONMENT_FAILURE  35
PRODUCT_BUG                     17
UNSUPPORTED_EXPECTED            17
```

但 `PRODUCT_BUG` 是 runner 的初始粗分类。逐项检查结构化 `bootstrap_log_tail` 后，17 项应全部重分类：

```text
16 项：Maven Central TLS / artifact / plugin / parent / build-extension
       解析失败

 1 项：Dubbo test_patch 调用 gold patch 才会新增的 isDeprecated(Method)，
       base checkout testCompile 无法通过
```

证据化派生结论因此是：

```text
确定的 joLink 产品缺陷                0
明确、保守的不支持边界               17
数据集或执行环境失败                 52
```

这不等于“69 题都受支持”。恰恰相反，本轮正式全量运行没有一题完成 Fast Test 增量往返：大量题目没有拉到镜像、官方 base 无法编译，或 joLink Probe 的 Maven 依赖因 TLS 失败。

独立 pilot 提供了一条正向证据：

```text
instance: apolloconfig__apollo-4568
official selected test: PASS
Fast Test baseline: PASS
forward: 只编译目标测试源码，PASS
reverse: 只编译目标测试源码，PASS
result parity: true
cleanup: true
total: 166.4s
```

因此当前准确表述是：

> joLink Fast Test 的真实 benchmark 闭环已经成立，连续 runner 和安全失败行为稳定；但 69 题的广泛覆盖尚未得到证明，A/B 前仍需先稳定环境并处理高频产品边界。

## 17 项明确边界

```text
Java target 低于 8                         7
Surefire runtime configuration 不支持      4
main Processor discovery mode 不支持        2
profile 控制 Maven modules 不支持           2
Maven + Gradle 同时存在，构建系统歧义       2
```

这些结果都以结构化错误返回，没有假装测试通过，没有执行不可信字节码；已进入 joLink 的相关实例也都完成 cleanup。

## A/B 前必须扫清的障碍

### 1. 镜像必须提前物化

23/69 在 GHCR 拉取阶段失败。正式 A/B 不应边跑边拉镜像，应先：

```text
拉取目标镜像
记录 digest
验证可创建容器
冻结本地镜像集合
```

### 2. Maven/Probe 依赖必须提前预热

官方测试能运行，不代表 joLink Bootstrap 所需依赖已经存在。高频失败包括：

```text
maven-help-plugin:3.2.0
plugin dependencies
parent POM
project build extensions
```

正式 A/B 应先在每个目标镜像中物化 joLink Probe 与项目 Maven Bootstrap 所需依赖，然后在缓存冻结后运行；不能让两组 Agent 承受不同的网络状态。

本轮随后实现了prepared image v2：

```text
官方base image
  ↓
在线解析Maven effective POM
  ↓
Maven offline重复验证
  ↓
安装当前wheel锁定的完整JDT Candidate
  ↓
断开容器网络
  ↓
再次加载并校验JDT Candidate
  ↓
确认/testbed Git状态逐字节不变
  ↓
docker commit成本地prepared image
  ↓
manifest绑定base image id、wheel SHA和uv SHA
```

评分runner可通过`--prepared-images`读取manifest，并通过
`--require-prepared-images`禁止静默回退到在线base image。

### 3. 排除无效 base 题目

12 项官方 base compile 失败，常见原因是 `test_patch` 直接引用 gold patch 才提供的 API。它们不能直接用于评价 Agent 或 joLink。正式题集应在 A/B 前完成：

```text
base checkout + test_patch
官方 selected test 可发现
项目可编译
测试具有确定初态
```

### 4. 显式选择构建系统

Gson 中真实出现同时包含 Maven 和 Gradle 的仓库。joLink 当前保守返回 `BUILD_SYSTEM_AMBIGUOUS`。产品与 runner 应支持显式指定：

```text
build_system=maven | gradle
```

避免 Agent 被迫通过改路径或猜测构建权威来绕过。

本轮已把该参数加入`java_application(action=test)`。选择结果会进入TestAttempt
快照和持久Build World身份；同一路径切换Maven/Gradle不会复用另一套Session。

### 5. 题目分层

第一轮 A/B 建议分为：

```text
主测组：环境已冻结、base 有效、joLink Fast Test 已通过预检
边界组：joLink 明确拒绝，验证 Agent 是否正确降级
压力组：RocketMQ/Trino 等长构建，仅少量纳入
排除组：镜像/依赖未物化或 base test 无效
```

## 当前稳定性判断

正面证据：

- 69 个实例串行运行约 3 小时，runner 正常完成；
- 没有发现 joLink 进程崩溃、跨题会话污染或错误宣称成功；
- 不支持场景均为结构化 fail-closed；
- Apollo 真实实例完成 baseline、forward、reverse 和 cleanup 闭环；
- Fast Test 返回的 `compiled_source_units` 能证明增量编译目标身份。

尚未证明：

- 69 题上的广泛 Fast Test 支持率；
- 多种 Surefire 高级配置的等价执行；
- Java 8 以下项目的 Fast Compile；
- profile-controlled module 与未知 Processor 模型；
- 网络未冻结时的 benchmark 可重复性。

## 原始证据位置

本机原始证据保存在：

```text
/Users/lich/.cache/jolink-runtime/benchmarks/rc5/results/full-1a73395
/Users/lich/.cache/jolink-runtime/benchmarks/rc5/results/pilot-apollo4568
```

原始 `result.json` 不做覆盖。后续所有重分类和题目筛选都应作为派生报告保存，以保留审计链。

## 后续 RC6 复核

prepared image与显式build system落地后，进行了两项代表性复核。

### Gson 1989

```text
原结果：BUILD_SYSTEM_AMBIGUOUS
显式build_system=maven：歧义消失
下一真实边界：FAST_TEST_COMPILER_CONFIGURATION_UNSUPPORTED
```

该历史版本Gson为testCompile配置了JDK toolchain和Java 6 source/target。joLink
当前Fast Test只支持已验证的Java 8/11等价模型，因此继续fail-closed是正确的，
不应为了benchmark放宽。

### Apollo 4568

prepared image v1只冻结Maven依赖，随后真实暴露JDT artifact仍会在运行时下载；
prepared image v2补齐JDT Candidate并断网验证后，完整结果为：

```text
official selected test         PASS
Fast Test baseline             PASS
forward incremental            PASS
reverse incremental            PASS
compiled source identity       PASS
result parity                  true
cleanup                        true
total                          51.2s
```

同一实例RC5未预热pilot耗时约166.4s。这里不能把差值全部解释成joLink性能提升，
但它证明把网络和编译器物化移出评分阶段可以同时改善可重复性与实际周转时间。
