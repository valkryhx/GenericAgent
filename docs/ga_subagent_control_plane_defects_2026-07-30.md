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

**由此得出的顺序：M5 → B1 → B3 → （B2 继续后放）**。理由见第 4 节。

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

## 4. 优先级判断

1. **M5（P0）**：实测 75% 丢行 + run_id 碰撞绕过 S1/S2 的隔离粒度 + Windows 直接抛错。
   修法与 M1/M2 完全同构（复用现成的 `cross_process_lock`），成本低、风险低、收益确定。
   **✅ 已完成（2026-07-30）**，见 §1.6。
2. **B1（P2）**：阻塞前提已证实（24 条阈值），改 per-subscriber bounded 队列 + lagged 标记。
   与 R2 的"realtime 只发空信号、正文永远从 durable mailbox 读"天然契合 ——
   **丢事件不丢消息**，所以队列满时直接丢弃是安全的。**✅ 已完成（2026-07-30）**，见 §2.3。
3. **B3（P2）**：改 watch 语义，顺带砍掉每秒 20 次原子写。必须排在 M5 之后。
   **✅ 已完成（2026-07-30）**，见 §3.1。
4. **B2 继续后放**：要动 `subagent_manager.py` 与 13 个工具 handler，纯结构收益，
   没有实测出来的正确性问题。

同样维持后放：S8（subagent 与 workflow child agent 抽象统一，文档自评"v2 稳定后再做"）、
S11 remote isolation（文档自评优先级低）、`reply.txt` 移除（P1 保留、P2 移除）。

## 5. 探针复现方式

四个探针脚本按既有约定用后即删（`temp/_probe_*.py`），要点记录在此以便复现：

| 探针 | 做法 | 关键断言 |
| --- | --- | --- |
| registry 丢行 | 4 线程 × 独立 `SubagentRegistry` 实例 × `threading.Barrier` 对齐 × `create_child()` 40 次 | 幸存行数 vs 期望行数 |
| registry run_id | 同上，但记录每次 `create_child()` **返回值**的 `(agent_path, run_id, artifact_dir)` | `Counter(run_id)` 里 `n > 1` 的个数 |
| publish 阻塞 | 订阅者收完 ack 后不再 recv，工作线程持续 publish，主线程 `join(timeout)` | 线程是否在超时内跑完 + 卡死前发出的条数 |
| wait 写放大 | monkeypatch `subagent_state/manager/registry` 三处 `atomic_write_json` 计数，跑一次 `wait_agents` 超时周期 | 按文件名分类的写次数 |
| 通道双向复用 | 父侧线程 `multiprocessing.connection.wait([sink.conn])` 阻塞读，同时 sink 发送线程持续写同一 handle，子侧交错 recv/send 300 轮 | 有无异常 + 上行信号到达数 |

写并发探针时有一处要注意：`SubagentRegistry` 默认 `max_active_agents=8`，
不显式传 `max_depth=0, max_active_agents=0` 关掉上限的话，探针会先撞 G1 的
`SubagentTreeLimitError` 而不是测到竞态。

## 6. 相关文档

- 任务分解与 TDD 要求：`docs/ga_subagent_ipc_implementation_plan_2026-07-29.md`
- 设计与缺陷全景：`docs/ga_subagent_v2_optimization_design_2026-07-27.md`
- Codex / Claude Code 参考实现：`docs/ga_subagent_claudecode_codex_ipc_reference_2026-07-29.md`
