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
main SourceSet runtimeClasspath + compileJava authority
                  ↓
       sealed Generation → managed JVM + readiness
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
JDWP HotSwap；结构、metadata、资源或显式`hotswap=false`进入Candidate Restart。
Candidate只有在`ready_port`成功后才promote；启动失败自动恢复last-good
Generation并返回`rolled_back=true`。

## Authority边界

G4不是调用`gradle run`或`bootRun`。Gradle只负责导出并建立正式Build World：

- main Java source roots、实际`compileJava.sourceFiles`；
- main classes output、compile/runtime classpath及其顺序；
- Compiler Toolchain、source/target、encoding、`-parameters`；
- Annotation Processor path与Lombok分类；G4首版只放行Lombok；
- main resources output；
- build/settings脚本、version catalog、Wrapper、用户init scripts、相关环境。

IDEA配置继续提供main class、working directory、JVM/program args、环境和Runtime
JDK意图。因此普通`Application`和Spring Boot IDEA配置走同一条直接main-class
启动路径，不复制`bootRun`的自定义Task行为。

## G4 v0.1边界

```text
Gradle                  Wrapper 8.10 / 8.14
Project                 单Project；拒绝buildSrc/build-logic/composite
SourceSet               标准main Java/resource roots；无include/exclude
Java                    source=target 8或11；release为空
Compiler args           空或仅-parameters；fork/provider暂拒绝
Processor               仅无Processor或Lombok；其他Processor暂拒绝
Launch intent           IDEA Application或Spring Boot配置
Before launch           IDEA Make/Build必须启用
Runtime                 main SourceSet runtimeClasspath
Reload                  显式1～16个项目内Java文件
Readiness               Candidate Restart/rollback必须配置ready_port
```

JPMS、多Project、custom SourceSet、Gradle自定义JavaExec/bootRun语义、任意Compiler
args仍fail closed。跨编译器metadata不同（例如某些Java 11 `InnerClasses`输出）会
安全选择Candidate Restart，而不会把不确定class强行发给JDWP。

## 本机真实证据（2026-08-30）

```text
Gradle 8.14 runtime-scope Probe只执行classes       PASS
Java 11 formal output vs JDT FULL Tier 1            PASS
Gradle 8.10 + target Java 8完整闭环                 PASS
Gradle 8.14 + target Java 11 strict offline闭环     PASS
managed JVM TCP readiness                            PASS
方法体增量编译 + JDWP HotSwap                        PASS
结构变化 + Candidate Restart                        PASS
Candidate启动失败 + last-good rollback              PASS
最终stop / owned JVM清理                             PASS
```

验证入口为`scripts/validate_gradle_runtime_product.py`。
