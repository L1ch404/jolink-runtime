# 发布 PyPI 与官方 MCP Registry

仓库使用 GitHub Actions 的 **Publish prerelease** 工作流，手动触发一次：

```text
从 pyproject.toml 自动同步 Registry 版本
→ 构建 wheel/sdist
→ Windows 独立安装后的真实 MCP/JVM 验证
→ PyPI Trusted Publishing
→ MCP Registry GitHub OIDC 发布
→ 回读 Registry 中对应版本的条目
```

## 发布前

1. 更新 `pyproject.toml`、`src/jolink_runtime/__init__.py`、`uv.lock` 和当前版本文档。
2. **不用手动更新 `server.json` 的版本号。** 发布工作流在构建前运行
   `python scripts/prepare_registry_metadata.py`，从 `pyproject.toml` 的
   `project.version` 自动填入顶层 `version` 和 `packages[0].version`。
   本地检查发布元数据时也可以运行同一命令；仓库里这两个字段的旧值会被覆盖。
3. 英文 README 保留所有权标记 `mcp-name: io.github.L1ch404/jolink-runtime`。
   PyPI 使用这份 README 作为包描述；只改 GitHub 页面不能补到已发布的旧包中。
4. 完成本地验证，把发布提交推到 `main`，确认该提交的 CI 通过。

`server.json` 登记的是 PyPI 包和本地 `uvx`/stdio 启动方式，配套 Skill 仍按安装文档
单独安装。下载源默认 `official`，国内用户可选 `cn`；编译后内存回收默认启用。

## 在 GitHub 上操作

打开仓库 **Actions → Publish prerelease → Run workflow**，选择 `main`。
这是手动工作流，不由创建 GitHub Release 或推送 tag 自动触发。

如果 `pypi` environment 要求审批，按页面提示批准。PyPI 发布沿用已有的 Trusted
Publishing；Registry 使用工作流的 GitHub OIDC 身份认证，无需额外保存 Registry Token，
也无需在本机先执行 `mcp-publisher login`。

发布任务使用同一份构建产物里的 `server.json`。只有 Windows 验证和 PyPI 发布均成功，
才会运行 Registry 发布。工作流最后回读准确的 Registry 名称、版本、包和启动方式。

本轮版本为 `0.1.0a7`，发布后检查：

- [PyPI 0.1.0a7](https://pypi.org/project/jolink-runtime/0.1.0a7/)
- [官方 MCP Registry 0.1.0a7 条目](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.L1ch404%2Fjolink-runtime/versions/0.1.0a7)

## 某一步失败时

- 构建或 Windows 验证失败：修复后重新运行，尚未上传 PyPI。
- PyPI 已成功、Registry 发布失败：检查失败日志，仅重跑失败的 Registry job，避免重复
  上传同一 PyPI 版本。如果日志显示 Registry 发布已成功、只是回读失败，先查询条目；
  已存在且信息正确就无需再次发布该版本。
- 某个版本已经发布：其包和 Registry 版本信息不能原地替换；后续内容变更使用新版本。

依据：[官方 GitHub Actions 发布说明](https://modelcontextprotocol.io/registry/github-actions)、
[PyPI 所有权验证](https://modelcontextprotocol.io/registry/package-types)、
[Registry 版本规则](https://modelcontextprotocol.io/registry/versioning)。
