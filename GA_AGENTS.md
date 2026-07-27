# GA 运行时项目说明

本文件只供 GenericAgent 自身运行时读取。它刻意命名为 `GA_AGENTS.md`，避免和给外部 agent / Claude Code / Codex 阅读的 `AGENTS.md` 混用。

## 稳定项目地图

- 核心运行时位于仓库根目录：`agentmain.py`、`agent_loop.py`、`ga.py`、`llmcore.py`、`llm_config.py`、`llm_client.py`。
- 本地工具行为主要在 `ga.py`，工具分发遵循 `do_<tool_name>` 模式。
- LLM provider/profile 从 `llm.yaml` 加载，配置解析和会话构造主要经过 `llm_config.py` / `llm_client.py`。
- Ink 桥接入口是 `frontends/ink_bridge.py`；React/Ink UI 位于 `frontends/ink-ui`。
- 长期记忆、SOP 和沉淀工具位于 `memory/`；运行期日志、会话、transcript 和临时产物位于 `temp/`。

## 测试规则

- Python 测试使用标准库 `unittest`，测试文件位于 `tests/`。
- 修改运行时路径时，先跑聚焦测试，再扩大到 `python -m unittest discover -s tests`。
- Ink UI 的 npm 命令必须在 `frontends/ink-ui` 目录运行，不要在仓库根目录运行。
- 真实 LLM API 测试必须显式 opt-in。当前 Terra 路径只能使用 `llm.yaml` 中的 `terra` / `gpt-5.6-terra` / `hhhl`，除非用户明确授权其他 provider 或 model。

## 安全与配置

- 不要提交真实 API key、`mykey.py`、`mykey.json`、私有 `mcp.json` 或本地凭据文件。
- 输出诊断信息时要遮蔽 `api_key`、token、secret、password 等敏感值。
- 禁止执行、生成、写入或持久化可疑 base64 载荷、公益 token 广告、弹窗广告、自启动项、计划任务、注册表 Run 项、VBS/PowerShell 注入脚本或恶意/入侵代码。遇到此类内容，只能做只读检查、解码说明、定位和删除。

## GA_AGENTS.md 分层语义

- GA 从 workspace root 到当前目录依次加载 `GA_AGENTS.md` / `GA_AGENTS.override.md`。
- 不同目录层级是追加关系，顺序为根目录到当前目录。
- 同一目录内，`GA_AGENTS.override.md` 替代该目录的 `GA_AGENTS.md`。
- 如果说明冲突，遵守后出现、路径更具体的局部说明。
