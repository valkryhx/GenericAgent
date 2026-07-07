# GA Ink UI Workflow 用户使用说明

本文面向想在 **GA Ink UI** 中亲自测试和使用 workflow 功能的用户。目标是让你可以从终端 UI 里通过 `/workflow plan ...` 发起动态工作流，查看计划、审批、执行、停止和复盘结果。

> 适用范围：GenericAgent 的 React/Ink 终端 UI，即 `ga` 或 `ga ink` 启动的界面。
>
> 安全提醒：不要把 API key、token、密码、`mykey.py`、`mykey.json`、`mcp.json` 内容粘贴进任务描述。需要真实 API / MCP 时，使用本地已有配置即可。

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
/workflow plan [--manual] [--timeout SECONDS] TASK
/workflow detail RUN_ID
/workflow approve RUN_ID
/workflow resume RUN_ID
/workflow deny RUN_ID [reason]
/workflow stop RUN_ID [reason]
```

常用命令说明：

| 命令 | 作用 |
| --- | --- |
| `/workflows` | 打开 workflow 列表，查看当前和历史 runs |
| `/workflow plan <任务>` | 根据自然语言任务创建并默认自动执行 workflow |
| `/workflow plan --manual <任务>` | 创建 workflow draft，但先等待人工审批 |
| `/workflow detail <runId>` | 查看某个 workflow 的详情、阶段和 agent 信息 |
| `/workflow approve <runId>` | 批准等待审批的 workflow 执行 |
| `/workflow deny <runId> <原因>` | 拒绝等待审批的 workflow |
| `/workflow stop <runId> <原因>` | 停止正在运行的 workflow |
| `/workflow resume <runId>` | 恢复可恢复的 workflow |

---

## 3. 推荐的第一次测试流程

第一次亲测建议使用 **manual 模式**，这样你可以先看 planner 生成的 workflow，再决定是否执行。

### 步骤 1：发起一个只读审查 workflow

在 Ink UI 输入：

```text
/workflow plan --manual --timeout 600 请只读审查当前项目的 workflow UI 实现，重点检查 slash command 入口、workflow overview、agent detail、live status bar 和测试覆盖。不要修改文件，不要读取 mykey.py、mykey.json、mcp.json，不要提交。
```

预期现象：

- UI 会创建一个 workflow run；
- 状态通常为 `awaiting_approval`；
- 底部可能出现类似 `Enter review` 的 workflow status bar；
- workflow 不会立刻执行，因为使用了 `--manual`。

### 步骤 2：查看 workflow 列表

输入：

```text
/workflows
```

预期你会看到 workflow 列表，其中包含刚创建的 run。记下或复制它的 `runId`，形如：

```text
wf_abc123...
```

### 步骤 3：查看详情

输入：

```text
/workflow detail wf_abc123
```

把 `wf_abc123` 替换成你实际看到的 runId。

预期你会看到：

- workflow 名称和状态；
- phases / agents overview；
- 每个 agent 的标签、状态；
- 如果进入 agent detail，可查看 Prompt / Activity / Outcome；
- 不应把 raw script 当作主界面展示。

### 步骤 4：批准执行

如果计划看起来合理，输入：

```text
/workflow approve wf_abc123
```

预期 workflow 开始运行。运行过程中可能看到：

- live workflow status bar；
- 已完成 agent 数；
- 当前 active agent；
- token 用量摘要；
- `/workflows` 中状态更新。

### 步骤 5：查看最终结果

执行完成后再次输入：

```text
/workflow detail wf_abc123
```

确认：

- run 状态是否为 `succeeded`；
- agents 是否完成；
- outcome 是否有可读摘要；
- 是否存在失败、拒绝工具调用或错误信息。

---

## 4. `/workflow plan` 的参数详解

### 4.1 默认自动执行

```text
/workflow plan 审查当前分支最近提交的风险，并给出修复建议
```

含义：

- planner 生成 workflow；
- 如果通过校验，通常会自动开始执行；
- 适合低风险、只读任务。

### 4.2 `--manual`：先审批再执行

```text
/workflow plan --manual 设计一个多 agent 代码审查 workflow，分别检查安全、性能、测试缺口和回归风险
```

含义：

- 只生成计划；
- 状态进入 `awaiting_approval`；
- 你需要 `/workflow approve <runId>` 后才执行。

推荐在以下场景使用：

- 任务复杂；
- 涉及写文件；
- 涉及 MCP / skill；
- 想先看 planner 是否理解正确；
- 想避免 workflow 立即开始消耗真实 API。

### 4.3 `--timeout SECONDS`：设置超时

```text
/workflow plan --timeout 900 审查 workflow runtime 的调度、取消、恢复和错误处理
```

含义：

- 设置 workflow runtime 的最长执行时间；
- 单位是秒；
- `900` 表示 15 分钟。

### 4.4 `--manual` 和 `--timeout` 可以一起用

```text
/workflow plan --manual --timeout 900 审查当前 workflow UI，并在执行前让我确认计划
```

顺序互换也可以：

```text
/workflow plan --timeout 900 --manual 审查当前 workflow UI，并在执行前让我确认计划
```

---

## 5. 复杂任务测试示例

如果你想验证 GA workflow 是否真的具备多 agent 能力，可以使用下面这个复杂任务。建议先用 `--manual`。

```text
/workflow plan --manual --timeout 1800 设计并执行一个复杂 GA workflow 验证：包含一个 research agent 负责只读调研，一个 coding agent 负责在临时目录中实现一个很小的安全函数和测试，一个 review agent 负责审查实现质量，一个 synthesis agent 负责汇总证据和风险。要求不要读取 mykey.py、mykey.json、mcp.json，不要提交，不要写临时目录以外的文件。
```

审批前检查重点：

- 是否至少包含 research / coding / review / synthesis 之类阶段或 agent；
- coding 是否限制在临时目录；
- 是否明确禁止读取密钥文件；
- 是否有测试或验证步骤；
- 是否有最终 synthesis。

如果计划合理：

```text
/workflow approve <runId>
```

如果计划不合理：

```text
/workflow deny <runId> 计划没有限制写入范围，请重新规划
```

---

## 6. 真实 MCP / Skill 复杂验证示例

如果你的环境已经配置好 MCP 和 skills，可以尝试更强的复杂任务。例如：

```text
/workflow plan --manual --timeout 1800 设计一个复杂 GA workflow：一个 agent 使用可用的 MCP 搜索工具调研 2026 FIFA World Cup July 7 2026 matches results today，一个 agent 使用现有 using-superpowers skill 在临时 workspace 写入并读取一个小型 Python 函数，一个 synthesis agent 汇总结果。要求使用真实工具，不要读取 mykey.py、mykey.json、mcp.json，不要提交，所有写入必须限制在临时 workspace。
```

注意：

- 这个任务可能消耗真实 API；
- 如果 MCP 没配置好，workflow 可能失败或 planner 生成的计划无法执行；
- 建议先 `--manual` 查看 draft；
- 真实搜索结果可能随时间变化。

---

## 7. 查看 workflow 详情时如何操作

### 7.1 打开列表

```text
/workflows
```

### 7.2 打开某个 run

```text
/workflow detail <runId>
```

### 7.3 在 overview 中查看 agent detail

在 workflow detail/overview 面板中：

- 使用 `Up` / `Down` 切换 phase；
- 使用 `Enter` 进入选中 phase 的 agent detail；
- 在 agent detail 中通常可以看到：
  - `Prompt`
  - `Activity`
  - `Outcome`
- 使用 `j` / `k` 滚动；
- 使用 `Esc` 返回 overview。

> 具体按键表现取决于当前 panel 状态和终端输入支持。

---

## 8. live workflow status bar

当 workflow 正在运行或等待审批，并且你没有打开其它 panel 时，底部可能出现 live workflow status bar。

常见显示：

```text
Enter review · › ◌ xxx awaiting approval
```

或：

```text
Enter view · x stop · › ◌ xxx 1/3 agents done · current-agent · 12k tok
```

含义：

- `Enter review`：打开等待审批 workflow 的详情；
- `Enter view`：查看正在运行的 workflow；
- `x stop`：停止正在运行的 workflow；
- `N/M agents done`：已完成 agent 数；
- `tok`：token 使用摘要。

注意：

- 只有输入框为空时，`Enter` / `x` 才会作为 status bar 快捷键；
- 如果你正在输入文字，`Enter` / `x` 不会被 workflow status bar 抢走；
- `awaiting_approval` 状态不会显示 `x stop`。

---

## 9. 什么时候用自动执行，什么时候用 manual？

推荐规则：

| 场景 | 推荐 |
| --- | --- |
| 简单只读审查 | 可以直接 `/workflow plan ...` |
| 涉及写文件 | 用 `--manual` |
| 涉及 MCP / 网络搜索 | 用 `--manual` |
| 涉及真实 API 成本较高 | 用 `--manual --timeout ...` |
| 想先看 planner 是否理解任务 | 用 `--manual` |
| 批量修复 / 复杂编码 | 强烈建议 `--manual` |

---

## 10. 安全写法建议

在任务描述中明确写约束，例如：

```text
不要读取 mykey.py、mykey.json、mcp.json，不要打印 API key/token/credential，不要提交，不要写临时目录以外的文件。
```

如果是只读任务，建议加：

```text
只读分析，不要修改文件。
```

如果是编码任务，建议加：

```text
先写测试，再实现；只修改与任务相关的文件；完成后运行测试并总结证据。
```

---

## 11. 常见问题排查

### 11.1 `/workflow plan` 没有执行

检查：

- 是否输入了 task text；
- 是否只是按 Tab/Enter 补全了 `/workflow plan `，但还没有输入任务；
- 是否当前后端正忙；
- 是否 planner/API 配置可用。

### 11.2 状态一直是 `awaiting_approval`

这是 `--manual` 的预期行为。你需要：

```text
/workflow approve <runId>
```

或者拒绝：

```text
/workflow deny <runId> <原因>
```

### 11.3 workflow 失败

查看详情：

```text
/workflow detail <runId>
```

重点看：

- 哪个 agent 失败；
- 是否工具不可用；
- 是否 MCP 未连接；
- 是否 API 失败；
- 是否权限或安全策略拒绝；
- 是否 timeout。

### 11.4 MCP 任务失败

可能原因：

- MCP server 未配置；
- MCP server 未连接；
- 对应 tool 名称不可用；
- 网络或 API 不稳定；
- planner 生成的任务没有正确指定工具使用方式。

建议先用较简单的 MCP 任务测试，再上复杂任务。

### 11.5 想停止正在运行的 workflow

方式一：

```text
/workflow stop <runId> 用户主动停止
```

方式二：

- 如果底部 status bar 显示 `x stop`；
- 并且输入框为空；
- 直接按 `x`。

---

## 12. 推荐亲测清单

你可以按下面顺序逐步测试：

### 基础可用性

```text
/workflows
```

```text
/workflow plan --manual 请只读总结当前项目 workflow 功能的入口、状态展示和审批流程，不要修改文件
```

```text
/workflow detail <runId>
```

```text
/workflow approve <runId>
```

### UI 入口可发现性

输入：

```text
/workflow p
```

预期 slash suggestions 中能看到：

```text
/workflow plan
```

按 Tab 或 Enter 应补全为：

```text
/workflow plan 
```

不会在没有 task text 时直接执行。

### manual 审批

```text
/workflow plan --manual --timeout 600 请设计一个两阶段 workflow：先分析当前仓库 workflow UI 的使用入口，再给出测试建议。只读，不要修改文件。
```

然后：

```text
/workflow approve <runId>
```

### 复杂能力

```text
/workflow plan --manual --timeout 1800 设计一个复杂 GA workflow：包含 research、coding、review、synthesis 四类 agent。coding 只能写临时目录，必须有测试或验证步骤；不要读取 mykey.py、mykey.json、mcp.json；不要提交。
```

---

## 13. 一个完整示范会话

下面是一个完整示例，你可以照着做：

```text
/workflow plan --manual --timeout 900 请只读审查 GA Ink UI workflow 功能是否可用。检查 /workflow plan 入口、/workflows 列表、workflow detail overview、agent detail、live status bar，以及相关测试覆盖。不要修改文件，不要读取 mykey.py、mykey.json、mcp.json，不要提交。
```

然后：

```text
/workflows
```

假设看到：

```text
wf_1234567890abcdef awaiting_approval
```

查看详情：

```text
/workflow detail wf_1234567890abcdef
```

如果计划合理：

```text
/workflow approve wf_1234567890abcdef
```

运行中可再次查看：

```text
/workflow detail wf_1234567890abcdef
```

如果想停：

```text
/workflow stop wf_1234567890abcdef 用户停止测试
```

---

## 14. 判断测试是否成功

一个成功的 workflow 使用体验通常满足：

- `/workflow plan ...` 能创建 run；
- `--manual` 会进入 `awaiting_approval`；
- `/workflows` 能看到 run；
- `/workflow detail <runId>` 能看到结构化 overview；
- approve 后 workflow 能运行；
- 运行中 status bar 能显示摘要；
- agent detail 能看到 Prompt / Activity / Outcome；
- 结束后状态为 `succeeded`，或失败时能看到明确错误原因。

如果以上都成立，说明 GA Ink UI 中的 workflow 入口和基本 UI 链路是可用的。
