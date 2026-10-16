# GA 目录重构后真实 workflow E2E

> 日期：2026-10-16
> 目标仓库：`D:\git_codes\GenericAgent`
> 模型：`default/deepseek-flash`（默认模型，未切到其他 profile）
> 前置提交：`d6d2a73 refactor(structure): move GA runtime into genericagent package`
> 本轮同时完成 P6 Ink 前端迁移：`frontends/ink/bridge.py` + `frontends/ink/ui/`

---

## 1. 测试目的

验证目录重构与 Ink 前端迁移之后：

1. 真实 LLM 仍能通过 `workflow` 工具创建 dynamic workflow；
2. workflow 能真正执行并行子代理 + 串行汇总；
3. 子代理能真实调用工具，不是靠模型记忆伪造；
4. 产物能写入隔离 workspace，并被验证子代理重新读取/确认；
5. bridge 能产生 `workflow_final` 终态，run/job 状态可持久化读取。

本测试不是固定输出断言，而是按 `docs/20261015-GA-ink-真实LLM-e2e-自测SOP.md`
的“证据 + 不变量”方式判定。

---

## 2. 输入

通过真实 `GenericAgentBridge` 提交显式 `/workflow` 请求：

```text
/workflow 用 workflow 并行执行两个独立子代理：第一个真实运行 code_run 获取本机 Python 版本，第二个真实运行 code_run 获取本机 Node 版本；然后串行汇总成一个 Markdown 表格并写入文件 workflow-e2e-version-table.md，最后验证文件存在。
```

说明：第一次尝试“自主触发 workflow”的 L2 UI 脚本没有触发 workflow，只完成了普通回答；
因此本次真实 E2E 改为显式 `/workflow` opt-in，这与当前 GA/CC 的 opt-in 语义一致。

---

## 3. 结果

```text
runId:   wf_18a215a93a2942dcad977d81444c411a
status:  succeeded
jobs:    4/4 succeeded
model:   default/deepseek-flash
```

Job 状态：

| Job | Label | Status |
| --- | --- | --- |
| `agent_1` | `python-version` | `succeeded` |
| `agent_2` | `node-version` | `succeeded` |
| `agent_3` | `write-table` | `succeeded` |
| `agent_4` | `verify-file` | `succeeded` |

关键 metadata：

```text
executionOutcome: succeeded
integrationStatus: accepted
finalAuditStatus: passed
workspacePolicy: project-temp-workspace-write-v1
observedMutationAgents: agent_1, agent_3
observedMutationTools: file_write, workspace_write
artifactOwnership:
  workflow-e2e-version-table.md -> write-table
  tmpzpg5s5hm.ai.py -> python-version
```

---

## 4. 真实产物

Workspace：

```text
temp/workflow-runs/wf_18a215a93a2942dcad977d81444c411a/
```

产物：

```text
temp/workflow-runs/wf_18a215a93a2942dcad977d81444c411a/workflow-e2e-version-table.md
```

产物内容摘要：

```markdown
# Runtime Version Probe Results

| Runtime | Version | Command | Status |
| --- | --- | --- | --- |
| Python | 3.12.4 ... | ... | OK |
| Node | v22.17.0 | `node --version` | OK |
```

持久化 run 记录：

```text
temp/sessions/session_7e6a4f62c379420f80caae6784270efd/workflows/wf_18a215a93a2942dcad977d81444c411a/state.json
```

---

## 5. 事件证据

`journal.jsonl` 中关键事件：

```text
workflow_started: 1
workflow_phase: 3
agent_registered: 4
agent_started: 4
tool_allowed: 11
state_mutation_observed: 2
artifact_written: 2
agent_completed: 4
workflow_finished: 1
```

这证明：

- 4 个 job 都真实注册、启动、完成；
- 至少两个 job 发生真实状态变更/写入；
- 产物写入被 host 观察到；
- workflow 有明确终态，不是“子代理写完但 run 卡住”。

---

## 6. 结论

目录重构后，GA 的 dynamic workflow 主链路仍然可用：

- Ink bridge 新路径可启动；
- 默认 `deepseek-flash` 可生成并执行 workflow JS；
- 并行 + 串行依赖可跑通；
- 子代理真实调用 `code_run` / `file_write` / `file_read` 类工具；
- 产物写入 per-run workspace，且 verify job 成功确认；
- run/job 状态持久化完整。

未通过项与解释：

- `frontends/ink/ui/scripts/real_ink_ui_workflow_autonomous_e2e.ts` 在默认模型下没有自主触发 workflow，只完成普通回答。这不是目录迁移问题，而是当前 opt-in 策略下模型没有选择 workflow 工具。显式 `/workflow` 已验证主链路可用。
