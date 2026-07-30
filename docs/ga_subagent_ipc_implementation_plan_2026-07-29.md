# GA subagent IPC 待实现优化规划

日期：2026-07-29

依据：`docs/ga_subagent_claudecode_codex_ipc_reference_2026-07-29.md`（Claude Code / Codex
源码级调研 + GA 缺陷实测），归属 feat：`docs/ga_subagent_v2_optimization_design_2026-07-27.md`。

## 0. 路线判定（已决策）

**不切路线。保留"独立进程 + durable 文件为权威源"，把通知信号实时化。**

三条依据：

1. **参考实现都没有父子 agent IPC。** Claude Code 子 agent 是同进程隔离 context
   （`forkedAgent.ts:345-462`），Codex 子 agent 是同进程 Tokio task
   （`session/mod.rs:662`；`core/src/agent/` 下 `Command::new` 调用点为 0）。
   所以"改走 Claude Code 路线"和"改走 Codex 路线"在传输层是**同一件事**：取消 IPC、改同进程。
   GA 是三者中唯一把子 agent 放进独立 OS 进程的，需要 IPC 是必然结果而非设计失误。
2. **同进程化的前置成本大于收益。** GA 工具层依赖进程级可变全局：
   `ga.py:437` `os.chdir(cwd)`、`agentmain.py:269-272` 模块全局 `TOOLS_SCHEMA`
   （还按模型名切中英文 schema）。同进程并发子 agent 会互相踩。
   另外 Python 同进程多 agent 共享 GIL 与异常传播面，会把 Codex 用 Rust 换来的
   隔离性代价原样承担；且失去 Windows 上最可靠的进程级取消（GA 有 `code_run()` 这类阻塞调用，
   协作式 cancel 未必及时）。
3. **GA 想做的通知模型就是 Codex 的通知模型。** Codex `wait_agent` 阻塞在
   `multi_agents_v2/wait.rs:151` `wait_for_mailbox_change(mailbox_rx: &mut watch::Receiver<()>)`，
   信号源 `session/input_queue.rs:26` `mailbox_tx: watch::Sender<()>`——负载是**空元组**，
   只说"变了，去读"。GA 要做的"realtime channel 只推 trigger、内容仍回读 durable mailbox"
   与之完全同构，只是把进程内 `watch` 换成跨进程 named pipe / socket。

同时确认 GA 的工具面已与 Codex `multi_agents_v2` **6/6 同名**
（`spawn_agent`/`wait_agent`/`close_agent`/`send_message`/`followup_task`/`list_agents`），
协议层无需改造。**剩余工作全部在传输与并发正确性上。**

## 1. 任务总表（按优先级）

P0 是正确性/安全，必须先做；P1 是本次要达成的实时性目标；P2 是控制面清晰度（纯收益、不动进程模型）。

| ID | 优先级 | 任务 | 类型 | 依赖 |
| --- | --- | --- | --- | --- |
| M1 | **P0** | mailbox 加跨进程锁，消除并发丢消息 | 正确性 | — |
| M2 | **P0** | mailbox 落盘改原子替换，消除截断脏读 | 正确性 | M1 |
| M3 | **P0** | 消除 `message_id` 行数派生碰撞 | 正确性 | M1 |
| M4 | **P0** | 统一两套分叉的 mailbox 消费实现，删死代码 | 正确性 | M1, M2 |
| **M5** | **P0** | `registry.json` 加跨进程锁，消除并发丢行与 `run_id` 碰撞 | 正确性 + **安全** | — |
| S1 | **P0** | realtime channel 加 per-run authkey 认证 | **安全** | — |
| S2 | P1 | POSIX socket 目录权限 0o700 / Windows server owner 校验 | 安全 | S1 |
| R1 | **P1** | 子 agent 订阅 realtime channel（接通现有死通道） | 实时性 | S1 |
| R2 | **P1** | mailbox trigger 走 realtime 通知，durable 仍为权威源 | 实时性 | M1, R1 |
| R3 | P1 | 轮询粒度与空转寿命解耦（`poll_interval_s` / `idle_timeout_s`） | 实时性 | R1 |
| R4 | P1 | realtime 不可用时优雅退回轮询，且状态可观测 | 健壮性 | R1 |
| G1 | **P1** | agent 深度 / 总数上限守护 | 安全 | — |
| B1 | P2 | realtime channel 补 bounded + Lagged 背压（**✅ 2026-07-30 已完成**） | 健壮性 | R1 |
| B2 | P2 | `spawn/send/close/interrupt` 收敛为显式 `Op` 结构（**✅ 2026-07-30 已完成**） | 控制面 | — |
| B3 | P2 | 状态订阅从轮询 `state.json` 改 watch 语义（**✅ 2026-07-30 已完成**） | 控制面 | R1, B2 |
| **M6** | **P1** | 活着的同名 agent 拒绝 spawn，不再静默改名（**✅ 2026-07-30 已完成**） | 正确性 + 花费 | M5 |
| **M7** | **P1** | `resume_agent` 拒绝 resume 活着的 agent，并补 `submission_id`（**✅ 2026-07-30 已完成**） | 正确性 + 花费 | B2 |

**不在范围内**：同进程运行时 / asyncio 化（4.2.1 阶段 C，除非出现第 5 节的切换信号）；
`workflow_runtime.py` / `workflow_scheduler.py` / `workflow_child_agent.py` 重构（本 feat 明确边界）。

> **M5 是 2026-07-30 复核时新增的（原表 14 项 → 15 项）**，不是原始规划的一部分。
> 它与 M1 是同一种结构缺陷（无锁读改写），但作用在 `registry.json` 而非 mailbox 上，
> 且后果更重：`run_id` 碰撞会让 S1/S2 建立的"每 run 一把密钥"隔离粒度被悄悄放大。
> 实测证据见 `docs/ga_subagent_control_plane_defects_2026-07-30.md`，任务详情见本文 §1.5。
> 同一轮复核还实测校准了 B1 的真实爆炸半径、量到了 B3 的写放大，结论都在那份文档里。

> **M6 / M7 是 B2 完成后（2026-07-30）新增的（15 项 → 17 项）**。做完 B2 的提交幂等之后
> 回头查"还有哪个控制面 op 会悄悄多起一个进程"，量到两处：同名 spawn 对**活着的** agent
> 静默改名（M6），以及 `resume_agent` 既能 resume 活着的 agent、又漏了 `submission_id`（M7）。
> 两者都是"模型以为在复用一个 agent，实际多了一个进程 + 一份真实 LLM 花费"，
> 与 Codex 的语义唯一性拒绝（`codex-rs/core/src/agent/registry.rs:247-250`）同构。
> 详情见本文 §1.6，实测证据见缺陷文档 §3.5 / §3.6。

### 1.1 实施进度（2026-07-29）

切片 1（M1→M2→M3→M4，P0 正确性）**已完成**。全部按 TDD 落地：先写红测、确认红、最小实现、转绿。

| ID | 状态 | 落地内容 | 红测证据 |
| --- | --- | --- | --- |
| M1 | ✅ 完成 | `subagent_state.py` 新增共享 `cross_process_lock(lock_path)`；`subagent_event_bus._locked()` 改为委托它（消除第二套锁实现）；`SubagentMailbox` 持 `mailbox.jsonl.lock`，`enqueue()` / `consume_trigger_turn()` 全程持锁 | `expected 6 persisted messages, got 1`；`msg_new was neither persisted nor consumed, i.e. lost` |
| M2 | ✅ 完成 | `subagent_state.py` 新增 `atomic_write_lines(path, lines)`（tmp + `_replace_file`）；`SubagentMailbox._write_rows()` 改用它，不再 `open(...,"w")` 截断 | `41 truncated reads observed, e.g. row counts [0,0,0,0,0] < 40` |
| M3 | ✅ 完成 | `_new_message_id(rows)` 改为跳过已占用 ID（不再纯行数派生），避免碰撞被 `:35-36` 去重分支伪装成幂等命中 | `['explicit'] != ['explicit', 'auto']` |
| M4 | ✅ 完成 | 删除 `subagent_state.consume_mailbox_trigger()` 死实现与 `agentmain.py:27` 的 import；`SubagentMailbox.consume_trigger_turn()` 成为唯一消费实现 | `subagent_state still exposes a second mailbox consumer` |

`tests/test_subagent_mailbox.py` 从 4 个测试增至 8 个，新增 4 个均为并发/结构红测。

**M3 实现与原方案的偏差（有意）**：原方案写"改为 `uuid4().hex[:12]`"。实际改为
"行数起步 + 跳过已占用 ID"，保留 `msg_%06d` 格式。原因：`agentmain.py:980` 与
`subagent_manager.py:1107` 都把 `message_id` 透出到事件与工具返回值，换成随机 hex 会让
日志里的消息顺序不可读，而顺序可读性在排查父子通信时价值很高；持锁后唯一性已由
M1 保证，跳过占用只是补上"库层不依赖调用方持锁"这一条。

**顺带修的一处副作用**：M2 让 mailbox 也走 `_replace_file()`，而 Windows 上
`os.replace` 遇到读者持有目标文件句柄会抛 `PermissionError`。原重试预算
`(0.02, 0.05, 0.1, 0.2)` 在"写入期持续并发读"下不够，红测直接打出
`PermissionError(13, '拒绝访问。')`。改为 `(0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.8)`：
首个退避更短（读者 open-read-close 只需微秒级），总预算更长（读者绝不能让写入失败）。
`tests/test_subagent_state.py` 里断言首个退避值的测试改为引用
`subagent_state._WINDOWS_REPLACE_RETRY_DELAYS[0]`，不再硬编码 `0.02`。

**回归基线**：subagent 相关 `Ran 130 tests ... OK`；全量 `Ran 748 tests in 106.855s ... OK (skipped=1)`
（较上次基线 743 增加的 5 个即本次新增测试）。

切片 2（S1，P0 安全）**已完成**。

| ID | 状态 | 落地内容 | 红测证据 |
| --- | --- | --- | --- |
| S1 | ✅ 完成 | `subagent_realtime_ipc.py` 新增 `new_channel_authkey()`（`secrets.token_bytes(32)`）/ `write_channel_authkey()` / `read_channel_authkey()` / `remove_channel_authkey()`；`SubagentManager._env_realtime_channel_factory()` 为每个 run 生成独立 authkey；spawn 建好 task dir 后由 `_write_realtime_authkey()` 把密钥写进 `<task_dir>/ipc_authkey`（`chmod 0o600`，POSIX 生效）；`close_agent` 关通道时一并删除密钥文件 | `None is not an instance of <class 'bytes'>`；`cannot import name 'read_channel_authkey'`；`b'cccc...' is not None` |

**密钥不外泄的三条保证**（均有测试断言）：
- 密钥走 task dir 侧车文件，**不进 `state.json`**——`state.json` 会被 `read_agent` 返回给 LLM。
- `ipc_endpoint`（`subagent_realtime_ipc.py` `endpoint()`）只含 status/address/family/subscriber_count，
  测试直接断言序列化后的 `ipc_endpoint` 里不出现 `authkey` 字面量。
- 通道关闭即删密钥，避免留下一个已无用途的秘密。

**回归**：`Ran 134 tests ... OK`（较 M 系列的 130 增加的 4 个即 S1 新增测试）。

下一步：切片 3（R1 子 agent 订阅 realtime channel → R2 → R4 → R3）。R1 之所以必须排在 S1 之后：
R1 会让这条通道从"没人用的摆设"变成"承载真实消息的通路"，先接通再补认证等于把泄露面从理论变成现实。

切片 3 的 R1 / R2 / R4 **已完成**（R3 待做）。

| ID | 状态 | 落地内容 | 红测证据 |
| --- | --- | --- | --- |
| R1 | ✅ 完成 | `subagent_realtime_ipc.py` 新增 `SubagentRealtimeSubscriber`（`wait(timeout)` 阻塞在 `conn.poll` 上，排空积压信号）与 `resolve_child_subscription()` / `open_child_subscription()`；`run_task_worker_loop()` 启动时读 `state.json` 的 `ipc_endpoint` + task dir 里的 authkey 完成订阅，等待回复时用 `subscriber.wait(reply_sleep_s)` 取代 `sleep_fn(reply_sleep_s)`；异常与正常退出都 `close()` | `[0] != [1]`（子从未订阅）；`[0.05, 0.05] != []`（持有订阅仍在盲睡） |
| R2 | ✅ 完成 | 无需新增发布代码——`SubagentManager._queue_message()` 落盘后已经走 `event_bus.append_event("message_queued", ...)`，而 event bus 的 `_publish()` 会把事件 fan-out 到 channel。R1 接通订阅后这条链路自动成立。本项落的是**契约测试**：断言 (a) 先落盘、(b) 通道上收到 `message_queued` 信号、(c) 信号里**不含消息正文**、(d) 信号丢失时轮询兜底仍能送达 | 见下方说明 |
| R4 | ✅ 完成 | 子侧订阅结果写入 `state.json` 的 `child_ipc_status`（`subscribed` / `fallback` / `file`）+ `child_ipc_fallback_reason` + `child_ipc_address`，并在非 `file` 时补一条 `child_ipc_status` 事件；运行中通道被父侧关闭会重新记为 `fallback` 且原因含 `disconnected mid-run` | `KeyError: 'child_ipc_status'` ×3；`'subscribed' != 'fallback'`（中途断连未被记录） |

**R2 是唯一一项红测一开始就绿的任务**，需要说明清楚：R1 落地后 `_queue_message` →
`event_bus.append_event` → `_publish` → channel → subscriber 这条链路已经自然连通，
所以 R2 的价值不是"写新代码"，而是**把不可妥协的设计约束固化成断言**：
realtime 事件只是 Codex `watch::Sender<()>` 式的空信号，正文永远从 durable mailbox 读。
测试额外开了一个裸 `connect_realtime_channel` 连接直接检查载荷，
断言 `"second turn please" not in json.dumps(signal)`——如果将来有人把正文塞进事件走捷径，
这条断言会立刻变红。这正是规划里"反模式"一节要防的退化。

**R4 的一处设计取舍**：`child_ipc_status == "file"`（未配置 realtime，绝大多数情况）
**不写事件**，只写 state 字段。否则每个子 agent 的事件流都会多一条毫无信息量的记录，
而现有生命周期测试正是按事件序列断言的（`['turn_started', 'turn_completed', ...]`）。
状态字段本身仍然写，可观测性不受影响。

**回归**：subagent 相关 `Ran 142 tests ... OK`；全量 `Ran 760 tests in 116.766s ... OK (skipped=1)`。

下一步：R3（轮询粒度与空转寿命解耦）。R1 落地后这个耦合更值得拆——
realtime 已保证低延迟，轮询间隔本该放大省 CPU，但当前放大就会缩短子 agent 寿命。

R3 **已完成**，切片 3 全部收尾。

| ID | 状态 | 落地内容 | 红测证据 |
| --- | --- | --- | --- |
| R3 | ✅ 完成 | `agentmain.py` 新增 `resolve_reply_wait_schedule()`（把 iterations×sleep 折算成 interval + timeout）、`_reply_wait_slices()`（按 deadline 产出每次等待时长，末片钳到 deadline）、`resolve_reply_wait_schedule_from_env()`（`GA_SUBAGENT_POLL_INTERVAL_S` / `GA_SUBAGENT_IDLE_TIMEOUT_S`）；`run_task_worker_loop()` 新增 `poll_interval_s` / `idle_timeout_s` / `monotonic_fn` 三个可选参数，`__main__` 的 spawn 入口读环境变量 | `unexpected keyword argument 'monotonic_fn'` ×5；`cannot import name 'resolve_reply_wait_schedule_from_env'` |

**向后兼容按"旧参数原义不变"实现**（有专门测试锁定）：只传 `reply_wait_iterations` /
`reply_sleep_s` 时，等待次数与时长与改动前逐次一致（`reply_wait_iterations=3, reply_sleep_s=0.5`
→ `[0.5, 0.5, 0.5]`）；默认值 `300×2s` 仍等于 600s 空转寿命。这一点很关键——
仓库里有 12 处调用点直接传这两个参数，静默改变时序会让"测试还绿但行为变了"。

**两个边界处理**：
- `poll_interval_s` 大于 `idle_timeout_s` 时最后一片钳到 deadline（`5.0` 间隔 + `1.0` 寿命 → 只等 `[1.0]`），
  否则粗间隔会越过寿命边界。
- `interval <= 0`（测试里大量使用 `reply_sleep_s=0`）退回按迭代次数计数，
  否则 deadline 驱动的循环会因为时间不推进而空转不止。

**为什么额外补了环境变量入口**：子 agent 由 `SubagentManager` 用固定命令行拉起
（`subagent_manager.py:454-470`，只有 task/llm_no/permission 相关参数），
不加环境变量路径的话这两个新旋钮**只有测试能用到**，真实 spawn 的子进程永远拿不到。
非法值（空串 / 非数字 / 非正数）一律回落默认而不是让启动失败。

**回归**：subagent 相关 `Ran 147 tests ... OK`；全量 `Ran 766 tests in 113.908s ... OK (skipped=1)`。

下一步：切片 4（G1 agent 深度/总数上限守护 + S2 传输层加固）。

G1 **已完成**。

| ID | 状态 | 落地内容 | 红测证据 |
| --- | --- | --- | --- |
| G1 | ✅ 完成 | `subagent_registry.py` 新增 `SubagentTreeLimitError`、`resolve_tree_limits_from_env()`（`GA_SUBAGENT_MAX_DEPTH` / `GA_SUBAGENT_MAX_ACTIVE`）、`SubagentRegistry(max_depth=3, max_active_agents=8)` 与 `_check_tree_limits()`（`create_child()` 落盘前校验）；`SubagentManager` 新增 `self_agent_path`（构造参数或 `GA_SUBAGENT_AGENT_PATH` 环境变量），spawn 以它为 parent 而非硬编码 `/root`，并通过 `_child_popen_kwargs()` 把 agent path 传给子进程；越限时先写 `spawn_rejected` 事件再抛错 | `cannot import name 'SubagentTreeLimitError'` / `'resolve_tree_limits_from_env'`；`unexpected keyword argument 'self_agent_path'`；`KeyError: 'env'`；`'SubagentManager' object has no attribute 'self_agent_path'`；`0 != 1`（拒绝无事件） |

**实现中发现的真实前置缺陷（原规划未列出）**：`spawn_agent()` 把
`parent_path=AgentPath.root()` **硬编码**，所以孙 agent 也注册成 `/root/<name>`——
**深度上限即使实现了也永远不会触发**，因为树在 registry 里永远是扁平的。
必须先让嵌套被正确记录，守护才有意义。为此加了两条：
- `SubagentManager.self_agent_path`：当前 manager 代表哪个 agent。
- spawn 时通过 `env` 传 `GA_SUBAGENT_AGENT_PATH` 给子进程。子进程跑的是同一份
  `agentmain.py` / 同一个 registry，不告诉它"你是谁"，它的 spawn 又会退回 `/root`。
  `env` 用 `{**os.environ, ...}` 扩展而非替换（有测试断言 `PATH` 仍在），
  否则子进程会丢掉整个环境。

**语义选择**：上限管的是**活跃** agent 数，不是历史累计。测试显式验证
"关掉一个就能再开一个"——累计计数会让长会话逐渐无法 spawn，那是资源治理而非安全守护。
默认 depth ≤ 3 / 活跃 ≤ 8，保守但可用环境变量放宽。

**回归**：subagent 相关 `Ran 156 tests ... OK`；全量 `Ran 774 tests in 119.629s ... OK (skipped=1)`。

S2 **已完成**，切片 4 收尾，规划内 P0/P1 全部落地。

| ID | 状态 | 落地内容 | 红测证据 |
| --- | --- | --- | --- |
| S2 | ✅ 完成 | `subagent_realtime_ipc.py`：POSIX 侧 `default_channel_address()` 在 `mkdir` 后每次都 `os.chmod(base, 0o700)`；Windows 侧新增 `validate_channel_owner()` + `_pipe_server_user_sid()` / `_process_user_sid()` / `_current_user_sid()` / `_is_pipe_address()`（ctypes 调 `GetNamedPipeServerProcessId` → `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` → `OpenProcessToken` → `GetTokenInformation(TokenUser)` → `ConvertSidToStringSidW`），`connect_realtime_channel()` 在 `Client()` 之后立即校验，失败先关句柄再抛 `PermissionError` | `does not have the attribute '_pipe_server_user_sid'`；`cannot import name 'validate_channel_owner'` |

**为什么 S1 的 authkey 不够**：authkey 只证明"对端知道这个 secret"，不证明
"这个端点值得把 secret 交出去"。剩下两个洞方向相反：

- POSIX：channel 目录按 ambient umask 创建，别的本地账号可以往里放自己的 socket
  抢地址。所以 `chmod` 每次调用都做，而不是只在新建时做——上一次跑在宽 umask 下
  建出来的目录不能永久敞着（有独立测试覆盖"目录已存在"这条路径）。
- Windows：命名管道是全局命名空间，地址由**顺序递增**的 `run_id` 推出，任何本地进程
  都能先抢注 `\\.\pipe\ga_subagent_<run_id>`，然后坐等子 agent 把 authkey 送上门。
  管道**所有者**正是 authkey 无法证明的东西。参考 Codex
  `codex-rs/tui/src/ide_context/windows_pipe.rs:263` `validate_pipe_server_owner`：
  同样是 pid → token → user SID → 比对当前用户。

**fail closed 的取舍**：owner 读不出来时按"不可信"处理并拒连。唯一会读不出来的现实原因
就是那个服务端进程不归我们、查不了；把"查不到"当"可信"等于给冒名管道开后门。
代价是子 agent 退回轮询——由测试锁住：`resolve_child_subscription()` 把
`PermissionError` 归到 `fallback` 状态并带上原因，durable mailbox 仍然送达，
所以最坏情况是延迟变差而不是消息丢失。

**平台边界**：`validate_channel_owner()` 只在 Windows 且地址是 `AF_PIPE` 时动作，
POSIX socket 直接返回 `None`——它的防线是 `0o700` 目录，不是 SID 比对；有一条
非 Windows 也会跑的测试断言这条短路存在，免得以后有人给 POSIX 路径加无意义的 ctypes 调用。

**回归**：subagent 相关 `Ran 163 tests ... OK (skipped=2)`；全量
`Ran 781 tests in 117.700s ... OK (skipped=3)`。（新增 skip 是两条 Windows-only /
POSIX-only 的平台守卫测试，在当前 Windows 环境下 POSIX 权限那两条被跳过。）

下一步：规划内 P0/P1 已全部完成。P2 的 B1 / B3 / B2 也已于 2026-07-30 全部落地
（realtime channel 背压、watch 语义状态订阅、显式提交身份 + 多播），落地记录见 §6。
规划外新增的 M6 / M7（语义唯一性守卫）也已在同日完成，见 §1.6。

### 1.2 真实 API E2E 验收（2026-07-29）

§7 要求的真实 API 验收。原计划用 `gpt-5.6-terra`（`provider: wapq`），
实跑时该渠道 `ConnectTimeout`（`hhhl` 中转在 `/v1/chat/completions` 上可用但子 agent
首轮仍超时），**改用 `claude-opus-5`（`provider: gorouter`，anthropic 线）**完成验收。
两个脚本都保留 `SECRET_RE` / `sanitize()` 脱敏。

| 脚本 | 覆盖 | 结果 |
| --- | --- | --- |
| `tests/real_subagent_terra_e2e.py` | spawn → 真实 LLM 首轮 → close → `resume_agent` 复用同 run_id 第二轮 → artifact / transcript / round 断言 | `passed: true`，`issues: []`，`finalOutputRounds: [0, 1]` |
| `tests/real_subagent_realtime_e2e.py`（**新增**） | `GA_SUBAGENT_REALTIME_IPC=1` + `ipc_mode=socket` 真实命名管道：authkey 交付 → 子进程订阅 → 真实 LLM 首轮 → `followup_task` 实时唤醒 → authkey 泄漏扫描 → close 后删密钥 | `passed: true`，`issues: []` |
| `tests/real_subagent_guard_e2e.py`（**新增，2026-07-30**） | M6 / M7 / B2 守卫对真实进程判活：真 pid + 真 registry 行 + `psutil` 扫真实子进程；详见 §1.7 | `passed: true`，`issues: []` |

realtime E2E 的关键数字：`effectiveIpcMode: socket`、`childIpc.status: subscribed`、
`subscriberCount: 1`、`authkey.bytes: 32`、`authkeyLeaks: []`、
`endpointKeys: [address, family, status, subscriber_count]`、`authkeyRemovedOnClose: true`，
以及 **`followupWakeSeconds: 0.25`**。

最后这个数字是整份规划的验收点：脚本把 `GA_SUBAGENT_POLL_INTERVAL_S=30` /
`GA_SUBAGENT_IDLE_TIMEOUT_S=600` 故意设成粗轮询 + 长寿命，所以 0.25 秒被唤醒
**只可能**来自 realtime 信号——轮询定时器要 30 秒才会醒。脚本对此设了硬断言
（≥25 秒记 `followup_woke_on_poll_not_realtime`），否则"realtime 生效"这个结论
就退化成一句无法证伪的话。§8 预期的"父→子延迟从平均 1s / 最差 2s 降到毫秒级"
至此有了实测数字。

**为什么必须补这个脚本**：单元测试里 owner 校验是 mock 的，realtime 路径也都在
进程内驱动 `run_task_worker_loop`。真实子进程 + 真实管道 + 真实 authkey + 真实
owner 校验 + 真实 LLM 轮次这一条组合路径，之前零覆盖。

### 1.3 E2E 暴露的两个真实缺陷（规划外，已按 TDD 修复）

真实跑批各暴露一个只有真实链路才会触发的缺陷。两个都是"单测全绿但功能不可用"，
正是补 E2E 的理由。

| 缺陷 | 现象 | 根因 | 修复 | 红测证据 |
| --- | --- | --- | --- | --- |
| G1 的上限守护把 registry 变成一次性资源 | 第一次跑 terra E2E 直接起不来：`SubagentTreeLimitError: active agent limit exceeded: 36 agents already active, max active is 8` | `_check_tree_limits()` 把"`status != closed`"当活跃，而 `status` 只在 `close_agent` 走完时才变 `closed`。崩溃、被 kill、机器重启留下的行永久算活跃——实测 registry 里 36 行"活跃"，实际存活进程 **1 个** | `SubagentRegistry(process_exists=...)` + `_reap_stale_agents()`：进程没了就把行改成 `closed` / `closed_status="stale"`（**不删**，行本身是崩溃的唯一证据），返回真实活跃数；`SubagentManager` 把自己的 `process_exists` 传下去 | `unexpected keyword argument 'process_exists'`；`'SubagentRegistry' object has no attribute 'process_exists'` |
| 所有 resume 轮次在 anthropic 线上必炸 | resume 第二轮输出 `ValueError: dictionary update sequence element #0 has length 1; 2 is required @ llmcore.py:842, raw_ask` | `build_resume_context()` 写出的 `_history.json` 行是 `{"role": ..., "content": "<str>"}`（content 是**字符串**），而 `BaseSession.ask()` / `ClaudeSession.make_messages()` / `NativeClaudeSession.raw_ask()` 都 `list(m["content"])`，把字符串拆成**逐字符** block，随后 `dict('s', cache_control=...)` 抛错 | 新增 `llmcore._content_blocks()`（str → 单个 text block，list → 拷贝，dict → 包一层），替换三处 `list(content)`，并在 `_fix_messages()` 入口统一归一化 | `Lists differ: ['c','o','n','t',...] != [{'type':'text','text':'continue analysis'}]`；`ValueError: dictionary update sequence element #0 has length 1; 2 is required` |

第二个缺陷的影响面比本 feat 大得多：它让**任何** anthropic-wire 后端上的 subagent
resume 都无法完成第二轮，而单测全绿是因为没有一条测试用 str content 的 history 走过
真实 payload 组装。`tests/test_llmcore_history_content.py`（6 条）把这个契约锁住，
包含一条直接打在崩溃行 `raw_ask` 上的用例。

**回归**：subagent 相关 `Ran 168 tests ... OK (skipped=2)`；全量
`Ran 792 tests in 114.913s ... OK (skipped=3)`。

### 1.4 规划外补齐：`close_agent(cascade=true)`（2026-07-30）

复核 v2 设计文档 §6 时发现一处**从未实现的 P0 子项**：§6.2 的 schema 里有 `cascade` 字段、
返回值里有 `closed_descendants`、§6.3 的 P0 测试清单里有"close descendants"，
但 `close_agent()` 从头到尾只关目标本身，`cascade` / `descendants` 在全仓 grep 零命中。

**为什么现在比设计当初更该做**：G1 落地后活跃 agent 有了硬上限，而关掉一个中间层 agent
会把它的后代留成孤儿——没人读它们的输出，却继续各占一个活跃额度，直到进程自己死掉
才被 1.3 节的 stale-row 回收注意到。cascade 是让 close 成为树操作的那一步。

| 落地内容 | 红测证据 |
| --- | --- |
| `SubagentRegistry.descendants(agent_path, include_closed=False)`：复用 `list_agents` 的前缀过滤（`path == prefix or path.startswith(prefix + "/")`，所以 `/root/a` 不误伤 `/root/ab`），排除自身，按 **段数倒序** 返回（深的在前） | `'SubagentRegistry' object has no attribute 'descendants'` ×4 |
| `SubagentManager.close_agent(..., cascade=False)`：原实现整体下移为 `_close_single_agent()`，`close_agent()` 变成"先 `_close_descendants()`，再关自己"的编排层；`CloseResult` 新增 `closed_descendants: list[dict]`；`AgentState` 新增 `close_reason` | `unexpected keyword argument 'cascade'` |
| 工具层：`ga.py do_close_agent` 透出 `cascade` 与 `closed_descendants`，中英文 schema 补 `cascade` 字段（描述里点明"只关目标会把后代留成孤儿"） | `KeyError: 'closed_descendants'`；`'cascade' not found in {...}` ×2 |

**三条实现决定与理由**：
1. **后代由深到浅关、目标最后关。** 子必须先于派生它的父消失，否则父的停机会与自己子进程的
   写入相互竞争；目标放最后是为了"cascade 中途失败时不会只剩目标还活着"。
2. **单个后代失败只记录不抛。** 某后代 `state.json` 被别的进程锁住时若直接抛出，
   剩余后代和目标都会继续运行——半棵树加一个异常是最坏结果。失败以
   `{"status": "error", "msg": ...}` 记入 `closed_descendants`，测试用注入的
   `_close_single_agent` 打桩验证这条路径。
3. **后代 `close_reason` 写 `cascade_close:<ancestor>`，不沿用父的 reason。** 事后看
   `state.json` 能直接知道它是被谁的收尾带走的，而不是显示成一次独立的 `parent_cleanup`。

**一处测试自身的修正**：最初的 fixture 给每个 agent 写了带 `[ROUND END]` 的 output，
于是 `read_agent()` 把它们全部提升为 `completed`，close 变成对"已结束进程"的 no-op，
`test_each_cascaded_descendant_gets_a_real_close_not_just_a_registry_flag` 断言
`'completed' != 'interrupted'` 而红。改掉的是 **fixture**（去掉 `[ROUND END]`，让 agent 处于
真正的 mid-turn 状态）而非实现——cascade 有意义的场景本来就只有"后代还在跑"。

**回归**：subagent 相关 `Ran 131 tests ... OK (skipped=2)` + 生命周期 17 项全绿；
全量 `Ran 804 tests in 129.133s ... OK (skipped=3)`（较上次 792 增加的 12 项即本次新增测试：
registry 4 + manager 6 + 工具层 2）。

至此 v2 设计 §6 的 P0 条目全部落地，`memory/supervisor_sop.md` 的收尾条目已改为在目标
派生过子 agent 时使用 `cascade=true`。

### 1.5 M5（P0）registry.json 加跨进程锁 —— 2026-07-30 复核新增

**实测证据全文见 `docs/ga_subagent_control_plane_defects_2026-07-30.md`**，此处只列任务要求。

**问题**：`subagent_registry.py:321-329` 的 `_load()` / `_save()` 是无锁读改写，
`create_child()` / `update()` / `mark_closed()` / `_reap_stale_agents()` 全走这条路，
全仓 grep 不到 registry 使用 `cross_process_lock`。而 `agentmain.py:1132` 里每个会 spawn
子 agent 的子进程都自建 `SubagentManager`，即自建一个 registry 写者，
`temp/subagents/registry.json` 是整棵树共享的单一文件。

**实测（4 writer × 40 轮，`threading.Barrier` 对齐）**：
- 丢行：**160 行只存活 40 行（丢 75%）**，丢失量 = `(writers-1)×rounds`，与 M1 修复前
  mailbox 的数字和公式完全一致 —— 结构性必然，不是偶发竞态。
- **`run_id` 碰撞：120 次调用只发出 48 个不同 run_id，41 个被 ≥2 个 agent 共用（最多 4 次）。**
- Windows 上并发写直接抛 `PermissionError [WinError 5] @ subagent_state.py:62, _replace_file`。

**为什么是 P0 而不是 P1**：`run_id` 决定三样东西 —— `artifact_dir`
（`registry_dir/runs/<run_id>`）、realtime 通道地址（`ga_subagent_<run_id>`）、
authkey 侧车路径。碰撞后两个 agent 共享 artifact 目录、抢同一个管道地址、读同一份密钥。
第三条**直接放大了 S1/S2 建立的隔离粒度**：S1 立论是"每 run 一把独立密钥"，
碰撞让它变成"三个 run 一把"；S2 的 owner 校验只验用户身份，验不出"这条管道属于哪个 run"。
两道防线都还在，但保护粒度被悄悄改变了。

**方案**：
- `SubagentRegistry` 持 `registry.json.lock`，复用 `subagent_state.cross_process_lock`
  （M1 已把它做成共享实现，不要写第二套锁）。
- 锁必须覆盖**整个** read-modify-write：`_load()` → 改 dict → `_save()` 不能被拆开加锁，
  否则 run_no 分配与行写入之间仍有窗口。`create_child()` 里
  `_check_tree_limits` → 分配 `next_run_no` → 写 entry 必须在同一把锁内。
- `_reap_stale_agents()` 已经 mutate `data` 后由调用方 `_save()`，加锁时注意别把
  "reap 后不保存"的既有路径改坏（`tests/test_subagent_registry.py` 有断言锁定）。
- Windows `os.replace` 的 `PermissionError` 加锁后自然消失（并发写被序列化），
  **不需要再动 `_WINDOWS_REPLACE_RETRY_DELAYS`** —— M2 那次调整针对的是"写入期持续并发读"。

**TDD**：先写并发红测。写红测时注意 `SubagentRegistry` 默认 `max_active_agents=8`，
不显式传 `max_depth=0, max_active_agents=0` 关掉上限的话，测试会先撞
`SubagentTreeLimitError` 而不是测到竞态。至少三条：
1. 并发 `create_child()` 后所有行都在（丢行红测）。
2. 并发 `create_child()` 返回的 `run_id` 互不相同（碰撞红测 —— 这条比第 1 条重要，
   因为它检查的是**返回值**而不是幸存者）。
3. 并发 `update()` 不互相覆盖字段。

**回归注意**：现有 21 个 registry 测试全部单线程，加锁不应改变任何单线程语义。

#### M5 落地记录（2026-07-30，已完成）

| 状态 | 落地内容 | 红测证据 |
| --- | --- | --- |
| ✅ 完成 | `SubagentRegistry` 新增 `lock_path`（`registry.json.lock`）与 `_write_locked()` 上下文管理器，委托 M1 的共享 `subagent_state.cross_process_lock`（**不新增第二套锁实现**）；`create_child` / `update` / `mark_closed` / `mark_running` 四个读改写全程持锁；`_unique_child_name` 改名为 `_unique_child_name_unlocked` 并在文档串里声明"调用方必须已持锁" | `expected 48 persisted rows, got 12`；`12 != 48 : 36 run_id(s) handed to >1 agent`；`None != 1000 : agent0 lost its pid update` |

**锁边界的关键决定**：`create_child()` 里"名字分配 → `_check_tree_limits` → `next_run_no`
分配 → 写行"必须在**同一把锁内**。把 `_unique_child_name()` 留在锁外（它自己也 `_load()`）
就等于保留了原来的窗口 —— 两个调用方仍可能读到同一个 `next_run_no`。
把它改成 `_unique_child_name_unlocked()` 而不是让它自己取锁，是因为
`cross_process_lock` 在 Windows 上是 `msvcrt.locking` 自旋、不可重入，
嵌套取锁会**死锁**。方法名里带 `_unlocked` 是为了让这个约束在调用点可见。

**读路径没加锁，是有意的**：`get()` / `list_agents()` / `descendants()` 仍是无锁读。
M2 已让 `_save()` 走 `atomic_write_json`（tmp + `os.replace`），所以读者只会看到
某个完整版本，不会看到半个文件；给读加锁只会让 `wait_agents` 每秒 20 次的读把写者饿死。

**Windows `PermissionError` 确认消失**：加锁后并发写被序列化，探针里那条
`PermissionError [WinError 5] @ _replace_file` 不再出现，`_WINDOWS_REPLACE_RETRY_DELAYS`
未做任何改动（M2 那次调整针对的是"写入期持续并发读"，与本项无关）。

**验证**：3 条并发红测转绿（正式测试用 4 writer × 12 轮以控制 CI 时长）。
另用一次性探针在 4 writer × 60 轮下复验：

```
expected=240 persisted=240 returned=240 distinct_run_ids=240 errors=0
```

修复前同规模是 75% 丢行 + 大量 run_id 复用。

**回归**：`tests.test_subagent_registry` `Ran 24 tests ... OK`（21 → 24）；
全量 `Ran 807 tests in 126.270s ... OK (skipped=3)`（804 → 807）。
现有 21 个单线程 registry 测试全部未受影响，单线程语义未变。

### 1.6 M6 / M7（P1）语义唯一性守卫 —— 2026-07-30 B2 之后新增

**实测证据全文见 `docs/ga_subagent_control_plane_defects_2026-07-30.md` §3.5 / §3.6。**

做完 B2 之后被问到"重复执行的 bug 修好了吗"。诚实答案是"机制在，但要模型自己带 id"，
于是先去查两个参考实现有没有"分发层自动派 id"这一层 —— **都没有**（Codex 的
`Submission.id` 只为关联 Event；Claude Code 宿主层完全没有幂等，见缺陷文档 §3.4）。
两家真正的护栏是**语义唯一性**：Codex 在 `agent/registry.rs:247-250` 对已存在的
agent path 直接报错。GA 的 `agent_loop.py:68-92` 也没有任何重放路径，
在分发层自动派 id 挡不住唯一真实的重复来源（模型在**新一轮**里重发调用，
那时任何按 `(session_id, turn, index)` 推导的 id 都不一样）。

所以取舍是：**不造分发层 id，改为补语义唯一性守卫，并把 schema 措辞从"可选"改成带触发条件的义务。**

| ID | 状态 | 落地内容 | 红测证据 |
| --- | --- | --- | --- |
| M6 | ✅ 完成 | `subagent_registry.py` 新增 `SubagentNameConflictError`（带 `agent_path` 字段）；`_unique_child_name_unlocked` 在写锁内判活，活着抛错、已关闭/崩溃照旧改名；新增只读前置检查 `reject_if_live()`；`SubagentManager._next_available_task_name` 改为先拒绝再扫目录；拒绝走新的 `_record_spawn_rejection()` 记 `spawn_rejected` 事件 | `'/root/parent/worker_1' != '/root/parent/worker'`；`SubagentNameConflictError not raised` |
| M7 | ✅ 完成 | `resume_agent` 新增 `submission_id`（走 B2 同一套 `_replayed_submission` / `_record_submission` + `_resume_result_from_submission`）；新增 `_reject_resume_of_a_live_agent()` / `_is_live_state()`；`ga.py` 透传 `submission_id` 并复用 `_subagent_error_result()` | `TypeError: resume_agent() got an unexpected keyword argument 'submission_id'`；`SubagentNameConflictError not raised` |

四条实现决定：

1. **只有"活着"才冲突**。已关闭或崩溃的 agent 仍然拥有它的 task dir 和 artifact ——
   那是它做过什么的唯一证据，复用同名会把证据清掉，所以那种情况**改名才是对的**。
   判活规则与 `_reap_stale_agents` 一致，包括"**判断不了就算活着**"：
   这里猜死会静默复用一个正在运行的 agent 的名字，正是要修的缺陷。
2. **拒绝必须发生在推导 task dir 之前**，所以要有 `reject_if_live()` 这个只读前置检查。
   task dir 是 `temp/<task_name>`，如果让 `create_child` 把 agent path 改名而 manager
   仍用请求的目录名，新 agent 会拿到旧 agent 的目录 —— 第一版实现就是这么写错的，
   被 `test_spawning_over_a_closed_agent_still_renames_and_keeps_its_artifacts` 抓到。
   写锁内的 `_unique_child_name_unlocked` 仍是权威判定，前置检查只是让目录与路径保持同步。
3. **错误文案是写给模型的**，点名三个替代动作（`followup_task` / `close_agent` / 换名字），
   因为一句干巴巴的 "already exists" 只会让模型换个畸形名字重试。
   为此 `ga.py` 的 `_subagent_error_result()` 对这个错误类**跳过 `format_error`**：
   `@ file:line, func -> \`src\`` 的噪声会把一个刻意的策略答复伪装成 GA 崩溃。
   其它异常照旧带 traceback —— 那才是真崩溃时有用的部分。
4. **resume 复用同一个错误类**，因为从模型视角这就是同一件事（要的 agent 已经活着），
   三个替代动作也一样。`interrupt_agent` 不需要 `submission_id`：它只写 `_stop` 文件，天然幂等。

**schema 措辞同步**（Codex 就是靠措辞让模型交出 `item_id` 的）：五个 op 的
`submission_id` 说明从"可选幂等键"改成带触发条件的义务 ——
"只要你在重试、或不确定上一次调用是否生效，就必须带上"，
并说明代价（第二个进程 / 第二个活跃名额 / 第二份真实 LLM 花费）。
`resume_agent` 的工具描述也补上"仍在运行的子智能体不能 resume，请改用 followup_task"。

**验收**：`SpawnNameConflictTest`（6）+ `ResumeAgentGuardTest`（4）+
`tests/test_subagent_registry.py` 两条新用例 + `tests/test_ga_subagent_tools.py` 四条新用例。
回归 `Ran 859 tests in 116.513s ... OK (skipped=3)`（843 → 851 → 859）。

**顺带改对的两个既有测试**：`test_spawn_agent_duplicate_task_name_...` 与
`test_duplicate_task_name_creates_unique_child_path_...` 原来依赖"同名 spawn 会改名"，
现在必须先让第一个 agent 死掉（显式传 `process_exists=lambda _: False` 或先 `mark_closed`）。
判活相关的测试都**必须自己注入 `process_exists`** —— 默认实现会去问 `psutil.pid_exists`，
断言就变成了"宿主上恰好有没有这个假 pid"。

真实 API 验收见 §1.7：单测把判活 stub 掉了，而这两个守卫的判定输入正是真实进程存活，
所以必须再补一条真实链路。

### 1.7 M6 / M7 / B2 的真实 API E2E 验收（2026-07-30）

`tests/real_subagent_guard_e2e.py`（新增，`GA_RUN_REAL_API_E2E=1` 开关，
沿用 `SECRET_RE` / `sanitize()` 脱敏），`claude-opus-5`（`provider: gorouter`）。

**为什么必须补**：M6 / M7 的判定输入是 `psutil.pid_exists` 的真实进程存活，
而 §1.6 列的 14 条单测**全部注入了假的 `process_exists`** —— 恰好把要验的那一环 stub 掉了。
所以脚本先 spawn 一个真实子进程、跑完一轮真实 LLM、停在 `waiting_reply`，
让守卫对着真 pid、真 registry 行、真 OS 进程判活。
进程数也不从脚本自己的 popen 计数里读，而是 `psutil.process_iter` 扫 `agentmain.py`
命令行按 task_name 精确匹配 —— 否则"被拒绝的调用到底有没有偷偷起进程"这个断言
就是在验脚本自己的账本。

实跑结果 `passed: true`，`issues: []`。关键数字：

| 阶段 | 断言 | 实测 |
| --- | --- | --- |
| 前置 | 子进程真的活着 | `realPidAlive: true`，`pid: 20232`，`process_status: waiting_reply` |
| M6 manager | 同名 spawn 被拒 + 没起新进程 + 没留改名目录 | `refused: true`，`newProcessesAfterRefusal: []`，`noStrayTaskDir: true` |
| M6 工具层 | 模型收到的是可执行指引而非 traceback | `reason: name_conflict`，msg 含 `followup_task` / `close_agent`，无 `` -> ` `` 后缀 |
| M7 live | resume 被拒 + 没偷走 pid | `refused: true`，`pidUnchanged: true`，`newProcesses: []` |
| B2 | 同 `submission_id` 重放 followup 只排一次 | `triggerRowsAdded: 1`，`followupOutputFile: output1.txt` |
| M7 重放 | 同 `submission_id` 重放 resume 不起第二个进程 | `samePid: true`，`sameRunId: true`，`runIdMatchesOriginal: true`，`newProcessCount: 1` |
| M6 改名 | close 后同名改名 + 旧 artifact 逐字节完好 | `guard_e2e_…_1`，`separateTaskDir: true`，`originalArtifactIntact: true` |
| 事件 | 每次拒绝都记事件 | `spawnRejectedEvents: 4` |

三个 marker（`FIRST` / `FOLLOWUP` / `RESUME`）分别落在 `output.txt` / `output1.txt` /
`output2.txt`，改名后的 agent 落在自己的 `output.txt` —— 即"被拒绝"的那几步之外，
真实 LLM 轮次全部照常完成，守卫没有把正常路径一起挡掉。

**跑批中踩到的两件事，都不是 GA 缺陷**：

1. 中途把 `claude-opus-5` 切到 `provider: claude-100x` 时子 agent 首轮 `HTTP 403
   Cloudflare`。两侧抓 `requests.post` 对比确认：父进程同 profile 每次都成功，
   子进程的 `POST https://sub.100xlabs.space/v1/messages?beta=true`（`n_tools=51`,
   `payload_bytes=59174`）稳定 403（`cf-ray …-SEA`）；换回 gorouter 后**同一个请求**
   （`n_tools=51`, `payload_bytes=59177`）200（`cf-ray …-HKG`）。是该渠道边缘对
   大体积多工具请求的拒绝，与代理环境 / `CREATE_NO_WINDOW` / 系统提示形状均无关
   （逐项排除过）。
2. 脚本第一版把 `TASK` 写成固定字符串 `guard_e2e`，上一次被杀的跑批留下的目录让
   E2E **自己的**首次 spawn 就被改名，之后每条断言都在验一个没人持有的名字。
   已改成 `f"guard_e2e_{int(time.time())}"`，并让后续所有 op 都用
   `first.task_name` 而不是请求名；`live_child_pids` 也从子串匹配改成精确匹配
   （否则 `guard_e2e` 会匹配到 `guard_e2e_1`）。这类"测试脚本自身缺陷伪装成产品缺陷"
   的坑，判据是：先确认脚本断言的对象和产品实际创建的对象是同一个。

## 2. P0 mailbox 正确性修复（实测已确认的缺陷）

### 2.0 实测证据摘要

一次性探针脚本实测（Python 3.12.4 / Windows，`threading.Barrier` 对齐起跑放大竞态窗口，
跑完即删）。线程级竞态即证明跨进程竞态——跨进程没有 GIL 保护，只会更差。

| 探针 | 条件 | 结果 |
| --- | --- | --- |
| 并发 `enqueue` | 4 writer × 200 轮 = 800 条 | **丢 600 条（75%）** |
| `enqueue` vs `consume_trigger_turn` | 2 线程 × 200 轮 | **93/200 轮（46.5%）消息彻底丢失** |
| 并发自动 `message_id` | 4 writer × 200 轮 | **200/200 轮碰撞** |
| 套用 event bus 现成锁后重跑 | 4 writer × 200 轮 | **丢 0 条**（修复方案已验证） |
| `_write_rows` 截断窗口 | 500 行，写入期并发读 | **86/259 次（33%）读到残缺/空 mailbox** |

完整归因见调研文档 3.4 节。

### M1（P0）mailbox 加跨进程锁

**问题**：`subagent_mailbox.py:30,50,51` 的 `enqueue()` 是无锁 read-modify-write
（`rows = self._read_rows()` → `rows.append(row)` → `self._write_rows(rows)`）；
`consume_trigger_turn()`（`:55` 读 → `:75` 写回全量）同样无锁。
父进程 `send_message` / `followup_task` 与子进程轮询消费天然并发，实测 46.5% 概率静默丢消息。
丢失量 = (writers-1) × rounds 与实测完全吻合，说明是**结构性必然，不是偶发**。

**方案**：复用 `subagent_event_bus.py:117-142` 的 `_locked()`（Windows `msvcrt.locking`
自旋 + POSIX `fcntl.flock`）。抽成共享工具（如 `subagent_state.py` 里的
`cross_process_lock(lock_path)`），`SubagentMailbox` 持有自己的 `mailbox.lock`，
`enqueue()` / `consume_trigger_turn()` 全程持锁。探针 5 已验证丢失归零。

不要新造锁机制——仓库里已有一个能用的，重复实现会产生第三套分叉。

**TDD**：先写并发红测（N 线程 `Barrier` 对齐后各 `enqueue` 一条，断言落盘条数 == N；
以及 enqueue/consume 并发断言消息不丢），确认红，再实现。

**验收**：并发红测转绿；`tests.test_subagent_mailbox` 全绿。

### M2（P0）落盘改原子替换

**问题**：`subagent_mailbox.py:111` `open(self.path, "w")` 先截断再逐行写。
截断到写完之间任何读者看到残缺文件（实测 33%）；此刻崩溃会**留下被截断的 mailbox**，
直接违反"durable 为权威源"。

**方案**：照 `subagent_state.py:102-106` 的写法——写 tmp 文件后 `_replace_file()`
（内部 `os.replace` + Windows `PermissionError` 重试）。同一仓库已有正确实现。

**注意**：M1 的锁和 M2 的原子替换**都要**。锁防止逻辑覆盖，原子替换防止读者脏读和崩溃残缺；
两者解决的是不同问题，不能互相替代。

**TDD**：先写"写入期间并发读永不观测到少于已提交行数"的红测，确认红，再实现。

### M3（P0）消除 message_id 行数派生碰撞

**问题**：`subagent_mailbox.py:33`
`message_id = message_id or f"msg_{len(rows) + 1:06d}"` 用行数派生 ID，
并发写入者读到同样行数生成同一 ID，实测 200/200 轮碰撞。更糟的是 `:31-32` 的去重逻辑
会把碰撞消息当成同一条返回——**丢失被伪装成幂等命中**，比直接报错更难查。

**方案**：M1 的锁已能消除并发碰撞（持锁期间 `len(rows)` 稳定）。
但库层不该依赖调用方持锁，ID 应自身唯一：改为 `f"msg_{uuid4().hex[:12]}"`
或 `序号 + 随机后缀`。保留显式 `message_id` 的幂等去重语义不变（那是有意设计，
供工具层重试用）。

**TDD**：先写并发自动 ID 唯一性红测，确认红，再实现。

### M4（P0）统一两套分叉的 mailbox 消费实现

**问题**：仓库里有两套 mailbox 消费实现，语义不一致，且**被调用的那套用的是不安全写法**：

| | `SubagentMailbox.consume_trigger_turn()` | `consume_mailbox_trigger()` |
| --- | --- | --- |
| 位置 | `subagent_mailbox.py:54` | `subagent_state.py:77` |
| 落盘 | `open(...,"w")` 就地截断（**不安全**） | tmp + `os.replace`（安全） |
| 消费范围 | trigger 消息**及其之前所有 queue_only**（`:64`） | 只消费第一条 trigger 行（`:96-99`） |
| 返回 | dict（content/messages/consumed_at） | 仅 content 字符串 |
| 被调用 | 是（`agentmain.py:969`） | **否——死代码**（`agentmain.py:27` import 但全仓无调用） |

**方案**：保留 `SubagentMailbox` 的消费语义（范围正确，符合 QueueOnly/TriggerTurn 协议），
落盘换成 M2 的原子替换，然后**删除 `consume_mailbox_trigger` 与 `agentmain.py:27` 的 import**。
留着会让后来者改错文件。

**验收**：`grep -rn "consume_mailbox_trigger"` 只剩历史文档；子 agent 生命周期测试全绿。

## 3. P0 安全：realtime channel 认证

### S1（P0）per-run authkey

**问题**：`subagent_realtime_ipc.py:39` `__init__(..., authkey=None)` +
`:52` `Listener(self.address, authkey=self.authkey)` —— **默认不做任何认证**。
而地址完全可预测：`subagent_registry.py:76` `run_id = f"run_{run_no:06d}"` 是顺序编号，
`subagent_realtime_ipc.py:15` 拼成 `\\.\pipe\ga_subagent_run_000001`。
本机任何进程都能猜到管道名后 `Client()` 连上，**接收该子 agent 的完整事件流**——
events 里含 task 内容、工具调用参数、权限决策。Windows named pipe 默认 ACL
允许同 session 其它进程连接。

**这一条优先级高于所有延迟优化：延迟是体验问题，未认证事件流是信息泄露。**
而且必须与 R1 同批落地——R1 会让这条通道从"没人用的摆设"变成"承载真实消息的通路"，
在没有认证的情况下接通它等于把泄露面从理论变成现实。

**方案**：
- spawn 时生成 per-run 随机 authkey（`secrets.token_bytes(32)`），写入子 agent
  task dir 内的 state（仅父与该子进程可达），不进 registry 全局文件、不进日志。
- `SubagentRealtimeChannel(address, authkey=...)` 与 `connect_realtime_channel(address, authkey=...)`
  双向使用（两个函数**已有 authkey 参数**，只是没人传）。
- `ipc_endpoint` 暴露地址但**不得暴露 authkey**（`ipc_endpoint` 会写进 `state.json` 并被
  `read_agent` 返回给 LLM）。
- 日志与事件 payload 不得出现 authkey；沿用 E2E 脚本的 secret redaction 思路。

**TDD**：先写"未带 authkey 的 Client 连接被拒"红测，确认红，再实现。

### S2（P1）传输层加固

- POSIX：`default_channel_address()`（`subagent_realtime_ipc.py:16-18`）建的
  channel 目录权限收到 `0o700`。
- Windows：参考 Codex `tui/src/ide_context/windows_pipe.rs:56-82` 的
  `validate_pipe_server_owner`，客户端连上后校验管道服务端所有者，防连到冒名管道。

## 4. P1 实时性：把死通道接通

### R1（P1）子 agent 订阅 realtime channel

**问题（最高性价比修复点）**：`subagent_realtime_ipc.py:21`
`connect_realtime_channel()` 在**整个非测试代码里零调用者**——
`grep -rn "connect_realtime_channel" --include=*.py . | grep -v tests` 只命中定义处本身。
父侧 `SubagentManager._open_realtime_ipc()`（`subagent_manager.py:209-218`）
确实 `start()` 了 Listener 并把地址写进 `ipc_endpoint`（`:421`），
但**子进程 `agentmain.py` 从未连接**。

所以 `GA_SUBAGENT_REALTIME_IPC=1` 目前只做到"父侧监听 + 事件 fan-out 给不存在的订阅者"
（`subagent_manager.py:220-224` `_publish_realtime_event` 找到 channel 后
`channel.publish(event)`，而 `publish()` 的 `subscribers` 永远是空列表）。
**父→子延迟因此完全没有改善，子侧仍是纯 2 秒轮询。这是"realtime IPC 感觉没用"的直接原因。**

**方案**：
- 子进程启动时从 `state.json` 读 `ipc_endpoint.address` + authkey（S1），
  调 `connect_realtime_channel()` 订阅；连接失败不报错，退回轮询（R4）。
- 传递路径：`ipc_endpoint` 已经在 `state.json` 里（`subagent_manager.py:421`），
  子进程 `run_task_worker_loop` 已经在读 `state.json`（`agentmain.py:848-849`），
  **不需要新增命令行参数或环境变量**。
- 连接生命周期跟随 worker loop，退出时关闭。

**TDD**：先写"子 agent 连上父 channel 后能收到父 publish 的事件"红测（用真实
`SubagentRealtimeChannel` + 真实 `connect_realtime_channel`，不 mock 传输），确认红，再实现。

### R2（P1）mailbox trigger 走 realtime 通知

**目标**：把父→子延迟从"平均 1s / 最差 2s"压到毫秒级，**同时不改变权威源语义**。

**当前路径**：`agentmain.py:960-961` `for _ in range(reply_wait_iterations): sleep_fn(reply_sleep_s)`
先睡 2 秒，再 `consume_trigger_turn()`（`:969`）。父进程
`SubagentManager.send_message`（`subagent_manager.py:1094`）落盘后要等子 agent 下一次醒。

**目标路径**（这就是 Codex 的模型，见第 0 节第 3 条）：
1. 父 `enqueue` 到 `mailbox.jsonl`（持锁 + 原子替换，M1/M2）——**先落盘**。
2. 落盘成功后 publish 一条 `message_queued` / `message_trigger` 事件到 realtime channel。
   与 `SubagentEventBus._publish()`（`subagent_event_bus.py:58,61-68`）一致：
   durable append 先完成，publish 是 best-effort，失败被吞掉不影响 durable。
3. 子 agent 阻塞在 `conn.poll(timeout=poll_interval_s)` 而非 `sleep()`；
   收到通知后**回去读 durable mailbox**（`consume_trigger_turn()`）拿内容。
4. 通知丢失不影响正确性——`poll` 超时后照样走一次 `consume_trigger_turn()`，
   轮询降级为兜底而非主路径。

**关键设计约束（不可妥协）**：realtime 事件**只作为 trigger 信号，不携带消息内容**。
内容始终从 durable mailbox 读。这样：
- 崩溃时未消费消息仍可重放（GA 相对两个参考实现的唯一优势，不能丢）。
- 通知丢失退化为"延迟到下次轮询"，不是"消息丢失"。
- 与 Codex `watch::Sender<()>` 空负载语义一致。

**反模式**：不要把消息内容塞进 realtime 事件然后跳过读盘——那会让 realtime 通道变成
第二个权威源，退化成 Claude Code 的内存队列语义，同时失去 durable 优势。

**TDD**：先写红测断言三件事同时成立——(a) 消息仍持久化到 `mailbox.jsonl`；
(b) realtime channel 上收到 trigger 事件；(c) 子 agent 消费到的内容来自 durable mailbox。
确认红，再实现。

### R3（P1）轮询粒度与空转寿命解耦

**问题**：`agentmain.py:827` `reply_wait_iterations=300, reply_sleep_s=2` 这两个参数
同时决定了两件无关的事：
- 轮询粒度 = `reply_sleep_s` = 2s（决定延迟）
- 子 agent 空转寿命 = `300 × 2s = 600s`（决定何时判定 `agent_exited`，
  见 `agentmain.py:986-989` 的 `for...else` 分支）

后果：想降低延迟就得减小 `reply_sleep_s`，但那会同时缩短子 agent 寿命；
反之想延长寿命就得加大轮询间隔。R1/R2 接通后这个耦合更别扭——
realtime 已经保证低延迟，轮询间隔本该放大以省 CPU，但放大就缩短寿命。

**方案**：拆成 `poll_interval_s`（单次等待粒度）与 `idle_timeout_s`（总空转寿命），
循环条件改为按 deadline 判定而非固定迭代次数。保持默认行为等价（600s 寿命），
避免破坏现有测试与真实 E2E 的时序预期。

**注意**：`run_task_worker_loop` 的签名被测试直接调用
（`tests/test_agentmain_subagent_lifecycle.py` 传 `sleep_fn` 等），
改签名要保留旧参数兼容或同步更新测试，不能静默破坏。

### R4（P1）优雅降级与可观测性

- realtime 连接失败 / 中途断开 → 自动退回轮询，不得让子 agent 卡死或退出。
- 降级要在 `state.json` / events 里可见：沿用现有
  `effective_ipc_mode` + `ipc_fallback_reason` 字段（`subagent_ipc.py:26-51` 已有这套语义），
  子侧订阅失败时也要写一条，让 `read_agent` 能看出"父侧监听成功但子侧没连上"。
  当前只有父侧的 fallback 会被记录，子侧静默失败——这也是本轮才发现死通道的原因之一。
- `subagent_ipc.py` 的 `_REALTIME_IPC_MODES`（`:4`）保持 opt-in 默认关闭，
  直到 R1-R4 + S1 全绿再考虑是否默认开启。

## 5. P1 安全守护：agent 树上限

### G1（P1）深度 / 总数上限

**问题**：`subagent_registry.py` / `subagent_manager.py` / `subagent_agent_path.py`
里 grep 不到任何 depth / max_agents / MAX_ 限制。子 agent 可以递归 spawn 子 agent，
每个都是**独立 OS 进程**——失控时后果比同进程实现严重得多（进程耗尽、内存耗尽、
真实 LLM API 费用失控）。

**参考**：Codex `core/src/agent/registry.rs:23-26`
`AgentRegistry { active_agents: Mutex<ActiveAgents>, total_count: AtomicUsize }`
守护 agent 树深度与活跃总数，`control.rs:221` `reserve_spawn_slot` 在 spawn 前预留槽位。

**方案**：`SubagentRegistry.create_child()` 里按 `AgentPath` 深度 + 活跃 entry 计数拒绝超限，
返回明确错误而非静默失败。默认值保守（如 depth ≤ 3、活跃 ≤ 8），可配置。
拒绝时写 event，便于排查。

**TDD**：先写"超过深度上限时 spawn 被拒且有明确原因"红测。

## 6. P2 控制面清晰度（纯收益，不动进程模型）

这一组是 Codex 路线里**值得抄且不需要切换运行时**的部分（调研文档 4.2.1 阶段 B）。

### B1 realtime channel 背压

**问题**：`subagent_realtime_ipc.py:59-82` `publish()` 是同步全量 fan-out，
对每个 subscriber 顺序 `conn.send(event)`。慢订阅者（或管道缓冲满）会**阻塞 publish 调用方**，
而 publish 是从 `SubagentEventBus.append_event()`（`subagent_event_bus.py:58`）
在持锁路径之后调用的——一个卡住的订阅者能拖慢整个事件写入链路。

**参考**：Codex `app-server-client/src/lib.rs:130-136` 的
`AppServerEvent { Lagged, ServerNotification, ServerRequest, Disconnected }`——
显式区分 lossless 与 best-effort，慢订阅者收到 `Lagged` 而不是拖垮发送方；
`app-server/src/transport.rs:129-166` 对可丢连接用 `try_send`，满了直接断开。

**方案**：per-subscriber bounded 队列 + 后台发送线程；队列满则丢弃并投递一个
"你落后了，请回读 durable"的 lagged 标记。这与 R2 的"trigger 不携带内容"设计天然契合——
丢事件不丢消息。

> **2026-07-30 实测校准**（详见 `docs/ga_subagent_control_plane_defects_2026-07-30.md` §2）：
> **阻塞前提为真**：订阅者收完 ack 后不再 recv，用真实 `message_queued` 事件形状
> （pickle 后 **329 字节**）持续 publish，**第 24 条即卡死**。24 条是管道缓冲的真实容量，
> 这才是评估风险的依据（第一版探针用 4KB 填充事件，第 2 条就卡，但那不是 GA 的真实事件大小）。
>
> **但本节"一个卡住的订阅者能拖慢整个事件写入链路"的推断未能复现。** 让子 agent 处于
> "已订阅但忙在 LLM turn 里不读通道"的状态，父侧连发 4 次 `followup_task` 全部
> 0.01–0.03s 完成。原因在 `subagent_manager.py` `_publish_realtime_event()`：
> 它按 `event["agent_path"]` 只 fan-out 给**那一个** agent 的通道，且每个通道只有 1 个订阅者，
> 所以不存在"一个慢订阅者影响其他 agent"的路径。
>
> **修正后的结论**：B1 该修（阻塞是真的，且 `publish` 在 `append_event` 释放锁之后、
> 在调用方栈上同步执行），但它是**单 agent 局部风险**而非全局链路风险，
> 优先级维持 P2，排在 M5 之后。

#### B1 落地记录（2026-07-30，已完成）

`subagent_realtime_ipc.py` 新增 `_SubscriberSink`：每个订阅者一条 bounded
`collections.deque`（`queue_size`，默认 `DEFAULT_QUEUE_SIZE=64`）+ 一条自己的
`ga-subagent-realtime-send` 发送线程 + 一个 `threading.Condition`。
`publish()` 只做 `offer()`（入队，不阻塞、不抛错），返回值语义由"已写入 socket 条数"
改为"已入队条数"；真正的 `conn.send()` 挪到订阅者自己的线程上。队列满时丢最老的一条并
置 `_lagged_pending`，下一次出队时**插队**投出 `{"type": CHANNEL_LAGGED, "dropped": n}`。

三条实现决定：

1. **lagged 标记插队而不是排队**。标记的语义是"你落后了，回读 durable 源"，
   排在一队即将被丢弃的旧事件后面等于延迟通知；插队让订阅者尽早知道要回读。
   标记只带 `type` + `dropped` 两个键，与 R2 的"realtime 不携带正文"一致
   —— 已用 `test_the_lagged_marker_carries_no_message_body` 锁死键集合。
2. **连接归发送线程所有**。send 失败是订阅者消失的主要方式，线程在自己的 `finally`
   里 `conn.close()` 并置 `alive=False`；`subscriber_count` 顺带剪掉死 sink，
   所以调用方永远不从别的线程碰这个 handle。副作用：`publish()` 不再同步发现断开的订阅者，
   原有的 `test_publish_drops_closed_subscriber_without_raising` 改为轮询等待收敛。
3. **`close()` 必须能叫回卡在 `send()` 里的线程**。仅 `conn.close()` 在 Windows 上够用
   （挂起的 overlapped write 被取消），但 POSIX 的 socket send 不会因为 fd 被关就返回，
   所以 `_force_shutdown()` 先 `os.dup(fileno())` 出一个 fd 包成 socket 做
   `shutdown(SHUT_RDWR)`（shutdown 作用于 socket 本身而非某个 fd），再关闭连接。

`_accept_loop()` 里的 `SUBSCRIBED_ACK` 也改为 `offer()` 后再 `start()` 线程 ——
连上就不读的客户端此前能卡住 accept 循环本身。

验收（`tests/test_subagent_realtime_ipc.py::ChannelSlowSubscriberTest`，5 条）：
`queue_size=4` + 不读的订阅者下 200 次 publish 全部快速返回（修复前第 24 条即卡死）；
lagged 标记必现且 `dropped > 0`；停滞订阅者不拖慢同一通道上的健康订阅者，
健康侧事件仍严格有序；`close()` 后无 `ga-subagent-realtime*` 线程残留。

### B2 显式 Op 结构

把 `spawn/send/close/interrupt/resume` 收敛成显式提交对象（参考 Codex
`protocol.rs:123-133` `Submission { id, op, trace }`），带 `id` 便于幂等与追踪。
GA 的 mailbox 消息字段已与 `Op::InterAgentCommunication`
（`protocol.rs:663-670`：`author`/`recipient`/`other_recipients`/`content`/`trigger_turn`）同构，
`other_recipients`（多播）是 GA 目前缺的，可顺带补上。

收益：控制面可审计、可重放、可测试；代价：触及 `subagent_manager.py`（1544 行）
与 13 个工具 handler，建议在 P0/P1 全绿后单独一个切片做。

#### B2 落地记录（2026-07-30，已完成）

先实测再动手，因为原文把 B2 记为"纯结构收益"。实测结果推翻了这个判断
（详见 `docs/ga_subagent_control_plane_defects_2026-07-30.md` §2.4）：

```
followup_task 重放同一逻辑提交 → mailbox rows = 2, trigger_turn rows = 2   （子 agent 把任务干了两遍）
spawn_agent   重放同一逻辑提交 → /root/dup + /root/dup_1, run_000001 + run_000002 （两个真实进程）
enqueue 已有 message_id 去重分支，但上层从未有人传过 id
enqueue 没有 other_recipients 参数
```

也就是说 B2 不是重构，而是修一个可测的正确性缺陷：**控制面 op 没有身份，重放就重复执行**。

三块落地：

1. **`subagent_submissions.py`（新）**：`SubagentSubmissionLog`，`submissions.jsonl` +
   `cross_process_lock`，`record()` 首写为准、`find()` 查重、`normalize_id()` 把空 id 当作
   "别给我去重"（否则不相干的调用会撞在 `""` 上）。`MAX_ROWS=2000` 截断 —— 它是重放判据，
   不是审计流水。`_serializable()` 在结果无法 JSON 化时降级成 `{"repr": ...}`：
   **去重比载荷重要**，少一行记录就等于重放会再执行一次。
2. **消息面走 mailbox 自己的去重**：`_submission_message_id(submission_id, recipient)` 把提交 id
   推导成 `sub_<id>_<recipient>` 的 mailbox 行 id，交给 `SubagentMailbox.enqueue` 已有的
   `message_id` 分支。这样去重发生在唯一的真相源上，而不是再加一层可能与 mailbox 不一致的账本。
3. **`spawn_agent` / `close_agent` 走提交日志**：入口 `_replayed_submission()` 命中就重建首次结果
   （`_handle_from_submission` / `_close_result_from_submission` 用 `dataclasses.fields` 过滤字段，
   老记录多/少字段都不炸），出口 `_record_submission()` 落 `asdict(...)` 快照。
   op 名参与判定 —— 同一个 id 复用到两个不同 op 上，不能让 close 看起来像"已完成的 spawn"。
   读写提交日志的异常都被吞成"照常执行"：坏账本只该损失幂等，不该让 op 本身失败。

多播（`other_recipients`）作为 Codex `Op::InterAgentCommunication` 的对齐项一并补上：
`enqueue` 落 `other_recipients` 字段，新增 `SubagentMailbox.annotate()` 事后补同收者列表
—— 多播只有在所有收件人都解析完之后才知道完整 peer 列表，而这个补写**绝不能**有本事让
已经成功的投递失败。`_fanout_message()` 里单个兄弟 mailbox 坏掉只记 `status: error`，
不影响其它收件人和主投递。

验收：`tests/test_subagent_submissions.py::SubmissionLogTest`（6 条）+
`tests/test_subagent_manager.py::SubmissionIdempotencyTest`（5 条）+
`MulticastMessageTest`（5 条）。回归 `Ran 843 tests ... OK (skipped=3)`。
工具层同步：`ga.py` 的 `do_spawn_agent` / `do_send_message` / `do_followup_task` /
`do_close_agent` 透传 `submission_id`（`other_recipients` 兼容 list 与逗号串两种形态，
模型对 array 参数两种都会发），两份 `assets/tools_schema*.json` 补齐字段说明。

### B3 状态订阅改 watch 语义

`wait_agents()`（`subagent_manager.py:775-830`）目前轮询
`_event_size()` / `state.json` / 父 inbox。R1 接通后可改为"阻塞等 realtime 事件 +
轮询兜底"，与 Codex `control.rs:880-887` `subscribe_status` 返回
`watch::Receiver<AgentStatus>` 的语义对齐。

注意 watch 语义是"最新值"而非"全部事件"，GA 的 `event_seq` cursor 语义
（`subagent_event_bus.py:40,55`）不能因此丢——两者要共存：
watch 负责唤醒，cursor 负责保证不漏事件。

> **2026-07-30 实测补充**（详见 `docs/ga_subagent_control_plane_defects_2026-07-30.md` §3）：
> `wait_agents()` 每个 poll 周期对每个 target 调 `read_agent()`，而 `read_agent()` 无条件
> `atomic_write_json(state.json)` + `_write_registry_entry()`。4 agent / 2s 超时 / 0.5s 间隔实测：
> **40 次原子写 = registry.json 20 次 + state.json 20 次，即每秒 20 次 tmp+os.replace。**
>
> 这改变了 B3 的性质：那 20 次 `registry.json` 写正是 M5 无锁竞态的主要喂食者，
> 也就是说 `wait_agents` 自己在高频喂竞态。**B3 必须排在 M5 之后**——
> 先修锁，B3 才是纯优化；反过来先做 B3 只是在竞态之上叠优化。

#### B3 落地记录（2026-07-30，已完成）

分两半做，因为两半解决的是两件事：**写放大**（观察不该写）与 **watch 语义**（等待不该靠计时器）。

**写放大半**：`SubagentManager.probe_agent()` —— 与 `read_agent()` 走同一条
`_refresh_state()` 派生逻辑，但 `persist_side_effects=False`：不 `mkdir`、不写 `state.json`、
不写 registry、不 shell 出去 `git` 算 worktree summary。`wait_agents` 的检测循环改调它。
一旦真的有东西要报，`_states_for_events()` 仍走 `read_agent()` 落盘 ——
**值得返回的状态就值得持久化**，所以返回值和以前完全一样。

**watch 半**：`SubagentRealtimeChannel.wait_for_signal(timeout)` +
`SubagentRealtimeSubscriber.signal()`，双向复用同一条 Connection。
`wait_agents` 的 sleep 点换成 `_wait_for_change()`：有活通道就在通道上阻塞，
没有（`ipc_mode=file` 或 realtime 被拒）就保留原来的盲 sleep。
子侧在 `agentmain._subagent_event()` 末尾统一 `_signal_parent()` ——
那是本进程唯一的"我写了 durable 东西"choke point，11 个调用点全覆盖。

四条实现决定：

1. **双向复用一条 Connection，先探针验证再动手**。300 轮交错 send/recv 探针
   （父侧 `wait()` 阻塞读 + sink 线程并发写同一 handle）零错误、300/300 信号到达，
   两个 family 都通过。否则要么另开一条反向通道，要么退回文件 mtime watch。
2. **信号不带状态**。`{"type": "child_signal"}`，与下行事件同理：父侧被叫醒后回读
   `state.json` / `events.jsonl`，所以丢信号只损失一个 poll 间隔，不损失正确性。
   `event_seq` cursor 语义完全不动 —— watch 负责唤醒，cursor 负责不漏。
3. **多通道时按 budget 切片轮询**。一条线程不能同时阻塞在多个通道上，
   所以 `poll_interval_s` 被 N 个通道均分，任一信号立即结束等待；
   通道抛错则退回 sleep 该切片，绝不让死通道把整个 wait 弄挂。
4. **子进程的 subscriber 存在模块级变量里**。`_subagent_event` 有 11 个调用点，
   给每个都加一个参数只是在重复说同一件事；一个进程就是一个 agent，
   所以进程级句柄是这里的正确粒度。副作用：mid-run 掉线的检测点从
   `subscriber.wait()` 分支内挪到循环体，因为现在出站 `signal()` 一样会先发现掉线。

验收：`WaitAgentsWriteAmplificationTest`（4 条）+ `WaitAgentsWatchTest`（5 条）+
`ChannelUpstreamSignalTest`（5 条）+ `ChildEventSignalsTheParentTest`（1 条）。
同规模复验（4 agent / 2s / 0.5s）：

```
total atomic_write_json during wait = 0   (修复前 40：registry.json 20 + state.json 20)
```

## 7. 实施顺序与验收

### 建议切片顺序

每个切片走 TDD（先红测→确认红→最小实现→绿→回归），落地后更新
`docs/ga_subagent_v2_optimization_design_2026-07-27.md` 进度段。

1. **切片 1（P0 正确性）**：M1 → M2 → M3 → M4。
   先修正确性，因为 R2 会显著提高父子并发访问 mailbox 的频率——
   在无锁 mailbox 上接通实时通知，等于把 46.5% 的丢消息概率暴露得更频繁。
2. **切片 2（P0 安全）**：S1。必须在 R1 之前或同批——不能先把未认证通道接通。
3. **切片 3（P1 实时性）**：R1 → R2 → R4 → R3。
   R1 单独可验证（能收到事件），R2 才是业务价值，R4 保证不引入新的卡死路径，
   R3 最后做因为它要改被测试直接调用的签名。
4. **切片 4（P1 守护）**：G1 + S2。独立于上面，可并行。
5. **切片 5（P2）**：B1 → B3 → B2。B2 最大，放最后。

> **2026-07-30 复核后的顺序修订**：切片 1-4 已全部完成，规划外补了
> `close_agent(cascade=true)`（§1.4）。剩余工作的实际顺序改为：
>
> 6. **切片 6（P0 正确性 + 安全）**：**M5**（§1.5）。插在切片 5 之前 ——
>    它是唯一还没修的 P0，且 B3 的写放大正在高频喂它的竞态。
> 7. **切片 7（P2）**：B1 → B3 → B2，顺序不变，但都排在 M5 之后。
>    **B1、B3、B2 已于 2026-07-30 全部完成**（落地记录见 §6 对应小节），切片 7 收尾。
>    原表把 B2 列为 B3 的依赖，实测不成立：B3 只需要 R1 的通道，不需要 `Op` 结构，
>    所以按 B1 → B3 → B2 的顺序做时 B3 并未被 B2 阻塞。
>    B2 原本被记为"纯结构收益"，实测推翻：无提交身份时重放 `followup_task` 会让子 agent
>    把任务干两遍、重放 `spawn_agent` 会起第二个进程，所以它是正确性修复而非重构。
> 8. **切片 8（P1 语义唯一性）**：**M6 → M7**（§1.6）。B2 之后新增，因为 B2 的幂等是
>    opt-in（要模型自己带 id），而两个参考实现的真正护栏都是语义唯一性拒绝，不是 id。
>    **M6、M7 已于 2026-07-30 完成。**
>
> 依据全部来自实测，见 `docs/ga_subagent_control_plane_defects_2026-07-30.md`。

### 每切片的回归要求

- focused fresh test：本切片的红测转绿。
- subagent 回归：`python -m unittest tests.test_subagent_state tests.test_subagent_agent_path
  tests.test_subagent_mailbox tests.test_subagent_artifacts tests.test_subagent_transcript
  tests.test_subagent_notifications tests.test_subagent_roles tests.test_subagent_worktree
  tests.test_subagent_ipc tests.test_subagent_realtime_ipc tests.test_subagent_registry
  tests.test_ga_subagent_permissions tests.test_subagent_manager tests.test_subagent_event_bus
  tests.test_ga_subagent_tools tests.test_agentmain_subagent_lifecycle tests.test_ink_bridge`
  （当前基线 `Ran 217 tests ... OK`）。
- workflow 回归（确认未动 workflow 语义），当前基线 `Ran 165 tests ... OK`；
  已知 transient `test_runtime_observes_external_kill_state` 属 workflow 侧
  deadline-vs-kill 竞态，**不在本 feat 范围，不修**。
- 全量：`python -m unittest discover -s tests`（当前基线 `Ran 743 tests ... OK (skipped=1)`）。
- 前后端模拟用户调用：涉及工具层改动时补 `tests.test_ink_bridge` 的 `submit()` 驱动用例。

### 并发类测试的写法要求

M1/M2/M3 的红测是并发测试，容易写成偶尔通过的假绿。要求：

- 用 `threading.Barrier` 对齐起跑点放大竞态窗口，不要靠 `sleep` 碰运气。
- 断言**确定性事实**（落盘条数 == 写入条数、ID 集合大小 == writer 数、
  并发读永不观测到少于已提交行数），不要断言时序。
- 重复足够轮次（实测 200 轮足以让无锁实现 100% 暴露）；
  但正式测试里控制轮次以免拖慢 CI——无锁实现在 4 writer 下每轮必丢，
  少量轮次即可稳定复现。
- 跨进程语义优先用真实子进程验证一次（如 R1 的端到端），线程级测试作为快速回归。

### 真实 LLM E2E

若切片影响父子消息真实链路（R2 尤其），补一轮真实 API E2E：
沿用 `tests/real_subagent_terra_e2e.py` 的模式，`llm.yaml` 的 `gpt-5.6-terra`
（`provider: wapq`），并**保留 `SECRET_RE` / `sanitize()` 脱敏机制**。
S1 落地后要额外确认 authkey 不出现在任何 E2E 输出、日志、`state.json` 可见字段里。

## 8. 完成后的预期状态

- 父→子消息延迟：从平均 1s / 最差 2s 降到毫秒级（realtime 通知），轮询降级为兜底。
- 消息不丢：并发 enqueue/consume 不再互相覆盖；崩溃后未消费消息可重放
  （相对 Claude Code / Codex 的唯一架构优势得以真正成立，而非名义上成立）。
- 事件流认证：本机其它进程无法窃听子 agent 事件流。
- agent 树有界：递归 spawn 不再能失控。
- 架构定位清晰：GA = 跨进程版的 Codex 通知模型（durable 内容 + 空信号唤醒），
  而不是"没做完的 Claude Code"。

## 9. 何时才应重新考虑切换路线

出现以下信号才值得付同进程化的成本（调研文档 4.4）：

- 需要子 agent 与父共享内存态（共享文件读取缓存、共享 MCP 连接池），
  序列化成本成为瓶颈 → 考虑 Claude Code 同进程路线。
- 需要同时管理数十个 agent、需要 fork/replay 成为一等公民 → 考虑 Codex SQ/EQ 路线。
- **需要 live tool permission request**（子 agent 实时请求父批准工具调用）→
  这是文件协议的真实边界。它是请求-应答语义，用 mailbox 轮询实现会很别扭；
  realtime channel 双向化能缓解，但如果要做成完整交互 runtime（接管子 stdin +
  TUI 控制权转交），那时切路线的理由才充分。

在此之前，本文档的 P0/P1/P2 是更高性价比的投入。

## 10. 相关文档

- `docs/ga_subagent_claudecode_codex_ipc_reference_2026-07-29.md`——源码级调研与缺陷实测证据
- `docs/ga_subagent_control_plane_defects_2026-07-30.md`——M5 / B1 / B3 的 2026-07-30 实测证据与优先级判断
- `docs/ga_subagent_v2_optimization_design_2026-07-27.md`——本 feat 主设计与进度
- `docs/ga_subagent_mechanism_research_2026-07-27.md`
- `docs/ga_subagent_codex_reference_2026-07-13.md`



