# GA Ink/subagent 四轮修复总览（2026-11-08）

> 今日共四轮修复，从"输入框不能选区"一路查到"team 面板显示 0 个成员"。
> 本文是**入口文档**：每轮改了什么、发现怎么来的、遗留什么。
> 细节在各自专项文档与代码注释里，这里只给结论和坐标。
>
> 状态：前三轮已推送；第四轮已推送（`859f1f1`）。
> **遗留项见 §6，其中 §6.1 是唯一需要你真跑一次才能收口的。**

---

## 1. 四轮修复一览

| # | 提交 | 现象 | 根因（一句话） |
| --- | --- | --- | --- |
| 1 | `259961b` | 输入框里无法拖拽选中/复制 | 选区模型只有 transcript 视口坐标，输入框在模型里**没有地址** |
| 2 | `d879bbd` | 子代理都跑完了，UI 全播报 failed | UI 把 `sourceStatus`（进程状态）当成工作结论，而它永不含 `completed` |
| 3 | `e829872` | 同一个子代理先 failed 后 completed，两行并存 | 去重 key 含 `word`，结论一变就被当成"另一个子代理" |
| 4 | `859f1f1` | 已结束会话里 `/team` 面板 0 个成员 | roster 默认过滤掉 `closed`，而跑完的子代理全是 `closed` |

四轮有一个共同形状：

> **某一层用错了词表或坐标系，上层看不到，于是安静地给出错误答案。**

这不是巧合——它们都是"消费方按自己的假设重新推导，而不是问唯一权威来源"。

---

## 2. 第一轮：输入框选区（`259961b`）

### 根因

`transcriptSelection.ts` 的坐标系是 transcript 视口相对坐标；`App.tsx` 直接把鼠标
y/x 减 `headerRows` 和左侧 gutter 换算进去。落在输入框里的点全被钳到最后一行
transcript —— 要么选不中，要么选中错的行。

### CC 的做法（319 `m0701`）

`handleSelectionStart(n, u)` / `handleSelectionDrag(n, u)` 接收**屏幕绝对坐标**，
取字直接从 `this.frontFrame.screen`（整帧渲染结果）读。所以它天然能跨
"transcript → 输入框"。

### 修复

换成整帧行模型（`frameSelection.ts` + `framePlan.ts`）：

- 每行带自己的 `leftGutter` 和 `contentColumns`（两个区域换行宽度不同）
- **composer 的行号不重算**，取 `inputCursorPosition` 同一份锚点 —— 那是 caret 每帧
  摆放的值，也是用户肉眼验过的几何量。自己推一遍会让模块"按自己的算式正确"，
  哪怕画出来已经不一致（本仓库踩过的假通过形状）。
- 切分用一次 `partitionByDisplayColumns`，不用三次重叠切片（后者让 CJK 字符跨边界
  重复、行变宽、truncate 点滑动 —— 就是"拖拽时文字左右抖"）

### 验收

- 39 例新测试，先跑红
- **变异测试 9/9 全抓**
- **真机 e2e**：真跑 Ink，在渲染出来的 composer 行上发 SGR 按下/拖拽/松开，
  断言 OSC 52 载荷含输入框文字 + 一次性 token。改回原 bug → `payloads: []`

> 坑：这个 e2e 必须 `debug: false`。debug 模式 Ink 顺序打印整帧不做光标寻址，
> 没有屏幕可还原。为此新增 `virtualScreen.ts`（CC `frontFrame.screen` 的等价物）。

---

## 3. 第二轮：子代理全部播报 failed（`d879bbd`）

### 根因

UI 把 `sourceStatus` 排在判定链第一位当"工作结论"。但它含义随记录类型不同：

| | process 记录 | workflow 记录 |
| --- | --- | --- |
| `source_status` | `state.process_status`（`process.py:187`） | `run.status`（`workflow.py:263`） |
| 取值 | `waiting_reply`/`exited`/`shutdown`/`killed`/`alive` | 真正的工作结论 |
| 含 `completed` 吗 | **永远不含** | 含 |

真实抓包（`temp/real-subagent-fold-e2e/*`，四份一致）里已完成子代理是：

```
status       = 'succeeded'        ← 桥的投影早就答对了
sourceStatus = 'waiting_reply'    ← 进程在等下一条消息
turnStatus   = 'completed'
metadata.completionStatus = None
```

`workStatus` 取到 `waiting_reply`，不在成功词表 → 判 failed。

**这是同一个 bug 的第二次上报。** 第一次（`800c606`）修的是 `killed` 抢先那一种
实例，没修规则，所以换个进程状态就复发。

### 修复

判定链收敛成一条：`metadata.completionStatus → turnStatus → sourceStatus（仅当不是
进程生命周期词）→ status`。`PROCESS_LIFECYCLE_STATUSES` 这行是**承重的**，去掉它
`waiting_reply` 立刻又成结论。

顺带删掉冗余的 `FAILED_STATUSES`（成功词表已覆盖其余全部，第二张表不可达）。

### 验收

- 13 例，修复前 3 红
- **变异 5/5 全抓**
- **真实记录重放**：修复后 `[OK] completed`；改回旧顺序 `[FAIL] failed`

---

## 4. 第三轮：同一子代理两行并存（`e829872`）

### 根因（两层）

1. 去重 key 是 `${executionId}:${word}` —— 结论一变就被当成"另一个新子代理"。
   而状态本来就会演进：`running` → `closed`（进程回收但工作无定论）→ `succeeded`。
2. 早先那条错的不会退场。两条互相矛盾的结论并存，比任何一条单独存在都糟。

### 修复

按 `executionId` 去重，Map 保存**最新**结论：新结论替换旧的，不追加。

选"最新胜出"而非"第一次终态胜出"，因为第一次终态恰恰最不可靠——它产生于回合自己的
工作结论落地之前。

### 验收

- 4 例，修复前 2 红
- 反向确认：把旧 key 放回去 → 那 2 条重新变红

> 坑（让我误判两轮）：测试最初读 `stdout.chunks` 累积字符流。那里装着**每一帧**的
> 历史输出，早先的 `failed` 和后来的 `completed` 都在，于是**正确的实现看起来像重复**。
> 更糟的是先 `stripAnsi` 再按 ESC 切帧——分隔符本身被剥掉了，"最后一帧"其实是整条流。
> 最终改用 `virtualScreen.paintedFrame()` 回放原始流读当前屏幕。

---

## 5. 第四轮：team 面板 0 个成员（`859f1f1`）

### 发现怎么来的

你问"team 要不要也复查"。我先扫了全部真实 `registry.json` 再下结论，而不是读代码猜。
测绘结果（7 个文件、11 条记录）：

```
status:          closed x11
closed_status:   shutdown x11
previous_status: completed x11
turn_status:     completed x11
process_status:  waiting_reply x8 / shutdown x2 / killed x1
```

**11 条里没有一条 `running`。**

### 两个结论

**（a）我原先的担心不成立。** registry 只写 `running`/`closed` 两个 `status`
（`registry.py:220/309/329`；`_write_registry_entry` 压根不写 `status`），所以
"完成被画成 idle"没有发生。`previous_status` 存的是 `turn_status`
（`manager.py:2263`），不是 status。

**（b）查出一个更严重的真缺陷。** 子代理一跑完就被 `manager.close_agent` →
`registry.mark_closed` 标成 `closed`，而 `SubagentTeam.view()` 默认
`include_closed=False` 把 `closed` 全过滤掉。于是**任何已结束的会话里 `/team` 都显示
0 个成员**——而"打开面板看我团队干得怎么样"恰恰是唯一会打开它的时刻。

### 修复

- `team.py`：`members()` / `view()` 默认 `include_closed=True`。roster 是**报告**，
  不是工作队列；要问"谁还活着"应直接问 registry 并显式传 `False`。
- `team.py`：额外透传 `previousStatus` / `closedStatus`。否则 `closed` 行无法区分
  "完成"和"出错"——两者 status 都是 `closed`。
- `bridge.py` + `protocol.ts`：两字段透传到 UI。
- `teamPanel.ts`：新增 `teamMemberVerdict()`，结论取自 `previousStatus`；
  字形表按结论查表（`completed` → `✓`，`errored`/`interrupted` → `✗`）。
- `teamPanel.ts`：未知结论不再兜底成空心圈 `○`（等于伪装成 idle），改为显式 `?`。
- `team.py`：新字段用 `getattr` 读取，鸭子类型桩/旧 registry 不会让整个面板报错。

### 验收

- 后端 `tests/test_subagent_team.py` 新增 `TeamRosterLivenessTest`（3 例）
- 前端 `teamPanelRoster.test.ts`（8 例，先跑红）
- **反向确认 3 个变异全抓**：`include_closed=False` → Python 2 例红；
  渲染 `member.status` → 红；未知兜底回 `○` → 红
- Python `team/registry/subagent` 子集 590 passed / 2 skipped

---

## 6. 遗留问题

### 6.1 【必做】team 面板从未真跑一次

**这是今天唯一的实质缺口。** 四轮修复的验证全部止步于代码级 + 桩测试级，
**没有一张真实的 `/team` 截图**。

下次跑 agent team 时请确认：

- 三个成员跑完后，面板显示 **3 行**（不是 0 行）
- 每行是 `✓ name completed`，不是 `· name closed`
- 出错的那个是 `✗ name errored`，不是 `?` 也不是 `○`
- 有没有出现 `probe_fast` 与 `probe_fast_1` 两行同名（见 6.3）

若任一条不符，把截图和 `temp/ink_bridge_backend.log` 给我。

### 6.2 任务板与成员状态可能不一致（未验证）

成员表只有 `closed`，任务板有 `completed`。可能出现：
**成员已 `closed`，但名下任务仍 `in_progress`**（子代理被中断在中间）。

**已核实的一点**：`SubagentTaskStore.complete_owned_in_progress()`（`tasks.py:367`）
**没有任何生产调用点**，只有测试在调。这不是缺陷——`tasks.py:368-374` 的 docstring
明确写了设计意图：CC 依赖 teammate 自己调 TaskUpdate，GA 的 idle hook **不得**仅因
`turn_status == completed` 就自动收口（一个被阻塞的 teammate 可能没产出有效结果就结束
了回合）。这是 `docs/20261019-GA-workflow-contract与agent-team防重复spawn实施方案.md`
§143 当时的决定。

所以遗留的是**另一个问题**：如果子代理被中断，它名下的 `in_progress` 任务**没有
任何机制回收**，会永久挂着。要不要处理（超时回收？关闭时降级为 pending？）需要你定。

验证方法：造一次"任务做一半被 close"，看面板任务板最终状态。

### 6.3 重名后缀在面板里是否两行（未验证）

真实日志里出现过"运行时因旧名占用自动加 `_1` 后缀"（`probe_fast` / `probe_fast_1`）。

现在 `view()` 包含 `closed` 行了，所以**旧行也会显示**。理论上旧行 `closed`、新行
`running`，会看到两行同名。要确认这是否可接受，还是应该合并/标记为同一个逻辑成员。

这一条与 6.1 一并观察即可。

### 6.4 UI 测试存在既有并发抖动

`npm test` 全量跑时，每 1-2 次会有 1-2 条失败，**失败项每次不同**：

- `App keeps workflow panel keys out of the composer`
- `App shows a Codex-style completion summary only after each turn ends`
- `Enter while running queues the draft instead of discarding it`
- `App wheel scrolling pages the transcript, not the composer history`

已做基线对照：**完全 stash 掉今日全部改动后同样抖**（基线 run 1 挂了另一条）。
单独跑这些文件多数能过。`workflowList.app.test.ts` 自 `42a1b1f`（结构性移动）后
未再改过。

结论：既有问题，与本次改动无关。但值得单独排一次——抖动会训练人忽略红色。
怀疑方向：这些测试都真跑 Ink 并等 `waitUntil` 超时，可能是共享计时器/负载下的时序竞争。

### 6.5 team 面板尚未做"未知状态"的运行时观测

`?` 兜底已经加上，但没有真实数据证明什么情况下会触发。如果 6.1 实测中看到 `?`，
说明 registry 出现了 `VERDICT_GLYPH` 之外的词，那需要补表——这本身是个有用的信号，
不是错误。

---

## 7. 今天踩过、值得记住的三个坑

### 坑 1：读累积 stdout 字符流做 UI 断言

`stdout.chunks` 装着**每一帧**的历史输出，会让正确的实现看起来像重复。
**正确做法**：`virtualScreen.paintedFrame()` 回放原始流读当前屏幕。

### 坑 2：先 `stripAnsi` 再按 ESC 序列切帧

分隔符本身是转义序列，剥掉就切不开。
**正确做法**：在**原始流**上切分，再对切出的那片 strip。

### 坑 3：用正则批量改同名键

我用正则改 `terminalInput.test.ts` 时写出重复的 `end: false`，`tsc` 报 TS1117。
**正确做法**：改多个同名键用 AST 或逐处 Edit，不要正则批量替换。

三个坑的共同点：**测试本身在骗人**时，比没有测试更危险——它会给你"已修好"的错觉。
今天靠变异测试 + 真实记录重放各抓到过一次假通过。

---

## 8. 相关文档与文件

| 文件 | 内容 |
| --- | --- |
| `docs/20261109-agent-team在subagent播报修复后的复查清单.md` | 第四轮的详细发现记录（含被推翻的原假设） |
| `frontends/ink/ui/src/frameSelection.ts` / `framePlan.ts` | 整帧选区模型（第一轮） |
| `frontends/ink/ui/src/virtualScreen.ts` | 终端屏幕回放，UI 断言与 e2e 的正确读法 |
| `frontends/ink/ui/src/agentNotice.ts` | 子代理播报判定链（第二、三轮） |
| `frontends/ink/ui/src/teamPanel.ts` | team 面板、`teamMemberVerdict()`（第四轮） |
| `genericagent/agents/team.py` | `SubagentTeam.view()`（`include_closed` 默认 True） |
| `genericagent/agents/tasks.py:367` | `complete_owned_in_progress` —— 有定义、无生产调用点（见 6.2） |
| `tests/test_subagent_team.py` | `TeamRosterLivenessTest` |
| `frontends/ink/ui/src/teamPanelRoster.test.ts` | team 面板结论渲染测试 |
| `frontends/ink/ui/scripts/mutation_check_*.py` | 两个变异测试脚本 |
| `frontends/ink/ui/scripts/replay_real_subagent_records.py` | 真实抓包重放验证 |
| `frontends/ink/ui/scripts/real_ink_composer_selection_e2e.ts` | 输入框选区真机 e2e |

---

## 9. 明天的建议顺序

| # | 事项 | 成本 |
| --- | --- | --- |
| 1 | 跑一次 agent team（3 个探针），开 `/team` 看面板 | 约 2 分钟 |
| 2 | 按 §6.1 逐条核对，顺带看 6.3 重名 | 边看边记 |
| 3 | 若面板不对，把截图 + `ink_bridge_backend.log` 给我 | — |
| 4 | 决定 6.2 的孤儿 `in_progress` 任务要不要回收 | 需要你定策略 |
| 5 | （可选）排一次 6.4 的 UI 测试抖动 | 半天起 |

第 1 步做完，今天四轮修复就能真正收口——现在的验证都在桩上，差这最后一步。
