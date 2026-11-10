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

另：MCP 横幅在 `MCP ready` 之后计数仍可能继续变化（6 servers/25 tools → 7/37），
“ready”这个词偏早；已有 “connecting in the background … you can type now” 分支，但收敛后
再失败仍会改回 “MCP ready with failures”。

## 8. 本轮没覆盖

- 多 child 并发 workflow（只跑了 1 child）、child 失败/重试路径。
- 真开 teammate（`/team` 只看了面板；子代理是 Agent 工具，不是 teammate）。
- `/resume`、`/rewind`、goal 模式、hooks 的真实触发。
- 长会话压力（压缩只做了一次手动 partial）。
