# GA Ink UI Workflow 用户使用说明

本文面向想在 **GA Ink UI** 中亲自测试和使用 workflow 功能的用户。目标是让你从终端 UI 里用 **一个启动命令** 发起动态工作流，由智能体自动规划并执行，同时可查看列表、详情、停止和恢复。

> 适用范围：GenericAgent 的 React/Ink 终端 UI，即 `ga` 或 `ga ink` 启动的界面。
>
> 安全提醒：不要把 API key、token、密码、`mykey.py`、`mykey.json`、`mcp.json` 内容粘贴进任务描述。需要真实 API / MCP 时，使用本地已有配置即可。
>
> 产品原则：workflow **不需要人手写 JS script**，也 **不需要人工 approve/deny**。`/workflow <任务>` 会让 planner 自动生成 plan/script 并立刻执行。

---

## 1. 启动 Ink UI

在仓库根目录或安装后的环境中启动：

```bash
ga
```

或显式启动 Ink UI：

```bash
ga ink
```

进入界面后，你会看到底部输入框。所有 workflow 命令都直接在这个输入框中输入。

---

## 2. 目前可用的 workflow slash 命令

```text
/workflows
/workflow
/workflow list
/workflow [--timeout SECONDS] TASK
/workflow detail RUN_ID
/workflow resume RUN_ID
/workflow stop RUN_ID [reason]
```

| 命令 | 作用 |
| --- | --- |
| `/workflows` | 打开 workflow 列表，查看当前和历史 runs |
| `/workflow` / `/workflow list` | 同上，打开列表 |
| `/workflow <任务>` | **唯一启动入口**：根据自然语言任务自动规划并执行（默认超时 **900 秒 / 15 分钟**） |
| `/workflow --timeout N <任务>` | 同上，并覆盖 runtime 超时（秒） |
| `/workflow detail <runId>` | 查看某个 workflow 的详情、阶段和 agent 信息 |
| `/workflow stop <runId> [原因]` | 停止正在运行的 workflow |
| `/workflow resume <runId>` | 从可恢复的终态 run 继续（cache 前缀可复用） |

已移除的用户入口（不要再依赖）：

- `/workflow plan ...`：仍兼容，会映射到 `/workflow ...` 自动执行
- `/workflow plan --manual ...`：`--manual` 被忽略，仍会自动执行
- `/workflow approve` / `/workflow deny`：已从产品路径删除
- 人手写 JS draft：产品路径不再提供

---

## 3. 推荐的第一次测试流程

### 步骤 1：直接启动一个只读审查 workflow

```text
/workflow --timeout 600 请只读审查当前项目的 workflow UI 实现，重点检查 slash command 入口、workflow overview、agent detail、live status bar 和测试覆盖。不要修改文件，不要读取 mykey.py、mykey.json、mcp.json，不要提交。
```

预期现象：

- UI 会创建一个 workflow run；
- planner 生成 plan/script 后 **自动进入 `running`**；
- 底部可能出现 live status bar（`N/M agents done`）；
- 不需要 approve。

### 步骤 2：查看 workflow 列表

```text
/workflows
```

记下 `runId`，形如：

```text
wf_abc123...
```

### 步骤 3：查看详情

```text
/workflow detail wf_abc123
```

预期：

- workflow 名称和状态；
- phases / agents overview；
- 每个 agent 的标签、状态；
- agent detail 中可查看 Prompt / Activity / Outcome；
- 不应把 raw script 当作主界面展示。

### 步骤 4：运行中可停止

```text
/workflow stop wf_abc123 用户停止测试
```

或在 status bar 显示 running 时，输入框为空按 `x` 停止。

### 步骤 5：结束后再看结果

```text
/workflow detail wf_abc123
```

确认：

- run 状态是否为 `succeeded`；
- agents 是否完成；
- outcome 是否有可读摘要；
- 是否存在失败、拒绝工具调用或错误信息。

---

## 4. `/workflow` 启动参数

### 4.1 默认：自动规划并执行

```text
/workflow 审查当前分支最近提交的风险，并给出修复建议
```

含义：

- planner 根据自然语言生成 WorkflowPlan 并编译为 script；
- 校验通过后自动开始执行；
- subagent 使用主会话当前 `/model` 对应的 `llm.yaml` profile。

### 4.2 `--timeout SECONDS`

```text
/workflow --timeout 1800 审查 workflow runtime 的调度、取消、恢复和错误处理
```

- 单位是秒；
- **不写 `--timeout` 时，产品默认是 900 秒（15 分钟）**；
- 复杂 MCP / 多 agent 任务可显式加大，例如 `1800`。

### 4.3 兼容旧写法

下列写法仍可用，但语义都等价于自动启动：

```text
/workflow plan 审查 UI
/workflow plan --manual 审查 UI
/workflow --manual --timeout 600 审查 UI
```

`--manual` 会被忽略，不会进入人工审批。

---

## 5. 复杂任务测试示例

```text
/workflow --timeout 1800 设计并执行一个复杂 GA workflow 验证：包含一个 research agent 负责只读调研，一个 coding agent 负责在临时目录中实现一个很小的安全函数和测试，一个 review agent 负责审查实现质量，一个 synthesis agent 负责汇总证据和风险。要求不要读取 mykey.py、mykey.json、mcp.json，不要提交，不要写临时目录以外的文件。
```

关注点：

- 是否至少包含 research / coding / review / synthesis 之类阶段或 agent；
- coding 是否限制在临时目录；
- 是否明确禁止读取密钥文件；
- 是否有测试或验证步骤；
- 是否有最终 synthesis。

涉及 MCP + skill 的例子：

```text
/workflow --timeout 1800 设计一个复杂 GA workflow：一个 agent 使用可用的 MCP 搜索工具调研 2026 FIFA World Cup July 7 2026 matches results today，一个 agent 使用现有 using-superpowers skill 在临时 workspace 写入并读取一个小型 Python 函数，一个 synthesis agent 汇总结果。要求使用真实工具，不要读取 mykey.py、mykey.json、mcp.json，不要提交，所有写入必须限制在临时 workspace。
```

---

## 6. 列表、详情、停止、恢复

### 列表

```text
/workflows
```

### 详情

```text
/workflow detail <runId>
```

### 停止

```text
/workflow stop <runId> 用户停止
```

### 恢复

对 `succeeded` / `failed` / `killed` / `interrupted` 的 run：

```text
/workflow resume <runId>
```

会创建新的 run，并尽量 cache 前缀 agent 结果后继续执行。

---

## 7. Live status bar

运行中底部可能显示：

- `Enter view`：打开 detail；
- `x stop`：停止正在运行的 workflow；
- `N/M agents done`：已完成 agent 数；
- `tok`：token 使用摘要。

注意：

- 只有输入框为空时，`Enter` / `x` 才会作为 status bar 快捷键；
- 正在输入文字时不会被 status bar 抢走。

---

## 8. 安全写法建议

在任务描述中明确写约束，例如：

```text
不要读取 mykey.py、mykey.json、mcp.json，不要打印 API key/token/credential，不要提交，不要写临时目录以外的文件。
```

只读任务建议加：

```text
只读分析，不要修改文件。
```

编码任务建议加：

```text
先写测试，再实现；只修改与任务相关的文件；完成后运行测试并总结证据。
```

因为产品路径会自动执行，**写操作 / MCP / 高成本任务** 请在 task text 里写清楚边界，而不是指望人工审批闸门。

---

## 9. 常见问题排查

### 9.1 `/workflow` 没有执行

检查：

- 是否输入了 task text（单独 `/workflow` 只打开列表）；
- 是否只是补全了 `/workflow `，还没输入任务；
- 是否当前后端正忙；
- 是否 planner/API（`llm.yaml` 当前 profile）可用。

### 9.2 为什么没有 `awaiting_approval`？

当前产品路径固定 `autoApprove=true`。planner 校验通过后直接 `running`。  
旧文档中的 `--manual` / approve / deny 已废弃。

### 9.3 workflow 失败

```text
/workflow detail <runId>
```

查看 error、agent 失败原因、tool denied、progress。

### 9.4 想中途停止

```text
/workflow stop <runId>
```

或 status bar 上 `x`。

---

## 10. 建议的自测清单

### 基础可用性

```text
/workflows
```

```text
/workflow --timeout 600 请只读总结当前项目 workflow 功能的入口和状态展示，不要修改文件
```

```text
/workflow detail <runId>
```

### UI 入口可发现性

输入：

```text
/workflo
```

预期 slash suggestions 中能看到：

```text
/workflow
```

按 Tab/Enter 应补全为 `/workflow `（需要继续输入任务，不会空跑）。

### 复杂能力

```text
/workflow --timeout 1800 设计一个复杂 GA workflow：包含 research、coding、review、synthesis 四类 agent。coding 只能写临时目录，必须有测试或验证步骤；不要读取 mykey.py、mykey.json、mcp.json；不要提交。
```

---

## 11. 完整示范会话

```text
/workflow --timeout 900 请只读审查 GA Ink UI workflow 功能是否可用。检查 /workflow 启动入口、/workflows 列表、workflow detail overview、agent detail、live status bar，以及相关测试覆盖。不要修改文件，不要读取 mykey.py、mykey.json、mcp.json，不要提交。
```

然后：

```text
/workflows
```

```text
/workflow detail <runId>
```

运行中可：

```text
/workflow stop <runId> 用户停止测试
```

---

## 12. 判断测试是否成功

一个成功的 workflow 使用体验通常满足：

- `/workflow <任务>` 能创建 run 并自动执行；
- `/workflows` 能看到 run；
- `/workflow detail <runId>` 能看到结构化 overview；
- 运行中 status bar 能显示摘要；
- agent detail 能看到 Prompt / Activity / Outcome；
- 结束后状态为 `succeeded`，或失败时能看到明确错误原因；
- 不需要人手写 JS，也不需要 approve/deny。

---

## 13. 历史回归记录说明

2026-07-07 及更早文档中记录过 `/workflow plan --manual` + `/workflow approve` 的真实 bridge 回归。  
**自 2026-07-22 起，产品入口收敛为单一自动启动**；那些审批步骤仅作为历史实现与底层 API 兼容说明，不再是用户操作路径。

底层 bridge 仍保留 `workflow_plan(auto_approve=...)`、`workflow_draft`、`workflow_approve` 等方法，主要供自动化测试与内部兼容使用，Ink UI 用户 slash 不再暴露它们。

复杂 MCP + skill 真实 E2E 仍可用：

```text
GA_RUN_REAL_API_E2E=1 GA_RUN_REAL_MCP_E2E=1 GA_WORKFLOW_LLM_PROFILE=grok GA_REAL_API_EXPECTED_MODEL=grok-4.5 GA_REAL_API_EXPECTED_NAME=grok python tests/real_complex_workflow_mcp_skill_coding_e2e.py
```
