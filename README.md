# GenericAgent

GenericAgent 是一个面向本地开发与自动化任务的多前端 Agent Runtime。当前 `main` 已经不只是早期的最小 Agent 原型：它把 `llm.yaml` 配置层、React/Ink 终端 UI、动态 workflow、MCP/Skill 接入、权限策略、会话转录与上下文压缩整合到同一个轻量 Python 运行时中，目标是在本地可审计、可恢复、可测试地运行真实 LLM Agent。

> 本 README 以当前 `main` 分支的实际能力为准，重点说明最近一轮大更新后的可用特性、运行方式和测试入口。

## 当前重点能力

### 1. 统一 LLM 配置与运行时切换

- 使用 `llm.yaml` 管理 provider、model、profile、默认值与上下文窗口参数。
- 支持 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages 等 wire protocol。
- 支持 profile 级运行时切换：`/model`、`/llm` 可在 UI/CLI 中切换当前会话模型。
- workflow、child agent、Ink bridge 默认跟随主会话 profile，避免 workflow 子任务偷偷回落到旧 `mykey.py` 配置。
- 支持自动上下文压缩阈值、硬限制阈值、thinking/reasoning 参数、流式输出、重试与超时等模型级配置。

### 2. React + Ink 终端 UI 成为默认入口

- `ga` 默认启动 React/Ink 终端 UI；`ga ink` 显式进入 Ink UI。
- `frontends/ink_bridge.py` 通过 JSONL stdio 协议连接 Node/Ink 前端与 Python `GenericAgent` 后端。
- bridge 将后端 stdout/stderr 重定向到日志文件，保证 stdout 始终是可解析 JSONL 协议流。
- 支持会话恢复、转录回放、workflow 详情展示、停止/恢复诊断，以及更稳定的终端渲染路径。
- Windows Terminal/IME 场景下，Ink UI 对原生光标位置有专门处理，便于中文输入候选窗定位。

### 3. 动态 Workflow 与真实子 Agent

- 新增动态 workflow planner/compiler/runtime 基础设施：根据任务语义生成受限 workflow 计划，而不是只依赖固定模板。
- workflow runtime 支持多阶段、多 job、并行/串行任务、artifact、transcript、resume 与失败诊断。
- child agent runner 支持真实 LLM 子任务，并按 profile 继承 `llm.yaml` 配置。
- prompt-guided planner 具备验证与 fallback deterministic 路径，减少 LLM 输出不合法时的中断概率。
- 已覆盖真实 LLM 的 prompt planner E2E：research/review/coding/planning 四类场景可以跑通 workflow 计划生成与 runtime 校验。

### 4. 权限、工具与 MCP 接入

- 主 agent 与 workflow 子 agent 共享读/写工具分类规则。
- 支持 `ask`、`read_only`、`full_access` 等权限模式，并覆盖中途切档、审批循环等测试。
- `mcp_runtime.py` 可从 `mcp.json` 或 `GA_MCP_CONFIG` 加载 MCP server，并将工具规范化为 `mcp__<server>__<tool>`。
- 静态工具 schema、MCP 工具、Skill/Memory/Distillation 工具可以按场景组合暴露。
- 遇到取消/停止时，MCP 调用会进入 cancellation scope，降低半关闭调用遗留状态的风险。

### 5. 会话转录、恢复与上下文压缩

- `session_transcript.py` 记录 JSONL 会话事件：`session_start`、`turn`、`compact`、`rewind`。
- 后端历史可在每轮前精确恢复，支持 rewind/resume 场景。
- `/compact` 手动压缩和自动压缩共用 `compact_context.py` 核心逻辑。
- 压缩会把长历史替换为摘要 pair，并可更新 legacy log，便于长任务继续运行。

### 6. 多前端与集成入口

- `ga`：默认 Ink UI。
- `ga ink`：React/Ink 终端 UI。
- `ga cli`：纯 Python CLI。
- `ga list`：查看/选择已有会话。
- `python launch.pyw`：桌面启动入口。
- `python frontends/tuiapp.py`：Textual TUI。
- `streamlit run frontends/stapp2.py`：Streamlit Web UI。
- `frontends/genericagent_acp_bridge.py`：供 Codeg 等外部桌面/Web UI 作为本地 ACP Agent 调用。

## 快速开始

### 1. 安装 Python 包

```bash
python -m pip install -e .
```

如果要使用 Ink UI：

```bash
python -m pip install -e ".[ui]"
```

Windows 下也可以在仓库根目录运行：

```bash
install_ink_ui.cmd
```

### 2. 配置 LLM

复制模板并填写本地密钥：

```bash
cp mykey_template.py mykey.py
```

当前推荐优先使用 `llm.yaml` 管理真实 provider/profile。`mykey.py` / `mykey.json` 仍可作为兼容配置来源，但新的 workflow 与 model/session 选择路径正在向 `llm.yaml` 收敛。

最小概念结构：

```yaml
providers:
  provider-name:
    wire_api: openai_chat
    base_url: https://example.com/v1
    api_key: ${YOUR_API_KEY_ENV}

models:
  model-name:
    provider: provider-name
    api_model: real-model-id
    context_window: 200000

profiles:
  default:
    model: model-name

active_profile: default
```

> 不建议把真实密钥提交到仓库。`llm.yaml`、`mykey.py`、运行日志和临时产物应保持本地私有。

### 3. 启动

```bash
ga
```

或选择具体前端：

```bash
ga ink
ga cli
ga list
python launch.pyw
python frontends/tuiapp.py
streamlit run frontends/stapp2.py
```

Ink UI 的 npm 命令需要在 `frontends/ink-ui` 下执行：

```bash
cd frontends/ink-ui
npm install
npm run start
```

## 常用命令

### Python 测试

```bash
python -m unittest discover -s tests
python -m unittest tests.test_ink_bridge
python -m unittest tests.test_workflow_prompt_guided_planner
```

### Ink UI 测试

```bash
cd frontends/ink-ui
npm run test
npm run typecheck
```

### 真实 LLM E2E 示例

真实 API 测试需要本地 `llm.yaml` 已配置可用 provider/profile，并显式 opt-in。例如使用 `terra` profile：

```bash
GA_RUN_REAL_PROMPT_PLANNER_E2E=1 GA_WORKFLOW_LLM_PROFILE=terra GA_REAL_API_PROFILE=terra GA_REAL_API_CONFIG=terra GA_REAL_API_EXPECTED_MODEL=gpt-5.6-terra GA_REAL_API_EXPECTED_NAME=terra python tests/prompt_guided_planner_real_e2e.py
```

更多真实 API 诊断入口位于 `tests/*real*e2e*.py`，默认不会在普通单元测试中触发。

## 项目结构

```text
agentmain.py                 # GenericAgent 主体：任务队列、模型/会话选择、slash commands
agent_loop.py                # LLM turn loop 与工具调用调度
ga.py                        # 本地工具实现与 GenericAgentHandler
llm_config.py                # llm.yaml 的 pydantic 配置层
llm_client.py                # 从 llm.yaml 构造 runtime client/session
llmcore.py                   # provider session、流式解析、历史/context 行为
workflow_*.py                # 动态 workflow planner/compiler/runtime/权限/子 agent
mcp_runtime.py               # MCP server 加载、工具发现、取消域
skills_runtime.py            # Claude/Codex 风格 Skill 发现
session_transcript.py        # JSONL 会话转录、恢复、rewind/compact 事件
compact_context.py           # 自动/手动上下文压缩
frontends/ink_bridge.py      # Python 后端与 React/Ink UI 的 JSONL bridge
frontends/ink-ui/            # React 18 + Ink 5 终端 UI
tests/                       # Python unittest 与真实 API 诊断入口
```

## 测试状态与质量基线

当前主干的核心回归覆盖包括：

- Python `unittest` discovery。
- Ink UI Node test runner 与 TypeScript typecheck。
- `llm.yaml` 配置解析、profile/model 解析、runtime model switching。
- workflow planner、compiler、runtime、resume、partial failure、permission policy。
- Ink bridge 的 JSONL 协议、会话恢复、workflow 启停与详情展示。
- 真实 LLM prompt planner E2E（需本地密钥与显式 opt-in）。

对终端 UI 问题，优先写程序化回归测试：虚拟终端、ANSI 字节断言、frame geometry、`string-width` CJK/emoji 宽度断言，而不是只靠截图。

## 安全边界

GenericAgent 可以调用本地文件、命令、浏览器、MCP 工具和真实 LLM。使用时请注意：

- 不要把真实 API key、session transcript、MCP 配置或运行日志提交到公开仓库。
- 真实机器上的写操作、命令执行、MCP mutating tool 应启用审批或只读模式。
- 不要执行、生成、写入或持久化可疑 payload、广告注入、自启动项、注册表 Run 项、VBS/PowerShell 注入脚本或恶意代码。
- 对未知来源的 Skill、MCP server、workflow artifact 做只读审计后再启用。

## 许可证

本项目沿用仓库中的许可证文件。使用第三方前端、MCP server、模型 provider 或外部服务时，请同时遵守对应项目和服务条款。
