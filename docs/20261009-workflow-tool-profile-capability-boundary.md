# GA Workflow 工具边界：从"模型声明具体工具"改为"宿主声明能力档位"

日期：2026-10-09
触发问题：`/workflow 使用 workflow 来调研 openai 2026年7月后解决了哪些比较知名的数学猜想`
运行 `wf_8c50c89b...` 卡死 10 分钟以上，子代理 12 次 `code_run`、14 次报错、0 次 MCP 调用。

## 1. 现场证据（只读排查）

同一份 prompt、同一个模型（`cc-deepseek-v4.1-flash-chat`）、同一份系统提示词，两次运行结果相反：

| | `wf_d1790ea5...`（18:46，succeeded） | `wf_8c50c89b...`（19:15，卡死） |
|---|---|---|
| 任务 prompt | 逐字相同 | 逐字相同 |
| model / profile | `deepseek-flash` / `cc-deepseek-v4.1-flash-chat` | 完全相同 |
| plannerMode | deterministic | deterministic |
| 系统提示词 | `GA_AGENTS.md` | 完全相同 |
| 工具调用 | `tavily_search`x4、`fetch_markdown`x2、`fetch_readable`x2 | `code_run`x12 |
| MCP 调用 | 8 | 0 |

同一份系统提示词一次用对 MCP、一次没用，所以"提示词把模型推向 `code_run`"解释不了差异。
两次之间真正变化的是 **MCP 工具集**：18:46 那轮 schema 有 tavily + fetch；19:15 那轮
`temp/mcp_tools_cache.json` 只剩 tavily + context7（`complete: False`）。
现场复现 MCP 发现：`exa`/`fetch`/`sequential-thinking`/`memory` 四个 server 全部
`CERTIFICATE_VERIFY_FAILED` 或 `Failed to initialize server session`，只有 tavily/context7 活着。

## 2. GA 当前实现的两个结构性缺陷

### 缺陷 A：子代理没有被限制工具，只能靠"猜"

- `workflow_planner._build_plan` 的 `research` 分支**一个 `requiredTools` 都不声明**；
  子代理拿到的是完整 31 工具 schema，其中包含 `code_run` 和 13 个编排工具。
- `mixed` 分支靠 `search_tool = "mcp__tavily__tavily_search" if "tavily" in task_text.lower() else "web_scan"`
  —— 用任务文本里有没有 "tavily" 这个词来选工具，是纯猜；`web_scan` 还可能不存在。
- 子 schema 里塞了 13 个编排工具（`spawn_agent`/`wait_agent`/...），违反 AGENTS.md 自己写的
  不变量 "Default child schema has no orchestration tools"。剥离逻辑只在 `agentmain.py` 的
  subagent 路径生效，workflow child 直接读 `assets/tools_schema.json`。
- 结果：MCP 搜索不可用时，子代理手里有 `code_run`，于是自己写 Python 抓 bing/ddg/brave/arxiv，
  全部超时/403，一路撞到 14 次报错仍不停止（`max_turns=40`，无墙钟超时）。

### 缺陷 B：MCP 不可用被当成硬失败

`workflow_child_agent.prepare_run_capabilities` 一旦发现声明工具缺失就
`raise RuntimeError("capability_unavailable: ...")`，runtime 记
`workflow_capability_preflight_failed` 并 fail-fast。

## 3. 参考实现怎么做的

### Step-Code（`packages/coding-agent/src/features/workflow/`）

- `tool-profile.ts`：**命名档位**，不是逐工具声明。

  ```ts
  export const WORKFLOW_TOOL_PROFILES = {
      planner: [...READ_ONLY_TOOLS, "clarify_user"],
      developer: [...READ_ONLY_TOOLS, "write_file", "edit_file", "run_command"],
      qa: [...READ_ONLY_TOOLS, "run_command"],
      ...
  };
  ```

  `agent(prompt, {toolProfile})` 把档位解析成子代理的**工具白名单**——子代理物理上没有档位外的工具。
  还支持 `"*"`（不限制）和显式数组。
- `runtime.ts` 里 `normalizeAgentOptions` 只做归一化和 `clampRetries`，**全仓没有任何
  `requiredTools`/`mustCall`/`minimumCalls` 概念**（`grep -rn "requiredTool\|mustCall\|minimumCalls"` 为空）。
- 硬约束只有三处：工具档位（能不能用这类工具）、路径 ACL（能碰哪些路径）、schema（结构化输出 + 重试）。
- 档位与 phase 绑定：`hoh-planner` / `hoh-developer` / `hoh-qa`，由宿主按阶段决定，不问模型。

### Codex CLI（`D:\git_codes\codex\codex-rs`）

- `ext/extension-api/src/tool_policy.rs`：

  ```rust
  /// A startup ceiling on tool selection ... This policy only restricts tools;
  /// permission and approval checks still apply.
  pub struct ToolPolicy { pub allowed_tools: Option<Vec<ToolName>>, ... }
  ```

  `None` = 保持常规选择；`Some(list)` = 上限。**只做减法，从不要求模型必须调用某个工具。**
- `core/src/agent/role.rs`：role "may customize the child or **reduce its capabilities**,
  but never replace the parent session's authority"。
- `core/src/tools/handlers/multi_agents_spec.rs`：`spawn_agent` 的 schema 参数里**没有**
  `tools`/`allowed_tools`——模型根本无法给子代理挑工具，工具边界由宿主的 role config 决定。
- MCP 启动失败是**状态事件**（`McpStartupUpdateEvent` + `mcp_startup_failure_reason` +
  `mcp_init_error_display` 给出恢复建议），不是致命错误；已经起来的 server 照常用。

**结论**：两个参考实现都只做"能力上限"（工具白名单/档位 + 路径 ACL），
从不做"必须调用某工具"的逐工具声明。GA 的 `requiredTools` 是模型在猜具体工具名，
既脆弱（server 改名/不可用就崩）又死板（缺失即 fail-fast）。

## 4. 改造方案

### 4.1 宿主拥有的命名档位（对齐 Step-Code `WORKFLOW_TOOL_PROFILES`）

新增 `workflow_tool_profiles.py`，档位用**能力类别**表达，而不是工具名白名单，
这样新增工具自动落在正确类别里（不再犯 `observedArtifacts` 只认 `file_write`/`file_patch` 的错）：

| 档位 | 允许 | 拒绝 |
|---|---|---|
| `planner` | 只读 + 检索 | `file_write`、`execute` |
| `research` | 只读 + 检索 + 落盘 | `execute` |
| `authoring` | 全部 | （仅编排工具） |
| `verify` | 只读 + 执行 | `file_write` |
| `*` | 全部 | （仅编排工具） |

编排工具（`spawn_agent`/`wait_agent`/...）对 workflow child **永远拒绝**，与 Step-Code
给子进程加 `STEP_DISABLE_WORKFLOW=1` 一致。

角色 → 默认档位由宿主推导（`verification`/`review` → `verify`，`research` → `research`，
`implementation`/`tests`/`repair` → `authoring`，`synthesis`/`understanding` → `planner`），
计划可用 `toolProfile` 覆盖。这与现有 `resolve_job_permission_profile` 的 EVIDENCE_ROLES 推导同构。

### 4.2 计划声明"能力类别"，不再声明具体工具名

- 计划写 `capabilities: ["web_search", "file_write"]`，宿主在 preflight 时从**实际 schema**
  解析出具体可用工具（`capabilityCoverage`）。
- 验收用 `requiredCapabilityEvidence: [{"capability": "web_search", "agent": "...", "minimumCalls": 1}]`，
  按**类别**统计 transcript 里的调用次数，而不是按死某个工具名。
- 兼容：旧 `requiredTools`/`requiredToolEvidence` 仍然解析（prompt-guided 旧计划不至于崩），
  但确定性分支不再产出它们，planner prompt 改为要求 `capabilities`。

### 4.3 MCP 不可用 → 可见降级，而不是 fail-fast

- `prepare_run_capabilities` 不再 raise，改为返回带 `unavailableCapabilities` 的 snapshot。
- 子代理 prompt 增加 `toolProfile` 与 `unavailableCapabilities` 两行，模型据此改用可用工具或
  如实报告不可用，而不是退化成 `code_run` 硬抓网页。
- 若计划把某能力声明为 `mode: required` 且该能力在 preflight 时**一个可用工具都没有**，
  运行终态是 `degraded`（`capability_unavailable`），不是 `failed`、也不是静默通过。

## 5. 不变量（写入 AGENTS.md）

1. workflow child 的工具边界由**宿主档位**决定，计划只声明档位名或能力类别，永不声明具体工具名。
2. 档位是**减法**：只限制"能用哪一类工具"，不要求"必须调用某个工具"。
3. 编排工具对 workflow child 永远不可用。
4. 工具/能力不可用是**可见降级**，不是 fail-fast，也不是静默通过。
