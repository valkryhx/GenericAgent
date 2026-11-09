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
| 后端单元（team / registry / subagent） | ✅ 544 + 23 + 44 例全绿（2 skipped）；修复后重跑仍 544 + 25 + 44 |
| `/team` 面板单测（TS） | ✅ 26 例全绿（原 17 + 新增 9） |
| Ink UI 全量单测 | ✅ 630/630 通过；`tsc --noEmit` 干净 |
| `/team` 版面 e2e（L2，无 LLM） | ✅ PASS（header / Task board / footer / 宽度） |
| 真实 LLM e2e（zhanlu-ds，L2+L3 混合） | ✅ PASS：3 teammate、3 任务全 completed、面板 3 行 `✓ completed`、report.md 落盘 |
| 发现的真缺陷 | 3 个，均已修：`turnStatus` 未透出（§4）、roster 不问活体权威（§5.1）、自释放不归还任务（§5.2） |
| 未修 / 待观察 | 1 个（§5.3 重名后缀，本次未触发；另见 §5.4 的两条非缺陷记录） |

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

## 7. 变更清单（本轮）

| 文件 | 类型 |
| --- | --- |
| `genericagent/agents/team.py` | 后端：roster 透出 `turnStatus` / `live` |
| `genericagent/agents/registry.py` | 后端：新增公开 `is_live()`（复用 `_row_is_live`，说不清算活） |
| `genericagent/core/app.py` | 后端：抽出 `_subagent_task_store()`；子进程自释放前归还未完任务 |
| `frontends/ink/bridge.py` | 后端：桥透传 `turnStatus` / `live` |
| `frontends/ink/ui/src/protocol.ts` | 契约：`TeamMember.turnStatus` / `live` |
| `frontends/ink/ui/src/teamPanel.ts` | UX：verdict 链使用回合结论、活体结论、关闭原因 |
| `frontends/ink/ui/src/teamPanelRoster.test.ts` | 测试：+9 例（turn 结论、活体 stale、legacy closed 回落） |
| `tests/test_subagent_team.py` | 测试：+5 例（roster 暴露 turnStatus / live） |
| `tests/test_child_bus_handdown_paths.py` | 测试：+1 例（自释放归还未完任务） |
| `tests/test_ink_bridge.py` | 测试：桥 payload 透传 `turnStatus` |
| `frontends/ink/ui/scripts/real_ink_team_swarm_e2e.ts` | 测试：模型断言 + 面板 verdict 断言 + 陈旧注释订正 |
| `temp/regression/team_registry_audit.py` | 取证脚本（temp 不入库） |
