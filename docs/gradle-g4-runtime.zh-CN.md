# Gradle G4：Runtime Launch 与持久 Reload

## 已实现链路

G4把G1-G3已经验证的Gradle task-native authority接入现有Runtime产品链路：

```text
IDEA Application / Spring Boot launch intent
                  ↓
Gradle Wrapper + content-checked Probe(scope=runtime)
                  ↓
             classes（不执行testClasses）
                  ↓
main SourceSet runtimeClasspath + compileJava/resource authority
                  ↓
  classes + resources无冲突合并成sealed Generation
                  ↓
             managed JVM + readiness
                  ↓
        persistent JDT FULL / INCREMENTAL
                  ↓
        HotSwap 或 Candidate Restart / rollback
```

没有新增Tool或Action。调用仍然是：

```json
{
  "action": "launch",
  "project_path": "/workspace/gradle-project",
  "launch_name": "Application",
  "jdwp_port": 5005,
  "ready_port": 8080
}
```

随后使用已有`reload(source_files, hotswap)`。方法体且class schema兼容时使用
JDWP HotSwap；结构、metadata、JDT/APT生成的resource delta或显式
`hotswap=false`进入Candidate Restart。
Candidate只有在`ready_port`成功后才promote；启动失败自动恢复last-good
Generation并返回`rolled_back=true`。

`src/main/resources`变化不会伪装成一次Java reload。它会使当前Build World失效，
要求重新launch/Bootstrap；新的formal resource output只有在重新seal并通过readiness
后才成为current Generation。

## Authority边界

G4不是调用`gradle run`或`bootRun`。Gradle只负责导出并建立正式Build World：

- main Java source roots、实际`compileJava.sourceFiles`；
- main classes output、compile/runtime classpath及其顺序；
- Compiler Toolchain、source/target、encoding、`-parameters`；
- Annotation Processor path与Lombok分类；G4首版只放行Lombok；
- main resources output；
- runtime scope完全不读取test SourceSet、compileTestJava、Test classpath或Test秘密；
- `classes`实际Task graph及class output ownership；未知post-compile task拒绝；
- build/settings脚本、version catalog、Wrapper、用户init scripts、相关环境。

IDEA配置继续提供main class、working directory、JVM/program args、环境和Runtime
JDK意图。因此普通`Application`和Spring Boot IDEA配置走同一条直接main-class
启动路径，不复制`bootRun`的自定义Task行为。

## G4 v0.1边界

```text
Gradle                  Wrapper 8.10 / 8.14
Project                 单Project；拒绝buildSrc/build-logic/composite
Plugins                 当前验证Gradle内置Java/Application插件集合
SourceSet               标准main Java/resource roots；无include/exclude
Java                    source=target 8或11；release为空
Compiler args           空或仅-parameters；fork/provider暂拒绝
Processor               仅无Processor或Lombok；其他Processor暂拒绝
Launch intent           IDEA Application或Spring Boot配置
Before launch           IDEA Make/Build必须启用
Runtime                 main SourceSet runtimeClasspath
Generation              classes/resources连续且无路径冲突后合并seal
Reload                  显式1～16个项目内Java文件
Readiness               Candidate Restart/rollback必须配置ready_port
```

JPMS、多Project、custom SourceSet、Gradle自定义JavaExec/bootRun语义、任意Compiler
args仍fail closed。跨编译器metadata不同（例如某些Java 11 `InnerClasses`输出）会
安全选择Candidate Restart，而不会把不确定class强行发给JDWP。

IDEA的Spring Boot configuration类型可以被导入，但应用了Spring Boot Gradle
plugin的构建仍需单独取得task/action/输出证据；当前插件allowlist会先保守拒绝，
不会假设它与普通Application完全等价。

Gradle Runtime Bootstrap同时冻结Java和resource source manifest，要求pre、post、
private snapshot三方一致。formal classes/resources在seal前后也按完整相对路径和
SHA-256验证。JVM及后续restart只引用Generation私有输出，不再引用可变的
`build/classes`或`build/resources`目录。

## 本机真实证据（2026-08-31）

```text
Gradle 8.14 runtime-scope Probe只执行classes       PASS
Java 11 formal output vs JDT FULL Tier 1            PASS
Gradle 8.10 + target Java 8完整闭环                 PASS
Gradle 8.14 + target Java 11 strict offline闭环     PASS
managed JVM TCP readiness                            PASS
方法体增量编译 + JDWP HotSwap                        PASS
结构变化 + Candidate Restart                        PASS
Candidate启动失败 + last-good rollback              PASS
外部修改/删除formal resource后restart仍读取sealed v1 PASS
resource source drift使Build World失效               PASS
runtime Probe忽略缺失test依赖与test秘密              PASS
runtime Probe私有JSON立即删除                        PASS
自定义post-compile class修改Task被拒绝               PASS
8.10/Java8与8.14/Java11-offline自动heavy矩阵         PASS
最终stop / owned JVM清理                             PASS
```

验证入口为`scripts/validate_gradle_runtime_product.py`。
