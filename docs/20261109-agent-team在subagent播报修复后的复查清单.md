# Agent Team 在 subagent 播报修复后的复查（已执行）

> 创建：2026-11-08　执行：2026-11-09
> 本文档最初写成"明日待办清单"，写完当天就查完了。现在它是一份**发现记录 + 剩余项**：
> §1 的猜测被实测推翻，同时查出一个更严重的真缺陷并已修复。**剩余项见 §10。**

---

## 0. 先明确：这次改了什么、没改什么

| | 本次改动 | 数据源 |
| --- | --- | --- |
| subagent 播报（底部绿/红行） | ✅ 改了 | `agent_snapshot` → `AgentControl._record_from_state` → `_project_status()` |
| 输入框鼠标选区 | ✅ 改了 | `App.tsx` 帧行模型 |
| **team 面板（`/team` 打开的列表）** | ❌ **没改** | `team_roster` → `SubagentTeam.view()` → `SubagentRegistry.list_agents()` → `registry.json` |

两条链路的**状态字段不是同一个**：

```
subagent 播报：  status/sourceStatus/completionStatus  ← 运行时投影，每次快照重算
team 面板：      entry.status                          ← registry 持久化，生命周期写入
```

证据坐标：

- `genericagent/core/control/process.py:180` — `status = self._project_status(state)`
- `genericagent/core/control/process.py:231-265` — `_project_status()`，先问工作再问进程
- `genericagent/core/control/process.py:187` — `source_status=state.process_status`
- `genericagent/agents/registry.py:220` — 新建时写 `"status": "running"`
- `genericagent/agents/registry.py:309` — 关闭时写 `"status": "closed"` + `closed_status`
- `genericagent/agents/registry.py:329` — `mark_running` 写回 `"status": "running"`
- `genericagent/agents/team.py:67-78` — `view()` 原样透传 `entry.status`
- `frontends/ink/bridge.py:1479-1504` — roster payload 原样透传

**结论：两条链路不相交，本次改动不会破坏 team 面板；但也没有改善它。**

---

## 1. P0：team 面板可能把已完成成员画成"空闲"

### 现象

`frontends/ink/ui/src/teamPanel.ts:36-42`：

```ts
const MEMBER_GLYPH: Record<string, string> = {
  running: '●',
  idle: '○',
  closed: '·',
  failed: '✗',
  evicted: '×',
}
```

`teamMemberGlyph()` 对未知状态兜底返回 `'○'`（空心圈 = idle 的形状）。

registry 目前只会写 `running` / `closed`，所以**大概率不触发**。但如果任何路径让
`entry.status` 变成 `succeeded` / `completed` / `killed`，面板会画成 `○` —— 看起来像
"还没开始"，而实际是"已经完成"。这比显示错误更糟：用户会以为成员卡住了。

### 实际查证结果（2026-11-09）

**§1 的猜测被推翻，但查出了一个更严重的真缺陷。**

遍历全部 7 个 `registry.json`、11 条记录，实测分布：

```
status:          closed x11
closed_status:   shutdown x11
previous_status: completed x11
turn_status:     completed x11
process_status:  waiting_reply x8 / shutdown x2 / killed x1
```

- **没有**出现 `succeeded`/`completed` 落在 `status` 上的情况 → §1 的担忧不成立。
- registry 只会写 `running` / `closed` 两个 `status`（`registry.py:220/309/329`，
  且 `_write_registry_entry` 压根不写 `status`）。
- `previous_status` 存的是 **`turn_status`**（`manager.py:2263` 传
  `previous_status=previous.turn_status`），不是 status —— 所以那 11 条 `completed`
  是工作结论，不是状态词。

**真正的缺陷：11 条记录里没有一条 `running`。** 因为子代理一跑完就被
`manager.close_agent` → `registry.mark_closed` 标成 `closed`，而
`SubagentTeam.view()` 默认 `include_closed=False`，把 `closed` 全部过滤掉。

结果：**`/team` 面板对任何已结束的会话都显示 0 个成员**——而"打开面板看我的团队干得怎么样"
恰恰是用户唯一会打开它的时刻。

### 已修复（见 §11）

1. `team.py`：`members()` / `view()` 默认改为 `include_closed=True`（roster 是**报告**，
   不是工作队列；要问"谁还活着"应直接问 registry）。
2. `team.py`：`view()` 额外透传 `previousStatus` / `closedStatus`——否则 `closed` 行
   无法区分"完成"和"出错"。
3. `bridge.py` + `protocol.ts`：把这两个字段透传到 UI。
4. `teamPanel.ts`：新增 `teamMemberVerdict()`，用 `previousStatus` 作为结论，
   而不是显示生命周期词 `closed`；字形表改为按结论查表（`completed` → `✓`）。
5. `teamPanel.ts`：未知结论不再兜底成空心圈 `○`（那等于伪装成 idle），
   改为显式 `?`。

---

## 2. P1：team 面板的降级兜底是静默的　→ ✅ 已修

`teamMemberGlyph` / `teamTaskGlyph` 原来都是 `?? '○'` 兜底。未知状态不报错、不打日志，
还会被画成一个"看起来像 idle"的圈——**比显示错误更糟**：用户会以为成员卡住了。

**这与那两个 subagent 播报 bug 同源**：都是"某一层悄悄用了错的词表，上层看不到"。

已改为：未知结论返回显式 `'?'`（`UNKNOWN_MEMBER_GLYPH`），渲染层一眼能看出
"这里有个我没见过的值"。

`teamTaskGlyph` 保持 `'[ ]'` 兜底不动——任务板只有三个状态且由 `tasks.py:29`
的 `STATUSES` 约束，兜底成"未开始"是安全的。

---

## 3. P1：任务板 `completed` 与成员 `closed` 的语义是否一致

`genericagent/agents/tasks.py:29` — `STATUSES = ("pending", "in_progress", "completed")`

任务板有 `completed`，成员表没有（成员只有 `closed`）。所以要确认：

- 成员被关闭时，它名下 `in_progress` 的任务会不会被自动标 `completed`？
  `tasks.py:367` 有 `complete_owned_in_progress(owner)`，注释明确写了**不要**仅因为
  `turn_status == completed` 就调用它。确认这条约束在 team 路径上也没被违反。
- 反过来：任务标 `completed` 的成员，成员本身会不会仍是 `running` 没关闭、
  或已 `closed` 但任务还 `in_progress`（截断在中间）？

### 明天怎么查

1. 读 `tasks.py:367-385` 与调用点，确认谁在什么时候调 `complete_owned_in_progress`。
2. 造一次"任务做一半被 close"的场景，看任务板最终状态。
3. 判据：**不允许出现"成员已 closed 但名下任务仍 in_progress"**，否则用户会看到
   一个已经退出的成员挂着一个还在进行中的任务。

---

## 4. P2：三代名字后缀（`probe_fast` / `probe_fast_1`）在 team 面板里是否会造成困惑

用户实测日志里出现过"运行时因旧名占用自动加 `_1` 后缀"。team 面板按 `member.name`
显示，`view()` 透传 `entry.task_name`。

要确认的是：**同一个逻辑任务重跑后，面板里是两行（`probe_fast` + `probe_fast_1`），
还是应该合并成一行？** 如果两行，用户会以为有两个成员。

- 若 registry 里旧行是 `closed`，`view()` 默认 `include_closed=False`（`team.py:64`）
  应该会过滤掉 → 只剩一行。**这条需要实测确认**，不能靠读代码下结论。

---

## 5. P2：本次两个修复的共同教训，team 面板要不要复用

本次两次 bug 的共同形状：

> 一个状态字段在不同记录类型里含义不同，消费方按其中一种含义统一信任它。

修复做法（`frontends/ink/ui/src/agentNotice.ts`）：

1. 判定链只留一条，每级含义明确；
2. 进程状态词**显式列举**并排除（`PROCESS_LIFECYCLE_STATUSES`），而不是靠"不在成功词表里"兜底；
3. 结论只能前进到**最新**结论（`executionId` 去重 + 替换），不能让矛盾结论并存。

team 面板当前**没有** `sourceStatus` 混用问题（它压根不读那个字段），所以 (1)(2) 不需要
搬过去。但 **(3) 值得检查**：`team_roster` 是每次整体重发还是有增量？如果重发，
`teamPanel.ts:28-33` 的 `fromBridgeEvent` 是整体替换还是合并？确认不会出现同名成员两行。

---

## 6. 验收方式：不要重复今天踩的坑

今天写测试时踩了两个坑，明天做任何 UI 断言都要避开：

### 坑 1：读累积的 stdout 字符流

`stdout.chunks` 装着**每一帧**的历史输出。早先那行 `✗ failed` 和后来那行
`✓ completed` 都会出现，于是**正确的实现看起来像在重复**。我因此误判两轮，
一度以为修复没生效。

**正确做法**：用 `frontends/ink/ui/src/virtualScreen.ts` 的 `paintedFrame()` 回放原始流，
读**当前屏幕**。这与用户真正看到的一致。

### 坑 2：先 `stripAnsi` 再按 ESC 序列切帧

分隔符本身是转义序列，剥掉之后就再也切不开，"最后一帧"实际是整条流。

**正确做法**：在**原始流**上切分，再对切出来的那一片做 strip。

### 反向确认（AGENTS.md §8.5 强制）

新写的每条守卫都要做反向确认：把实现改回 bug 状态，测试必须重新变红。
今天 `subagentAnnouncementFlipFlop.test.ts` 就是靠这一步才发现最初版本是假通过的。

---

## 7. 原始执行顺序（保留作记录；1、2、5 已做，3、4 未做）

| # | 事项 | 状态 |
| --- | --- | --- |
| 1 | §1 查 registry 状态取值集合 | ✅ 已做，猜测被推翻，但查出 §1 的真缺陷 |
| 2 | §6 把 `paintedFrame` 作为 team 面板测试的标准读法 | ✅ 已用 |
| 3 | §3 任务板/成员语义一致性 | ⬜ **未做** |
| 4 | §4 重名后缀实测 | ⬜ **未做**（需真跑一次） |
| 5 | §2 / §5 兜底与去重 | ✅ §2 已修；§5 已核实无需改 |

---

## 8. 相关文件索引

| 文件 | 作用 |
| --- | --- |
| `frontends/ink/ui/src/teamPanel.ts` | 面板渲染、`teamMemberVerdict()`、字形表、`?` 兜底 |
| `frontends/ink/ui/src/teamPanel.test.ts` | 原有用例（已随签名改为对象参数） |
| `frontends/ink/ui/src/teamPanelRoster.test.ts` | **新增**：已完成成员仍列出 / 结论可见 / 未知不伪装 |
| `frontends/ink/bridge.py:1438-1504` | `_team_roster_payload()`，透传 `previousStatus` / `closedStatus` |
| `genericagent/agents/team.py` | `SubagentTeam.view()`（`include_closed` 默认 True） |
| `genericagent/agents/registry.py` | `registry.json` 读写、状态生命周期（只写 running/closed） |
| `genericagent/agents/tasks.py` | 任务板状态机（`STATUSES` 三元组） |
| `genericagent/core/control/process.py:231` | `_project_status()`（subagent 播报用的那条，**team 不走这里**） |
| `frontends/ink/ui/src/agentNotice.ts` | 同一原则的判定链，team 侧改动的参照 |
| `frontends/ink/ui/src/virtualScreen.ts` | UI 断言的正确读法（`paintedFrame`） |
| `tests/test_subagent_team.py` | **新增** `TeamRosterLivenessTest`：3 例 |

---

## 9. 一句话结论

原猜测（team 面板把完成画成 idle）**不成立**：registry 只写 `running`/`closed`。
但实测 11/11 条记录都是 `closed`，暴露出真缺陷——**已结束的会话里 `/team` 显示 0 个成员**。
已修复：roster 默认包含已关闭成员，并透传 `previousStatus`，面板显示 `✓ completed`
而不是 `closed`。

---

## 10. 剩余项（未做）

| # | 事项 | 为什么值得做 |
| --- | --- | --- |
| 1 | §3 任务板 / 成员语义一致性 | 可能出现"成员已 closed 但名下任务仍 in_progress"的错乱 |
| 2 | §4 重名后缀（`probe_fast` vs `probe_fast_1`）在面板里是否两行 | 只影响观感，但需要真跑一次才能确认 |
| 3 | team 面板运行时实测 | 以上全部是单元/桩测试，**没有真跑一次 `/team` 看渲染** |

第 3 项是最重要的缺口：今天所有验证都是代码级 + 桩测试级，没有一个真实的
`/team` 截图。下次跑 agent team 时应确认：

- 三个成员跑完后面板显示 **3 行**（不是 0 行）
- 每行是 `✓ name completed`，不是 `· name closed`
- 出错的那个是 `✗ name errored`

---

## 11. 本次修复的验收记录

**后端**（`tests/test_subagent_team.py` 新增 `TeamRosterLivenessTest`，3 例）

- 三个成员全部 `mark_closed` 后，`view()["memberCount"]` 仍为 3（修复前为 0）
- `previousStatus` / `closedStatus` 正确透传
- 鸭子类型的旧桩（没有新字段）不会让整个面板报错

**前端**（`teamPanelRoster.test.ts`，8 例）

- 已完成成员仍有独立行
- 结论来自 `previousStatus`，不是 `closed`
- `completed` → `✓`，`errored`/`interrupted` → `✗`，未知 → `?`
- 未跑就关闭的成员（`closed_status=worktree_error`）报 `failed`，不谎称 completed

**反向确认（3 个变异，全抓）**

| 变异 | 结果 |
| --- | --- |
| `view()` 改回 `include_closed=False` | 红（Python 2 例失败） |
| 面板渲染 `member.status` 而非 verdict | 红 |
| 未知结论兜底回 `'○'` | 红 |

**回归**

- Python `team or registry or subagent` 子集：590 passed / 2 skipped
- UI 全量：627 例，稳定通过除既有并发抖动外全部（已确认基线同样抖）
- `tsc --noEmit` 干净

**一处过程记录**：`terminalInput.test.ts` 被我误改出重复的 `end: false` 键
（`tsc` 报 TS1117），已 `git checkout` 还原。改文件时不要用正则批量替换同名键。
