# P8 Dynamic Workflows E2E 覆盖分析、缺口与下一步执行计划

> 由动态工作流（7 个并行智能体）分析生成
> 基于: `docs/P8-e2e-todo.md` + 完整代码库交叉对照
> 生成日期: 2026-06-05

---

## 1. P8 当前状态总览

### 1.1 目标回顾

P8 的目标是实现 Dynamic Workflows 的 Resume / Cache Replay 机制。核心能力：

- 基于 JS Worker 的 workflow 脚本运行时 (`WorkflowRuntime`)
- 真实 child agent 启动 (`NativeGPTChildAgentRunner`)
- 最长成功前缀缓存复用 (`resume_from_run_id` + `cache_key` + 前缀匹配)
- Ink bridge 完整生命周期（draft → approve → running → final）
- Child agent 的 result/transcript artifact 隔离
- Secret 扫描安全门
- Permission profile/policy version 进入 cache key

### 1.2 交付提交

P8 分布在以下提交（branch `feat/dynamic-workflows-foundation`）：

| 提交 | 说明 |
|---|---|
| `c71da39` | Foundation models and store |
| `4a26e1d` | Scheduler foundation |
| `41eab7b` | 纵向集成测试 |
| `7b7f5b5` | 真实子智能体运行器 |
| `bdf84a2` | 工作流工具权限门 |
| `b0f0bb8` | 记录工具权限事件到工作流日志 |
| `389deed` | JS 运行时最小切片 |
| `426ab2c` | 并发和超时边界测试 |
| `0a3b330` | 接入 Ink 工作流协议 |
| `f840926` | 增加 Ink 工作流审批界面 |
| **`aa0a2b0`** | **实现 P8 resume 缓存复用（关键交付提交）** |
| `93c635e` | 补充 P8 failed 和 killed resume 真实 E2E |
| `fa2954d` | 补充 P8 权限继承 deterministic E2E |
| `0eee3ef` | 补完 Native child 工具继承验证 |
| `4f4e04f` | 补充真实 API 工具继承 E2E |
| `42b0b6c` | 增加真实 MCP 诊断 E2E |

### 1.3 按层覆盖总结

| 层 | 当前覆盖 | 文件 | 用例数 |
|---|---|---|---|
| **Unit (Runtime)** | 完整 — phase/log/agent/parallel/pipeline/timeout/cancel/resume/kill | `test_workflow_runtime.py` | 14 |
| **Unit (Scheduler)** | 完整 — register/cache_key/permission/failure_policy/stop | `test_workflow_scheduler.py` | 14 |
| **Unit (Store)** | 完整 — create/event/write_result/transcript/resume_projection/permission | `test_workflow_store.py` | 9 |
| **Unit (Bridge)** | 正常路径 + 部分拒绝路径 | `test_ink_bridge.py` | 6 |
| **Real API E2E** | 正常主路径 + failed/killed resume + 权限 metadata smoke + 真实 Native child file/skill/stub MCP tool calling + 真实 MCP diagnostic（opt-in） | `p8_real_api_e2e.py` | 6 main cases + 1 diagnostic |
| **Bridge E2E** | 仅正常路径（succeeded 终态） | `p8_real_api_e2e.py` | 1 case |

### 1.4 总体覆盖度估算

**按 TODO 文档的 12 项评估：**

- ✅ 完全覆盖（单元 + 真实 API E2E）：1 项（正常主路径）
- 🔶 单元覆盖完全但无真实 API E2E：3 项（parallel 部分失败、kill/stop/interrupted、cache key 变体）
- 🔶 单元覆盖部分 + 真实 API E2E 部分：3 项（超时边界、failed/killed resume、大 artifact 隔离）
- ❌ 完全未覆盖（任何层级）：4 项（网络抖动、rate limit/429、bridge 终态事件、返回格式异常）

**粗估百分比：**
- 单元测试覆盖率：~85%（几乎全部核心路径已覆盖）
- 真实 API E2E 覆盖率：~40%（正常路径、failed/killed resume、权限/工具/skill/MCP 继承与真实 MCP diagnostic 已覆盖）
- Bridge E2E 覆盖率：~15%（仅 succeeded 路径）

---

## 2. 已验证完成项

### 2.1 正常主路径真实 API E2E（已提交 `aa0a2b0`）

**文件：** `tests/p8_real_api_e2e.py`
**用例：** `run_runtime_real_api_case()` + `run_bridge_real_api_case()`

| 验收点 | 结果 |
|---|---|
| 使用 `native_oai_config` / `gpt-native` / `gpt-5.5` | `check_profile()` 验证通过 |
| `agent()` 正常执行 | script 包含 `agent('Reply...GA_P8_SINGLE')` |
| `parallel()` 正常执行 | script 包含 2-agent parallel |
| `pipeline()` 正常执行 | script 包含 4-agent pipeline |
| source run 7 个 child agent 全部 succeeded | `len(runner.started_job_ids) == 7`，全部 `status == "succeeded"` |
| resumed run 复用 7 个 cached job | `resume_runner.started_job_ids == []`，全部 `status == "cached"` |
| `resumeResultEqualsSource: true` | `resume_outcome.result == outcome.result` |
| result/transcript artifact 存在 | `resultExists` 和 `transcriptExists` 均为 true |
| result JSON 不内联完整 transcriptEvents | `resultJsonOmitsTranscriptEvents` 为 true |
| result JSON 包含 transcriptRef | `result_data["transcriptRef"]` 指向 `agents/agent_1/transcript.jsonl` |
| parallel job 全部在第一个 completion 前已注册 | `assert_parallel_started_before_first_completion()` 通过 |
| 连续 3 轮全部通过 | `runtime_case` + `failed_resume_case` + `killed_resume_case` + `bridge_case` 全部 `passed == True` |
| `secretScan: []` | `scan_for_secret_material()` 返回空列表 |

### 2.2 Failed Source Run Resume（已由 `93c635e` 提交）

**用例：** `run_failed_source_resume_real_api_case()`

| 验收点 | 结果 |
|---|---|
| Source run `agent_1` 成功 | `source_runner.started_job_ids == ["agent_1"]` |
| Source run 最终状态为 `failed` | `source_loaded.status == "failed"` |
| 错误信息包含 `GA_P8_FORCED_SOURCE_FAILURE` | 确认 |
| `agent_1` result/transcript artifact 保留 | 均为 true |
| Resumed run `agent_1` 为 `cached` | `resumed_jobs[0]["status"] == "cached"` |
| `cachedFromRunId` / `cachedFromJobId` 正确 | 指向 source run 的 `agent_1` |
| Resumed run `agent_2` 为 fresh job | `resume_runner.started_job_ids == ["agent_2"]` |
| Resumed run 最终 `succeeded` | `resumed_loaded.status == "succeeded"` |
| Event 计数正确 | `agent_cached: 1`, `agent_started: 1`, `agent_completed: 1` |
| Result marker 正确 | `"GA_P8_FAILED_RESUME_DONE"` |

### 2.3 Killed Source Run Resume（已由 `93c635e` 提交）

**用例：** `run_killed_source_resume_real_api_case()`

| 验收点 | 结果 |
|---|---|
| Source run `agent_1` 成功 | `source_jobs[0]["status"] == "succeeded"` |
| Source run `agent_2` 被取消 | `source_jobs[1]["status"] == "cancelled"` |
| Source run 被外部 kill | `source_loaded.status == "killed"` |
| Source error 包含 `GA_P8_FORCED_SOURCE_KILL` | 确认 |
| `workflow_killed` 事件存在 | 在 source event 中 |
| Resumed run `agent_1` 为 `cached` | `resumed_jobs[0]["status"] == "cached"` |
| Resumed run `agent_2` 为 fresh job | `resume_runner.started_job_ids == ["agent_2"]` |
| Resumed run 最终 `succeeded` | 确认 |
| Result marker 正确 | `"GA_P8_KILLED_RESUME_DONE"` |

### 2.4 权限 / MCP / Skills / Tools 继承（已补 Native child 执行链路 + 真实 API + 真实 MCP diagnostic）

**文件：** `tests/test_workflow_permission_inheritance_e2e.py` + `tests/test_workflow_child_agent.py` + `tests/p8_real_api_e2e.py` + `tests/test_p8_real_api_e2e_diagnostic.py`

| 验收点 | 结果 |
|---|---|
| 默认 permission profile 为 `inherit-current-permissions` | deterministic E2E 断言 run/job/cacheKey；真实 API smoke 断言 metadata/cacheKey |
| policy version 为 `inherit-current-v1` | deterministic E2E 与真实 API smoke 均断言 metadata/cacheKey |
| Native child 接入工具执行链路 | `0eee3ef` 让 `NativeGPTChildAgentRunner` 通过 `agent_runner_loop -> GenericAgentHandler.dispatch -> ToolPermissionPolicy` 执行工具 |
| Child 通过 dispatch 使用内置工具 | deterministic E2E 覆盖 `file_write`；Native child 测试覆盖 `file_read`；真实 API `nativeToolCallingFileSkillMcp` 覆盖真实模型主动调用 `file_read` |
| Child 加载临时 skill | deterministic E2E 与 Native child 测试覆盖 `load_skill`；真实 API case 验证真实模型主动调用并 `skillLoadOk=true` |
| Child 调用受控 MCP 工具 | deterministic/Native child 测试覆盖 stub MCP；真实 API case 验证真实模型主动调用 `mcp__p8_stub__read_marker` 且 `mcpReadOk=true` |
| 真实 MCP 非 mock 调用 | `42b0b6c` 新增 `GA_RUN_REAL_MCP_E2E=1` diagnostic-only case；实际调用 `mcp__fetch__fetch` 读取 `https://example.com`，返回 `Example Domain` |
| read_only 反事实 | 同一 runner 下 `file_write` 与非只读 MCP 被 deny；只读命名 MCP 可按策略放行 |
| Permission events 落盘 | child transcript 与 workflow journal 包含 `permission_profile_selected` / `tool_allowed` / `tool_denied` |
| Result artifact 隔离 | `result.json` 不内联 `transcriptEvents`，只保留 `transcriptRef` / `toolSummary` / payload |
| Parent transcript 隔离 | parent session transcript 不包含 child tool marker、skill marker、MCP marker 或 permission events |
| Profile/version cache miss | profile 或 policy version 改变时不命中 source cache；完全相同时命中并复制 transcript |
| 默认不烧真实 API/MCP | `python tests/p8_real_api_e2e.py` 默认 skip；真实 API 用 `GA_RUN_REAL_API_E2E=1`；真实 MCP 诊断另需 `GA_RUN_REAL_MCP_E2E=1` |

当前限制：真实 MCP diagnostic 是 diagnostic-only，不纳入主 `summary.passed` 门禁；结果依赖本机 MCP 配置、网络和外部 MCP 服务状态。Context7 未出现在 GenericAgent 当前 `mcp_runtime` discovery 列表中，当前真实 MCP 诊断覆盖的是已配置可用的 `fetch` MCP。

### 2.5 Bridge Draft/Approve/Final 成功（已提交）

**用例：** `run_bridge_real_api_case()`

| 验收点 | 结果 |
|---|---|
| `workflow_draft` 返回有效 runId | 是 |
| `workflow_approve` 返回 true | `approved == True` |
| 状态流正确 | `awaiting_approval → running → succeeded` |
| `workflow_final` 事件存在 | `final_events` 非空 |
| `final_result.status == "succeeded"` | 确认 |
| Runtime 实际启动了 child agent | `runtime_started_jobs == ["agent_1"]` |
| Event 完整性 | 关键事件全部出现 |

### 2.6 单元测试覆盖的核心实现细节

| 实现细节 | 文件 | 测试方法 |
|---|---|---|
| 安全扫描拒绝不安全脚本 | `test_workflow_runtime.py` | `test_runtime_rejects_forbidden_script_tokens_before_worker_starts` |
| Worker 脚本抛异常 → run failed | `test_workflow_runtime.py` | `test_runtime_marks_run_failed_when_worker_errors` |
| 超时取消 child agent | `test_workflow_runtime.py` | `test_runtime_timeout_cancels_running_child_agents` |
| Worker error 后 parallel cancel pending | `test_workflow_runtime.py` | `test_runtime_worker_error_after_parallel_cancels_pending_jobs` |
| 外部 kill 检测 | `test_workflow_runtime.py` | `test_runtime_observes_external_kill_state` |
| Terminate → kill → stdio close 逐级升级 | `test_workflow_runtime.py` | `test_runtime_terminate_escalates_to_kill_and_waits` |
| 缓存只复用最长未变前缀 | `test_workflow_runtime.py` | `test_runtime_reuses_longest_unchanged_agent_prefix_after_script_changes` |
| args 类型变化不 cache | `test_workflow_runtime.py` | `test_runtime_does_not_reuse_cached_agent_when_resume_args_change_type_only` |
| 跨 session 不 cache | `test_workflow_runtime.py` | `test_runtime_does_not_reuse_cached_agent_across_sessions` |
| Cache key 包含 permission 字段 | `test_workflow_scheduler.py` | `test_registers_job_with_cache_key_permission_fields_and_journal_event` |
| Permission profile 变化 → cache key 不同 | `test_workflow_scheduler.py` | `test_cache_key_changes_when_permission_profile_or_policy_version_changes` |
| Permission 事件 journal 写入（成功/失败） | `test_workflow_scheduler.py` | 两个 test 方法 |
| Continue 策略保持其他 job 运行 | `test_workflow_scheduler.py` | `test_continue_failure_policy_keeps_other_jobs_running` |
| Fail-fast 策略取消全部 | `test_workflow_scheduler.py` | `test_fail_fast_failure_policy_cancels_queued_and_running_jobs_and_fails_run` |
| Stop 取消 queue 和 running | `test_workflow_scheduler.py` | `test_stop_cancels_queued_jobs_and_requests_cancellation_for_running_jobs` |
| Resume 投影将 running 标记为 stale | `test_workflow_store.py` | `test_mark_running_jobs_stale_on_resume_projection` |
| Bridge 层 blank runId 拒绝 | `test_ink_bridge.py` | `test_workflow_resume_rejects_blank_run_id_with_protocol_error` |
| Bridge 层未完成 source resume 拒绝 | `test_ink_bridge.py` | `test_workflow_resume_rejects_unfinished_source_run` |
| Bridge 层已取消 source resume 拒绝 | `test_ink_bridge.py` | `test_workflow_resume_rejects_denied_cancelled_source_run` |
| Bridge 层 stop 正在运行的 workflow | `test_ink_bridge.py` | `test_stop_stops_active_running_workflow_instead_of_reporting_idle` |

---

## 3. 剩余缺口详细分析

### 项目 1：真实 API 超时边界 E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | 超时场景只使用 fake runner 验证（`NeverFinishesRunner`）。从未在真实 LLM API 调用场景下触发超时。`p8_real_api_e2e.py` 中的 `timeout_seconds` 设置很大（420/360/240/180），测试通过时超时从未被命中。 |
| **当前覆盖** | 单元测试 `test_runtime_timeout_cancels_running_child_agents`（fake runner）。JS worker 侧 `test_runtime_timeout_kills_never_resolving_async_script` 和 `test_runtime_timeout_uses_configured_deadline_for_sync_infinite_loop`。 |
| **缺失内容** | ① 真实 API 响应极慢时的超时行为；② Bridge 层 `workflow_approve(..., timeout_seconds=极短值)`；③ JS worker 子进程 terminate/kill 与端口回收；④ 超时后 bridge 的 `workflow_final`/error event；⑤ 超时后 activity/status 恢复 idle |
| **风险评级** | 中 |
| **建议优先级** | **P1** |

### 项目 2：真实 API 网络抖动 / 慢响应持续 E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | 没有任何统计稳定性测试。当前只做单次通过的测试，没有连续多轮的统计分析。 |
| **当前覆盖** | 无 |
| **缺失内容** | ① 连续多轮自动化执行与结果聚合；② P50/P95/P99 延迟记录；③ 偶发 5xx/429/空响应的收集与脱敏输出；④ 脱敏稳定性报告 |
| **风险评级** | 中 |
| **建议优先级** | **P2** |

### 项目 3：Rate Limit / 429 边界 E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | 没有任何 mock provider 返回 429，也没有压力测试通过增大 `parallel()` fan-out 触发真实 rate limit。 |
| **当前覆盖** | 完全无覆盖 |
| **缺失内容** | ① 增大 `parallel()` fan-out（8/16/32 child agent）；② 连续多轮执行观察 429；③ failed job 的 `agent_failed` 记录；④ 成功 job artifact 保留；⑤ workflow final status 与 failure policy 一致性 |
| **风险评级** | **高** |
| **建议优先级** | **P2**（需 opt-in + 单独 env var 控制成本） |

### 项目 4：Parallel 部分失败 E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | Scheduler 层 `continue`/`fail_fast` 已单元测试。但没有任何真实/半真实 E2E 验证 parallel 中部分 child agent 真实失败。 |
| **当前覆盖** | 单元测试 `test_continue_failure_policy_keeps_other_jobs_running`、`test_fail_fast_failure_policy_cancels_queued_and_running_jobs_and_fails_run`、`test_runtime_worker_error_after_parallel_cancels_pending_jobs` |
| **缺失内容** | ① parallel 中 child timeout + 其他 child 成功的真实 E2E；② parallel 中 child API error + 其他 child 成功的真实 E2E；③ 成功/失败 job 的 artifact 写入验证；④ `agent_failed` 事件脱敏 error 验证；⑤ bridge 层 `workflow_failed` 事件中部分失败信息 |
| **风险评级** | 中 |
| **建议优先级** | **P1** |

### 项目 5：Failed / Killed / Interrupted Run Resume E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | **Interrupted source resume 完全缺失（无任何层级测试）。** Bridge 层的 failed/killed/interrupted resume 为零覆盖。 |
| **当前覆盖** | Failed source resume: 运行时层真实 API E2E（未提交）。Killed source resume: 运行时层真实 API E2E（未提交）。Store 层 `test_mark_running_jobs_stale_on_resume_projection`。Bridge 层只测试了 succeeded source resume。 |
| **缺失内容** | ① interrupted source run resume — 构建 running 状态 run 然后从中 resume；② Bridge 层 `workflow_resume(failed_source_run_id)`；③ Bridge 层 `workflow_resume(killed_source_run_id)`；④ Prompt/options/args/permission profile 变体下的 resume |
| **风险评级** | **高** |
| **建议优先级** | **P0** |

### 项目 6：Kill / Stop / Interrupted 真实 E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | 没有测试在 **LLM 调用进行中**（streaming 途中）杀死/停止 workflow。bridge 层 `workflow_interrupted` 路径完全未测试。 |
| **当前覆盖** | 单元测试 `test_runtime_observes_external_kill_state`（fake runner）、`test_stop_cancels_queued_jobs_and_requests_cancellation_for_running_jobs`、Bridge `test_stop_stops_active_running_workflow`（`BlockingRuntime` fake runner） |
| **缺失内容** | ① LLM 调用 streaming 进行中被 kill/stop；② 中断后 artifact 保留；③ Bridge 层 `workflow_interrupted` 事件；④ 中断后 resume 完整性 |
| **风险评级** | 中 |
| **建议优先级** | **P1** |

### 项目 7：Bridge 超时后的 Final/Error 事件 E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | bridge `_run_workflow_runtime` 的 `except` 分支几乎完全无测试覆盖。`_workflow_final_payload` 的 fallback 路径完全无测试。当前只覆盖 `succeeded` 终态。 |
| **当前覆盖** | 完全无覆盖 |
| **缺失内容** | ① `failed` 终态的 bridge event 流；② `killed` 终态；③ `interrupted` 终态；④ `_workflow_final_payload` 在 result.json 缺失时的 fallback；⑤ `_run_workflow_runtime` 的 except 分支在异常后是否正确 emit error |
| **风险评级** | **高** |
| **建议优先级** | **P0** |

### 项目 8：真实 API 返回格式异常 E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | 没有任何测试注入 malformed API 响应。 |
| **当前覆盖** | 完全无覆盖 |
| **缺失内容** | ① 空 content；② streaming 中断；③ usage 缺失；④ malformed JSON / SDK exception 降级；⑤ transcript artifact 可读性 |
| **风险评级** | 中 |
| **建议优先级** | **P2**（需要 semi-real runner/mock 基础设施） |

### 项目 9：权限 / MCP / Skills / Tools 继承 E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | 该项原缺口是默认 workflow/child agent 不能退化为 read-only，且真实 Native child 要能使用工具/skills/MCP。当前已补默认 deterministic、Native child 本体、真实 API tool-calling、真实 MCP diagnostic 四层覆盖。 |
| **当前覆盖** | Cache key 包含 permission 字段和 policy version；Permission 事件 journal 写入；Store 层默认 profile 为 `"inherit-current-permissions"`；deterministic E2E 覆盖内置工具、临时 skill、stub MCP、toolSummary artifact、child transcript/journal 和 parent transcript 隔离；`NativeGPTChildAgentRunner` 已接入 `GenericAgentHandler.dispatch`；真实 API `nativeToolCallingFileSkillMcp` 覆盖真实模型主动调用 `file_read`/`load_skill`/stub MCP；`realMcpDiagnostic` 覆盖非 mock 真实 MCP `mcp__fetch__fetch`。 |
| **剩余边界** | ① explicit approval 工作流内审批闭环仍缺失；② Context7 未出现在当前 GenericAgent MCP discovery 中，若本机后续配置 Context7，可补 Context7 专项 diagnostic；③ 真实 MCP diagnostic 是报告型，不纳入主门禁。 |
| **风险评级** | **低-中** |
| **建议优先级** | **P3（扩展/专项诊断）** |

### 项目 10：大 Artifact / Transcript 隔离 E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | 核心 artifact/transcript 结构和隔离已验证。但"大量"场景完全未测试。 |
| **当前覆盖** | 单元测试覆盖了 artifact 写入、transcript ref、JSON 不内联完整 transcriptEvents。真实 API E2E 验证了 artifact 存在且结构正确。 |
| **缺失内容** | ① 大量 child transcript（1000+ 行）；② 多 job 大 transcript 下 resume copy 性能；③ Parent session transcript 完全不包含 child transcript；④ Bridge/UI detail 查看大 artifact 时不卡顿 |
| **风险评级** | 中 |
| **建议优先级** | **P3** |

### 项目 11：Workflow JS Worker 异常脚本 E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | 单元测试覆盖了 worker error 后的 parallel cancel。但脚本自身 throw、pipeline/parallel thunk throw、非法 options、不可序列化返回值、桥接层错误事件未覆盖。 |
| **当前覆盖** | `test_runtime_marks_run_failed_when_worker_errors`（`throw new Error('boom')`）、`test_runtime_rejects_forbidden_script_tokens_before_worker_starts` |
| **缺失内容** | ① Pipeline stage throw；② Parallel thunk throw；③ Agent options 非法；④ 返回值不可 JSON 序列化；⑤ 安全扫描拒绝后 bridge 行为 |
| **风险评级** | 中 |
| **建议优先级** | **P2** |

### 项目 12：Cache Key 真实变体 E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | 单元测试覆盖非常广泛。但没有任何真实 API E2E 验证 cache hit/miss 与真实 API 成本挂钩。Bridge 层 cache 验证完全缺失。 |
| **当前覆盖** | 单元测试覆盖：args 类型、跨 session、prefix 匹配、permission 变化、argsHash 计算、JSON 与字符串区分（共 7 个测试方法） |
| **缺失内容** | ① 同 script 不同 args 的真实 API E2E；② 同 args prompt 改动的真实 E2E；③ Options 改动的真实 E2E；④ Permission profile/version 改动的真实 E2E；⑤ Bridge 层 cache 验证 |
| **风险评级** | 低（cache key 是确定性计算，单元测试已非常充分） |
| **建议优先级** | **P3** |

---

## 4. 推荐下一步（按优先级排序）

### Priority P0（立即执行）

---

#### 4.1 Interrupted Source Resume E2E

**目标：** 验证 interrupted source run 的 resume 行为和最长成功前缀复用。

**前置条件：**
- Store 层 `project_resume_state()` 已实现并测试（`test_mark_running_jobs_stale_on_resume_projection`）
- Runtime 层已有 killed source resume 真实 E2E 的实现模式
- Scheduler 的 cache 前缀匹配逻辑已充分单元测试

**建议实现方式：**
- 在 `p8_real_api_e2e.py` 中新增 `run_interrupted_source_resume_real_api_case()`
- 构建 3-agent parallel script，在 agent_2 启动后但未完成前通过外部将 run 置为 kill
- 验证 `project_resume_state()` 投影后将 running agent 标记为 stale
- 验证 resume 后只复用 completed agent，stale job 重新执行

**关键验收点：**
- Source run 中 completed agent 的 artifact 保留，running agent 标记为 cancelled
- Resumed run 中 completed agent 为 `cached`，stale agent 为 fresh run
- `workflow_interrupted` 事件出现在 source journal 中
- Resumed run 最终 `succeeded` 且结果正确
- `secretScan: []`

**估计工作量：** 1-2 天（可大量复用 killed resume 代码模式）

---

#### 4.2 Bridge 层非成功终态事件 E2E

**目标：** 验证 Ink bridge 在 `failed` / `killed` / `interrupted` 结束时向 UI 推送正确的终态事件。

**前置条件：**
- `ink_bridge.py` 中 `_run_workflow_runtime` 的 try/except/finally 逻辑已完整
- `_workflow_final_payload` 的 fallback 路径已实现
- 已有 bridge 单元测试框架（`FakeRuntime` / `BlockingRuntime`）

**建议实现方式：**
- 在 `test_ink_bridge.py` 中新增 3-4 个测试方法：
  1. `test_workflow_approve_emits_failed_final` — FakeRuntime 使 run 进入 failed
  2. `test_workflow_approve_emits_killed_final` — FakeRuntime 使 run 进入 killed
  3. `test_workflow_approve_emits_interrupted_final` — FakeRuntime 使 run 进入 interrupted
  4. `test_workflow_approve_handles_runtime_exception` — FakeRuntime.run 抛出异常
- 在 `p8_real_api_e2e.py` 中扩展 bridge 失败路径 E2E

**关键验收点：**
- failed 终态：事件流包含 `workflow_failed` + `workflow_final` status=failed
- killed 终态：事件流包含 `workflow_killed` + `workflow_final` status=killed
- interrupted 终态：事件流包含 `workflow_interrupted` + `workflow_final` status=interrupted
- 运行时异常：emit `workflow_run_failed` error event
- 所有场景下 finally 块执行：activity → None, status → idle

**估计工作量：** 1-2 天

---

#### 4.3 权限 / MCP / Skills / Tools 继承 E2E（已完成，保留扩展项）

**状态：** 已由 `fa2954d`、`0eee3ef`、`4f4e04f`、`42b0b6c` 覆盖核心验收点。

已完成：
- Child agent 默认权限不是 read-only。
- Native child 本体走 `GenericAgentHandler.dispatch`。
- Tool events 只写入 child transcript / artifact。
- Parent backend history 不包含 child tool transcript。
- Cache key 包含 permission profile 和 policy version。
- Permission profile/policy version 改动导致 cache miss。
- 真实 API child 主动调用 `file_read`、`load_skill`、受控 MCP。
- 真实 MCP 非 mock diagnostic 调用 `mcp__fetch__fetch`。

剩余扩展项：
- 如当前 GenericAgent MCP 配置后续加入 Context7，可补 Context7 专项 diagnostic。
- explicit approval 工作流内审批闭环仍未覆盖。
- 真实 MCP diagnostic 目前是 diagnostic-only，若要变成门禁需另行评估稳定性和成本。

---

### Priority P1（次优先）

---

#### 4.4 超时边界真实 E2E

**目标：** 验证 `timeout_seconds` 极短值时 workflow 正确进入 failed/killed 终态，子进程被回收，bridge 事件正确。

**前置条件：**
- Runtime 层 timeout 逻辑已实现（`WorkflowRuntime.__init__` 接受 `timeout_seconds`）
- JS worker 超时 escalate 逻辑已单元测试
- Bridge 层接受 `timeout_seconds` 参数

**建议实现方式：**
- 在 workflow script 中插入 `await new Promise(() => {})` 永不 resolve 的 Promise
- 设置 `timeout_seconds=2`，验证：子进程被 terminate/kill、run 状态正确、bridge emit 终态事件

**关键验收点：**
- 不挂死，不遗留 running run，不遗留僵尸子进程
- 已产生 artifact 的 job 保持可读
- 不污染 parent transcript

**估计工作量：** 1 天

---

#### 4.5 Parallel 部分失败 E2E

**目标：** 验证 parallel 中部分 child agent 真实失败时其他 job 的状态处理。

**前置条件：**
- Scheduler 层 failure_policy 已实现并单元测试
- Worker error + parallel cancel 逻辑已单元测试

**建议实现方式：**
- 3-agent parallel script，一个 agent timeout（短 timeout）/一个正常
- 验证：成功 job artifact 写入、失败 job 的 `agent_failed` 事件（脱敏）、bridge 终态正确

**关键验收点：**
- 成功 job 不被失败 job 污染
- Failed job 有明确脱敏 error payload
- Workflow 终态与 failure policy 一致

**估计工作量：** 1 天

---

#### 4.6 Mid-Call Kill / Stop E2E

**目标：** 验证 workflow 在真实 LLM API 调用进行中被 kill/stop 后的状态和 artifact 处理。

**前置条件：**
- Runtime 层检测外部 kill 的逻辑已实现
- Scheduler stop 逻辑已实现
- Bridge stop 逻辑已实现

**建议实现方式：**
- 在 `p8_real_api_e2e.py` 中新增 mid-call kill 场景
- 使用需要较长 LLM 响应的 prompt
- 在 child agent 的 `start()` 返回后但 `poll()` 返回前，外部设置 run status = "killed"

**关键验收点：**
- 不挂死，不遗留 running workflow
- 已完成结果可恢复
- Resume 不重复执行已完成 job

**估计工作量：** 1.5 天

---

### Priority P2（后续关注）

| 项目 | 说明 | 前置条件 |
|---|---|---|
| Rate Limit / 429 边界 E2E | 成本高，需 opt-in + 单独 env var | 核心 P0/P1 完成后 |
| 网络抖动 / 稳定性 E2E | 需压力测试基础设施 | 压力测试就绪后 |
| JS Worker 异常脚本 E2E | 可在异常测试框架搭建后统一补充 | 现有单元测试模式 |
| 返回格式异常 E2E | 需 semi-real runner/mock 基础设施 | P9/P10 阶段 |

### Priority P3（低优先级）

| 项目 | 说明 |
|---|---|
| Cache Key 真实变体 E2E | 单元测试已非常充分，可在其他测试中自然覆盖 |
| 大 Artifact / Transcript 隔离 | 性能/规模测试，P9/P10 阶段评估 |

---

## 5. 执行约束与安全注意事项

### 5.1 真实 API 测试的安全门

```bash
GA_RUN_REAL_API_E2E=1 python tests/p8_real_api_e2e.py
```

- 所有真实 API E2E 必须通过 `GA_RUN_REAL_API_E2E` 环境变量 opt-in
- 默认 unittest 和 pytest 不得烧真实 API
- 压力测试（rate limit / 大并发）必须使用单独的环境变量 `GA_RUN_REAL_API_STRESS=1`
- 绝不读取、打印、提交真实 API key

### 5.2 脱敏处理

- 所有 E2E 输出必须经 `sanitize()` 函数脱敏（`SECRET_PATTERNS` 覆盖 bearer token、api_key、sk-*、JWT 等）
- 脱敏格式：匹配到 secret 的部分替换为 `[REDACTED]`

### 5.3 Secret 扫描

- 所有工作完成时必须调用 `scan_for_secret_material()` 扫描 artifact 目录
- 发现 secret 时测试必须 FAIL（`secretScan: []` 为必要条件）

### 5.4 不提交 artifact

- 真实 API 调用产生的 artifact 不得提交到 git
- 测试使用 `tempfile.mkdtemp()` 创建临时目录后自动清理
- `temp/` 目录已在 `.gitignore` 中

### 5.5 默认权限约束

- Child agent 默认 permission profile 必须为 `"inherit-current-permissions"`
- Permission policy version 默认必须为 `"inherit-current-v1"`
- 默认权限必须是非 read-only，继承 GenericAgent 当前 agent 的工具/skill/MCP 权限

### 5.6 架构约束提醒

| 项目 | 说明 |
|---|---|
| `scriptHash` | 由 `_cache_key()` 计算但**不在匹配逻辑中比较** — resume 使用同一 script 时由最长前缀匹配覆盖，这是设计选择 |
| `failure_policy` | 运行时只使用 `"continue"`，Scheduler 支持的 `fail_fast` 未被运行时使用 |
| transcript 复制 | 通过 `shutil.copyfile` 物理复制（非软链接），大量 artifact 场景需关注磁盘使用 |

### 5.7 后续阶段参考（P9 / P10）

- **P9（Saved workflow registry）**：需要将 workflow script 的持久化和版本化纳入范围，可能影响 cache key 设计
- **P10（Planner/trigger integration）**：需要 workflow 与 GenericAgent 的 planner/trigger 机制集成，child agent 的工具继承变得更加关键
- 当前 E2E 测试框架（`p8_real_api_e2e.py`）应设计为易于扩展，后续 P9/P10 E2E 建议遵循同样的 opt-in + 脱敏 + secret 扫描模式

---

## 附录：关键文件路径

| 文件 | 说明 |
|---|---|
| `tests/p8_real_api_e2e.py` | P8 真实 API E2E 主文件（工作区包含 failed/killed resume 扩展但未提交） |
| `tests/test_workflow_runtime.py` | Runtime 层单元测试（14 个用例） |
| `tests/test_workflow_scheduler.py` | Scheduler 层单元测试（14 个用例） |
| `tests/test_workflow_store.py` | Store 层单元测试（9 个用例） |
| `tests/test_ink_bridge.py` | Bridge 层单元测试（含 workflow 共 6 个用例） |
| `frontends/ink_bridge.py` | Ink bridge 实现（`_run_workflow_runtime` 第 518-541 行，`_workflow_final_payload` 第 568-576 行） |
| `docs/P8-e2e-todo.md` | P8 E2E TODO 文档（本分析起点） |
| `docs/dynamic-workflows-implementation-roadmap.md` | 完整实施路线图（P1-P10） |
