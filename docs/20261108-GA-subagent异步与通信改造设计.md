# GA Subagent 异步与 Agent 间通信：现状核对与改造设计

- 日期：2026-11-08
- 对照基线：`D:\git_repos\claude_hack\docs\20261108-CC-subagent异步与agent间通信机制调研.md`（CC 2.1.319）
- 对象：`D:\git_repos\GenericAgent`
- 状态：**调研 + 设计稿，实施前**
- 方法：逐条读 GA 源码核对，CC 侧引用调研文档的已核实结论（该文档 §10.9/§10.10 有运行时实测）

---

## 0. 结论先行（先纠正一个前提）

**「GA 的 subagent 是同步的、会阻塞 main」这个判断不准确。** 逐条核对源码后：

| 事实 | 证据 |
| --- | --- |
| 子 agent 是**独立 OS 进程**（`popen`），不占用主进程 LLM 循环 | `agents/manager.py:246` `SubagentManager`；`AgentState` 有 `pid`、`task_dir` |
| `spawn_agent` **立刻返回**，不等子 agent 结束 | `handler.py:1771` 调 `manager.spawn_agent`；`handler.py:1823` `[Status] ... started (pid=...)`；返回 `status="started"` |
| `background` 默认 `True` | `handler.py:1785` `background=bool(args.get("background", True))` |
| 已有跨进程通知总线（`<task-notification>`） | `agents/notifications.py:85`、`agents/event_bus.py:26` |
| 已有 agent 间消息工具 | `send_message` / `followup_task` / `wait_agent` / `read_agent_result` / `resume_agent` |
| UI 已有 agent 事件通道 + 后台 watcher | `frontends/ink/bridge.py:1838` `_emit_agent_read_model`、`:1869` `_watch_agent_state`（0.5s stat 轮询） |

**所以「异步」本身不缺。** 用户看到的「同步、卡住、看不到进度」是由下面四个具体缺陷共同造成的：

1. **通知的消费时机错了（主因）**：GA 只在**用户发起新的一轮输入**时拉一次通知，
   CC 在每个**工具轮结束后的 fold 点**拉。子 agent 在 main 的长任务中途跑完时，
   GA 的 main 要等用户下次说话才知道。
2. **通知消费是破坏性且吞异常的**：`consume_notifications()` 读完即推进游标，
   而调用点外面套着 `except Exception: pass` —— 拼装一旦失败，通知**永久丢失**。
3. **`wait_agent` 的默认路径是 sleep 轮询**：只有 `ipc_mode=realtime` 且通道活着
   才会被事件唤醒；**默认 `ipc_mode="file"`**，走的是 `self.sleep(poll_interval_s)`，
   即 CC 明确列为反模式的做法。
4. **`send_message` 没有名字钉住，并且会静默投进死邮箱**（见 §1.6，这一条最危险）。

另外有一个 GA 独有、**不打算改**的结构差异：子 agent 是独立进程，所以 CC 那种
「直接改对方 `pendingMessages`」的零成本路由（调研文档 §3.5）在这里不存在，
一切通信必须落盘或走 socket。这是隔离性的代价，值得付。

---

## 1. GA 现状事实（逐条带坐标）

### 1.1 架构：子 agent = 独立进程 + 双传输

```
SubagentManager (manager.py:246)
  ├─ spawn_agent() → popen 一个 python 子进程
  │                  AgentState{ pid, task_name, agent_path, task_dir, state_path, run_id }
  └─ 传输：
       agents/ipc.py            file 模式（默认；文件 + 事件 bus）
       agents/realtime_ipc.py   realtime 模式（mkdir socket / 双向通道）
```

`manager.py:1673` 的注释写得很明白：「With live channels this is the watch half of B3 …
Without them (ipc_mode=file, or realtime refused) the blind sleep has to stay,
because the file transport has nothing to be woken by.」

**这就是缺陷 3 的准确描述**：能力已经写好了，但默认没开。

### 1.2 工具面：22 个 agent 相关工具，四通道已有其三

| CC 通道（调研文档 §2-§5） | GA 对应 | 状态 |
| --- | --- | --- |
| A. `task-notification` 推送 | `agents/notifications.py` + `event_bus.py` | ✅ 有，**消费时机错** |
| B. `SendMessage` 对等消息 | `send_message` + `agents/mailbox.py` | ✅ 有，**无钉住** |
| C. `SubagentHandback` 一次性报告 | `read_agent_result`（main 主动拉） | ⚠️ 语义相反 |
| D. `notify_when_idle` 反向订阅 | 无（`idle.py:63` 有 frame 构造，无订阅入口） | ❌ 缺 |

CC 通道 C 是**子主动交、调用即结束**（调研文档 §4.2 原文 “It is not a messaging channel”）。
GA 是 main 完成后主动 `read_agent_result` 去拉。差别在于：CC 的交回是**事件**，
推送给你；GA 的读取是**动作**，需要 main 自己想起来。

### 1.3 缺陷 1：通知只在用户 turn 开头消费

唯一调用链：

```
app.py:1844   sections = build_system_prompt_sections(self)      # 每轮 user turn 开头
  → app.py:569 build_system_prompt_sections
    → app.py:579 build_subagent_notifications_prompt(project_root)
      → notifications.py:65 bus.consume_notifications()          # 破坏性
```

全仓 `build_subagent_notifications_prompt` 只有这一个生产调用点（其余是测试）。
`loop.py` 里检索 `notification|subagent` **只有 1 条命中，且是注释**（`loop.py:48`）。

**后果时序**：

```
t0  main 派 3 个子 agent，然后开始跑一个 20 步的长任务
t1  子 agent A 完成 → event_bus.append_event(notify=True)
t2  main 在第 5 步 → 没有任何 fold 点消费 → 通知躺在 notifications.jsonl
...
t3  main 跑完 20 步，任务结束，等用户输入
t4  用户输入「好了吗」 → 此时才 consume → main 一次看到 3 条
```

用户侧观感就是「子 agent 早干完了，main 一直不知道」。这正是 CC 那条调研文档
要解决的问题。

### 1.4 缺陷 2：消费是破坏性的，异常即丢

两段代码合起来构成一个静默丢数据的组合：

```python
# event_bus.py:87-93
def consume_notifications(self):
    cursor = read_json_or_none(self.notification_cursor_path) or {}
    last_seq = int(cursor.get("last_event_seq") or 0)
    notifications = [e for e in self._read_jsonl(self.notifications_path)
                     if int(e.get("event_seq") or 0) > last_seq]
    if notifications:
        atomic_write_json(self.notification_cursor_path,
                          {"last_event_seq": int(notifications[-1].get("event_seq") or 0)})
    return notifications          # ← 游标已经推进了
```

```python
# app.py:576-581
if not is_subagent:
    try:
        notifications = build_subagent_notifications_prompt(project_root)   # ← 游标推进
    except Exception:
        pass                                                                # ← 文本没了
```

游标在 `event_bus` 里推进，文本在 `app.py` 里被 `pass` 掉。两者之间任何异常
（`apply_result_budget` 的 import 失败、`_render_task_notification` 遇到畸形
event、`render_system_prompt` 之前的任何问题）都会让这批通知**永久消失**。

### 1.5 缺陷 3：默认路径是 sleep 轮询

```
handler.py:2278   poll_interval_s 默认 0.5
handler.py:1786   ipc_mode 默认 "file"
manager.py:1682   channels = self._channels_for(targets); if not channels: self.sleep(poll_interval_s)
manager.py:1691   if channel.wait_for_signal(slice_s): woken = True
```

`_channels_for` 依赖 `self._realtime_channels`，而它只在 realtime 通道建立时填充。
默认 file 模式下 `_channels_for` 返回 `[]`，**每次都盲睡 0.5s**。

CC 的 Agent prompt 反复禁止 “do NOT sleep, poll, or proactively check”
（调研文档 §9.3 第 1 条，`m0765` 原文）。GA 自己的 prompt 也在劝
（`agents/prompts.py:29`：「avoid reflexive polling, prefer a long timeout」），
**但底层实现没做到** —— 这是一处「提示词说的和代码做的相反」。

### 1.6 缺陷 4：`send_message` 会静默投进已关闭 agent 的邮箱（最危险）

三处代码串起来看：

```python
# registry.py:545-567
def _unique_child_name_unlocked(self, parent, task_name):
    existing = taken.get(requested)
    if existing is not None and self._is_live_row(existing):
        raise SubagentNameConflictError(...)      # 活的 → 报错
    if existing is None:
        return base
    index = 1
    while True:                                   # 已关闭的 → 静默改名 a_1
        candidate = f"{base}_{index}"
        ...
```

```python
# manager.py:2209-2214  _queue_message
task_name = self._task_name_from_target(target)   # normalize，不查 registry
task_dir = self.task_root / task_name
task_dir.mkdir(parents=True, exist_ok=True)       # 目录不存在就新建
```

```python
# registry.py:548 注释（作者已知）
"a closed or crashed row still owns its task dir and artifacts, so reusing
 that exact name would clobber the only evidence of what it did"
```

**串起来就是**：`a` 跑完并 `close_agent` 之后，再 `spawn_agent("a")` 得到的是
`a_1`；此时 `send_message(target="a")` 会：
1. `normalize_task_name("a")` → `"a"`；
2. `task_dir = temp/a`（**已关闭那个 agent 的目录**）；
3. `enqueue` 写入 `temp/a/mailbox.jsonl`；
4. `receipt_ledger.record_send(...)` 记一条「已发送」；
5. main 收到 `{"status": "queued"}` 的成功回执；
6. **没人会消费它** —— `a` 进程已退出，`a_1` 用的是 `temp/a_1`。

结果是：主 agent 以为消息送到了，回执也说送到了，实际上石沉大海。
这正是 CC `sendMessagePins`（调研文档 §7.5，运行时实测才发现）要防的事。
GA 比 CC 更糟：CC 是「静默投给新 agent」，GA 是「静默投给死信箱」。

### 1.7 已核实：三条「反模式」GA 都守住了

| CC 反模式（调研文档 §9.3） | GA 现状 | 证据 |
| --- | --- | --- |
| 让 peer 消息参与权限判定 | ✅ 未违反 | `core/permissions/` 检索 `mailbox|send_message|peer` **0 命中** |
| teammate 无限嵌套 | ✅ 未违反 | `capabilities.py:112-117` 子 agent 默认从 allowed 里移除 `ORCHESTRATION_TOOLS`；`registry.py:10` `DEFAULT_MAX_DEPTH = 3`；还有 `max_active_agents` 上限 |
| 让子 agent 纯文本结尾充当结果 | ✅ 未违反 | 有 `final_output_path` / `final_output_sha256` / `final_output_ref`（`app.py:2719`） |

### 1.8 已核实：「停用 agent 不能派新 agent」GA 没有

CC 有 `subagent_spawner_stop_pending`（调研文档 §9.2 第 12 条）。
GA 的 `registry.py` 有 `status` 字段和 `_is_live_row`，但**没有「停机中」这个中间态**，
也没有阻止正在 shutdown 的父 agent 继续 spawn。严重度中（只在并发关闭时有影响），
本次列为 P2。

### 1.9 已核实：`notifications.jsonl` 只增不减

`consume_notifications` 每次都 `self._read_jsonl(self.notifications_path)` 全量读，
再用游标过滤。`_publish` 注释说 JSONL append 是权威源，但**没有任何 compaction**。
长时间运行的会话里这个文件会一直长；每次 `build_system_prompt_sections` 都全量解析一遍。

不是 P0（单条通知很小），但是改造时**顺手就能修**：peek/ack 方案天然只需要读游标之后的部分。

---

## 2. 改造设计

### 2.0 设计原则

1. **不改「子 agent 是独立进程」**。隔离和崩溃韧性值得保留。
2. **只补「main 何时看到」和「消息发给谁」两件事**，不动子 agent 的执行模型。
3. **通知必须可重放**：宁可重复看到，也不能丢。重复的代价是一次多余的模型调用；
   丢失的代价是 main 永远不知道任务完成。
4. **不引入 sleep 轮询作为默认路径。**

### 2.1 通知队列：peek / ack 分离（修缺陷 2，为 2.2 铺路）

新增 `genericagent/agents/notification_queue.py`：

```python
def peek_notifications(root_dir) -> list[dict]:
    """读游标之后的全部通知，不推进游标。纯读，可重放。"""

def ack_notifications(root_dir, upto_event_seq: int) -> None:
    """把游标推进到 upto_event_seq。只在确认已注入模型之后调用。"""
```

改造 `event_bus.SubagentEventBus`：

- `consume_notifications()` **保留**（向后兼容，测试在用），内部改为 `peek` + `ack`；
- 新增 `peek_notifications()`（不写游标）与 `ack_notifications(seq)`。

改造 `notifications.build_subagent_notifications_prompt`：

```python
def build_subagent_notifications_prompt(root_dir):
    events = peek_notifications(root_dir)          # 不再 consume
    if not events:
        return "", None                            # ← 返回 (text, ack_upto)
    blocks = apply_result_budget([...])
    text = "\n".join([...])
    return text, int(events[-1]["event_seq"])
```

调用方拿到 `ack_upto` 之后，**只有确认文本进了 `messages` 或 `sections` 才 ack**。
`app.py:576-581` 的 `except Exception: pass` 保留（不能因为通知失败就中断用户轮），
但不 ack 就不会丢。

**签名变更的兼容**：现有测试 `tests/test_subagent_notifications.py` 断言返回 `str`。
用一个薄包装保持 `build_subagent_notifications_prompt(root) -> str` 不变（内部自己 ack），
另加 `peek_subagent_notifications_prompt(root) -> (str, ack_upto)` 给新路径用。
这样测试不用改，新路径拿到可重放语义。

### 2.2 把通知注入移到 fold 点（修缺陷 1，核心改动）

**改动点**：`genericagent/core/loop.py` 的 `agent_runner_loop`。

`loop.py:306` 已经有一个天然的 fold 点：

```python
messages = [{"role": "user", "content": next_prompt, "tool_results": tool_results}]
```

这是「本轮工具都跑完、下一次 `client.chat()` 之前」。在它前面注入：

```python
# loop.py，turn_end_callback 之后、messages 赋值之前
if not getattr(handler.parent, "task_dir", None):        # 只主 agent 注入
    notif_text, notif_ack = peek_subagent_notifications_prompt(project_root)
    if notif_text:
        next_prompt += "\n" + notif_text
        pending_notification_ack = notif_ack              # 延迟到 chat 成功后再 ack
...
messages = [{"role": "user", "content": next_prompt, "tool_results": tool_results}]
```

ack 的时机：下一轮 `client.chat()` **成功返回后**（`loop.py:216` 之后）。
如果 `chat()` 抛异常，本轮消息作废，游标不推进，下一轮重投 —— 语义是
「至多重复一次，绝不丢失」。

**为什么不用系统提示**：系统提示改动会让 prompt cache 失效，而通知是高频的；
且 `note_system_prompt_sections`（`app.py:1845`）已经在检测 section 变化并打印，
说明作者也知道这是开销点。

**为什么注入 `next_prompt` 而不是独立的 user message**：`loop.py:306` 的
`messages` 列表是「一次新的 user 消息 + 本批 tool_results」，GA 的协议要求这两者绑在一起。
通知作为 `next_prompt` 的前缀，跟着这条消息一起走，不破坏协议。

**防刷屏**：`apply_result_budget` 已经把单条限到 4000、整批限到 16000
（`idle.py:26-27`，对齐 CC `m0300`）。fold 点注入复用同一套预算，不新增闸门。

**顺序**：通知应该排在 `next_prompt` 前面还是后面？按 CC 的做法（`queued_command`
attachment 紧贴 tool_result），通知是**先于**本轮继续指令被看到的。所以前缀。

### 2.3 `wait_agent` 默认走 realtime —— 已按方案 A 实施

候选方案有三：

| 方案 | 做法 | 结论 |
| --- | --- | --- |
| **A（已采用）** | 把实时通道改成默认传输，file 作为降级 | ✅ 已实施并实测 |
| B | 保持 file 默认，`_wait_for_change` 改成监听 `notifications.jsonl` 的 stat 变化 | 不再需要 |
| C | 只改 prompt 让模型少轮询 | 治标 |

**当初的犹豫是错的。** 我原本推荐 B，理由是"realtime 在 Windows 上的行为只有代码证据"。
实测后（§8）不仅可用，而且快得多：**唤醒 0.25-0.27 秒，轮询间隔设的是 30 秒**。
B 方案本质仍是轮询，只是把 500ms 缩短一点——既然 A 能跑通，没有理由退而求其次。

**已做的改动**：

| 位置 | 改动 |
| --- | --- |
| `manager.py:342` `_env_realtime_channel_factory` | 从"`GA_SUBAGENT_REALTIME_IPC=1` 才开"改成"`=0` 才关"，默认开 |
| `manager.py` `spawn_agent` 签名 | `ipc_mode` 默认 `"socket"`（原 `"file"`） |
| `ipc.py:15` `normalize_ipc_metadata` | 默认值 `"socket"`；`"file"` 仍是显式选项和通用降级 |
| `handler.py` `do_spawn_agent` | 工具层默认 `"socket"` |
| `assets/tools_schema{,_cn}.json`、`genericagent/resources/assets/tools_schema{,_cn}.json` | `default` 改 `"socket"` + 描述重写（4 份都要改：仓库根是运行时副本，包内是模板） |

**保留的降级链完全不变**：打不开通道（权限、平台、异常）→ `effective_ipc_mode="file"`
+ `ipc_fallback_reason` 说明原因。子进程订阅失败同理，记 `child_ipc_status="fallback"`。
`wait_agents` 的 `_wait_for_change`（`manager.py:1677`）本来就两条路都实现了：
有通道走 `wait_for_signal`，没通道才 `self.sleep(poll_interval_s)`。改默认值后
绝大部分情况走前者，后者成为真正的兜底。

#### 2.3.1 顺带修掉的两个真问题（不改默认值就会被放大）

**(a) Windows 管道名跨项目冲突。** `default_channel_address` 在 Windows 上**完全忽略
`base_dir`**，只用 `run_id` 拼名字，而每个 manager 的 `run_id` 都从 `run_000001` 起：

```
root A: \\.\pipe\ga_subagent_run_000001
root B: \\.\pipe\ga_subagent_run_000001   ← 同一个
```

以前 opt-in，几乎没人同时开两个；一旦成为默认，**第二个 GA 会话的每次 spawn 都会
bind 失败并静默降级成 file**——正好把这个改动的好处全部抵消。

修法：给管道名加一个由 `base_dir` 派生的短摘要（`realtime_ipc.py:_root_tag`，
sha256 前 10 位）。实测两个不同 root 现在得到
`ga_subagent_beb5ad4765_run_000001` 与 `ga_subagent_a605f25063_run_000001`，不再冲突；
同一项目的名字跨重启不变（因为摘要的是解析后的目录，不是进程）。

**(b) 通道线程泄漏。** 每条实时通道的 accept loop 在自己的 daemon 线程上。
`close_agent` 会释放对应通道，但**agent 自然跑完而没被 close 时，通道和线程一直留着**。
以前 opt-in 所以见不到；默认开之后每次 spawn 都可能漏一个。

修法：新增 `SubagentManager.close_realtime_channels()`，一次性关闭本 manager 持有的
全部通道。没有把它塞进 `_refresh_state`，因为那是轮询路径——对还活着的 agent 调
`close()` 会直接杀掉它的通道。

#### 2.3.2 一处被实测推翻的改动（记录以免重犯）

我曾把 `_accept_loop` 里的 `sink.offer(ack)` 和 `_subscribers.append(sink)` 调换顺序，
理由是"客户端收到 ack 就该能立刻发信令，此时 sink 必须已注册"。跑测试后
`test_a_stalled_subscriber_does_not_delay_delivery_to_a_healthy_one` 开始偶发失败
（原始代码 10/10 通过，改后 2/10 失败）。

原因：调换后 `publish()` 可以在 append 和 offer 之间插进一条真事件，队列变成
`[event, ack]`，而客户端的 `connect_realtime_channel` 会把**第一条消息当成 ack 读掉**，
那条真事件就此丢失。

而原来的顺序本来就没有信令竞态：ack 是由 sink **自己的线程**在 `start()` 之后发出的，
那时 append 早已完成，客户端不可能在注册前发信令。**已改回原顺序并加了注释说明为什么不能换。**

教训：`offer(ack)` 必须在 sink 对 `publish()` 可见之前，否则 ack 就不再是第一条消息。

### 2.4 `send_message` 名字钉住（修缺陷 4，安全相关）

两个独立的漏洞，分别修：

**(a) 投递前必须校验目标活着。**

```python
# manager.py:_queue_message，在 mkdir 之前
row = self.registry.get_agent(primary_path)     # 用现有 API
if row is None:
    raise ValueError(f"no agent named {task_name!r}; use list_agents to see live agents")
if not self.registry._is_live_row(row):
    raise ValueError(
        f"agent {task_name!r} is closed; send_message does not revive it. "
        f"Use resume_agent to bring it back, or spawn_agent with a new task_name."
    )
```

这一条就能堵住死信箱。错误信息必须给出下一步（`resume_agent` 或新名字），
否则模型会重试同一个名字。

**(b) 首次投递时钉住名字，之后同名换人必须拒绝。**

新增 `genericagent/agents/name_pins.py`：

```python
# temp/subagents/name_pins.json
{"a": {"agent_path": "/root/a", "run_id": "...", "pinned_at": "..."}}
```

- 首次 `send_message(target="a")` 成功时写钉住记录；
- 之后若 `target="a"` 解析出的 `run_id` 与钉住的不一致，**拒绝并报错**，
  提示「用完整 agent_path 或先 close 再重新造」；
- `close_agent` 时**不**删钉住记录（删了就退化成「静默投给新 agent」，
  正是 CC 明确说错的「latest wins」）。

CC 的语义（调研文档 §7.5）就是「首次发信钉住 `(name → id, ref)`」。
GA 已有 `run_id`（`AgentState.run_id`），天然可做钉住的 key。

### 2.5 `notify_when_idle`（补通道 D）

`send_message` 增加可选参数 `notify_when_idle: bool`。语义：

- 投递时在 `name_pins` 或独立 `idle_subscriptions.json` 记一条订阅；
- 目标进入 idle（`state.json` 的 `turn_status` 变为 `idle`/`completed`）时，
  由**已有的** `build_idle_notification`（`idle.py:63`）构造 frame，
  写进 event bus（`notify=True`）；
- 复用 2.1/2.2 的通知队列与 fold 点注入，**不新开通道**。

注意 GA 是独立进程，idle 判定只能靠 `state.json`，不能靠内存回调。
这里可以用 `_wait_for_file_bump` 那套 stat 检测，不需要额外线程。

### 2.6 顺手修：`notifications.jsonl` 只读游标之后

`peek_notifications` 天然只关心游标之后，可以改成**从文件尾部反向扫**：
读最后 N 字节，换行切分，取 `event_seq > cursor` 的部分；不够再往前扩。
这解决 §1.9 的「全量解析」问题，且不改文件格式。

**不做的**：不删除旧记录（审计价值 + 简单性），只优化读取。

### 2.7 UI：不需要改

`bridge.py:1869` `_watch_agent_state` 每 0.5s stat 一次 `registry.json` 与
`*/state.json`，变化才 `_emit_agent_read_model`。这一层**比 main agent 自己还及时**。

只需确认一件事：2.2 改造后 notification 游标推进不影响 UI 的读模型
（两者读的是不同的东西：UI 读 `registry.json`/`state.json`，main 读
`notifications.jsonl` + 游标），**互不干扰，无需改动**。

**建议**（非必需）：agent 面板加「最近一次通知时间」，便于排查
「UI 显示了 main 没看到」这类不一致。

---

## 3. 实施顺序与验收

| 步 | 内容 | 优先级 | 状态 |
| --- | --- | --- | --- |
| 0 | **实时通道改默认 + 修管道名冲突 + 修线程泄漏** | P0 | ✅ **已完成**（§8） |
| 1 | `notification_queue.py` + `event_bus` peek/ack，保留旧 API | P0 | ⬜ 待做 |
| 2 | `loop.py` fold 点注入 + 延迟 ack | P0 | ⬜ 待做 |
| 3 | `send_message` 投递前校验 alive | P0 | ⬜ 待做 |
| 4 | ~~`_wait_for_change` file 模式改 stat 等待~~ | — | ❌ 不需要（步 0 已用 realtime 解决） |
| 5 | `name_pins.py` 钉住 | P1 | ⬜ 待做 |
| 6 | `notify_when_idle` | P2 | ⬜ 待做 |
| 7 | spawner stop-pending 守卫 | P2 | ⬜ 待做 |
| 8 | 回归 | — | `test_subagent_*` 全部 + `test_agent_loop_*` 全绿 |

**步 1-3 是核心**，做完用户看到的现象就该消失。

### 3.1 关键测试用例（写成真的集成测试，不是 mock）

**fold 点注入**（步 2，最重要）：

```
t0  构造一个 handler，让第一个 tool_call 是 spawn_agent（子 agent 立即完成）
t1  main 的第 2 个 tool_call 是一个慢工具（sleep 0.3s）
t2  断言：main 的**第 3 轮** messages 里出现 <task-notification>
    —— 而不是等下一次 put_task
```

这个测试必须用真子进程，不能 mock `spawn_agent`，否则测不到 `event_bus` 的时序。

**死信箱**（步 3）：

```
t0  spawn "a"，等它完成，close_agent("a")
t1  spawn "a"  → 得到 "a_1"
t2  send_message(target="a")  → 断言抛错，且错误信息含 "resume_agent"
t3  断言 temp/a/mailbox.jsonl 没有新增行
```

**可重放**（步 1）：

```
t0  写入 3 条 notify 事件
t1  peek → 3 条，游标未动
t2  peek 再来一次 → 还是 3 条（不是 0 条）
t3  ack(第 2 条的 seq) → peek → 只剩 1 条
```

---

## 4. 与 CC 的逐条对照（复刻清单打勾）

CC 文档 §9.1「必需」：

| # | CC 要求 | GA 现状 | 本次 |
| --- | --- | --- | --- |
| 1 | 统一输入队列，三档 `now`/`next`/`later` | ❌ 无 | **不做**（见 §5） |
| 2 | 任务注册表 | ✅ `agents/registry.py` | 不需要 |
| 3 | 子代理 = 异步任务，启动即返回句柄 | ✅ 已是独立进程 | 不需要 |
| 4 | 收尾必发通知，单一函数 + 幂等 | ⚠️ 有通知，无幂等 ack | **P0（步 1）** |
| 5 | fold 点取队列 | ❌ 只有 user turn 开头 | **P0（步 2）** |
| 6 | 两套载体不混 | ✅ file/realtime 分离 | 不需要 |

CC 文档 §9.2「强烈建议」：

| # | CC 建议 | GA 现状 | 本次 |
| --- | --- | --- | --- |
| 7 | 对等消息带来源标注 + 反洗白 | ✅ `ANTI_SPOOF_PREAMBLE`（`notifications.py:30`） | 不需要 |
| 8 | handback 与 progress 分离 | ⚠️ `read_agent_result` 兼两职 | P2（§4.1） |
| 9 | 完成后可 resume | ✅ `resume_agent`（`manager.py:1279`） | 不需要 |
| 10 | 水平信号兜底（全量快照） | ✅ `_emit_agent_snapshot` | 不需要 |
| 11 | 名称注册表 + 钉住 | ❌ 无钉住 | **P1（步 5）** |
| 12 | 停用 agent 不能派新 agent | ❌ 无 `spawner_stop_pending` | P2（步 7） |

CC 文档 §9.3「反模式」：

| # | 反模式 | GA 现状 | 本次 |
| --- | --- | --- | --- |
| 13 | 用 sleep/轮询等完成 | ⚠️ 默认 file 模式就是盲睡 | **P1（步 4）** |
| 14 | 纯文本结尾充当结果 | ✅ 有 `final_output_path` | 不需要 |
| 15 | peer 消息参与权限判定 | ✅ 未违反（§1.7） | 不需要 |
| 16 | teammate 无限嵌套 | ✅ 未违反（深度 3 + 无编排工具） | 不需要 |

### 4.1 关于第 8 条（handback 与 progress 分离）

GA 的 `read_agent_result` 同时承担「读最终报告」和「看进度」两职。
CC 明确说 handback “It is not a messaging channel”（调研文档 §4.2）。

但这在 GA 里**不是真问题**：`read_agent_result` 返回的是 `state.json` 里的
持久化结果，不是一条消息通道，误用代价小。**列为 P2，只做文档标注**：
在 tool description 里写明「进度请用 wait_agent / task-notification，
read_agent_result 只取最终结果」。

---

## 5. 明确不做

| 项 | 理由 |
| --- | --- |
| 统一三档输入队列 `now`/`next`/`later` | GA 的 `task_queue`（`app.py:1114`）已经承担了 `now` 的角色，且 `put_task` 的调用方只有 UI 层。为了 2.2 能工作，**不需要**先建通用队列 —— 在 `loop.py` 里直接注入就够了。过早抽象会引入一个没人用的中间层 |
| 把子 agent 改同进程异步任务 | 独立进程带来隔离与崩溃韧性；CC 同进程是为了共享内存，GA 已有 file/realtime 双传输 |
| `uds:` / `bridge:` / `did:` 跨会话寻址 | GA 无跨会话需求（调研文档 §10.3 也证实 `did:` 在本 build 不可达） |
| cloud control plane | 无此需求 |
| 完整 `local_bash` 任务类型 | GA 的后台命令走子 agent，不需要单独类型 |

---

## 6. 实施前必须确认的两件事

1. **~~`realtime_ipc` 在 Windows 上的实际可用性~~ → 已实测可用，见 §8。**
   结论：方案 A（改默认）已实施，方案 B 不再需要。

2. **`registry.get_agent` 的 API 名**。本设计里假设有这么一个按 `agent_path` 取行的接口；
   核对到的是 `list_agents` / `descendants` / `_is_live_row`。
   实施时若没有单行 get，用 `list_agents(include_closed=True)` 过滤，或加一个。
   **（步 3 仍未做，届时再定。）**

---

## 7. 一句话总结

> GA 的 subagent **已经是异步的**（独立进程 + 立刻返回），
> UI 也**已经能看到进度**（0.5s stat watcher，比 main 还及时）。
> 卡住的是两件事：
> **① main 的 LLM 循环只在用户输入时才看通知**（CC 在每次工具轮结束就看）；
> **② `send_message` 会静默投进已关闭 agent 的邮箱**并回执成功。
> 前者把通知从「系统提示的副作用」改成「fold 点的显式可重放消费」，
> 后者加一道 alive 校验 + 名字钉住。
> 修完这两条，用户看到的现象就消失了。

---

## 8. 步 0 实测与实施结果（2026-11-08）

### 8.1 实测：Windows 命名管道可用，唤醒 0.25 秒

用仓库自带的真实 E2E（`tests/real_subagent_realtime_e2e.py`，真实 LLM、真实子进程、
真实命名管道、真实 authkey），把轮询间隔故意设成 **30 秒**，这样任何快速唤醒只可能来自通道：

```bash
GA_RUN_REAL_API_E2E=1 \
GA_REAL_API_EXPECTED_MODEL=moma/deepseek-latest-flash \
GA_REAL_API_EXPECTED_NAME=default \
python tests/real_subagent_realtime_e2e.py
```

关键输出：

```json
{
  "passed": true,
  "issues": [],
  "handle": {"ipcMode": "socket", "effectiveIpcMode": "socket", "ipcFallbackReason": null},
  "childIpc": {"status": "subscribed", "address": "\\\\.\\pipe\\ga_subagent_0accb82ff3_run_000002"},
  "subscriberCount": 1,
  "followupWakeSeconds": 0.27,
  "authkey": {"delivered": true, "bytes": 32},
  "authkeyLeaks": [],
  "authkeyRemovedOnClose": true
}
```

- 子进程真的订阅上了（`subscribed`，不是 `fallback`）
- **followup 唤醒 0.27 秒**，而轮询间隔是 30 秒 → 证明是通道唤醒，不是定时器
- authkey 32 字节、无泄漏、close 后删除
- 首次跑的是 `run_000001`（管道名 `ga_subagent_run_000001`），改默认值后再跑是
  `run_000002`（管道名 `ga_subagent_0accb82ff3_run_000002`）——root tag 已生效

### 8.2 改动清单

| 文件 | 改动 |
| --- | --- |
| `genericagent/agents/manager.py` | `_env_realtime_channel_factory` 改为默认开（`=0` 才关）；`spawn_agent` 的 `ipc_mode` 默认 `"socket"`；新增 `close_realtime_channels()` |
| `genericagent/agents/ipc.py` | `normalize_ipc_metadata` 默认值 `"socket"` |
| `genericagent/agents/realtime_ipc.py` | 新增 `_root_tag()`，Windows 管道名加 root 摘要修跨项目冲突；`_accept_loop` 加注释说明为何 ack 必须先于注册 |
| `genericagent/core/handler.py` | `do_spawn_agent` 工具层默认 `"socket"` |
| `assets/tools_schema{,_cn}.json`、`genericagent/resources/assets/tools_schema{,_cn}.json` | `default` 与描述更新（4 份） |
| `tests/test_subagent_manager.py` | 一处断言 file 降级的用例改为显式 `GA_SUBAGENT_REALTIME_IPC=0` |
| `tests/test_subagent_realtime_ipc.py` | `test_close_joins_the_sender_threads` 改为只统计本测试的线程；信令测试先等订阅再等信号；复位进程级 `_REALTIME_SUBSCRIBER` |

### 8.3 回归结果

| 测试模块 | 结果 |
| --- | --- |
| `test_subagent_manager`（106 例） | 3/3 轮 OK；三模块合并连跑 4/4 轮 OK |
| `test_subagent_realtime_ipc`（38 例，2 skip） | 3/3 轮 OK；单进程连跑 5/5 轮 OK；`ChildEventSignalsTheParentTest` 单独 12/12 轮 OK（修复前曾 2/10 偶发失败） |
| `test_subagent_ipc` | 3/3 轮 OK |
| `test_ga_subagent_tools` / `test_subagent_wait` / `test_subagent_team` / `test_subagent_registry` / `test_subagent_event_bus` / `test_subagent_artifacts`（104 例） | OK |
| 真实 E2E | `passed: true`，`issues: []` |

**注意：以上都是每个模块独立进程跑的。** 我一度用一个探针脚本在**同一个解释器**里
连跑 8 轮，看到 `ga-subagent-realtime-accept` 线程累积到几千个并据此判断"线程泄漏"——
那是探针自己的问题（每轮新建 manager 不复位），不是测试的真实行为。
真实 CI 形态是每个模块一个进程，进程退出时 daemon 线程随之消失。

不过 §2.3.1(b) 的 `close_realtime_channels()` 仍然值得保留：它解决的是**长驻进程里**
agent 自然结束但没被 close 的情况，那在真实使用中确实存在。

**一条未定位的历史记录（如实记）**：在补齐最后 3 个用例之前的代码上（当时 146 例），
一次 3 轮合并回归的第 3 轮出现过 `FAILED (failures=1)`；那轮循环随后被系统内存回收器
中断，失败用例名没落盘。当前 149 例的代码上，三模块合并连跑 4/4 轮全绿、单模块 5/5 轮全绿、
目标用例所在类 12/12 轮全绿，但**没有证据能证明那 1 例失败已经消失**——它只出现在
146 例的版本上，且无名字可追。如果后续再出现，第一步是用 `-v` 把用例名抓下来。

### 8.4 对这一轮的自我更正

- 我原本推荐"先做方案 B（stat 轮询）"，理由是 realtime 缺运行时证据。**实测后推翻**：
  可用且快两个数量级（0.27s vs 30s 轮询间隔）。已按方案 A 实施。
- 我改错过一次 `_accept_loop` 的 ack/注册顺序，被现有测试抓住（`test_a_stalled_subscriber`
  从 0/10 变成 2/10 失败），已改回并加注释。
- 我一度把"线程累积到几千个"当成生产泄漏，实际是探针脚本在单进程里连跑所致，已在 §8.3 更正。
