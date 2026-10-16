# GA 项目目录重构方案

> 状态：**方案已落盘，尚未实施代码迁移**
> 日期：2026-10-16
> 目标仓库：`D:\git_codes\GenericAgent`
> CC 证据仓库：`D:\git_codes\claudecode283_hack`
> 关联记录：`AGENTS.md` → `### 中途调研 2026-10-16：GA 项目目录扁平化 / 对齐 CC 的逻辑分层`

---

## 0. 执行摘要

GA 当前把绝大多数 Python 实现平铺在仓库根目录。以本次盘点为准：

| 项目 | 当前数量/事实 |
| --- | ---: |
| 根级 `*.py` | **73** |
| 根级 `workflow_*.py` | **18** |
| 根级 `subagent_*.py` | **25** |
| 根级 LLM/API 相关文件 | **6**（`llmcore.py`、`llm_client.py`、`llm_config.py`、`mykey*.py` 等） |
| 根级 workflow JS 资源 | **2**（`workflow_js_worker.js`、`workflow_script_contract.js`） |
| 已存在的结构化目录 | `ga_cli/`、`frontends/`、`tests/`、`skills/`、`memory/`、`plugins/`、`reflect/`、`scripts/`、`assets/` |

结论：

1. **CC 的原始目录树不可恢复**。Claude Code 2.1.283 是 Bun `--compile` 产物，源码以 `mNNNN_0x...js` 模块形式存在于 `.bun` 段；`mNNNN` 是提取索引，不是原始文件名，且没有可用的 sourcemap 还原原始目录。
2. 因此本方案**不伪造 CC 目录名**，而是从 CC 源码的模块职责和调用链中提炼逻辑分层，再映射到 GA 的 Python 包结构。
3. 目标结构采用 `genericagent/` 主包，按 `core`、`llm`、`mcp`、`agents`、`workflow`、`web`、`media`、`resources` 分域；`ga_cli/` 保留为稳定 CLI 门面，根目录最终只保留薄入口与兼容 shim。
4. 迁移必须分阶段进行：先建立路径解析层与兼容层，再按领域 `git mv`，每一批单独测试、单独提交。**禁止一次性移动全部文件。**
5. 本方案不改变 agent 行为、不重写 workflow、不实现 Claude Code 云端会话。云端会话仍按项目既有边界处理：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。

---

## 1. 调研结论：CC 能提供什么结构证据

### 1.1 CC 原始目录不可恢复

证据链：

- `claudecode283/extracted/README.md` §“怎么复现的”说明：源码是从 `claude.exe` 的 `.bun` 段切出的 2,135 个明文模块，再按关键字筛出 workflow 相关模块。
- `claudecode283/modules/INDEX.tsv` 的模块名形如 `m1502_0xdb4a986.js`，这是提取器编号与偏移，不是 Claude Code 开发仓库中的原始路径。
- Bun 单文件编译会重写模块边界和标识符，但字符串、属性名、对象键和模板字面量大量保留；因此我们能识别“模块职责”，不能还原“原仓库目录”。

**对本方案的约束**：GA 可以对齐 CC 的逻辑边界，但不能声称复刻了 CC 的真实目录树，也不应创建 `claudecode/`、`m1502/` 这类伪路径。

### 1.2 CC workflow 的逻辑分层

主要证据：

- `claudecode283/extracted/README.md` 的模块地图。
- `claudecode283/extracted/dynamic-workflow-internals.md` 的入口、执行、持久化、resume、UI 章节。
- `reconstructed/GLOSSARY.md` §1 的 workflow 模块地图。

可辨识职责：

| CC 逻辑职责 | 证据模块/坐标（摘要） | 对 GA 的目录映射 |
| --- | --- | --- |
| Workflow 工具入口、schema、权限校验 | `m1569`、`m1502` | `genericagent/workflow/controller.py`、`models.py` |
| JS 脚本解析与 meta 契约 | `m0862` | `genericagent/workflow/script_validation.py`、`js/script_contract.js` |
| workflow 执行引擎、VM、并发、预算、journal、resume | `m0869` | `genericagent/workflow/runtime.py`、`scheduler.py`、`store.py` |
| launch 状态机 | `m1372` | `genericagent/workflow/controller.py`、`models.py` |
| 详情进度树、`/workflows` 历史、状态行 | `m1460`、`m1906`、`m2000`、`m1456` | `frontends/ink/` 与 bridge 的 UI 层 |
| 后台任务、跨会话 adopt | `m1478` | `genericagent/workflow/store.py` + `frontends/ink/bridge.py` |

### 1.3 CC Agent Team 的逻辑分层

主要证据：

- `claudecode283/extracted/agent-team-internals.md` §5 模块地图。
- `reconstructed/deminified-agent-team/` 专项可读化副本。
- `claudecode283/extracted/hooks-internals.md` 中 `TeammateIdle`、`TaskCreated`、`TaskCompleted` 等事件证据。

可辨识职责：

| CC 逻辑职责 | 证据模块/符号 | 对 GA 的目录映射 |
| --- | --- | --- |
| teammate spawn 与后端路由 | `m1578 spawnTeammate` | `genericagent/agents/manager.py`、`worktree.py` |
| Agent 工具、team_name 分支 | `m0511` | `genericagent/core/handler.py` 的 agent 工具注册层 |
| In-process teammate task | `m0527` | `genericagent/agents/manager.py`、`tasks.py` |
| 任务板 `TaskCreate/Get/Update/List` | `m0271`、`m0376`、`m0377`、`m0531` 等 | `genericagent/agents/tasks.py` |
| `SendMessage` 与接收者解析 | `m1548`、`m1549`、`m1344`、`m0261` | `genericagent/agents/mailbox.py`、`ipc.py`、`realtime_ipc.py` |
| team 文件持久化 | `m1614`、`m0319` | `genericagent/agents/registry.py`、`state.py` |

### 1.4 CC hooks/权限/隔离提供的横向分层证据

- `claudecode283/extracted/hooks-internals.md` §1 表明：执行引擎、匹配、spawn、超时、异步注册表、结果分发和安全门禁是独立逻辑职责。
- `dynamic-workflow-internals.md` 对 permissions、workspace、resume、journal 的拆分，支持 GA 把权限、工作区、持久化、执行引擎拆开，而不是继续堆在根目录。

### 1.5 结论

GA 应对齐的是**职责边界**，不是 CC 的压缩模块号：

```text
入口/命令层
   ↓
编排/运行层
   ↓
执行/隔离/权限层
   ↓
持久化/转录层
   ↓
UI/桥接层
```

当前 GA 的问题是这些层都落在根目录，导致：
- 同名前缀只能靠命名区分，不能靠目录边界约束；
- `agentmain.py`、`ga.py` 同时承担入口、工具实现、子代理编排、路径解析；
- `workflow_*`、`subagent_*`、`llm*` 的依赖边难以审计；
- 测试和前端大量使用根模块名，重构缺少兼容面。

---

## 2. GA 现状事实与风险

### 2.1 根目录职责混装

根目录同时存在：

- 应用入口：`agentmain.py`、`ga.py`、`ga`、`ga.cmd`、`launch.pyw`、`hub.pyw`；
- Agent runtime：`agent_loop.py`、`agent_runtime_models.py`、`agent_control*.py`；
- LLM：`llmcore.py`、`llm_client.py`、`llm_config.py`、`mykey*.py`；
- MCP：`mcp_runtime.py`；
- subagent/team：25 个 `subagent_*.py`；
- workflow：18 个 `workflow_*.py` 与 2 个 JS 资源；
- web/media：`TMWebDriver.py`、`simphtml.py`、`image_codec.py`、`image_gc.py`、`listing_cache.py`；
- 横切能力：权限、脱敏、skill、transcript、compact、token meter、tool result budget。

### 2.2 已确认的关键耦合

根级 AST 依赖扫描显示：

- `agentmain.py` 直接依赖 `agent_loop`、`ga`、`llmcore`、`llm_client`、`mcp_runtime`、权限、skills 和大量 `subagent_*`。
- `ga.py` 直接依赖 `agent_loop`、`mcp_runtime`、`skills_runtime`、`subagent_manager`、`workflow_*`。
- `workflow_child_agent.py` 依赖 `agent_loop`、`ga`、`mcp_runtime`、`skills_runtime`、`workflow_*`。
- `tool_result_budget.py` 内部延迟导入 `workflow_child_agent`，这是现有循环依赖的规避手段。
- `workflow_scheduler.py`、`workflow_store.py` 依赖 `subagent_state`。
- `subagent_permissions.py`、`subagent_roles.py` 依赖 `workflow_permissions`。
- `subagent_team_activation.py` 依赖 `workflow_activation`。
- `workflow_tool_profiles.py` 依赖 `subagent_capabilities`。

这意味着不能只按文件名移动；需要先把跨域契约和路径解析抽出来，再迁移实现。

### 2.3 打包事实

`pyproject.toml` 当前是：

```toml
[tool.setuptools]
py-modules = []
packages = ["ga_cli"]
```

因此：

- 新增 `genericagent/` 后必须改为包发现；
- 如果 JS、schema、prompt 模板作为包资源发布，必须配置 `package-data`；
- 如果继续只发布 `ga_cli`，迁移后的包无法通过 wheel 正常安装；
- `ga_cli/__init__.py` 当前通过 `importlib.util.spec_from_file_location` 从根 `ga.py` 加载核心类，重构后必须改为稳定的包导入，否则会制造重复模块实例和类身份不一致。

### 2.4 路径风险

当前多处以 `__file__` 或根目录相对路径定位资源：

- `agentmain.py`：`assets/`、`temp/`、`memory/`、`llm.yaml`；
- `ga.py`：`assets/code_run_header.py`、`temp/`、`memory/`；
- `llm_config.py` / `llmcore.py`：`llm.yaml`、`mykey.json`、`temp/model_responses/`；
- `mcp_runtime.py`：`mcp.json`、`temp/mcp_logs/`、`temp/mcp_tools_cache.json`；
- `session_transcript.py` / `workflow_store.py`：`temp/sessions/`；
- `skills_runtime.py`：`skills/`；
- `image_gc.py`：`temp/ga-images/`；
- `workflow_runtime.py`：`workflow_js_worker.js` 同目录；
- `workflow_script_validation.py`：`workflow_script_contract.js` 同目录；
- `subagent_manager.py`：默认从 `__file__` 所在目录启动 `agentmain.py`。

**迁移前必须引入 `genericagent/paths.py`，否则文件一移动，资源定位就会静默指向错误目录。**

---

## 3. 目标目录结构

### 3.1 后端主包

推荐目标：

```text
genericagent/
  __init__.py
  paths.py                    # 项目根、资源、temp、config 路径唯一入口
  contracts/
    __init__.py
    permissions.py            # 跨 core/agents/workflow 的权限契约
    agents.py                 # 跨域 agent/team 轻量类型
  core/
    __init__.py
    app.py                    # 原 agentmain.py
    handler.py                # 原 ga.py
    loop.py                   # 原 agent_loop.py
    models.py                 # 原 agent_runtime_models.py
    context.py                # 原 compact_context.py
    transcripts.py            # 原 session_transcript.py
    tokens.py                 # 原 token_meter.py
    cache_stats.py            # 原 cache_stats.py
    project_instructions.py   # 原 ga_agents_runtime.py
    skills.py                 # 原 skills_runtime.py
    redaction.py              # 原 sensitive_redaction.py
    listing_cache.py          # 原 listing_cache.py
    control/
      __init__.py
      control.py              # 原 agent_control.py
      process.py              # 原 agent_control_process.py
      workflow.py             # 原 agent_control_workflow.py
    permissions/
      __init__.py
      policy.py               # 原 permission_policy.py
      runtime.py              # 原 permission_runtime.py
    tool_results/
      __init__.py
      budget.py               # 原 tool_result_budget.py
      limits.py               # 原 tool_result_limits.py
  llm/
    __init__.py
    core.py                   # 原 llmcore.py
    client.py                 # 原 llm_client.py
    config.py                 # 原 llm_config.py
    keys.py                   # 原 mykey.py 的兼容加载逻辑（不移动真实密钥）
    templates/
      __init__.py
      mykey_template.py       # 原 mykey_template.py
      mykey_template_en.py    # 原 mykey_template_en.py
  mcp/
    __init__.py
    runtime.py                # 原 mcp_runtime.py
  agents/
    __init__.py

    manager.py                # 原 subagent_manager.py
    registry.py               # 原 subagent_registry.py
    state.py                  # 原 subagent_state.py
    agent_path.py             # 原 subagent_agent_path.py
    artifacts.py              # 原 subagent_artifacts.py
    capabilities.py           # 原 subagent_capabilities.py
    event_bus.py              # 原 subagent_event_bus.py
    idle.py                   # 原 subagent_idle.py
    inbound_gate.py           # 原 subagent_inbound_gate.py
    ipc.py                    # 原 subagent_ipc.py
    mailbox.py                # 原 subagent_mailbox.py
    notifications.py          # 原 subagent_notifications.py
    permissions.py            # 原 subagent_permissions.py
    prompts.py                # 原 subagent_prompts.py
    realtime_ipc.py           # 原 subagent_realtime_ipc.py
    receipts.py               # 原 subagent_receipts.py
    roles.py                  # 原 subagent_roles.py
    submissions.py            # 原 subagent_submissions.py
    swarm.py                  # 原 subagent_swarm.py
    tasks.py                  # 原 subagent_tasks.py
    team.py                   # 原 subagent_team.py
    team_activation.py        # 原 subagent_team_activation.py
    transcript.py             # 原 subagent_transcript.py
    wait.py                   # 原 subagent_wait.py
    worktree.py               # 原 subagent_worktree.py
  workflow/
    __init__.py
    runtime.py                # 原 workflow_runtime.py
    scheduler.py              # 原 workflow_scheduler.py
    controller.py             # 原 workflow_controller.py
    store.py                  # 原 workflow_store.py
    models.py                 # 原 workflow_models.py
    activation.py             # 原 workflow_activation.py
    child_agent.py            # 原 workflow_child_agent.py
    permissions.py            # 原 workflow_permissions.py
    path_acl.py               # 原 workflow_path_acl.py
    policy.py                 # 原 workflow_policy.py
    workspace.py              # 原 workflow_workspace.py
    workspace_guard.py        # 原 workflow_workspace_guard.py
    script_validation.py      # 原 workflow_script_validation.py
    determinism.py            # 原 workflow_determinism.py
    tool_profiles.py          # 原 workflow_tool_profiles.py
    verification.py           # 原 workflow_verification.py
    check_adapters.py         # 原 workflow_check_adapters.py
    llm.py                    # 原 workflow_llm.py
    js/
      worker.js               # 原 workflow_js_worker.js
      script_contract.js      # 原 workflow_script_contract.js
  web/
    __init__.py
    tmwebdriver.py            # 原 TMWebDriver.py
    simphtml.py               # 原 simphtml.py
  media/
    __init__.py
    image_codec.py            # 原 image_codec.py
    image_gc.py               # 原 image_gc.py
  resources/
    __init__.py
    code_run_header.py        # 原 assets/code_run_header.py
    prompts/
    schemas/
  services/
    __init__.py
    agent_bbs.py              # 原 assets/agent_bbs.py，如确认仍属服务
```

> `mykey.py` 的真实密钥文件、`llm.yaml`、`mcp.json`、`temp/`、`sessions/`、`memory/`、`skills/`
> 属于用户/项目运行数据，**不放进包内**。`genericagent.paths` 负责从项目根定位它们。

### 3.2 CLI 与入口

保留稳定门面：

```text
ga_cli/                       # 继续作为 pip console_scripts 和 python -m ga_cli 门面
  __init__.py
  __main__.py
  cli.py
```

最终根目录只保留：

```text
agentmain.py                  # 兼容 shim → genericagent.core.app
ga.py                         # 兼容 shim → genericagent.core.handler
ga                            # 兼容 shell 入口
ga.cmd                        # 兼容 Windows 入口
launch.pyw                    # 桌面启动器
hub.pyw                       # 服务管理器入口
pyproject.toml
README.md
AGENTS.md
...
```

`ga_cli/__init__.py` 不再通过 `spec_from_file_location` 加载根 `ga.py`，而是：

```python
from genericagent.core import handler as _handler
GenericAgentHandler = _handler.GenericAgentHandler
# ...按需重导出稳定符号
```

如果后续要把 CLI 也纳入主包，可再增加 `genericagent/cli/`，但**不要在本轮同时搬迁 `ga_cli`**，避免入口和运行时迁移互相放大风险。

### 3.3 前端目录

目标结构：

```text
frontends/
  common/                     # chatapp_common.py、tui_input_history.py 等共享层
  terminal/
    tuiapp.py
    tuiapp_v2.py
    continue_cmd.py
    btw_cmd.py
    export_cmd.py
  ink/
    bridge.py                 # 原 frontends/ink_bridge.py
    ui/                       # 原 frontends/ink-ui/ 的正式源码
      src/
      scripts/
      package.json
      tsconfig.json
  desktop/
    qtapp.py
    fsapp.py
    desktop_pet.pyw
    desktop_pet_v2.pyw
  web/
    stapp.py
    stapp2.py
  bots/
    tgapp.py
    qqapp.py
    dcapp.py
    dingtalkapp.py
    wechatapp.py
    wecomapp.py
  acp/
    genericagent_acp_bridge.py
  skins/                      # 保持现有资源目录
```

注意：

- `frontends/ink-ui/node_modules/` 是本地依赖，不提交；迁移后运行 `npm ci` 或 `npm install` 重建。
- `frontends/ink-ui/_verify_liu.py`、`liu_guoliang_introduction.html`、
  `research_liu_guoliang_*.json` 属于测试/研究产物，应移到 `tests/fixtures/ink-ui/` 或
  `dev_ideas/`，不要与正式前端源码混放。
- 前端迁移放 P6，优先级低于后端包分层；如果 P0–P5 尚未稳定，不要先动前端目录。

---

## 4. 旧路径到新路径映射

### 4.1 精确映射：核心与横切能力

| 旧文件 | 新路径 |
| --- | --- |
| `agentmain.py` | `genericagent/core/app.py`（根文件保留 shim） |
| `ga.py` | `genericagent/core/handler.py`（根文件保留 shim） |
| `agent_loop.py` | `genericagent/core/loop.py` |
| `agent_runtime_models.py` | `genericagent/core/models.py` |
| `agent_control.py` | `genericagent/core/control/control.py` |
| `agent_control_process.py` | `genericagent/core/control/process.py` |
| `agent_control_workflow.py` | `genericagent/core/control/workflow.py` |
| `compact_context.py` | `genericagent/core/context.py` |
| `session_transcript.py` | `genericagent/core/transcripts.py` |
| `token_meter.py` | `genericagent/core/tokens.py` |
| `cache_stats.py` | `genericagent/core/cache_stats.py` |
| `ga_agents_runtime.py` | `genericagent/core/project_instructions.py` |
| `skills_runtime.py` | `genericagent/core/skills.py` |
| `sensitive_redaction.py` | `genericagent/core/redaction.py` |
| `listing_cache.py` | `genericagent/core/listing_cache.py` |
| `permission_policy.py` | `genericagent/core/permissions/policy.py` |
| `permission_runtime.py` | `genericagent/core/permissions/runtime.py` |
| `tool_result_budget.py` | `genericagent/core/tool_results/budget.py` |
| `tool_result_limits.py` | `genericagent/core/tool_results/limits.py` |

### 4.2 精确映射：LLM / MCP / web / media

| 旧文件 | 新路径 |
| --- | --- |
| `llmcore.py` | `genericagent/llm/core.py` |
| `llm_client.py` | `genericagent/llm/client.py` |
| `llm_config.py` | `genericagent/llm/config.py` |
| `workflow_llm.py` | `genericagent/workflow/llm.py` |
| `mykey.py` | 保留为项目根用户配置；兼容加载逻辑放 `genericagent/llm/keys.py` |
| `mykey_template.py` | `genericagent/llm/templates/mykey_template.py` |
| `mykey_template_en.py` | `genericagent/llm/templates/mykey_template_en.py` |
| `mcp_runtime.py` | `genericagent/mcp/runtime.py` |
| `TMWebDriver.py` | `genericagent/web/tmwebdriver.py` |
| `simphtml.py` | `genericagent/web/simphtml.py` |
| `image_codec.py` | `genericagent/media/image_codec.py` |
| `image_gc.py` | `genericagent/media/image_gc.py` |

### 4.3 规则映射：subagent / agent team

所有 `subagent_*.py` 去前缀后进入 `genericagent/agents/`：

```text
subagent_agent_path.py      → agents/agent_path.py
subagent_artifacts.py       → agents/artifacts.py
subagent_capabilities.py    → agents/capabilities.py
subagent_event_bus.py       → agents/event_bus.py
subagent_idle.py            → agents/idle.py
subagent_inbound_gate.py    → agents/inbound_gate.py
subagent_ipc.py             → agents/ipc.py
subagent_mailbox.py         → agents/mailbox.py
subagent_manager.py         → agents/manager.py
subagent_notifications.py   → agents/notifications.py
subagent_permissions.py     → agents/permissions.py
subagent_prompts.py         → agents/prompts.py
subagent_realtime_ipc.py    → agents/realtime_ipc.py
subagent_receipts.py        → agents/receipts.py
subagent_registry.py        → agents/registry.py
subagent_roles.py           → agents/roles.py
subagent_state.py           → agents/state.py
subagent_submissions.py     → agents/submissions.py
subagent_swarm.py           → agents/swarm.py
subagent_tasks.py           → agents/tasks.py
subagent_team.py            → agents/team.py
subagent_team_activation.py → agents/team_activation.py
subagent_transcript.py      → agents/transcript.py
subagent_wait.py            → agents/wait.py
subagent_worktree.py        → agents/worktree.py
```

### 4.4 规则映射：workflow

所有 `workflow_*.py` 去前缀后进入 `genericagent/workflow/`：

```text
workflow_activation.py          → workflow/activation.py
workflow_check_adapters.py      → workflow/check_adapters.py
workflow_child_agent.py         → workflow/child_agent.py
workflow_controller.py          → workflow/controller.py
workflow_determinism.py         → workflow/determinism.py
workflow_llm.py                 → workflow/llm.py
workflow_models.py              → workflow/models.py
workflow_path_acl.py            → workflow/path_acl.py
workflow_permissions.py         → workflow/permissions.py
workflow_policy.py              → workflow/policy.py
workflow_runtime.py             → workflow/runtime.py
workflow_scheduler.py           → workflow/scheduler.py
workflow_script_validation.py   → workflow/script_validation.py
workflow_store.py               → workflow/store.py
workflow_tool_profiles.py       → workflow/tool_profiles.py
workflow_verification.py        → workflow/verification.py
workflow_workspace.py           → workflow/workspace.py
workflow_workspace_guard.py     → workflow/workspace_guard.py
workflow_js_worker.js           → workflow/js/worker.js
workflow_script_contract.js     → workflow/js/script_contract.js
```

### 4.5 资源与非 Python 文件

| 旧位置 | 新位置/处理 |
| --- | --- |
| `assets/code_run_header.py` | `genericagent/resources/code_run_header.py`（由 `code_run` 读取并内联） |
| `assets/configure_mykey.py` | `genericagent/cli/configure.py` 或 `genericagent/tools/configure.py` |
| `assets/agent_bbs.py` | `genericagent/services/agent_bbs.py`（若确认仍作为服务保留） |
| `assets/` 中的图片、模板、浏览器扩展 | 继续留在 `assets/`，`assets/` 不再承载运行时 Python 实现 |
| `workflow_js_worker.js` | `genericagent/workflow/js/worker.js` |
| `workflow_script_contract.js` | `genericagent/workflow/js/script_contract.js` |
| `frontends/ink_bridge.py` | `frontends/ink/bridge.py` |
| `frontends/ink-ui/` | `frontends/ink/ui/`（P6，单独迁移） |
| `frontends/ink-ui/_verify_liu.py` 等研究产物 | `tests/fixtures/ink-ui/` 或 `dev_ideas/` |

---

## 5. 必须先建立的兼容层

### 5.1 `genericagent/paths.py`

职责：集中解决“代码在哪里”和“用户数据在哪里”两个问题。

建议 API：

```python
from pathlib import Path
import os

def _discover_project_root() -> Path:
    """优先 GA_PROJECT_ROOT；否则从当前文件向上寻找 pyproject.toml / ga / assets。"""

PROJECT_ROOT: Path
PACKAGE_ROOT: Path
TEMP_DIR: Path
ASSETS_DIR: Path
SKILLS_DIR: Path
MEMORY_DIR: Path
FRONTENDS_DIR: Path
RESOURCES_DIR: Path
MCP_CONFIG_PATH: Path
LLM_CONFIG_PATH: Path
WORKFLOW_JS_DIR: Path
```

硬性要求：

1. **不得依赖 `os.getcwd()`**：`code_run`、subagent、workflow child 都可能改变当前工作目录。
2. 支持 `GA_PROJECT_ROOT` 环境变量覆盖，便于测试、worktree 和打包安装场景。
3. 在找不到项目根时明确报错，不要静默回退到包目录后写错位置。
4. 所有路径同时提供 `Path` 和必要的 `str` 兼容访问。
5. 根目录发现逻辑只允许放在 `paths.py`；其他模块禁止再各自写 `Path(__file__).parent` 猜根目录。

### 5.2 根级兼容 shim

过渡期保留旧模块名，例如：

```python
# workflow_runtime.py
from genericagent.workflow import runtime as _impl
import sys as _sys

_sys.modules[__name__] = _impl
```

要求：

- 使用**模块别名**，不要只写 `from ... import *`；测试可能访问 `_private` 符号。
- 保证 `mock.patch("workflow_runtime.xxx")`、`importlib.reload(workflow_runtime)` 仍作用于同一个规范模块对象。
- 每个 shim 都应有身份测试：`import workflow_runtime as a; import genericagent.workflow.runtime as b; assert a is b`。
- shim 只用于过渡，不在 shim 中新增业务逻辑。
- 最终清理 shim 前，先完成全仓 import 迁移和至少一轮真实 E2E。

### 5.3 跨域契约

迁移前建议先抽以下契约，避免 `core → agents → workflow → core` 的循环扩大：

- `genericagent/contracts/permissions.py`
  - 权限决策、profile 常量、通用 `ToolPermissionPolicy` 协议；
  - 供 `core/permissions`、`agents/permissions`、`workflow/permissions` 共享。
- `genericagent/contracts/agents.py`
  - agent path、agent 状态的最小跨域类型；
  - 不把 manager 实现暴露给 workflow。
- `genericagent/workflow/models.py`
  - 保持为 workflow 领域模型，不反向依赖 core 实现。

现有延迟导入可以暂时保留，但迁移后应标记为“已知循环依赖”，并在 P7 逐条消除或降级为契约调用。

---

## 6. 导入与启动路径迁移规则

### 6.1 包内导入

包内统一使用绝对包导入，便于测试、`importlib`、worktree 和兼容 shim 共用同一模块身份：

```python
from genericagent.core import loop
from genericagent.agents import manager
from genericagent.workflow import runtime as workflow_runtime
from genericagent.core.permissions import policy as permission_policy
```

不要在新包内继续写：

```python
import agent_loop
from workflow_runtime import WorkflowRuntime
```

除非它位于明确的兼容 shim 中。

### 6.2 subagent 启动

`subagent_manager.py` 当前用：

```python
agentmain_path = self.repo_dir / "agentmain.py"
cmd = [self.python_executable, str(agentmain_path), ...]
```

迁移方案：

- P1–P5 保留根 `agentmain.py` shim，确保现有启动方式不断；
- P6 改为 `python -m genericagent.core.app` 或新的稳定 `python -m genericagent`；
- worktree 隔离场景必须确认 worktree 内同时存在新包和根 shim；
- 新增回归测试：普通 subagent、worktree subagent、resume subagent 三种路径均可启动。

### 6.3 workflow JS 资源

当前：

```python
Path(__file__).resolve().with_name("workflow_js_worker.js")
Path(__file__).resolve().with_name("workflow_script_contract.js")
```

迁移后统一改为：

```python
paths.WORKFLOW_JS_DIR / "worker.js"
paths.WORKFLOW_JS_DIR / "script_contract.js"
```

并增加测试：

- 从仓库根运行；
- 从 `temp/` 运行；
- 从 worktree 运行；
- wheel 安装后运行（至少检查资源被打包）。

### 6.4 `code_run` 头文件

`assets/code_run_header.py` 是运行时读取后内联到临时脚本的资源，不是普通入口。迁移后：

- 文件放 `genericagent/resources/code_run_header.py`；
- `ga.py`/`core/handler.py` 通过 `paths.RESOURCES_DIR` 读取；
- 头文件内对 `workflow_workspace_guard` 的导入改为新包路径；
- 增加“从任意 cwd 执行 Python code_run”的回归测试。

---

## 7. 分阶段实施计划

### P0：盘点、冻结与结构检查

目标：迁移前建立可复现基线，不移动实现。

动作：

1. 记录根级 `*.py` 清单、领域数量和 AST 依赖边。
2. 新增 `scripts/check_project_structure.py`：
   - 输出根级实现文件数量；
   - 检查根级允许清单；
   - 扫描 `__file__`、`sys.path`、`os.chdir`、资源路径；
   - 输出“旧模块名 → 新模块名”迁移缺口。
3. 记录当前测试基线：
   - `python -m unittest discover -s tests`；
   - 关键 focused tests；
   - Ink UI `npm test`、`npx tsc --noEmit`。
4. 确认 `pyproject.toml`、`ga_cli/__init__.py`、`subagent_manager.py` 的入口假设。

退出条件：

- 有结构检查脚本；
- 有当前 import/路径/测试基线；
- 没有移动任何实现文件。

### P1：建立包骨架与路径层

动作：

1. 创建 `genericagent/` 及子包 `__init__.py`。
2. 新增 `genericagent/paths.py`。
3. 把 `pyproject.toml` 改为包发现，加入 `genericagent*`、`ga_cli*`。
4. 先迁移一个低风险横切模块（建议 `sensitive_redaction.py`）验证：
   - `git mv`；
   - 新路径导入；
   - 根 shim；
   - 新旧模块身份；
   - focused tests。
5. 不改 `agentmain.py`、`ga.py`、workflow、subagent 的主逻辑。

退出条件：

- 新包可导入；
- `ga_cli` 仍可启动；
- 新路径模块与旧路径 shim 指向同一对象；
- 包发现配置可安装。

### P2：迁移叶子域

顺序建议：

1. `llm/`：`llmcore.py`、`llm_client.py`、`llm_config.py`、`mykey_template*.py`；
2. `mcp/`：`mcp_runtime.py`；
3. `web/`：`TMWebDriver.py`、`simphtml.py`；
4. `media/`：`image_codec.py`、`image_gc.py`；
5. `core/` 横切小模块：`cache_stats.py`、`token_meter.py`、`listing_cache.py`、
   `sensitive_redaction.py`、`tool_result_limits.py`、`tool_result_budget.py`。

每迁移一个域：

- `git mv` 而不是复制；
- 更新内部导入；
- 添加根 shim；
- 跑该域 focused tests；
- 单独提交。

退出条件：

- 叶子域无根实现；
- 旧 import 仍可用；
- `llm.yaml`、`mcp.json`、`mykey.json` 路径测试通过。

### P3：迁移 workflow

动作：

1. 先抽 `workflow/models.py` 与权限契约的稳定导入。
2. 迁移 18 个 `workflow_*.py` 到 `genericagent/workflow/`。
3. 迁移 `workflow_js_worker.js`、`workflow_script_contract.js` 到 `workflow/js/`。
4. 更新 `workflow_runtime.py`、`workflow_script_validation.py` 的资源路径。
5. 更新 `agent_control_workflow.py`、`subagent_team_activation.py` 等跨域引用。
6. 添加根 shim，确保 `/workflows`、resume、stop、artifact、schema 等旧入口不断。

重点测试：

- `tests/test_workflow_runtime.py`
- `tests/test_workflow_store.py`
- `tests/test_workflow_meta_contract.py`
- `tests/test_workflow_workspace.py`
- `tests/test_workflow_workspace_guard.py`
- `tests/test_workflow_child_agent.py`
- `tests/test_workflow_llm.py`
- 真实 workflow E2E：按 `docs/20261016-GA-workflow-17k-prompt-真实e2e-并行串行.md` 与
  `docs/20261015-GA-ink-真实LLM-e2e-自测SOP.md` 执行。

退出条件：

- workflow 目录内无根模块导入；
- JS worker/contract 从任意 cwd 可定位；
- `/workflows` 列表、详情、resume、stop 回归通过。

### P4：迁移 agents / agent team

动作：

1. 先处理 `subagent_state.py`、`subagent_capabilities.py`、`workflow_permissions.py` 的跨域契约。
2. 迁移 25 个 `subagent_*.py` 到 `genericagent/agents/`。
3. 更新 `agentmain.py`、`ga.py`、workflow scheduler/store 的引用。
4. 更新 subagent 子进程启动路径与 worktree 副本逻辑。
5. 保留根 shim，保证 `subagent_manager`、`subagent_tasks`、`subagent_team` 等旧模块名可用。

重点测试：

- `tests/test_subagent_manager.py`
- `tests/test_subagent_registry.py`
- `tests/test_subagent_state.py`
- `tests/test_subagent_mailbox.py`
- `tests/test_subagent_tasks.py`
- `tests/test_subagent_team.py`
- `tests/test_subagent_worktree.py`
- `tests/test_ga_task_tools.py`
- 真实 subagent / agent team E2E 按 `docs/20261015-GA-ink-真实LLM-e2e-自测SOP.md` 执行。

退出条件：

- 普通 subagent、worktree subagent、resume subagent 均能启动；
- task board、mailbox、send_message、wait_agent 回归通过；
- 旧模块名仍可 import。

### P5：迁移 core / 应用入口

动作：

1. 迁移 `agent_loop.py`、`agent_runtime_models.py`、`agent_control*.py`、`compact_context.py`、
   `session_transcript.py`、`ga_agents_runtime.py`、`skills_runtime.py`、`permission_*.py`。
2. 迁移 `agentmain.py` → `genericagent/core/app.py`，根 `agentmain.py` 变 shim。
3. 迁移 `ga.py` → `genericagent/core/handler.py`，根 `ga.py` 变 shim。
4. 更新 `ga_cli/__init__.py`、`ga_cli/cli.py`、`launch.pyw`、`hub.pyw`。
5. 运行核心回归与真实 Ink E2E。

退出条件：

- 根目录只剩薄入口/shim；
- `python -m ga_cli`、`ga`、`ga.cmd`、`python launch.pyw`、`python agentmain.py` 均可工作；
- `GenericAgentHandler` 等核心类只有一个规范模块身份。

### P6：前端目录与资源清理

动作：

1. 建立 `frontends/{common,terminal,ink,desktop,web,bots,acp}`。
2. 按 §3.3 迁移 Python 前端。
3. `frontends/ink_bridge.py` → `frontends/ink/bridge.py`。
4. `frontends/ink-ui/` → `frontends/ink/ui/`，更新 npm scripts、TS imports、启动器路径、文档和测试。
5. 把 `_verify_liu.py` 和研究 JSON/HTML 移到测试夹具或 `dev_ideas/`。
6. 更新 `ga_cli/cli.py` 的 frontend 命令映射。

退出条件：

- 前端目录按运行形态分组；
- `frontends/` 顶层不再混放大量互不相关的适配器；
- Ink UI 的 `npm test` 与 `tsc --noEmit` 通过；
- 所有启动命令在 Git Bash 和 PowerShell 下均能定位资源。

### P7：清理、打包与结构门禁

动作：

1. 全仓搜索旧模块导入，迁移测试到 `genericagent.*` 规范路径。
2. 保留根 shim 一个过渡周期；确认无外部依赖后逐批删除。
3. 更新 `pyproject.toml` 的 `package-data`，确保 JS、prompt、schema 资源进入 wheel。
4. 更新 README、AGENTS、测试 SOP、workflow 文档中的路径。
5. 让 `scripts/check_project_structure.py` 成为 CI/提交前检查：
   - 根目录禁止新增实现 `*.py`；
   - 新 Python 文件必须落入允许的包目录；
   - 禁止新增 `Path(__file__).parent` 猜项目根；
   - 禁止在根目录新增 `workflow_*`、`subagent_*`、`llm*` 实现。

退出条件：

- 根目录实现文件数量达到目标；
- `genericagent` 包可安装、可导入；
- 旧入口和新入口均通过；
- 结构检查脚本能阻止目录再次扁平化。

---

## 8. 每阶段统一验收矩阵

### 8.1 导入与身份

```bash
python -m compileall -q genericagent ga_cli
python - <<'PY'
import agentmain
import ga
import workflow_runtime
import subagent_manager
import mcp_runtime
import genericagent.core.app
import genericagent.workflow.runtime
import genericagent.agents.manager
import genericagent.mcp.runtime
print("import smoke ok")
PY
```

必须额外断言：

```python
import workflow_runtime as old
import genericagent.workflow.runtime as new
assert old is new
```

### 8.2 单元测试

- 全量：`python -m unittest discover -s tests`
- 聚焦：按 P2–P6 对应测试文件执行。
- 允许使用 `pytest -q` 的现有测试保持原命令不变；迁移不得改变测试语义。

### 8.3 路径与 cwd

从三个不同 cwd 运行资源定位测试：

1. 仓库根；
2. `temp/`；
3. `frontends/` 或 worktree。

至少覆盖：

- `llm.yaml`；
- `mcp.json`；
- `skills/`；
- `assets/code_run_header.py`；
- `workflow/js/worker.js`；
- `workflow/js/script_contract.js`；
- `temp/sessions/`；
- `temp/model_responses/`。

### 8.4 Ink UI

```bash
cd frontends/ink-ui
npm test
npx tsc --noEmit
```

P6 移动前端后，测试命令应更新为新目录，但验收语义不变。

### 8.5 真实 LLM E2E

真实测试使用默认 `deepseek-flash`，按现有 SOP 执行，不依赖固定输出：

- `docs/20261015-GA-ink-真实LLM-e2e-自测SOP.md`
- `docs/20261016-GA-workflow-17k-prompt-真实e2e-并行串行.md`

至少验证：

- workflow 启动、`/workflows` 列表、详情、resume/stop；
- subagent 普通启动、worktree 启动、resume；
- tool result 持久化路径；
- 终端 UI 与 bridge 不因目录迁移而丢失事件。

### 8.6 打包

```bash
python -m pip install -e .
python -m pip wheel . -w temp/wheels
python -m pip install temp/wheels/genericagent-*.whl --force-reinstall
ga list
```

如果构建工具不可用，先记录缺口，不要伪造通过。打包验收至少检查：

- `genericagent` 子包进入 wheel；
- JS 资源进入 wheel；
- `ga_cli` console script 仍可用；
- 安装后资源路径解析不依赖源码仓库 cwd。

---

## 9. 风险、回滚与不做项

### 9.1 高风险

| 风险 | 后果 | 缓解 |
| --- | --- | --- |
| 循环依赖 | import 时崩或部分模块为空 | 先抽契约；保留延迟导入；按域迁移 |
| `__file__` 资源定位 | 找不到 assets/skills/temp/config | 统一 `genericagent.paths` |
| 旧测试 patch 模块名 | mock 打不到规范模块 | 根 shim 使用模块别名，不复制实现 |
| subagent 启动路径 | 子进程找不到 agentmain | 过渡期保留根 shim；增加三种启动回归 |
| JS worker/contract 路径 | workflow 启动失败或 meta 校验失败 | 统一 `paths.WORKFLOW_JS_DIR` |
| pyproject 只打包 `ga_cli` | wheel 缺主包 | 改为 `packages.find` + package-data |
| 前端目录迁移 | npm/tsx/启动器路径断裂 | P6 单独做，更新所有命令与测试 |
| 行尾/中文路径 | Git Bash、Windows 启动异常 | 遵循 `AGENTS.md` 的 LF/UTF-8/Git Bash 规则 |

### 9.2 回滚策略

- 每个阶段一个独立 commit；失败直接 `git revert` 该阶段。
- 迁移期间不删除旧根 shim；回滚不会让旧导入立刻失效。
- 不使用递归删除、不用脚本批量覆盖未提交修改。
- 若某领域迁移失败，保持该领域旧实现，只回退该领域 commit，不阻塞其他领域。

### 9.3 本轮明确不做

- 不重写 workflow DSL；
- 不改变 subagent/team 行为；
- 不实现 Claude Code 云端会话；
- 不迁移 `temp/`、`sessions/`、`node_modules/` 等运行/依赖数据；
- 不为了“像 CC”伪造 CC 原始目录；
- 不在目录迁移 commit 中夹带业务重构。

---

## 10. 推荐提交顺序

```text
chore(structure): add package skeleton and project path resolver
refactor(llm): move LLM modules into genericagent
refactor(mcp): move MCP runtime into genericagent
refactor(media): move web and media helpers into genericagent
refactor(workflow): move workflow runtime and JS resources
refactor(agents): move subagent and team modules
refactor(core): move agent runtime and entrypoints
refactor(frontends): regroup frontend adapters by runtime shape
chore(structure): enforce package boundaries and update packaging
```

每个 commit 必须包含：

1. 迁移文件；
2. 根级兼容 shim；
3. 内部 import 更新；
4. 对应 focused tests；
5. 实施记录或文档回链。

---

## 11. 最终 Definition of Done

- 根目录不再承载 73 个实现 `*.py`；只保留允许的入口/shim。
- `genericagent/` 主包按 `core`、`llm`、`mcp`、`agents`、`workflow`、`web`、`media`、`resources` 分层。
- `ga_cli`、`ga`、`ga.cmd`、`python -m ga_cli` 均可用。
- 旧根模块名通过 shim 仍可 import；新旧模块身份一致。
- `paths.py` 统一解析项目根、资源、temp、config、skills、sessions。
- workflow JS worker/contract 不再依赖 `__file__` 同目录猜测。
- subagent 普通/worktree/resume 启动路径全部通过。
- 全量 Python 测试、Ink UI 测试、真实 LLM E2E 有明确结果。
- wheel/sdist 打包包含主包和包资源。
- `scripts/check_project_structure.py` 能阻止目录再次扁平化。
- 文档中的路径、启动命令、测试命令与重构后结构一致。

---

## 12. 给后续实施 Codex 的阅读顺序

1. 本文件：`docs/20261016-ga-project-structure-reorg-plan.md`
2. `AGENTS.md`：中途调研记录与项目强制流程
3. `README.md`：当前项目结构与运行命令
4. `pyproject.toml`：打包边界
5. CC 证据：
   - `D:\git_codes\claudecode283_hack\claudecode283\extracted\README.md`
   - `D:\git_codes\claudecode283_hack\claudecode283\extracted\dynamic-workflow-internals.md`
   - `D:\git_codes\claudecode283_hack\claudecode283\extracted\agent-team-internals.md`
   - `D:\git_codes\claudecode283_hack\claudecode283\extracted\hooks-internals.md`
   - `D:\git_codes\claudecode283_hack\reconstructed\GLOSSARY.md`
6. GA 既有迁移/测试文档：
   - `docs/20261015-GA-ink-真实LLM-e2e-自测SOP.md`
   - `docs/20261016-GA-workflow-17k-prompt-真实e2e-并行串行.md`
   - `docs/20261013-ga-workflow-隔离目录清理方案.md`

实施时先执行 P0，不要直接开始 `git mv` 全部文件。任何新发现的 CC 结构事实，按 `AGENTS.md` 的“中途补充调研的持久化流程”先补记录，再改代码。
