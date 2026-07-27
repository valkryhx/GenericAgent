# GA 测试层本地覆盖说明

本文件适用于 `tests/` 目录。它使用 `GA_AGENTS.override.md`，用于验证同层 override 文件名优先于普通 `GA_AGENTS.md` 的语义。

## 测试编写约束

- 新增测试优先使用标准库 `unittest`，除非被测子项目已有其他测试框架。
- 外部服务、真实 LLM、MCP server、子进程长任务都应默认 stub 或显式 opt-in。
- 对 bugfix，优先先写能失败的聚焦回归测试，再实现修复。
- 测试断言应检查可观察行为，不要只检查内部实现细节。

## 真实 API 测试

- 真实 API 测试必须通过环境变量显式启用。
- 当前会话只允许使用 Terra 配置：`terra` / `gpt-5.6-terra` / `hhhl`。
- 测试输出不得打印 API key 或完整私有配置。
