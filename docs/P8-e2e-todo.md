# P8 Dynamic Workflows E2E TODO

本文记录 P8 `Workflow Resume / cache replay` 已完成的真实 E2E 覆盖，以及后续还需要补充的真实 API / 故障注入 / 边界 E2E。

## 已完成并提交的 E2E

提交：`aa0a2b0 feat(workflow): 实现 P8 resume 缓存复用`

补充提交：

- `9e00a05 test(workflow): 补充 bridge 非成功终态覆盖`
- `f8b1850 test(workflow): 补充 interrupted resume 前缀复用覆盖`
- `1c8933f test(workflow): 增强真实 API 失败诊断`

已完成：

- 正常主路径真实 API E2E。
- 使用 `native_oai_config` / `gpt-native` / `gpt-5.5`。
- `WorkflowRuntime` + JS worker + `NativeGPTChildAgentRunner`。
- `agent()` 正常执行。
- `parallel()` 正常执行。
- `pipeline()` 正常执行。
- source run 真实启动 7 个 child agent job，全部 `succeeded`。
- resumed run 复用 7 个 `cached` job，没有再次启动真实 API child job。
- `resumeResultEqualsSource: true`。
- result artifact / transcript artifact 存在。
- result JSON 不内联完整 `transcriptEvents`。
- Ink bridge draft / approve / final 成功。
- bridge workflow 状态流：`awaiting_approval -> running -> succeeded`。
- 连续 3 轮真实 API 主路径 E2E 全部通过。
- 真实 native GPT P8 主套件已在 `GA_RUN_REAL_API_E2E=1`、`GA_REAL_API_CONFIG=native_oai_config`、`GA_REAL_API_EXPECTED_NAME=gpt-native`、`GA_REAL_API_EXPECTED_MODEL=gpt-5.5` 下复跑通过：6 个主 case 全部 `passed`，`diagnostics={}`（真实 MCP diagnostic 关闭），`secretScan=[]`。
- `secretScan: []`，未发现密钥泄漏。
- bridge 非成功终态已覆盖：`failed` / `killed` / `interrupted` 均可收敛为明确终态事件，不再停留 running；包含 failed final fallback、runtime exception final/error/idle、failed/killed/interrupted source resume bridge 允许路径，以及 killed stop `workflow_final(status=killed)`。
- interrupted source run resume 已覆盖：已成功 job 可按最长成功前缀复用，后续 job fresh rerun；同时验证 stale/running middle job 阻断后续复用，以及 cached artifact/transcript/event metadata。
- 真实 API harness 已支持 case 级 `run_case_safely` 诊断，便于定位单个真实 E2E case 的失败原因；同时补充 `code_run` 缺失 `code_cwd` 的明确 error，以及 `py` alias 清理 `.ai.py`。

已提交的 opt-in harness：

```bash
python tests/p8_real_api_e2e.py
GA_RUN_REAL_API_E2E=1 python tests/p8_real_api_e2e.py
```

默认不设置 `GA_RUN_REAL_API_E2E=1` 时必须跳过，避免默认测试烧真实 API。

`1c8933f` 后真实 API harness 支持 case 级诊断输出，用于定位单个真实 API E2E case 的耗时、失败原因和脱敏摘要。

## 还没做的 E2E

### 1. 真实 API 超时边界 E2E

目标：验证真实网络/API 慢响应或超时时，workflow 不会假死，最终状态、事件和资源回收正确。

建议覆盖：

- `WorkflowRuntime(timeout_seconds=极短值)`。
- bridge 层 `workflow_approve(... timeout_seconds=极短值)`。
- JS worker 子进程是否被 terminate/kill 并回收。
- run 最终状态是否符合设计：`failed` / `killed` / `interrupted`。
- journal 是否写入足够的失败/中断事件。
- bridge 是否 emit `workflow_final` 或明确 error event。
- activity/status 是否恢复 idle。

验收要点：

- 不挂死。
- 不遗留 running run。
- 不遗留僵尸子进程。
- 不污染 parent transcript。
- 已产生 artifact 的 job 保持可读。

### 2. 真实 API 网络抖动 / 慢响应持续 E2E

目标：更系统地观察真实网络/API 延迟波动。

当前已做：连续 3 轮真实主路径 E2E，全部通过，但耗时波动明显。

建议补充：

- 连续 10 轮或更多轮。
- 统计 P50 / P95 / P99 延迟。
- 记录每轮：runtime 耗时、bridge 耗时、source job 数、cached job 数、错误类型。
- 检查是否出现偶发 5xx、429、空响应、SDK 异常。
- 自动生成脱敏稳定性报告。

验收要点：

- 全部轮次可收敛为 pass/fail，不挂死。
- 失败时输出脱敏错误摘要。
- timeout 设置足够宽，避免把真实慢响应误判为产品问题。

### 3. rate limit / 429 边界 E2E

目标：验证并发或连续真实调用触发限流时，workflow 失败处理和 artifact 保留是否正确。

建议覆盖：

- 增大 `parallel()` fan-out，例如 8、16、32 个真实 child agent。
- 连续执行多轮，观察 provider 是否返回 429 / rate limit。
- 检查 failed job 是否记录 `agent_failed`。
- 检查成功 job artifact 是否保留。
- 检查 workflow final status 是否符合 failure policy。
- 检查 bridge 是否能正常收敛，不挂死。

注意：该测试会明显消耗真实 API，并可能触发供应商限流。必须 opt-in，并建议单独环境变量控制，例如：

```bash
GA_RUN_REAL_API_E2E=1 GA_RUN_REAL_API_STRESS=1 python tests/p8_real_api_fault_e2e.py
```

### 4. parallel 部分失败 E2E

目标：验证并发任务中部分 child agent 失败时，其他任务和整体 workflow 的状态处理是否正确。

单元测试已有 fake runner 覆盖部分 failure policy，但还没有真实/半真实 E2E。

建议覆盖：

- parallel 中一个 child timeout，另一个 child 成功。
- parallel 中一个 child API error，另一个 child 成功。
- parallel 中部分 jobs failed 后，workflow result 如何返回。
- 成功 job 和失败 job 的 artifact 是否都正确写入。
- `agent_failed` 事件是否包含脱敏 error，不内联完整 transcript。

验收要点：

- 成功 job 不被失败 job 污染。
- failed job 有明确 error payload。
- workflow 最终状态和 failure policy 一致。
- bridge 不挂死。

### 5. failed / killed / interrupted run resume E2E

目标：验证 P8 最核心的“最长成功前缀复用”在失败、停止或中断 run 上也成立。

当前进展：已完成 failed source run 与 killed source run 的真实 API E2E，并已由 `93c635e` 提交到 `tests/p8_real_api_e2e.py`。

已验证 failed source run resume：

- source run 先真实执行 `agent_1` 成功，再由 JS workflow script 主动抛出 `GA_P8_FORCED_SOURCE_FAILURE`。
- source run 最终状态为 `failed`，但 `agent_1` result/transcript artifact 保留。
- resumed run 使用改造后的 script 继续执行。
- resumed run 中 `agent_1` 为 `cached`，来源为 failed source run 的 `agent_1`。
- resumed run 中 `agent_2` 为 fresh real API job，真实启动 child runner。
- resumed run 最终 `succeeded`，返回 marker `GA_P8_FAILED_RESUME_DONE`。
- resumed event 包含 `agent_cached: 1`、`agent_started: 1`、`agent_completed: 1`。
- `secretScan: []`。

已验证 killed source run resume：

- source run 先真实执行 `agent_1` 成功。
- source run 注册并启动 `agent_2` 后被外部置为 `killed`，错误标记为 `GA_P8_FORCED_SOURCE_KILL`。
- runtime 观测到外部 kill，最终抛出 `workflow killed: GA_P8_FORCED_SOURCE_KILL`。
- source run 最终状态为 `killed`。
- `agent_1` 为 `succeeded`，result/transcript artifact 保留。
- `agent_2` 为 `cancelled`，未写入 result/transcript artifact。
- source event 包含 `agent_cancelled: 1` 与 `workflow_killed: 1`。
- resumed run 中 `agent_1` 为 `cached`，来源为 killed source run 的 `agent_1`。
- resumed run 中 `agent_2` 为 fresh real API job，真实启动 child runner。
- resumed run 最终 `succeeded`，返回 marker `GA_P8_KILLED_RESUME_DONE`。
- resumed event 包含 `agent_cached: 1`、`agent_started: 1`、`agent_completed: 1`。
- `secretScan: []`。

已验证 interrupted source run resume：

- source run 中已完成 job 的 result/transcript artifact 保留。
- source run 进入 `interrupted` 非成功终态。
- resumed run 复用 interrupted source run 的最长成功前缀。
- interrupted 后未完成的 job 不复用，会 fresh real API rerun。
- stale/running middle job 会阻断后续复用，避免错误跳过中间不确定状态。
- cached artifact/transcript/event metadata 指向 source run/job 且保持可追踪。
- resumed run 最终可继续收敛到成功终态。

当前进展：failed / killed / interrupted source run 的 resume 前缀复用均已有覆盖；其中 interrupted source run resume 已由 `f8b1850` 覆盖，验证已成功 job 可复用、未完成/中断后的 job 会 fresh rerun。

仍未覆盖：prompt/options/args/permission profile 变体的真实 E2E；bridge 层 source resume 如仍缺少完整链路覆盖，应单独标注具体缺口。

建议继续覆盖：

- bridge 层 failed source run 经 `workflow_resume` 恢复的完整真实 API 链路。
- bridge 层 killed source run 经 `workflow_resume` 恢复的完整真实 API 链路。
- 如需提升覆盖层级，补充 interrupted source run resume 的完整 bridge 链路验证。
- prompt 改动后只复用改动前的前缀。
- options 改动后停止复用。
- args 改动后不复用。
- permission profile/version 改动后不复用。

验收要点：

- cached job 数量等于最长可复用前缀长度。
- fresh rerun job 会真实启动 child runner。
- `agent_cached` 与 `agent_started` 事件顺序清晰。
- failed/killed/interrupted source run 的已完成 artifact 可恢复。
- resumed final result 正确。

### 6. kill / stop / interrupted 真实 E2E

目标：验证 workflow 正在真实 API 调用时被 stop/kill 后状态和 artifact 处理正确。

建议覆盖：

- workflow 正在真实 API 调用时执行 `workflow_stop`。
- run 状态是否为 `killed` / `interrupted`。
- 已完成 job artifact 是否保留。
- running job 是否标记 stale/interrupted。
- stop 后是否允许 resume。
- resume 后是否只复用已完成 job。
- bridge/UI 是否恢复 idle。

验收要点：

- 不挂死。
- 不遗留 running workflow。
- 已完成结果可恢复。
- resume 不重复执行已完成 job。

### 7. bridge 非成功终态 final/error 事件 E2E

目标：验证 bridge 在非成功 workflow 结束时也能向 UI 发出明确终态；timeout 专项仍归入第 1 节真实 API 超时边界。

当前进展：已由 `9e00a05` 补充 bridge 非成功终态覆盖。

已覆盖状态流：

```text
awaiting_approval -> running -> succeeded
awaiting_approval -> running -> failed
awaiting_approval -> running -> killed
awaiting_approval -> running -> interrupted
```

已覆盖细节：

- failed final fallback。
- runtime exception 后 final/error/idle 恢复。
- failed/killed/interrupted source resume bridge 允许路径。
- killed stop `workflow_final(status=killed)`。

验收要点状态：

- 优先 emit `workflow_final`：已覆盖。
- 如果没有 final，也必须 emit 明确 error/status：已覆盖 fallback / exception 分支。
- 非成功终态不再停留 running：已覆盖。
- bridge 能向 UI 暴露明确 final/error/status：已覆盖。
- workflow panel 能显示失败/中断事件：基础事件链已覆盖；真实 UI 展示仍可在完整 bridge E2E 中补强。

### 8. 真实 API 返回格式异常 E2E

目标：验证 upstream 返回异常结构或 streaming 中断时，runner 和 workflow 能正确降级。

这类不易靠真实 API 自然触发，通常需要 mock、代理或半真实 runner 注入。

建议覆盖：

- upstream 返回空 content。
- upstream 返回非预期 message shape。
- streaming 中途断开。
- usage 缺失。
- assistant message 没有 text block。
- malformed JSON / provider SDK exception。

验收要点：

- runner 返回 `failed` result，而不是 poll 抛异常导致 workflow 崩溃。
- error payload 脱敏。
- transcript artifact 可读。
- workflow 状态和 failure policy 一致。

### 9. 权限 / MCP / skills / tools 继承 E2E

目标：验证 dynamic workflow child agent 默认权限继承当前 GenericAgent 普通 agent，而不是 read-only。

用户明确要求：默认 workflow/child agent 权限不能 read-only，必须继承 GenericAgent 当前普通 agent 的内置工具、skills、MCP 权限。

当前新增进展（已提交）：

- `fa2954d test(workflow): 补充 P8 权限继承 E2E` 增加默认可跑的半真实 deterministic E2E。
- `0eee3ef test(workflow): 补完 Native child 工具继承验证` 让 `NativeGPTChildAgentRunner` 具备可测试的工具执行路径：`agent_runner_loop -> GenericAgentHandler.dispatch -> ToolPermissionPolicy`。
- `4f4e04f test(workflow): 补充真实 API 工具继承 E2E` 在 opt-in 真实 API harness 中新增 `nativeToolCallingFileSkillMcp`，用真实 `native_oai_config` / `gpt-native` / `gpt-5.5` child agent 主动调用 `file_read`、`load_skill` 与受控 stub MCP。
- `42b0b6c test(workflow): 增加真实 MCP 诊断 E2E` 在 `GA_RUN_REAL_MCP_E2E=1` 下新增 diagnostic-only 的真实 MCP 非 mock 调用，实际发现并调用 `mcp__fetch__fetch` 读取 `https://example.com`，返回 `Example Domain`。

已覆盖：

- `inherit-current-permissions` 下 child 通过真实 `GenericAgentHandler.dispatch` 使用工具。
- `read_only` 反事实：`file_write` 与非只读 MCP 被阻断，`load_skill` 仍允许。
- permission profile/version 进入 `cacheKey`，且 profile 或 policy version 改变时 resume cache miss；完全相同时 cache hit 并复制 transcript。
- child transcript/journal 写入 permission/tool events，result artifact 不内联 `transcriptEvents`，parent session transcript 不包含 child tool/skill/MCP 细节。
- 真实 API `nativeToolCallingFileSkillMcp`：真实 child agent 的 `toolCalls` 包含 `file_read`、`load_skill`、`mcp__p8_stub__read_marker`、`no_tool`，`toolSummary.denied == 0`，`fileReadOk/skillLoadOk/mcpReadOk == true`。
- 真实 MCP diagnostic：不 mock `mcp_runtime.call_mcp_tool`，通过真实 `mcp_runtime.discover_mcp_tools_cached()` 发现 MCP 工具，在 `GA_RUN_REAL_API_E2E=1 GA_RUN_REAL_MCP_E2E=1` 下真实调用 `mcp__fetch__fetch`，`mcpCalled/mcpReturned == true`，`secretScan: []`。
- 真实 native GPT 主套件已通过，当前真实 API 主路径、工具继承与诊断 case 具备稳定回归基线。

当前边界：

- `nativeToolCallingFileSkillMcp` 中的 MCP 是受控 stub，用于稳定验证真实模型 tool-calling 与权限/转发链路。
- `realMcpDiagnostic` 调用真实 MCP，但设计为 diagnostic-only，不纳入主 `summary.passed` 门禁；其结果依赖本机 MCP 配置、网络和外部 MCP 服务状态。
- 当前未把 Context7 纳入 GenericAgent P8 runtime，因为 `mcp_runtime` 当前发现到的本机 MCP 工具包含 `fetch`、`memory`、`sequential-thinking`、`tavily`，未发现 context7。

验收要点状态：

- 默认不是 read-only：已覆盖。
- 继承权限信息进入 cache key：已覆盖。
- tools/skills/MCP 调用事件只进入 child transcript/artifact：已覆盖。
- parent backend history 不包含 child 内部 tool transcript：已覆盖。
- 真实 API child 主动调用内置工具/skill/MCP：已覆盖。
- 真实 MCP 非 mock 调用：已通过 diagnostic 覆盖。

### 10. 大 artifact / transcript 隔离 E2E

目标：验证大量 child transcript / artifact 下的隔离、复制和 UI 行为。

已测：

- result artifact 存在。
- transcript artifact 存在。
- result JSON 不内联 `transcriptEvents`。

还需要测：

- 大量 child transcript。
- 多 job 大 transcript。
- resume 时 transcript copy 性能。
- parent session transcript 是否完全不污染。
- bridge/UI 查看 detail 时是否不会加载过大内容导致卡顿。

验收要点：

- result artifact 仍保持轻量。
- transcript 单独存储。
- resume copy 后路径指向 resumed artifact 目录。
- UI/detail 不因 artifact 过大卡死。

### 11. workflow JS worker 异常脚本 E2E

目标：验证 worker 脚本自身异常时，runtime 和 bridge 能正确收敛。

建议覆盖：

- script 抛异常。
- pipeline stage 抛异常。
- parallel thunk 抛异常。
- agent options 非法。
- return value 不可 JSON 序列化。
- workflow script 被安全扫描拒绝。

验收要点：

- run 进入失败终态。
- journal 有明确 worker error。
- bridge emit final/error。
- 不遗留子进程。

### 12. cache key 真实变体 E2E

目标：把单元测试已覆盖的 cache key 变体提升到真实/半真实 E2E。

单元测试已覆盖：

- args `{}` vs `"{}"`。
- args `None` vs `"null"`。
- cross-session 不 replay。
- cached transcript ref 指向 resumed artifact。

建议真实 E2E 补充：

- 同 script，不同 args，确认会重新烧 API。
- 同 args，prompt 改动，确认只复用改动前前缀。
- options 改动，确认停止复用。
- permission profile/version 改动，确认不复用。

验收要点：

- cache hit/miss 与预期完全一致。
- fresh job 数量符合预期。
- 不发生跨 session replay。
- 不发生权限变更后的 replay。

## 推荐下一步优先级

建议优先补以下 5 类：

1. `P1 timeout / stop / mid-call 边界真实 E2E`：覆盖真实网络慢响应、极短 timeout、stop/kill 后资源回收与 resume 行为。
2. `P1 parallel 部分失败真实/半真实 E2E`：验证部分 child 失败时成功 job、失败 job、artifact、failure policy 与 bridge 终态一致。
3. `P2 JS worker 异常脚本 bridge E2E`：覆盖脚本 throw、pipeline/parallel thunk throw、非法 options、不可序列化返回和安全扫描拒绝后的 bridge 事件。
4. `P2 rate limit/429、网络抖动 diagnostic`：用 opt-in 压力/稳定性诊断暴露真实 API 服务商限流、429、5xx 与高 fan-out 下的收敛问题。
5. `P3 cache key 真实变体、大 artifact、Context7/explicit approval 专项`：验证 args/prompt/options/permission profile 改动的真实 hit/miss、大 transcript 隔离，以及后续 Context7/审批闭环专项。

原 P0 “bridge 非成功终态完全缺失 / interrupted source resume 完全缺失”已更新为：基础 deterministic/bridge 单元已完成，剩余是 opt-in 真实 API/完整 bridge E2E 层。

## 安全与执行约束

- 所有真实 API E2E 必须 opt-in。
- 默认 unittest 不得烧真实 API。
- 不读取、不打印、不提交真实 API key。
- 输出必须脱敏。
- 真实 API artifact 必须扫描 secret。
- 不提交 `mykey.py`、`mykey.json`、`mcp.json`。
- 不提交真实 API 输出 artifact。
- 压力测试必须单独环境变量控制，避免误触发高成本或限流。
- case 级诊断输出只能包含脱敏摘要、case 名称、状态、耗时和错误类型，不得输出 provider key、完整原始请求或未脱敏响应。
