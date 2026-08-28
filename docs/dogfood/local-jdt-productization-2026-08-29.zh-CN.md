# 本机 JDT 产品化 Dogfood（2026-08-29）

本报告只记录结构化结果，不包含业务源码、依赖凭据、接口数据或私有
路径。所有源码修改均发生在 `/tmp` 下的隔离副本。

## 1. 单模块 Spring H2 项目

环境：Mac、Maven 3.9、Java 11 目标、三个产品 MCP Tool。

### 正常 Make/Build before run

- `java_application.launch` 成功；
- TCP readiness 为 `ready`；
- `java_application.restart` 未携带 `project_path`，约 6 秒完成；
- restart 没有重新执行 Maven；
- `java_application.stop` 正常清理；
- Java 11 超出当前产品 JDT Java 8 模型后，旧的严格 direct FastCompile
  安全回退仍可用。

观测到的本机产品 timing：

```text
Generation seal       ≈ 3.5 ms
source manifest before ≈ 1.4 ms
source manifest after  ≈ 0.5 ms
```

### Make disabled + stale Maven output

隔离副本先保留旧 `target/classes`，再修改方法体并关闭 IDEA Make。

结果：

```text
fast_update.available = false
reason = JDT_RELOAD_REQUIRES_FRESH_MAVEN_BASELINE

reload
→ ok=false
→ JDT_RELOAD_REQUIRES_FRESH_MAVEN_BASELINE
```

没有出现 `no_changes/applied=true` 假成功。

## 2. 继承父 POM 的 Java 8 服务

该项目不是 Maven aggregator module，但通过 `../pom.xml` 继承父 POM，
包含 Lombok、标准 JSR-269 Processor、较大的 compile classpath 和旧企业依赖。

### Maven baseline

```text
严格 offline Maven compile
Java sources: 423
结果: BUILD SUCCESS
耗时: 约 6.45 秒
```

### 未修改源码的产品 JDT FULL

```text
JDT FULL: success
compiled sources: 423
errors: 0
耗时: 约 5.5 秒

Maven declared types: 439
JDT declared types:   439
missing/extra:        0 / 0
class major mismatch: 0
API mismatch:          1
```

唯一 Tier 1 差异来自一个 DTO 泛型参数上的 runtime type-use validation
annotation。javac 与 ECJ 生成的公开 API metadata 不同。产品 gate 正确地
拒绝了该 baseline；这不是 classpath、APT 或 JDT 编译失败。

### 控制变量后的 Incremental

仅在隔离副本移除上述一处 type-use annotation，重新建立 Maven baseline：

```text
JDT FULL: success
Tier 1: compatible
missing/extra/API/major mismatch: 0
FULL 耗时: 约 5.4 秒
```

随后修改一个普通工具类的方法体：

```text
requested source count: 1
compiled source count:  1
candidate class count:  1
errors:                 0
Incremental 耗时:       约 0.29 秒
```

## 3. 额外发现

完整 `java_application.launch` 对该老项目仍保守返回：

```text
ANNOTATION_PROCESSING_OR_BYTECODE_TRANSFORM_UNVERIFIED
```

但同一份 compile classpath、Lombok agent 和两个标准 Processor 已由产品
Worker 完成真实 FULL 与 Incremental。这说明剩余问题位于 Maven 产品模型
的插件/Processor 可证明边界，不是 JDT Worker 能力。后续应根据具体
effective POM 规则收窄拒绝原因，不能笼统放开安全检查。

进一步定位发现唯一拒绝项是：

```text
maven-compiler-plugin optimize=true
```

该参数在当前 Maven Compiler Plugin 中已经是明确提示的 deprecated no-op。
将它加入无语义安全项后，完整产品链路得到：

```text
java_application.launch → runtime_active
jdt_bootstrap_state      → ready
compile_ready            → true
fast_update.strategy     → jdt_incremental

Generation seal          ≈ 108 ms
source manifest before   ≈ 45 ms
source manifest after    ≈ 12 ms
source snapshot           ≈ 57 ms
JDT bootstrap             ≈ 5.8 s
```

随后对带 Lombok 的启动类修改普通方法体。JDT Incremental 成功，但严格的
Maven-Generation/JDT class shape preflight 选择 Candidate Restart；由于该
遗留服务没有可验证 `ready_port`，reload 正确返回：

```text
RELOAD_RESTART_REQUIRES_READINESS
```

没有在应用 readiness 未验证时 promote Candidate。普通无 Lombok工具类的
direct JDT 增量仍然只产生一个 Candidate class。

应用 JVM 随后因外部配置依赖缺失退出，这与编译验证无关。

## 4. 当前结论

- 三 Tool 生命周期和 restart 语义通过本机真实 MCP 验证；
- Make-disabled 假 `no_changes` 已被阻止；
- Java 8、Lombok、JSR-269、423-source JDT FULL 成立；
- Tier 1 gate 能发现真实的 javac/ECJ API metadata 差异；
- 建立兼容 baseline 后，单文件 Incremental 已进入亚秒级并只发布一个
  Candidate class；
- 周一大项目 dogfood 前，不再存在已知 Candidate 分发、attempt 状态丢失或
  Make-disabled 假成功阻塞项。
