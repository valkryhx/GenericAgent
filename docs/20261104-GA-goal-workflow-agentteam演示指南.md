# GA goal × workflow × agent-team 演示指南


```
1. /workflow  on
用workflow 看看最近的3个commit 然后总结

2. /team on
用agent team 分别写牛顿法和八皇后的python demo 到D:\git_codes\GenericAgent\bug\tests目录

3. 接入mcp skill等

4. /goal 模式

5. 图片 resume compact 多轮 这些都是基本
```


日期：2026-11-04
适用：`ga` / `ga ink`（React/Ink 终端，受支持入口）
定位：面向**现场演示**的操作脚本，不是测试报告。每个场景都按
「怎么输入 → 屏幕上应看到什么 → 如何判定成功 → 证据在哪」四段走，
照着念就能讲清楚"GA 的 goal 如何驱动多智能体把活干完并自动收尾"。

> 与测试类文档的分工：
>
> - 想复现、想追根因、想看断言清单 → 看 `20261030-GA-goal-workflow-agentteam协同设计与测试基线.md`
>   和 `20261030-GA-goal多智能体协同测试方法论.md`；
> - 想在一次会议上把事情演示明白 → 用本文。
>   本文只保留演示需要的**输入、屏幕现象、判定口径、证据路径**，底层机制给出坐标即可。

---

## 1. 30 秒讲解稿（开场白）

> "GA 里有一个会话级的 `goal`：你用一句话描述一个**可验证的终止条件**，
> 之后每一轮 agent 要停之前，Stop 门禁都会判断'这个条件满足了没有'，
> 没满足就继续推进，满足了自动清除并给一个 `✓ Goal met`。
> 难点不在 goal 本身，而在于——当任务被派给 **workflow 后台脚本**、**agent-team 的并行 teammate**
> 去干的时候，主会话不能在后台还在跑的时候就误判'完成了'。
> GA 的做法是：只要还有后台活在产出，就 **defer（推迟求值）**，本轮正常结束；
> 等后台结果回来，新回合再求值一次。所以你会看到先 defer、后台交付、最后一击达成。"

一句话记住三个词：**可验证条件 → 后台在飞就 defer → 交付后一次性判定**。

---

## 2. 环境准备（演示前 3 分钟）

### 2.1 从源码启动，别用本机旧安装

```bash
# 卸掉可能存在的旧 ga，避免跑到旧代码
python -m pip uninstall -y genericagent
# 源码目录可编辑安装（只需一次）
cd D:/git_codes/GenericAgent
python -m pip install -e .
# Ink UI 依赖（只需一次）
npm install --prefix frontends/ink/ui
# 启动
python -m ga_cli ink
```

启动后应看到 `Ready.`，无 `Traceback`。先做两项体检：

- `/status`：确认 frontend/bridge 正常、模型是期望模型；
- `/model`：默认 `default/deepseek-v4.1-flash`。

判定：能正常对话一轮、输入框可编辑、`/status` 无异常。

### 2.2 演示用目录

统一约定一个外部目录，演示前清空其中的子目录，避免上一次产物干扰判断。
本文示例用 `D:/demo20261104`，你可以换成任意**不在仓库里**的绝对路径
（这也是顺便演示"GA 能在任意目录读写"）。

```text
D:/demo20261104/
  gw/    <- 场景 A（goal + workflow）
  gt/    <- 场景 B（goal + agent-team）
  gwt/   <- 场景 C（三合一）
```

---

## 3. 协同原理（一张图讲清，演示时可只讲这一段）

```text
        用户一句话（可验证条件）
                  │
                  ▼
        /goal 设置 → 主 agent 开始做事
                  │
        ┌─────────┴──────────┐
        │  派活给后台执行体   │
        │  ├ workflow（JS 脚本，后台 run）
        │  └ agent-team（并行 teammate）
        └─────────┬──────────┘
                  │  主 agent 回合即将结束
                  ▼
         Stop 门禁 run_stop_gate()
                  │
      还有后台活在产出吗？
        ├─ 有（workflow 在飞 / 子 agent 在飞 / 刚启动过 workflow）
        │      → defer：本轮不求值，正常结束（界面 goal 被推迟）
        │
        └─ 没有 → 正常求值
                  │
      后台交付回来 → 新回合 → 求值
                  │
        ├─ 条件满足 → ✓ Goal met，自动清除
        └─ 未满足 → 继续推进（受迭代上限保护）
```

判定入口唯一真源（讲源码时可点开）：

- `genericagent/core/handler.py:465` — `run_stop_gate()`
- `genericagent/core/goal_evaluator.py` — `_IN_FLIGHT_WORKFLOW_STATUSES`（:174）、
  `has_in_flight_workflows`（:252）、`workflow_launched_this_turn`（:257）、
  `list_in_flight_task_labels`（:269）、`has_in_flight_agents`（:301）
- `frontends/ink/bridge.py` — `_workflow_in_flight_labels()`（:1926）、`workflow_launch_from_tool()`（:1269）
- `genericagent/hooks/runtime.py:562` — `goal_defer=True` 时确实跳过自动求值

> 一句话：goal 只挂在主会话上；workflow / teammate 是替主会话干活的执行体。
> 只要后台还在产出，门禁就不求值，等它交付后再一击判定。

---

## 4. 三个开关先弄清（演示现场最容易踩的坑）

| 能力                | 开关             | 默认           | 说明                                          |
| ------------------- | ---------------- | -------------- | --------------------------------------------- |
| 设置/查看/清除 goal | `/goal`        | **常开** | 直接输入 `/goal ...`，无需任何开关          |
| workflow 会话模式   | `/workflow on` | 关             | 会话内命令，让主 agent 编写并运行 JS workflow |
| agent-team 会话模式 | `/team on`     | 关             | 会话内命令，允许自然并行请求拉起命名 teammate |

要点：

- `/workflow on`、`/team on` 是**会话内**命令，不需要重启、不需要环境变量。
- **每个 slash 命令单独回车输入，不要一次粘贴多行**（多行粘贴会被当成同一轮）。
- 演示前确认底部没有残留的 goal；有就 `/goal clear`。

---

## 5. 场景 A：goal + workflow（后台脚本）

### 5.1 怎么输入

第一步，打开 workflow 会话模式（单独一行回车）：

```text
/workflow on
```

第二步，设 goal（单独一行回车）：

```text
/goal 用 workflow 在 D:/demo20261104/gw 下产出 report.md，内容包含三个并行子任务的结论（A/B/C），并确认文件存在
/goal 用workflow 看看最近的3个commit 然后总结
```

### 5.2 屏幕上应看到什么

1. 主 agent 会**编写并运行一个 JS workflow**（工具调用里出现 `workflow(script: ...)`），
   并返回类似 `[Workflow] launched <name> (wf_xxxx) in the background`；
2. 底部出现 **workflow 状态栏**：`› ◌ <name>  0/0 agents done`（可 `Enter` 进 `/workflows`）；
3. workflow 运行期间，goal **defer**：不会提前出现 `✓ Goal met`，
   日志里能看到 goal 被推迟的痕迹（`goal_status(reason='deferred')`）；
4. workflow 交付后，主 agent 基于真实产物判定，出现 `✓ Goal met`，卡片消失。

### 5.3 如何判定成功

- [ ] 有 `workflow launched ... in the background` 之类的启动回执；
- [ ] 运行期间**没有**提前 met（出现 defer 才对）；
- [ ] 结束后 `✓ Goal met`；
- [ ] `D:/demo20261104/gw/report.md` 存在且内容含 A、B、C。

### 5.4 现场可指的证据（自动化已跑过，8/8 PASS）

脚本：`tests/real_goal_workflow_coop_e2e.py`
证据：`temp/goal_wf_coop/summary_20261030-154951.json`

关键结果：

```text
launch_turn        : done=true, steps=4, secs=12.3
last_goal_defer    : true
goal_iterations    : 0        # defer 时根本不求值
workflow.status    : succeeded
wf_out.txt         : 写入目标目录，内容 WF_OK
goal after handoff : met -> cleared
```

---

## 6. 场景 B：goal + agent-team（后台 teammate）

### 6.1 怎么输入

先开 team 模式（单独一行）：

```text
/team on
```

再设 goal（单独一行）：

```text
/goal 在 D:/demo20261104/gt 下生成 api.md 和 db.md 两份设计，用两个 teammate 并行写，最后你汇总校验两份都存在且非空
```

### 6.2 屏幕上应看到什么

1. 主 agent 建任务板并 `spawn_agent(...)` 拉起两个 teammate（例如 `teammate1` / `teammate2`）；
2. 出现 `Subagent <name> started (pid=...)` 之类的启动回执；
3. 子 agent 在飞期间 goal **defer**（`goal_status(reason='deferred')`），不提前 met；
4. 子 agent 完成后主 agent 汇总校验，`✓ Goal met`，两文件都在且非空；
5. 敲 `/team` 可看到 roster 与 task board。

### 6.3 如何判定成功

- [ ] 出现两个 teammate 的启动回执；
- [ ] 运行期间出现 defer、没有提前 met；
- [ ] 结束后 `✓ Goal met`；
- [ ] `D:/demo20261104/gt/api.md`、`db.md` 都存在且非空。

### 6.4 证据（自动化已跑过，6/6 PASS）

脚本：`tests/real_goal_2a_defer_e2e.py`
证据：`temp/goal_2a/summary_20261030-155200.json`

关键结果：

```text
/goal 设置成功
spawn 回合正常结束
出现 goal_status(reason='deferred')
deferred 后 goal 仍 active
后台子 agent 写出 child_done.txt
子 agent 结束后 goal 正常求值 -> met
```

---

## 7. 场景 C：三合一（goal + workflow + agent-team 同时）

这是最能说明问题的场景：**同一个 goal，同时被 workflow 通道和 team 通道并行推进**。

### 7.1 怎么输入

先 `/workflow on`，再 `/team on`（各占一行），然后设 goal（单独一行）：

```text
/workflow on
/team on
/goal 在 D:/demo20261104/gwt 下：用 workflow 组织 A/B 两个子任务的产物 a.txt、b.txt，同时用两个 teammate 并行写 c.txt、d.txt；四份文件都非空后合并成 all.txt，最后确认五份文件都在且 all.txt 含 A、B、C、D 四个标记
```

### 7.2 屏幕上应看到什么（逐条对照讲）

1. 同一会话里**同时**出现 workflow 通道与 team 通道的活动：
   - workflow 状态栏 `› ◌ <name> ...`；
   - teammate 的 `started`/完成回执；
2. 两条通道都在飞期间 goal **defer**（这是关键：看到"没有提前 met"就是对的）；
3. 两通道 settle 后 goal 求值一次，`✓ Goal met`；
4. 打开 `/workflows` 或 `/team` 面板时，goal 卡折叠成**一行**摘要且不遮挡；关闭后恢复整卡。

### 7.3 如何判定成功

- [ ] workflow 和 team 两通道**同时活跃**（都在飞）；
- [ ] 双通道在飞期间 goal defer、不提前 met；
- [ ] 最终 `✓ Goal met`；
- [ ] `a.txt b.txt c.txt d.txt all.txt` 五份都在，`all.txt` 含 A、B、C、D；
- [ ] overlay 打开时 goal 折叠、关闭后恢复。

### 7.4 证据（自动化已跑过，13/13 PASS）

脚本：`tests/real_goal_workflow_team_combo_e2e.py`
证据：`temp/goal_combo/summary_20261030-161457.json`

关键结果：

```text
team_spawn_turn     : done=true, steps=6
team_last_goal_defer: true
wf_launch_turn      : done=true, steps=4
wf_last_goal_defer  : true
both_inflight       : true          # 双通道同时在飞
marker_wf / marker_team : 均正确落盘
goal_met            : true
```

---

## 8. UI 观察要点（画面怎么讲）

### 8.1 goal 卡片长什么样

普通状态底部是两行整卡：

```text
◎ Goal active · running 0s · 0 turns · 0 tokens
Goal: <你设的条件>
```

达成后出现 `✓ Goal met` 并清除卡片；判为不可能/放弃则 `✗ Goal failed`。

### 8.2 面板 / 补全打开时，goal 不会消失

- 打开 `/workflows` 或 `/team`（overlay）时，整卡折叠成**一行**摘要 `◎ Goal active · ...`，
  让位给面板，**不遮挡**面板内容；
- 关闭 overlay 后恢复两行整卡；
- 输入框敲 `/`（弹命令补全）时同样是一行摘要，删掉 `/` 恢复。

### 8.3 一个重要的澄清（现场一定会被问）

> "为什么不是三个全屏面板一起铺在屏幕上？"

因为那不是设计目标。真实规则是：

- **goal 卡片 + workflow 状态栏**（非 overlay 的次要带）可以**同屏**纵向堆叠；
- `/workflows`、`/team` 是 **overlay**，打开时**面板优先**，goal 折叠成一行；
- 三个子系统可以**同时活跃**（后台都在跑），但不要求三个全屏面板并排常驻。

一句话：**执行层可以三方并存，显示层 overlay 互斥是刻意的。**

### 8.4 UI 渲染证据（真实 Ink 抓帧，10/10 PASS）

脚本：`frontends/ink/ui/scripts/real_ink_goal_workflow_team_combo_e2e.ts`
证据：`temp/goal_combo_ui/data_1793348818528/frames.txt` + `summary.json`

10 个通过项覆盖：

```text
goal card paints (real /goal)
workflow status bar paints (real workflow)
goal card + workflow status bar coexist in one frame
/team overlay paints (Team/Task board)
team overlay hides the goal card (overlay owns bottom)
goal card returns after closing /team
/workflows overlay paints
workflows overlay hides the goal card
goal card returns after closing /workflows
no captured frame overflows terminal width
```

真实抓到的界面原文（可在 frames.txt 里 grep 到）：

```text
◎ Goal active · running 0s · 0 turns · 0 tokens
Enter · /workflows · x stop · › ◌ combo-ui-wf  0/0 agents done
```

---

## 9. 确定性证据：Stop 门禁受控验证（17/17 PASS）

如果台下有人问"defer 是不是碰巧没来得及求值"，用这个受控场景回答——
不靠模型运气，直接把两条通道都稳住在飞，检查门禁动作。

脚本：`tests/real_goal_workflow_team_gate_e2e.py`
证据：`temp/goal_gate/summary_20261030-161236.json`

关键结果：

```text
workflow 在飞
team 子 agent 在飞
both_inflight = true
gate 动作     = none
last_goal_defer = true
evaluator 调用数 0 -> 0     # defer 时根本不求值（这是硬证据）
两者 settle 后 evaluator 调用数 0 -> 1
marker 均正确落盘
```

> 讲解口径：defer 期间 evaluator **一次都没被调用**，所以不存在"误判"；
> 后台 settle 后求值恰好 **1 次**，一锤定音。

---

## 10. 一键自动化（演示前后都可跑，作为"不只是嘴说"的支撑）

### 10.1 纯执行层（无需图形界面，直接跑，几秒到几分钟）

```bash
cd D:/git_codes/GenericAgent

# goal + workflow（8 项）
python tests/real_goal_workflow_coop_e2e.py

# goal + agent-team（6 项）
python tests/real_goal_2a_defer_e2e.py

# workflow + team 双通道 Stop 门禁受控验证（17 项，确定性最强）
python tests/real_goal_workflow_team_gate_e2e.py

# 三通道纯模型驱动全链（13 项）
python tests/real_goal_workflow_team_combo_e2e.py
```

每个脚本跑完在 `temp/<场景>/summary_<时间>.json` 落盘，终端也会打印各项 PASS/FAIL。

### 10.2 真实 Ink UI 抓帧（真实 bridge + 真实模型）

```bash
cd D:/git_codes/GenericAgent/frontends/ink/ui
npx tsx scripts/real_ink_goal_workflow_team_combo_e2e.ts
```

期望末尾 `RESULT 10/10`。产物：
`temp/goal_combo_ui/data_<run>/frames.txt`（逐帧 ANSI）和 `summary.json`（10 项 checks）。

> 提示：这些脚本都要**真实 LLM（default/deepseek-v4.1-flash）+ 网络**，
> 且属于脚本式 e2e，`unittest discover` / `npm test` **不会**自动收集，需按上面命令显式运行。

### 10.3 证据一览表

| 场景                 | 脚本                                                                  | 结果  | 证据文件                                               |
| -------------------- | --------------------------------------------------------------------- | ----- | ------------------------------------------------------ |
| goal + workflow      | `tests/real_goal_workflow_coop_e2e.py`                              | 8/8   | `temp/goal_wf_coop/summary_20261030-154951.json`     |
| goal + agent-team    | `tests/real_goal_2a_defer_e2e.py`                                   | 6/6   | `temp/goal_2a/summary_20261030-155200.json`          |
| workflow + team 门禁 | `tests/real_goal_workflow_team_gate_e2e.py`                         | 17/17 | `temp/goal_gate/summary_20261030-161236.json`        |
| 三通道组合           | `tests/real_goal_workflow_team_combo_e2e.py`                        | 13/13 | `temp/goal_combo/summary_20261030-161457.json`       |
| 真实 UI 抓帧         | `frontends/ink/ui/scripts/real_ink_goal_workflow_team_combo_e2e.ts` | 10/10 | `temp/goal_combo_ui/data_1793348818528/summary.json` |

> 注：证据目录里的时间戳是当时运行留下的；重跑会生成新的 `summary_<时间>.json`，判定逻辑一致。

---

## 11. 常见坑与现场答疑

| 现象                                       | 原因                                       | 现场怎么说 / 怎么处理                                                                                                           |
| ------------------------------------------ | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| 敲了 `/goal ...` 没反应、`0 turns/<1s` | 老版本 slash 命令不回灌成模型回合          | 已在 `62c3409`/`403650a` 修复。演示前务必从**源码**启动，先 `pip uninstall -y genericagent` 再 `pip install -e .` |
| 出现 `defer`，看着像"没完成"             | **这是设计行为**，不是失败           | 后台还在飞就不求值；等 workflow/teammate 交付后新回合再求值。看到 defer 说明门禁在工作                                          |
| workflow 一直不启动                        | 忘了 `/workflow on`                      | 会话内先 `/workflow on`，再设 goal；两个命令各占一行                                                                          |
| team 拉不起写文件的子 agent                | 选错 `agent_type`（如 `explore` 只读） | 主 agent 若用只读角色会被 `team_role_capability_mismatch` 挡下；通用子代理（不带 agent_type）可写。这是能力门禁，不是 bug     |
| 多个 slash 命令一次粘进去只生效一条        | 多行粘贴被当成同一轮                       | **每个命令单独回车**，等生效再输入下一条                                                                                  |
| 面板打开后找不到 goal                      | overlay 打开时 goal 折叠成一行             | 一行摘要仍在底部；Esc 关面板恢复整卡。这是刻意设计                                                                              |
| 产物没落在指定目录                         | 路径不是绝对路径 / 写到了默认工作区        | goal 条件里写**绝对路径**（`D:/...`，正斜杠更稳）；本项目已支持主 agent 在任意目录读写                                  |
| 中文看着乱码                               | 控制台代码页问题 ≠ 文件坏                 | 用字节校验：`python -c "open(p,'rb').read().decode('utf-8')"` 不报错即合法                                                    |

### 现场最可能被问的两句

1. **"后台还在跑，goal 会不会误判完成？"**
   → 不会。Stop 门禁发现后台在飞就 defer，evaluator 一次都不调用（受控测试里 `0 -> 0`）。
2. **"三个面板为什么不一起显示？"**
   → 执行层三方并存没问题；显示层 overlay 互斥是刻意的，打开面板时 goal 折叠成一行让位，关掉恢复。

---

## 12. 演示前检查清单（照着打勾）

- [ ] 已 `pip uninstall -y genericagent` 并从源码 `pip install -e .`；
- [ ] `python -m ga_cli ink` 启动，看到 `Ready.`，`/status`、`/model` 正常（`default/deepseek-v4.1-flash`）；
- [ ] 演示目录 `D:/demo20261104/{gw,gt,gwt}` 已清空旧产物；
- [ ] 确认底部无残留 goal（有就 `/goal clear`）；
- [ ] 场景 A 前：`/workflow on`；
- [ ] 场景 B 前：`/team on`；
- [ ] 场景 C 前：`/workflow on` 再 `/team on`；
- [ ] 每个 slash 命令**单独一行**输入，等生效再下一条；
- [ ] 已提前跑过 §10 的自动化，手里有 `summary_*.json` 可随时投屏佐证。

---

## 13. 一页速查（演示时贴在手边）

```text
场景 A  goal + workflow
  /workflow on
  /goal 用 workflow 在 D:/demo20261104/gw 下产出 report.md，内容包含三个并行子任务的结论（A/B/C），并确认文件存在
  看：workflow 状态栏 + defer + 最终 ✓ Goal met + report.md 含 A/B/C

场景 B  goal + agent-team
  /team on
  /goal 在 D:/demo20261104/gt 下生成 api.md 和 db.md 两份设计，用两个 teammate 并行写，最后你汇总校验两份都存在且非空
  看：两个 teammate started + defer + 最终 ✓ Goal met + 两文件非空

场景 C  三合一
  /workflow on
  /team on
  /goal 在 D:/demo20261104/gwt 下：用 workflow 组织 A/B 两个子任务的产物 a.txt、b.txt，同时用两个 teammate 并行写 c.txt、d.txt；四份文件都非空后合并成 all.txt，最后确认五份文件都在且 all.txt 含 A、B、C、D 四个标记
  看：双通道同飞 + defer + 最终 ✓ Goal met + 五文件齐 + overlay 折叠/恢复

判定三信号：① Goal active 卡片在 → ② 后台在飞时 defer、不提前 met → ③ 交付后 ✓ Goal met
```
