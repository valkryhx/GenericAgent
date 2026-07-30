# GA subagent 控制面缺陷实测（2026-07-30）

日期：2026-07-30
起因：`close_agent(cascade=true)` 落地后复核"还有哪些该实现但未实现"，对规划文档剩下的
P2 项（B1/B2/B3）做实测验证，结果推翻了原有优先级排序 —— 最该先修的是一项**从未被记录过**
的 registry 并发缺陷，而不是文档里列的任何一条 P2。

本文只记录**实测到的事实**与由此得出的优先级判断。任务分解与 TDD 要求归口
`docs/ga_subagent_ipc_implementation_plan_2026-07-29.md`（第 1.5 节起）。

## 0. 结论摘要

| ID | 缺陷 | 优先级 | 文档此前状态 | 实测结论 |
| --- | --- | --- | --- | --- |
| **M5** | `registry.json` 无锁读改写：丢行 + `run_id` 碰撞 + Windows `os.replace` 抛错 | **P0 正确性 + 安全** | **完全未记录** | 4 writer × 40 轮：**丢 120/160 行（75%）**，120 次调用只发出 **48 个不同 run_id**，**41 个 run_id 被 ≥2 个 agent 共用**（最多复用 4 次），并直接抛 `PermissionError [WinError 5]`。**✅ 已修复（2026-07-30）** |
| **B1** | `publish()` 同步 fan-out 会阻塞在慢订阅者上 | P2 健壮性 | 规划文档 §6 已列，未实现 | **前提为真**：真实 `message_queued` 事件（pickle 329 字节）**24 条未读即卡死**；但"拖慢整个事件写入链路"的推断**未复现**，实际爆炸半径远小于文档描述。**✅ 已修复（2026-07-30）** |
| **B3** | `wait_agents()` 轮询 `state.json` 而非 watch 语义 | P2 控制面 | 规划文档 §6 已列，未实现 | 顺带量到写放大：4 agent / 2s / 0.5s 间隔 → **40 次原子写（每秒 20 次）**，且这些写正是 M5 竞态的主要喂食者。**✅ 已修复（2026-07-30）** |
| **B2** | 控制面 op 没有提交身份，重放即重复执行 | P2 控制面 → **实测为正确性问题** | 规划文档 §6 已列，本文初版判为"纯结构收益，无实测正确性问题" | 重放 `followup_task` → **2 条 trigger_turn 行（任务干两遍）**；重放 `spawn_agent` → **两个真实进程、两个 run_id**；`enqueue` 的 `message_id` 去重分支存在但 13 个 handler 从未使用。**✅ 已修复（2026-07-30）** |
| **M6** | 同名 spawn 静默改名：活着的 agent 被复制成第二个进程 | **P1 正确性 + 花费** | **完全未记录** | `spawn_agent("reviewer")` 在 `/root/reviewer` 仍在运行时产出 `/root/reviewer_1` —— 第二个 OS 进程、第二个活跃名额、第二份真实 LLM 花费，而模型以为只有一个 agent。Codex 同场景直接报错（`agent/registry.rs:247-250`）。**✅ 已修复（2026-07-30）** |
| **M7** | `resume_agent` 无任何守卫：可 resume 活着的 agent，且重放不设防 | **P1 正确性 + 花费** | **完全未记录** | resume 一个进程仍活着的 agent 会**再起一个进程并把 `state.json` 的 pid 覆盖成新的**，首个 pid 从此无人引用（wait/interrupt/close 都摸不到），却继续烧真实 LLM 花费；且 B2 给 spawn/close/followup 都加了 `submission_id`，唯独漏了 resume，重放即起第二个进程。**✅ 已修复（2026-07-30）** |

**由此得出的顺序：M5 → B1 → B3 → B2 → M6 → M7**（M6/M7 是做完 B2 后回头查
"还有哪个控制面 op 会悄悄多起一个进程" 时发现的）。理由见第 4 节。

## 1. M5：registry.json 的无锁读改写（新发现）

### 1.1 代码事实

`subagent_registry.py:321-329`：

```python
def _load(self):
    data = read_json_or_none(self.path) or {}
    ...
def _save(self, data):
    data["updated_at"] = now_iso()
    atomic_write_json(self.path, data)
```

`create_child()` / `update()` / `mark_closed()` / `_reap_stale_agents()` 全部是
`_load()` → 改 dict → `_save()` 的读改写，**全仓 grep 不到 registry 用 `cross_process_lock`**。
这与 M1 修复前的 `SubagentMailbox.enqueue()` 是同一种结构。

**写者不止一个进程**：`agentmain.py:1132` 里每个会 spawn 子 agent 的子进程都自建
`SubagentManager`，也就自建了一个 registry 写者；`registry.json` 是整棵 agent 树共享的
单一文件（`temp/subagents/registry.json`）。G1 的深度/活跃上限、stale-row 回收、
`descendants()`（cascade close）全部读这一个文件。

### 1.2 实测：丢行

探针：4 个线程各持独立 `SubagentRegistry` 实例（模拟独立进程），`threading.Barrier`
对齐起跑，各 `create_child()` 40 次，共 160 次。

```
writers=4 rounds=40 expected rows=160
rows actually persisted = 40  (lost 120)
duplicate run_id count  = 0
errors raised           = 0
VERDICT: registry.json read-modify-write LOSES rows
```

丢失量 = `(writers-1) × rounds` = 120，与 M1 修复前 mailbox 的 75% 丢失率**数字与公式完全一致**，
说明同样是**结构性必然而非偶发竞态**。

### 1.3 实测：run_id 碰撞（比丢行更严重）

丢行探针只看幸存者，但真正要紧的是 `create_child()` **返回给各调用方**的东西。
第二个探针记录每次调用拿到的 `(agent_path, run_id, artifact_dir)`：

```
create_child calls returned = 120
distinct run_ids handed out = 48
run_ids handed to >1 agent  = 41  (max reuse 4)
  e.g. run_000001 handed to 3 agents: ['/root/w3_0', '/root/w2_0', '/root/w0_0']
duplicate agent_paths       = 0
VERDICT: run_id COLLIDES -> shared artifact_dir, shared channel address, shared authkey path
```

`run_id` 不是一个装饰性编号，它决定三样东西：

| 派生物 | 位置 | 碰撞后果 |
| --- | --- | --- |
| `artifact_dir` | `subagent_registry.py` `create_child()`：`registry_dir/runs/<run_id>` | 两个 agent 的 final artifact 写进同一目录，互相覆盖 |
| realtime 通道地址 | `subagent_realtime_ipc.py` `default_channel_address()`：`ga_subagent_<run_id>` | 两个 agent 抢同一个命名管道 / socket 地址 |
| authkey 侧车路径 | 通道地址对应的 task dir `ipc_authkey` | 两个 run 共用一份密钥 |

第三条**直接绕开 S1/S2 刚建立的隔离**：S1 的立论是"每个 run 一把独立密钥"，S2 的立论是
"owner 校验保证地址不被冒名"。run_id 碰撞让"每个 run 一把"变成"三个 run 一把"，
而 owner 校验只验用户身份、验不出"这条管道属于哪个 run" —— 两道防线都还在，
但它们保护的粒度被悄悄放大了。**这是把 M5 判为 P0 而非 P1 的决定性理由。**

### 1.4 实测：Windows os.replace 抛错

第二个探针跑出来时直接带一条真实 traceback：

```
PermissionError: [WinError 5] 拒绝访问。:
  '...\temp\subagents\.registry.json.25160.1300.tmp' -> '...\temp\subagents\registry.json'
  @ subagent_state.py:62, _replace_file
```

M2 修 mailbox 时把 `_WINDOWS_REPLACE_RETRY_DELAYS` 调成
`(0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.8)`，但那次只覆盖了"写入期持续并发读"
的场景。registry 是**并发写**，重试预算在多写者互相 replace 时依然被打穿。
加锁后这条自然消失（锁把并发写序列化），所以不需要再动重试预算。

### 1.5 为什么之前的测试没抓到

`tests/test_subagent_registry.py` 现有 21 个测试全部是单线程顺序调用。M1 补的并发红测
只覆盖 mailbox 与 event bus，没有人给 registry 写过并发用例 —— 而 registry 恰好是
G1、cascade close、stale-row 回收三项功能共同的数据底座。

### 1.6 修复结果（2026-07-30，已完成）

`SubagentRegistry` 新增 `registry.json.lock` + `_write_locked()`，委托 M1 已有的共享
`cross_process_lock`；`create_child` / `update` / `mark_closed` / `mark_running` 全程持锁。
落地细节与三条实现决定见规划文档 §1.5 的"M5 落地记录"。同规模复验：

```
expected=240 persisted=240 returned=240 distinct_run_ids=240 errors=0
```

（4 writer × 60 轮，修复前同规模为 75% 丢行 + 大量 run_id 复用；Windows `PermissionError` 亦消失。）

三条并发红测已固化在 `tests/test_subagent_registry.py::RegistryConcurrencyTest`。

## 2. B1：publish() 阻塞 —— 前提为真，爆炸半径小于文档描述

### 2.1 前提确认

`subagent_realtime_ipc.py` `publish()` 对每个 subscriber 顺序 `conn.send(event)`，
无队列、无超时、无 lagged 标记。探针：连一个订阅者，收完 ack 后**再也不 recv**，
然后从工作线程持续 publish。

第一版探针用 4KB 填充事件，**第 2 条就卡死**。但 4KB 不是 GA 的真实事件大小，
所以换成真实 `message_queued` 事件形状重测：

```
one pickled event = 329 bytes
events delivered before publish() blocked = 24 (finished=False)
VERDICT: blocks after ~24 unread real events
```

24 条是管道缓冲的真实容量，这个数字才是评估风险的依据。

### 2.2 文档推断中未能复现的部分

规划文档 §6 B1 写"一个卡住的订阅者能拖慢整个事件写入链路"。实测未复现：
让子 agent 处于"已订阅但忙在 LLM turn 里不读通道"的状态，父侧连发 4 次 `followup_task`：

```
followup_task #1  0.01s
followup_task #2  0.02s
followup_task #3  0.03s
followup_task #4  0.03s
VERDICT: no parent stall observed
```

原因在 `subagent_manager.py` `_publish_realtime_event()`：它按 `event["agent_path"]`
只 fan-out 给**那一个** agent 的通道，且每个通道只有 1 个订阅者。所以不存在
"一个慢订阅者影响其他 agent"的路径 —— 阻塞窗口仅在"某个子进程正好在 turn 中
且父对它连发 24+ 条事件"时出现。

**结论**：B1 该修（阻塞是真的，且 `publish` 在 `append_event` 的锁释放之后、
调用方栈上同步执行），但它是**单 agent 局部风险**，不是文档描述的全局链路风险。
优先级维持 P2，排在 M5 之后。

### 2.3 修复结果（2026-07-30，已完成）

`_SubscriberSink`：每订阅者一条 bounded deque（`queue_size` 默认 64）+ 独立发送线程；
`publish()` 退化为纯入队，返回值语义改为"已入队条数"；队列满丢最老一条并**插队**投出
`{"type": CHANNEL_LAGGED, "dropped": n}`（只有这两个键，维持 R2 的"realtime 不带正文"）。
`close()` 用 `os.dup` + `shutdown(SHUT_RDWR)` 把卡在 POSIX `send()` 里的线程叫回来。
落地细节与三条实现决定见规划文档 §6 的"B1 落地记录"。同规模复验（`queue_size=4`，
订阅者收完 ack 后不再 recv）：

```
200 publishes against a non-reading subscriber: all returned promptly (pre-fix: blocked at #24)
CHANNEL_LAGGED marker delivered with dropped > 0
healthy subscriber on the same channel still received events in order
no ga-subagent-realtime* threads left alive after close()
```

五条测试固化在 `tests/test_subagent_realtime_ipc.py::ChannelSlowSubscriberTest`。

## 3. B3：顺带量到的写放大

`wait_agents()` 每个 poll 周期对每个 target 调 `read_agent()`，而 `read_agent()` 无条件
`atomic_write_json(state.json)` + `_write_registry_entry()`。实测 4 agent、2s 超时、
0.5s 间隔：

```
total atomic_write_json during wait = 40
by file = {'registry.json': 20, 'state.json': 20}
```

每秒 20 次原子写，每次都是 tmp 文件 + `os.replace`。

**这一条改变了 B3 的性质**：这 20 次 `registry.json` 写正是第 1 节那个无锁竞态的主要喂食者。
也就是说 `wait_agents` 自己在高频喂 M5 的竞态。**先修 M5，B3 才是纯优化；
反过来先做 B3 只是在竞态之上叠优化。**

### 3.1 修复结果（2026-07-30，已完成）

写放大半：新增 `SubagentManager.probe_agent()`（`_refresh_state(persist_side_effects=False)`），
`wait_agents` 的检测循环改用它；真要上报时 `_states_for_events()` 仍走 `read_agent()` 落盘。
watch 半：`SubagentRealtimeChannel.wait_for_signal()` +
`SubagentRealtimeSubscriber.signal()` 双向复用同一条 Connection，
子侧在 `agentmain._subagent_event()` 末尾统一 `_signal_parent()`；
无通道时保留盲 sleep。**先用探针验证了双向复用的安全性**（300 轮交错 send/recv，
父侧 `wait()` 阻塞读 + sink 线程并发写同一 handle，零错误、300/300 信号到达），
否则这条路根本不能走。落地细节与四条实现决定见规划文档 §6 的"B3 落地记录"。
同规模复验（4 agent / 2s / 0.5s）：

```
agents=4 timeout=2.0s interval=0.5s timed_out=True
total atomic_write_json during wait = 0
by file = {}
VERDICT: wait is write-free
```

15 条测试固化在 `WaitAgentsWriteAmplificationTest` / `WaitAgentsWatchTest`（manager）与
`ChannelUpstreamSignalTest` / `ChildEventSignalsTheParentTest`（realtime ipc）。

## 3.2 B2：实测推翻"纯结构收益"的判断

本文初版把 B2 记为"纯结构收益，没有实测出来的正确性问题"（见 §4 第 4 条的原文）。
真去测之后这个判断不成立——**控制面 op 没有身份，重放就重复执行**：

```
followup_task 同一逻辑提交重放两次 → mailbox rows = 2, trigger_turn rows = 2   （子 agent 把任务干了两遍）
spawn_agent   同一逻辑提交重放两次 → /root/dup + /root/dup_1, run_000001 + run_000002 （两个真实进程、两份 LLM 花费）
SubagentMailbox.enqueue 已有 message_id 去重分支，但上层 13 个 handler 从未有人传过 id
SubagentMailbox.enqueue 没有 other_recipients 参数（Codex Op::InterAgentCommunication 有）
```

第三行是关键：去重能力一直在，只是没有任何调用方使用它。Codex 给每个 op 都挂
`Submission { id, op, trace }`（`codex-rs/protocol/src/protocol.rs`）正是为此 ——
op 自带身份，而不是让调用方祈祷它只到达一次。

### 3.3 B2 修复结果（2026-07-30，已完成）

- 消息面：`_submission_message_id()` 把提交 id 推导成 `sub_<id>_<recipient>` 的 mailbox 行 id，
  复用 mailbox 已有的 `message_id` 去重分支 —— 去重发生在唯一真相源上，不另立账本。
- spawn / close：新增 `subagent_submissions.py::SubagentSubmissionLog`
  （`submissions.jsonl` + `cross_process_lock`，首写为准，`MAX_ROWS=2000`）；
  `spawn_agent` / `close_agent` 入口查重、出口记 `asdict()` 快照，
  重放时用 `dataclasses.fields` 过滤重建首次返回值，**任务目录已被删掉也不抛错**。
- 多播：`enqueue` 落 `other_recipients`，`SubagentMailbox.annotate()` 事后补同收者列表
  （多播只有解析完所有收件人才知道完整 peer 列表，而补写绝不能让已成功的投递失败）。

同规模复验：

```
followup_task 重放 → trigger_turn rows = 1（首次 message_id 与重放返回值一致）
spawn_agent   重放 → popen 调用 1 次，agent_path/run_id 与首次相同，list_agents() = 1
close_agent   重放（且已 rmtree 任务目录）→ 不抛错，previous_state.agent_path = /root/vanishing
无 submission_id 时两次同文本 followup_task 仍是 2 条 —— 去重靠 id，不靠文本
```

16 条测试固化在 `tests/test_subagent_submissions.py::SubmissionLogTest`（6）、
`tests/test_subagent_manager.py::SubmissionIdempotencyTest`（5）与 `MulticastMessageTest`（5）。

### 3.4 B2 的遗留问题：去重是 opt-in 的

B2 落地后被问到"重复执行的 bug 修好了吗"，诚实的答案是：**机制在，但要模型自己带 id**。
`ga.py` 读的是 `args.get("submission_id")`，GA 既不自动生成也没有自动重试路径，
不带 id 的两次调用仍然是两条消息 —— 这是刻意的（`test_without_a_submission_id_two_calls_stay_two_messages`
固化了它），因为去重必须靠 id 而不是文本：两次同文本的 followup 可能是两个真实意图。

先查了两个参考实现有没有"分发层自动派 id"这一层，结论是**都没有**：

- **Codex**：`Submission.id` 由宿主生成（`core/src/session/mod.rs:688` `Uuid::now_v7()`），
  doc comment 明说它存在的目的是"to correlate with Events"。`submission_loop`
  （`handlers.rs:715`）只在 tracing 里碰 `sub.id`，没有任何已执行集合。
  `SendMessageArgs` / `SpawnAgentArgs`（`message_tool.rs:36-47`、`spawn.rs:244-253`）
  根本不带 id。唯一以模型给的 id 做 at-most-once 的地方是 agent-jobs 对 `item_id`
  的 SQL compare-and-set（`agent_jobs.rs:445-463`）。
- **Claude Code**：宿主层完全没有幂等。`AgentTool.tsx:82-100` 的 schema 没有幂等键；
  `spawnMultiAgent.ts:268-293` 对重名直接追加 `-2`/`-3`；`inProgressToolUseIDs`
  在完成时被删除（`toolOrchestration.ts:179-188`），结构上不可能当已执行集合用。
  唯一的守卫 `handledToolUseIds`（`cli/print.ts:5272`）只服务 SDK/bridge 的
  orphaned-permission WebSocket 重连。

所以两家真正的护栏都不是 id，而是**语义唯一性**：Codex 在 `registry.rs:247-250` 对
已存在的 agent path 直接 `Err(UnsupportedOperation)`。GA 的 `agent_loop.py:68-92` 没有任何
重放路径（无重试、无 WebSocket 重连、无 orphaned-permission 再入），在分发层自动派 id
挡不住唯一真实的重复来源 —— 模型在**新一轮**里重发同一个调用，那时任何按
`(session_id, turn, index)` 推导的 id 都不一样。

因此这里的取舍是：**不在分发层造 id，改为补语义唯一性守卫（M6/M7），
并把 schema 措辞从"可选"改成带触发条件的义务**（Codex 就是靠措辞让模型交出 `item_id` 的）。

## 3.5 M6：活着的同名 agent 被静默改名（新发现）

```
spawn_agent("reviewer", ...)                       → /root/reviewer   pid 7000（仍在运行）
spawn_agent("reviewer", ...)  ← 模型以为在复用它    → /root/reviewer_1 pid 7001
结果：2 个 OS 进程、2 个活跃 agent 名额、2 份真实 LLM 花费，模型以为只有 1 个 agent
```

改名本身不是 bug —— 对**已关闭或已崩溃**的 agent 它是对的：那个 agent 的 task dir
和 artifact 仍然是它做过什么的唯一证据，复用同名会把证据清掉。错的是不区分死活。

### 3.5.1 M6 修复结果（2026-07-30，已完成）

- `subagent_registry.py` 新增 `SubagentNameConflictError`（带 `agent_path` 字段，
  让工具层能把冲突路径当结构化数据给模型，而不是让它去解析句子）。
- `_unique_child_name_unlocked` 在写锁内判活：活着 → 抛错；已关闭/崩溃 → 照旧改名。
  判活规则与 `_reap_stale_agents` 一致，包括"**判断不了就算活着**" ——
  这里猜死会静默复用一个正在运行的 agent 的名字，正是要修的缺陷。
- `SubagentRegistry.reject_if_live()` 作为**只读**前置检查暴露给 manager：
  task dir 是按 task name 命名的（`temp/<task_name>`），如果让 `create_child`
  把 agent path 改名而 manager 仍用请求的目录名，新 agent 会拿到旧 agent 的目录。
  所以拒绝必须发生在推导目录**之前**。
- 拒绝走 `_record_spawn_rejection()` 记 `spawn_rejected` 事件（与 G1 树上限拒绝同一出口）。
- 错误文案是写给模型的，点名三个替代动作：
  `Use followup_task to give it more work, close_agent to end it first, or pick a different task_name.`
  `ga.py` 的 `_subagent_error_result()` 对这个错误类**跳过 `format_error`**，
  不让 `@ file:line, func -> \`src\`` 的噪声把一个刻意的策略答复伪装成 GA 崩溃、
  诱导模型换个畸形名字重试。

## 3.6 M7：resume_agent 完全没有守卫（新发现）

```
案例 1：resume 一个进程仍活着的 agent
  spawn worker → pid 5000（活着），state.json pid = 5000
  resume worker → 返回成功，pid 5001，state.json pid 被覆盖成 5001
  => pid 5000 从此无人引用：wait/interrupt/close 都摸不到它，但它继续烧真实 LLM 花费

案例 2：同一逻辑提交重放 resume
  resume(submission_id 不存在这个参数) × 2 → 2 个 resume 进程
  => B2 给 spawn/close/followup 都加了 submission_id，唯独漏了 resume
```

`interrupt_agent` 天然幂等（只写 `_stop` 文件），所以只有 resume 是缺口。

### 3.6.1 M7 修复结果（2026-07-30，已完成）

- `resume_agent` 新增 `submission_id`，走 B2 同一套 `_replayed_submission` /
  `_record_submission`，重放时用 `_resume_result_from_submission()` 重建首次的
  `ResumeResult`（含 handle 快照与 resume_context）。
- 新增 `_reject_resume_of_a_live_agent()`：复用 `SubagentNameConflictError`，
  因为从模型视角这就是同一件事（要的 agent 已经活着），三个替代动作也一样。
  判活用 `_is_live_state()`，同样"判断不了就算活着"。
- `resume_agent` 的 schema 描述与错误文案都改为指向 `followup_task`：
  给活着的 agent 加活儿本来就该用它。

同规模复验：

```
resume 活着的 agent  → 抛 SubagentNameConflictError，popen 调用数不变，state.json pid 仍是首个 pid
resume 已关闭的 agent → 照旧成功，起 1 个进程
resume 重放（同 submission_id）→ popen 总数不变，pid/run_id/target 与首次一致
不带 submission_id 的第二次 resume → 仍是新一轮（两次 deliberate resume 是合法的）
```

12 条测试固化在 `tests/test_subagent_manager.py::SpawnNameConflictTest`（6）、
`ResumeAgentGuardTest`（4）、`tests/test_subagent_registry.py` 的两条新用例，
外加 `tests/test_ga_subagent_tools.py` 的 4 条（冲突文案无 traceback 噪声、
真实崩溃仍带 traceback、resume 冲突同样处理、schema 措辞含触发条件）。

### 3.7 真实 API E2E：把被 stub 掉的那一环补上（2026-07-30）

上面 §3.5 / §3.6 的 14 条单测有一个共同的空洞：它们都注入了假的 `process_exists`。
而 M6 / M7 的**唯一判定输入**就是真实进程是否活着（默认走 `psutil.pid_exists`）。
所以单测证明的是"给定判活结果，分支走对了"，没有证明"判活本身在真实进程上成立"。

`tests/real_subagent_guard_e2e.py`（`GA_RUN_REAL_API_E2E=1` 开关，
`claude-opus-5` / `provider: gorouter`，保留 `SECRET_RE` / `sanitize()`）补的就是这一环：
spawn 一个真实子进程 → 等它跑完一轮真实 LLM 并停在 `waiting_reply` →
让守卫对着真 pid / 真 registry 行 / 真 OS 进程判活。进程数用
`psutil.process_iter` 扫 `agentmain.py` 命令行按 task_name **精确**匹配，
而不是脚本自己的 popen 计数 —— 否则"被拒绝的调用有没有偷偷起进程"就是在验脚本的账本。

实跑 `passed: true`，`issues: []`。八段断言与实测值见
`docs/ga_subagent_ipc_implementation_plan_2026-07-29.md` §1.7，
其中最关键的三个：`realPidAlive: true`（守卫确实是在对活进程判活）、
`newProcessesAfterRefusal: []` / `newProcesses: []`（拒绝后 OS 层面真的没有新进程）、
`originalArtifactIntact: true`（close 后改名，旧 agent 的 `output.txt` 逐字节完好）。

## 4. 优先级判断

1. **M5（P0）**：实测 75% 丢行 + run_id 碰撞绕过 S1/S2 的隔离粒度 + Windows 直接抛错。
   修法与 M1/M2 完全同构（复用现成的 `cross_process_lock`），成本低、风险低、收益确定。
   **✅ 已完成（2026-07-30）**，见 §1.6。
2. **B1（P2）**：阻塞前提已证实（24 条阈值），改 per-subscriber bounded 队列 + lagged 标记。
   与 R2 的"realtime 只发空信号、正文永远从 durable mailbox 读"天然契合 ——
   **丢事件不丢消息**，所以队列满时直接丢弃是安全的。**✅ 已完成（2026-07-30）**，见 §2.3。
3. **B3（P2）**：改 watch 语义，顺带砍掉每秒 20 次原子写。必须排在 M5 之后。
   **✅ 已完成（2026-07-30）**，见 §3.1。
4. **B2（P2 → 实测为正确性问题）**：本文初版判为"纯结构收益，没有实测出来的正确性问题"，
   实测推翻 —— 重放 `followup_task` 让子 agent 干两遍任务、重放 `spawn_agent` 起第二个进程。
   **✅ 已完成（2026-07-30）**，实测证据见 §3.2，修复见 §3.3，遗留取舍见 §3.4。
5. **M6（P1）**：活着的同名 agent 被静默改名成第二个进程。与 Codex 同构（语义唯一性拒绝），
   收益确定：省掉一个进程 + 一份 LLM 花费，且模型不再对"有几个 agent"产生错觉。
   **✅ 已完成（2026-07-30）**，见 §3.5。
6. **M7（P1）**：`resume_agent` 既能 resume 活着的 agent（把首个 pid 变成无人引用的孤儿进程），
   又漏了 B2 的 `submission_id`。是 M6 的同类缺陷，只是发生在另一个入口。
   **✅ 已完成（2026-07-30）**，见 §3.6。

M6 / M7 / B2 三者的真实 API E2E 验收见 §3.7 —— 单测把判活 stub 掉了，
必须再补一条真对真实进程的链路。

同样维持后放：S8（subagent 与 workflow child agent 抽象统一，文档自评"v2 稳定后再做"）、
S11 remote isolation（文档自评优先级低）、`reply.txt` 移除（P1 保留、P2 移除）。

## 5. 探针复现方式

探针脚本按既有约定用后即删（`temp/_probe_*.py`），要点记录在此以便复现：

| 探针 | 做法 | 关键断言 |
| --- | --- | --- |
| registry 丢行 | 4 线程 × 独立 `SubagentRegistry` 实例 × `threading.Barrier` 对齐 × `create_child()` 40 次 | 幸存行数 vs 期望行数 |
| registry run_id | 同上，但记录每次 `create_child()` **返回值**的 `(agent_path, run_id, artifact_dir)` | `Counter(run_id)` 里 `n > 1` 的个数 |
| publish 阻塞 | 订阅者收完 ack 后不再 recv，工作线程持续 publish，主线程 `join(timeout)` | 线程是否在超时内跑完 + 卡死前发出的条数 |
| wait 写放大 | monkeypatch `subagent_state/manager/registry` 三处 `atomic_write_json` 计数，跑一次 `wait_agents` 超时周期 | 按文件名分类的写次数 |
| 通道双向复用 | 父侧线程 `multiprocessing.connection.wait([sink.conn])` 阻塞读，同时 sink 发送线程持续写同一 handle，子侧交错 recv/send 300 轮 | 有无异常 + 上行信号到达数 |
| 提交重放 | 对同一逻辑提交重复调 `followup_task` / `spawn_agent`，统计 mailbox `trigger_turn` 行数与 `popen` 调用次数 | 行数 / 进程数是否 > 1 |
| 同名活 agent | 注入 `process_exists=lambda _: True` 让首个 agent 恒定判活，再 spawn 同名 | 第二次拿到的 `agent_path` 与 `popen` 调用次数 |
| resume 守卫 | 注入 `process_exists` 控制死活，对活着 / 已关闭 / 重放三种情形各跑一次 resume，记录 `popen` pid 序列与 `state.json` 的 pid | 进程数是否增加 + `state.json` 的 pid 是否被覆盖 |

写并发探针时有一处要注意：`SubagentRegistry` 默认 `max_active_agents=8`，
不显式传 `max_depth=0, max_active_agents=0` 关掉上限的话，探针会先撞 G1 的
`SubagentTreeLimitError` 而不是测到竞态。

另一处：判活相关的探针和测试都必须**自己注入 `process_exists`**。默认实现会去问
`psutil.pid_exists`，于是断言变成"宿主上恰好有没有这个假 pid"，在别人机器上会翻脸。
M6/M7 的所有用例都显式传了它。

## 6. 相关文档

- 任务分解与 TDD 要求：`docs/ga_subagent_ipc_implementation_plan_2026-07-29.md`
- 设计与缺陷全景：`docs/ga_subagent_v2_optimization_design_2026-07-27.md`
- Codex / Claude Code 参考实现：`docs/ga_subagent_claudecode_codex_ipc_reference_2026-07-29.md`
