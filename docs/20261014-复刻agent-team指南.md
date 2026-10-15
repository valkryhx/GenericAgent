# Claude Code 2.1.283 `Agent Team` 协作机制 —— 深度调研 + 复刻指南

> **位置说明（GA 仓库副本）**：本文是对 Claude Code 2.1.283 源码的调研，原始证据与全部模块位于
> `D:\git_codes\claudecode283_hack`（`claudecode283/modules/`、`claudecode283/extracted/`、
> `reconstructed/`）。文中 `mNNNN @ 偏移` 坐标与 `reconstructed/...` 路径均相对该仓库。
> GA 复刻映射见 §12。

> 坐标格式 `mNNNN @ <偏移>` = `claudecode283/modules/` 里该模块内第 N 个字符偏移。
> 本文是 `claudecode283/extracted/agent-team-internals.md` 的**深度补充**：那份文档讲清了
> 「怎么 spawn 一个 teammate」（路由 + 三后端 + 身份预留），本文补齐它 §9 明确留下的 5 个缺口，
> 并把散落的子系统（**通信 / 共享任务板 / 团队持久化 / swarm 共享总线 / 协调者提示词**）
> 串成一张完整机制图，供开发者把它复刻进自研 Agent（GenericAgent）。
>
> **§8 为第二轮深挖**：聚焦多 agent 互动的**稳定性 / 通信 / 集体协作**，提炼出 CC 的
> **四大支柱 —— 通信可靠 / 冲突减少 / 进度推进 / 发现即通知**，每条都带源码坐标。
>
> 云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
>
> 证据等级沿用 `reconstructed/NAMING-METHOD.md`：**权威**（导出名/字符串字面量/属性名）、
> **推断**（由用法与日志反推）。所有结论都标了来源；不确定的单列。

---

## 0. 一句话结论

CC 的 Agent Team = **一个协调者（lead，名字固定 `team-lead`）+ 若干 teammate**，跑在
**单一隐式团队**里（v2.1.283 起 `team_name` 已废弃），靠四条正交通道协作：

| 通道 | 工具 | 载体 | 作用 |
| --- | --- | --- | --- |
| **控制** | `Agent` / `TaskStop` | 主会话 | 起/停 teammate |
| **消息** | `SendMessage`（+ `ListAgents` / `SendFile`） | UDS socket + mailbox | teammate↔lead↔teammate 点对点消息 |
| **任务板** | `TaskCreate/Get/Update/List` | `storageV5`（tasks 目录） | 共享、可认领、带依赖的任务清单 |
| **状态** | team 文件（`config.json`） | `<configDir>/teams/<name>/config.json` | 团队名 / lead / 成员表 |

关键设计点（复刻必须照抄的）：
1. **协调者只协调、不执行**（`m0280`）：lead 的裸文本对用户可见，但 teammate 的裸文本**对任何人不可见**——必须用 `SendMessage`。
2. **值传递，不共享上下文**：teammate 看不到 lead 的对话；每次派活必须是自包含 prompt。
3. **名字即地址，且跨会话稳定**：`SendMessage({to: "<name>"})`；名字在 agent 结束后仍可用（send 会把它从 transcript 唤醒）；重名用 `name [ref]` 消歧。
4. **任务板是协作契约**：teammate 空闲后 `TaskList` 找 `pending`+无 owner+无 blockedBy 的任务，用 `TaskUpdate({owner: <自己名>})` 认领。
5. **持久化是 CAS + 锁文件**：team 文件所有写入走带 lockfile 的 read-modify-write（`m0319`）。

---

## 1. 概念模型

- **lead（协调者）**：名字恒为 `"team-lead"`（`m0093 var mi="team-lead"`）。用户只跟 lead 交互。
- **teammate**：`Agent` 工具带 `name` 起出来的具名子 agent；可跑在 tmux/iTerm2 分屏或 in-process。
- **team**：会话级**单一隐式团队**，名字默认 `session-<sessionId 前 8 位>`（`m1614 c(t)=session-${t.slice(0,8)}`）。
- **task board**：会话级任务清单（非 teammate 也可用），teammate 模式下额外启用「认领」工作流。
- **mailbox / inbox**：teammate 的收件箱（未读消息、held 消息、投递回执）。

---

## 2. 模型可见工具面（完整清单）

| 工具 | 常量 | 模块 | 关键参数 | 作用 |
| --- | --- | --- | --- | --- |
| `Agent` | `ht="Agent"` | `m0511` | `name`, `prompt`, `subagent_type`, `isolation`, `model`（`team_name`/`mode` 已废弃） | 起子 agent；带 `name` 即具名 teammate |
| `ListAgents` | `Nl` | `m1534` | （`channel`/`q` 本 build 不可用） | 列出可 `SendMessage` 的 agent |
| `SendMessage` | `lo="SendMessage"` | `m1548`（描述+工具）/`m1549`（执行） | `to`, `message`, `summary?` | agent 间点对点消息 |
| `SendFile` | `mZ` | `m1549` | `files`, `to` | 给另一个会话发文件 |
| `TaskStop` | `og="TaskStop"` | `m0531` | `task_id`（也接受 teammate 的 agentId/名字） | 停一个后台任务/teammate |
| `TaskCreate` | `Ek` | `m0531` | `subject`, `description`, `activeForm?`, `metadata?` | 建任务（status=pending） |
| `TaskGet` | `MB` | `m0531` | `taskId` | 取任务全量 |
| `TaskUpdate` | `Tk` | `m0531` | `taskId`, `status?`, `owner?`, `addBlocks?`, `addBlockedBy?`… | 改任务/认领/删（status=deleted） |
| `TaskList` | `Sw` | `m0531` | （无） | 列任务摘要 |
| `ExitPlanMode` | `iu="ExitPlanMode"` | `m0357` | `launchSwarm`, `teammateCount` | 退出计划模式时可选批量拉起 swarm |
| `TeamCreate`/`TeamDelete` | — | 仅 `m0357` | — | **本 build 未提供**（见 §11） |

> 证据：`m0357 @ 921444` 的 `nOn=new Map([[iu,["launchSwarm","teammateCount"]],[ht,["name","team_name","mode"]]])`
> ——把「swarm 专属 schema 字段」映射到工具；`m0511 @ 10530` 的 `Agent` schema 里
> `team_name` 的描述是 **"Deprecated; ignored. The session has a single implicit team."**。

---

## 3. spawn（引用现有文档 + 一处更正）

`spawnTeammate` 的路由与三后端（tmux 新窗口 / tmux split-pane / in-process）见
`agent-team-internals.md` §3，此处不重复。**一处更正**：

- 现有文档说「带 `team_name` 时就是 spawn teammate」。实测 v2.1.283 的 `Agent` schema 已把
  `team_name` 标为 **Deprecated; ignored**（`m0511 @ 10530`），团队是**会话级隐式单例**。
  起 teammate 的判据现在是 **传了 `name`**（描述："Name for the spawned agent. Makes it
  addressable via SendMessage({to: name}) while running."），不再是 `team_name`。

---

## 4. 通信机制（SendMessage）★ 补齐缺口 2

### 4.1 工具定义与 schema

```js
// m1548 @ 47069
var vs = Ft({
  name: lo,                                  // lo = "SendMessage"（m0261）
  searchHint: "send messages to agent teammates",
  userFacingName(){ return "SendMessage" },
  get inputSchema(){ return Se() },
  coerceInput: (s)=>Tsn(s,{applySplit:Fe()}),
  isReadOnly(s){ return typeof s.message === "string" },
  backfillObservableInput(s){ /* legacy type/recipient/content -> to/message */ },
});
```

Input schema（`m1548 @ 31647`）：

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `to` | 必填 | "a teammate name, or `main`" |
| `summary` | 可选，`max 200`（`o3e=200`） | 5–10 词预览；**超长截断不报错** |
| `message` | 必填（string 或协议对象） | 正文；对象形态用于协议帧（§4.6） |

> legacy 字段：`m0261 kPr=["type","recipient","content","request_id","approve"]` —— 旧版
> `type:"message"` + `recipient` + `content` 会由 `backfillObservableInput` 回填成 `to`/`message`。

### 4.2 寻址模型

`SendMessage` 描述里的 `to` 表：

| `to` | 含义 |
| --- | --- |
| `"researcher"` | 按名字找 teammate |
| `"main"` | 主会话（**仅后台 subagent 可用**） |
| `"worker [3fa9c1]"` | 名字 + ref 消歧（`ListAgents` 显示两行同名时用） |

规则（`m1548 @ 24305`）：
- **裸名字**只要在**本机/它机/云端**唯一匹配就直接投递；只有歧义时（前缀匹配到多个、或 `ListAgents`
  显示两行同名）才要求加 ` [ref]`。
- **名字在 agent 结束后仍可用**——send 会把它从 transcript **唤醒**（`m1344` 的 `agent-stopped`）。
- 只有「没有名字」或「新 agent 顶了旧名」时才用原始 `agentId`（格式 `a...-...`，latest wins）。

### 4.3 recipient resolver（`m1344`）

`m1344` 把 `to` 解析成一个 **kind**，据此决定投递或唤醒：

```
E(e):  // 把 resolver 结果归类
  main / not-found / ambiguous / agent-stopped-by-user / mailbox / local-session / cloud-session -> null（不 pin）
  agent-live / agent-stopped / agent-evicted -> { kind:"subagent", id:agentId, name:agentName }
```

- `agent-stopped` 与 `agent-evicted` 都算「可从 transcript 恢复」。
- resolver 还负责**改名检测**：若 pin 的 `agentId` 对应名字已被新 agent 占用，返回 `kind:"rebound"`
  （`m1344 @ 2009` 的 `{kind:"rebound", name, previous, next}`）。

### 4.4 send pins（跨会话稳定寻址）

`SendMessage` 每次成功会记录一条 **pin**（`{name,id,ref}`），持久化在会话 transcript 里
（`m1344` 的 `cnr()` 从 `tool_use SendMessage` + 其 `tool_result.pin` 重建 `sendMessagePins`）。
下次按同名字发送时先查 pin：命中就直接用 `id`；名字被顶则触发 `rebound` 逻辑。这让
「按名字继续一个已完成 worker」在**跨会话恢复**后依然成立。

- AppState 字段：`sendMessagePins`（`m0974` / `m0162` 的 `setSendMessagePin`）。
- 遥测：`send_message_delivery`，带 `route / duration_ms / blocked_wait / via / exact_unique /
  previously_pinned / search_truncated / renamed_hint / restarted_since_pin / identity_contested`。

### 4.5 投递通道与错误类（本地 socket）

投递走**本机 UDS（unix domain socket）**，错误被归一化成稳定类（`m1548 we(s)`）：

| 错误类 | 触发 |
| --- | --- |
| `stale_socket` | socket 已消失 |
| `socket_busy` | socket 忙 |
| `too_large` | 超长 |
| `sender_paced` | 发送端限速 |
| `invalid_target` | symlink / 不可验证 / 非本地 |
| `timeout` | 发送超时 |
| `other` | 其它 |

> 复刻启示：**别自己造传输**。GA 已有 `subagent_ipc.py` / `subagent_mailbox.py` / `subagent_event_bus.py`，
> 对齐这些错误类即可（见 §12）。

### 4.6 协议帧（legacy，但 GA 直接可用）

`SendMessage` 描述里的协议约定（`m1548 @ 25319`）：收到 `{"type":"shutdown_request"}` 或
`{"type":"plan_approval_request"}` 时，回一个匹配的 `_response`，**echo `request_id`，置 `approve`**：

```json
{"to": "team-lead", "message": {"type": "shutdown_response", "request_id": "...", "approve": true}}
{"to": "researcher", "message": {"type": "plan_approval_response", "request_id": "...", "approve": false, "feedback": "add error handling"}}
```

- **批准 shutdown 会终止对方进程**；**拒绝 plan** 会把 teammate 打回重做。
- 不要主动发起 `shutdown_request`（除非被要求）。

### 4.7 mailbox / inbox（未读投递）

- in-process teammate 有 **inbox**：`m1575` 的 `[inProcessRunner] ... delivering N held message(s)`，
  以及 `worker_mark_read_failed_streak` 的读回执重试。
- 空闲结果投递：`m0300` 的 `swarm_idle_result_delivery`（`budget_truncated` / `mailbox_write_failed` /
  `suppressed_lead_dm`）。
- 未读集合：`m1448` 的 `suppressed_lead_dm`（lead 私信被抑制时）。
- 关键：**消息是自动送达的**（描述："Messages from teammates are delivered automatically; you
  don't check an inbox."）——模型不需要轮询收件箱。

### 4.8 ListAgents / SendFile

- `ListAgents`（`m1534`）：`userFacingName "ListAgents"`，`isEnabled(){return Ws()}`，
  内部 `listAllPeers` → `formatForModel`；输出 `{listing}`。`channel`/`q` 参数本 build 标
  "Not available in this build; leave unset."。
- `SendFile`（`m1549 @ 20885`）：`name:mZ`，`userFacingName "SendFile"`，`isEnabled(){return nlt()}`，
  参数 `files` + `to`；描述含安全提示「发往 Remote Control / cloud 会话时，文件内容经 Anthropic 服务器
  跨机传输」——**云端部分不复刻**。

---

## 5. 共享任务板（TaskCreate/Get/Update/List）

### 5.1 工具与 schema（`m0531`）

| 工具 | 输入 | 输出 | 语义 |
| --- | --- | --- | --- |
| `TaskCreate` | `{subject, description, activeForm?, metadata?}` | `{task:{id,subject}}` | 建任务，**status 恒为 pending**，owner 空，blocks/blockedBy 空 |
| `TaskGet` | `{taskId}` | `{task:{id,subject,description,status,blocks,blockedBy}\|null}` | 取全量 |
| `TaskUpdate` | `{taskId, subject?, description?, activeForm?, status?, addBlocks?, addBlockedBy?, owner?, metadata?}` | `{success,taskId,updatedFields,error?,statusChange?}` | 改/认领/删 |
| `TaskList` | `{}` | `{tasks:[{id,subject,status,owner?,blockedBy}]}` | 摘要（blockedBy 只留**未完成**的） |

### 5.2 状态机与依赖

- 状态：`pending` → `in_progress` → `completed`；`deleted` 永久删除（`TaskUpdate` 描述）。
- `status` 枚举由 `fat()` 给出（`m0531`），`TaskUpdate` 额外允许 `"deleted"`。
- 依赖：`blocks` / `blockedBy` 双向边，用 `addBlocks` / `addBlockedBy` 设置；
  **`TaskList` 只列「未完成的 blockedBy」**（已完成依赖自动隐藏，`m0531 @ 137456`）。
- **铁律**（描述原文）：「ONLY mark a task as completed when you have FULLY accomplished it」；
  测试失败/实现不全/有未解错误/找不到依赖时**保持 in_progress**，另建任务描述阻塞点。

### 5.3 teammate 工作流（认领式协作）

`TaskList` 描述里的 **Teammate Workflow**（`m0531 @ 135724`，仅当 `_o()` 为真时附加）：

```
1. 完成当前任务后，调 TaskList 找可用的活
2. 找 status='pending' 且无 owner 且 blockedBy 为空的任务
3. 多个可用时按 ID 升序（低 ID 优先，早任务常为后任务铺上下文）
4. 用 TaskUpdate 认领（owner 设为你的名字），或等 lead 指派
5. 被阻塞就去做能解阻塞的活，或通知 lead
```

- 认领/指派会产生一条 `task_assignment` 消息发给 owner（`m0531 @ 134321`：
  `b({type:"task_assignment", taskId, subject, description, ...})`）。
- 任务完成会提示：「Task completed. Call TaskList now to find your next available task or see if
  your work unblocked others.」（`m0531 @ 135222`）。

### 5.4 持久化

任务存在 `storageV5`（`kR()` 指向 tasks 目录）；`TaskCreate` 落盘后触发 `TaskCreated` 钩子
（可被 hook 拒绝：`TaskCreated hooks cancelled`，`m0531 @ 126634`）。任务带 `metadata`（可放
`_internal` 之类内部标记，`TaskList` 会 `filter(h=>!h.metadata?._internal)` 隐藏）。

---

## 6. 团队持久化（team 文件）★ 补齐缺口 3

### 6.1 路径与命名（`m0018` + `m0319`）

- 配置根：`Se() = CLAUDE_CONFIG_DIR ?? ~/.claude`（`m0018`，NFC 归一化）。
- teams 目录：`LBe() = join(Se(), "teams")`。
- 团队目录名：`ksn(name) = name.replace(/[^a-zA-Z0-9]/g,"-").toLowerCase()`（`m0319`）。
- **team 文件**：`KEe(name) = join(LBe(), ksn(name), "config.json")`（`m0319`）。
  → 即 `<configDir>/teams/<sanitized-name>/config.json`。
- 团队名校验：`m0018 A=/^[A-Za-z0-9_-]{1,64}$/`，且拒绝 Windows 保留名（`con/prn/aux/nul/comN/lptN`）。

### 6.2 schema（`m1614 initializeSessionTeam`）

```jsonc
{
  "name": "<teamName>",            // 默认 session-<sessionId[:8]>
  "createdAt": <ms>,
  "leadAgentId": "<lead agentId>", // 由名字+团队派生（wD）
  "leadSessionId": "<sessionId>",
  "members": [
    {
      "agentId": "<id>",           // 由 (name, team) 派生
      "name": "team-lead",         // 首成员是 lead，name = mi = "team-lead"
      "agentType": "team-lead",
      "joinedAt": <ms>,
      "tmuxPaneId": "leader",      // lead 恒为 "leader"；teammate 为 pane id 或 "in-process"
      "cwd": "<cwd>",
      "subscriptions": [],
      "backendType": "in-process"  // "in-process" | "tmux" | "iterm2"
    }
  ]
}
```

- 读取 `ay(teamName, storageV5?)`：优先走 storage v5 `read`，否则 `fs.readFile(KEe(name))`；
  读到后 `D(e)` **过滤非法成员**（保留 `agentId`/`name` 均为 string 的项）。
- 写入 `U1n(teamName, config, storageV5?)`：`mkdir(recursive)` + `writeFile(config.json, JSON.stringify(cfg,null,2))`。

### 6.3 CAS + lockfile（并发安全）

所有变更走 `Bae(teamName, mutator, opts?, storageV5?)`（`m0319`）：
- 取锁：`lockfilePath = <config.json>.lock`，用 `Ri()`（proper-lockfile 风格，retries=10）。
- 读 → 应用 `mutator`（返回 `false` 表示不写）→ 写。
- storage v5 路径用**乐观并发**：`update()` 返回 `LockSuspect`/`LockContended` 时最多重试 5 次。
- 语义化辅助：`mNe`（按 agentId/name 删成员）、`o0t`（按 agentId 删，支持 `onlyIfJoinedBefore`
  防「删了又被重加」的竞态）、`B1n`（设成员 `mode`）、`i0t`（设成员 `isActive`）。

> 成员可选字段：`mode`（权限模式）、`isActive`（active/idle）。

### 6.4 成员生命周期

- 加入：spawn 时 `j()`（reserveTeammateIdentity）CAS 推进一条成员（见 `agent-team-internals.md` §3.3）。
- 退出：`mNe`/`o0t`/`lxr` 从 `members` 摘除；`o0t` 的 `onlyIfJoinedBefore` 防止「移除发起后又被重加」。
- 空闲：`TeammateIdle` 钩子（§9）；`isActive=false` 表示 idle。

---

## 7. swarm 共享总线（`m0184`）

`ti()` 返回一个**会话级 swarm host 单例**，把协作状态集中管理：

```js
// m0184
class g {
  inbound;          // class o：收到的消息（held / modeAtUnwire / onPeerHeld ...）
  receipts;         // class d：outstandingSends / awaitingTerminal（投递回执）
  outbound;         // class l：pacer（发送限速）
  ingress;          // class a：ownUdsHopToken / ownBridgePeerAddressResolver / messageDropped
  swarmPermissions; // class u：pending Map / pendingSandbox Map / pendingPlanApproval
  mailbox;          // class p：reportedDroppedEntries / pendingPrunes / ledger
  taskList;         // class m：updated 事件 / locks / leaderTeamName
}
```

- **权限同步**：`swarmPermissions.pending`（in-process teammate 的权限请求）由
  `m0970 SwarmPermissionPoller` 轮询；`pendingPlanApproval` 承载计划审批。
- **沙箱权限**：`m0347` 把沙箱权限请求同步给 lead（`swarm_sandbox_permission_request`；
  需要 team name + leader name，`zE()==="team-lead"` 判定是否 lead）。
- **消息回执**：`receipts` + `mailbox.ledger`（`m0184` 的 `wrote()` 用 msg_id 去重）。
- **tmux 会话名**：`m0093 IFt()=claude-swarm-${process.pid}`；`BLo="CLAUDE_CODE_TEAMMATE_COMMAND"`。

---

## 8. 多智能体协作四大支柱：通信可靠 / 冲突减少 / 进度推进 / 发现即通知（第二轮深挖）

> 本轮聚焦「多个 agent 互动时的**稳定性、通信、集体协作**」。结论先行：CC **不靠"事后冲突检测"**，
> 而是靠 **结构化消息 + 认领式任务板 + 工作区隔离 + 结果自动投递** 四件事，把冲突**消灭在发生之前**。
> 本节每条结论都带模块坐标，可回查源码。
>
> **【复刻时暂时不需要参考 claudecode 云端会话的实现】**（详见 §8.7）：本节涉及远端 task list、
> 跨机器 peer 等云端分支可只做本地实现；但**本地协议语义要照抄**。

### 8.1 通信可靠性：三层保障（传输 → 投递回执 → 消费）

一条 teammate 消息要穿过三层，每层都有独立的可靠性设计。

#### (A) 传输层：本地 UDS + 对端校验（`m0188`）

发送函数 `Pe(path, frame, storage, {noFollowSymlink, expectPeerPid, expectPeerProcStart, preflightedJson})`：

- **路径合法性**：`IN(path)` 拒绝非本地 IPC 路径（remote / UNC / 多段 pipe / 结尾点或空格），
  抛错误类 `non-local`。
- **对端可信用**：`MLo()` 校验对端 inbox 的 **owner token**（`requireLiveOwner`）。若对端没有活会话注册 inbox，
  抛 `No running session has registered an inbox at <path> (<kind>) — refusing to send to an unvouched pipe`。
- **符号链接防护**：`noFollowSymlink` 时对 reply target 做 `lstat`，是 symlink 就拒发（错误类 `symlink`）。
- **连接超时** `5000ms`。
- **帧大小上限**：`ye(n)` 断言 `DLo + len(n) + 1 <= GOt`，超限抛错（常量 `Ve=150` 族）。
- **发送即记账**：成功发送后 `Xe(msg_id, to)` 把 `{msgId,to}` 压入 `receipts.outstandingSends`
  （上限 `be=200`，FIFO 淘汰）。

#### (B) 投递回执层：状态机 + 入站闸门（`m0188` receipts + `m1356` inbound gate）

回执状态机 `XAr(msgId, state)`：

- `outstandingSends` 命中且 `state==="held"` → 移入 `awaitingTerminal`（标 `wasHeld:true`）。
- 命中 `awaitingTerminal` 且 `state!=="held"` → 从两个表移除，返回 `{destination, wasHeld:true}`。
- `JAr(deadIds)`：发送者已死时清理其未决回执，按目标统计 `{dropped, wereHeld}`。

对端**入站闸门** `peer_inbound_gate`（`m1356`）：

- 策略集合 `{accept:0, hold:1, refuse:2}`，越严越高；跨
  `policySettings > flagSettings > userSettings/repoSettings` 归并（同优先级时 repo 让位 policy）。
- `accept` → 投递，`recordCorrespondent`。
- `hold` → 压入 `held[]`（上限 **`F=100`**），回执 `"held"`；**缓冲满时淘汰最旧的为 `"expired"`**
  （`hold buffer full — evicted oldest as expired`），并触发 `onPeerHoldDropped`。
- `refuse` → 回执 `"dropped"`（带 `dropReason` / `droppedMsgIds`）。
- **shutdown**：把仍 held 的消息统一结算为 `"expired"`（`settling N still-held peer message(s) as expired`）。

#### (C) 消费/已读层：防伪造 + 已读去重（`m1575` in-process runner + `m0300 Wae`）

- **收件**：`gNe(agentName, teamName, storageV5)` 读自己的 inbox 文件；读失败累加 `readFailures`，
  连续失败时 warn（`could not read mailbox (N consecutive)`）。
- **防伪造 provenance**（`m0300 Wae(unread, inboxPath, {roster, isPaneTeammate})`）→ 逐条判：
  - `"verified"`：`msg_id` 存在、未重复，且**本进程 ledger 记录过写这条消息**（`ledger.wrote(recipient,msg)`，
    ledger 用 sha256 摘要记账，见 §7 `class i`）。
  - 否则：发送者**在 roster 里** → `"forged"`；不在 → `"unverifiable"`。
  - **只有非 `forged` 的消息被消费**（`u.get(r)==="forged"` 直接跳过）。
- **已读标记** `wge(...)`：失败累计 `l.count`；`l.count < Sge` 时下轮重试；达到 `Sge` 则
  「先投递、不标记」（`deliveredUnmarked`），并发遥测
  `swarm_inbox_poll` / `worker_mark_read_failed_streak`。
- **held 集合**：`l.deliveredUnmarked` / `l.consumedUnmarked` 避免重复投递同一条未标记消息。
- **shutdown 优先**：若 inbox 里出现 lead 的 `shutdown_request`，**先把更早的 held 消息投递完**再执行 shutdown
  （`delivering N held message(s) ahead of a shutdown_request`）。
- **协议帧过滤**（安全边界）：
  - `plan_approval_response` **仅接受 `from === mi`（`team-lead`）**，否则
    `Ignoring permission response from non-team-lead`。
  - `mode_set_request` **从 inbox 收到一律丢弃**：
    `dropping mode_set_request message: permission mode changes are never accepted from the inbox`。
  - 其它协议帧丢弃并 warn（`dropping protocol frame from <from>`）。

> **复刻要点**：CC 把「消息可靠」拆成 **传输校验 + 回执状态机 + 已读去重 + 防伪造** 四件事。
> **GA 现状**：`SubagentMailbox` 有未读投递，但**无回执状态机、无 provenance 校验、无 held/expired**。

### 8.2 冲突减少：不是"检测冲突"，而是"结构上不产生冲突"

CC 用三层机制让「两个 agent 同时改同一个东西」几乎不可能发生。

#### (A) 工作区隔离（worktree）—— 物理隔离 + 值传递

- 每个 teammate 在**独立 git worktree** 里干活，物理上不共享工作目录。
- 产物通过**值传递**（消息里带路径 / 内容）回传，而不是共享同一份文件互改。
- 详见 `claudecode283/extracted/agent-team-internals.md` 与主文档 worktree 隔离章节。

#### (B) 任务认领 = 软锁（`m0300 claimTask`）

任务 schema 带 `owner` 字段。认领逻辑：

```js
// m0300 claimTask(taskList, taskId, owner)
if (task.owner && task.owner !== owner) return { success:false, reason:"already_claimed", task }
if (task.status === "completed")         return { success:false, reason:"already_resolved", task }
const open = new Set(all.filter(t=>t.status!=="completed").map(t=>t.id))
const blocked = task.blockedBy.filter(id => open.has(id))
if (blocked.length)                      return { success:false, reason:"blocked", blockedByTasks:blocked }
return { success:true, task: write(task, { owner }) }
```

- 变体 `claimTaskWithBusyCheck` 额外拒绝 `agent_busy`（一个 agent 同时只持有一个未完成任务）。
- 失败原因枚举：`task_not_found / already_claimed / already_resolved / blocked / agent_busy`。
  → **同一任务同一时刻只有一个 owner**，两个 teammate 不会同时做同一件事。

#### (C) 任务板文件锁（`m0300`）—— 写操作串行化

- 每个 task list 一个目录，写前取锁：`lockfilePath = <dir>/.lock`（`flag:"wx"` 独占创建），
  外包 proper-lockfile 风格 `{retries:30, minTimeout:5, maxTimeout:100, onCompromised}`（常量 `oe`）。
- 进程内再叠一层 `taskList.locks.run(key, fn)` 串行化（`ti().taskList.locks`）。
- 写失败（`LockSuspect`）最多重试 `gt=5` 次。
- **任务 ID 分配**：`createTask` 用 `ifAbsent` 语义抢 ID，冲突则取下一个（最多 `$e=16` 次）。
  → 多进程并发建任务也不会撞 ID。

#### (D) 收敛结论：CC 没有"文件级 owner"

- 全量搜源码：`artifactCollision` / `artifactOwnership` **0 命中**。
- `exclusive` / `owned by` / `claim` 的命中全部落在**文件锁、权限、worktree、任务 owner** 语境，
  **没有"某个文件被某 agent 独占"的概念**。
- 结论：CC 的防冲突 = **worktree 隔离（空间分开）+ 任务 owner（逻辑分开）+ 锁（写时串行）**，
  **不做运行时冲突检测**——这是比冲突检测**更工程稳健**的路线。

### 8.3 进度推进：空闲 teammate 自动认领 + 完成即解锁 + 死亡回收

#### (A) 空闲自动认领（`m1575 qe`）

teammate 进入 poll loop 后，每次轮询（间隔 **`he=500ms`**）除了查 mailbox，还调 `qe()`：

```js
// m1575 Ve(tasks)：找第一个「可认领」的任务
tasks.find(t => t.status === "pending"
             && !t.owner
             && t.blockedBy.every(id => !openIds.has(id)))
```

找到 → `QEo()` 认领 → 置 `status:"in_progress"` → 返回提示词
`He(task)` = `"Complete all open tasks. Start with task #N: <subject>\n\n<description>"`。

- **意义**：teammate 不空转；team 吞吐随空闲 agent 数**自动提升**；推进不需要 lead 逐个派活。

#### (B) 完成即解锁（`m0531` + `m0300`）

- 任务置 `completed` 后，`TaskList` **只列未完成的 `blockedBy`**，依赖自动"解锁"。
- 完成提示：「Call TaskList now to find your next available task or see if your work unblocked others.」
- 闭环 `pending → in_progress → completed`，并触发 `TaskCompleted` 钩子（§9）。

#### (C) 生命周期兜底：终止即回收（`m0300 N4e`）

- teammate **终止 / 关闭**时，把它名下所有未完成任务**回收**：`owner=undefined, status="pending"`。
- 生成通知：
  「`<name> has shut down. N task(s) were unassigned: #a "#subj", ... Use TaskList to check availability and TaskUpdate with owner to reassign them to idle teammates.`」
- **意义**：死掉的 agent 不会"占着任务不放"，team 不会因单点失败而卡死。

### 8.4 发现即通知：结果自动投递 + 去重

#### (A) 空闲通知帧（`idle_notification`，`m0300 mat`）

teammate 结束一轮、变 idle 时，向 lead 发一条**结构化帧**：

```jsonc
{ "type": "idle_notification",
  "from": "<agentName>",
  "timestamp": "<ISO>",
  "idleReason": "available" | "failed" | "interrupted",
  "summary": "<压成一行、截断的摘要>",
  "completedTaskId": "<id?>",
  "completedStatus": "completed" | "failed",
  "failureReason": "<body，仅失败时>",
  "result": "<结果正文，≤4000 字符>" }
```

#### (B) 结果长度预算（`m0300`）

- 单条 `result` 上限 **`xB=4000`** 字符，超出截断并附
  `[result truncated — ask the agent for the rest via <SendMessage>]`。
- 一次 drain（收集多条 idle 结果）总预算 **`eTo=16000`** 字符，超出替换为
  `[result truncated — this drain's results exceeded 16000 characters; ask the agent for it via <SendMessage>]`。
- 展示层另有 **`Rsn=40000`** 上限（`kF`）。
- 遥测：`swarm_idle_result_delivery` = `budget_truncated` / `per_frame_truncated` / `mailbox_write_failed`。

#### (C) 去重：显式 DM 抑制重复通知（`m1448 zo`）

- 提取 idle result 时，若**最后一条 assistant 消息**里已有成功的 `SendMessage`（工具名 `lo`）发往
  lead（`mi="team-lead"`）或 lead 别名（`Om`）→ **直接抑制** idle 结果投递，遥测 `suppressed_lead_dm`。
- 语义：teammate 已经主动汇报过，就不再重复投递，避免 lead 收到两份。

#### (D) 广播 / 多播

- `SendMessage` 支持发往 `team-lead`、具体 teammate、以及广播（寻址模型见 §4.2）。
- 一条发现经消息即可到达需要的成员；配合任务板 `owner` / `blockedBy`，
  **"上游发现"自动变成"下游可认领的活"**。

> **复刻要点**：CC 让"单个 agent 的发现"以 **结构化帧 + 长度预算 + 去重** 自动流到 lead 和其它 agent，无需人工搬运。
> **GA 现状**：有 `SubagentEventBus` 与父收件箱，但**无 idle_notification 帧格式、无长度预算、无显式 DM 去重**。

### 8.5 与「上万 agent」类比的工程落点

大规模 agent 协作的瓶颈**不是算力，而是协调开销**：消息风暴、重复劳动、状态漂移、死锁。
CC 的四支柱恰好各治一种病：

| 病 | CC 的机制 | 模块坐标 |
| --- | --- | --- |
| 消息丢失 / 乱序 / 伪造 | 传输校验 + 回执状态机 + provenance | `m0188` / `m1356` / `m0300 Wae` |
| 重复劳动 / 撞车 | worktree 隔离 + 任务 owner 软锁 | worktree / `m0300 claimTask` |
| 进度停滞 / 单点卡死 | 空闲自动认领 + 完成解锁 + 终止回收 | `m1575 qe` / `m0300 N4e` |
| 发现不共享 | idle_notification 自动投递 + 去重 | `m0300` / `m1448` |

> 「人多力量大」能兑现的前提是**协调层足够稳健**：加人才能加产出，否则加人只加混乱。
> CC 的四支柱正是把「协调开销」压到接近零的工程答案。

### 8.6 关键常量速查（本轮实测）

| 常量 | 值 | 含义 | 模块 |
| --- | --- | --- | --- |
| `be` | 200 | 未决发送 / 待终态回执表上限 | `m0188` |
| `F` | 100 | 入站 hold 缓冲上限 | `m1356` |
| `xB` | 4000 | 单条 idle result 上限 | `m0300` |
| `eTo` | 16000 | 单次 drain 结果总预算 | `m0300` |
| `Rsn` | 40000 | 展示层结果上限（`kF`） | `m0300` |
| `he` | 500 | poll 间隔（ms） | `m1575` |
| `gt` | 5 | 任务写锁重试次数 | `m0300` |
| `$e` | 16 | 抢任务 ID 最大尝试次数 | `m0300` |
| `oe` | retries 30 / 5..100ms | 任务板 lockfile 选项 | `m0300` |
| `mi` / `yye` / `e5e` | `team-lead` / `user` / `system` | 保留名 | `m0093` |
| `wD(e,n)` | `name@team` | agentId 派生 | `m0093` |

### 8.7 【复刻时暂时不需要参考 claudecode 云端会话的实现】

- 本节涉及的 **`Wo()`（远端 task list）**、**`storage v5` 远端分支**、
  **跨机器 `peer_inbound_gate`**，在自研 agent 中**可只做本地实现**。
- 但**本地协议语义要照抄**：回执状态机、任务 owner 软锁、空闲自动认领、`idle_notification` 去重——
  这些是**协议级**设计，与是否上云无关，**应当保留**。

## 9. 生命周期与钩子

钩子事件（`m0052` 事件枚举）里与团队相关的三个：

| 钩子 | 触发 |
| --- | --- |
| `TeammateIdle` | teammate 空闲 |
| `TaskCreated` | `TaskCreate` 落盘后（可拒绝：`TaskCreated hooks cancelled`） |
| `TaskCompleted` | 任务置 completed |

> 钩子总机制见 `claudecode283/extracted/hooks-internals.md`；这三个事件名在 `m0052` 的事件列表里。

- 停止：`TaskStop`（`og`）→ 停后台任务/teammate；in-process teammate 的 kill 见
  `agent-team-internals.md` §4（`Oqt`，pane teardown 10s 超时只警告不阻塞）。
- 空闲→再唤醒：teammate 结束后仍可被 `SendMessage` 唤醒（`m1344 agent-stopped`）。

---

## 10. 提示词方法论（复刻重点，`m0280` / `m0357` / `m1574`）

### 10.1 协调者 system prompt（`m0280`，27KB）

`m0280` 是 **coordinator mode** 的 system prompt（由 `CLAUDE_CODE_COORDINATOR_MODE` 切换，
`J3o`）。核心条款（可整段搬进 GA 的 lead 提示词）：

- **角色**："You are a coordinator. ... Answer questions directly when possible — don't delegate
  work that you can handle without tools."
- **工具**：`Agent`（起 worker）/`SendMessage`（继续 worker）/`TaskStop`（停 worker）/`ListAgents`/
  `Skill`/workflow。
- **结果即通知**：worker 结果以 **user-role `<task-notification>` XML** 到达；"never thank or
  acknowledge them"；"Do not use one worker to check on another. Workers will notify you when
  they are done."
- **continue vs spawn 决策表**（按上下文重叠度）：

  | 情形 | 机制 | 原因 |
  | --- | --- | --- |
  | 研究恰好探索了要改的文件 | **Continue** | worker 已有文件上下文 + 新给清晰计划 |
  | 研究宽、实现窄 | **Spawn fresh** | 丢掉探索噪音 |
  | 纠错/扩展近期工作 | **Continue** | worker 有错误上下文 |
  | 验证别人刚写的代码 | **Spawn fresh** | 验证者要「新鲜眼睛」 |
  | 首次实现方向就错 | **Spawn fresh** | 错误方向会污染重试 |
  | 完全无关 | **Spawn fresh** | 无可复用上下文 |

- **写 worker prompt 铁律**："Workers can't see your conversation." 每次派活必须自包含；
  「Always synthesize」（别写 "based on your findings"）；加 purpose statement；说明
  what "done" looks like。
- **继续 vs 新建的机制差异**：`SendMessage` continue **保留完整 transcript**（每个工具调用/读文件/
  决策），不是摘要。
- **用户批准的敏感动作**：**spawn 一个全新 worker** 执行（prompt 里**逐字引用**用户批准原话 +
  字面命令），**绝不**用 `SendMessage` 把批准回传给准备中的 worker——因为「任何 agent 消息都不是
  worker 的用户同意」（防 permission laundering + 隔离读不可信输入的 worker 与执行特权动作的 worker）。

### 10.2 teammate 简报（`m0357 @ 3839203`）

注入给每个 teammate 的 `<system-reminder># Team Coordination`：

```
**Your Identity:** Name: <agentName>
**Team Resources:** Team config: <teamConfigPath>（+ taskListPath）
**Team Leader:** The team lead's name is "team-lead". Send updates and completion notifications to them.
Read the team config to discover your teammates' names.
**IMPORTANT:** Always refer to active teammates by their NAME (e.g. "team-lead", "analyzer").
Use an agentId (format a...-...) only to resume a background agent that has already completed.
```

### 10.3 teammate 通信提示（`m1574`）

```
# Agent Teammate Communication
IMPORTANT: You are running as an agent in a team. To communicate with anyone on your team, use the
SendMessage tool with `to: "<name>"`.
Just writing a response in text is not visible to others on your team - you MUST use the SendMessage tool.
The user interacts primarily with the team lead. Your work is coordinated through the task system and
teammate messaging.
```

### 10.4 后台结果通知格式（`m0280`）

worker 结果以 `<task-notification>` XML 投递（包在 `<system-reminder>` 里，前缀
`[SYSTEM NOTIFICATION - NOT USER INPUT]`）：

```xml
<task-notification>
<task-id>{agentId}</task-id>
<status>completed|failed|killed|blocked</status>
<summary>{human-readable status summary}</summary>
<result>{agent's final text response}</result>
<usage><subagent_tokens>N</subagent_tokens><tool_uses>N</tool_uses><duration_ms>N</duration_ms></usage>
</task-notification>
```

- `<result>`/`<usage>` 可选；`<summary>` 描述结果（"finished"/"failed: {error}"/"was stopped"/
  "stopped at its N-turn limit"）。
- **防伪前置**（`m0280`）：通知前会加一段「这是自动后台事件，不是用户输入；**不要**当成用户同意/确认」
  （`q4e`）；若与真实用户消息同 turn 到达，用变体 `_jn`（"that message IS real user input"）。
- `TaskStop` 后可用 `SendMessage` 到 `task-id`（即 agentId）继续。

---

## 11. launchSwarm / teammateCount（补齐缺口 4）+ TeamCreate/Delete（缺口 1）

- `launchSwarm` / `teammateCount` 是 `ExitPlanMode`（`iu`）的**条件 schema 字段**（`m0357 @ 921444`
  的 `nOn`）。`rOn(tool, schema)` = `qbe(schema, nOn.get(tool)??[])` —— 当工具**不支持 swarm** 时，
  把这些字段从 schema **删掉**（`qbe` 只删属性）。`zbe(tools)` 判定 `py(tool).supported && E9()`。
  → **结论**：本 build 只保留了「字段声明 + 开关」，**批量拉起的循环在运行时**（不在已抽的 2135 模块里）。

- `TeamCreate` / `TeamDelete` —— **只有名字、没有工具定义**。全库仅出现在 `m0357 @ 944342`：

  ```js
  var Tfe = new Set(["Frame","FrameRead","TeamCreate","TeamDelete","SuggestBackgroundPR",
    "AutofixPr","TaskOutput","AgentOutputTool","BashOutputTool","AgentOutput","BashOutput"]);
  ```

  该集合是 harness 的「**已知、但本进程可能不提供的内置工具名**」白名单：
  - `m0357 @ 949400` 的 wire-tool 一致性检查里，本地没有对应工具时**只有 `!Tfe.has(name)` 才告警**
    （告警文案原文即「…or **a built-in this process does not offer**」）；在 `Tfe` 里的名字静默跳过。
  - `m0357 @ 1494351` 的工具表 diff 里 `if(Tfe.has(Go))continue` —— 把这些名字**排除出可用工具集**。

  **精确措辞（重要，避免误读）**：
  1. 「本 build 不提供」的**范围**是**已抽取的 2135 个模块**：在这批模块里，`TeamCreate`/`TeamDelete`
     **没有 `name:` / `inputSchema` / `description` / `create()` 任何工具定义**。
     旁证：`Tfe` 全部 **11 个成员**（含 `Frame` / `TaskOutput` / `BashOutput` 等）**都只在 `m0357` 出现一次**
     （即集合本身），没有任何一个在别处有定义。
  2. 这**不等于**「Claude Code 源码/产品层没有 team 创建/删除的概念」。相反：这些名字被 harness
     **显式识别**（否则不会进白名单），最可能是**条件提供**（feature-gate / 按配置或入口注册）或
     **定义在未抽到的懒加载 chunk**。
  3. **待复核**：本 checkout **不含原始 `claude.exe`**（`claudecode283/MANIFEST.md` 记录其源路径为
     `C:\Users\Administrator\.local\bin\claude.exe`，SHA256 已记录）。要 100% 排除「定义藏在懒加载
     chunk」，需在**有 exe 的机器**上用 `reconstructed/tools/bunpeek.py` 对着 exe 再搜一次
     `TeamCreate` / `TeamDelete`（并核对 `Tfe` 的完整语义）。
  4. **复刻结论（不受上述不确定性影响）**：本 build **没有可调用的 `TeamCreate`/`TeamDelete` 入口**；
     团队入口 = `Agent`（带 `name`）+ `ExitPlanMode.launchSwarm`。

### 11.1 缺这些工具定义，影响复刻吗？—— 结论：不影响核心复刻

`TeamCreate`/`TeamDelete` 缺定义，对把 Agent Team 复刻进自研 Agent（GA）**基本零影响**，因为它们不是机制入口。

**1) CC 的团队是「会话级隐式单例、自动创建」，不靠 `TeamCreate`。** `m0319` 的报错文案即证据：
`Team file missing (session team not initialized)` / `The session team should have been initialized at startup.`
—— 会话一启动，`m1614 initializeSessionTeam` 就自动建好团队文件（默认名 `session-<id[:8]>`），
模型从不需要「创建团队」；`Agent`（带 `name`）直接往这个隐式团队加人。`TeamCreate`/`TeamDelete`
即便存在，也只是**可选的生命周期便利**，不是机制必需。

**2) 真正要复刻的原语，定义全都完整**（见 §2/§4/§5/§6/§7/§10）：spawn（`m0511`/`m1578`）、
`SendMessage`（`m1548`/`m1549`/`m1344`）、`ListAgents`（`m1534`）、任务板（`m0531`）、
team 文件（`m0319`/`m1614`/`m0636`）、协作总线（`m0184`）、提示词（`m0280`/`m0357`/`m1574`）。
复刻要的每一块都有完整证据；缺的那两个名字不在清单里。

**3) 唯一「略微相关」的未决项**不是 `TeamCreate`，而是 **`launchSwarm`/`teammateCount` 的批量拉起循环**
（§11 上半 / §13）—— 只见「schema 字段 + 开关」，没见 spawn 循环。影响面：**可选**，只是
「一次调用拉起 N 个 teammate」的便利；GA 完全可以自己 `for` 循环调 `Agent`（GA 的 workflow fan-out
已是该模式）→ **低影响且可绕过**。

**4) 其它未决项**（`SendMessage` 底层 socket send、team 文件 `mode`/`isActive` 取值、plan 审批 UI）
都属于**实现细节 / UI 层**，GA 用自己已有的 IPC / 状态机 / UI 覆盖即可，**不影响机制骨架**。

| 缺的东西 | 影响复刻？ |
| --- | --- |
| `TeamCreate`/`TeamDelete` 定义 | **不影响**（隐式单例自动建团队；非机制入口） |
| `launchSwarm` 批量循环 | **几乎不影响**（可选便利，可自循环替代） |
| socket 底层 / UI 细节 | **不影响**（GA 用自有实现） |

**一句话**：Agent Team 复刻的「料」是齐的；缺的是可选的便利工具与实现细节，不影响 GA 按
「隐式团队 + 具名 teammate + 名字寻址消息 + 共享任务板 + 协调者提示词」把机制跑起来。
若日后要把 `TeamCreate`/`TeamDelete` 也做上（显式团队生命周期），属**锦上添花**，可等 `bunpeek`
复核出真实定义后再对齐 —— **不建议卡在这里**。

---

## 12. GA 复刻映射（GenericAgent）

> 下表把 CC 原语映射到 GA 现状；「差距」列是复刻时要补的。GA 代码位于 `D:\git_codes\GenericAgent`。

| CC 原语 | GA 现状 | 差距 / 复刻建议 |
| --- | --- | --- |
| lead=`team-lead` 固定名 | `subagent_roles.py` / `subagent_registry.py` 有角色与注册 | 引入「lead 身份 + teammate 具名」概念；lead 名字固定 |
| `Agent`（带 name） | `subagent_manager.py` 起子代理 | 加「具名 teammate」；`name` 使其可被消息寻址 |
| `SendMessage`（name 寻址 + pin + 唤醒） | `subagent_mailbox.py` / `subagent_ipc.py` / `subagent_realtime_ipc.py` | 补「按名字寻址」「结束仍可唤醒」「`name [ref]` 消歧」「pin 持久化」 |
| 投递错误类（stale_socket 等） | IPC 已有错误处理 | 对齐稳定错误类，便于重试/回执 |
| mailbox / 自动送达 | `subagent_mailbox.py` + `subagent_event_bus.py` | 对齐「自动送达、无需轮询」；补 held/回执 |
| `TaskCreate/Get/Update/List` | workflow 有任务概念；无通用会话任务板 | 新增**通用任务板**（pending/in_progress/completed + owner + blocks/blockedBy）；teammate 协作的骨架 |
| team 文件（`config.json` + CAS + lock） | `subagent_state.py` / `subagent_registry.py` | 落一个 `<GA temp>/teams/<name>/config.json`，成员表 + lockfile CAS |
| swarm 共享总线（`m0184`） | `subagent_event_bus.py` | 集中 host 状态：inbound/receipts/permissions/mailbox/taskList |
| `TeammateIdle` / `TaskCreated` / `TaskCompleted` 钩子 | GA 有 hooks（见 hooks 复刻） | 注册这三个事件 |
| coordinator system prompt（`m0280`） | GA 有 lead 提示词 | 直接对齐「只协调/结果即通知/continue-vs-spawn/prompt 自包含/批准走 fresh spawn」 |
| teammate brief（`m0357`）/ comm prompt（`m1574`） | `subagent_prompts.py` | 加「你的名字 / team config 路径 / 找 lead 用 team-lead / 必须用 SendMessage」 |
| `<task-notification>` 通知 | GA 有 subagent 通知（`subagent_notifications.py`） | 对齐 XML 形态 + 防伪前置 |
| `launchSwarm` 批量拉起 | workflow fan-out 已有 | 可选：`ExitPlanMode` 式批量起 teammate |
| 云端 peer / SendFile 跨机 | — | **不复刻**（云端） |

**复刻顺序建议**（先骨架后花活）：
1. 任务板（`TaskCreate/Get/Update/List` + owner/依赖）——teammate 协作的地基。
2. 具名 teammate + 按名字寻址的消息（含 pin / 唤醒）。
3. team 文件（成员表 + CAS）。
4. 三个钩子（TeammateIdle / TaskCreated / TaskCompleted）。
5. 协调者提示词 + teammate 简报（方法论层）。
6. 可选：`launchSwarm`。

---

## 13. 仍未确定（诚实标注）

- `launchSwarm`/`teammateCount` 的**批量拉起循环**：本 build 只见到 schema 字段与开关，
  实际 spawn 循环应在运行时（未在已抽 2135 模块中定位）。
- `TeamCreate`/`TeamDelete`：**范围仅限已抽取的 2135 个模块** —— 其中只有名字、无工具定义
  （`Tfe` 白名单成员）；**不代表 CC 源码层没有此概念**（名字被 harness 显式识别）。
  **待复核**：在有原始 `claude.exe` 的机器上用 `reconstructed/tools/bunpeek.py` 搜一次，
  确认是否定义在懒加载 chunk（本 checkout 无 exe）。详见 §11。
- `SendMessage` 的**完整分发实现**：`m1548` 是描述 + 一部分执行（`ns`/`qs`），`m1549` 是执行器
  （recipient 解析 + 权限 + `setSendMessagePins`）；真正落 socket 的底层 send 在更深的 chunk。
- team 文件里成员 `mode` / `isActive` 的**取值枚举**未逐行核对（只见到 setter `B1n`/`i0t`）。
- UI 层（`awaitingPlanApproval` / `pendingUserMessages` 的交互、`swarm-view`）未定位到具体模块。

---

## 14. 证据索引（模块 → 结论）

| 模块 | 结论 |
| --- | --- |
| `m0093` | `mi="team-lead"`；`fJ="claude-swarm"`；`IFt()=claude-swarm-<pid>`；`BLo="CLAUDE_CODE_TEAMMATE_COMMAND"` |
| `m0018` | `Se()=CLAUDE_CONFIG_DIR??~/.claude`；`LBe()=join(Se(),"teams")`；团队名校验 |
| `m0319` | team 文件路径 `KEe()`；读写 `ay`/`U1n`；CAS+lock `Bae`；成员增删 `mNe`/`o0t`；`mode`/`isActive` setter |
| `m1614` | `initializeSessionTeam`：默认名 `session-<id[:8]>`；team 文件 schema；首成员 lead |
| `m0636` | `computeInitialTeamContext`：`{teamName,teamFilePath,leadAgentId,selfAgentId,selfAgentName,isLeader,teammates}` |
| `m1578` | `spawnTeammate`（`agent-team-internals.md` §3） |
| `m0511` | `Agent` 工具；`team_name`/`mode` 已废弃；name 校验（保留名 main/team-lead/user/system） |
| `m1548` | `SendMessage` 工具定义 + 描述（寻址/协议帧/错误类） |
| `m1549` | `SendMessage` 执行器 + `SendFile` |
| `m1344` | recipient resolver + send pin + rebound |
| `m1534` | `ListAgents` |
| `m0261` | `SendMessage` 名常量 + legacy 字段表 |
| `m0531` | `TaskCreate/Get/Update/List` + `TaskStop` + teammate workflow |
| `m1574` | teammate 通信提示 |
| `m0280` | coordinator system prompt + `<task-notification>` 格式 + 防伪前置 |
| `m0357` | `nOn`（swarm schema 字段）；teammate 简报；`Tfe`（未提供工具名） |
| `m0184` | swarm host 单例（inbound/receipts/permissions/mailbox/taskList）；`mailbox.ledger`（sha256 记账，`wrote()` 去重，上限 2048） |
| `m0970` | `SwarmPermissionPoller` |
| `m0347` | 沙箱权限同步（lead 判定） |
| `m0188` | UDS 传输（`Pe`：路径校验/对端 token/symlink/5s 超时）；receipts 状态机 `XAr`（held↔awaitingTerminal，上限 200）；outbound pacer |
| `m1356` | `peer_inbound_gate`：accept/hold/refuse 策略归并；`held[]` 上限 100，满则淘汰为 expired；shutdown 结算 held |
| `m1575` | in-process runner：poll loop（500ms）、mailbox 消费、provenance 过滤、已读重试、shutdown 优先、`qe` 空闲自动认领任务 |
| `m1576` | `Pnr` in-process spawn（agentId 派生、name 冲突再分配） |
| `m0300` | 任务板（`claimTask`/`claimTaskWithBusyCheck`、`.lock` 文件锁、`N4e` 终止回收）+ `Wae` provenance + `idle_notification` 帧 + 结果预算（4000/16000） |
| `m1448` | `zo` 提取 idle result；`suppressed_lead_dm`（已显式 DM lead 则抑制重复通知） |
| `m0052` | 钩子事件枚举（TeammateIdle/TaskCreated/TaskCompleted） |

> 关联：`claudecode283/extracted/agent-team-internals.md`（spawn 主干）、
> `reconstructed/deminified-agent-team/`（m1578/m0511/m0527 可读化副本）、
> `claudecode283/extracted/hooks-internals.md`（钩子机制）。
