# Gradle G4：Runtime Launch 与持久 Reload

## 已实现链路

G4把G1-G3已经验证的Gradle task-native authority接入现有Runtime产品链路：

```text
IDEA Application / Spring Boot launch intent
                  ↓
Gradle Wrapper + content-checked Probe(scope=runtime)
                  ↓
       只执行导出Task，不执行classes/testClasses
                  ↓
main SourceSet runtimeClasspath + compileJava/resource authority
                  ↓
    持久JDT workspace：FULL或按源码变化INCREMENTAL
                  ↓
  JVM直接使用JDT classes；source resources直接放入classpath
                  ↓
             managed JVM + readiness
                  ↓
       兼容类HotSwap；其他变化要求重新launch
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
JDWP HotSwap。`reload`不再自动重启JVM；JDT/APT生成的resource
delta、HotSwap拒绝或显式`hotswap=false`返回`RELOAD_REQUIRES_RELAUNCH`，保持
当前JVM不变。调用方应执行`stop`后重新`launch`，由JDT编译建立新Runtime。

`src/main/resources`直接在Runtime classpath中，不复制、不扫描、不执行Gradle
processResources。应用重新读取文件可以看到变化；框架已经缓存的配置仍需重新启动。

## Authority边界

G4不是调用`gradle run`或`bootRun`。Gradle只负责导出并建立正式Build World：

- main Java source roots、实际`compileJava.sourceFiles`；
- main classes output、compile/runtime classpath及其顺序；
- Compiler Toolchain、source/target、encoding、`-parameters`；
- Annotation Processor path与Lombok分类；G4首版只放行Lombok；
- main resources output；
- runtime scope完全不读取test SourceSet、compileTestJava、Test classpath或Test秘密；
- 导出Task graph及class output ownership；未知post-compile action仍拒绝；
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
Before launch           IDEA Make/Build不触发Gradle编译
Runtime                 main SourceSet runtimeClasspath
Runtime classpath       JDT bin与resource源码目录，均不做启动副本
Reload                  显式1～16个项目内Java文件
Readiness               launch/restart可通过ready_port验证应用就绪
```

JPMS、多Project、custom SourceSet、Gradle自定义JavaExec/bootRun语义、任意Compiler
args仍fail closed。reload不再做class结构/metadata预检；实际提交JDWP后，
根据JVM的接受或拒绝结果返回。接受不代表静态初始化或框架状态已经刷新。

IDEA的Spring Boot configuration类型可以被导入，但应用了Spring Boot Gradle
plugin的构建仍需单独取得task/action/输出证据；当前插件allowlist会先保守拒绝，
不会假设它与普通Application完全等价。

JVM及后续restart直接引用持久JDT workspace的bin，不读取`build/classes`。
Build World和JDT workspace都会持久化，再次launch直接使用缓存，不自动重验
构建配置。修改build/settings或依赖后，手动清除该项目缓存再launch。
详见`jdt-first-launch.zh-CN.md`。

## 本机真实证据（当前JDT-first链路）

```text
Gradle runtime-scope Probe只执行导出Task             PASS
不产生build/classes或build/resources               PASS
Gradle 8.10 + target Java 8完整闭环                 PASS
Gradle 8.14 + target Java 11 strict offline闭环     PASS
managed JVM TCP readiness                            PASS
方法体增量编译 + JDWP HotSwap                        PASS
结构变化返回RELOAD_REQUIRES_RELAUNCH且JVM不变        PASS
source resource修改后，Runtime新读取直接看到变化     由当前验证脚本覆盖
runtime Probe忽略缺失test依赖与test秘密              PASS
runtime Probe私有JSON立即删除                        PASS
自定义post-compile class修改Task被拒绝               PASS
8.10/Java8与8.14/Java11-offline自动heavy矩阵         PASS
最终stop / owned JVM清理                             PASS
```

验证入口为`scripts/validate_gradle_runtime_product.py`。
