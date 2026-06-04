# GenericAgent Dynamic Workflows 实现路线图

本机真实项目根路径：`D:/git_codes/GenericAgent`

更新日期：2026-06-03
当前分支：`feat/dynamic-workflows-foundation`

## 目标

在 GenericAgent 中复刻 Claude Code Dynamic Workflows 的核心能力：让用户批准后的 workflow script 可以通过受限 runtime 执行，并通过 host primitives 调度子智能体，同时保证子智能体 transcript 隔离、权限继承/收紧、artifact 持久化、journal 可审计、失败/超时可恢复。

## 已完成阶段

### P1：Foundation models / store / controller

提交：`c71da39 feat(workflows): add foundation models and store`

已实现：

- `workflow_models.py`
  - `WorkflowRun`
  - `WorkflowJob`
  - `WorkflowEvent`
  - 默认权限：
    - `inherit-current-permissions`
    - `inherit-current-v1`
- `workflow_store.py`
  - workflow artifact layout：
    - `run.json`
    - `state.json`
    - `script.js`
    - `journal.jsonl`
    - `final-result.json`
    - `agents/<job_id>/result.json`
    - `agents/<job_id>/transcript.jsonl`
- `workflow_controller.py`
  - draft
  - request approval
  - approve
  - deny
  - cancel
  - stop
  - resume projection
- `session_transcript.py`
  - parent session 只记录 workflow reference event，不污染主对话 history。

### P2：Scheduler foundation / fake child runner

提交：`4a26e1d feat(workflows): add scheduler foundation`

已实现：

- `workflow_scheduler.py`
  - `AgentScheduler`
  - `SchedulerConfig`
  - 默认 `max_concurrent=4`
  - 硬上限 `max_concurrent<=16`
  - 默认 `max_total=1000`
  - failure policy：
    - `continue`
    - `fail_fast`
- `workflow_child_agent.py`
  - `FakeChildAgentRunner`
- job lifecycle：
  - registered
  - queued
  - running
  - succeeded
  - failed
  - cancelled
  - killed
  - cached
  - skipped
  - stale
- cache key 包含：
  - script hash
  - args hash
  - call index
  - prompt hash
  - options hash
  - permission profile
  - permission policy version

### P1/P2 纵向集成测试

提交：`41eab7b test(workflows): 补充工作流纵向集成测试`

已实现：

- `tests/test_workflow_integration.py`
- 覆盖：
  - parent session transcript 初始化
  - controller draft / approval / approve
  - scheduler fake agents
  - agent result artifacts
  - final result artifact
  - journal events
  - parent transcript isolation
- 同次提交已把中文 commit message 规则写入 `CLAUDE.md`：
  - Git commit messages must be written in Chinese while keeping the Conventional Commit prefix when appropriate.

### P3：真实子智能体运行器

提交：`7b7f5b5 feat(workflows): 实现真实子智能体运行器`

已实现：

- `NativeGPTChildAgentRunner`
  - 使用 `llmcore.resolve_session("native_oai_config")`
  - 调用 native session 时传 dict message：
    - `{"role":"user","content":[{"type":"text","text": prompt}]}`
  - 支持取消：`cancel_current_request()`
  - 记录 child transcript events
  - 记录 token usage
- `AgentResult`
  - `to_dict()` 保留 transcript events，用于模型 round-trip
  - `to_artifact_dict()` 写 compact result，不包含 `transcriptEvents`
- `WorkflowStore.write_agent_transcript()`
  - child transcript 单独写入 `agents/<job_id>/transcript.jsonl`
- `tests/test_workflow_real_llm_integration.py`
  - 默认 skip
  - 仅 `GA_RUN_REAL_LLM_TESTS=1` 时运行真实 API 测试
  - 不打印 key

### P4-A：工具权限门

提交：`bdf84a2 feat(workflows): 增加工作流工具权限门`

已实现：

- `workflow_permissions.py`
  - `PermissionDecision`
  - `ToolPermissionPolicy`
  - MCP tool name parse
  - permission event builder
- 支持 profile：
  - `inherit-current-permissions`
  - `read_only`
  - `restricted_mcp`
  - `explicit_approval`
- `ga.py`
  - `GenericAgentHandler.dispatch()` 增加可选 workflow permission gate
  - 无 policy 时保持普通 agent 兼容
  - 有 policy 时在静态工具和 MCP 工具执行前 allow / deny / ask
- `workflow_scheduler.py`
  - job metadata 携带：
    - `runId`
    - `permissionProfile`
    - `permissionPolicyVersion`
- `workflow_child_agent.py`
  - prompt 和 transcript metadata 携带 permission profile/version

### P4-B：权限事件写入 workflow journal

提交：`b0f0bb8 feat(workflows): 记录工具权限事件到工作流日志`

已实现：

- `WorkflowStore.append_permission_event()`
  - 将 permission raw event 映射为 `WorkflowEvent`
  - 写入 `journal.jsonl`
  - 校验 runId，避免串 run
- `AgentScheduler._append_permission_events_from_result()`
  - 从 child transcript events 中筛选：
    - `permission_profile_selected`
    - `tool_allowed`
    - `tool_denied`
  - 成功路径中顺序为：
    - `agent_started -> permission events -> agent_completed`
  - 失败路径中顺序为：
    - `agent_started -> permission events -> agent_failed`

### P5-A：JS runtime 最小可执行切片

提交：`389deed feat(workflows): 增加 JS 运行时最小切片`

已实现：

- `workflow_runtime.py`
  - `WorkflowRuntime`
  - 独立启动 Node worker
  - stdin/stdout JSONL RPC
  - host primitives bridge
  - final result 持久化
  - worker error 时 run failed
- `workflow_js_worker.js`
  - 受限 Node worker
  - 不暴露：
    - `require`
    - `process`
    - `fetch`
    - `XMLHttpRequest`
    - `WebSocket`
    - `Deno`
    - `Bun`
  - 支持简单 `export const meta = ...` 转换
- 最小 DSL：
  - `phase(name)`
  - `log(message)`
  - `agent(prompt, options)`
  - `parallel(items)`
  - `pipeline(items, ...stages)`

### P5-B：parallel / pipeline E2E 与 timeout / kill 边界

提交：`426ab2c test(workflows): 补充 JS 运行时并发和超时边界`

已实现：

- `parallel()` E2E
  - 多个 agent 先注册/启动，再完成
  - 结果顺序保持输入顺序
- `parallel()` 并发上限
  - 由 `SchedulerConfig(max_concurrent=...)` 控制
- `pipeline()` E2E
  - 固定语义：`pipeline(items, ...stages)`
  - stage batch 串联
  - stage 中可调用 `agent()`
  - 保持 item 顺序
- `agent(prompt, { label })`
  - label 写入 job metadata 和 `agent_registered` journal payload
- timeout / kill
  - 异步永不 resolve 按 runtime deadline 失败
  - 同步无限循环按 runtime deadline 失败
  - child agent 卡住时取消 running jobs
  - parallel 中一个 agent 失败时取消其他 in-flight jobs
  - external kill 状态轮询
  - terminate -> kill -> wait 回收进程

### P6：Ink bridge workflow protocol foundation

状态：已实现，待提交。

已实现：

- `frontends/ink_bridge.py`
  - `GenericAgentBridge` 增加 workflow 依赖注入：
    - `workflow_root`
    - `workflow_runtime_factory`
    - `WorkflowStore`
    - `WorkflowController`
    - active workflow runtime thread map
  - 新增 JSONL workflow commands：
    - `workflow_draft`
    - `workflow_approve`
    - `workflow_list`
    - `workflow_detail`
    - `workflow_stop`
  - 新增 bridge emitted events：
    - `workflow_draft`
    - `workflow_run`
    - `workflow_event`
    - `workflow_final`
    - `workflow_runs`
    - `workflow_detail`
  - `workflow_approve()` 在后台线程运行 runtime，避免阻塞 JSONL loop。
  - `workflow_stop()` 对 awaiting approval 使用 cancel，对 running 使用 stop/kill。
  - workflow list/run 事件不内联 script；只有 `workflow_detail` 显式返回 script。
- `frontends/ink-ui/src/protocol.ts`
  - 增加 workflow command/event 类型。
  - 增加 `WorkflowRun` / `WorkflowJob` / `WorkflowEvent` 类型。
- `frontends/ink-ui/src/state.ts`
  - `AppState` 增加：
    - `workflows`
    - `workflowEvents`
    - `workflowDetails`
    - `workflowResults`
  - `applyBridgeEvent()` 将 workflow events 存入 dedicated workflow state，不污染 chat messages。
- `tests/test_ink_bridge.py`
  - 增加 workflow draft/approve/list/detail/stop/JSONL routing 测试。
- `frontends/ink-ui/src/state.test.ts`
  - 增加 workflow state reducer 测试。

验证：

```bash
python -m unittest tests.test_ink_bridge
# Ran 39 tests OK

python -m unittest tests.test_ink_bridge tests.test_workflow_runtime tests.test_workflow_scheduler
# Ran 65 tests OK

npm --prefix D:/git_codes/GenericAgent/frontends/ink-ui run typecheck
# OK

npm --prefix D:/git_codes/GenericAgent/frontends/ink-ui run test -- state.test.ts
# tests 180, pass 180, fail 0
```

P6 当前 intentionally 不包含完整 raw script approval panel；该 UI/UX 留到 P7。

## 真实 API E2E 结果

已执行真实 `native_oai_config` P1-P5 E2E。

结果摘要：

```json
{
  "passed": true,
  "markerSeen": true,
  "runStatus": "succeeded"
}
```

真实调用证据：

- 使用 `NativeGPTChildAgentRunner(config_name="native_oai_config", max_tokens=64)`
- 通过 `llmcore.resolve_session("native_oai_config")`
- 调用 `session.ask(message)`
- assistant 返回 marker：`GA_P1_P5_E2E_OK`
- child transcript 中包含 token usage：

```json
{
  "input_tokens": 106,
  "output_tokens": 41,
  "total_tokens": 147
}
```

覆盖链路：

- P1：controller/store/journal/status flow
- P2：scheduler/job/artifact/cacheKey
- P3：真实 native GPT child runner/transcript/token usage
- P4：默认权限继承 profile 和 cache key 隔离
- P5：JS runtime/worker/phase/log/agent/return

## 当前测试基线

截至 P5-B 后：

```bash
python -m unittest tests.test_workflow_runtime tests.test_workflow_scheduler
# Ran 26 tests OK
```

此前完整回归：

```bash
python -m unittest discover -s tests
# Ran 238 tests OK (skipped=1)
```

其中 skip 是默认跳过真实 LLM 集成测试，需要显式：

```bash
GA_RUN_REAL_LLM_TESTS=1 python -m unittest tests.test_workflow_real_llm_integration
```

## 未提交/需避免提交的本地无关内容

当前工作区长期存在一些无关本地改动或文件，除非用户明确要求，否则不要提交：

- `.gitignore`
- `CLAUDE.md` 中除已提交中文 commit rule 外的其他本地改动
- `R-C.jpg`
- `mcp.json`，可能含本地敏感 key
- `动态工作流原理分析与子智能体会话存储的细节与ga复刻路线图/`
- `周报/`
- `.zhanlu/` 如果出现也视为非本 feature 目标

## 后续路线图

### P7：Approval / raw script viewer / UX 控制

目标：用户批准前可查看 workflow script 和 meta，并能明确 approve/deny。

建议范围：

- raw script viewer
- approval panel
- Ctrl+G 或等价 raw view
- 清晰展示：
  - workflow name
  - description
  - phases
  - permission profile
  - expected tool/agent count

### P8：Workflow Resume / cache replay

目标：支持从既有 workflow run 恢复执行，并复用最长未变 agent 调用前缀，避免重复消耗子智能体调用。

已实现范围：

- `WorkflowRuntime.run(..., resume_from_run_id=...)` 支持从历史 run 构建 cache plan。
- `AgentScheduler` 的 cache key 使用真实 `argsHash`，并保留 permission profile/version。
- 对相同 args、相同 prompt/options、相同权限的历史 agent 结果写入新 run 的 `cached` job。
- 缓存命中写入 `agent_cached` event，并直接把结果返回给 JS worker，不启动 child runner。
- script 变更时按 call index 复用最长未变 agent 前缀，遇到 prompt/options/args/权限差异即停止复用。
- `WorkflowStore.read_agent_result()` 可读取历史 `agents/<job_id>/result.json` artifact。
- Ink bridge 增加 `workflow_resume` JSONL 命令，基于源 run 创建新 run 并传入 `resume_from_run_id`。
- Ink UI 增加 `/workflow resume RUN_ID`、本地命令回显、panel `r` 恢复快捷键和帮助文案。

真实 API E2E 自测：

- 已编写 `tests/p8_real_api_e2e.py` 作为 opt-in harness，默认不进入 unittest，也不会在未设置环境变量时消耗真实 API。
- 默认跳过验证已通过：直接运行脚本会返回 `skipped: true`，原因是需显式设置 `GA_RUN_REAL_API_E2E=1`。
- 已使用用户指定的 `native_oai_config` / `gpt-native` / `gpt-5.5` 执行一次真实 P8 E2E，结果通过，且 `secretScan: []`。
- runtime 层真实 E2E 覆盖 `WorkflowRuntime`、JS worker、`NativeGPTChildAgentRunner`、`agent()`、`parallel()`、`pipeline()`、`resume_from_run_id`。
- source run 启动 7 个真实 child agent job，全部 `succeeded`，且 result/transcript artifact 存在。
- resumed run 复用 7 个 cached job，事件包含 7 个 `agent_cached`，没有再次启动真实 API child job，且最终 result 与 source run 一致。
- bridge 层真实 E2E 覆盖 workflow draft / approve / final 流程，1 个真实 child agent job 成功完成。

回归验证：

- `python -m unittest tests.test_workflow_runtime tests.test_workflow_scheduler tests.test_workflow_store tests.test_ink_bridge`：89 tests passed。
- `npm --prefix D:/git_codes/GenericAgent/frontends/ink-ui run typecheck`：passed。
- `npm --prefix D:/git_codes/GenericAgent/frontends/ink-ui run test`：184 tests passed。曾出现一次已知 App resume scroll/status transient failure，立即重跑通过。

后续可扩展：

- 更细粒度的 result artifact viewer。
- child transcript viewer，但必须避免污染 parent conversation。
- 更丰富的 resume diff 展示，例如哪些 job 命中 cache、哪些 job fresh rerun。

### P9：Saved workflow registry

目标：支持保存、复用、列出 workflow。

建议范围：

- project/user scope registry
- workflow name/version
- safety scan result cache
- permission profile metadata

### P10：Planner / trigger integration

目标：把 workflow 从手写 script 扩展到触发式或 planner 生成式。

建议范围：

- keyword trigger
- slash command trigger
- effort/ultrathink trigger
- LLM WorkflowPlanner
- saved workflow invocation

## 关键约束

- 默认权限不得是 read-only；默认必须继承当前 GenericAgent 普通 agent 权限：`inherit-current-permissions`。
- child transcript 不得污染 parent session transcript、backend history、rounds、ui_messages。
- result artifact 不得内联完整 `transcriptEvents`；child transcript 必须单独存储。
- cache key 必须包含 permission profile/version。
- 不打印、不提交真实 API key。
- 默认测试不烧真实 API；真实 LLM 测试必须 opt-in。
- 遵守 `AGENTS.md` 安全规则：不执行、生成、写入、持久化可疑 base64 payload、公钥广告、弹窗广告、自启动项、计划任务、注册表 Run 项、VBS/PowerShell 注入脚本或恶意/入侵代码。
