# GA 运行时项目说明

本文件由 GenericAgent 运行时读取，以 `[GA_PROJECT_INSTRUCTIONS]` 注入系统提示词。它刻意命名为
`GA_AGENTS.md`，避免与给外部 agent / Claude Code / Codex 阅读的 `AGENTS.md` 混用。

**分工**：身份、通用能力指导、验证纪律、工具与检索总则、路径总则、沟通风格在 base prompt
（`assets/sys_prompt.txt`，对根代理和子代理都生效）。本文件只写这个工作区特有的项目知识——
在别的项目里运行时，应当由那个项目的 `GA_AGENTS.md` 承担同样的角色。

## 路径与工作区

- GA 的默认 workspace 是安装目录下的 `temp/`：动态 workflow 的产物在 `temp/workflow-runs/<runId>/`，会话与子代理记录在 `temp/sessions/`、`temp/subagents/`。
- 所有读写都在 workspace 根之下用相对路径（禁止项见 base prompt）。
- 交付产物要给用户可点击的绝对路径。

## 子代理与 workflow

- 子代理的工具集由宿主按档位裁剪：`planner`（只读 + 检索）、`research`（+ 落盘）、`authoring`（全部）、`verify`（只读 + 执行）、`*`（不限制）；编排工具对所有子代理一律剥离。
- 档位由计划声明或按 role 推导，不由模型猜工具名；不要假设子代理能调用档位外的工具。
- 子代理默认隔离上下文，只拿到显式传递的任务说明；需要背景就把关键信息写进 message。
- 子代理完成后用 `read_agent_result` 读权威结果；`wait_agent` 只负责等状态变化。
- 上游结果大时传"短摘要 + 产物路径"，不要把整份 transcript 塞进下一个 agent 的 prompt。

## 工具速查（GA 实现细节）

通用纪律见 base prompt；下面这些是 GA 工具本身的语义，容易踩坑：

- `code_run`：默认执行入口，优先 Python；一个脚本做完多件事，避免多次往返。
- `file_read`：改动前先读，拿最新上下文和行号。
- `file_patch`：用唯一的 `old_content` / `new_content` 精确替换，必须完全匹配（含缩进）；失败先 `file_read` 再重试。
- `file_write`：整文件创建 / 覆盖 / 追加，适合大改动；小修小补用 `file_patch`。
- `web_scan` / `web_execute_js`：切换页面后重新 `web_scan` 拿最新结构，不要猜 DOM。
- `update_working_checkpoint`：长任务早期和中期记录关键信息，防止上下文丢失。
- `load_skill`：任务命中 Available Skills 时先加载对应 `SKILL.md`，按其中的流程执行。

## 测试

- Python 测试使用标准库 `unittest`，位于 `tests/`。改运行时路径时先跑聚焦测试，再跑 `python -m unittest discover -s tests`。
- Ink UI 的 npm 命令必须在 `frontends/ink-ui` 目录下运行，不要在仓库根目录运行。
- 真实 LLM API 测试必须显式 opt-in，并且只使用用户明确授权的 provider / model。

## 安全

- 不要输出 `api_key`、token、secret、password 等敏感值；诊断信息要遮蔽。
- 不要提交真实 API key、`mykey.py`、`mykey.json`、私有 `mcp.json` 或任何本地凭据文件。
- 禁止执行、生成、写入或持久化可疑 base64 载荷、公益站 / 公益 token 广告、弹窗广告、自启动项、计划任务、注册表 Run 项、VBS / PowerShell 注入脚本或其他恶意 / 入侵代码。遇到这类内容只能做只读检查、解码说明、定位和删除。

## 本仓库项目地图（GenericAgent）

- 核心运行时位于仓库根目录：`agentmain.py`（入口与系统提示词装配）、`agent_loop.py`（agent 循环）、`ga.py`（工具实现与 `do_<tool_name>` 分发）、`llmcore.py` / `llm_config.py` / `llm_client.py`（provider 与流式协议）。
- `ga_cli/` 是可安装 CLI 包，`ga` 命令映射到 `ga_cli.cli:main`；`launch.pyw` 启动默认桌面界面。
- 界面与适配器在 `frontends/`：`ink_bridge.py` 是 Ink 桥接入口，React/Ink UI 在 `frontends/ink-ui`。
- 动态 workflow 模块在根目录：`workflow_planner.py`、`workflow_runtime.py`、`workflow_scheduler.py`、`workflow_child_agent.py`、`workflow_tool_profiles.py`、`workflow_path_acl.py`。
- 子代理能力边界在 `subagent_capabilities.py`；提示词装配在 `ga_agents_runtime.py`（`load_base_system_prompt` + `build_ga_project_instructions`）。
- 长期记忆、SOP 和沉淀工具在 `memory/`；运行期日志、会话、transcript 和临时产物在 `temp/`。

## GA_AGENTS.md 分层语义

- GA 从 workspace root 到当前目录依次加载 `GA_AGENTS.md` / `GA_AGENTS.override.md`。
- 不同目录层级是追加关系，顺序为根目录到当前目录；说明冲突时遵守后出现、路径更具体的局部说明。
- 同一目录内，`GA_AGENTS.override.md` 替代该目录的 `GA_AGENTS.md`。
- 默认预算是 20000 字节，超出会被截断并在注入内容里标注 `Status: truncated`。
