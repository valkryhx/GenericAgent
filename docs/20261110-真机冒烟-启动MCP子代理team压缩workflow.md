# 真机冒烟：启动 / MCP / 子代理 / agent team / 压缩 / workflow（2026-11-10）

> 用户要求：跑真实的 `./ga`，只看「有没有 bug 或能力退化」和「UX 是否正常」，不做全量 e2e。
> 方法：ConPTY（`winpty`）+ `pyte` 抓屏，真实 `llm.yaml`（zhanlu-ds），
> 会话数据隔离到 `temp/smoke_data`；脚本 `temp/ga_smoke_probe.py`、
> `temp/startup_timing_probe.py`、`temp/ga_workflow_probe.py`。

## 0. 结论

六块都通，没有发现功能退化；只有三处「值得看一眼」的观感问题（见 §7），其中两处大概率是抓屏工具本身的伪影。

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 启动 | ✅ 输入 1.7s 可用，从不被 MCP 阻塞 | `input-ready 1.7s / mcp-settled 39.2s` |
| MCP | ✅ 37 工具全部可发现，真实调用成功 | `● mcp__memory__read_graph` → `⎿ MCP success` |
| 压缩 | ✅ `--list` + `--from 1` | `Compacted 15 of 16 messages into summary context (kept 1).` |
| 子代理 | ✅ spawn → wait → read → close 全链路 | `▸ ✓ read_tokens_head completed` |
| agent team | ✅ 面板 + 成员/任务板 | `Team session-… · lead team-lead · 0 member(s)` |
| workflow | ✅ 真实跑通一个 1-child 工作流 | `▸ ✓ count-skill-md-files 1/1 agents done · integration accepted · audit passed` |

## 1. 启动：输入就绪 ≠ MCP 就绪

```text
input-ready  : 1.7s
mcp-settled  : 39.2s
mcp line     : MCP ready with failures · 7 servers · 35 tools · 1 failed
```

`49e9bf7 fix(mcp): keep MCP cold start off the first-turn critical path` 的设计仍然成立：
输入框 1.7s 就能打字，MCP 从不在首轮关键路径上。

但本轮 MCP 收敛用了 39.2s（对照：Python 侧冷启动 `mcp_status(timeout=120)` 11.7s 全连上、
37 个工具）。多出来的时间来自「某个 server 一次失败 + 只重连失败项」：几十秒后打开 `/mcp`
面板，7 个 server 全是 `connected`（fetch 6 / tavily 5 / sequential-thinking 1 / memory 9 /
exa 2 / context7 2 / sqlbotmcp 12）。

## 2. MCP 真实调用

```text
 ● mcp__memory__read_graph
   ⎿  Calling MCP tool: mcp__memory__read_graph
   ⎿  MCP success
 图谱是空的：0 个实体，0 条关系。entities: []、relations: []
 Worked for 26s · ↑26.5k ↓59 Σ26.6k cache 26.2k
```

工具名、调用行、结果行、用量行都齐。注意 ↑26.5k：系统提示 + 37 个 MCP 工具 schema 的固定成本，
跟问题本身无关。

## 3. 子代理

```text
 ● spawn_agent(task_name: read_tokens_head, fork_turns: none, permission_profile: read_only, agent_type: explore, …)
   ⎿  Spawning subagent: read_tokens_head
   ⎿  Subagent /root/read_tokens_head started (pid=42160).
 ● wait_agent(target: read_tokens_head, condition: all_terminal, timeout_seconds: 180)
 ● read_agent_result(target: read_tokens_head, max_output_chars: 2000)
   ⎿  Read result for /root/read_tokens_head: success.
 ● close_agent(target: read_tokens_head, reason: task done)
子代理 read_tokens_head 已完成并关闭……
Worked for 1m 18s
```

参数过长自动折成 `(ctrl+o to expand)`，没有撑破布局；最终回答正确（`tokens.py` 首行是 `"""`）。

## 4. agent team / 压缩

```text
 Team session-5221105a · lead team-lead · 0 member(s) · 0 task(s)
 ▸ ✓ read_tokens_head completed    0 task(s)
```

```text
 /compact --list            → 16 行（含子代理的大 tool_result，单条 8012 tok）
 /compact --from 1          → Compacted 15 of 16 messages into summary context (kept 1).
 耗时 80.9s
```

80.9s 是真实摘要调用；期间只有 “Compacting conversation” 活动行，没有进度百分比。

## 5. workflow

```text
 ● workflow(script: export const meta = { name: 'count-skill-md-files', … })
   [Workflow] launched count-skill-md-files (wf_b37bb4679cdc42058097f7689740ba2c) in the background.
   You will be notified when it completes; do not wait for it. Report the launch in one line and end your turn.
 …（回合结束，通知触发新一轮）…
 ▸ ✓ count-skill-md-files  1/1 agents done · 26.5k tok · integration accepted · audit passed
```

即：启动后回合正确结束、完成通知唤醒新一轮、child 结果并入最终回答，全部符合设计。
（第一次尝试被上游 502 打断，属 zhanlu-ds 侧故障；502 在 `_RETRYABLE` 名单里，已按
`max_retries=3` 退避重试后仍失败才报错。）

## 6. UX 观感

- 面板一律「一行 dismissed」收尾（`/skills`、`/workflows`、`/mcp`），不残留半屏。
- 工具调用行 `● name(args…)` + 结果行 `⎿ …`，长参数折行并给 `(ctrl+o to expand)`。
- 状态行、用量行（`↑ ↓ Σ cache`）、`Worked for …` 尾部信息齐全。
- `/hooks` 的尾部提示不是 “Esc close”（探针按固定文案判定才报 FAIL），面板本身内容正常：
  覆盖矩阵、contract-only 事件、HTTP policy、Recent executions。

## 7. 值得看一眼的三处（都未定性为 bug）

1. **`/skills` 列表行尾残留**：出现 `… invoke 13028 chars]chars]`、`… chars] 20302 chars]`。
   典型的「重绘时没有清到行尾」。但 ConPTY 会重排/丢弃部分序列，需在真终端复核再定性。
2. **最终回答里 `skills/**/SKILL.md���同样只返回这一条`**：`���` 是抓屏时 UTF-8 被切断的伪影
   （pyte 在 chunk 边界按字节处理），不是应用输出。
3. **502 的整段 HTML 原样进入对话区**：有 `!!!Error:` 前缀，但读起来很吵，可考虑只留首行。

另：MCP 横幅的**连接中**分支是准确的（`MCP connecting in the background … 5/7 connected … you can type now`），
但收敛成 `MCP ready` 之后再掉线会退回 `MCP ready with failures`，计数也会继续变化（6/25 → 7/37）——
措辞上“ready”仍偏绝对，属观感问题。

## 8. 本轮没覆盖

- 多 child 并发 workflow（只跑了 1 child）、child 失败/重试路径。
- 真开 teammate（`/team` 只看了面板；子代理是 Agent 工具，不是 teammate）。
- `/resume`、`/rewind`、goal 模式、hooks 的真实触发。
- 长会话压力（压缩只做了一次手动 partial）。

## 9. 第二轮：/rewind、/resume、/goal（`temp/ga_ux2_probe.py`）

| 命令 | 屏幕 | 判定 |
| --- | --- | --- |
| `/rewind` | `Rewind Conversation` + 用户消息行 + `Enter select - Up/Down move - Esc cancel` | ✅ |
| `/resume` | `No resumable sessions found.`（隔离数据目录，确实没有） | ✅ |
| `/goal <条件>` | `Goal: 代码改完后用一句话总结改动`，随即起一轮（`✻ Tinkering (2s …)`） | ✅ |
| `/goal` / `/goal clear` | 跑批中 → `Queued 2 messages · sends at the next tool boundary` | ⚠️ 见下 |

**排队语义**：running 状态下输入的任何斜杠命令都不会立即执行，会作为用户消息排队
（`queued — will be sent at the next tool boundary`）。这和「执行中继续输入」的设计一致，
但意味着一件事：**`/goal clear` 在跑批时不能立刻生效**，要等下一个工具边界。想“现在停”只能按 Esc。
（是否要让 `/goal clear`、`/stop` 一类控制命令走即时通道，值得单独决定。）

**MCP 横幅文案**在本轮反而验证是准确的：

```text
 MCP connecting in the background · exa ◌ context7 ◌ · 5/7 connected · 33 tools · you can type now
```

§7 里“ready 偏早”的说法据此修正：连接中会明确说 connecting + 可输入，收敛后才变 ready。

## 10. 两 child 并行 workflow（`temp/ga_workflow_probe2.py`）

```text
 ● workflow(script: export const meta = { name: 'count-py-files', … })
   [Workflow] launched count-py-files (wf_199cd31e7bcd483e90c03e92a4cca476) in the background.
 …通知唤醒新一轮…
 ├───────────────┼──────────────┤
 │ tests/        │ 321          │
 │ genericagent/ │ 125          │
 │ 合计          │ 446          │
 └───────────────┴──────────────┘
 ▸ ✓ count-py-files  2/2 agents done · 54.4k tok · integration accepted · audit passed
```

两个 child 真并行、结果经 schema 回传、脚本汇总、集成审计通过。

顺带一个**能力观察（不是 bug）**：`file_glob` 有 100 条硬上限，两个 child 都被截到 100，
脚本里的约定让它们改用 `code_run` + `pathlib.rglob` 复核，模型在最终回答里主动说明了这一点，
并自己又独立跑了一次计数（321 / 125 两次一致）。

## 11. 修掉「控制类斜杠命令被当聊天文本排队」

### 11.1 问题

真机里 `/goal clear` 在跑批时显示 `(queued — will be sent at the next tool boundary)`，
随后经 `render_queued_prompts` 变成一段模型输入文本——既没生效，模型还会读到字面量
“/goal clear”。CC 不会这么干：忙碌时只有 `prompt` / `bash` 可入队，其它模式直接拒绝
（`m1635:71938`，`mode_not_queueable`）。

### 11.2 改法（控制类即时 + 其余保留草稿）

- **即时**：`/goal`（看状态）、`/goal clear|stop|off|reset|none|cancel` —— 纯状态变更，
  新增 `goal_command` 命令，走 `InkBridge.goal_command()` 直接调
  `agent._handle_goal_command()`，不排轮次。
- **拒绝但保留草稿**：其它斜杠命令在跑批时不入队，输入框内容保持原样，并提示
  `运行中：斜杠命令不会排队，请等回合结束或先 Esc 停止（/goal clear 可直接执行）`。
- **普通文本行为不变**：仍然折叠进当前回合（这是“边跑边收”的核心能力）。
- `/goal <条件>` 自身要起一轮，跑批中拒绝（`goal_busy`），不会变成模型文本。

### 11.3 自测

- `frontends/ink/ui/src/composerDuringRun.test.ts` 新增两条（真实 Ink 渲染 + 假 bridge）：
  `/goal clear` 必须发 `goal_command` 且不出现 `queue_prompt`；`/goal 条件` 必须保留草稿、
  不发任何命令。stash 掉实现后两条都变红。
- `tests/test_ink_bridge.py` 新增两条：`goal_command` 立即执行 status/clear；
  `/goal <条件>` 返回 `goal_busy` 且不调底层。stash 后变红。
- 全量：UI 649 passed；Python 见下。

### 11.4 真机复核（`temp/ga_goal_running_probe.py`，zhanlu-ds）

跑批中发 `/goal clear`：

```text
 GenericAgent                            running
 LLM Running (Step 1) ...
   No goal set.                     ← 立刻执行，没进队列
```

紧接着发 `/goal 写完这段总结再停`：

```text
 > /goal 写完这段总结再停             ← 草稿保留，可以等回合结束再发
 运行中：斜杠命令不会排队，请等回合结束或先 Esc 停止（/goal clear 可直接执行）
```

提示 2 秒后自动消失，恢复常规 hint 行。五项断言全过（boot / turn_started /
goal_clear_immediate / goal_set_kept_draft / goal_set_notified）。

### 11.5 补测：命令形状与普通文本的边界

第一版判定用 `expanded.startsWith('/')`，会把「跑批中贴进来的绝对路径」也拒掉
（`/usr/bin/python3 跑一下` 会被当成命令保留成草稿）。改成形状判定：

```ts
const SLASH_COMMAND_SHAPE = /^\/[a-zA-Z][\w:-]*(?:\s|$)/
```

即只有 `/name`、`/name:sub` 这种才当命令；`/`、`/usr/bin/python3 …` 仍然按普通文本排队。
新增的回归用例：

- `/goal`（裸）、`/goal clear`、`/goal STOP`（大写同义词）、`stopping` 态 → `goal_command`
- `/goal 写完测试再停` → 拒绝 + 草稿保留
- `/foobar`（未知命令）→ 拒绝，不喂给模型
- `/usr/bin/python3 跑一下这个`、`/` → 仍然 `queue_prompt`

### 11.6 补测：zhanlu-ds 的 502 / 504 是可重试的偶发故障

真机冒烟里撞到过 502（provider 侧），`tests/test_llm_retry_transient.py` 把行为钉住：

| 场景 | 断言 |
| --- | --- |
| 502 → 200 | 重试一次后成功，输出里没有 `!!!Error` |
| 504 → 200 | 同上 |
| 连续 502 | 共尝试 `max_retries + 1` 次，最后只报一条 `!!!Error: HTTP 502` |
| 403 | **不重试**（非 `_RETRYABLE`），直接报错 |

反向确认：把 `502, 504` 从 `_RETRYABLE` 里删掉 → 3 条变红；恢复 → 4 条全绿。

## 12. 断链重试的 UX：从「整段报错」改成 CC 式横幅

### 12.1 问题

真机里 zhanlu-ds 侧偶发 502 / 504。GA 原来的表现是：

```text
 !!!Error: HTTP 502: <html>
 <head><title>502 Bad Gateway</title></head>
 ...（整段 nginx HTML）
```

两个毛病：① 重试期间界面**完全没反馈**，看起来像卡死；② 重试用尽后，整段
HTML 原样进对话区，还留在转录里（截图/滚动区都会一直看到）。

用户的要求是「换成 CC 源码里的 retrying n/m，重连上之后正常输出把它覆盖掉」。

### 12.2 CC 的做法（319 源码坐标）

| 事实 | 坐标 |
| --- | --- |
| `api_error` 系统消息带 `retry_in_ms` / `retry_attempt` / `max_retries`，注释写明 "REPL renders the retry banner from this" | `m0165:9917-9939` |
| 横幅文案 `· Retrying in ${x} · attempt ${attempt}/${maxRetries}` | `m1795`（约 820-1010） |
| 默认重试次数 `FDr = 10`，上限 `Ist = 15`，可用 `CLAUDE_CODE_MAX_RETRIES` 覆盖 | `m0591:125212-126126` |
| 回调 `onRetryStatus({kind:'retrying', error, attempt, maxRetries, retryInMs, deadline})` | `m0591:132618` |

关键点：**横幅是 UI 状态，不是对话内容**。CC 从不把 5xx 的正文写进转录，
只有重试用尽才落一条错误。

### 12.3 GA 的落法

分三层，每层都可独立测试：

1. **wire 层**（`genericagent/llm/core.py`）
   - `BaseSession.on_retry_status = None`，默认不打扰任何前端。
   - `_stream_with_retry` 在 `retry_http` / `retry_error` 分支调用
     `_notify_retry_status({"kind": "retrying", ...})`；收到重连后的**第一个
     chunk 之前**先发 `{"kind": "recovered"}`。
   - 顺带修掉一个真 bug：重试用尽时错误是通过 `chunk` 通道送出的，
     会被"恢复"判定误判成重连成功 → 横幅先消失、错误再弹出。改成独立的
     `fatal` 通道，只有真正的流数据才算恢复。
   - 错误正文只留首行 + 160 字符（`_short_error_body`），不再是整段 HTML。
   - 默认 `max_retries` 从 3 提到 **10**（对齐 CC `FDr`），写在 `DefaultsCfg`。

2. **bridge 层**（`frontends/ink/bridge.py`）
   - `_bind_retry_status(agent)` 把回调装到 `agent.llmclient.backend`；
     `_bind_agent` 与 `model_switch` 都调用它（换模型 = 换 backend，不重绑
     就再也收不到横幅）。
   - 转发成 wire 事件 `retry_status`，字段 `kind/status/attempt/maxRetries/delayMs/error`。

3. **UI 层**（`frontends/ink/ui/src/`）
   - `protocol.ts` 加 `retry_status` 事件；`state.ts` 存 `retryStatus`
     （`resumeAt` 是本地 deadline，横幅自己倒计时）。
   - 渲染**复用已有的 activity 行**，行数不变 —— 多一行/少一行都会让提示行与
     输入框跳一下，而断链恰好是最不该抖动的时刻。
   - 文案：`· Retrying in 3s (HTTP 502) · attempt 1/10 · esc to interrupt`。
   - `recovered` 到达即清空；`status: idle` / `clear` / 新会话也清空（兜底）。
   - 重试事件到达时同时把 `error` 槽置空：重试不是回合失败，红字不该一直挂着。

### 12.4 自测

Python（`tests/test_llm_retry_transient.py`，新增 `RetryStatusBannerTest`）：

| 场景 | 断言 |
| --- | --- |
| 502 → 200 | 事件序列恰为 `retrying` → `recovered`，无 `!!!Error` |
| 504 → 200 | 顺序是 `retrying` → `recovered` → `token`（先撤横幅再出字） |
| 一次成功 | **零**横幅事件 |
| 重试用尽 | 只有 `retrying`，**没有** `recovered`（不能让横幅先消失再报错） |
| 403 | 不重试、无横幅 |

Bridge（`tests/test_ink_bridge.py::InkBridgeRetryStatusTest`）：
回调确实装到 backend；`retrying`/`recovered` 只产生 `retry_status`，
**不**产生任何会留在转录的 `error` / `system` / `assistant_delta`；换模型后新
backend 拿到回调。

UI（`state.test.ts` / `activityStatus.test.ts`，新增 6 条）：横幅建立与清除、
不冒充回合错误、回合结束自动清、文案三种形态（带码 / 无码 / 分母不倒挂）。

反向确认（`git stash` 掉实现后重跑）：

| 被 stash 的文件 | 结果 |
| --- | --- |
| `genericagent/llm/core.py` | 3 条红（`recovered` 顺序、502 序列、用尽不发 recovered） |
| `frontends/ink/bridge.py` | 3 条红（`AttributeError: no attribute 'on_retry_status'`） |
| `frontends/ink/ui/src/state.ts` | 3 条红（`retryStatus` 恒为 `undefined`） |

全量：前端 660 passed；Python 见下。

### 12.5 真机复核（`temp/ga_retry_banner_probe.py`）

做法：本地起一个假网关，**前 2 次返回 502（带整段 nginx HTML），第 3 次返回正常 SSE**，
把 `GA_LLM_CONFIG` 指过去，ConPTY 抓屏。

抓到的横幅原文：

```text
 · Retrying in 2s (HTTP 502) · attempt 1/5 · esc to interrupt
```

恢复后：

```text
 重连成功了，这是一段正常输出。
```

七项断言全过：

| 断言 | 结果 |
| --- | --- |
| boot | ✅ |
| banner_shown | ✅ 屏幕上出现过 `Retrying in` |
| banner_mentions_502 | ✅ 带状态码 |
| banner_mentions_attempt | ✅ `attempt 1/5` |
| stream_recovered | ✅ 第三次请求的正文出现在对话区 |
| banner_cleared | ✅ 恢复后屏幕上不再有 `Retrying in` |
| no_error_html | ✅ 整段 HTML 与 `!!!Error` **都**没进对话区 |

网关侧计数 3，即 2 次 502 + 1 次成功，与 `max_retries: 5` 的配置一致。

### 12.6 真机抓到的一个渲染瑕疵（已修）

第一版把横幅塞进既有的 `ActivityView`（复用 `✻ {verb} ({seconds})` 骨架），
屏幕上变成了：

```text
 ✻ · Retrying in 2s (HTTP 502) · attempt 1/5 · esc to interrupt (0s)
```

`(0s)` 是假信息 —— 重试期间没有"已运行多久"这回事；前缀 `✻` 也让一行里出现两个符号。
改成独立的 `RetryBannerView`（固定 1 行高，样式与 `WorkflowStatusBarView` 一致），
**行高不变**所以布局不跳，但不再套那层骨架。复查屏幕输出即上面的干净版本。

这条只能靠真机抓屏发现：单测断言的是 `retryStatusText()` 的返回值，它一直是对的，
错的是外面那层渲染容器。
