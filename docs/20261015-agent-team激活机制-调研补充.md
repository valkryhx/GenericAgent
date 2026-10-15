# 调研补充：agent team 的「激活机制」——不依赖用户正规输入

> 触发：用户指出——GA 的真实 e2e 要求用户输入很正规的指令
> （先 task_create 再 spawn_agent(team_mode=true)），但真实用户不会这样提问；
> 应像 CC 那样「用户自然提问就能用起来 agent team」，或给个简短斜杠命令。
>
> 结论先行：**GA 其实已经能自触发**（§3 有实证）；但与 CC 相比仍有差距。

---

## 1. CC 源码事实（权威调研在证据仓库）

完整调研见 **`claudecode283_hack/docs/20261015-CC-agent-team激活机制调研.md`**（含逐条偏移坐标）。
摘要：

1. **会话级实验开关**：`isAgentSwarmsEnabled`（`m0283_0xc5c1f6e.js`）=
   `(--agent-teams 启动参数 或 CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1) && featureFlag("tengu_amber_flint")`。
   **默认关**。
2. **CC 没有团队斜杠命令**（全模块只有无关的 `/team-onboarding`）。
3. **不靠正规输入的真正机制 = 计划模式「批准后 nudge」**（`m0503_0xccfe7a3.js @78213`）：
   用户批准计划后，ExitPlanMode 的 tool_result 追加一句
   「If this plan can be broken down into multiple independent tasks, consider spawning named
   teammates with the Agent tool (pass a name) to parallelize the work.」→ 模型自主组队。
4. **两个模型可见入口**（`m0357_0xc63ba04.js` 的 `nOn` 映射，随开关挂载/剥离）：
   `ExitPlanMode` → `launchSwarm` + `teammateCount`（退出计划模式批量拉起 swarm）；
   `Agent` → `name` + `team_name` + `mode`（起单个 teammate）。
5. teammate 系统提示：「You are a teammate in this session's agent team.」
   「Always refer to active teammates by their NAME.」
6. roster 扁平：「Teammates cannot spawn other teammates — the team roster is flat.」

---

## 2. GA 现状

- **无会话级开关**（CC 有 `--agent-teams`/env）。
- **无计划模式 / 无 `launchSwarm`**（GA 没有 plan mode，这块不复刻）。
- **已有**：`spawn_agent(team_mode=true)` + 共享任务板（`task_create/task_update/...`）+ `/team` 名册面板。
- **lead hint 常驻**：`subagent_prompts.LEAD_TEAM_USAGE_HINT_ZH` 已告诉 lead「需要多智能体协作时先
  task_create 再 spawn_agent(team_mode=true)」——**但它没说「什么时候该组队」**。

---

## 3. 实证：自然输入能否自触发？（默认 `deepseek-flash`）

用 SOP 脚本的观测模式（`GA_TEAM_E2E_PROMPT` + `GA_TEAM_E2E_SOFT=1`），从 **Ink 输入框**输入一句**自然中文**
（**不含** task_create / spawn_agent / team_mode 等任何团队语法）：

> 帮我并行做三件互不相关的小统计，然后汇总成一份报告：1) 根目录 .py 数；2) tests/ 下 test_*.py 数；
> 3) AGENTS.md 里 `## ` 标题数。写进 `{ART_DIR}/report.md`。只读，不要 git commit。

**结果：GA 自主组队成功。**

- 自动建了共享任务板 `temp/teams/session-cb790b6a/tasks.json`（3 条）；
- 自动 spawn 了 **3 个 `team_mode=true` teammate**（`t1_rootpy` / `t2_tests` / `t3_agentsmd`），
  全部 `turn_status=completed`；
- 自动写出 `report.md`（8334 B），三项数字 **70 / 107 / 28 正确**（已独立复核）。

→ **结论：GA 的 agent team 已经能靠自然输入自触发**，不必让用户写正规语法。
（之前 e2e 写正规语法，只是为了让断言**确定性**，不是 GA 的真实能力上限。）

**但暴露一个真实差距**：任务板 3 条始终是 `pending` / `owner=null`——
teammate 是**按 spawn message 干活的**，没有走「从板上认领 → 完成置 completed」的协议。
即：**任务板存在，但不是协作的事实来源（source of truth）**。CC 的模型是「teammate 从板上认领」。

---

## 4. 差距与建议

| 维度 | CC | GA 现状 | 建议 |
| --- | --- | --- | --- |
| 会话级开关 | `--agent-teams` / env | 无 | 可选：`GA_AGENT_TEAMS=1` / `/team on`（低优先） |
| 场景化 nudge | 计划批准后「可拆就 spawn teammate」 | lead hint 常驻、**无场景触发** | **优先**：自然请求含「并行/多个独立子任务/N 个对象」等信号时注入 CC 同款 nudge |
| 任务板是否 source of truth | 是（teammate 认领/置完成） | **否**（teammate 只按 message 干活） | **优先**：nudge/spawn message 强化「认领+置完成」，或 spawn 时自动绑任务 |
| 批量拉起 | `ExitPlanMode(launchSwarm,teammateCount)` | 无 plan mode | 不复刻（GA 无 plan mode） |

**建议落点（按优先级）**：

1. **场景化 nudge（对齐 CC，最省用户心智）**：在提交给模型的 user 消息里，当检测到并行信号时追加一句
   「若可拆成多个独立任务，考虑先用 task_create 建板，再 spawn_agent(team_mode=true) 起 teammate 并行，
   并要求 teammate 从板上认领、完成后 task_update 置 completed」。
   落点：`frontends/ink_bridge.py` 的 submit 组装处（或 agent 侧 user-message 组装处）。
2. **任务板成为事实来源**：spawn message 模板里强制「先 task_list 认领、完成 task_update」；
   或在 `spawn_agent(team_mode=true)` 时自动把某个 task 的 owner 设为该 teammate。
3. （可选）`/team on` 或 `GA_AGENT_TEAMS=1`：显式会话开关，贴 CC 的 `--agent-teams`。

> 不复刻：plan mode / `launchSwarm`（GA 无 plan mode）；云端团队。

---

## 5. 验收

- 自然输入（无团队语法）跑一次：出现 `team_mode=true` teammate ≥2、任务板有对应任务，
  且**任务被认领并置 completed**（当前只有前者满足）。
- 用 `frontends/ink-ui/scripts/real_ink_team_swarm_e2e.ts` 的观测模式（`GA_TEAM_E2E_SOFT=1`）复跑，
  对比 `taskBoard.completed` 是否 >0。

---

## 6. 证据路径

- CC 调研（权威）：`claudecode283_hack/docs/20261015-CC-agent-team激活机制调研.md`
- 自然输入实证：`temp/team_natural.log`、`temp/team_e2e_<runid>/report.md`、
  `temp/teams/session-cb790b6a/tasks.json`
- 驱动脚本：`frontends/ink-ui/scripts/real_ink_team_swarm_e2e.ts`
  （支持 `GA_TEAM_E2E_PROMPT` / `GA_TEAM_E2E_SOFT`）

---

## 7. 实施设计（本轮落盘，已实现）

> 见 `docs/20261015-ga-agent-team-实施进度.md` **P11**。三条建议全部落地。

### 7.1 场景化 nudge（建议 1，对齐 CC）

- 新模块 `subagent_team_activation.py`（镜像 `workflow_activation.py` 的结构）：
  - 信号分级：显式团队词（`多智能体`/`agent team`/`teammate`/`组队`）→ `requested`；
    `并行 + (计数/枚举/独立/multi_item)` → `recommended`；仅弱词（`同时`）→ `none`（避免噪音）。
  - `team_activation_reminder()` 产出 `[TEAM ORCHESTRATION OPT-IN]` 包裹的 CC 同款措辞：
    "可拆成多个互不依赖子任务 → 先 `task_create` 建板、再 `spawn_agent(team_mode=true)`、
    要求 teammate 认领并置 completed"。
- 注入点：`frontends/ink_bridge.py::submit()`，只加在 **model-facing text**，transcript 保留原文。

### 7.2 任务板成为 source of truth（建议 2）

- `subagent_tasks.team_task_protocol()`：team_mode spawn 的 `[TEAM TASK PROTOCOL]` 前言。
- `ga.do_spawn_agent`：`team_mode=True` 时 `_bind_team_task()` **预绑定**一条可认领任务
  （优先 subject/description 命中 teammate 名，否则首个可认领，CC `qe` FIFO），并把它写进前言；
  无任务可绑时前言降级为"先 task_list 认领 / 无则 task_create"。
- **owner 统一为 agent 路径** `/root/<task_name>`：与 idle 认领（`agentmain._teammate_idle_hook`）、
  `/team` 面板（`_team_roster_payload` 用 `agentId` 归属任务）一致；`TEAMMATE_USAGE_HINT` 措辞已同步。

### 7.3 会话开关（建议 3，贴 CC `--agent-teams`）

- `/team on|off`（bridge 本地应答，`TeamActivationState`）+ `GA_AGENT_TEAMS=1`（进程级播种）。
- `inputController.ts` 把 `/team on|off` 路由为 `submit`；`/team`、`/team list`、`/agents` 仍是名册面板。

### 7.4 与 workflow 的优先级（避免双入口打架）

显式 workflow 请求（`/workflow <task>` 或"用 workflow"）**恒优先** → 注入 workflow opt-in；
其余并行/独立请求 → team nudge；`explicit_fanout`（如"并行调研"）→ 若未命中 team 强信号则回落 workflow opt-in。
（CC 本身无 workflow 自动触发，此优先级是 GA 两套机制共存下的收敛规则。）

### 7.5 验收

- 自然输入（无团队语法）：`team_mode=true` teammate ≥2 + 任务板任务**被认领并置 completed**。
- 观测模式复跑：`frontends/ink-ui/scripts/real_ink_team_swarm_e2e.ts`（`GA_TEAM_E2E_SOFT=1`）对比 `taskBoard.completed`。
