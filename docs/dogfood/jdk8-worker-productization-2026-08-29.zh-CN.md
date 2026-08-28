# Java 8 Worker 产品化深测（2026-08-29）

本报告只包含本机生成的编译器证据，不包含业务源码、凭据或接口数据。

## 1. 兼容性结论

旧产品Worker全部9个class为major 61，JDK8直接返回
`UnsupportedClassVersionError`。源码包含10处Java 11/17 API：

```text
String.isBlank                  2
Path.of                         6
Collection.toArray(IntFunction) 1
java.util.HexFormat             1
```

正式Worker现已使用Java 8等价实现，manifest为`JavaSE-1.8`，由真实JDK8
确定性构建。产品lock记录：

```text
worker_java_minimum = 8
worker_class_major  = 52
```

构建JDK vendor/version/SHA只作为provenance，不限制用户本机JDK。

## 2. 同一JAR运行矩阵

所有运行均以Java8系统类库作为编译target，依次执行READY、FULL、方法体
Incremental、编译错误、恢复和STOP：

```text
Worker JDK8   FULL ≈ 900 ms   Incremental ≈ 12 ms   Recovery ≈ 5 ms
Worker JDK11  FULL ≈ 939 ms   Incremental ≈ 16 ms   Recovery ≈ 10 ms
Worker JDK17  FULL ≈ 765 ms   Incremental ≈ 13 ms   Recovery ≈ 8 ms
```

三组均：

```text
compile error observed = true
recovery successful     = true
STOP settled            = true
```

## 3. Java 8 APT完整套件

JDK8 Worker + Spring configuration Processor：

```text
FULL                      PASS
APT factory path          verified
generated source root     verified
新增property Incremental  PASS / clean-FULL oracle equal
删除property Incremental  PASS / clean-FULL oracle equal
```

资源峰值：

```text
ready RSS       ≈ 128 MB
FULL peak RSS   ≈ 147 MB
incremental peak ≈ 139-141 MB
```

已知大边界（本轮记录，不直接扩实现）：

```text
删除Processor annotation
或删除整个source
→ JDT native incremental可能保留旧generated metadata
→ clean + FULL fallback可恢复并与oracle一致
```

产品当前已禁止直接source deletion，但“移除Processor annotation”仍需要后续
设计processor-aware invalidation或安全的clean/FULL fallback，不能笼统放开。

## 4. 423-source真实项目

```text
Worker JVM       JDK8 64-bit
Java sources     423
Lombok agent       1
标准Processors     2
JDT FULL          PASS
errors              0
Tier 1            compatible
declared types     439 / 439
耗时               ≈ 6.6秒
```

完整MCP产品launch也观察到：

```text
launch_phase        runtime_active
compile_ready       true
jdt_worker.major    8
jdt_worker.data_model 64
fast_update.strategy jdt_incremental
```

说明只有项目现有JDK8时，不再需要额外安装JDK17。

## 5. 真实MCP产品闭环

一个只依赖JDK8系统类库的本地Maven Socket应用完成：

```text
launch
→ Worker JDK8
→ compile_ready=true
→ 行为 before
→ method-body reload / HotSwap（≈17 ms）
→ 行为 after
→ restart current Generation
→ 行为仍为 after
→ schema change / Candidate Restart
→ 行为 structural
→ 启动失败Candidate
→ 自动rollback
→ last-good行为仍为 structural
```

最终rollback状态：

```text
applied      false
apply_method restart
rolled_back  true
error_code   CANDIDATE_START_FAILED
```

## 6. 深测中发现并修复的小Bug

连续快速修改同一个源码时，文件系统时间戳粒度可能让Eclipse漏掉第二次
resource delta，曾出现：

```text
source bytes changed
Worker compiled_source_count = 0
compile_ok = true
Candidate仍指向上一次class
```

修复采用双保险：

1. private source mirror的mtime强制单调前进；
2. 源码字节变化但Worker未编译任何source时，返回
   `JDT_SOURCE_CHANGE_NOT_OBSERVED`并poison session，绝不发布旧Candidate。

修复后快速连续Incremental、编译错误和恢复矩阵全部通过。

## 7. 长驻Worker压力和生命周期

最终Java 8 Worker完成A9长驻压力流程：

```text
warm-up correctness gate
→ 100次混合FULL/Incremental/错误恢复操作
→ 取消与生命周期竞态
→ 资源趋势检查
→ cooperative STOP
```

结果：

```text
status                    a9_evidence_passed
process-tree RSS peak     ≈ 185 MB
measured RSS delta        ≈ 22 MB（门限67 MB）
heap delta                ≈ 3 MB
metadata delta            ≈ 1.1 MB
forced termination        false
```

说明同一Java 8 Worker在长驻混合编译、错误恢复和取消后，没有
观察到越过已冻结门限的内存增长或强制退出。

## 8. 最终回归与发布流程

最终工作区回归：

```text
non-real-JVM tests  678 passed, 15 skipped
real MCP/JVM E2E     10 passed
compileall            PASS
git diff --check      PASS
```

`build_worker.py`现在一条命令完成：

```text
真实JDK8 javac校验
→ source/target 8
→ 全class major 52校验
→ JavaSE-1.8 manifest校验
→ 确定性JAR
→ 更新实验provenance
→ 更新product lock/Worker SHA
→ 更新wheel base64资源
```

重复执行产物hash不变，已验证idempotent。
