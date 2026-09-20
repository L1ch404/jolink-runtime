# Historical research records / 历史研究记录

这里保存早期方案、实验契约和阶段证据。**不要把里面的命令、目录或支持限制
当作当前产品说明。** 当前入口在[文档导航](../README.md)。

旧实现和 Runner 可在清理前提交 `88fe07d` 查看，例如：

```sh
git show 88fe07d:experiments/jdt-incremental-worker/README.md
git show 88fe07d:src/jolink_runtime/experiments/compile.py
```

本轮整理并未删除现有产品回归测试。原 `tests/experiments/` 中的依赖解析、
Worker 可重复构建和产物身份测试迁入 `tests/unit/test_jdt_build_tools.py`；
真实 Gradle 验证复用的 fixture 迁入 `scripts/gradle_test_support.py`。
旧 direct-javac 等退役实现及其专属测试仅保留在 Git 历史，不再作为发布门禁。

Worker、Maven/Gradle Probe 的正式源码已迁入 `java/`，构建脚本迁入 `scripts/`。
这不是替换编译器，也不改变用户已有缓存路径。
