# JDT 私有源码布局适配（2026-09-13）

## 改动

JavaBuilder会根据源码相对source root的位置推导包名。Maven/javac可以显式编译
`a/b/Foo.java`中没有package声明的`Foo`，但同样目录直接交给JavaBuilder会报包名
不匹配。joLink现在根据源码实际package声明安排**已有私有编译副本**，不改用户
的文件内容、目录和资源路径，不修改ECJ内核，不恢复direct-javac。

- 普通`package a.b`文件放到编译根下`a/b/文件名.java`，默认包放在编译根下。
- 只识别源码头部，跳过注释、字符串、注解参数；不构造AST、不解析方法体或类型。
- 沿用main/test及各模块编码。文件内容原样复制，诊断行号不变；诊断路径映射回
  原文件。无法识别的错误package头保留原相对路径，由JDT报告源码错误。
- 包声明变化时删除旧副本并更新新位置；同一批文件的路径交换先统一计算，避免
  一个文件的新副本被另一个文件的旧路径清理删除。
- 保留既有路径冲突错误，不默默覆盖内容不同的源码。
- 原文件仍作为原路径的测试资源使用，例如Checkstyle可以继续读取无package样本。

共享逻辑位于`java_source_layout.py`和现有CompileSession；ModuleCompileSession
提供源码根/编码，复用同一套同步逻辑。没有改Worker/JAR、Maven/Gradle Probe、
工具Schema或JDWP adapter。

## 持久化与增量

仍然只有已有`source-index.json`，记录原文件、编译位置、mtime/size；增加
`source_layout=package-v1`标识。随既有编译保存流程落盘；只改物理位置、不需
触发编译时，也更新索引。没有新增缓存服务或逐文件保存。

- 首次：识别package、放置副本、FULL、保存。
- 无改动重开：恢复索引，沿用既有文件属性检查，不再解析package/复制源码。
- 变更：只读取变更文件；内容没变不重新解析，内容变化才确定编译位置。
- 删除：使用保存的旧路径清理，不读取已删除源码。
- 旧索引：第一次重新识别布局，只有副本位置/内容发生变化才请求增量；不因升级
  布局主动丢弃整个workspace或要求FULL。新索引保存后不重复这项迁移。

## 实测

Checkstyle固定SHA `b54819d1f783a10ca8df13c5987ec0ac28052b3a`，本机Mac，JDK17
构建环境、项目Java11源码，真实stdio MCP调用产品工具：

| 场景 | 结果 |
|---|---|
| 首次 | 4079 sources，actual FULL，0 errors；CommonUtilTest 48/48 |
| 新MCP无修改重开 | 无BUILD；6个相关测试类132/132，约5.23秒（选择范围与首次不同） |
| 一个默认包样本追加末尾注释 | actual INCREMENTAL，1 source，0 errors；15/15，Worker编译约521ms |
| 恢复原文件 | 再次增量，15/15；原项目git状态恢复干净 |

六个测试类覆盖CommonUtil、IllegalInstantiation、PackageDeclaration、ImportControl、
RedundantImport、UncommentedMain；不是Checkstyle全部测试套件。
启动时Fast Test最终汇总中的compiled_source_count未包含bootstrap编译数量，因此
以上FULL/INCREMENTAL数量取自实际`mcp.log`的JavaBuilder结果，不把汇总0解释成无编译。

性能单项测量：读取后的4079份真实源码（约20.9MB），识别全部package三次分别
127.92/127.73/131.84ms；该次读取耗时424.19ms。这是本机局部测量，不是Windows
保证值，也不是整体启动耗时。正常复用不会反复执行这轮全源码解析。

新增真实MCP回归沉淀在`tests/e2e/test_jdt_source_layout.py`：

- 单模块及上游/应用两模块；main/test源码与物理路径不匹配；
- 默认包、非默认包、资源原路径和原始字节；
- 方法体修改、package改变、错误诊断/恢复、删除、重新添加、物理目录移动；
- 旧包class（包含内部类）清理、跨MCP无变更复用；
- 实际应用启动、请求读取结果、原文件路径reload及新请求观察值改变。

增加内部类删除回归时也实际遇到既有reload策略：同一源码产生的`Foo$Nested`
尚未加载时返回`RELOAD_REQUIRES_RELAUNCH / CLASS_NOT_LOADED`，编译本身成功。
测试保留这项断言，随后通过应用请求加载内部类、再次reload并验证值改变。
这项既有HotSwap限制没有在本轮扩大修改，不能把源码布局修复说成同时解决了它。

单元测试补充注释/注解/Unicode escape/编码识别、旧索引迁移、冲突和批量交换、
main/test独立编码；通过禁止调用解析函数确认无变化恢复和显式无变化reload不解析。

收尾：普通测试733 passed / 7 skipped；26项真实MCP回归通过（包含Maven/Gradle
多模块、源码生成、main/test、Processor及本轮布局用例）。随后增加内部类清理和
未加载类边界断言，两项布局MCP用例再次通过。compileall、新文件lint、diff检查、
wheel/sdist打包通过。本轮实际执行环境为本机Mac，不将其写成Windows实测通过。
