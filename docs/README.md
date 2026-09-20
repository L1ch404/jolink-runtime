# joLink 文档导航

## 当前产品

- [英文安装说明](../INSTALL.md) / [中文安装说明](../INSTALL.zh-CN.md)
- [Agent Skill](../skills/jolink-java/SKILL.md)
- [MCP 工具与调用语义](mcp-contract-v0.1.md)
- [项目启动流程](project-launch-contract-v0.1.md)
- [JDT-first 启动](jdt-first-launch.zh-CN.md)、[Maven 多模块](jdt-modules.zh-CN.md)、[Gradle 多模块](gradle-modules.zh-CN.md)
- [编译感知 restart / HotSwap](project-restart.zh-CN.md)
- [Fast Test](fast-test-v0.1.zh-CN.md)
- [编译状态持久化](jdt-workspace-persistence.zh-CN.md)、[编译诊断日志](jdt-build-diagnostics.zh-CN.md)
- [JDT / Worker JDK](jdt-346-upgrade.zh-CN.md)、[下载源](runtime-download-mirror.zh-CN.md)、[内存与 GC](jdt-worker-memory.zh-CN.md)
- [尚未解决的兼容性问题](java-compatibility-followup-2026-09.md)
- [正式 Java 源码与构建](../java/README.md)

## 历史和设计背景

[研究记录归档](archive/README.md)保留早期 direct-javac、JDT POC、APT、
Maven Probe 和 Gradle 分阶段实验。它们记录当时的结论，不是当前版本的安装、
操作步骤或支持边界。`dogfood/` 中的日期报告同样是对应版本的实测记录。

研究 Runner 和退役的 Python `experiments` 包已从产品主线移除。历史源码可从
清理前提交 `88fe07d` 或对应实验分支查阅；不需要为了运行产品保留这些实现。
