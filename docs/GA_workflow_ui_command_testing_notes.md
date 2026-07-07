# 如何测试 GA Ink UI 内部 Workflow Slash 命令

本文记录一次针对 **GA Ink UI workflow slash 命令** 的测试方法说明，方便后续学习、复盘和扩展测试体系。

核心问题：

> 用户在 GA Ink UI 中输入 `/workflow plan ...` 后，我们如何确认它真的触发了 GA workflow，而不是只在内部函数或 mock 中看起来可用？

答案是：不要只测 planner，也不要依赖不稳定的终端按键模拟；应该从 Ink UI 的真实输入解析层开始，生成真实 `BridgeCommand`，再通过 JSONL 协议喂给真实 Python bridge，最后让真实 workflow controller/runtime 调用真实 `gpt-5.5`、MCP 和 skill。

---

## 1. GA Ink UI workflow 命令的真实链路

用户在 Ink UI 输入：

```text
/workflow plan --manual --timeout 240 帮我规划今晚健康晚餐
```

真实代码链路大致是：

```text
用户输入文本
→ frontends/ink-ui/src/inputController.ts
→ handleInput()
→ parseSlashSubmit()
→ BridgeCommand
→ bridgeClient 通过 JSONL 发给 Python
→ frontends/ink_bridge.py
→ GenericAgentBridge.workflow_plan()
→ WorkflowController / planner / runtime
→ 真实 gpt-5.5 / MCP / skill / child agents
```

因此，如果要测试“UI 命令是否真的可用”，至少要覆盖：

1. slash 文本是否被 Ink UI 解析成正确 command；
2. command 是否能通过 JSONL bridge 分发；
3. bridge 是否调用 workflow controller；
4. workflow 是否能进入 draft / approval / runtime；
5. 真实 API / MCP / skill 是否能在 runtime 中工作。

---

## 2. 第一层：从 slash 文本生成 BridgeCommand

Ink UI 的关键入口是：

```ts
handleInput(value, rawInput, key, status, pasteStore, skillNames)
```

例如用户输入：

```text
/workflow plan --manual --timeout 120 周末家庭出游计划...
```

测试时可以调用：

```ts
const decision = handleInput(
  '/workflow plan --manual --timeout 120 周末家庭出游计划...',
  '',
  { return: true },
  'idle',
  createPasteStore(),
)
```

期望得到：

```json
{
  "value": "",
  "command": {
    "type": "workflow_plan",
    "taskText": "周末家庭出游计划...",
    "autoApprove": false,
    "timeoutSeconds": 120
  }
}
```

这一层能验证：

- `/workflow plan` 被识别为内置命令；
- `--manual` 被解析为 `autoApprove: false`；
- `--timeout 120` 被解析为 `timeoutSeconds: 120`；
- 任务正文进入 `taskText`；
- 命令不会被当成普通聊天消息发给 LLM；
- 即使存在名为 `workflow` 的 skill，也不会截获 `/workflow plan`。

---

## 3. 第二层：把 BridgeCommand 喂给 Python JSONL bridge

Ink UI 最终会把 command 通过 JSONL 发给 Python bridge。

例如：

```json
{"type":"workflow_plan","taskText":"...","autoApprove":false,"timeoutSeconds":120}
```

测试时可以把这行 JSONL 喂给：

```text
python frontends/ink_bridge.py
```

`frontends/ink_bridge.py` 内部会 dispatch：

```python
elif cmd_type == "workflow_plan":
    bridge.workflow_plan(
        taskText,
        context=...,
        auto_approve=...,
        args=...,
        timeout_seconds=...
    )
```

这会进入真实：

```text
GenericAgentBridge.workflow_plan()
→ WorkflowController.create_planned_run()
→ WorkflowPlanner / LLMWorkflowPlanner
```

如果设置真实 API 环境变量，就会调用真实 `gpt-5.5` planner。

---

## 4. 第三层：测试 workflow list/detail/approve/deny/stop/resume

同样从 slash 文本开始：

```text
/workflows
/workflow
/workflow list
```

期望生成：

```json
{"type":"workflow_list"}
```

喂给 bridge 后，期望返回：

```json
{"type":"workflow_runs", "runs": [...]}
```

再如：

```text
/workflow detail wf_xxx
```

期望生成：

```json
{"type":"workflow_detail", "runId": "wf_xxx"}
```

返回：

```json
{
  "type": "workflow_detail",
  "run": {...},
  "draft": {...},
  "progress": {...}
}
```

其它命令同理：

```text
/workflow approve wf_xxx
/workflow deny wf_xxx reason
/workflow stop wf_xxx reason
/workflow resume wf_xxx
```

都应该先由 `handleInput()` 生成对应 command，再由 bridge 执行。

---

## 5. 为什么需要持久 bridge 测试？

如果测试脚本这样写：

```json
{"type":"workflow_plan", ...}
{"type":"shutdown"}
```

对于 list/detail/deny 等命令是可以的，但对自动运行或 approve 后运行的 workflow 不够严谨。

原因是：workflow 可能刚启动，bridge 就收到 shutdown，导致 run 被 `killed`。

这不能证明：

> approve 后 workflow 能真实运行完成。

因此需要 **持久 bridge 测试**：保持 `frontends/ink_bridge.py` 进程不退出，按真实用户顺序发命令。

示例顺序：

```text
1. /workflow plan --manual --timeout 240 <健康晚餐生活场景>
2. 等待 workflow_run status=awaiting_approval
3. /workflow detail <runId>
4. /workflow approve <runId>
5. 等待 workflow_run status=running
6. 等待 workflow_run status=succeeded
7. /workflow detail <runId>
8. /workflows
9. shutdown
```

这种测试可以证明：

- manual plan 创建成功；
- detail 可读；
- approve 后 workflow 真实运行；
- child agents 调用真实模型；
- 最终 run 到达 `succeeded`；
- detail 中有 progress。

---

## 6. 真实 gpt-5.5 测试环境变量

真实 API 测试通常通过环境变量启用，例如：

```text
GA_RUN_REAL_API_E2E=1
GA_WORKFLOW_PLANNER_MODE=prompt_guided
GA_WORKFLOW_PLANNER_CONFIG=native_oai_config
GA_WORKFLOW_PLANNER_REPAIR_ATTEMPTS=2
```

复杂 MCP / skill E2E 还可能使用：

```text
GA_RUN_REAL_MCP_E2E=1
GA_REAL_API_CONFIG=native_oai_config
GA_REAL_API_EXPECTED_MODEL=gpt-5.5
GA_REAL_API_EXPECTED_NAME=gpt-native
```

安全要求：

- 不读取 `mykey.py`；
- 不读取 `mykey.json`；
- 不读取 `mcp.json`；
- 不打印 API key / token / credential；
- 只通过已有配置调用真实模型。

---

## 7. MCP 和 skill 如何纳入测试？

workflow slash 命令测试证明 UI 入口和 bridge/control-plane 可用；复杂 runtime 能力则可以通过真实复杂 E2E 验证。

项目中已有复杂 E2E：

```text
tests/real_complex_workflow_mcp_skill_coding_e2e.py
```

它覆盖：

- 真实 `gpt-5.5` planner；
- 真实 MCP discovery；
- 真实 Tavily MCP tool：`mcp__tavily__tavily_search`；
- 真实 skill：`using-superpowers`；
- workflow child agent；
- 临时 workspace 写文件；
- 读回文件；
- synthesis agent；
- progress 生成。

成功结果的关键字段通常包括：

```text
passed: true
mcpCalled: true
mcpReturned: true
usingSuperpowersLoaded: true
codingFileWritten: true
codingFileOk: true
progressEntryCount: 3
jobStatuses: succeeded, succeeded, succeeded
```

把两类测试结合起来，就能判断：

1. UI slash 入口链路可用：

```text
/workflow plan → handleInput → BridgeCommand → ink_bridge.py → workflow
```

2. GA workflow runtime 复杂能力可用：

```text
gpt-5.5 + MCP + skill + coding + progress
```

---

## 8. 为什么不直接自动操作 Ink 终端 UI？

Ink 是 TUI，真实交互依赖：

- terminal raw mode；
- stdin event emitter；
- ANSI 渲染；
- 光标定位；
- 键盘事件；
- React Ink 内部 input handling。

在非交互式工具环境中，直接模拟：

```ts
stdin.emit('data', '/workflow plan ...')
```

并不稳定，可能无法触发 Ink 的 internal event emitter。

因此，不应伪装成“真实终端按键已完整自动化”。更可靠的做法是：

```text
跳过脆弱的终端按键模拟层，直接调用 Ink UI 的真实 input parser。
```

也就是使用：

```ts
handleInput(...)
```

然后继续走真实：

```text
BridgeCommand
→ JSONL bridge
→ Python bridge
→ workflow controller
→ planner
→ runtime
→ gpt-5.5 / MCP / skill
```

这样跳过的是不稳定的终端事件模拟，而不是跳过业务逻辑。

---

## 9. 这类测试能证明什么？

可以证明：

- `/workflow plan` 不会被当作普通聊天文本；
- 它能生成真实 `workflow_plan` command；
- `--manual` / `--timeout` 参数有效；
- `/workflows` 能返回 workflow list；
- `/workflow detail` 能返回 draft/progress；
- `/workflow approve` 能启动真实 workflow runtime；
- `/workflow deny` 能取消等待审批的 workflow；
- `/workflow stop` 能通过 bridge 分发；
- `/workflow resume` 即使不能恢复，也能返回结构化错误而不是崩溃；
- 真实 `gpt-5.5` API 可以完成 planner 和 child agent 运行；
- MCP 和 skill 在复杂 workflow 中能真实工作。

---

## 10. 这类测试不能完全证明什么？

不能 100% 证明：

- 真实终端中每个按键都能在所有环境中工作；
- 光标、raw mode、terminal resize、IME 等行为完全无 bug；
- ANSI 渲染在所有 terminal 下都没有问题；
- Tab/Enter 的视觉交互在所有终端都一致。

这些属于更完整的 **TUI 端到端自动化测试**，需要专门的 terminal harness。

当前方法更适合证明：

```text
用户输入 /workflow plan 后，GA 内部命令链路和 workflow 后端能力是否真的可用。
```

---

## 11. 建议的测试分层

推荐后续维护时按以下层次测试：

### 11.1 纯函数单元测试

覆盖：

```text
inputController.test.ts
slashCommands.test.ts
localCommandTranscript.test.ts
bridgeClient.test.ts
```

验证：

- slash 解析；
- suggestions；
- command 文本还原；
- JSONL serialization。

### 11.2 Python bridge 单测

覆盖：

```text
tests.test_ink_bridge.InkBridgeTest.test_jsonl_loop_dispatches_workflow_plan_command
tests.test_ink_bridge.InkBridgeTest.test_workflow_plan_creates_auto_approved_run_and_starts_runtime
tests.test_ink_bridge.InkBridgeTest.test_workflow_plan_can_create_awaiting_approval_run_when_auto_approve_false
tests.test_ink_bridge.InkBridgeTest.test_workflow_plan_emits_rejected_plan_without_runtime
```

验证：

- JSONL dispatch；
- auto approve；
- manual approval；
- rejected plan。

### 11.3 真实 slash-to-bridge 测试

覆盖：

```text
slash text
→ handleInput()
→ BridgeCommand
→ ink_bridge.py JSONL
→ workflow controller
```

验证：

- list/detail/deny/stop/resume；
- plan + real gpt-5.5 draft。

### 11.4 持久 bridge approve 测试

覆盖：

```text
plan --manual
→ detail
→ approve
→ wait succeeded
→ detail
→ list
```

验证：

- approve 后真实 runtime 成功。

### 11.5 复杂真实 E2E

覆盖：

```text
gpt-5.5 planner
+ MCP
+ skill
+ child agents
+ temporary coding
+ progress
```

验证：

- GA workflow 的真实复杂能力。

### 11.6 专门的 TUI harness（未来增强）

如果要证明真实键盘/终端行为，还需要单独建设：

- pty-based test harness；
- terminal screenshot / ANSI snapshot；
- raw mode input simulation；
- resize / cursor / IME 测试。

---

## 12. 一句话总结

测试 GA Ink UI 内部 workflow 命令时，最可靠的方式是：

```text
不模拟脆弱的终端键盘；
直接使用 Ink UI 的真实 input parser；
生成真实 BridgeCommand；
通过 JSONL 喂给真实 Python bridge；
让真实 workflow controller/planner/runtime 执行；
使用真实 gpt-5.5、真实 MCP、真实 skill 验证能力。
```

这不是纯 mock 测试，而是从 UI 命令入口到 GA workflow 后端的真实链路测试。
