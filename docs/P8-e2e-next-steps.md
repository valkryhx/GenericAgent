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
| `9e00a05` | 补充 bridge 非成功终态覆盖 |
| `f8b1850` | 补充 interrupted resume 前缀复用覆盖 |
| `1c8933f` | 增强真实 API 失败诊断 |
| `d13bde5` | 补充 bridge timeout 终态覆盖 |
| `c7616ce` | 补充真实 API timeout 诊断覆盖 |
| `7aa318b` | 补充 parallel 部分失败 E2E 覆盖 |
| `394070b` | 补充 bridge stop resume 诊断覆盖 |
| `cd662fb` | 补充 real provider mid-call stop diagnostic-only 覆盖 |
| `1370d08` | 补充真实 API stability 诊断 harness |
| `cc057a3` | 补充 Rate Limit / 429 deterministic 覆盖与真实 API stress diagnostic harness |

### 1.3 按层覆盖总结

| 层 | 当前覆盖 | 文件 | 用例数 |
|---|---|---|---|
| **Unit (Runtime)** | 完整 — phase/log/agent/parallel/pipeline/timeout/cancel/resume/kill/parallel partial failure/provider 429 | `test_workflow_runtime.py` | 16 |
| **Unit (Scheduler)** | 完整 — register/cache_key/permission/failure_policy/stop | `test_workflow_scheduler.py` | 14 |
| **Unit (Store)** | 完整 — create/event/write_result/transcript/resume_projection/permission | `test_workflow_store.py` | 9 |
| **Unit (Bridge)** | 正常路径 + resume 拒绝路径 + stop + 非成功终态事件覆盖 + timeout failed/final/error/idle + 真实 `WorkflowRuntime` JS 异常脚本 failed/final/error/idle + parallel partial failure/provider 429 final/error/idle + stop/resume prefix 覆盖 | `test_ink_bridge.py` | 56 |
| **Real API E2E** | 正常主路径 + failed/killed/interrupted resume + 权限 metadata smoke + 真实 Native child file/skill/stub MCP tool calling + 真实 API timeout bridge diagnostic + parallel partial failure diagnostic + bridge stop/resume diagnostic + real provider mid-call stop diagnostic + stability diagnostic harness + stress diagnostic harness + 真实 MCP diagnostic（opt-in）；真实 native GPT 主套件已通过 | `p8_real_api_e2e.py` / `p8_real_api_stability_e2e.py` / `p8_real_api_stress_e2e.py` | 6 main cases + 5 diagnostics + 1 stability harness + 1 stress harness |
| **Bridge E2E** | succeeded 主路径已覆盖；failed/killed/interrupted 终态已有 bridge 单元/半集成覆盖；timeout failed/final/error/idle 已有近真实 bridge 覆盖并补充真实 API diagnostic；parallel partial failure bridge final/error/idle 已补；provider 429 bridge final/error/idle 已补；bridge workflow_stop + resume prefix 串联已补；real provider mid-call stop diagnostic 已补为观察项 | `p8_real_api_e2e.py` / `test_ink_bridge.py` | 1 real bridge case + bridge unit coverage + timeout/parallel/429/stop-resume/mid-call diagnostics |

### 1.4 总体覆盖度估算

**按 TODO 文档的 12 项评估（更新至 `9e00a05` / `f8b1850` / `1c8933f` / `d13bde5` 及后续 timeout/parallel diagnostic 补强）：**

- ✅ 完全或主路径充分覆盖：4 项（正常主路径、failed/killed/interrupted resume 前缀复用、权限/MCP/Skills/Tools 继承、parallel 部分失败 deterministic/diagnostic 覆盖）
- 🔶 单元/半集成覆盖充分但真实 API E2E 仍有限：2 项（bridge 非成功终态事件、cache key 变体）
- 🔶 部分覆盖：3 项（超时边界、大 artifact 隔离、JS Worker 异常脚本）
- ❌ 仍基本未覆盖：1 项（真实 API 返回格式异常）；网络抖动/稳定性已有基础 harness，rate limit/429 已补 deterministic + stress diagnostic 基础覆盖

**粗估百分比：**
- 单元测试覆盖率：~90%（核心 runtime/scheduler/store/bridge 路径基本覆盖，bridge 非成功终态与 timeout final/error/idle 已补）
- 真实 API E2E 覆盖率：~70%（正常路径、failed/killed/interrupted resume、权限/工具/skill/MCP 继承、timeout bridge diagnostic、parallel partial failure diagnostic、bridge stop/resume diagnostic、real provider mid-call stop diagnostic、真实 native GPT 主套件通过）
- Bridge 覆盖率：~70%（succeeded 主路径、resume 拒绝、stop、非成功终态事件、timeout failed/final/error/idle、parallel partial failure final/error/idle、workflow_stop + resume prefix 串联已覆盖；real provider mid-call stop 已有 diagnostic-only 观察项）
- P0 阻塞缺口：0

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

### 2.4 Interrupted Source Run Resume（已由 `f8b1850` 补充）

**用例：** `run_interrupted_source_resume_real_api_case()` 及配套 deterministic/bridge 单元覆盖

| 验收点 | 结果 |
|---|---|
| Source run 中已完成 job 可作为成功前缀 | 确认 |
| Source run 中 running/interrupted job 不被错误复用 | 确认 |
| Resume 后仅复用最长成功前缀 | 确认 |
| Stale / interrupted 后续 job 重新执行 | 确认 |
| Resumed run 最终成功 | 确认 |
| Prefix cache metadata 指向 source run/job | 确认 |
| Cached artifact/transcript/event metadata 保持可追踪 | 确认 |

### 2.5 权限 / MCP / Skills / Tools 继承（已补 Native child 执行链路 + 真实 API + 真实 MCP diagnostic）

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
| 真实 native GPT 主套件通过 | 6 个主 case 全部 `passed`，`diagnostics={}`（真实 MCP diagnostic 关闭），`secretScan=[]` |

当前限制：真实 MCP diagnostic 是 diagnostic-only，不纳入主 `summary.passed` 门禁；结果依赖本机 MCP 配置、网络和外部 MCP 服务状态。Context7 未出现在 GenericAgent 当前 `mcp_runtime` discovery 列表中，当前真实 MCP 诊断覆盖的是已配置可用的 `fetch` MCP。

真实 native GPT 主套件已通过，说明该链路不只是 deterministic/stub 覆盖，主流程在真实模型调用下也已验证。

### 2.6 Bridge Draft/Approve/Final 成功（已提交）

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

### 2.7 Bridge 非成功终态事件覆盖（已由 `9e00a05` 补充）

**文件：** `tests/test_ink_bridge.py`

| 验收点 | 结果 |
|---|---|
| failed 终态事件流 | 已覆盖 |
| killed 终态事件流 | 已覆盖 |
| interrupted 终态事件流 | 已覆盖 |
| 非成功终态仍 emit `workflow_final` | 已覆盖 |
| failed final fallback | 已覆盖 |
| runtime exception / fallback final payload | 已覆盖 |
| failed/killed/interrupted source resume bridge 允许路径 | 已覆盖 |
| killed stop `workflow_final(status=killed)` | 已覆盖 |
| 运行结束后 activity/status 清理 | 已覆盖 |

### 2.8 单元测试覆盖的核心实现细节

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
| **缺口描述** | 真实 API 主路径仍使用大 timeout（420/360/240/180），不会把 timeout 作为主门禁触发；这是为了避免真实网络波动影响主套件稳定性。 |
| **当前覆盖** | Runtime timeout 单元、JS worker timeout 单元、`d13bde5` 近真实 bridge timeout failed/final/error/idle 覆盖，以及 `realApiTimeoutBridgeFinalDiagnostic` 真实 API diagnostic-only 覆盖。 |
| **剩余内容** | ① 长期观察真实 API timeout diagnostic 的稳定性；② 真实网络慢响应统计；③ 若后续证明稳定，再考虑更细粒度的资源回收指标。 |
| **风险评级** | 低-中 |
| **建议优先级** | **观察项 / P2 稳定性诊断** |

### 项目 2：真实 API 网络抖动 / 慢响应持续 E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | 原缺口是没有任何统计稳定性测试。当前已新增独立 opt-in stability diagnostic harness，可连续多轮执行低成本真实 API workflow 并聚合延迟/失败摘要。 |
| **当前覆盖** | 新增独立 opt-in diagnostic harness `tests/p8_real_api_stability_e2e.py`，通过 `GA_RUN_REAL_API_E2E=1` + `GA_RUN_REAL_API_STABILITY=1` 二级 opt-in 执行；默认 skip，且不纳入 `tests/p8_real_api_e2e.py` 主门禁。支持 `GA_REAL_API_STABILITY_ROUNDS` 配置轮数并限制 1..10；每轮执行 1 个 single agent + 2 个 parallel agent，真实 child agent 显式 `enable_tools=False`，记录 elapsed/status/jobs/tokenUsage/error，并输出 passedRounds/failedRounds、P50/P95/max/min/avg、observedErrorTypes 和 secretScan。真实 native GPT 3 轮已通过，`passedRounds=3`、`failedRounds=0`、`secretScan=[]`。 |
| **缺失内容** | ① 单次 10 轮或跨多次运行累计更多轮的稳定性统计；② P99 延迟属于后续扩展指标，需要更多轮次才有意义；③ 偶发 5xx/429/空响应需要长期运行收集；④ 自动生成独立脱敏稳定性报告文件 |
| **风险评级** | 中 |
| **建议优先级** | **P2** |

### 项目 3：Rate Limit / 429 边界 E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | 原缺口是没有任何 workflow 层 provider 429 / rate-limit 覆盖，也没有压力测试通过增大 `parallel()` fan-out 观察真实 rate limit。当前已补 deterministic runtime/bridge 429 覆盖和独立真实 API stress diagnostic harness。 |
| **当前覆盖** | Runtime 新增 provider 429 deterministic 覆盖：parallel 中一个 child 成功、另一个 child 返回 `HTTP 429 Too Many Requests`，断言 workflow failed、`agent_failed`/`workflow_failed`、成功/失败 artifact/transcript 保留、failed payload 包含 `statusCode=429` 与 `category=rate_limit`。Bridge 新增 provider 429 覆盖：断言 `workflow_final(status=failed)`、`error(code=workflow_run_failed)`、activity/status idle、artifact 保留。新增独立 `tests/p8_real_api_stress_e2e.py`，通过 `GA_RUN_REAL_API_E2E=1` + `GA_RUN_REAL_API_STRESS=1` 二级 opt-in 执行真实 fan-out stress diagnostic；默认 skip，真实 child agent 显式 `enable_tools=False`。已用真实 native GPT 跑 `fanout=8`、`rounds=1`，8 个 child 全部 succeeded，`rateLimitDetected=false`，`secretScan=[]`。该 harness 区分 `contractPassedRounds` / `contractFailedRounds`（诊断是否收敛）、`cleanSuccessRounds`（无 rate-limit 的成功轮）和 `rateLimitRounds`（观测到 429/rate-limit 的轮次）；`summary.passed=true` 表示 diagnostic contract 收敛，不等价于没有出现 rate limit。 |
| **缺失内容** | ① 更高 fan-out（16/32）和更多轮真实 stress 观察；② 长期运行收集自然 429/5xx/空响应；③ provider 特定限流策略、retry-after 行为和恢复策略；④ 若真实 429 出现，持续验证 failed job 与成功 artifact 保留、workflow final/failure policy 一致性 |
| **风险评级** | 中-高 |
| **建议优先级** | **已完成基础覆盖 / P2 扩展观察**（真实 stress 仍需 opt-in + 单独 env var 控制成本） |

### 项目 4：Parallel 部分失败 E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | 原缺口是没有真实/半真实 E2E 验证 parallel 中部分 child agent 真实失败。当前已补 runtime deterministic、bridge 半集成和真实 API diagnostic-only 三层覆盖。 |
| **当前覆盖** | Scheduler 层 `continue`/`fail_fast` 单元测试；`test_runtime_worker_error_after_parallel_cancels_pending_jobs`；新增 runtime `test_runtime_parallel_partial_failure_preserves_success_artifact_and_failed_result`；新增 bridge `test_workflow_approve_parallel_partial_failure_emits_failed_final_error_and_preserves_artifacts`；新增 `realApiParallelPartialFailureDiagnostic`。 |
| **剩余内容** | ① 真实 provider 自然 API error / 429 / timeout 的压力场景仍归入 rate limit/网络抖动专项；② fail_fast runtime 真实 DSL 变体后续可作为扩展；③ diagnostic 稳定性持续观察。 |
| **风险评级** | 低-中 |
| **建议优先级** | **已完成核心覆盖 / P2 扩展观察** |

### 项目 5：Failed / Killed / Interrupted Run Resume E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | Failed/killed/interrupted source resume 的核心前缀复用路径已覆盖。剩余缺口主要是 bridge 层真实 API 场景、更多 options/args/permission profile 组合，以及异常恢复后的 UI 端到端确认。 |
| **当前覆盖** | Failed source resume 与 killed source resume 已有真实 API E2E；`f8b1850` 已补充 interrupted resume 前缀复用覆盖；Store 层 `test_mark_running_jobs_stale_on_resume_projection` 覆盖 running job stale 投影；`9e00a05` 已补 failed/killed/interrupted source resume bridge 允许路径。 |
| **剩余内容** | ① Bridge 层对 failed/killed/interrupted source resume 的完整真实 API E2E；② Prompt/options/args/permission profile 变体下的 resume；③ interrupted 场景 artifact 与 parent transcript 隔离的更大规模验证。 |
| **风险评级** | 中 |
| **建议优先级** | **P2** |

### 项目 6：Kill / Stop / Interrupted 真实 E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | 原缺口是 workflow_stop / kill 期间 completed prefix、running child cancel、killed final/idle 与 resume 前缀复用没有串成完整链路。当前已补 bridge 半集成、真实 API diagnostic-only 以及 real provider mid-call stop diagnostic-only 覆盖；后续重点是持续观察 provider cancel 稳定性。 |
| **当前覆盖** | 单元测试 `test_runtime_observes_external_kill_state`、Scheduler stop 单元、Bridge `test_stop_stops_active_running_workflow`、`9e00a05` killed final；新增 `test_workflow_stop_after_completed_prefix_cancels_running_child_and_resume_uses_prefix` 串联 workflow_stop + completed prefix + running child cancel + killed final + resume cached prefix；新增 `realApiBridgeStopResumeDiagnostic` 以真实 native GPT prefix + 受控 running child 验证 bridge stop/resume 诊断链路；新增 `realApiMidCallStopDiagnostic` 使用真实 native provider job 触发 mid-call stop，并验证 cancel 请求、killed/final/idle、cached prefix 与 fresh rerun。 |
| **剩余内容** | ① 持续观察 provider 真实 streaming/mid-call cancel 的稳定性；② 当前不强断言 SDK 网络流一定即时中断，只把 cancel 请求、runtime/bridge 收敛和 resume 语义作为诊断指标；③ UI 层快捷键/面板触发 stop 的完整前端 E2E；④ 更多 stop reason / permission / args 变体。 |
| **风险评级** | 低-中 |
| **建议优先级** | **核心覆盖已完成 / P2 真实 streaming 诊断观察** |

### 项目 7：Bridge 非成功终态 / 超时后的 Final/Error 事件 E2E

| 维度 | 详情 |
|---|---|
| **缺口描述** | Bridge failed/killed/interrupted 非成功终态事件已补覆盖；timeout failed/final/error/idle 也已补近真实覆盖和真实 API diagnostic-only 覆盖。剩余重点转为真实 API bridge failed/killed/interrupted 路径、`_workflow_final_payload` fallback 在 artifact 缺失/损坏时的行为，以及 `_run_workflow_runtime` runtime exception 分支的真实/完整链路。 |
| **当前覆盖** | `9e00a05` 已补充 bridge 非成功终态覆盖；`d13bde5` 已补充 timeout 后 bridge event 流；新增 `realApiTimeoutBridgeFinalDiagnostic` 覆盖真实 API timeout 诊断路径；既有 bridge succeeded 主路径、resume 拒绝、stop fake runtime、failed final fallback、runtime exception final/error/idle、killed stop final 均已有覆盖。 |
| **剩余内容** | ① 真实 API bridge failed/killed/interrupted 路径；② result.json 缺失/损坏时 fallback payload 的更完整验证；③ runtime exception 后 error event 与 idle 恢复的端到端补强；④ timeout diagnostic 的持续稳定性观察。 |
| **风险评级** | 低-中 |
| **建议优先级** | **P2** |

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
| **当前覆盖** | Cache key 包含 permission 字段和 policy version；Permission 事件 journal 写入；Store 层默认 profile 为 `"inherit-current-permissions"`；deterministic E2E 覆盖内置工具、临时 skill、stub MCP、toolSummary artifact、child transcript/journal 和 parent transcript 隔离；`NativeGPTChildAgentRunner` 已接入 `GenericAgentHandler.dispatch`；真实 API `nativeToolCallingFileSkillMcp` 覆盖真实模型主动调用 `file_read`/`load_skill`/stub MCP；`realMcpDiagnostic` 覆盖非 mock 真实 MCP `mcp__fetch__fetch`。真实 native GPT 主套件已通过，说明该链路不只是 deterministic/stub 覆盖，主流程在真实模型调用下也已验证。 |
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

### Priority P0（当前无阻塞项）

截至 `9e00a05`、`f8b1850`、`1c8933f`，原 P0 中的 interrupted resume 前缀复用与 bridge 非成功终态核心覆盖已补齐，权限 / MCP / Skills / Tools 继承核心验收也已完成。P8 当前不再存在必须立即阻塞合入的 P0 E2E 缺口。

保留的高价值工作转入 P1/P2：

- parallel 部分失败真实/半真实 E2E。
- mid-call kill/stop 真实 E2E。
- bridge 非成功终态真实 API 端到端补强。
- timeout diagnostic 持续稳定性观察。

---

### Priority P1（次优先）

---

#### 4.1 Timeout + Bridge Final/Error E2E

**目标：** 验证 `timeout_seconds` 极短值时 workflow 正确终止，child/worker 被回收，bridge emit failed/final/error 事件，activity/status 恢复 idle。

**前置条件：**
- Runtime 层 timeout 逻辑已实现（`WorkflowRuntime.__init__` 接受 `timeout_seconds`）
- JS worker 超时 escalate 逻辑已单元测试
- Bridge 层接受 `timeout_seconds` 参数

**覆盖核查结论：**

本项当前已进入收尾状态：

- Runtime timeout 单元层已覆盖：`test_runtime_timeout_kills_never_resolving_async_script`、`test_runtime_timeout_uses_configured_deadline_for_sync_infinite_loop`、`test_runtime_timeout_cancels_running_child_agents`、`test_runtime_terminate_escalates_to_kill_and_waits`。
- Bridge 非成功终态基础覆盖已由 `9e00a05` 补齐：failed/killed/interrupted/fallback/exception 的 final/error/idle 路径已有单元/半集成覆盖。
- Bridge timeout 参数传递已有覆盖：approve/resume/JSONL 能把 `timeoutSeconds` 传到 runtime factory。
- 真实 API bridge succeeded 主路径已覆盖，但使用大 timeout，不触发 timeout。
- 第一阶段已由 `d13bde5` 完成：`test_workflow_approve_timeout_emits_failed_final_error_and_idle` 使用真实 `WorkflowRuntime`、永不 resolve 的 JS Promise 与 `workflow_approve(..., timeout_seconds=0.2)`，验证 runtime deadline 后 run failed、`final-result.json`、`workflow_final(status=failed)`、`workflow_failed`、`error(code=workflow_run_failed)` 与最终 idle 收敛。
- 第二阶段已补强到真实 API harness：`tests/p8_real_api_e2e.py` 中新增 diagnostic-only `realApiTimeoutBridgeFinalDiagnostic`，在 `GA_RUN_REAL_API_E2E=1` 下运行，但不纳入主 `summary["passed"]` 门禁，输出 case 级脱敏摘要：run status、finalSeen、workflowFailedSeen、errorSeen、idleSeen、activityCleared、threadAliveAfterWait 等；顶层 `summary["secretScan"]` 统一报告 artifact secret 扫描结果。

**后续仅剩观察项：** 真实 API timeout diagnostic 受本机网络、provider 响应速度和真实模型流式取消时机影响，应作为诊断报告持续观察；不要把它升级为默认门禁，除非后续证明足够稳定。

**关键验收点：**
- 不挂死，不遗留 running run，不遗留僵尸子进程
- 超时后 bridge 发出明确 terminal run 与 `workflow_final(status=failed)`
- 超时后 error 表达清晰：`workflow_failed` event 或 `error(code=workflow_run_failed)` 至少其一可见
- activity 清空，status 恢复 idle
- final-result artifact 可读，错误信息脱敏
- 不污染 parent transcript

**状态：** 第一阶段 deterministic/近真实 bridge 覆盖已完成；第二阶段真实 API diagnostic-only 覆盖已补充。

---

#### 4.2 Parallel 部分失败 E2E

**目标：** 验证 parallel 中部分 child agent 真实失败时其他 job 的状态处理。

**前置条件：**
- Scheduler 层 failure_policy 已实现并单元测试
- Worker error + parallel cancel 逻辑已单元测试

**已实现方式：**
- Runtime deterministic：`parallel()` 中一个 child 成功后另一个 child 返回 failed，验证成功/失败 job artifact、transcript、`agent_completed`/`agent_failed`、`final-result.json(status=failed)`。
- Bridge 半集成：通过真实 `WorkflowRuntime` + 受控 runner 验证 bridge 看到 `workflow_run(status=failed)`、`workflow_final(status=failed)`、`error(code=workflow_run_failed)`，并最终 activity/status idle。
- 真实 API diagnostic-only：`realApiParallelPartialFailureDiagnostic` 使用真实 native GPT 执行成功 child，另一个 child 由受控 runner 注入失败；输出成功/失败 job、artifact、event、final 状态摘要，不纳入主门禁。

**关键验收点：**
- 成功 job 不被失败 job 污染
- Failed job 有明确 error payload 和 result artifact
- Workflow 终态与当前 runtime continue/failure 行为一致：整体 failed，已完成 job artifact 保留
- Bridge 不挂死，final/error/idle 收敛

**状态：** 核心 deterministic/bridge/真实 API diagnostic 覆盖已完成；provider 自然 429/timeout 归入 P2 rate limit/网络抖动专项。

---

#### 4.3 Mid-Call Kill / Stop E2E

**目标：** 验证 workflow 在真实 LLM API 调用进行中被 kill/stop 后的状态和 artifact 处理。

**前置条件：**
- Runtime 层检测外部 kill 的逻辑已实现
- Scheduler stop 逻辑已实现
- Bridge stop 逻辑已实现

**已实现方式：**
- Bridge 半集成：`test_workflow_stop_after_completed_prefix_cancels_running_child_and_resume_uses_prefix` 使用真实 `WorkflowRuntime` 和受控 runner，验证 `workflow_stop` 发生在 `agent_1` 成功、`agent_2` running 后；source run killed，`agent_2` cancelled，`workflow_final(status=killed)`，随后 `workflow_resume` 中 `agent_1` cached、`agent_2` fresh succeeded。
- 真实 API diagnostic-only：`realApiBridgeStopResumeDiagnostic` 使用真实 native GPT 产出 prefix job，第二个 job 受控 gate/hang，通过 bridge `workflow_stop` 停止并 resume，验证 cached prefix、fresh rerun、final/idle 和 artifact。
- Real provider mid-call diagnostic-only：`realApiMidCallStopDiagnostic` 让第二个 job 真实进入 native provider 调用后触发 `workflow_stop`，验证 cancel 请求已发出、source killed/final/idle 收敛、resume 复用 prefix 并 fresh rerun 第二个 job；该诊断不强断言 SDK 网络流一定即时中断。

**关键验收点：**
- 不挂死，不遗留 running workflow：已覆盖 source/resumed thread 退出与 idle 收敛
- 已完成结果可恢复：已覆盖 source `agent_1` artifact/transcript 与 resumed cached artifact
- Resume 不重复执行已完成 job：已覆盖 `cachedPrefix=true` 与后续 fresh job

**状态：** 核心 bridge/runtime/真实 API diagnostic 覆盖已完成；真实 provider mid-call/streaming cancel 已新增 diagnostic-only 观察项，后续继续观察稳定性。

---

### Priority P2（后续关注）

| 项目 | 说明 | 前置条件 |
|---|---|---|
| JS Worker 异常脚本 Bridge E2E | pipeline/parallel throw、非法 options、不可序列化返回值等异常脚本桥接收敛 | 现有单元测试模式 |
| Failed/killed/interrupted resume bridge 真实 API 与变体组合 | runtime/基础 bridge 已覆盖，剩余完整链路与参数矩阵 | P1 稳定后 |
| Rate Limit / 429 边界 E2E | 已有 deterministic runtime/bridge 429 覆盖 + 基础真实 stress harness；剩余为 fanout 16/32、更多轮和 provider 自然 429 长期观察 | stress harness 已可用，扩展观察需单独 opt-in |
| 网络抖动 / 稳定性 E2E | 已有基础 opt-in diagnostic harness；剩余为更多轮次/长期统计/压力专项 | stability harness 已可用，压力专项另行 opt-in |
| 返回格式异常 E2E | 需 semi-real runner/mock 基础设施 | P9/P10 阶段 |

### Priority P3（低优先级）

| 项目 | 说明 |
|---|---|
| Cache Key 真实变体 E2E | 单元测试已非常充分，可在其他测试中自然覆盖 |
| 大 Artifact / Transcript 隔离 | 性能/规模测试，P9/P10 阶段评估 |
| Context7 专项 diagnostic | 仅在本机 GenericAgent MCP discovery 出现 Context7 后补充 |
| Explicit approval 审批闭环 | 工作流内审批专项扩展 |

---

## 5. 历史去重后的下一步实施计划（2026-06-09 动态工作流审计后）

本节基于一次面向提交历史、现有测试和文档的 dynamic workflow 审计，用于避免把已经完成的 P8 覆盖重复实施。结论是：P8 当前不应再重复补 runtime-only worker throw、deterministic cache key、基础 artifact/transcript 隔离、429、timeout、stop/resume 或 parallel partial failure；下一轮应收窄到 bridge + 真实 `WorkflowRuntime` 异常脚本、provider anomaly、bridge cache 可观测性、大 transcript 边界和少量 opt-in diagnostic。

### 5.1 明确不要重复的历史覆盖

| 已覆盖方向 | 证据 | 后续避免重复 |
|---|---|---|
| Runtime-only JS worker 基础异常 | `tests/test_workflow_runtime.py::test_runtime_marks_run_failed_when_worker_errors`、`test_runtime_rejects_forbidden_script_tokens_before_worker_starts`；相关提交 `389deed`、`426ab2c` | 不再新增单纯 runtime-only 顶层 throw 或 forbidden token 拒绝测试 |
| Bridge stub runtime exception / timeout final/error/idle | `tests/test_ink_bridge.py::test_workflow_runtime_exception_emits_error_final_and_idle`、`test_workflow_approve_timeout_emits_failed_final_error_and_idle`；提交 `9e00a05`、`d13bde5` | 不再用 fake `ExplodingRuntime` 重测 bridge exception；不把 timeout 当异常脚本覆盖 |
| Provider 429 / rate limit 基础覆盖 | `tests/test_workflow_runtime.py::test_runtime_provider_429_rate_limit_failure_preserves_artifacts_and_events`、`tests/test_ink_bridge.py::test_workflow_approve_provider_429_rate_limit_emits_failed_final_error_and_preserves_artifacts`、`tests/p8_real_api_stress_e2e.py`；提交 `cc057a3` | 不再补 deterministic 429 或基础 bridge 429 final/error/idle；真实 stress 仅作为 opt-in 观察 |
| Parallel partial failure 基础覆盖 | `tests/test_ink_bridge.py::test_workflow_approve_parallel_partial_failure_emits_failed_final_error_and_preserves_artifacts`、`realApiParallelPartialFailureDiagnostic`；提交 `7aa318b` | 不再重复成功/失败 job artifact 与 failed final/error/idle 基础断言 |
| Stop/resume prefix 与 mid-call stop | `test_workflow_stop_after_completed_prefix_cancels_running_child_and_resume_uses_prefix`、`realApiBridgeStopResumeDiagnostic`、`realApiMidCallStopDiagnostic`；提交 `394070b`、`cd662fb` | 不再重复 stop 后 cached prefix / fresh rerun 基础路径 |
| Failed/killed/interrupted resume 基础矩阵 | `tests/test_workflow_runtime.py` 的 args/cache/prefix/interrupted/stale/running middle 测试；`tests/test_ink_bridge.py` 的 `workflow_resume` 允许/拒绝路径；`tests/p8_real_api_e2e.py` failed/killed 真实 API E2E；提交 `aa0a2b0`、`93c635e`、`f8b1850` | 不再重复 `workflow_resume` 参数转发、failed/killed 是否允许 resume、unfinished/cancelled 拒绝、interrupted deterministic stale/running middle 矩阵 |
| Artifact/transcript 基础隔离 | `tests/test_workflow_store.py::test_write_agent_result_persists_result_artifact_and_updates_job_ref`、`tests/test_workflow_runtime.py::test_runtime_cached_agent_transcript_ref_points_to_resumed_artifact`、`tests/test_workflow_permission_inheritance_e2e.py::test_inherit_current_child_records_tool_skill_mcp_events_without_polluting_parent_transcript` | 不再重复 `result.json` 不内联 `transcriptEvents`、单 job `transcriptRef`、小规模 parent transcript marker 隔离 |
| Cache key deterministic 正确性 | `tests/test_workflow_scheduler.py` 的 permission/profile/argsHash/JSON-like string 测试、`tests/test_workflow_runtime.py` 的 args/type/cross-session/prefix 测试、`test_permission_profile_and_version_partition_runtime_resume_cache` | 不再写 cacheKey 字段存在性、argsHash deterministic、JSON 值 vs 字符串、permission profile/version deterministic 分区测试 |

### 5.2 新实施优先级

#### P0：Bridge + 真实 `WorkflowRuntime` 的 JS 异常脚本收敛矩阵

**为什么不是重复：** 历史覆盖的是 runtime-only 顶层 throw、bridge stub runtime exception、bridge timeout、parallel child failure；缺口是测试真实 `WorkflowRuntime` 执行异常 JS script 后，经 `GenericAgentBridge.workflow_approve()` 收敛为 `workflow_final(status=failed)`、`error(code=workflow_run_failed)`、activity/status idle 的完整链路。

**目标文件：** `tests/test_ink_bridge.py`、`frontends/ink_bridge.py`

**第一批高确定性场景（已实现）：**

1. top-level script throw：`throw new Error('GA_P8_TOP_LEVEL_THROW')`。
2. pipeline stage throw：`pipeline()` 某 stage 抛 `GA_P8_PIPELINE_STAGE_THROW`。
3. forbidden token preflight：脚本包含 `process.env` 并被安全扫描拒绝。

**已补测试：**

- `test_workflow_approve_real_runtime_script_throw_emits_failed_final_error_and_idle`
- `test_workflow_approve_real_runtime_pipeline_throw_emits_failed_final_error_and_idle`
- `test_workflow_approve_forbidden_script_emits_failed_final_error_and_idle`

**已修复问题：** forbidden token preflight 原本会在 runtime 启动前抛错，bridge 异常兜底只能 emit error/fallback，但 run 可能保持 `running`。`frontends/ink_bridge.py::_run_workflow_runtime` 现会在 runtime 抛错且 run 尚非终态时，将 run 落盘为 `failed`、写入 `final-result.json`、追加 `workflow_failed` event，再 emit `workflow_final(status=failed)` 与 `error(code=workflow_run_failed)`，最终恢复 idle。

**共用验收：** bridge thread 退出；run terminal status 为 `failed`；events 包含 `workflow_final(status=failed)` 与 `error(code=workflow_run_failed)`；journal/workflow events 包含 `workflow_failed`；`final-result.json` 存在且 status 为 `failed`；activity 清空；status 回到 idle；错误消息含场景 marker 且不泄露 secret。

**第二批暂缓场景：** `parallel()` thunk throw、invalid agent options、non-serializable return（例如 BigInt 或循环对象）。这些需要先确认 runtime/worker contract，避免测试写死错误语义。

**验证命令：**

```bash
python -m unittest tests.test_ink_bridge
python -m unittest tests.test_workflow_runtime
```

#### P1：半真实 provider anomaly E2E

**为什么不是重复：** 429、timeout、stop/resume、parallel partial failure 都不是 provider 返回格式异常；当前仍缺 empty content、assistant message 无 text block、usage missing、malformed stream / JSON、SDK exception 的 workflow 降级契约。

**目标文件：** 优先新增 `tests/test_p8_provider_anomaly_e2e.py`，必要时配合 `tests/test_workflow_runtime.py`、`tests/test_ink_bridge.py`、`workflow_child_agent.py`。

**第一批不打真实 API：** child runner 返回空 summary/content；assistant message 无 text block；usage 缺失或为 `None`；runner 抛 SDK-like exception；transcript artifact 仍为可读 JSONL；`result.json` 不内联 `transcriptEvents`；error payload 脱敏；workflow final status 与当前 contract 一致。

**注意：** 需要先读取当前 `NativeGPTChildAgentRunner` / `AgentResult` contract，不能预设空 content 一定 failed 或 degraded succeeded。

**验证命令：**

```bash
python -m unittest tests.test_p8_provider_anomaly_e2e
python -m unittest tests.test_workflow_runtime
python -m unittest tests.test_ink_bridge
```

#### P2：Bridge cache hit/miss 半集成矩阵

**为什么不是重复：** deterministic cache key 已充分覆盖；缺的是 bridge 层实际 resume 时，UI/bridge 是否可观察到 `agent_cached` vs fresh child 行为。

**目标文件：** `tests/test_ink_bridge.py`

**最小两例：**

1. same args 命中 cache：source run succeeded 后 `workflow_resume(source_run_id, args=same_args)`，断言 resumed run 有 `agent_cached`、无 fresh child started、`cachedFromRunId`/`cachedFromJobId` 正确、`transcriptRef` 指向 resumed artifact。
2. different args 不命中 cache：`workflow_resume(source_run_id, args=different_args)`，断言无 `agent_cached`、fresh child executed、result marker 来自 fresh run。

**后续扩展：** prompt 改动 prefix hit/miss、options 改动 miss、permission profile/version 改动 miss。

**验证命令：**

```bash
python -m unittest tests.test_ink_bridge
```

#### P3：大 artifact / transcript 隔离 deterministic E2E

**为什么不是重复：** 基础 artifact/transcript 分离已覆盖，但没有真正制造 1000+ 行、多 job、大 transcript、resume copy 与 bridge 不返回大 body 的规模边界。

**目标文件：** `tests/test_workflow_runtime.py`、`tests/test_workflow_integration.py`，可选 `tests/test_ink_bridge.py`。

**第一批范围：** source run 生成 2-3 个 completed child jobs；每个 child transcript 写入 1000+ JSONL events；每个 transcript 中放唯一 marker；断言 `result.json` 不包含 `transcriptEvents`、`transcriptRef` 文件存在、行数符合预期、parent session transcript raw 不包含大 marker、resume 后 copied transcript 行数/hash 相同、resumed `transcriptRef` 指向 resumed run artifact 而不是 source artifact 绝对路径。

**可选 bridge 小切片：** bridge approve/resume 大 transcript workflow；bridge emitted events 不包含 1000+ child transcript body；final/error/idle 正常。

**验证命令：**

```bash
python -m unittest tests.test_workflow_runtime
python -m unittest tests.test_workflow_integration
python -m unittest tests.test_ink_bridge
```

#### P4：真实 API interrupted source resume 独立 diagnostic

**为什么不是重复：** runtime deterministic interrupted prefix/stale/running middle 已覆盖，bridge 允许 interrupted source resume 已覆盖，failed/killed 真实 API E2E 已覆盖；缺的是像 failed/killed 一样可独立审计的真实 API interrupted source resume diagnostic。

**目标文件：** `tests/p8_real_api_e2e.py`、`tests/test_p8_real_api_e2e_diagnostic.py`

**范围：** opt-in diagnostic-only。source run 完成 `agent_1`/`agent_2`；source run 被标记 interrupted，`agent_3` stale 或 running；resume 后 `agent_1`/`agent_2` cached、`agent_3` fresh、resumed final succeeded、`agent_cached` 事件数正确、metadata `cachedFromRunId`/`cachedFromJobId` 正确、bridge final/status idle 序列完整、diagnostic summary 脱敏。

**验证命令：**

```bash
python -m unittest tests.test_p8_real_api_e2e_diagnostic
GA_RUN_REAL_API_E2E=1 python tests/p8_real_api_e2e.py
```

#### P5：真实 API cache key args variant diagnostic

**为什么不是重复：** 已有 deterministic argsHash miss 与真实 API permission metadata smoke；缺的是真实 API 下 cache hit/miss 是否实际避免或触发真实 child 调用。

**目标文件：** `tests/p8_real_api_e2e.py`、`tests/test_p8_real_api_e2e_diagnostic.py`

**范围：** opt-in diagnostic-only、最小成本。source run 使用 script + args A 并真实 child 成功；resume with args A 断言 `agent_cached` 且没有 fresh child started；resume with args B 断言无 `agent_cached` 且 fresh child started；用 `agent_cached` event、started job IDs、artifact copy/fresh marker 作为成本代理，不依赖 provider 账单。

**验证命令：**

```bash
python -m unittest tests.test_p8_real_api_e2e_diagnostic
GA_RUN_REAL_API_E2E=1 python tests/p8_real_api_e2e.py
```

#### P6：真实 API stability/stress 扩展观察

**为什么不是重复：** 基础 stability/stress harness、deterministic/bridge 429 已存在；剩余只是长期/高 fanout/provider 自然异常观察，不应变成默认门禁。

**目标文件：** `tests/p8_real_api_stability_e2e.py`、`tests/p8_real_api_stress_e2e.py`、对应 unit tests 与本文件。

**范围：** 仅增强 summary 聚合：observed 5xx count、observed empty response count、observed SDK exception count、retry-after / rate-limit hint、更明确的 high fanout / rounds 配置说明。真实运行继续要求二级 opt-in。

**验证命令：**

```bash
python -m unittest tests.test_p8_real_api_stability_e2e
python -m unittest tests.test_p8_real_api_stress_e2e
GA_RUN_REAL_API_E2E=1 GA_RUN_REAL_API_STABILITY=1 python tests/p8_real_api_stability_e2e.py
GA_RUN_REAL_API_E2E=1 GA_RUN_REAL_API_STRESS=1 GA_REAL_API_STRESS_FANOUT=16 GA_REAL_API_STRESS_ROUNDS=2 python tests/p8_real_api_stress_e2e.py
```

### 5.3 推荐下一步

P0 第一批 **Bridge + 真实 `WorkflowRuntime` 的 JS 异常脚本收敛矩阵** 已完成 top-level script throw、pipeline stage throw、forbidden token preflight 三个高确定性场景，并修复 preflight 异常后 run 可能停留 `running` 的 bridge 兜底问题。

后续建议二选一：

1. 若继续完善同一方向，进入 P0 第二批：`parallel()` thunk throw、invalid agent options、non-serializable return。执行前先确认 runtime/worker 对这些异常的 contract。
2. 若转向下一类缺口，进入 P1：半真实 provider anomaly E2E，先用 deterministic / semi-real runner 固定 empty content、无 text block、usage missing、SDK-like exception 的 workflow 降级与 artifact/transcript 脱敏契约。

---

## 6. 执行约束与安全注意事项

### 6.1 真实 API 测试的安全门

```bash
GA_RUN_REAL_API_E2E=1 python tests/p8_real_api_e2e.py
```

- 所有真实 API E2E 必须通过 `GA_RUN_REAL_API_E2E` 环境变量 opt-in
- 默认 unittest 和 pytest 不得烧真实 API
- 压力测试（rate limit / 大并发）必须使用单独的环境变量 `GA_RUN_REAL_API_STRESS=1`
- 绝不读取、打印、提交真实 API key
- 文档更新不得读取或打印 `mykey.py`、`mykey.json`、`mcp.json`
- case 级诊断输出只能包含脱敏摘要、case 名称、状态、耗时和错误类型，不得输出 provider key、完整原始请求或未脱敏响应

### 6.2 真实 native GPT 主套件状态

- 最新状态：真实 native GPT 主套件已通过。
- 执行环境：`GA_RUN_REAL_API_E2E=1`、`GA_REAL_API_CONFIG=native_oai_config`、`GA_REAL_API_EXPECTED_NAME=gpt-native`、`GA_REAL_API_EXPECTED_MODEL=gpt-5.5`。
- 结果：6 个主 case 全部 `passed`，`diagnostics={}`（真实 MCP diagnostic 关闭），`secretScan=[]`。
- 该状态只表示 opt-in 环境下主套件通过，不代表压力测试、rate limit、网络抖动、timeout/fallback 等专项场景已覆盖。
- 文档更新不得读取或打印 `mykey.py`、`mykey.json`、`mcp.json`。

### 6.3 脱敏处理

- 所有 E2E 输出必须经 `sanitize()` 函数脱敏（`SECRET_PATTERNS` 覆盖 bearer token、api_key、sk-*、JWT 等）
- 脱敏格式：匹配到 secret 的部分替换为 `[REDACTED]`

### 6.4 Secret 扫描

- 所有工作完成时必须调用 `scan_for_secret_material()` 扫描 artifact 目录
- 发现 secret 时测试必须 FAIL（`secretScan: []` 为必要条件）

### 6.5 不提交 artifact

- 真实 API 调用产生的 artifact 不得提交到 git
- 测试使用 `tempfile.mkdtemp()` 创建临时目录后自动清理
- `temp/` 目录已在 `.gitignore` 中

### 6.6 默认权限约束

- Child agent 默认 permission profile 必须为 `"inherit-current-permissions"`
- Permission policy version 默认必须为 `"inherit-current-v1"`
- 默认权限必须是非 read-only，继承 GenericAgent 当前 agent 的工具/skill/MCP 权限

### 6.7 架构约束提醒

| 项目 | 说明 |
|---|---|
| `scriptHash` | 由 `_cache_key()` 计算但**不在匹配逻辑中比较** — resume 使用同一 script 时由最长前缀匹配覆盖，这是设计选择 |
| `failure_policy` | 运行时只使用 `"continue"`，Scheduler 支持的 `fail_fast` 未被运行时使用 |
| transcript 复制 | 通过 `shutil.copyfile` 物理复制（非软链接），大量 artifact 场景需关注磁盘使用 |

### 6.8 后续阶段参考（P9 / P10）

- **P9（Saved workflow registry）**：需要将 workflow script 的持久化和版本化纳入范围，可能影响 cache key 设计
- **P10（Planner/trigger integration）**：需要 workflow 与 GenericAgent 的 planner/trigger 机制集成，child agent 的工具继承变得更加关键
- 当前 E2E 测试框架（`p8_real_api_e2e.py`）应设计为易于扩展，后续 P9/P10 E2E 建议遵循同样的 opt-in + 脱敏 + secret 扫描模式

---

## 附录：关键文件路径

| 文件 | 说明 |
|---|---|
| `tests/p8_real_api_e2e.py` | P8 真实 API E2E 主文件，覆盖正常主路径、failed/killed/interrupted resume、权限/工具/skill/MCP 继承等 opt-in 场景 |
| `tests/test_workflow_runtime.py` | Runtime 层单元测试（14 个用例，按当前实际测试数维护） |
| `tests/test_workflow_scheduler.py` | Scheduler 层单元测试（14 个用例，按当前实际测试数维护） |
| `tests/test_workflow_store.py` | Store 层单元测试（9 个用例，按当前实际测试数维护） |
| `tests/test_ink_bridge.py` | Bridge 层单元/半集成测试，覆盖 workflow succeeded、resume 拒绝、stop、非成功终态事件等 |
| `frontends/ink_bridge.py` | Ink bridge 实现（`_run_workflow_runtime` 第 518-541 行，`_workflow_final_payload` 第 568-576 行） |
| `docs/P8-e2e-todo.md` | P8 E2E TODO 文档（本分析起点） |
| `docs/dynamic-workflows-implementation-roadmap.md` | 完整实施路线图（P1-P10） |
