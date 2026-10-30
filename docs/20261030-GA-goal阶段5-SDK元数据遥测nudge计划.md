# GA goal 阶段 5 实施计划（SDK 元数据 / 遥测 / nudge）—— 计划纪要，暂不实施

> 状态：**已实施（S0–S4）**，落地记录见 `docs/20261030-GA-goal阶段5实施记录.md`。
> 目的：把 goal 阶段的"对外可观测性 + 轻量引导"补齐，并给出 CC 303 的对照做法与「为什么它不打扰用户」的分析，
> 供后续单独立项执行。
> 关联：
> - 设计稿 `docs/20261028-GA-goal模式设计与实施方案.md`（§10.2 元数据 / §10.4 遥测）
> - 进度与决策 `docs/20261030-GA-goal落地进度与阶段2-3决策.md`
> - CC 调研 `D:/git_codes/claudecode283_hack/docs/20261028-CC-goal模式实现调研.md`（§9 / §10 / §11.1）
> - 源码坐标（下称 mXXXX 均指 `reconstructed303/deminified/`，只读）

---

## 1. 这个阶段的作用（一句话）

**把"goal 正在起作用"这件事说清楚、记下来、并极轻地提示一次，但不改变 goal 的判定逻辑。**
分三块，彼此独立、都可单独开关：

1. **SDK 元数据**：把当前 goal 状态做成**结构化字段**对外暴露（哪些字段、什么时候推送、清除/达成怎么表示），
   让前端能画卡片、外部集成能读状态，而不用解析会话 jsonl。
2. **遥测**：记录 goal 生命周期事件（设置/达成/失败/清除/被覆盖、来源、耗时、迭代数、tokens），
   纯观测、**默认本地写、不发网络**，用于回答"goal 到底有没有用、卡在哪"。
3. **nudge**：在**被动**场景（会话建议位/状态位）给**一行**提示，例如"可以用 /goal 设一个目标"。
   关键是**不主动弹窗、不强打断**，且有冷却与相关性门。

为什么它算"轻"：2-B/3 都要先有"无用户消息唤醒调度器"这个重前提；阶段 5 完全不依赖它，纯增量、可回退。

## 2. CC 303 是怎么做的（源码取证）

### 2.1 结论：CC 的 goal 事务**全程不弹窗、不主动打扰**

CC 把 goal 的所有"存在感"都放在**被动展示**与**静默记录**上：

- 状态变化 → 走 `notifyMetadataChanged({goal})`，由 UI **被动**渲染成卡片/徽标，不打断输入；
- 达成/失败 → 写 `goal_status` 附件，前端渲染成**徽标**；
- 遥测 → 静默上报，用户无感；
- 唯一的"引导"是一个**符合条件才出现、且冷却 3 个会话**的**静态建议条**（不是弹窗）。

### 2.2 SDK 元数据（`m0696:378-400` + `m1809:22016`）

`Mqr(activeGoal)` 只取五个字段做浅比较与推送：

```js
var R = ["condition","setAt","iterations","tokensAtStart","lastReason"];
function Mqr(e){ return { condition:e.condition, iterations:e.iterations,
  set_at:e.setAt, tokens_at_start:e.tokensAtStart,
  ...(e.lastReason!==void 0 && { last_reason:e.lastReason }) }; }
```

状态 diff 时（`m0696:292-308`）：activeGoal 变化 → `notifyActiveGoalChanged`，有目标就
`notifyMetadataChanged({goal:{...meta,last_reason,met:false}})`，无目标就 `notifyMetadataChanged({goal:null})`。
远程/SDK 侧（`m1809:22016`）把同一份 `Mqr` 作为 `{type:"active_goal", value}` 入队。
**要点**：字段少、浅比较去抖、清除即 `goal:null`——这是"不打扰"的结构性根因。

### 2.3 达成/失败徽标（`m0274:51` + `m0671:3396-3420`）

`sentinel` goal_status（`m0274 VVn`）= `{type:"goal_status", met, sentinel:true, condition}`，仅用于在转录里标记状态、**不当作求值结果**。
真正达成/失败时写带统计的 goal_status：

- 达成：`{type:"goal_status", met:true, condition, reason, iterations, durationMs, tokens}`
- 失败/impossible：`{met:false, failed:true, condition, reason, iterations, durationMs, tokens}`

前端把非 sentinel 的 goal_status 渲染成徽标（含人类可读时长、turns、tokens），**sentinel 隐藏**。

### 2.4 活动目标卡片（`m2146:200-246`）

纯被动卡片，标题固定 `Goal active`，副标题 `running {duration} · {N} turns · {tokens} tokens`，
正文 `Goal: {condition}`、`Last check: {lastReason}`，底部提示 `/goal clear to stop early · escape dismiss`。
**它是常驻状态位，不是打断式弹窗。**

### 2.5 nudge（`m1837:1227-1231`）

整个 goal 唯一的"提示"是一条**建议条**：

```js
{ id: "goal-command-nudge",
  content: async e => `Set an objective with ${Kn("suggestion",e.theme)("/goal")} — Claude keeps working until it's met`,
  cooldownSessions: 3,
  isRelevant: async () => uJe() }
```

即：文案一行、**冷却 3 个会话**、且只有 `uJe()` 为真才出现（`uJe` 内部再判登录/是否已禁用等）。
**这就是"轻"的关键**：能被忽略的建议行 + 冷却 + 相关性门，而不是弹窗。

### 2.6 遥测事件（`m0017` 会话标记 + `i/g/p/m` 宏）

代表事件：`tengu_goal_proposed` / `tengu_goal_proposal_decided` / `tengu_goal_proposal_available` /
`tengu_stop_hook_added` / `tengu_stop_hook_removed` / `tengu_goal_cleared` / `tengu_goal_achieved` /
`tengu_goal_failed` / `goal_met`(met/impossible/evaluator_timeout/evaluator_error) /
`goal_set` / `goal_propose` / `goal_checkin` / `goal_interruption`。
`Qee`（`m0274:70`）是 goal_cleared 的字段样例：`{reason, iterations, durationMs, origin}`。
`proposeGoalAvailabilityLogged`（`m0017:2218`）保证"提议可用"只记一次。

---

## 3. GA 现状与差距

| 能力 | GA 现状 | 差距 |
| --- | --- | --- |
| goal 状态对外元数据 | 只有 `active_goal` 事件 + `goal_status` 写进会话 jsonl（`transcripts.record_active_goal/record_goal_status`） | 无稳定结构化字段、无 diff 去抖、无 `goal:null` 清除语义、无 SDK 契约 |
| 前端卡片 | 无 goal 卡片/徽标 | 需要只读状态位 |
| 遥测 | 无 | 需要本地事件记录 + 开关 |
| nudge | 无 suggestion 框架 | 需要最小建议条 + 冷却 + 相关性门 |

现有可复用点：`genericagent/core/transcripts.py` 已有 `record_active_goal`/`record_goal_status`（字段齐全，
包含 met/failed/reason/iterations/sentinel/session_id/created_at）；`app._record_active_goal/_record_goal_status`
是现成的写入口；Ink bridge 已有 emit 通道（`bridge.emit`）可承载元数据事件；App.tsx 已有面板/横幅渲染骨架。

---

## 4. 目标设计（对齐 CC，且刻意更克制）

### 4.1 设计第一原则：**默认零打扰**

- 元数据/卡片：**被动渲染**，绝不弹窗、不抢焦点、不打断输入；
- 遥测：**默认本地 JSONL 记录**，默认**不发网络**、可关；不采集对话正文，只记计数/时长/来源等元字段；
- nudge：**默认关闭**（`GA_GOAL_NUDGE=0`），开启后也只是**一行建议条**、有冷却、有相关性门、可 Esc 忽略。

### 4.2 SDK 元数据契约（新）

定义 goal 元数据对象（与 CC 对齐字段，命名用 snake_case 以贴合 GA）：

```json
{"condition": "...", "iterations": 2, "set_at": 1730000000,
 "tokens_at_start": 0, "last_reason": "...", "met": false}
```

- 变化才推送（沿用 CC 的浅比较字段集合 `condition/iterations/set_at/tokens_at_start/last_reason`）；
- 清除 → 推 `{"goal": null}`；达成 → `met:true` 后随清除顺序推 null（与 CC 一致）；
- 通道：新增 bridge 事件 `{type:"goal_metadata", goal: {...}|null}`，前端据此渲染卡片；
  同时提供查询接口（`agent` 侧 getter），供 SDK/ACP 读取。
- **只读**、无副作用；不引入新的模型行为。

### 4.3 前端卡片（只读状态位）

- 仿 CC `m2146`：标题 `Goal active`，副标题 `running {duration} · {N} turns · {tokens} tokens`，
  正文 `Goal: {condition}`、`Last check: {reason}`，底部 `/goal clear to stop early`；
- 常驻在既有状态栏区域，**不覆盖输入、不模态**；无 goal 时不显示。
- 达成/失败：用一行徽标（`goal met` / `goal failed`）表示，带时长/turns，**sentinel 不显示**。

### 4.4 遥测（本地优先）

- 事件集（GA 简化版，够用即可）：
  `goal_set` / `goal_cleared` / `goal_achieved` / `goal_failed` /
  `goal_met`(met|impossible|evaluator_timeout|evaluator_error) / `goal_proposed` / `goal_proposal_decided`。
- 字段：`{event, origin, iterations, durationMs, tokens, reason_len, at}`（**不含对话正文**，`reason` 只记长度）；
- 落盘：`<GA_DATA_DIR>/logs/goal_events.jsonl`（复用 RUNTIME 路径），**每行一个 JSON**；
- 开关：`GA_GOAL_TELEMETRY`（默认 `local`；`off` 关闭；未来若接远端再扩展，但默认不发网络）。
- 与既有 `goal_status` 转录**并存不冲突**：转录是会话事实，遥测是运行统计。

### 4.5 nudge（一条建议行，默认关）

- 文案（对齐 CC，改中文）：`用 /goal 设定一个目标 —— Claude 会一直工作到它达成`（可本地化）；
- 出现条件：会话空闲的建议位、**冷却 `GA_GOAL_NUDGE_COOLDOWN_SESSIONS`（默认 3）**、
  且 `uJe` 等价相关性门（未禁用 hooks、非受限环境、当前无 active goal）；
- 交互：一行、可在输入框旁展示、`Esc` 忽略；**绝不弹窗**；
- 默认 `GA_GOAL_NUDGE=0`（关）。开启后也只在"符合条件 + 冷却到期"时出现一次。

---

## 5. 分阶段实施步骤（未来执行时照此推进）

### S0：元数据层（最小、只读）
- 新增 `genericagent/core/goal_metadata.py`：`goal_metadata(active_goal)`（字段裁剪）+ `diff_goal_metadata(old,new)`（浅比较）；
- 在 `app._set_goal/_clear_goal/_apply_goal_verdict` 的状态变化点，调用一个统一的 `_notify_goal_metadata()`：
  推 `hook_emit`/bridge 事件 `{type:"goal_metadata", goal: {...}|null}`；无 emit（headless）时静默。
- 单测：字段裁剪、用 diff 去抖（同值不重复推）、清除推 null、达成后推 null。

### S1：前端卡片（只读）
- `protocol.ts` 加 `goal_metadata` 事件；App.tsx 加 `GoalStatusBar` 只读组件（不发命令、不抢焦点）；
- 卡片内容对齐 CC `m2146`；无 goal 不渲染；徽标用于达成/失败；
- 单测：纯函数 `goalCardModel(goal)` 的展示文案（duration/turns/tokens/Last check）。

### S2：遥测（本地）
- 新增 `genericagent/core/goal_telemetry.py`：`record(event, **fields)`，写 `<GA_DATA_DIR>/logs/goal_events.jsonl`（append，UTF-8/LF）；
- 在 set/clear/achieved/failed/goal_met/proposed/decided 处埋点；`GA_GOAL_TELEMETRY=off` 直接跳过；
- 单测：事件字段、off 时不写、失败不抛（best-effort）。

### S3：nudge（默认关）
- 新增 `genericagent/core/goal_nudge.py`：纯函数 `should_nudge(state, cooldown_sessions)` + 文案生成；
- 会话级冷却计数落 `settings.json`（或运行时状态），`GA_GOAL_NUDGE=0` 默认关；
- 前端建议位渲染一行，`Esc` 忽略；
- 单测：冷却逻辑、相关性门、默认关不出现。

### S4：文档与发布验证
- 用户文档：`/goal`、卡片、遥测开关、nudge 开关说明；
- 发布包按「env 文件化计划」要求把新增开关纳入 `ga.env.example`；
- 真实 LLM 冒烟：设目标→看到卡片→达成→变徽标→遥测 jsonl 有记录（nudge 关时完全不出现）。

## 6. 与「env 文件化」计划的衔接

阶段 5 会新增 `GA_GOAL_TELEMETRY` / `GA_GOAL_NUDGE` / `GA_GOAL_NUDGE_COOLDOWN_SESSIONS` 等开关。
按 `docs/20261030-GA配置env文件化实施计划.md` 的 E0/E2 步骤，这些开关应：
- 登记进 `KNOWN_CONFIG` 表；
- 通过 `env.get_*()` 读取（不裸写 `os.environ.get`）；
- 出现在 `genericagent/resources/ga.env.example`。
（两项计划可并行设计，落地时以 env 文件化先行为宜，避免又新增裸 env 读取点。）

## 7. 风险与取舍

- **隐私**：遥测绝不含对话正文/条件全文（只记长度/计数）；默认本地、默认不发网；可一键 off。
- **打扰**：nudge 默认关；即便开也只是一行 + 冷却 + 相关性门；元数据/卡片完全被动。
- **一致性**：元数据字段一旦对外就是契约，后续改字段要兼容；建议先按 CC 的 5 字段最小集。
- **与转录重复**：`goal_status`（会话事实）与遥测（运行统计）职责不同，不合并。
- **不做过度设计**：不引入新依赖；遥测不做远端上报（除非将来单独立项）。

## 8. 验收清单（DoD，未来执行时用）

- [ ] goal 状态变化能通过 `goal_metadata` 事件被动推送，字段为 CC 对齐的 5 项 + met；清除推 null；
- [ ] Ink 有只读 `Goal active` 卡片 + 达成/失败徽标，无 goal 不显示、不抢焦点；
- [ ] 遥测默认写 `logs/goal_events.jsonl`，`off` 可关，不含正文，best-effort 不抛；
- [ ] nudge 默认关；开启后一行建议条 + 默认冷却 3 会话 + 相关性门；
- [ ] 新增开关全部走 env 文件化计划（`KNOWN_CONFIG` + `ga.env.example`）；
- [ ] 真实 LLM 冒烟：设目标/达成/清除全链路卡片与遥测正确，nudge 关时零打扰。

> 本纪要仅规划，未改动任何运行代码。CC 论点均来自 `reconstructed303/deminified/` 只读源码（m0274/m0671/m0696/m1809/m1837/m2146/m0017），
> 坐标见 §2。真正实施按 S0→S4 推进，每阶段独立可测、可回退。
