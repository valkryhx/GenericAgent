# GA Agent Team 在 subagent 大改后的真实回归与 UX 验证（2026-11-09）

> 背景：2026-11-08 的 subagent 大改（实时 IPC、fold 点通知、opt-out 工具面、播报去重）
> 之后，Agent Team（`/team`、共享任务板、team_mode 子代理）**没有真跑过**。
> 本文记录：核实了哪些测试、跑了什么真实场景、发现并修了什么、还剩什么。
>
> 依据的方法论文档：`docs/20261015-GA-ink-真实LLM-e2e-自测SOP.md`（三层自测）、
> `docs/20261103-GA真实LLM测试策略-过程断言优先.md`、`docs/20261108-GA-subagent链路真实证据核验手册.md`。

---

## 0. 结论先行

| 项 | 状态 |
| --- | --- |
| 后端单元（team / registry / subagent / close / dispatch） | ✅ 目标 8 套件 183 例、`test_ga_subagent_tools` + `test_subagent_prompts` 56 例全绿 |
| `/team` 面板单测（TS） | ✅ 28 例全绿（verdict 链 + viewport） |
| Ink UI 全量单测 | ✅ 643/643 通过；`tsc --noEmit` 干净 |
| `/team` 版面 e2e（L2，无 LLM） | ✅ PASS（header / Task board / footer / 宽度） |
| 真实 LLM e2e（zhanlu-ds，L2+L3 混合） | ✅ 3 任务场景 PASS；15 任务规模场景见 §8（含 5 次真实运行与逐次根因） |
| 发现的真缺陷 | 6 个，均已修：`turnStatus` 未透出（§4）、roster 不问活体权威（§5.1）、自释放不归还任务（§5.2）、面板 16 行静默截断（§8.2）、关闭竞态把完成记成 interrupted（§8.7）、`/root` 非 lead 别名 + 工具异常掀翻 worker + `full_resp` 未绑定（§8.8） |
| 未修 / 待观察 | §5.3 重名后缀（未触发）；§5.4 两条非缺陷记录 |

---

## 1. 本次核实的环境

- 模型：`zhanlu-ds/moma/deepseek-latest-flash`（`_probe_models.ts` 的 `CURRENT`）。
  测试脚本现在会**断言**当前模型等于它（`GA_TEAM_E2E_EXPECT_MODEL` 可覆盖），
  避免"以为在测 zhanlu-ds，实际换了模型"。
- 入口：真实 `App`（伪 TTY）+ 真实 `bridge.py` + 真实 LLM，即 SOP 的 L2/L1 组合。
- 证据：`temp/team_e2e_<runId>/summary.json`、`temp/regression/team-swarm-e2e*.log`。

---

## 2. 测试现状核实（先跑，不先改）

```powershell
# 后端
python -m unittest discover -s tests -t . -p "test_*subagent*.py"   # 544 OK (skipped=2)
python -m unittest discover -s tests -t . -p "test_*team*.py"       # 23 OK
python -m unittest discover -s tests -t . -p "test_*registry*.py"   # 44 OK

# 面板单测
cd frontends/ink/ui; npx tsx --test src/teamPanel.test.ts src/teamPanelRoster.test.ts

# 版面 e2e（无 LLM，秒级）
npx tsx scripts/real_ink_team_panel_e2e.ts

# 真实 LLM 团队场景（约 4-10 分钟）
npx tsx scripts/real_ink_team_swarm_e2e.ts
```

第一次真实运行（修复前）就通过，证据：

```json
{
  "model": "zhanlu-ds/moma/deepseek-latest-flash",
  "teamRoster": { "memberCount": 3, "taskCount": 3,
    "members": [
      { "name": "t1_root_py_count",  "status": "closed", "previousStatus": "completed", "taskCount": 1 },
      { "name": "t2_tests_py_count", "status": "closed", "previousStatus": "completed", "taskCount": 1 },
      { "name": "t3_agents_md_h2",   "status": "closed", "previousStatus": "completed", "taskCount": 1 }
    ] },
  "evidence": { "taskBoard": { "count": 3, "completed": 3 },
                "report": { "exists": true, "size": 2932 } }
}
```

即：`/team` 对**已结束**的会话显示 3 行（前一天修的 `include_closed=True` 生效），
每行 verdict=`completed`，任务板 3/3 完成，lead 汇总报告落盘。

### 2.1 修复后的第二次真实运行（`runId=1794207060446`）

去掉 `turnStatus` 修复后重跑，一次通过；roster 现在同时带三种结论字段：

```json
{ "name": "t1_root_py", "status": "closed", "previousStatus": "completed",
  "closedStatus": "killed", "turnStatus": "completed", "taskCount": 1 }
```

注意 `t1` 的 `closedStatus=killed`（关闭时进程被强杀）但 `previousStatus/turnStatus=completed`：
面板必须读工作结论而不是关闭原因——这正是 §4 verdict 链的用途。

三次真实运行的共同证据：3 名 teammate、3 个唯一 task id、任务板 3/3 completed、
每个 teammate 的 durable transcript 里都有真实的 `send_message` 调用（每次 3 条）、
`report.md` 落盘。

### 2.2 第三次真实运行（`runId=1794207583857`，带过程断言）

新增 `send_message` 断言后一次通过：`sendMessageCalls` 分别为 3 / 3 / 3；
两行 `closedStatus=killed` 的成员（关闭时进程被强杀）依然判为 `completed`，
说明 verdict 读的是工作结论而不是关闭原因。

一处观测噪声（不影响判定）：lead 报告某个 teammate 首次 `task_update(owner=...)`
被拒 `not_task_owner`——它传了任务名简称而不是协议要求的 `/root/<name>`，
随后重试成功。协议文本本身没错，属于模型侧的一次格式滑动。

---

## 3. 真实数据审计（为什么要多看一步）

用 `temp/regression/team_registry_audit.py` 扫描本机全部真实 registry / 任务板：

```
registry rows: 80
status:          running 29 / closed 51
previous_status: None 29 / running 10 / completed 41
closed_status:   None 29 / released 10 / killed 5 / shutdown 36
running 行里 turn_status 已终结的: 24
任务行: 52；in_progress 且 owner 已 closed: 1
```

两类有价值的事实：

1. **24/80 行是"进程还在线、但本回合已经结束"**（`status=running`，
   `turn_status=completed`，`process_status=waiting_reply`）。背景 teammate 在两次
   回合之间保持存活是正常设计，`/team` 却只读 `status` → 显示 `● running`。
2. 51 条 closed 行里有 10 条 `previous_status=running` + `closed_status=released`，
   29 条没有 `previous_status`（旧数据）。只靠 `previousStatus` 判 verdict 会漏。

---

## 4. 已修缺陷：已完成但仍在线的成员被画成 `● running`

### 现象与根因

- `genericagent/agents/team.py::view()` 只透出 `status` / `previousStatus` / `closedStatus`，
  **没有 `turnStatus`**；
- `frontends/ink/ui/src/teamPanel.ts::teamMemberVerdict()` 对非 `closed` 行直接用 `status`；
- 于是"回合已完成、进程仍在等 followup"的成员和"刚创建、还没开始"的成员**长得一样**。
  这与 11-08 修掉的 `closed` 词表问题同源：**生命周期词冒充工作结论**。

### 修复（4 处，最小改动）

| 文件 | 改动 |
| --- | --- |
| `genericagent/agents/team.py` | `view()` 增加 `turnStatus`（`getattr(entry, "turn_status")`，鸭子类型安全） |
| `frontends/ink/bridge.py` | `_team_roster_payload()` 透传 `turnStatus` |
| `frontends/ink/ui/src/protocol.ts` | `TeamMember.turnStatus` |
| `frontends/ink/ui/src/teamPanel.ts` | verdict 链：非 closed 行若 `turnStatus` 是终态词 → 用它；closed 行在 `previousStatus` 缺失/非结论时回落到 `turnStatus` |

### TDD 与反向确认

- RED：Python `KeyError: 'turnStatus'`（2 例）；TS `actual 'running' != 'completed'`（2 例）。
- GREEN：Python 11 例、TS 20 例全绿。
- 反向确认：`git stash push -- genericagent/agents/team.py frontends/ink/ui/src/teamPanel.ts`
  → Python 2 errors、TS 2 failed；bridge 单测单独 stash `bridge.py` → 1 error；全部恢复后全绿。
- 真实 e2e 同时改为**用面板同一个 `teamMemberVerdict()` 判 verdict**，
  断言 `closed` 与 `running+turn completed` 两种形态都必须读成 `completed`。

---

## 5. 深入根因后追加修复的两个缺陷

初版本文把下面两项写成"未修/待观察"；顺着根因继续查，两项都能在**不改语义**的前提下修掉。

### 5.1 僵尸 `running` 行：读侧不问登记表的活体权威（已修）

**真实现场**：本机 29 条 `status=running` 的行，用 registry 自己的 `_row_is_live()`
逐条复核，**29/29 全部判定为死**（pid 已消失或进程身份不匹配）。也就是说：
登记表内部本来就有权威答案，`/team` 的读侧却只信生命周期词 `running`。

**根因**：`_reap_stale_agents()` 只在**派生子代理时**运行；一个会话不再派生，
死行永远留着。roster 读的是 `entry.status`，从不问活体探针。

**修复（只读投影，不改 registry 数据）**：

- `SubagentRegistry.is_live(agent_path)`：公开 `_row_is_live` 的判定，**"说不清"一律算活**
  （把活代理误判致死比反过来更糟）；
- `SubagentView.view()` 增加 `live`；bridge / `protocol.ts` 透传；
- 面板 verdict：非 closed 行在无回合结论时，`live === false` → `stale`（字形 `×`）；
- closed 行同样收口：`previous_status=running` + `closed_status=stale/released` 且无回合结论时，
  分别读成 `stale` / `released`，不再把 `running` 当结论。

**实测**：用真实会话（`session_c7e66…`）调用 `SubagentTeam.view()`，两行都返回
`live=False`，面板 verdict 为 `completed`（回合已完成优先于活体结论）。

### 5.2 任务挂在已关闭成员上：自释放路径不归还任务（已修）

**真实现场**：58 条任务里 1 条 `in_progress`，owner 的注册行已 `closed` +
`closed_status=released`，且 `closed_at` 有值——不是历史脏数据，是运行路径产生的。

**根因（两条终止路径，不变量不同）**：

| 终止路径 | 是否归还任务 |
| --- | --- |
| 父代理 `manager.close_agent()` | ✅ `reclaim_tasks_for()` |
| 子进程自己结束 → `app.py::_release_subagent_slot()` | ❌ 只关注册行 |

子代理空闲退出时走的是第二条，于是它名下的 `in_progress` 任务永久挂在一个死 owner 上。

**修复**：把子进程的任务板解析抽成 `_subagent_task_store(task_dir, state)`
（优先用 spawn 下发的 `GA_TASKS_ROOT/GA_TEAM_KEY/GA_PROJECT_ROOT/GA_DATA_DIR`，
legacy 布局才回落到路径推导），`_release_subagent_slot()` 在关行前调用
`reclaim_owner(agent_path)`。idle hook 复用同一个 helper，两处解析不再各写一份。

**TDD**：先写"自释放后任务应回到 pending 且 owner 为空"，在真实产品布局下变红
（`'/root/kid' is not None`）；修复后绿；stash `app.py` 重新变红。

### 5.3 重名后缀（`probe_fast` / `probe_fast_1`）

`view()` 现在默认包含 closed 行，同名重跑会在面板里留下两行（旧行 `✓ completed`，
新行 `● running`）。本次真实运行没有触发（名字唯一）。是否合并留给产品决定；
当前字形/verdict 至少能区分"这两个是两个生命周期"。

### 5.4 仍存在的行为（非缺陷，记录）

- `_reap_stale_agents` 只在派生时写回 registry，因此死行会一直留在文件里；
  面板现在读得对，但数据本身不会被清理（读侧主动写回注册表会在多写者下引入竞争，
  本次刻意不做）。
- `tasks.complete_owned_in_progress()` 仍没有自动调用者：回合完成不等于任务成功，
  这是 CC 对齐的语义，不是遗漏。

---

## 6. 复现命令（真实 LLM）

```powershell
Set-Location D:\git_codes\GenericAgent\frontends\ink\ui
$env:GA_TEAM_E2E_TIMEOUT_MS='900000'
npx.cmd tsx scripts\real_ink_team_swarm_e2e.ts
```

脚本默认断言：

- `model == zhanlu-ds/moma/deepseek-latest-flash`；
- 任务板 ≥ 2 个任务且全部 completed，每个 team_mode 子代理绑定唯一 task id；
- `/team` 的 `memberCount == members.length`，每个成员的 verdict 必须是 `completed`；
- 面板帧里每个 teammate 一行 `✓ ... completed`，且**不允许**出现未知结论 `?`；
- 每个 teammate 的 durable transcript 至少有一次 `send_message` 调用
  （过程断言：团队通信通道真的被用过，而不是"没人说话但报告看起来对"）；
- lead 的 `report.md` 真实落盘。

---

## 8. 规模场景：10 个简单 + 5 个中等任务的真实团队回归（2026-11-09）

前面几次真实运行都是 3 个任务，只能证明链路通。规模一上来，问题才露头：
任务板是否真的能被自驱动 teammate 逐条认领、`/team` 在执行**过程中**是否看得到进度、
面板装不装得下 15 条任务。

### 8.1 场景与脚本

新增 `frontends/ink/ui/scripts/real_ink_team_scale_e2e.ts`（真实 App + 真实 bridge + zhanlu-ds）：

- lead 用 `task_create` 建 15 个任务（S01–S10 简单、M01–M05 中等）；
- 起 3 个 teammate（`team_mode=true`，绑定前三个任务），其余任务由 idle hook 自动认领；
- 每个任务要求：产物写到 `temp/team_scale_<runId>/<subject前缀>.txt`，
  `task_update ... completed`，并 `send_message` 给 lead；
- **执行期间每 ~1.5s 发一次 `/team`**，记录 roster 快照（成员、任务状态、面板文本、真实 Ink 帧）。

断言分三层：

| 层 | 断言 |
| --- | --- |
| 后端 | 任务板恰好 15 条、id/subject 唯一、全部 completed 且有 owner；3 个 team_mode 子代理都 completed、都真的调过 `send_message`；lead 写出 `report.md` |
| 正确性 | 10 个简单任务的答案由测试**独立复算**（文件数 / 标题数），必须与产物里的 `count=` 完全一致；5 个中等任务摘要必须包含指定关键词 |
| 前端 | 采样期间至少 3 个快照；`completed` 单调不减；至少一个快照显示"正在干活"（`in_progress>0` 或 `0<completed<15`）；面板里出现 `Task board` 且带 `[~]`/`[x]`；成员行 verdict 不得是 `?`；行宽 < 终端宽度；截断必须可见 |

### 8.2 规模暴露的 UX 缺陷：面板 16 行硬截断且静默

`App.tsx` 原来把面板行 `slice(0, TEAM_PANEL_MAX_ROWS=16)`。15 个任务 + 3 个成员 + 表头/页脚
约 23 行，于是**任务板的后 6 条永远看不到，且界面没有任何"还有更多"的提示**——
用户会以为团队只做了 9 件事。

修复：`teamPanelViewport(lines, maxRows)`（`teamPanel.ts`）——保留表头、保留末尾键位提示、
中间插入 `… (+N more line(s); r refresh)`；App 改用它。新增 2 例单测，
并做反向确认：改回 `slice` 后"截断必须可见"立即变红。

### 8.3 第一次运行结果（`runId=1794210261197`）

- **任务板**：15/15 completed，id/subject 唯一，全部有 owner。
- **正确性**：10 个简单任务的答案与测试独立复算**全部一致**
  （S01=1、S02=176、S03=31、S04=7、S05=123、S06=130、S07=200、S08=8、S09=309、S10=28）；
  5 个中等摘要都包含指定关键词；`report.md` 3730 B；3 个 teammate 各有 9–12 次 `send_message`。
- **失败点**：w1 被 lead 以 `all 15 tasks completed` 关闭时仍处于回合中
  （`previous_turn_status=running`、`process_status=alive`），registry 记为 `interrupted`，
  测试"每个 teammate 回合 completed"断言失败。

**这条失败暴露的是真问题**：w1 在 15:55:25 已经产出最终 assistant 文本（transcript 有 assistant 事件），
但 `output.txt` 没有 `[ROUND END]`、没有 `turn_completed` 事件，state 仍是 `running/alive`；
lead 在 15:57:21 关闭它（116 秒后）。也就是**模型已给出最终答案，子进程却卡在收尾前**。
扫本机 95 个 task dir，同签名（有 output 但无 `[ROUND END]`、无 `turn_completed`）只出现 1 次，
不是系统性问题，但每次都会让 `/team` 把一个干完活的成员显示成 `✗ interrupted`。

### 8.4 第二次运行（`runId=1794212230279`）与收尾修复

同样 15 个任务、同模型，**一次通过**：

- 任务板 15/15 completed；10 个简单答案独立复算全部一致；5 个中等摘要含关键词；
  `report.md` 5026 B；3 个 teammate 全部 completed（`send_message` 6/9/18 次）。
- **执行期进度可见**：共 256 个 `/team` 采样，`completed` 序列从 0 单调升到 15；
  第一个部分进度样本是 `members=3, completed=1, inProgress=2, pending=12`；
  期间 `inProgress` 最高为 2；256 个采样中**真实 Ink 帧**都包含面板（`realPanelFrames=256`），
  面板行宽在终端宽度内，且未出现未知 verdict。
- 未复现 w1 的收尾卡顿。但为消除该类风险，worker 的完成信号做了收敛。

### 8.5 修复：不再把"显示队列的 done"当作唯一完成信号

`genericagent/core/app.py` 新增 `await_turn_done()`，替换 worker 里裸的
`dq.get(timeout=300)`：

- 正常收到 `done` → 原样返回；
- 若本轮**已有最终 assistant 文本**、agent 已停止运行、且队列空闲 ≥5s →
  用该文本完成收尾，并写 `turn_finalize_recovered` 事件；
- agent 仍在运行时**绝不**恢复；超时是**空闲超时**（每来一个流式 item 就重置），
  所以超过 5 分钟的长回合不会被误杀（与原先 `dq.get(timeout=300)` 的语义一致）。

依据是 w1 的证据：模型最终答案已经落盘（transcript assistant 事件），
卡住的是"完成信号"，不是"生成"。恢复只在可证明安全时发生（有最终文本 + 进程已停）。

新增 `tests/test_subagent_turn_finalize.py`（6 例）：正常 done、丢失 done 后恢复、
仍在运行时等待、无最终文本时超时、队列活动重置恢复窗口、长回合不被误杀。
反向确认：把恢复条件改成恒假 → 2 例红；恢复后全绿。
另保留 `GA_SUBAGENT_DEBUG=1` 门控日志（queue created / queue item keys /
assistant callback / round output written / finalize state write），后续复现可直接定位。

### 8.6 第三次运行（`runId=1794214021444`）：测试脚本自己的假失败

这一次任务是**全部完成**的：任务板 15/15 completed，10 个简单答案独立复算全部一致，
5 个中等摘要含关键词，产物全在；只有 `report.md` 缺失——因为脚本提前退出，把 bridge 杀了，lead 没来得及写。

提前退出的原因在**测试脚本**，不在产品：采样到约 412 s 时 `completed=5, inProgress=0, pending=10`，
脚本判定"卡住"（当时规则是连续 4 次没有变化就退出），可 lead 的 transcript 显示
16:53:57 还在派 `/root/w1` 领任务，worker 全部 `live=true`。**有活着的 worker 时，计数不动是派活间隔，不是卡死。**

修法：
- 停机判据改为「lead 空闲 **且** 没有活跃 worker **且** 计数连续 20 次不变」；
- 抽成纯函数 `frontends/ink/ui/src/teamSampling.ts::advanceSampling()`，配 5 例单测：
  计数变化重置、有活跃 worker 时永不退出、无活跃 worker + 20 次停、板满立停、lead 忙时不退出；
- 反向确认：把规则改回第三次的旧版（`stalled>=4`，不看 liveWorkers）→ "a live worker keeps the run going"
  立即变红；恢复后 5/5 绿。

### 8.7 第四次运行（`runId=1794214844108`）：暴露真缺陷——关闭竞态把干完活的成员记成失败

任务板 15/15 completed、答案全对、`report.md` 落盘，但 roster 里 w2/w3 是 `interrupted`，
面板把它们画成 `✗ failed`（只有 w1 是 `✓ completed`）。这不是模型问题，证据在子进程事件流里：

```
w2  17:14:09 stop_requested → 17:14:10 turn_finalize_recovered → turn_completed → 17:14:11 agent_closed
w3  17:14:11 stop_requested → 17:14:12 turn_finalize_recovered → turn_completed → 17:14:13 agent_closed
registry（关闭后）: status=closed, previous_status=running, closed_status=killed, turn_status=running
```

**根因**（`genericagent/agents/manager.py::_close_single_agent`）：

1. 关闭流程**先** `read_agent()` 取快照（此刻子进程还在回合中，`turn_status=running`）；
2. 然后写 `_stop`、等 `grace_s`（默认 2 s）——这 2 秒正是子进程 abort + 收尾的窗口
   （§8.5 的 `turn_finalize_recovered` 就是在这时触发的）；
3. 关闭逻辑却仍用**旧快照**算 `closed_turn_status`（`running` → `interrupted`），
   并把 `previous_status=running` 写进 registry，同时把子进程刚写的 `completed` 覆盖掉。

于是：**任务全部完成的成员，在 `/team` 里显示为失败。** 这是"生命周期词冒充工作结论"的第四个变种，
与 §4、§5.1 同源。

**修复**（同样只改读侧快照，不改进程控制）：

- `stop` + grace 结束后**重读** `state.json`，走一次 `_refresh_state()`（`[ROUND END]` 即视为完成）；
- 只有终态（`completed` / `errored`）才覆盖旧快照的 `turn_status` 与 `final_output_path`；
- 真正被中途杀掉的成员仍然是 `running` → `interrupted`，不会把失败洗成完成。

**TDD**：新增
`tests/test_subagent_manager.py::test_close_agent_keeps_a_turn_that_finished_during_the_stop_grace`
（模拟"读快照时还在跑、grace 期间写完 ROUND END"），RED 为 `'running' != 'completed'`；
stash 掉 `manager.py` 反向确认重新变红；修复后 `test_subagent_manager` / `test_agent_control*`
共 131 例全绿。


### 8.8 第五次运行（`runId=1794216552052`）：链路级缺陷三连

任务板 14/15，`#14 (M04)` 停在 `in_progress`；三个 teammate 中途各被 resume 过两次
（`output.txt → output1.txt → output2.txt`，`resume_source=sidechain_transcript`）。
这不是模型发挥不稳，而是一条**从工具调用一路炸到进程**的链路，证据在子进程 stderr：

```
do_send_message(target="/root")
  -> manager.send_message: _is_lead_target("/root") == False      # LEAD_TARGETS 不含 root
  -> _queue_message: primary_path = "/root/root"
  -> _reject_dead_mailbox: ValueError: no agent named `/root/root` exists
  -> loop.BaseHandler.dispatch: 未捕获，异常穿出工具层
  -> app._drain_agent_stream: next(gen) 抛出
  -> app.run except 分支: failed_resp = full_resp + ...           # full_resp 尚未绑定
  -> UnboundLocalError（把原始 ValueError 掩盖掉）-> worker 线程死亡
  -> lead 用 resume 重启 worker；该任务 owner 已被归还为 pending
  -> 重启后的 worker 写完产物却完不成 task_update，任务留在板上
```

三个修复，各带 TDD 与反向确认：

| # | 缺陷 | 修复 | 测试 |
| --- | --- | --- | --- |
| 1 | `/root` 是 lead 的真实 agent path，但不是 lead 别名 | `LEAD_TARGETS` 增加 `root`；同时把 `root` 加入 `RESERVED_AGENT_NAMES`（杜绝 `/root/root` 子代理名，消除歧义） | 别名用例 + "发给 `/root` 落进 parent inbox 且不创建 `/root/root` 目录" + 保留名用例 |
| 2 | 工具抛异常会把整个回合/worker 掀翻 | `BaseHandler.dispatch` 捕获工具异常 → `StepOutcome(status=error, tool_error=...)` + `next_prompt`，让模型看到失败原因并修正；只捕 `Exception`，`KeyboardInterrupt`/`SystemExit` 照常终止 | `ToolExceptionContainmentTest` 2 例（直接抛；yield 一半后抛），stash `loop.py` 反向确认 2 例变红 |
| 3 | 错误分支引用未绑定的 `full_resp`，`UnboundLocalError` 掩盖真实错误 | 流式 `try` 之前 `full_resp = ''` | `tests/test_app_run_error_path.py`：AST 守卫断言 "try 之前必须绑定 full_resp" 且 "except 仍读 full_resp"（改注释不影响判定），临时删掉初始化即变红 |

单测全绿：目标 8 个 Python 套件 **183 例**、`test_ga_subagent_tools` + `test_subagent_prompts` **56 例**。


### 8.9 第六次运行（`runId=1794220837477`）：三处修复后全绿

同场景、同模型（`zhanlu-ds/moma/deepseek-latest-flash`），`passed: true`：

| 维度 | 结果 |
| --- | --- |
| 任务板 | 15/15 `completed`，id/subject 唯一，全部有 owner |
| teammate | w1 / w2 / w3 全部 `turn=completed`，`send_message` 15 / 9 / 12 次 |
| 稳定性 | 三个 worker 各只有一份 `output.txt`——**没有一次 resume、没有 traceback**（第五次是 2 次 resume/人） |
| 关闭结论 | registry 三行 `previous_status=completed`；其中 w1 `closed_status=killed`（关闭时进程被强杀），面板仍读作 `✓ completed`，正是 §8.7 修复的目标 |
| 执行期进度 | 137 次 `/team` 采样、129 个有效进度样本；`completed` 0→15 单调递增；首个部分进度样本 `completed=1, inProgress=2, pending=12`；129 个采样都有真实 Ink 帧 |
| 正确性 | 10 个简单答案与独立复算完全一致（1/178/31/7/123/132/200/8/311/28，仓库新增测试后计数随之上移）；5 个中等摘要均含关键词；`report.md` 4312 B |

耗时约 7 分钟（第五次同规模因崩溃+重启耗时约 26 分钟）。三处修复都没有引入新的 `/team` 渲染问题：
面板 verdict 无 `?`，行宽在终端宽度内，15 条任务超长时仍有 `… (+N more line(s))` 提示。


## 9. 变更清单（本轮）

| 文件 | 类型 |
| --- | --- |
| `genericagent/agents/team.py` | 后端：roster 透出 `turnStatus` / `live` |
| `genericagent/agents/registry.py` | 后端：新增公开 `is_live()`（复用 `_row_is_live`，说不清算活） |
| `genericagent/core/app.py` | 后端：抽出 `_subagent_task_store()`；子进程自释放前归还未完任务；`await_turn_done()` 收尾恢复 + `GA_SUBAGENT_DEBUG` 日志 |
| `tests/test_subagent_turn_finalize.py` | 测试：收尾信号丢失时的恢复（5 例，含反向确认） |
| `frontends/ink/bridge.py` | 后端：桥透传 `turnStatus` / `live` |
| `frontends/ink/ui/src/protocol.ts` | 契约：`TeamMember.turnStatus` / `live` |
| `frontends/ink/ui/src/teamPanel.ts` | UX：verdict 链使用回合结论、活体结论、关闭原因；`teamPanelViewport()` |
| `frontends/ink/ui/src/App.tsx` | UX：面板改用 `teamPanelViewport()`，超长时显式提示隐藏行数 |
| `frontends/ink/ui/src/teamPanel.test.ts` | 测试：+2 例（viewport 截断可见 / 不截断原样） |
| `frontends/ink/ui/scripts/real_ink_team_scale_e2e.ts` | 测试：15 任务规模 e2e，执行期采样 `/team` |
| `genericagent/agents/manager.py` | 后端：关闭时重读子进程最终回合状态；`LEAD_TARGETS` 增加 `root` |
| `genericagent/agents/team.py` | 后端：`RESERVED_AGENT_NAMES` 增加 `root`，杜绝 `/root/root` 子代理名 |
| `genericagent/core/loop.py` | 后端：工具异常收敛为 `StepOutcome(status=error)`，不再掀翻回合/worker |
| `tests/test_subagent_manager.py` | 测试：+2 例（grace 内完成仍记 completed；`/root` 别名落 parent inbox） |
| `tests/test_subagent_team.py` | 测试：保留名用例纳入 `root` / `ROOT` |
| `tests/test_app_run_error_path.py` | 测试：AST 守卫（full_resp 绑定顺序 + except 前提）2 例 |
| `tests/test_agent_loop_parallel_tools.py` | 测试：+2 例（工具异常收敛，含 stash 反向确认） |
| `frontends/ink/ui/src/teamSampling.ts` | 测试基础设施：`/team` 采样停机规则（纯函数） |
| `frontends/ink/ui/src/teamSampling.test.ts` | 测试：+5 例（含第三次运行 bug 的反向确认） |
| `frontends/ink/ui/src/teamPanelRoster.test.ts` | 测试：+9 例（turn 结论、活体 stale、legacy closed 回落） |
| `tests/test_subagent_team.py` | 测试：+5 例（roster 暴露 turnStatus / live） |
| `tests/test_child_bus_handdown_paths.py` | 测试：+1 例（自释放归还未完任务） |
| `tests/test_ink_bridge.py` | 测试：桥 payload 透传 `turnStatus` |
| `frontends/ink/ui/scripts/real_ink_team_swarm_e2e.ts` | 测试：模型断言 + 面板 verdict 断言 + 陈旧注释订正 |
| `temp/regression/team_registry_audit.py` | 取证脚本（temp 不入库） |
