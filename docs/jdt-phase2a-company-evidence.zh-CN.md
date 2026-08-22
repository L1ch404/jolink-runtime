# JDT Phase 2A：公司真实项目实测记录

状态：`Phase 2A PASS / Phase 2B blocked`

更新时间：2026-08-21

这份文件记录脱敏后的当前结论。公司环境中保存的 canonical JSON 是原始机器证据；
更早的 DOCX 用于恢复详细排查过程，不再覆盖本文件中的最新状态。

## 证据优先级

```text
公司环境 canonical Phase 2A JSON
    -> 本文件中的脱敏事实
    -> 历史排查 DOCX
    -> 公司电脑上的临时 debugging patch
```

临时 patch 只能解释实验过程，不能直接作为生产实现。

## Canonical 结果

真实企业 Maven 模块的冻结 Build World：

```text
Java source count          4201
compile classpath entries  297
source / target            8 / 8
BuildWorld encoding        utf8
Eclipse effective encoding UTF-8
Target JDK                 Oracle JDK 8
Lombok                     1.18.20
JDT Core                   3.25.0.v20210223-0522
```

构建结果：

```text
Maven baseline             PASS
Maven duration             138980 ms
Worker startup             5901 ms
JDT actual build kind      FULL
JDT duration               356987 ms
JDT error count            0
JDT warning count          3566
```

跨编译器结构 Gate：

```text
Tier 1 status                    compatible
source-declared type sets equal  true
missing declared types           0
extra declared types             0
API mismatches                   0
class-major mismatches           0
```

Maven/javac 与 JDT 的全部 class 数量不同：

```text
Maven classes               4777
JDT classes                 4758
Maven generated Tier 2        37
JDT generated Tier 2          18
```

这些差异位于当前 `recorded_not_gate` 的 compiler-generated Tier 2，不覆盖已经
通过的 source-declared type / API / class-major Tier 1 结论。

最终状态：

```text
status    = phase2a_passed_with_incremental_blockers
decision  = PHASE2B_BLOCKED_BY_BUILD_WORLD
```

当前唯一 Phase 2B blocker：

```text
unknown_compile_time_annotation_processor
```

因此 Phase 2A 已正式通过；当前不能进入 Phase 2B，不是因为 JDT FULL 失败，而是
Annotation Processor 的增量刷新语义尚未得到证明。

## 本轮确认的 Build World 修复

### Eclipse Resources source encoding

`BuildWorldSnapshot` 已经捕获 Maven 的 source encoding，但早期 Worker 只把它保留在
joLink 模型中，没有写入 Eclipse Resources。JavaBuilder 的 `SourceFile` 通过
`IFile.getCharset()` 读取源码，因此在 Windows 上可能按平台默认编码解释 UTF-8 字节，
制造字符串未闭合、非法字符常量等级联假错误。

正式链路必须是：

```text
Maven effective encoding
    -> BuildWorldSnapshot.encoding
    -> Worker --source-encoding
    -> source folder setDefaultCharset(...)
    -> Java Charset canonicalization
    -> Worker READY 回报 raw/canonical/effective/verified encoding
```

encoding 已经参与 Build World fingerprint；encoding 改变必须使旧 generation 失效。
实现不能硬编码 UTF-8，也不能让 Python codec registry 替 Java/Eclipse 判断编码是否合法。

### Maven classpath membership

Maven Probe 是 compile classpath membership authority。一个被 Maven 放入 compile
classpath 的已知二进制 artifact type，即使只包含资源、不包含 `.class`，仍可能是
合法输入。明确的 sources/javadoc artifact 继续排除，未知 artifact type 继续
fail closed。

### javac / ECJ source compatibility

大项目最终剩余的稳定 blocker 是 raw `ArrayList` + double-brace anonymous class
参与泛型返回值推断。javac 8 以 unchecked/raw warning 接受，ECJ/JDT 3.25 返回硬
type mismatch。公司实验实际将代码改写为：

```java
BaseResponse.toSuccess(Arrays.asList(entity))
```

修改后 JDT FULL 达到 0 error。这个结果证明 blocker 与 raw anonymous collection /
generic inference 有关，不代表 `Arrays.asList` 是未来 Agent rewrite 的唯一推荐形式。

这是 compiler source-acceptance 差异，不是 classpath、encoding 或 Lombok 缺失。
joLink 不应在 JDT 失败后偷偷混入 javac class，也不应自动修改用户源码。

## 未被本轮证明的能力

- 真实企业项目的 JDT INCREMENTAL；
- unknown Processor 的输入、输出和增量语义；
- changed-class publication；
- HotSwap / fast restart；
- Runtime / HTTP 业务验证；
- Maven compiler invocation 的完整等价重建；
- JDT FULL 性能已经达到产品目标。

JDT FULL 比 Maven baseline 慢不改变 Phase 2A correctness 结论。产品价值要由保留
JavaBuilder state 后的增量耗时和正确性决定。

## 下一步

1. 使用 Maven/javac reference differential 确认 Processor 的真实身份和执行行为；
2. 不删除 fail-closed gate，先证明其 generated source/class/resource 与刷新语义；
3. gate 安全打开后，只做一个普通 method-body mutation；
4. 记录实际编译 source、changed/deleted class、耗时和内存；
5. 使用相同 JDT candidate 的独立 clean FULL 作为 oracle；
6. Incremental correctness 通过后再设计 publication 与 Runtime 集成。

## 临时 patch 的处理边界

已经吸收：

- BuildWorld encoding 动态传入 Eclipse Resources；
- Maven 已确认 classpath artifact 的 zero-class resource archive 语义；
- Tier 1 所选顶层 API attribute 的枚举顺序不影响比较。

不直接吸收：

- 固定 2 GB Worker heap；
- 把全部 raw diagnostics 打到 stderr；
- 仅根据 `.pom` 文件后缀过滤 classpath；
- 公司电脑上多轮 A/B 产生的 lock 文件整体覆盖；
- 将公司 class 名或 API shape 写入可分享报告。
