# GA Ask 档行内阻塞审批：Claude Code / Codex 源码调研与落地建议

日期：2026-07-21  
范围：**仅**主 agent `ask` 档的「工具级、阻塞式、行内审批 UI」；不含档位持久化、不含 workflow 子 agent 审批（见 `docs/ga_permission_modes_research_2026-07-20.md` §14 大件 #2/#3）。  
对照源码：

| 产品 | 本地路径 |
|------|----------|
| Claude Code | `D:\git_codes\claude-reviews-claude\claude-code-fork\src` |
| Codex | `D:\git_codes\codex\codex-rs` |

---

## 0. 一句话结论

两边都把「需要人拍板」做成 **async 挂起 + 带 requestId 的请求/应答 + 单例/队列 UI**，而不是像 GA 现在这样 **立刻 `approval_required` 返回并结束工具**。

| | Claude Code | Codex | GA 现状（P0→**P1 已交付**） |
|--|-------------|-------|---------------|
| 策略结果 | `allow` / `deny` / **`ask`** | 策略 → **`NeedsApproval`** | `allow` / `deny` / **`ask`** |
| `ask` 时执行路径 | **Promise 挂起**，等用户 | **oneshot 挂起**，等用户 | **`permission_runtime` Future 挂起**，等 accept/deny |
| 跨层协议 | 同进程 React 队列（SDK 另有 control schema） | Event/Op 双向队列 + app-server | JSONL `permission_request` / `permission_response` / settled |
| UI | 按工具类型的 PermissionRequest 组件 | bottom_pane `ApprovalOverlay` 列表选择 | **统一** `approvalPanel` accept/deny（+ 切档面板仍独立） |
| 用户选项 | Yes / Yes don't ask again / No（+ 工具特化） | Accept / Accept for session / Cancel（+ 策略修正项） | **仅 accept / deny**（P1 拍板） |
| 取消 | abortController + onAbort | Abort / channel 关闭 → Abort | `/stop` → `cancel_all()` 按 deny 解开 pending |

**P1 主路径已按本文落地（2026-07-21）；验证进度见 §12。**

### 0.1 产品决策（2026-07-21 拍板，简化）

> **所有工具共用同一套审批 UI；选项仅 `accept` / `deny`。**  
> 不做按工具特化页，不做 Allow for session / Always，不做 diff/命令高亮专用视图（摘要一行工具名 + 短 args 即可）。

| 项 | 决策 |
|----|------|
| UI 套数 | **1 套**（统一 overlay/列表） |
| 适用工具 | **所有** 被 policy 判为 `ask` 的工具（静态写/执行 + MCP 写类等） |
| 用户选项 | **`accept`**（仅本次执行该 tool call）/ **`deny`**（不执行，结果回模型） |
| Esc | = **deny**（显式拒绝，禁止静默放行） |
| `/stop` | 取消全部 pending → 按 **deny** 收尾（见 §0.2，与「用户点拒绝」同结果、不同原因） |
| session/always | **P1 不做**（以后若吵再加，不进本切片） |
| 无 UI / headless | **deny**（fail-closed；见 §0.2） |

**可实现性：能。** 比原调研里的三选项 + session 集合更简单；P1 只剩「Future 等待 + 两条协议 + 一个二选一面板」。无需为简化再开一轮源码调研。

### 0.2 「无 UI → deny」与「`/stop` → deny」——两件不同的事

两者结果都是 **本次工具不执行**（`decision=deny` 语义），但 **触发原因与目的不同**，实现时不要混成一个模糊开关。

| | **无 UI / headless** | **`/stop`（或 abort）时仍有 pending** |
|--|----------------------|----------------------------------------|
| **何时** | 需要审批，但 **没有审批通道**（无 Ink、`emit is None`、CLI/CI、bridge 未接 UI） | 已经在 **阻塞等待** 用户点 accept/deny 时，用户要求 **停掉整轮任务** |
| **目的** | **fail-closed**：没人看着绝不默认放行，否则 ask 档名存实亡 | **解开挂起**：否则 agent 线程一直 `wait`，stop 不干净；停了之后也绝不能再执行未批工具 |
| **和权限档** | 不改 mode | 不改 mode |
| **和「用户点拒绝」** | 用户根本没机会点 | 用户可能连面板都没操作；是 **任务取消** 附带把 pending 按 deny 结算 |
| **模型侧** | 工具结果 = 拒绝/错误（可注明 headless 无法审批） | 工具不执行 + 整轮 stop；与单纯 deny 后「模型改方案继续」不同——stop 后本轮结束 |

```text
无 UI：  ask 工具 → 无 emit → 直接 deny → 不执行
有 UI：  ask 工具 → emit permission_request → wait
              ├─ 用户 accept → 执行
              ├─ 用户 deny / Esc → 不执行，模型可改方案（本轮可继续）
              └─ 用户 /stop   → cancel_all → 各 pending 按 deny 解开 → 本轮停
```

**实现要点**：`cancel_all()` 只负责把 pending Future 设为 `deny` 并可选发 `permission_request_settled`；**整轮停止**仍由现有 `stop_sig` / abort 路径负责。不要把「stop」实现成「只 deny 一个工具却继续跑后面的 tool」。

---

## 1. Claude Code：机制拆解

### 1.1 调用链（主路径）

```
toolExecution.runToolUse / checkPermissionsAndCallTool
        │
        ▼
canUseTool(...)   ← useCanUseTool 注入的 Promise 函数
        │
        ├─ hasPermissionsToUseTool(...) → allow | deny | ask
        │
        └─ behavior === "ask"
                │
                ▼
        handleInteractivePermission(...)
                │  pushToQueue(ToolUseConfirm { onAllow, onReject, onAbort, ... })
                │  不 return 同步结果；通过 resolve() 结束外层 Promise
                ▼
        REPL / PermissionRequest 渲染队头
                │  用户选择
                ▼
        onAllow / onReject → resolveOnce(decision) → 工具继续 call 或拒绝消息回模型
```

关键文件：

| 文件 | 职责 |
|------|------|
| `hooks/useCanUseTool.tsx` | 把权限决策做成 **`Promise<PermissionDecision>`**；allow 直接 resolve，ask 转 interactive |
| `hooks/toolPermission/handlers/interactiveHandler.ts` | 入队 + 回调；**resolve-once / claim** 防双开；abort / bridge / channel 旁路 |
| `hooks/toolPermission/PermissionContext.ts` | 队列抽象 `push/remove/update`；`createResolveOnce`；`cancelAndAbort`；规则持久化钩子 |
| `utils/permissions/permissions.ts` | 规则表 + mode → `allow\|deny\|ask`（**不负责 UI**） |
| `components/permissions/PermissionRequest.tsx` | 按 **Tool 类型** 分发专用审批 UI；统一 `ToolUseConfirm` 契约 |
| `components/permissions/FallbackPermissionRequest.tsx` | 通用三选项：yes / yes-dont-ask-again / no |
| `components/permissions/FilePermissionDialog/permissionOptions.tsx` | 文件场景：accept-once / accept-session / reject（+ 路径 scope） |
| `screens/REPL.tsx` | 持有 `toolUseConfirmQueue` 状态，驱动队头渲染 |

### 1.2 阻塞语义（最重要）

`CanUseToolFn` 类型本质是：

```ts
(tool, input, ctx, assistantMessage, toolUseID) => Promise<PermissionDecision>
```

- 工具执行路径在 `await canUseTool(...)` 处 **真正挂起**。
- `ask` **不是**「返回一个 status 给模型然后结束」；而是 **不 resolve 直到用户（或 abort/classifier）决出**。
- 决议后：
  - `allow` → 同一条执行链继续 `tool.call`
  - `deny` / 用户拒绝 → 构造拒绝文案回消息流，**本 tool 不执行**

### 1.3 并发与「只 resolve 一次」

`interactiveHandler` + `createResolveOnce`：

1. **`claim()`**：异步回调里先原子占坑，再 await 副作用，关掉「检查 isResolved → await → resolve」窗口。  
2. **`onUserInteraction`**：用户开始按键后，取消 classifier 自动批准竞态（grace 200ms 防误触）。  
3. **多源决议**：本地 UI / bridge 远端 / MCP channel / classifier / hook 都可能 resolve；先 claim 者赢，其余 no-op。  
4. **abort**：`onAbort` → deny + 可选 `abortController.abort()`（整轮取消）。

对 GA 的直接含义：**阻塞点必须有 requestId + 单次完成语义**；`/stop` 必须能找到 pending 并统一 deny/abort。

### 1.4 UI 形态

- **队列表**（React state），通常只展示 **队头**；并行 tool_use 时后续项排队。  
- **按工具特化** 的审批页（Bash / FileWrite / FileEdit / MCP fallback…），共享 `PermissionRequestProps`：
  - `toolUseConfirm.onAllow(updatedInput, permissionUpdates, feedback?)`
  - `toolUseConfirm.onReject(feedback?)`
  - `onDone` / `onReject` 清队列项  
- 通用 Fallback 选项（`FallbackPermissionRequest`）：

| 选项 | 效果 |
|------|------|
| Yes | 仅本次 allow |
| Yes, and don't ask again | allow + `addRules` → localSettings 持久 allow 该 tool |
| No | reject + 可选 feedback |

文件类另有 **accept-session**（会话内同类路径/目录），比「整工具 always」更细。

### 1.5 拒绝如何回模型

`PermissionContext.cancelAndAbort` 生成标准拒绝 message（含子 agent 变体、可选「纠偏记忆」hint），行为上仍可能标 `behavior: 'ask'` 携带 message——重点是 **工具未执行**，模型收到的是 **拒绝说明**，不是半执行结果。

### 1.6 不建议 GA 一期照搬的 CC 复杂度

- Bash classifier / auto-mode / YOLO classifier 竞态  
- Bridge / 手机 channel 旁路批准  
- Swarm worker / coordinator 权限  
- 完整 PermissionRule 编辑器与多 destination（user/project/local/session）  

这些是产品力，但 **P1 主 agent 阻塞审批不需要**。

---

## 2. Codex：机制拆解

### 2.1 调用链（主路径）

```
工具 runtime（shell / apply_patch / network / …）
        │  策略判定 NeedsApproval
        ▼
Session::request_command_approval(...)   // 或 request_patch_approval
        │  oneshot::channel()
        │  turn_state.insert_pending_approval(approval_id, tx)
        │  send_event(ExecApprovalRequestEvent { call_id, approval_id, command, ... })
        ▼
await rx_approve   // 工具任务在此挂起
        │
        │  （UI / app-server 另一侧）
        │  用户选择 → Op / notify_approval(approval_id, ReviewDecision)
        │  remove_pending_approval → tx.send(decision)
        ▼
ReviewDecision → 继续执行 / 拒绝 / Abort
```

关键文件：

| 文件 | 职责 |
|------|------|
| `core/src/session/mod.rs` | `request_command_approval` / `request_patch_approval` / `notify_approval` |
| `core/src/state/turn.rs` | `pending_approvals: HashMap<String, oneshot::Sender<ReviewDecision>>`（同类还有 user_input / elicitation） |
| `protocol/src/protocol.rs` | `ReviewDecision` 枚举；SQ/EQ 会话协议说明 |
| `protocol` approvals / app-server protocol | `ExecApprovalRequestEvent`、`CommandExecutionApprovalDecision` 等 |
| `tui/src/approval_events.rs` | TUI 侧规范化请求结构（可延迟到 stream 结束后再弹） |
| `tui/src/bottom_pane/approval_overlay.rs` | **审批 modal**：队列 + ListSelectionView + 决策路由 |
| `tui/src/bottom_pane/pending_thread_approvals.rs` | 其他 thread 有 pending 时的提示条 |
| `tui/src/chatwidget/*approval*` | 事件接入、历史单元格「已批准/已拒绝」 |

### 2.2 阻塞语义

`request_command_approval` 核心模式（简化）：

```rust
let (tx_approve, rx_approve) = oneshot::channel();
turn_state.insert_pending_approval(effective_approval_id, tx_approve);
self.send_event(... ExecApprovalRequestEvent { ... }).await;
rx_approve.await.unwrap_or(ReviewDecision::Abort)
```

要点：

1. **先登记 oneshot，再发事件**——避免 UI 极快回复时丢应答。  
2. **key** = `approval_id` 或回退 `call_id`。  
3. 接收端 `notify_approval` **remove + send**；找不到 key 只 warn。  
4. `rx` 被 drop / 无发送者 → **`Abort`**（fail-closed，不是静默 allow）。  
5. 同 turn 可有多类 pending（approval / user_input / elicitation / dynamic tool），结构统一。

这与 GA 的 Python 世界可直接类比：

```text
threading.Event / concurrent.futures.Future / queue
pending[request_id] = future
emit permission_request
result = future.result(timeout?)  # 或 wait + stop_sig
```

### 2.3 决策枚举（比 CC 更偏「执行/补丁」）

`ReviewDecision`（protocol）：

| 变体 | 含义 |
|------|------|
| `Approved` | 仅本次 |
| `ApprovedForSession` | 会话缓存自动批同类 |
| `ApprovedExecpolicyAmendment` | 批 + 写入 execpolicy 前缀规则 |
| `NetworkPolicyAmendment` | 批/拒 + 主机网络规则 |
| `Denied` | 拒，agent 可改方案继续 |
| `Abort` | 拒且停到用户下一条 |
| `TimedOut` | 自动审查超时 |

TUI `ApprovalOverlay` 再映射到 app-server 的：

- `CommandExecutionApprovalDecision::{Accept, AcceptForSession, Cancel, …}`  
- `FileChangeApprovalDecision`  
- MCP elicitation 专用 Cancel（**Esc = Cancel，禁止静默 continue**）

### 2.4 UI 形态

`ApprovalOverlay` 契约（模块注释写得很清楚）：

1. 选择 **必须** 发出显式 decision 事件回 app。  
2. MCP elicitation 的 Esc **固定 Cancel**，避免「关掉 = 默认同意」。  
3. **不**在 overlay 里做安全评估，只展示 + 路由。

实现特征：

- `current_request` + `queue: Vec<ApprovalRequest>`  
- `enqueue_request` / 解决后 `advance_queue`  
- 请求类型：`Exec` | `Permissions` | `ApplyPatch` | `McpElicitation`  
- 列表项带 shortcut；footer hint  
- 跨 thread：`PendingThreadApprovals` 提示「别的 agent 在等你」

与 CC 对比：Codex UI **更统一**（一种 overlay + 按请求类型换 options），CC **更特化**（每个 Tool 一个 React 页）。  
**GA 更接近 Codex，并再砍一刀**：bridge 已是事件协议 → **统一 ApprovalPanel**；选项固定 **accept/deny**；仅展示 toolName + 短 argsPreview，**不按 tool 换选项集、不做专用页**。

### 2.5 协议风格（对 GA bridge 极相关）

Codex 整体是 **Submission Queue / Event Queue**：

- 下行：Agent → UI 的 `ExecApprovalRequest` 事件  
- 上行：UI → Agent 的 `Op::ExecApproval { id, decision }`（经 `notify_approval`）

GA 的 Ink bridge 已是 JSONL 命令/事件，**天然适合抄这套**：

```text
bridge → UI : permission_request
UI → bridge : permission_response
```

而不是把审批塞进 React 同进程闭包（CC 模式在 GA 上不成立：Python agent 与 Node Ink **分进程**）。

---

## 3. 两边对照：GA 该抄什么

### 3.1 必须抄（P1 成立条件）

| 模式 | 来源 | GA 落地 |
|------|------|---------|
| **await 点在工具执行前** | CC + Codex | `dispatch` 遇 `ask` 时 **阻塞**，允许后再 `do_*` |
| **requestId + pending map** | Codex oneshot map | `permission_runtime`：`{id: Future}` |
| **先注册等待再发请求** | Codex | 防 UI 秒回丢包 |
| **fail-closed** | Codex `unwrap_or(Abort)` | 无 UI / stop 清 pending → **deny**，永不默认 allow（§0.2） |
| **单次完成** | CC `claim/resolveOnce` | future 只 set 一次；重复 response 忽略 |
| **显式决策枚举** | 两边 | GA 再简化为仅 **`accept` / `deny`**（见 §0.1） |
| **Esc/取消 ≠ 静默同意** | Codex elicitation | Esc / `/stop` → **deny** |
| **队列** | 两边 | 连发 tool 时 UI 只显示一个，其余排队（或串行 dispatch 则单 pending） |

### 3.2 建议抄（体验）

| 模式 | 更像谁 | 说明 |
|------|--------|------|
| **统一** bottom overlay + **二选一** | **Codex** 再砍一刀 | 所有工具同一面板；Accept / Deny 即可 |
| 工具摘要（工具名 + 短 args） | 两边 | 一行/几行文本截断；**不要** diff / bash 高亮专用页 |
| 历史一行「已允许/已拒绝 tool X」 | Codex history cell | 可选，P1 有更好 |
| deny 文案回模型 | **CC** | 固定模板，引导模型改方案而非重试同一写 |

### 3.3 明确不抄 / 不做（P1）

| 项 | 原因 |
|----|------|
| OS sandbox / seatbelt | 产品决策已否 |
| Classifier 自动批 | 复杂度高；GA 无对应栈 |
| Allow for session / Always / 规则落盘 | 用户要求 P1 只要 accept/deny；持久化属大件 #2 |
| 按 Tool 拆多套审批组件 | **产品明确不要**；一律同一套 UI |
| 跨 thread agent 审批条 | 主 agent P1 单会话足够；workflow 属大件 #3 |
| Bridge 手机 channel 远程点同意 | 无需求 |

---

## 4. GA 缺口（实现前快照 → P1 后状态）

> 下表「缺口」列保留实现前描述；**状态**列标 P1 后结果。细节与 live 证据见 §12。

| 层 | 实现前 | 缺口（当时） | P1 后 |
|----|--------|--------------|-------|
| `permission_policy.evaluate` | 已能返回 `ask` | 无 | ✅ 不变 |
| `ga.GenericAgentHandler.dispatch` | 立刻 `approval_required` 并 return | wait → accept 执行 / deny 回模型 | ✅ 阻塞 wait |
| `agentmain` | 切档 + hint | pending + stop 取消 | ✅ runtime + abort→cancel_all |
| `ink_bridge` | 仅切档协议 | request/response | ✅ 齐 |
| Ink UI | 仅切档面板 | 统一审批 overlay | ✅ `approvalPanel` |
| 无 UI / 单测 | spy handler | fail-closed deny | ✅ |

~~半交付~~ **已关闭**：主会话 ask = 弹统一 accept/deny → 同意才执行。

---

## 5. 推荐架构（主 agent P1，**极简 accept/deny**）

> 设计取向：**协议与等待模型偏 Codex**；UI **一套统一面板 + 仅 accept/deny**（比 CC/Codex 默认选项更少）；拒绝文案可用固定中文/英文模板。

### 5.1 模块

```
permission_policy.py          # 已有：mode → allow|ask|deny（不动大逻辑）
permission_runtime.py         # 新增：pending map、wait/resolve、stop 取消（无 session 集合）
ga.py dispatch                # ask 时调 runtime.wait；accept 后才执行工具
frontends/ink_bridge.py       # emit permission_request；处理 permission_response
frontends/ink-ui/
  protocol.ts                 # 类型扩展
  approvalPanel.ts            # 纯函数状态机：二选一 + Esc=deny
  App.tsx                     # 挂 overlay、按键、发 response
```

**不要**把 Future 塞进 `permission_policy`；策略保持纯函数，等待属于 runtime。

### 5.2 协议草案（JSONL）— 仅 accept / deny

```ts
// bridge → UI
{
  type: 'permission_request',
  requestId: string,          // uuid
  toolName: string,
  argsPreview: string,        // 已截断的一行/短文本摘要（非完整 content）
  reason: string,             // 如 approval_required:static_write_or_execute
  mode: 'ask'
}

// UI → bridge
{
  type: 'permission_response',
  requestId: string,
  decision: 'accept' | 'deny'
}

// 可选：stop/超时后通知 UI 收起
{
  type: 'permission_request_settled',
  requestId: string,
  outcome: 'resolved' | 'cancelled'
}
```

与既有切档协议正交：

- `permission_status` / `set_permission_mode` —— **mode 切换**  
- `permission_request` / `permission_response` —— **单次工具审批（accept|deny）**

### 5.3 Runtime 伪代码

```python
# permission_runtime.py（示意）
class PermissionRuntime:
    def __init__(self):
        self._pending = {}          # id -> Future
        self._emit = None           # bridge emit hook；None = headless
        self._lock = threading.Lock()

    def wait_for_decision(self, tool_name, args_preview, reason, *, stop_event) -> str:
        # returns "accept" | "deny"
        if self._emit is None:
            return "deny"  # headless fail-closed
        req_id = new_id()
        fut = Future()
        with self._lock:
            self._pending[req_id] = fut
        self._emit({
            "type": "permission_request",
            "requestId": req_id,
            "toolName": tool_name,
            "argsPreview": args_preview,
            "reason": reason,
            "mode": "ask",
        })
        while not fut.done():
            if stop_event.is_set():
                self.cancel(req_id)
                return "deny"
            try:
                return fut.result(timeout=0.1)
            except TimeoutError:
                continue
        return fut.result()

    def resolve(self, req_id, decision: str) -> bool:
        if decision not in ("accept", "deny"):
            decision = "deny"
        with self._lock:
            fut = self._pending.pop(req_id, None)
        if not fut or fut.done():
            return False
        fut.set_result(decision)
        return True

    def cancel_all(self) -> None:
        ...
```

`dispatch` 改造要点：

```text
decision = mode_policy.evaluate(...)
if decision.action == "allow": 继续执行
if decision.action == "deny": 现有 error 路径
if decision.action == "ask":
    user = runtime.wait_for_decision(...)   # accept | deny
    if user == "accept":
        继续执行 do_* / MCP
    else:
        返回 error/deny（文案：用户拒绝，勿盲目重试同一写/执行）
```

**线程模型注意（GA 特有）**：

- `agent_runner_loop` / `dispatch` 多在 agent 工作线程；Ink bridge 读 stdin 在另一线程。  
- `Future` + bridge 线程 `resolve` 是自然适配（≈ Codex oneshot 跨任务）。  
- **禁止**在 asyncio 假设下写；GA 主路径是线程 + generator `yield`。  
- 若 `dispatch` 是 generator：wait 期间可 `yield` 一行 `[Permission] waiting: file_write`，保持 UI 有输出。

### 5.4 Ink UI 状态机（**同一套，仅 accept/deny**）

```text
idle
  │ permission_request（任意 toolName，UI 不分支）
  ▼
active { requestId, toolName, argsPreview, selected: accept|deny }
  │ ↑/↓ 或 j/k 切换  Enter → permission_response
  │ Esc / 选 deny+Enter → decision=deny
  │ 新 request → 入队，当前未决完不打断（GA 若串行 tool 则几乎总是单条）
  ▼
idle
```

展示模板（所有工具相同）：

```text
需要批准
  工具: file_write
  参数: path=... content=<truncated>
  > 允许 (accept)
    拒绝 (deny)
  Enter 确认 · Esc 拒绝
```

与 `/permissions` 切档面板冲突时：

- **审批 overlay 优先**  
- 打开审批时关闭切档面板，避免双 modal

### 5.5 `/stop` / abort 与 pending 的关系（详见 §0.2）

| 事件 | 行为 |
|------|------|
| 用户 `/stop` 或 abort | `runtime.cancel_all()` → 每个 pending Future 以 **`deny`** 完成（工具不执行）→ UI 收起；**同时**既有 stop 路径结束本轮 |
| bridge 断开 | 等同无审批通道：pending fail-closed **deny** |
| 重复 `permission_response` | ignore（只 resolve 一次） |
| 未知 `requestId` | ignore + 可选 log |

注意：这里的 `deny` 表示 **「本次 tool call 不执行」**，不是改 `permission_mode`，也不是永久拉黑该工具名。

### 5.6 Headless / 无 UI / 单测（详见 §0.2）

| 场景 | 行为 |
|------|------|
| 无 emit 钩子（`emit is None`） | `ask` → **直接 deny**，不创建 pending、不发事件 |
| Ink 未开 / bridge 无消费者 | 同 fail-closed deny（实现上可与无 emit 合并，或短超时后 deny——P1 优先「无 emit 即 deny」，**不做**长超时自动 allow） |
| 单测 | 注入 mock：`resolve(requestId, "accept")` 或测试专用 `auto_responder=lambda req: "accept"` |
| CI | 默认 fail-closed，禁止依赖真实 TTY |

---

## 6. 与「切档 UI」「workflow_approve」的边界

| 能力 | 是否本切片 | 说明 |
|------|------------|------|
| `/permissions` 三档切换 | 已有 P0 | mode 级 |
| Full Access 二次确认 | 已有 P0 | 进档确认，非 tool |
| **tool 行内审批（accept/deny）** | **本切片 P1** | 本文 |
| session allow / always / 档位落盘 | 否（大件 #2） | **P1 明确不做**；每次 ask 都弹 |
| `workflow_approve` | 否 | run 级脚本审批，正交 |
| workflow child ask | 否（大件 #3） | 依赖本协议稳定后再做 |

---

## 7. 分阶段实现建议（仅 P1，accept/deny）

> **2026-07-21**：下列切片 **均已交付主路径**；验收证据见 **§12**。

### Slice P1a — 协议 + runtime 阻塞 ✅

1. `permission_runtime.py` + 单测（accept/deny/resolve 一次/cancel_all/fail-closed）  
2. `dispatch` 接 runtime；emit 钩子由 bridge 注入  
3. bridge：`permission_request` / `permission_response`（decision 仅 accept|deny）  
4. 无 UI → deny；单测注入 mock `resolve(accept)`  

**验收**：ask 档下 `file_write` 在 `accept` 后 **真的写出文件**；`deny` 不落盘。任意其它 mutating 工具走同一路径。→ **live L1–L3 已过**。

### Slice P1b — Ink **同一套** 审批 UI ✅

1. `approvalPanel.ts`：二选一状态机 + Esc=deny + 单测  
2. `App.tsx`：所有 `permission_request` 同一组件渲染（**不** `switch(toolName)`）  
3. 与 `/permissions` 互斥；审批优先  

**验收**：切到 ask → 触发写/执行 → 同一弹层 → accept 后执行成功。→ **代码 + 单测已过**；真键盘 E2E 仍可选。

### Slice P1c — 小硬化（仍保持简单） ✅ 主路径

1. argsPreview 截断（长度上限）  
2. stop 取消竞态单测  
3. deny 回模型固定文案  

**不做**：session allow、always、按工具换 UI、diff 预览。

---

## 8. 风险与决策点（已按简化默认）

| # | 问题 | 默认（已拍板/建议） |
|---|------|---------------------|
| 1 | wait 会挂起当前 tool 链？ | **是**（阻塞语义）；`/stop` 必须 cancel → deny |
| 2 | 多 tool？ | 串行则单 pending；若并行则 queue，UI 仍同一套 |
| 3 | 只要 accept/deny？ | **是**（§0.1） |
| 4 | deny 后是否 abort 整轮？ | **否**，仅本 tool 拒绝，模型可改方案；`/stop` 才停整轮 |
| 5 | 无 Ink 的 `ga cli`？ | **deny** fail-closed（§0.2） |
| 6 | 旧 `approval_required` status？ | 有 runtime 阻塞后不再用它表示「等人」；无 runtime / 无 emit 时直接 deny/error |
| 7 | `/stop` 与用户点 deny 的差别？ | 结果都是工具不执行；stop 还结束本轮，点 deny 则本轮可继续（§0.2） |

---

## 9. 源码索引（深挖时按此打开）

### Claude Code

- `src/hooks/useCanUseTool.tsx` — Promise 门面  
- `src/hooks/toolPermission/handlers/interactiveHandler.ts` — 入队与多源 resolve  
- `src/hooks/toolPermission/PermissionContext.ts` — resolveOnce、队列、拒绝文案  
- `src/utils/permissions/permissions.ts` — 规则决策  
- `src/components/permissions/PermissionRequest.tsx` — 组件分发  
- `src/components/permissions/FallbackPermissionRequest.tsx` — 通用选项  
- `src/components/permissions/FilePermissionDialog/permissionOptions.tsx` — once/session  
- `src/services/tools/toolExecution.ts` — 执行前 `canUseTool`  

### Codex

- `codex-rs/core/src/session/mod.rs` — `request_command_approval` / `notify_approval`  
- `codex-rs/core/src/state/turn.rs` — `pending_approvals` oneshot map  
- `codex-rs/protocol/src/protocol.rs` — `ReviewDecision`  
- `codex-rs/tui/src/bottom_pane/approval_overlay.rs` — UI 队列与决策路由  
- `codex-rs/tui/src/approval_events.rs` — TUI 请求模型  
- `codex-rs/tui/src/bottom_pane/pending_thread_approvals.rs` — 跨 thread 提示  

### GA（P1 后）

- `permission_policy.py` — 三档 evaluate  
- `permission_runtime.py` — Future 挂起 / resolve / cancel_all  
- `ga.py` `GenericAgentHandler.dispatch` — ask 阻塞 wait + accept 执行  
- `agentmain.py` — 挂 runtime；abort → cancel_all  
- `frontends/ink_bridge.py` — 切档 + `permission_request`/`response`/`settled`  
- `frontends/ink-ui/src/permissionPanel.ts` — 切档 UI  
- `frontends/ink-ui/src/approvalPanel.ts` — 统一 accept/deny 审批 UI  
- `docs/ga_permission_modes_research_2026-07-20.md` §14 — 交付与剩余大件  

---

## 10. 总结

1. **CC** 证明：工具执行链上的 **`await canUseTool` + 队列 UI + resolve-once** 是交互式 TUI 的完整形态。  
2. **Codex** 证明：分进程/事件化系统应用 **`pending[id] + oneshot/Future + request/response 事件`**，且 **失败默认 Abort/Deny**。  
3. **GA P1** 已补：Codex 式 **跨 bridge 等待** + **统一 accept/deny 行内 UI** + fail-closed。  
4. **P1 产品拍板（再简化）**：**所有工具同一套 UI，仅 `accept` / `deny`**；协议与等待抄 Codex；不做 session/always、不做按工具特化页、不做 classifier、不做 workflow child。  
5. **fail-closed 两义**（§0.2）：**无 UI** = 没人审就不放行；**`/stop`** = 解开 pending 且不执行未批工具并停轮——勿与「用户点拒绝」混读。  
6. **验证**：单测 + 三级 live（runtime / bridge 方法 / 真子进程 JSONL）见 §12；真 Ink 键盘 E2E 仍可选。

---

## 11. 文档自检（2026-07-21 终审）

| 检查项 | 结果 |
|--------|------|
| 产品决策是否唯一：仅 accept/deny、一套 UI | ✅ §0.1 / §5 / §7 一致 |
| 无 UI 与 `/stop` 是否写清为两场景 | ✅ §0.2 + §5.5 / §5.6 |
| 是否残留 allow_once / allow_session / 三选项协议 | ✅ 已清；协议仅 `accept\|deny` |
| 是否残留「按 tool 换选项 / 专用页」为推荐 | ✅ §2.4 / §3 已改为不推荐 |
| P1 是否误含 session 内存 allow | ✅ §6 改为明确不做 |
| 伪代码 / 单测示例是否仍写 allow_once | ✅ 已改为 accept |
| 范围是否越界到 workflow / 持久化 / sandbox | ✅ 明确非本切片 |
| 与总文档 §14 大件 #1 交叉引用 | ✅ 总文档已链到本文 |

**结论（实现后更新）**：P1 已按 §7 落地并经 §12 验证；剩余大件见总文档 §14.3（持久化 / workflow ask）。

本文为调研、设计与验证记录，**不替代实现 PR**。行为以仓库代码 + 总文档 §14 为准；总权限背景见 `ga_permission_modes_research_2026-07-20.md`。

---

## 12. 实现与验证进度（2026-07-21）

### 12.1 切片完成度

| Slice | 内容 | 状态 |
|-------|------|------|
| **P1a** | `permission_runtime` + dispatch 阻塞 + bridge request/response + 无 UI deny | ✅ |
| **P1b** | `approvalPanel` 二选一 + App 统一 overlay + 审批优先于切档 | ✅ |
| **P1c** | argsPreview 截断、stop/`cancel_all`、deny 回模型 | ✅（主路径） |
| 真 Ink 键盘 E2E | 人工键入 accept/deny | ⬜ 未做（非阻断） |
| session/always、工具特化页 | — | ❌ 明确不做 |

### 12.2 Live 验证（模型：`grok` profile → `grok-4.5` / `grok-endpoint`）

| 层 | 路径 | 脚本 | 断言 | 结果 |
|----|------|------|------|------|
| L1 | 测试线程直接 `runtime.resolve` | `temp/_live_ask_approval_grok.py` | deny 不落盘；accept 写出 `hello-from-ask-*` | **ALL LIVE CASES PASSED** |
| L2 | 同进程 `bridge.permission_response`（禁止直触 Future） | `temp/_live_ask_approval_bridge_grok.py` | 同上（`bridge-ask-*`） | **ALL BRIDGE LIVE CASES PASSED** |
| L3 | **真子进程** `python frontends/ink_bridge.py`，stdin/stdout **仅 JSONL** | `temp/_live_ask_approval_subprocess_jsonl_grok.py` | 同上（`subproc-ask-*`） | **ALL SUBPROCESS JSONL LIVE CASES PASSED** |

L3 数据流（最接近 Ink）：

```text
父进程 write JSONL → 子进程 stdin → run_jsonl_loop
父进程 read  JSONL ← 子进程 stdout ← emit(permission_request / status / …)
父进程 write permission_response → bridge.permission_response → runtime.resolve
```

### 12.3 已知非阻断现象

- accept/deny 后若模型再调其它 mutating 工具（如 `update_working_checkpoint`），会再弹一次审批；L3 deny case 中该二次请求亦被 deny，文件仍不存在。  
- 工具执行后的下一轮 LLM 偶发 429/503 **不影响** 门控与落盘 oracle。  
- 模型选择须用 profile 精确 index；`select_llm("grok")` 可能与 `hhhl-grok` 歧义。

### 12.4 下一步（非本切片）

见总文档 `ga_permission_modes_research_2026-07-20.md` §14.3 / §14.5：P0b inherit、P2a 持久化、P2b workflow ask；可选真 Ink 键盘 E2E。
