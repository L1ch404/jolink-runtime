# 产品 direct-javac 路线移除

2026-09-10。用户业务源码的产品编译后端只保留 JDT，不能在 JDT 不可用时再回落到
direct-javac。本次是移除已放弃路线，不增加新的运行限制或拒绝条件。

2026-09-21 整理更新：下文记录的是当时的移除过程。后来保留在 `experiments/`
中的冻结研究实现及专属测试也已移出产品主线，历史可在清理前提交 `88fe07d`
查阅。当前更新入口为 `java_application(action="restart")`，编译、HotSwap与
普通重启沿用已实现的产品路径；[兼容性跟进](java-compatibility-followup-2026-09.md)
列出最新状态，不以本文的旧待办判断当前支持范围。

## 删除与保留

- 删除 `launch/fast_compile.py` 中的编译器、Plan、私有 staging 执行和专属错误类型。
- 删除 Runtime 对旧编译器的初始化、关闭、reload fallback、class staging 比较和
  旧 Plan/状态字段。JDT HotSwap、断点 stale 标记、取消和运行结果记录继续使用原路径。
- 删除产品 Maven adapter 中的旧 FastCompilePlan/JDT 旧 Plan 生成方法，以及其中
  的 direct-javac 专属前置策略。单模块与多模块启动统一使用已有 Maven-native Probe。
- 共用指纹函数移动至 `build_world_identity.py`。与删除前函数的签名/函数体做 AST
  对照，仅函数名改变，计算算法和输出不变，不为了重命名使缓存失效。
- 单模块仍使用原来的单工程 workspace 布局；多模块仍共用一个 Worker。保留
  Probe 给出的源码根、实际输出目录、编码、parameters、编译堆参数和配置文件输入。
- 冻结实验仍需的旧静态策略放在 `experiments/legacy_maven_policy.py`；产品不导入它，
  也不进入 wheel。旧业务编译后端本身已删除，没有换一个位置继续作为 fallback。
- Worker/Probe/Runner 的 `javac` 构建、原生编译对照和直接 JAR/classpath **运行**
  不属于被移除的业务源码编译后端，保持不变。

## 现在的路径

```text
launch：缓存 Build World / Maven、Gradle Probe → 持久 JDT 编译 → 应用 JVM
reload：现有 JDT session → 增量结果 → JDWP HotSwap
test：缓存 Test Build World / Probe → 持久 JDT 编译 → 独立 Runner
```

JDT 不可用时如实报告 JDT 状态，不再构造旧 Plan、启动临时 javac 或输出“备用编译可用”。
已有缓存仍可读取，Worker 包没有改变。新建模型使用 Probe 的实际数据；没有额外增加
源码/输出扫描、复制、校验轮次、GC 策略或常驻进程。

## 实际验证

原兼容性样本 Petclinic 旧版、同一固定 SHA、JDK8，使用真实 stdio MCP：

1. IDEA Make disabled，冷启动 JDT FULL 编译 25 个 main 源码，应用成功启动。
2. 首页 HTTP 200，按姓氏查询 Owner 返回正常重定向。
3. 修改 Controller 方法体，reload 实际 INCREMENTAL，只编一个源码，
   新 HTTP 请求的 Location 包含新增查询参数。
4. restart 后仍返回修改后的 Location，日志没有新增 BUILD/Worker 启动。
5. 恢复源码并 reload，HTTP Location 恢复原值，最后 stop。
6. 新 MCP 进程再次 launch：Probe 和 JDT workspace 均复用，实际不编译，HTTP 仍正常。

注意：restart 返回中可能仍带首次 bootstrap 的 FULL 历史字段；本例以实际
`jdt.build.started` 日志确认没有重新编译，不把历史字段误记为一次新 FULL。

本地业务服务样本（项目标识已脱敏），457 个 main/test 源码：GC 开/关各一次真实
MCP 全流程，包含 5 轮业务修改/恢复、编译错误/恢复、无改动和跨 MCP。
每组 14 次实际编译，除首次 FULL 外均为增量；业务修改产生预期 2 个失败，恢复后
6/6 通过。原项目未修改。

## 回归维护

旧后端的私有 staging、direct-javac 文件限额、旧 Plan freshness/拒绝条件等专属
测试随实现删除，不再要求产品保持已经废弃的行为。共用指纹、Maven 模型输入、
JDT reload、debug 观察保护、断点失效、错误恢复、持久化和真实 MCP 测试继续保留或补测。

结构回归会检查产品没有导入旧后端或实验策略；打包后也检查 wheel 不含这些模块。
这不代表其余兼容性问题全部解决：Java17、main/test Processor 差异、特殊编译参数等
仍按 `java-compatibility-followup-2026-09.md` 分开推进。

本轮本机：普通测试 `704 passed, 47 skipped`；真实 MCP 启动/debug/reload/Maven及
Gradle模块 `14 passed`，Java 8/11持久化/无改动重开/APT/禁用索引/GC/日志及
Surefire对照 `20 passed`，完整 Fast Test `6 passed`。对应环境用例单独开启开关，
不把 skip 算通过。wheel/sdist、compileall、git diff --check 通过；测试副本的源码和
临时启动配置已恢复。冻结实验测试改为使用实验自己的策略，没有让产品恢复旧限制。
