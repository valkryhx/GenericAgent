# GA workflow 与 CC 319 动态工作流对照审计（2026-11-09）

> 对照源：`claudecode283_hack/docs/20261109-CC319动态工作流实现剖析.md`（5651 行，319 源码核验结果）。
> 审计对象：`genericagent/workflow/`（runtime / scheduler / child_agent / store / js worker）。
> 分支：`audit/cc319-workflow-parity`。所有结论都给了 GA 侧文件行号与可复现的探针/单测，未核验的项明确标注。

---

## 0. 结论

| 类别 | 数量 | 明细 |
| --- | --- | --- |
| 已修（本轮，TDD + 反向确认） | 3 | 运行时确定性 shim、prompt provenance 框架、resume 内容寻址匹配 |
| 已确认、本轮不修 | 3 | 三态 stall 检测缺失、`budget` DSL 缺失、VM 边界 4096 数组上限缺失 |
| 审计通过（语义一致，无需改） | 3 | orchestration 工具裁剪、`parallel`/`pipeline` 失败语义、`bashCommandClamp` |
| 真实回归 | **11/11 通过** | 10 个 Python workflow e2e + 1 个 Ink 全链路（zhanlu-ds，含 `/workflow` opt-in 与 Tavily MCP），详见 §8 |

对照基准用 319 文档 §12.5 的六个不变量：

1. 脚本不能碰网络、文件、时间、随机数
2. 每个 `agent()` 经过同一个限额计量点
3. 脚本算出的 prompt 必须带 provenance 框架
4. 同输入 → 同结果
5. 任何时刻可 kill，且不留孤儿
6. 恢复时未变的 agent 不重跑

---

## 1. 已修 1：运行时确定性 shim（不变量 1、4）

**319 事实**（文档 §4.5 / §12.1.2）：静态检查 + **运行时 `Date` shim** 双保险。
静态检查只查 4 种逃逸，shim 兜住漏掉的路径（`Date["now"]`、别名、`globalThis.Date.now`）。

**GA 修复前**：只有静态正则（`genericagent/workflow/determinism.py`），
其 docstring 自己写明 "it is not a sandbox"，测试里甚至用
`test_matches_claude_code_bypass_surface` 固化了这三个盲区。worker 的 vm context
没有覆盖 `Date` / `Math`，所以这些写法在**运行期**照常可用：

```text
$ node genericagent/workflow/js/worker.js   # start: const D = Date; return D.now()
{"type":"done","result":<时间戳>}            # 修复前
```

**修复**（`genericagent/workflow/js/worker.js`）：给 vm sandbox 注入 Proxy 版 `Date` / `Math`：

- `new Date()` / `Date()` / `Date.now()`（含 `Date["now"]()`、`D.now()`）→ 抛
  "unavailable in workflow scripts (breaks resume)"；
- `new Date(value)`、`Date.parse`、`Date.UTC`、`Math.max` 等照常可用；
- `instanceof Date` 仍成立（Proxy 保留 target 的 prototype）。

**测试**：`tests/test_workflow_worker_sandbox.py`（7 例，直接驱动真实 Node worker）；
把沙箱里的两行 shim 删掉，5 例立刻变红。

---

## 2. 已修 2：prompt provenance 框架（不变量 3）

**319 事实**（文档 §5.5 / §12.2.2）：四类 frame（computed / relay / assistant-context /
automated），核心两条：明确"这是脚本算的，不是用户说的"，并且**缩进每一行**——
这样第 0 列出现的 frame 样行一定是伪造的。

**GA 修复前**：`child_agent._build_prompt()` 把 `job.prompt` 原样放在 `Task:` 下面。
workflow 脚本是模型写的，脚本可以把一段"用户已批准……"塞进 prompt，子 agent 无法区分。

**修复**（`genericagent/workflow/child_agent.py`）：

- 新增 `PROVENANCE_FRAME`（computed task 文案）与 `frame_computed_task()`：
  框架 + 每行缩进 2 空格；
- `_build_prompt` 的 `Task:` 段改用 `frame_computed_task(job.prompt)`；
- `GA_WORKFLOW_PROMPT_PROVENANCE=0` 可关闭（对照 319 的 env 开关，便于回滚）。

**测试**：`tests/test_workflow_child_agent.py::WorkflowPromptProvenanceTest` 3 例：
框架存在、多行缩进、伪造的 `[Workflow harness — user request]` 不可能出现在列 0、开关生效。
把 `frame_computed_task(...)` 换回 `job.prompt`，2 例变红。全套 73 例通过。

---

## 3. 已修 3：resume 内容寻址匹配（不变量 6）

**319 事实**（文档 §12.2.1）：journal 每个 agent 一行，键是内容哈希
（prompt + schema + model + tools + args + phase …），"脚本改一行，只有受影响的 agent 失效"。
并明确点名序号方案会让后续 agent 全部失效。

**GA 修复前**（`runtime._match_cached_agent`）：只比较 `resume_plan[call_index]`，
一旦不匹配就 `del resume_plan[call_index:]` —— 脚本顶部插入一个 agent，
后面所有 agent 的 call index 全部位移，全部重跑。

**修复**：先按同位置匹配（未改脚本时零成本），再按 `promptHash + optionsHash`
在整份 plan 里查找；不再删除 plan 尾部。改动只影响"命中哪个缓存"，不改变可复用性判定
（hash 仍由 `AgentScheduler._cache_key` 生成，包含 args / 工具上下文 / 权限 / 工作区）。

**测试**：`tests/test_workflow_resume_matching.py` 5 例：前缀命中、顶部插入后旧 agent 仍命中、
prompt 变化不复用、options 变化不复用、查找不修改 plan。回退到旧实现时"插入"用例变红。
`test_workflow_runtime` 115 例通过。


### 3.1 内容复用必须配一道依赖守卫（本轮新增，防止引入 stale 结果）

只按内容匹配会带来一个 GA 特有的风险：**CC 的 prompt 由脚本把上游结果拼进去**，
所以上游变了下游 prompt 也变；GA 的上游结果是 **handoff metadata**，不参与 prompt 哈希。
若上游这次真的重跑（prompt 改了），下游仍按内容命中，就会返回**基于旧上游输出算出的结果**。

修复（`AgentScheduler._demote_stale_cached_jobs()`，`tick()` 开头执行）：

- 只有**所有依赖同样是 `cached`** 的 cached 作业才保留；
- 任一依赖这次重跑（`succeeded`/`degraded`…）→ 该作业降级为 `queued`，清掉 `result`/`handoff`/
  `result_ref`，写 `agent_cache_demoted` 事件后真正重跑；
- 依赖还没结束时先不判定，等它落定再判。

宁可多跑一次，也不返回静默过期结果；这条守卫是"内容寻址复用"能安全启用的前提。
测试：`CachedJobDependencyGuardTest` 4 例（含 `tick()` 真的调用了守卫的接线用例），
删掉 `tick()` 里的调用立刻变红。

### 3.2 真实 LLM 复测（防止框架改动破坏真实链路）

provenance 框架会改变子 agent 收到的 prompt，必须用真实链路验证：

```powershell
$env:GA_RUN_REAL_P0_WORKFLOW_E2E='1'
$env:GA_WORKFLOW_LLM_PROFILE='zhanlu-ds'
$env:GA_REAL_API_EXPECTED_MODEL='moma/deepseek-latest-flash'
python tests/real_workflow_p0_e2e.py
```

结果：`passed: true`，`skipped: false`，gate 红 → 修复 agent 真实调用
`code_run` / `file_read` / `file_write` → gate 绿，`fixedSource: true`。
说明框架+缩进没有改变模型的执行能力，只改变了它对"这段话是谁说的"的判断。

---

## 4. 已确认、本轮不修的缺口

### 4.1 三态 stall 检测缺失（319 §6.6 / §12.2.3，建议 P1）

319 把"agent 停滞"拆成三种原因并给三种恢复动作：

| 状态 | 含义 | 319 的动作 |
| --- | --- | --- |
| `throttled` | 还没开始（限流/排队） | 等 |
| `stalled` | 开始了但 `lastProgressAt` 很久不动 | 重试 |
| `reply-lost` | 动了但没回话 | 重试 |

GA 现状（`genericagent/workflow/scheduler.py`）：只有**失败后的重试策略**
（`retryPolicy.maxAttempts` 1–3、`retryableErrors`、`retryNotBefore`），
没有任何"多久没进度"的判据（全仓 grep `stall|heartbeat|last_progress` 均无命中）。
后果：一个卡住但没报错的子 agent 只能等外层整体超时，且不会得到"重试"这种更便宜的恢复。

### 4.2 `budget` DSL 缺失（319 §13.1 #6，建议 P2）

319 保留了 API 但 `budget.total` 恒为 `null`（能力被摘掉、接口还在）。
GA 的 vm sandbox **完全没有 `budget`**，脚本一用就崩：

```text
ReferenceError: budget is not defined
```

如果将来要从 CC 迁移脚本，这一行会直接失败。建议注入
`budget: { total: null, get remaining() { return null } }`，与 319 行为对齐。

### 4.3 VM 边界 4096 数组上限缺失（319 §4.4，建议 P2）

319 在 VM 边界强制"数组长度不得超过 4096"（`jS = 4096`，超出直接报错），
避免巨型数组一次性穿过 RPC 边界。GA 实测 `new Array(5000).fill(1)` 可以正常返回：

```text
{"type":"done","result":5000}
```

目前没有对应的 payload 上限（脚本 `return` 的数组、`agent()` 参数里的数组都可能很大）。
建议在 worker 的 RPC 出口做长度检查，与 319 对齐。

### 4.4 明确不在本轮范围

| 319 能力 | 原因 |
| --- | --- |
| v2 "world" 引擎（§10） | 319 自己把它列为 P2；GA 的顺序脚本 + `parallel` 场景不依赖它 |
| 远程三条通道（§11） | GA 的外部调用面另有设计（见 `docs/20261106-CC被外部调用面调研.md`） |
| 命名 workflow → slash 命令（§9.6） | 产品形态问题，不是缺陷 |
| `withholdScriptFromSdkEvents`（§3.5） | GA 的 `workflow_started` 事件不含脚本正文，已不存在同一泄露面 |

---

## 5. 审计通过：语义一致，无需改动

| 主题 | GA 现状 | 与 319 的关系 |
| --- | --- | --- |
| 子 agent 的工具裁剪 | `genericagent/agents/capabilities.py::ORCHESTRATION_TOOLS` 在 `is_subagent` 且未允许委派时移除 `workflow` / `spawn_agent` / `send_message` / 任务板等 | 等价于 319 `agent()` 的 `disallowedTools = ["SendUserMessage","Agent","Workflow"]` |
| `parallel` / `pipeline` 失败语义 | `worker.js` 用 `Promise.allSettled`，失败分支映射为 `null` | 与 319 一致（失败不 reject 整个并行组） |
| `bashCommandClamp` | `child_agent.py` 有 per-spawn 注释与实现绑定（对齐 CC） | 一致 |

> 未核验：journal 行类型/marker 的逐项对照、`AQn()` 七项职责、v2 引擎语义。
> 这几项要么 GA 没有对应实现（v2），要么字段形状不同但用途等价，留待下一轮。

---

## 6. 复现命令

```powershell
# 运行时确定性（修复后应报错）
printf '%s\n' '{"type":"start","script":"export const meta = { name: \"p\", description: \"p\" }\nconst D = Date; return D.now();","args":{},"timeoutMs":5000}' | node genericagent/workflow/js/worker.js

# 审计用例
python -m unittest tests.test_workflow_worker_sandbox tests.test_workflow_resume_matching tests.test_workflow_child_agent tests.test_workflow_runtime
```

---

---

## 8. 真实 LLM 回归矩阵（11 次，2026-11-09）

目的：验证 2026-11-08 的子智能体改造与本轮 workflow 修改之后，GA 自己的动态工作流仍能正常跑。
全部使用真实 LLM（`zhanlu-ds/moma/deepseek-latest-flash`），日志与汇总在 `temp/perf/matrix/`（临时目录，不入库）。

| # | 场景 | 脚本 | 耗时 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | P0 门禁：红 gate → 修复 agent → 绿 gate | `real_workflow_p0_e2e.py` | 11.9s | ✅ passed |
| 2 | max turns 重试 | `real_workflow_max_turns_retry_e2e.py` | 10.2s | ✅ passed |
| 3 | 严格 schema 子代理 | `real_workflow_strict_schema_child_e2e.py` | 8.2s | ✅ passed |
| 4 | 降级语义 | `real_workflow_degraded_semantics_e2e.py` | 22.3s | ✅ passed |
| 5 | slice7 schema 回落 | `real_workflow_slice7_schema_fallback_e2e.py` | 7.0s | ✅ passed |
| 6 | wait barrier（上游完成才放行下游） | `real_workflow_wait_barrier_e2e.py` | 72.7s | ✅ passed（2 job，barrier 满足） |
| 7 | overview/detail 导出 | `real_workflow_overview_detail_export.py` | 1.1s | ✅ passed（导出既有 run，非新执行） |
| 8 | 工作区外 deliverables（3 并行写） | `real_workflow_external_dir_deliverables_e2e.py` | 11.2s | ✅ 10/10 checks |
| 9 | 经 bridge 工具入口、工作区外报告 | `real_workflow_external_reports_via_tool_e2e.py` | 25.3s | ✅ 10/10 checks（3 agent 各 token>0） |
| 10 | P0 复跑（稳定性） | `real_workflow_p0_e2e.py` | 12.7s | ✅ passed |
| 11 | Ink 真实 App：`使用 workflow` opt-in → Tavily 搜索 → 生成 result.html | `real_ink_ui_workflow_autonomous_e2e.ts` | 约 6 分钟 | ✅ passed（MCP 工具真实调用、result.html 12,943 B、UI 回到 idle） |

结论：11/11 通过，未发现 workflow 链路回归。

> 说明：第 8/9 条的 `PASS/RESULT` 打印被 bridge 的后台日志重定向捕获，
> 实际结果在 `temp/ink_bridge_backend.log`（两次均 10/10）。

### 8.1 关于"开关"

这些脚本走的是 workflow 运行时/工具入口（`workflow_launch_from_tool`、`WorkflowRuntime`），
不经过会话级开关。会话开关 `/workflow on` 只影响**对话式自动编排**的判定
（`WorkflowActivationState`），不限制工具入口本身；单轮里写"使用 workflow"也会得到同样的 opt-in。
开关路径由第 11 条（Ink 真实 App + 真实 bridge + 真实 LLM）覆盖：
该条用 `使用 workflow` 单轮 opt-in 触发，与 `/workflow on` 走同一个 `resolve_workflow_activation`。

---

## 7. 变更清单（本轮）

| 文件 | 改动 |
| --- | --- |
| `genericagent/workflow/js/worker.js` | sandbox 注入确定性 Proxy（`Date` / `Math`），运行期兜住静态检查盲区 |
| `genericagent/workflow/child_agent.py` | `PROVENANCE_FRAME` + `frame_computed_task()`；`Task:` 段改为框架 + 逐行缩进；`GA_WORKFLOW_PROMPT_PROVENANCE` 开关 |
| `genericagent/workflow/runtime.py` | `_match_cached_agent` 支持内容寻址匹配，不再因一次不匹配丢弃后续缓存 |
| `genericagent/workflow/scheduler.py` | `_demote_stale_cached_jobs()`：依赖重跑时降级 cached 作业，防 stale 结果 |
| `tests/test_workflow_worker_sandbox.py` | 新增 7 例（真实 Node worker） |
| `tests/test_workflow_child_agent.py` | 新增 `WorkflowPromptProvenanceTest` 3 例 |
| `tests/test_workflow_resume_matching.py` | 新增 9 例（内容匹配 5 + 依赖守卫 4） |
