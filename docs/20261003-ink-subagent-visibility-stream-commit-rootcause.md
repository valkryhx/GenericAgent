# GA Ink UI：3 个子智能体只显示 2 个的根因分析

**日期：** 2026-10-03
**现象：** 在 GA Ink UI 输入「使用3个子智能体分别用 tavily 搜搜 claudecode mods 是什么」后，UI 里似乎只出现了 2 个 subagent 的启动提示，第 3 个一闪就被后续内容覆盖。
**结论：** 不是子智能体漏启动，也不是 `spawn_agent` 失败。三个子进程全部真实启动并跑完；第 3 条 `[Action] Spawning subagent: ...` 也确实生成并写进了流式文本，只是**没能及时进入只增不改的 Static 滚动区**，在实时区被自己的 args 参数正文挤出了可视窗口。
**修复提交：** `9d5f4a0` fix(ink): stop stream-commit stalling behind open tool-args fences

---

## 1. 先排除「漏启动」：三条子代理都真实存在

排查的第一步是区分「没启动」和「启动了但没显示」。证据如下：

`temp/subagents/registry.json` 中三条记录俱全：

| 子代理 | pid | run_id |
|---|---|---|
| `ccmods_tav_def` | 20920 | run_000165 |
| `ccmods_tav_eco` | 24456 | run_000166 |
| `ccmods_tav_risk` | **11132** | run_000167 |

- `temp/subagents/events.jsonl` 中 `evt_005717/5718/5719` 依次记录三个 `agent_started`（均带 pid），`5720~5725` 依次记录三个 `turn_started` 与 `output_snapshot`。
- 三份产物均落盘：`temp/ccmods_tav_def/output.txt` 37333 B、`ccmods_tav_eco/output.txt` 7702 B、`temp/ccmods_tav_risk/output.txt` 18002 B。
- 三个 `state.json` 均为 `process_status=waiting_reply`、`turn_status=completed`。

**用户看到的「pid 以 1 开头」的那个是 `ccmods_tav_risk`（pid=11132）**，即第三个 spawn。所以「没启动」这个方向被彻底排除，问题一定在显示层。

> 排查注意：上一轮（05:34）还有同主题的另一组 `ccmods_definition` / `ccmods_ecosystem` / `ccmods_usage_risk`，与本次（05:39）的 tav 组并存。两组都有各自的 pid 与产物，分析时不要混为一谈。

## 2. 显示层怎么工作：Static 永久区 + 实时区

GA Ink UI 的聊天区是两层结构，分区逻辑在 `frontends/ink-ui/src/messagePartition.ts`：

- **Static 永久区**：由 Ink 的 `<Static>` 渲染，**只增不改**，一旦写入就不参与重绘（进入终端 scrollback）。
- **实时区（live）**：一个高度受限的视口，只绘制当前仍在流式的尾部内容。

流式期间，`frontends/ink-ui/src/streamCommit.ts` 负责把实时消息里「已经稳定」的靠前行**搬运**进 Static：把 `a-{taskId}` 这条 live 消息的溢出前缀切出来，作为一个 `done: true` 的 `a-{taskId}-c{n}` 段插入消息数组，live 消息只保留一个短尾巴。

这个「搬运」的正确性有个硬约束：**Static 是只增的**。如果先写进去的行后面又被修改，`<Static>` 会重新发出尾部内容，产生重复行。所以搬运必须是「前缀切分」，且切点不能破坏内容结构。

配套的两个容量上限：

- `DEFAULT_STREAM_LIVE_TAIL_LINES = 8`：搬运后 live 消息保留的行数。
- `DEFAULT_MAX_LIVE_ROWS = 12`：实时视口最多绘制的行数（`messageViewportPlan.ts`）。

也就是说：**实时区只画最后约 12 行**。任何还没被搬进 Static 的行，只要落到这 12 行之外，就看不见了。

## 3. 根因：搬运闸门要求「头部和尾部同时围栏平衡」

GA 每发起一次工具调用，主 agent 流出的文本形状是固定的（`agent_loop.py` 的 `_run_single_tool` + `ga.py:do_spawn_agent`）：

``````
🔨 Tool: `spawn_agent`  📥 args:
````text
{
  "task_name": "ccmods_tav_risk",
  ... 数百行 args JSON ...
}
````
`````
[Action] Spawning subagent: ccmods_tav_risk
[Status] Subagent /root/ccmods_tav_risk started (pid=11132).
`````
``````

因为 `verbose` 模式下工具输出会被 5 个反引号围起来，而 args 又自带一个 ````text 围栏，所以一次工具调用的输出里**同时存在 3 反引号、4 反引号、5 反引号三种围栏行**。

搬运函数 `findFenceSafeOverflowCount` 需要一个「围栏安全切点」，原实现是：

```ts
function isFenceSafeCut(lines: string[], overflowCount: number): boolean {
  if (overflowCount <= 0 || overflowCount >= lines.length) return false
  const head = lines.slice(0, overflowCount)
  const tail = lines.slice(overflowCount)
  return fenceOpenState(head) === 0 && fenceOpenState(tail) === 0
}
```

**要求已提交的头（head）和仍在流式的尾（tail）同时平衡。** 这在直觉上像是在防止「切点落在围栏中间」，但它把两件不同的事混为一谈了：

- 头必须平衡——这是真的必要，因为头会变成永久段。
- 尾也必须平衡——**这是个错误要求**。尾部本来就在流式输出中，一个 args JSON 正在逐行吐出来的时候，尾巴处于未闭合围栏里是**完全正常的状态**。

于是整个 args 输出期间，所有候选切点都因为「tail 不平衡」被否掉，函数一路 `overflow--` 到 0，返回 0 表示**本次不搬运**。

后果可以量化。用真实 transcript 重放（`temp/sessions/session_7d952b12e8d84cff97f3256824a21624.jsonl` 的 turn 2，882 个 16 字符流式分片）：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 实时文本峰值行数 | 59 行 | 42 行 |
| `ccmods_definition` 连续不可见分片数 | 60 | **0** |
| `ccmods_ecosystem` 连续不可见分片数 | 39 | **0** |
| `ccmods_usage_risk` 连续不可见分片数 | 17 | **0** |
| 三条 spawn 行首次进入 Static 的分片序号 | 154 / 207 / 240 | 91 / 165 / 223 |

实时文本堆到 59 行、而视口只画 12 行时，刚写入的 spawn 行立刻被它自己的 args 正文埋掉；同时它又因为闸门停摆而没能进 Static。**同一个 bug 在同一轮里能让任何一条 spawn 行消失**，只是第一条最终也能被搬进去，而越靠后的条目在「被埋」和「被搬」之间停留的时间越短、越容易正好被用户看到覆盖瞬间——第三个因此最扎眼。

## 4. 为什么「尾部平衡」这条要求可以去掉

原始注释担心的问题是真的：如果切点落在围栏中间，下一个 `**LLM Running (Turn N)**` 可能被 marked 当成代码块内容，加粗标记渲染成字面量。

但这个担心只由**头部**决定。围栏状态是二值的（0/1 切换），所以：

- 头部含**偶数**条围栏行 → 头部以闭合状态结束。
- 既然头部闭合，尾部必然从**围栏之外**开始——尾部开头那个闭合围栏不可能被重新读成开栏。
- 因此下一个 turn header 一定在闭合代码块之后，不会被吞。

反过来，如果头部以未闭合状态结束（奇数条围栏行），那确实是坏的，必须继续往下走候选切点。所以正确的判据是「只校验 head」。

修复后的实现：

```ts
let overflow = total - maxTailSafe
while (overflow > 0) {
  if (overflow < lines.length && fenceOpenState(lines.slice(0, overflow)) === 0) return overflow
  overflow -= 1
}
return 0
```

`overflow < lines.length` 保留了「不能把整段都提交掉」的边界。只有在极端情况下——文件里真的存在一条永不闭合的裸围栏行，且它恰好把剩余所有候选都卡住——才会返回 0 并停摆，这属于合理兜底。

## 5. 验证

**回归测试**（`frontends/ink-ui/src/streamCommit.test.ts`）：

1. 尾部 args 块仍在流式时，`findFenceSafeOverflowCount` 必须返回 > 0（不得停摆），且已提交头的围栏必须平衡；`commitStreamingAssistantMessages` 后 spawn 行必须已经出现在某个 `a-{id}-c*` 段里，live 文本必须比未提交前更短。
2. 尾部存在未闭合围栏时，必须能选出一个平衡的头部切点，而不是返回 0。

我把闸门临时改回旧逻辑（补上 `&& fenceOpenState(lines.slice(overflow)) === 0`）确认新回归测试确实变红（13 pass / 1 fail），恢复修复后全绿——证明测试是有承重作用的，而不是陪跑。

**全量测试：**

- `frontends/ink-ui`：`npm test` → **383 passed / 0 failed**
- 仓库根：`python -m pytest tests/ -q` → **1196 passed, 3 skipped**

## 6. 后续可考虑项

bridge 其实已经在推子代理名册（`agent_snapshot` / `agent_event`，见 `frontends/ink_bridge.py` 的 `_emit_agent_read_model`），Ink UI 也把数据收进了 `state.agents` / `state.agentEvents`，但**目前没有任何组件渲染它们**——子代理状态完全依赖主 agent 的流式文本行来表达。

这正是本次 bug 能造成用户可见困惑的深层原因：状态展示与「会被挤掉的流式文本」耦合。若给子代理加一个像 workflow 那样的稳定状态条（`workflowStatusBar.ts` 已有现成范式），状态就不再受实时视口行数限制，这类「一闪而过/被覆盖」的问题会从根上消失。
