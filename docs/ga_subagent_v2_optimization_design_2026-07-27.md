# GA subagent v2 优化详尽设计文档

日期：2026-07-27

## 实现进度（2026-07-29，foreground attach / realtime IPC / replay timeline 切片后）

### 已落地并通过 focused fresh test

- P0 身份与控制面：已实现 `AgentPath`、`SubagentRegistry`、`run_id`、同名 task 自动后缀、`list_agents(path_prefix)` 与 registry 持久化。
- P0 生命周期控制：已实现 `close_agent` 工具、root close 拒绝、previous/closed state 返回、registry closed 标记与 `_stop` 请求文件。
- P1 事件与 mailbox：已实现 `SubagentEventBus`、`event_seq` cursor、notification queue、`wait_agent(since_event_seq)`、`SubagentMailbox` 的 QueueOnly/TriggerTurn 单一协议；`reply.txt` 仅保留 legacy fallback。
- P1 fork policy：已记录 `fork_history_count`、`fork_history_token_estimate`、`fork_redacted`、`fork_policy_warning`。
- P1 权限继承：`inherit-current-permissions` 已显式记录 `parent_permission_mode`，并在子 agent worker 中按父会话 `read_only` / `ask` / `full_access` 解析，不再退回子 agent 默认全开权限。
- P1 event bus 并发安全：`append_event()` 已用跨进程锁包住 `event_seq` 分配、JSONL append 与 cursor 写入，覆盖并发 monotonic seq 回归。
- P2 artifact / notification：已实现 final artifact manifest、`final_output_ref`、`read_agent_result(artifact_id=...)`、common runtime dataclasses、父上下文 `GA_SUBAGENT_NOTIFICATIONS` 注入，且避免子 agent 消费父通知。
- P2 sidechain transcript：已实现 sidechain transcript metadata/events 镜像与基础敏感字段脱敏；可记录 `request`、`assistant`、`tool_call`、`tool_result`、`permission_decision`、`final_output` 与状态事件，具备审计执行过程的基础；已新增只读 `replay()` 摘要，可从 JSONL 重建请求、工具、权限、assistant、final output、关闭状态等恢复/审计所需摘要；已新增 `build_replay_timeline()`，可把 transcript 投影为带 `event_key` / `resume_role` / `resume_content` / `editable` 的可编辑 replay timeline；`build_resume_context(edits=...)` 与 `read_agent_result(include_transcript_timeline=true, resume_context_edits=...)` 可基于 timeline event_key 覆写或 drop 单条投影消息；已新增 `resume_agent` 可执行恢复切片，可从 sidechain transcript 写回 `_history.json`，复用同一 `agent_path/run_id` 重启同一子 agent 的下一轮，并从 `output1.txt` 等后续轮次输出，避免覆盖旧 `output.txt` / artifact；恢复前会清理旧 `_stop` / `reply.txt` 控制文件并清空旧 `final_output_ref`，避免 close 后恢复立即停机或被上一轮 artifact 误判。
- P3 高阶机制骨架：已实现 `.ga/subagents/*.json|*.md` role registry、`spawn_agent(agent_type=...)` role defaults、`background` 元数据、`foreground_agent` / `background_agent` handoff 状态切片、`attach_agent` / `detach_agent` 增量读取当前轮 `output*.txt` 的前台输出流切片、`isolation="worktree"` 创建 git worktree 的启动骨架、`ipc_mode` 元数据，以及 socket/pipe/event_server 请求到 durable file bus 的 fallback。
- P3 foreground/background attach：已实现最小可执行 attach/detach runtime 切片；`attach_agent` 会把目标切到 foreground handoff 状态并按 `since_offset` / `max_chars` 返回当前轮输出增量、`next_stream_offset`、`stream_eof`，`detach_agent` 读取剩余输出并切回 background，保持同一 `run_id` / task dir / transcript。Ink bridge 已有模拟用户调用覆盖真实 `GenericAgentHandler.do_attach_agent` / `do_detach_agent`。
- P3 realtime IPC：已新增 `SubagentRealtimeChannel` 常驻通道（Windows named pipe / POSIX socket 地址）、`connect_realtime_channel()` 客户端、`SubagentEventBus(publisher=...)` best-effort fan-out，以及 `SubagentManager(realtime_channel_factory=...)` 的实时事件推送；默认仍 durable file bus，设置 `GA_SUBAGENT_REALTIME_IPC=1` 或注入 factory 后，socket/pipe/event_server 请求可变为 effective realtime transport，并在 `ipc_endpoint` 中暴露监听端点。
- P3 worktree isolation 修正：`isolation="worktree"` 现在在 worktree 中执行 `agentmain.py`，并保持 `--task_root` 指向父协调根；worktree 创建失败会写 errored state/event 并把 registry entry 标为 closed/worktree_error，避免 running ghost entry。
- P3 worktree diff / cleanup：已实现 `summarize_subagent_worktree()` 与 `remove_subagent_worktree()`；`read_agent()` / `close_agent()` 会为 worktree 隔离子 agent 采集 `git status --short` 与 `git diff --stat` 摘要，`close_agent(cleanup_worktree=true)` 会在保留 `worktree_summary` 后执行 `git worktree remove --force` 并删除残留目录；工具层和中英文 schema 已暴露 `cleanup_worktree`。

### 仍属于协议预留 / 后续增强

- **IPC / mailbox 待实现优化已单独立规划文档：`docs/ga_subagent_ipc_implementation_plan_2026-07-29.md`。**
  内含路线判定（不切路线，走跨进程版 Codex 通知模型）、14 项任务的优先级总表
  （P0 正确性 M1-M4 / P0 安全 S1 / P1 实时性 R1-R4 / P1 守护 G1 / P2 控制面 B1-B3）、
  每项的问题定位（带 `file:line`）、方案、TDD 要求、切片顺序与回归基线。
  **动手实现 IPC / mailbox 相关切片前以该文档为准。**
- sidechain transcript 的完整 resume/replay 状态机仍是后续增强：当前 `resume_agent` 已能基于 transcript 投影恢复 backend history 并重启下一轮，replay timeline 已支持按 event_key 覆写/drop 投影消息；但仍未恢复 provider 内部未完成 tool-use 状态，也不支持在同一未完成 turn 中间无损续跑，timeline 编辑目前只影响 resume context 投影，不会回写 sidechain transcript 本体。
- foreground/background 已从状态 handoff 推进到 attach/detach 输出流切片，但仍不是完整 Claude Code 式交互 runtime：当前可附着读取当前轮输出并分离回后台，尚未接管子进程 stdin、未把 live tool permission/TUI 控制权完整转交父前台。
- realtime IPC 已有常驻通道与 event bus fan-out 的最小可执行切片，但仍是 opt-in/best-effort：默认继续 durable file bus，实时通道只推送已持久化事件，尚未承载 mailbox trigger、tool permission request、stdin/stdout 全双工交互或跨进程自动恢复。**且已实测确认该通道当前无人订阅（见下方缺陷 1），即 opt-in 打开后对父→子延迟无任何改善。**
- workflow runtime/scheduler/child runner 仍未重构：这是本 feat 的明确边界，已有 workflow 正确主流程不在本次改动范围内。

### 实现前必读：参考实现路线可能替换当前设计

> **⚠️ 实现 realtime IPC / mailbox / attach / resume 相关切片前，请先读
> `docs/ga_subagent_claudecode_codex_ipc_reference_2026-07-29.md`。**
>
> 该文档是对本地 Claude Code 源码（`D:\git_codes\claude-reviews-claude\claude-code-fork\src`）
> 与 Codex 源码（`D:\git_codes\codex`）的逐文件带行号调研，已验证的结论是：
> **Claude Code 和 Codex 都不用 GA 现在这套 "durable mailbox 为权威源 + 可选 realtime IPC 只做通知"。**
>
> - Claude Code：子 agent 同进程（`forkedAgent.ts` `createSubagentContext()`），live 父→子消息走
>   AppState 内存数组 `pendingMessages`，在下一个 tool round 以 `queued_command` attachment 注入；
>   sidechain JSONL 只做 transcript/resume，且运行期注入的 attachment **故意不落盘**
>   （`runAgent.ts:770-789`）。唯一的文件 mailbox 是 teammate swarm 专用的整文件 JSON 数组。
> - Codex：协议层就是 SQ/EQ 队列对，父子投递走内存 `ThreadManagerState::send_op` →
>   `CodexThread::submit`；rollout JSONL + SQLite 只做 history/replay/fork。其
>   `Op::InterAgentCommunication { author, recipient, other_recipients, content, trigger_turn }`
>   与 GA mailbox 消息字段几乎同构（连 `trigger_turn` 命名都一致），说明 GA 的**协议语义**是对的，
>   分歧只在**传输载体**。
>
> 后续实现可能改按 Claude Code 或 Codex 的做法，而不是继续沿用当前 GA 这套。该文档第 4 节给出了
> 三条可选路线的具体改造点与代价：
> 4.1 走 Claude Code 路线（同进程 + 内存队列）、4.2 走 Codex 路线（SQ/EQ + 内存 thread map）、
> 4.3 折中（durable mailbox 仍为权威源，只把 trigger 信号实时化）——4.3 即下方剩余增强项
> "realtime IPC 承载 mailbox trigger" 的技术依据。
>
> 顺带记录两个必须遵守的边界（源自参考实现的踩坑）：fork/snapshot 前必须先 flush 异步落盘队列
> （Codex `control.rs:390-395`）；元数据永不领先 JSONL（Codex `live_writer.rs:75-87`）；
> Windows named pipe 需做 owner/ACL 校验（Codex `windows_pipe.rs:56-82`）。

### 已确认的 IPC / mailbox 实现缺陷（2026-07-29，第三轮已实测复现）

复核 GA 代码后确认："当前父子 IPC 不合适" 的判断成立，但根因不是 durable-file 路线本身，
而是可就地修复的实现缺陷。**第三轮用一次性并发探针实测，mailbox 缺陷全部复现且比预估严重。**
完整任务分解与 TDD 要求见 `docs/ga_subagent_ipc_implementation_plan_2026-07-29.md`；
实测证据与归因见调研文档 3.3 / 3.4 / 3.5 节。

> **修复状态（2026-07-29）**：缺陷 3 / 4 / 5（P0 正确性，M1-M4）、缺陷 6（P0 安全，S1+S2）、
> 缺陷 1（死通道，R1+R2+R4）、缺陷 2（轮询耦合，R3）、缺陷 7（上限守护，G1）
> **已按 TDD 修复并全绿**，下面对应条目保留原始诊断以备回溯，并在条目末尾标注实际落地方式。
> 规划内 P0/P1 全部完成；P2 的 B1-B3 也已于 2026-07-30 全部落地，见规划文档 1.1 节进度表。
> 规划外另补了 M5（registry 锁）、M6/M7（语义唯一性守卫），见规划文档 §1.5 / §1.6。
> 真实 API E2E 已通过（`claude-opus-5` / `provider: gorouter`），实测父→子唤醒
> **0.25 秒**（轮询间隔故意设成 30 秒，所以只可能来自 realtime 信号）；
> E2E 另外暴露并修掉两个规划外缺陷（registry 陈旧行占满活跃额度、
> resume 的 str content history 在 anthropic 线必炸），见规划文档 1.2 / 1.3 节。
>
> **补充（2026-07-30）**：本文 §6.2 schema 里的 `cascade` 与 §6.3 P0 测试项 "close descendants"
> 此前从未实现（`close_agent` 只关目标本身），现已按 TDD 补齐：
> `SubagentRegistry.descendants()` + `SubagentManager.close_agent(cascade=True)`，
> 后代由深到浅先关、目标最后关，单个后代失败只记录不中断整棵树的收尾。
> 至此 §6 的 P0 条目全部落地。
>
> **同日复核发现一项新的 P0（M5），已修复**：`registry.json` 的 `_load()`/`_save()` 是无锁读改写，
> 与 M1 修复前的 mailbox 同构。实测 4 writer × 40 轮**丢 75% 的行**，且
> **41 个 `run_id` 被 ≥2 个 agent 共用** —— `run_id` 决定 `artifact_dir`、realtime 通道地址
> 与 authkey 侧车路径，碰撞会把 S1/S2 建立的"每 run 一把密钥"隔离粒度悄悄放大。
> 已按 TDD 加 `registry.json.lock`（复用 M1 的共享 `cross_process_lock`），
> 复验 4 writer × 60 轮：240 行全存活、240 个 run_id 全不同、零错误。
> 实测证据见 `docs/ga_subagent_control_plane_defects_2026-07-30.md`，
> 落地细节见规划文档 §1.5。同一轮复核还校准了 B1 的真实爆炸半径
> （单 agent 局部风险，24 条未读事件即阻塞，而非文档原先推断的全局链路风险）
> 并量到 B3 的写放大（`wait_agents` 每秒 20 次原子写，正在喂 M5 的竞态）。
>
> **B1 已修复（2026-07-30）**：`publish()` 改为 per-subscriber bounded 队列 + 独立发送线程，
> 队列满丢最老一条并插队投出 `channel_lagged`（只带 `type` / `dropped`，不带正文，
> 与 R2 一致）；`close()` 通过 `os.dup` + `shutdown(SHUT_RDWR)` 叫回卡在 POSIX `send()`
> 里的线程。复验：`queue_size=4` + 不读的订阅者下 200 次 publish 全部快速返回
> （修复前第 24 条即卡死），同通道上的健康订阅者不受影响且事件仍有序。
> 落地细节见规划文档 §6 的"B1 落地记录"。P2 仅剩 B3、B2。
>
> **B3 已修复（2026-07-30）**：分两半 —— `probe_agent()` 让 `wait_agents` 的检测循环
> 不再写盘（同规模复验 40 次原子写 → **0 次**），`wait_for_signal()` / `signal()` 让父子
> 在已有通道上双向唤醒（先用 300 轮交错 send/recv 探针验证了单 Connection 双向并发安全）。
> 无通道时保留盲 sleep，`event_seq` cursor 语义不动 —— watch 负责唤醒，cursor 负责不漏。
> 落地细节见规划文档 §6 的"B3 落地记录"。P2 仅剩 B2。
>
> **B2 已修复（2026-07-30）**：先实测再动手，结果推翻了"纯结构收益"的原判 ——
> 重放同一逻辑提交时 `followup_task` 会写出 2 条 `trigger_turn` 行（子 agent 把任务干两遍），
> `spawn_agent` 会起第二个真实进程和第二个 `run_id`；`enqueue` 的 `message_id` 去重分支
> 一直存在，但 13 个 handler 从未有人传过 id。修法对齐 Codex `Submission { id, op, trace }`：
> 消息面把提交 id 推导成 mailbox 行 id 复用既有去重（不另立账本），spawn/close 走新的
> `subagent_submissions.py::SubagentSubmissionLog`（首写为准，重放用 `dataclasses.fields`
> 过滤重建首次返回值，任务目录已删也不抛错）。顺带补上 Codex
> `Op::InterAgentCommunication` 的 `other_recipients` 多播 ——
> 同收者列表事后 `annotate()` 补写，绝不让它有本事让已成功的投递失败。
> 落地细节见规划文档 §6 的"B2 落地记录"。**P2 的 B1/B2/B3 至此全部完成。**

> **M6 / M7 已修复（2026-07-30，规划外新增）**：B2 落地后被追问"重复执行的 bug 修好了吗"，
> 诚实答案是"机制在，但要模型自己带 id"。于是先查两个参考实现有没有"分发层自动派 id"，
> 结论是**都没有** —— Codex 的 `Submission.id` 只为关联 Event（`session/mod.rs:688` 由宿主
> `Uuid::now_v7()` 生成，`submission_loop` 只在 tracing 里碰它，`SpawnAgentArgs` /
> `SendMessageArgs` 根本不带 id）；Claude Code 宿主层完全没有幂等（`AgentTool.tsx:82-100`
> 无幂等键，`spawnMultiAgent.ts:268-293` 对重名直接追加 `-2`/`-3`，`inProgressToolUseIDs`
> 完成即删除，结构上不可能当已执行集合）。两家真正的护栏是**语义唯一性**：
> Codex `agent/registry.rs:247-250` 对已存在的 agent path 直接 `Err(UnsupportedOperation)`。
>
> GA 的 `agent_loop.py:68-92` 同样没有重放路径（无重试、无 WebSocket 重连、无
> orphaned-permission 再入），所以自动派 id 挡不住唯一真实的重复来源 ——
> 模型在**新一轮**里重发同一个调用，那时任何按 `(session_id, turn, index)` 推导的 id 都不一样。
> 因此改为补语义唯一性守卫，实测量到两处规划外缺陷：
>
> - **M6**：`spawn_agent("reviewer")` 在 `/root/reviewer` 仍在运行时静默产出 `/root/reviewer_1`
>   —— 第二个 OS 进程、第二个活跃名额、第二份真实 LLM 花费，而模型以为只有一个 agent。
> - **M7**：`resume_agent` 完全没有守卫。resume 一个进程仍活着的 agent 会再起一个进程并把
>   `state.json` 的 pid 覆盖成新的，首个 pid 从此无人引用（wait/interrupt/close 都摸不到），
>   却继续烧真实花费；且 B2 给 spawn/close/followup 都加了 `submission_id`，唯独漏了 resume。
>
> 修法：`SubagentNameConflictError`（带 `agent_path`）+ 写锁内判活 + 只读前置检查
> `reject_if_live()`（拒绝必须发生在推导 `temp/<task_name>` 之前，否则新 agent 会拿到旧
> agent 的目录）；`resume_agent` 补 `submission_id` 并复用同一错误类。
> **只有"活着"才冲突** —— 已关闭/崩溃的 agent 仍拥有它的 task dir 和 artifact，
> 那是它做过什么的唯一证据，那种情况改名才是对的；判活"判断不了就算活着"。
> 错误文案写给模型（点名 `followup_task` / `close_agent` / 换名字），
> `ga.py` 对这个错误类跳过 `format_error`，不让 `@ file:line` 噪声把策略答复伪装成崩溃。
> 五个 op 的 `submission_id` schema 措辞从"可选"改成带触发条件的义务
> （Codex 就是靠措辞让模型交出 `item_id` 的）。落地细节见规划文档 §1.6。
>
> **真实 API E2E 已通过（2026-07-30）**：M6/M7 的 14 条单测全部注入了假的
> `process_exists`，恰好把"判活"这唯一判定输入 stub 掉了。
> `tests/real_subagent_guard_e2e.py` 用真实子进程 + 真实 LLM 轮次 + `psutil` 扫真实进程
> 补上这一环，`passed: true` / `issues: []`：拒绝后 OS 层面确无新进程
> （`newProcessesAfterRefusal: []`），活 agent 的 `state.json` pid 未被覆盖，
> 同 `submission_id` 重放 followup/resume 各只生效一次，close 后同名改名且旧
> `output.txt` 逐字节完好。细节见规划文档 §1.7、缺陷文档 §3.7。

1. **realtime channel 建了但子 agent 从未订阅（死通道）。** `subagent_realtime_ipc.py:21`
   `connect_realtime_channel()` 在非测试代码里零调用者（`grep` 仅命中定义处）；
   `SubagentManager._open_realtime_ipc()`（`subagent_manager.py:209-218`）只在父侧
   `start()` Listener 并把地址写进 `ipc_endpoint`（`:421`），`agentmain.py` 从未连接。
   `_publish_realtime_event()`（`:220-224`）的 subscribers 永远是空列表。
   所以 `GA_SUBAGENT_REALTIME_IPC=1` 目前对父→子延迟毫无改善——子侧仍是纯轮询。
   **这是"realtime IPC 感觉没用"的直接原因，最高性价比修复点（规划文档 R1）。**
   **✅ 已修复（R1+R2+R4）**：`subagent_realtime_ipc.py` 新增 `SubagentRealtimeSubscriber`
   与 `resolve_child_subscription()`；`run_task_worker_loop()` 启动即订阅，等回复时
   `subscriber.wait(reply_sleep_s)` 取代盲睡。realtime 事件**只作空信号**，正文仍从
   durable mailbox 读（有断言防止将来把正文塞进事件走捷径）；订阅结果写入
   `state.json` 的 `child_ipc_status` / `child_ipc_fallback_reason`，中途断连也会被记录，
   子侧不再静默失败。

2. **父→子延迟来自 2 秒轮询，且轮询粒度与寿命耦合。** `agentmain.py:827` 默认
   `reply_wait_iterations=300, reply_sleep_s=2`，`agentmain.py:960-961` 先 sleep 再
   `consume_trigger_turn()`，平均 1s / 最差 2s 才看到消息；`300×2s` 同时决定
   轮询粒度与子 agent 空转寿命（600s，见 `:986-989` 的 `for...else`），
   应拆成独立的 `poll_interval_s` 与 `idle_timeout_s`（规划文档 R3）。
   **✅ 已修复（R3）**：`run_task_worker_loop()` 新增 `poll_interval_s` / `idle_timeout_s`
   （+ 可注入的 `monotonic_fn`），等待改为 deadline 驱动、末片钳到 deadline；
   旧的 `reply_wait_iterations` / `reply_sleep_s` 原义不变（有测试锁定逐次时序）。
   子进程通过 `GA_SUBAGENT_POLL_INTERVAL_S` / `GA_SUBAGENT_IDLE_TIMEOUT_S` 环境变量生效——
   spawn 命令行是固定的，不走环境变量这两个旋钮只有测试能用到。

3. **mailbox 无跨进程锁 + 整文件截断重写 —— 已实测，丢消息率极高。**
   `subagent_mailbox.py:30,50,51` 的 `enqueue()` 是无锁 read-modify-write，
   `:111` 用 `open(self.path, "w")` 就地截断。实测（4 writer × 200 轮，
   `threading.Barrier` 对齐起跑）：
   - 并发 `enqueue`：800 条**丢 600 条（75%）**；丢失量 =(writers-1)×rounds，
     与理论完全吻合，说明是**结构性必然而非偶发竞态**。
   - `enqueue` 与 `consume_trigger_turn` 并发（**生产路径上的真实场景**：
     父 `send_message` 与子轮询消费天然并发）：**93/200 轮（46.5%）新消息
     既不在盘上也未被消费，即彻底丢失**。
   - `_write_rows` 截断窗口：500 行 mailbox 写入期间，**86/259 次并发读（33%）
     观测到不完整或空文件**；此刻崩溃会留下被截断的 mailbox。
   - **套用 `subagent_event_bus.py:117-142` 现成的 `_locked()` 后重跑，丢失归零**——
     修复方案已被实测验证，无需新造锁机制（规划文档 M1/M2）。
   结论：**"durable mailbox 为权威源" 目前不成立**，这是本 feat 最严重的正确性缺陷。
   **✅ 已修复（M1+M2）**：`subagent_state.py` 抽出共享 `cross_process_lock()`（event bus
   同时改为委托它，避免两套锁实现漂移），`SubagentMailbox` 持 `mailbox.jsonl.lock` 全程加锁；
   `_write_rows()` 改走新增的 `atomic_write_lines()`（tmp + `_replace_file`）。
   锁与原子替换二者都留——锁防逻辑覆盖，原子替换防脏读与崩溃残缺。

4. **`message_id` 由行数派生，并发 100% 碰撞。** `subagent_mailbox.py:33`
   `message_id or f"msg_{len(rows) + 1:06d}"`；实测 4 writer × 200 轮**全部碰撞**。
   更糟的是 `:31-32` 的去重逻辑会把碰撞消息当同一条返回——**丢失被伪装成幂等命中**，
   比直接报错更难查（规划文档 M3）。
   **✅ 已修复（M3）**：`_new_message_id(rows)` 从行数起步并跳过已占用 ID。
   未改成 uuid，因为 `agentmain.py:980` / `subagent_manager.py:1107` 会把 `message_id`
   透出到事件与工具返回值，`msg_%06d` 的顺序可读性在排查父子通信时价值更高。

5. **仓库里有两套分叉的 mailbox 消费实现，被调用的那套用的是不安全写法。**
   `subagent_state.py:77` `consume_mailbox_trigger()` 用 tmp + `os.replace` 原子替换（安全）
   但**从未被调用**（`agentmain.py:27` import 了却无调用点，死代码）；
   实际被调用的 `SubagentMailbox.consume_trigger_turn()`（`agentmain.py:969`）用就地截断（不安全）。
   两者消费范围语义也不一致（前者只消第一条 trigger 行，后者消 trigger 及其之前所有 queue_only）。
   应统一为一套并删死代码，否则后来者会改错文件（规划文档 M4）。
   **✅ 已修复（M4）**：`consume_mailbox_trigger()` 与 `agentmain.py:27` 的 import 已删除，
   `SubagentMailbox.consume_trigger_turn()` 为唯一实现；测试断言 `subagent_state`
   不再暴露第二个消费函数、`agentmain.py` 不再引用它，防止分叉复活。

6. **realtime channel 默认无认证，地址可预测（安全缺口）。**
   `subagent_realtime_ipc.py:39,52` 的 `authkey` 默认 `None`，
   而 `subagent_registry.py:76` 的 `run_id = f"run_{run_no:06d}"` 是顺序编号，
   拼出 `\\.\pipe\ga_subagent_run_000001`。本机任意进程可猜名连入并接收该子 agent
   **全部事件流**（含 task 内容、工具参数、权限决策）。
   **必须与 R1 同批修复——不能先把未认证通道接通**（规划文档 S1/S2）。
   参考 Codex `tui/src/ide_context/windows_pipe.rs:56-82` 的 `validate_pipe_server_owner`。
   **✅ 已修复（S1）**：per-run `secrets.token_bytes(32)` authkey，父侧 Listener 与子侧
   `connect_realtime_channel()` 双向使用；密钥走 `<task_dir>/ipc_authkey` 侧车文件
   （`chmod 0o600`），**不进 `state.json`**（后者会被 `read_agent` 返回给 LLM），
   `ipc_endpoint` 只暴露 status/address/family/subscriber_count；`close_agent` 关通道时删密钥。
   **✅ 已修复（S2）**：authkey 只证明"对端知道 secret"，不证明"该端点值得交出 secret"。
   POSIX 侧 `default_channel_address()` 每次调用都 `chmod 0o700`（不止新建时，宽 umask
   下建出的旧目录也要收紧）；Windows 侧 `validate_channel_owner()` 在 `Client()` 之后
   立刻按 Codex `validate_pipe_server_owner` 的路径校验管道服务端所有者
   （`GetNamedPipeServerProcessId` → `OpenProcess` → token user SID 比对当前用户），
   不匹配或读不出来一律 fail closed：先关句柄再抛 `PermissionError`，
   子侧归为 `child_ipc_status=fallback` 退回 durable mailbox 轮询。

7. **无 agent 深度 / 总数上限。** `subagent_registry.py` / `subagent_manager.py` /
   `subagent_agent_path.py` 里 grep 不到任何 depth / max_agents / MAX_ 限制，
   子 agent 可递归 spawn，且每个都是独立 OS 进程（进程/内存/真实 API 费用失控风险）。
   参考 Codex `core/src/agent/registry.rs:23-26` 的 `Mutex<ActiveAgents>` + `AtomicUsize`
   与 `control.rs:221` `reserve_spawn_slot`（规划文档 G1）。
   **✅ 已修复（G1）**：`SubagentRegistry` 加 `max_depth=3` / `max_active_agents=8`
   （`GA_SUBAGENT_MAX_DEPTH` / `GA_SUBAGENT_MAX_ACTIVE` 可覆盖），`create_child()` 落盘前校验，
   越限抛 `SubagentTreeLimitError` 并先写 `spawn_rejected` 事件。
   **实现时发现一个前置缺陷**：`spawn_agent()` 原先硬编码 `parent_path=AgentPath.root()`，
   孙 agent 也注册成 `/root/<name>`，树在 registry 里永远扁平——深度上限本来永远不会触发。
   因此新增 `SubagentManager.self_agent_path` 并通过 `GA_SUBAGENT_AGENT_PATH` 环境变量
   传给子进程（子进程跑同一份代码、同一个 registry，不告知身份它的 spawn 又会退回 `/root`）。
   上限管**活跃数**而非历史累计（关掉一个即可再开一个，有测试锁定）。
   **真实 E2E 又暴露一个后续缺陷**：活跃判定原先只看 `status != "closed"`，而 `status`
   只在 `close_agent` 正常走完时才变 `closed`——崩溃 / 被 kill / 机器重启留下的行永久占额，
   实测 registry 36 行"活跃"、实际存活进程 1 个，E2E 直接起不来。现加
   `SubagentRegistry(process_exists=...)` + `_reap_stale_agents()`：进程不在了就把行标成
   `closed` / `closed_status="stale"`（不删，行是崩溃的唯一证据），探针抛异常时按"活着"处理
   （误判活着只是少一个额度，误判死了会把活 agent 从 `list_agents` / `wait_agents` 里摘掉）。

修复顺序：先 P0 正确性（3→4→5）与 P0 安全（6），再 P1 实时性（1→2），
G1（7）可并行。全部不需要改路线。**尤其注意顺序：不能先做实时化再补锁——
实时通知会显著提高父子并发访问 mailbox 的频率，等于把 46.5% 的丢消息概率暴露得更频繁。**

**为什么暂不照搬 Claude Code 同进程路线：** GA 工具层依赖进程级全局状态，
`ga.py:435-442` 的 `inline_eval` 直接 `os.chdir(cwd)` 改进程 cwd，
`agentmain.py:268-281` 的 `load_tool_schema()` 写模块全局 `TOOLS_SCHEMA` 并按模型名
切换中英文 schema。同进程并发子 agent 会互相踩 cwd 与 schema。因此走 Claude Code
路线的前置重构成本（把 cwd / tool schema 收进 per-agent context）大于 IPC 本身，
应在上述缺陷修完、且确实需要共享内存态或 live tool permission 交互时再评估。

**为什么"改走 Codex 路线"也不解决 IPC 问题（2026-07-29 第二轮复核）：**
针对性复核确认 **Codex 根本没有父子 agent IPC**——`core/src/agent/`、
`core/src/thread_manager.rs`、`core/src/session/mod.rs` 下 `Command::new` /
`std::process` / `tokio::process` 调用点为 **0**；子 agent 就是
`session/mod.rs:662` 的一个 `tokio::spawn` task，并**直接继承父的 `Arc`**
（`thread_manager.rs:1220`）。它存在的所有 IPC（app-server stdio/UDS/WebSocket、
`\\.\pipe\codex-ipc`、远端 `codex exec-server`）都是 client↔core / IDE 上下文 /
远端命令执行宿主，从不用于 agent↔agent。所以"改走 Codex 路线"在传输层等价于
"取消 IPC、改同进程"，与 Claude Code 路线是**同一笔代价**，并额外要求 Python 侧
asyncio 化（GA 现有链路是同步阻塞风格）。

反过来，两个对 GA 极有利的发现：

- **GA 的工具面已经是 Codex 的工具面**：`spawn_agent` / `wait_agent` / `close_agent` /
  `send_message` / `followup_task` / `list_agents` 与 Codex `multi_agents_v2` **6/6 同名**
  （`core/src/tools/handlers/multi_agents_v2/*.rs`），加上 `trigger_turn` 语义同构。
  协议层无需改造。
- **Codex 的 `wait_agent` 就是"空信号 + 回读权威源"**：
  `multi_agents_v2/wait.rs:151` `wait_for_mailbox_change(mailbox_rx: &mut watch::Receiver<()>)`，
  信号源 `session/input_queue.rs:26` `mailbox_tx: watch::Sender<()>`——负载是空元组，
  只说"变了，去读"。这与 GA 设想的 "durable mailbox 存内容 + realtime channel 只推 trigger"
  **架构完全同构**。即 GA 的折中方案不是妥协，而是**跨进程版的 Codex 通知模型**。

可低成本借鉴、与路线无关的两项：`registry.rs:23-26` 的 **agent 数量/深度上限守护**
（GA 目前 grep 不到任何 depth / max_agents 限制，存在递归 spawn 失控风险）；
`agent_jobs/spawn_agents_on_csv.rs:20` 的批量 fan-out 工具。

渐进路径（已细化为 `docs/ga_subagent_ipc_implementation_plan_2026-07-29.md` 的任务总表）：
阶段 A = 上述缺陷修复（P0 正确性 M1-M4 + P0 安全 S1 + P1 实时性 R1-R4，
等价于跨进程版 Codex `watch` 通知）；
阶段 B（纯收益、不动进程模型）= 把 `spawn/send/close/interrupt` 收敛成显式 `Op`（B2）、
加 agent 深度/数量上限（G1）、realtime channel 补 bounded + `Lagged` 背压（B1）、
状态订阅改 watch 语义（B3）；
阶段 C（不建议）= 同进程运行时 + asyncio 化。详见调研文档 4.2.1 节。

### 缺陷实测方法记录（供后续复现）

上述 mailbox 缺陷用一次性探针脚本实测确认（写在 `temp/` 下，跑完即删，未入库）。
方法要点，后续写正式并发红测时沿用：

- 用 `threading.Barrier(N)` 对齐 N 个线程的起跑点放大竞态窗口，**不要靠 `sleep` 碰运气**。
- 断言确定性事实：落盘条数 == 写入条数、自动 ID 集合大小 == writer 数、
  并发读永不观测到少于已提交行数。**不要断言时序**。
- 线程级竞态即证明跨进程竞态——跨进程没有 GIL 保护，只会更差。
- 无锁实现在 4 writer 下每轮必丢（丢失量 =(writers-1)×rounds 与理论吻合），
  所以少量轮次即可稳定复现，正式测试不必跑 200 轮拖慢 CI。
- 对照组必做：把待验证的修复方案（这里是 event bus 现成的 `_locked()`）套上去重跑，
  确认缺陷消失，避免修了个不相关的东西。


### 最近验证记录

- 权限继承 fresh focused test：`python -m unittest tests.test_ga_subagent_permissions tests.test_subagent_manager.SubagentManagerSpawnWaitMailboxTest.test_spawn_agent_records_parent_permission_mode_for_inherit_profile tests.test_ga_subagent_tools.GaSubagentToolsTest.test_spawn_agent_tool_inherits_parent_permission_mode_metadata`，结果 `Ran 12 tests ... OK`。
- worktree 运行根 / 失败一致性 fresh focused test：`python -m unittest tests.test_subagent_manager.SubagentManagerSpawnWaitMailboxTest.test_spawn_agent_worktree_isolation_records_created_worktree_and_cwd tests.test_subagent_manager.SubagentManagerSpawnWaitMailboxTest.test_spawn_agent_marks_registry_closed_when_worktree_creation_fails`，结果 `Ran 2 tests ... OK`。
- worktree diff / cleanup fresh focused test：`python -m unittest tests.test_subagent_worktree.SubagentWorktreeTest.test_summarize_subagent_worktree_captures_status_and_diff tests.test_subagent_worktree.SubagentWorktreeTest.test_remove_subagent_worktree_runs_git_remove_and_deletes_leftover_path`，结果 `Ran 2 tests ... OK`；`python -m unittest tests.test_subagent_manager.SubagentManagerReadTest.test_read_agent_records_worktree_summary_for_worktree_isolation tests.test_subagent_manager.SubagentManagerReadTest.test_close_agent_can_capture_and_cleanup_worktree`，结果 `Ran 2 tests ... OK`；`python -m unittest tests.test_ga_subagent_tools.GaSubagentToolsTest.test_close_agent_tool_can_cleanup_worktree_and_return_summary tests.test_ga_subagent_tools.GaSubagentToolsTest.test_tool_schemas_include_subagent_tools`，结果 `Ran 2 tests ... OK`。
- event bus 并发 / artifact_id / schema fresh focused test：`python -m unittest tests.test_subagent_event_bus.SubagentEventBusTest.test_concurrent_append_event_assigns_unique_monotonic_event_seq tests.test_ga_subagent_tools.GaSubagentToolsTest.test_read_agent_result_supports_artifact_id tests.test_ga_subagent_tools.GaSubagentToolsTest.test_tool_schemas_include_subagent_tools`，结果 `Ran 3 tests ... OK`。
- sidechain transcript fresh focused test：`python -m unittest tests.test_subagent_manager.SubagentManagerReadTest.test_close_agent_appends_sidechain_agent_closed_event_when_session_is_known tests.test_agentmain_subagent_lifecycle.AgentMainSubagentLifecycleTest.test_task_worker_loop_records_sidechain_request_tool_result_permission_and_final_output tests.test_agentmain_subagent_lifecycle.AgentMainSubagentLifecycleTest.test_task_worker_loop_records_sidechain_permission_decision_for_denied_tool`，结果 `Ran 3 tests ... OK`。
- sidechain transcript replay summary fresh focused test：`python -m unittest tests.test_subagent_transcript.SubagentTranscriptStoreTest.test_replay_reconstructs_sidechain_summary_from_events tests.test_ga_subagent_tools.GaSubagentToolsTest.test_read_agent_result_can_include_transcript_replay_summary tests.test_ga_subagent_tools.GaSubagentToolsTest.test_tool_schemas_include_subagent_tools`，结果 `Ran 3 tests ... OK`。
- sidechain transcript resume context fresh focused test：`python -m unittest tests.test_subagent_transcript.SubagentTranscriptStoreTest.test_replay_reconstructs_sidechain_summary_from_events tests.test_subagent_transcript.SubagentTranscriptStoreTest.test_build_resume_context_projects_replay_into_backend_history tests.test_ga_subagent_tools.GaSubagentToolsTest.test_read_agent_result_can_include_transcript_replay_summary tests.test_ga_subagent_tools.GaSubagentToolsTest.test_read_agent_result_can_include_resume_context_from_transcript tests.test_ga_subagent_tools.GaSubagentToolsTest.test_tool_schemas_include_subagent_tools`，结果 `Ran 5 tests ... OK`。
- sidechain replay timeline TDD 证据：先写 `tests.test_subagent_transcript.SubagentTranscriptStoreTest.test_replay_timeline_supports_editable_resume_projection` 并确认红测 `AttributeError: 'SubagentTranscriptStore' object has no attribute 'build_replay_timeline'`；实现 `build_replay_timeline()`、`build_resume_context(edits=...)` 后 focused 结果 `Ran 1 test ... OK`。随后写工具层红测 `tests.test_ga_subagent_tools.GaSubagentToolsTest.test_read_agent_result_can_include_replay_timeline_and_edited_resume_context`，确认返回缺 `transcript_timeline`，并确认中英文 schema 缺 `include_transcript_timeline` / `resume_context_edits`；实现 `do_read_agent_result` timeline/edits 暴露和 schema 后 `python -m unittest tests.test_subagent_transcript.SubagentTranscriptStoreTest.test_replay_timeline_supports_editable_resume_projection tests.test_ga_subagent_tools.GaSubagentToolsTest.test_read_agent_result_can_include_replay_timeline_and_edited_resume_context tests.test_ga_subagent_tools.GaSubagentToolsTest.test_tool_schemas_include_subagent_tools`，结果 `Ran 3 tests ... OK`。
- foreground/background handoff fresh focused test：`python -m unittest tests.test_subagent_manager.SubagentManagerReadTest.test_foreground_background_handoff_updates_state_and_events tests.test_ga_subagent_tools.GaSubagentToolsTest.test_foreground_background_tools_request_handoff tests.test_ga_subagent_tools.GaSubagentToolsTest.test_tool_schemas_include_subagent_tools`，结果 `Ran 3 tests ... OK`。
- attach/detach 输出流 TDD 证据：先写 `tests.test_subagent_manager.SubagentManagerReadTest.test_attach_agent_streams_live_output_tail_and_detach_returns_to_background` 并确认红测 `AttributeError: 'SubagentManager' object has no attribute 'attach_agent'`；实现 `AttachResult`、`SubagentManager.attach_agent()` / `detach_agent()`、输出切片 offset/truncation/eof 后 focused 结果 `Ran 1 test ... OK`。随后写工具层红测 `tests.test_ga_subagent_tools.GaSubagentToolsTest.test_attach_agent_tool_streams_output_slice_and_detaches`，确认 `GenericAgentHandler` 缺 `do_attach_agent` 且中英文 schema 缺 `attach_agent`/`detach_agent`；实现 `do_attach_agent` / `do_detach_agent` 与 schema 后 `python -m unittest tests.test_ga_subagent_tools.GaSubagentToolsTest.test_attach_agent_tool_streams_output_slice_and_detaches tests.test_ga_subagent_tools.GaSubagentToolsTest.test_tool_schemas_include_subagent_tools`，结果 `Ran 2 tests ... OK`。
- attach/detach 前后端模拟用户调用验证：新增 `tests.test_ink_bridge.InkBridgeTest.test_submit_can_drive_attach_detach_stream_through_frontend_bridge`，用 Ink bridge `submit()` 模拟用户输入，FakeAgent 通过真实 `GenericAgentHandler.do_attach_agent` / `do_detach_agent` 驱动附着/分离，focused 结果 `Ran 1 test ... OK`。
- realtime IPC TDD 证据：先写 `tests.test_subagent_realtime_ipc` 并确认红测 `ModuleNotFoundError: No module named 'subagent_realtime_ipc'`；实现 `SubagentRealtimeChannel`、平台化 channel address、客户端连接、subscriber fan-out、close 唤醒 accept loop 后 `python -m unittest tests.test_subagent_realtime_ipc`，结果 `Ran 6 tests ... OK`。随后写 `tests.test_subagent_ipc.SubagentIpcTest.test_realtime_mode_becomes_effective_when_channel_opens` / `test_realtime_mode_falls_back_when_channel_cannot_open`，确认 `normalize_ipc_metadata()` 不支持 `channel_factory` 的红测；实现 effective realtime / fallback 元数据后 `python -m unittest tests.test_subagent_ipc`，结果 `Ran 5 tests ... OK`。
- realtime event bus / manager integration TDD 证据：先写 `tests.test_subagent_event_bus.SubagentEventBusTest.test_publisher_receives_appended_events_and_failures_do_not_break_durable_append` 并确认 `SubagentEventBus.__init__()` 缺 `publisher` 的红测；实现 durable append 后 best-effort publisher fan-out 后 `python -m unittest tests.test_subagent_event_bus`，结果 `Ran 7 tests ... OK`。随后写 `tests.test_subagent_manager.SubagentManagerSpawnWaitMailboxTest.test_spawn_agent_enables_realtime_ipc_channel_and_publishes_bus_events`，确认 `SubagentManager.__init__()` 缺 `realtime_channel_factory` 的红测；实现 manager 注入 realtime channel、`ipc_endpoint` 元数据、close 时关闭 channel 后 focused 结果 `Ran 1 test ... OK`。
- resume_agent 可执行恢复 TDD 证据：先写 `tests.test_subagent_manager.SubagentManagerSpawnWaitMailboxTest.test_resume_agent_restarts_same_task_with_transcript_resume_context` 并确认红测 `AttributeError: 'SubagentManager' object has no attribute 'resume_agent'`；实现 manager 恢复路径后 focused 结果 `Ran 1 test ... OK`；随后补旧 `final_output_ref` 与旧 `_stop` / `reply.txt` 控制文件边界红测，先分别确认 `final_output_ref` 未清理、`_stop` 残留失败，再修复为恢复时清空旧 artifact ref 与控制文件，focused 结果 `Ran 1 test ... OK`。
- worker 续跑轮次 TDD 证据：先写 `tests.test_agentmain_subagent_lifecycle.AgentMainSubagentLifecycleTest.test_task_worker_loop_resumes_from_state_round_without_overwriting_previous_output` 并确认红测旧 `output.txt` 被覆盖；实现按 `state.round` 初始化输出轮次后 focused 结果 `Ran 1 test ... OK`。
- resume_agent 工具层 / schema TDD 证据：先写 `tests.test_ga_subagent_tools.GaSubagentToolsTest.test_resume_agent_tool_restarts_closed_subagent_from_transcript` 并确认红测 `AttributeError: 'GenericAgentHandler' object has no attribute 'do_resume_agent'`；schema 期望 `resume_agent` 后中英文 schema 红测缺工具；实现 `do_resume_agent` 与中英文 schema 后 `python -m unittest tests.test_ga_subagent_tools.GaSubagentToolsTest.test_resume_agent_tool_restarts_closed_subagent_from_transcript tests.test_ga_subagent_tools.GaSubagentToolsTest.test_tool_schemas_include_subagent_tools`，结果 `Ran 2 tests ... OK`。
- resume_agent 相关子集回归：`python -m unittest tests.test_subagent_manager tests.test_agentmain_subagent_lifecycle tests.test_ga_subagent_tools tests.test_subagent_transcript tests.test_subagent_registry`，结果 `Ran 68 tests ... OK`。
- 前后端模拟用户调用验证：新增 `tests.test_ink_bridge.InkBridgeTest.test_submit_can_drive_resume_agent_tool_through_frontend_bridge`，用 Ink bridge `submit()` 模拟用户输入，FakeAgent 通过真实 `GenericAgentHandler.do_resume_agent` / `SubagentManager.resume_agent` 驱动恢复，focused 结果 `Ran 1 test ... OK`；`python -m unittest tests.test_ink_bridge` 结果 `Ran 91 tests ... OK`。
- subagent regression：`python -m unittest tests.test_subagent_state tests.test_subagent_agent_path tests.test_subagent_mailbox tests.test_subagent_artifacts tests.test_subagent_transcript tests.test_subagent_notifications tests.test_subagent_roles tests.test_subagent_worktree tests.test_subagent_ipc tests.test_subagent_realtime_ipc tests.test_subagent_registry tests.test_ga_subagent_permissions tests.test_subagent_manager tests.test_subagent_event_bus tests.test_ga_subagent_tools tests.test_agentmain_subagent_lifecycle tests.test_ink_bridge`，结果 `Ran 217 tests ... OK`。
- workflow regression（确认未改动 workflow 主流程语义）：`python -m unittest tests.test_workflow_controller tests.test_workflow_integration tests.test_workflow_llm tests.test_workflow_models tests.test_workflow_permissions tests.test_workflow_permission_inheritance_e2e tests.test_workflow_plan_validator tests.test_workflow_planner_compiler tests.test_workflow_prompt_guided_planner tests.test_workflow_runtime tests.test_workflow_scheduler tests.test_workflow_store tests.test_workflow_child_agent`，首次整套仍出现已知 transient `test_runtime_observes_external_kill_state` deadline 失败；单测复跑 `Ran 1 test ... OK`，整套复跑结果 `Ran 165 tests ... OK`。
- 全量 Python unittest：`python -m unittest discover -s tests`，结果 `Ran 743 tests ... OK (skipped=1)`。
- 真实 API E2E：`GA_RUN_REAL_API_E2E=1 GA_REAL_API_EXPECTED_NAME=terra GA_REAL_API_EXPECTED_MODEL=gpt-5.6-terra python tests/real_subagent_terra_e2e.py`，结果 `passed: true`，使用 `llm.yaml` 中 profile `terra / gpt-5.6-terra`。该脚本现在覆盖真实双轮恢复：第一轮输出 `GA_SUBAGENT_TERRA_E2E_OK_20260727` 并产出 `final_output_round_0`；随后 `close_agent(reason="real_e2e_before_resume")`，再 `resume_agent(...)` 复用同一 `run_id` 启动第二轮，输出写入 `output1.txt`，产出 `final_output_round_1`，`finalOutputRounds == [0, 1]`，sidechain transcript 记录两轮 `request/turn_started/permission_decision/tool_call/tool_result/assistant/output_snapshot/final_output/turn_completed/agent_waiting_reply` 以及关闭事件。真实复测第一次曾因旧 `_stop` 控制文件残留导致第二轮空输出失败；补恢复前清理 `_stop` / `reply.txt` 后复跑通过。

关联调研：`docs/ga_subagent_mechanism_research_2026-07-27.md`、`docs/ga_subagent_claudecode_codex_ipc_reference_2026-07-29.md`（Claude Code / Codex 源码级消息通道调研 + GA 缺陷实测证据，实现前必读）

关联规划：`docs/ga_subagent_ipc_implementation_plan_2026-07-29.md`（IPC / mailbox 待实现优化任务总表、优先级、TDD 要求与切片顺序；动手前以此为准）

## 1. 总目标

GA 当前文件/进程型 subagent 已有可用雏形：`SubagentManager`、`state.json`、`events.jsonl`、`mailbox.jsonl`、父 inbox、`wait_agent` / `read_agent_result` 分离、context fork、`interrupt_agent`。但它仍然更像轻量后台进程协议，不是完整 subagent control plane。

本设计目标是把 GA 文件/进程型 subagent 升级为：

```text
有正式身份 / 有事件通知 / 有权限边界 / 有生命周期 / 有结构化 artifact / 可逐步恢复与协作
```

设计取向：

```text
主线按 Codex：AgentPath + registry + close/list/wait/send/followup + notification
借鉴 GA workflow：permission / transcript_events / capability snapshot / scheduler 思路，但不改动现有 workflow 正确代码
中长期吸收 Claude Code：sidechain resume / foreground-background / worktree / agent definition registry
```

重要边界：

- 本 feat 聚焦文件/进程型 subagent，不直接重构 `workflow_runtime.py`、`workflow_scheduler.py`、`workflow_child_agent.py` 正确主流程。
- 明确约束：不要直接重构 workflow_runtime.py、workflow_scheduler.py 或 workflow_child_agent.py；这些已有正确 workflow 代码只能作为参考，除非另开 feat 并先补回归测试。
- 可以只读参考 workflow 的模型与测试经验。
- 如需共享能力，优先新增独立小模块，先用测试证明不改变 workflow 行为。
- 不一次性照搬 Claude Code 全套 Agent/Task runtime。

## 2. 设计原则

### 2.1 Codex-first

优先借鉴 Codex 的核心语义：

- `AgentPath` canonical tree。
- `list_agents(path_prefix)`。
- `send_message` = QueueOnly。
- `followup_task` = TriggerTurn。
- `close_agent(previous_status)`。
- root 不能 close。
- `subagent_notification` 作为事件/上下文片段。
- 子 agent 继承 live turn runtime state，而不只是继承 prompt/history。

### 2.2 Workflow-safe

GA dynamic workflow 当前已有正确代码和测试，不能为了 subagent v2 重构它。

允许：

- 借鉴 `workflow_permissions.py` 的 permission profile。
- 借鉴 `workflow_models.py` 的状态枚举设计。
- 借鉴 `workflow_child_agent.py` 的 transcript_events / capability snapshot。
- 新增 subagent 专用模块。

不允许作为本 feat 的初始步骤：

- 重写 `workflow_runtime.py`。
- 改 `workflow_scheduler.py` 调度语义。
- 改 `workflow_child_agent.py` runner 主流程。
- 强行把 workflow child 与 process subagent 合并。

### 2.3 Durable first, realtime second

GA 是跨进程 Python 运行时，文件协议对 Windows 兼容和故障恢复仍有价值。因此：

- P0 保留 durable files：`state.json`、`events.jsonl`、`mailbox.jsonl`。
- 但它们必须变成结构化协议，不再是临时补丁。
- P1/P2 再引入 event cursor / notification queue。
- P3 再考虑 socket/pipe/event server 等更实时通道。

### 2.4 TDD 分阶段落地

每个阶段必须先有 failing tests，再实现。

优先测试文件：

- `tests/test_subagent_agent_path.py`
- `tests/test_subagent_registry.py`
- `tests/test_subagent_event_bus.py`
- `tests/test_subagent_mailbox.py`
- `tests/test_ga_subagent_permissions.py`
- `tests/test_agentmain_subagent_lifecycle.py`
- `tests/test_ga_subagent_tools.py`

## 3. 目标架构

### 3.1 模块结构

建议新增：

```text
subagent_agent_path.py       # Codex-style AgentPath
subagent_registry.py         # registry / run_id / path tree
subagent_event_bus.py        # event append/read/cursor/notification abstraction
subagent_mailbox.py          # QueueOnly / TriggerTurn single mailbox protocol
subagent_permissions.py      # 文件/进程型 subagent permission glue，复用 workflow_permissions.py
subagent_artifacts.py        # final_output_ref / manifest / sha256
subagent_transcript.py       # 后续 sidechain transcript schema
```

保留并逐步改造：

```text
subagent_manager.py          # 变成 orchestrator，调用 registry/event/mailbox/artifact
subagent_state.py            # 可保留 atomic/jsonl helpers，逐步减少业务语义
agentmain.py                 # worker loop 接入 mailbox/event/artifact
 ga.py                       # tool handlers 增加 close_agent/permission args/path target
assets/tools_schema.json     # 更新工具 schema 和模型使用说明
```

注意：`workflow_*` 文件先不改主流程。

### 3.2 目标控制面

```text
spawn_agent
  ↓
AgentPath.resolve / validate
  ↓
SubagentRegistry.create_run
  ↓
SubagentEventBus.emit(agent_started)
  ↓
SubagentManager.spawn process
  ↓
worker loop
  ↓
SubagentEventBus.emit(turn_started / waiting_reply / completed / failed / closed)
  ↓
parent notification queue
  ↓
wait_agent / list_agents / read_agent_result / close_agent / followup_task
```

## 4. P0：身份、registry、run id

### 4.1 问题

当前 GA 文件/进程型 subagent 主要依赖：

```text
task_name
temp/<task_name>
```

缺点：

- 同名任务容易覆盖旧 artifact。
- path-like target 语义弱。
- 无法表达父子树。
- 无法按 path prefix list。
- 无法 close descendants。
- 无法稳定绑定某次 run。

### 4.2 方案：AgentPath

新增：`subagent_agent_path.py`

语义参考 Codex。

#### 数据结构

```python
@dataclass(frozen=True)
class AgentPath:
    value: str

    ROOT = "/root"

    @classmethod
    def root(cls) -> "AgentPath": ...
    @classmethod
    def parse(cls, value: str) -> "AgentPath": ...
    def join(self, name: str) -> "AgentPath": ...
    def resolve(self, ref: str) -> "AgentPath": ...
    @property
    def name(self) -> str: ...
    @property
    def parent(self) -> "AgentPath | None": ...
```

#### 校验规则

- absolute path 必须以 `/root` 开始。
- `/root` 是根。
- segment 只能包含：小写字母、数字、下划线。
- 禁止：`root`、`.`、`..`。
- 禁止空 segment。
- 禁止 path 以 `/` 结尾。

#### 示例

```text
/root                      ✅
/root/researcher           ✅
/root/researcher/worker_1  ✅
root                       ❌
/foo                       ❌
/root/Researcher           ❌
/root/a-b                  ❌
/root/..                   ❌
```

### 4.3 方案：SubagentRegistry

新增：`subagent_registry.py`

#### registry 文件

```text
temp/subagents/registry.json
```

#### registry entry

```json
{
  "schema_version": 1,
  "agent_path": "/root/researcher",
  "task_name": "researcher",
  "run_id": "agent_20260727_abcdef",
  "parent_path": "/root",
  "children": [],
  "task_dir": "temp/subagents/runs/agent_20260727_abcdef",
  "state_path": "temp/subagents/runs/agent_20260727_abcdef/state.json",
  "mailbox_path": "temp/subagents/runs/agent_20260727_abcdef/mailbox.jsonl",
  "events_path": "temp/subagents/runs/agent_20260727_abcdef/events.jsonl",
  "created_at": "...",
  "updated_at": "...",
  "closed_at": null,
  "status": "running",
  "turn_status": "running",
  "process_status": "alive",
  "pid": 12345,
  "permission_profile": "inherit-current-permissions",
  "fork_turns": "all",
  "parent_session_id": "..."
}
```

#### run dir

从：

```text
temp/<task_name>
```

迁移到：

```text
temp/subagents/runs/<run_id>
```

兼容期：

- 可以继续接受旧 `temp/<task_name>` 读取。
- 新 spawn 默认写 run dir。
- `task_dir` 从 registry 获取，不再由 task_name 直接推导。

#### API

```python
class SubagentRegistry:
    def create_agent(parent_path, task_name, *, metadata) -> RegistryEntry: ...
    def get(target: str) -> RegistryEntry | None: ...
    def list(path_prefix: str | None = None) -> list[RegistryEntry]: ...
    def update_status(agent_path, **fields) -> RegistryEntry: ...
    def mark_closed(agent_path, previous_status, reason) -> RegistryEntry: ...
    def descendants(agent_path) -> list[RegistryEntry]: ...
```

#### target 解析

工具 target 支持：

- canonical path：`/root/researcher`
- task name：`researcher`，解析为当前 agent path 下的 child 或 registry 中唯一 name
- run id：`agent_...`，用于 debug/恢复

冲突时要求用户或模型传 canonical path。

### 4.4 P0 测试

新增 `tests/test_subagent_agent_path.py`：

- parse root。
- join child。
- resolve absolute / relative。
- reject invalid names。

新增 `tests/test_subagent_registry.py`：

- create root child。
- duplicate task name 不覆盖 artifact。
- registry list by prefix。
- descendants。
- update status。
- mark closed 保留 artifact。

### 4.5 迁移风险

- 现有测试可能假设 `temp/<task_name>`。
- 兼容期可以让 registry entry 的 `legacy_task_dir` 指向旧位置。
- 工具返回继续包含 `task_dir`，但新增 `agent_path` / `run_id`。

## 5. P0：权限隔离

### 5.1 问题

文件/进程型 subagent 当前工具能力过宽，主要靠 prompt 限制。对只读调研、安全审查、并行探索等任务不够安全。

### 5.2 方案

复用现有：

```text
workflow_permissions.py
```

新增 glue：

```text
subagent_permissions.py
```

#### spawn_agent 新参数

```json
{
  "permission_profile": "inherit-current-permissions | read_only | restricted_mcp | explicit_approval",
  "allowed_tools": ["file_read", "load_skill"],
  "denied_tools": ["code_run", "file_write"],
  "allowed_mcp_servers": [],
  "denied_mcp_servers": [],
  "allowed_mcp_tools": [],
  "denied_mcp_tools": []
}
```

#### 执行方式

- `spawn_agent` 把 permission metadata 写入 registry/state。
- 子进程启动时读取 registry/state。
- `GenericAgentHandler` 初始化时读取当前 subagent permission policy。
- dispatch tool 前调用 policy。

#### approval 行为

- `read_only`：写/执行类工具直接 deny。
- `restricted_mcp`：按白名单/黑名单。
- `explicit_approval`：headless 子进程不阻塞等待 UI，返回 `approval_required`。
- `inherit-current-permissions`：保持现有行为。

### 5.3 P0 测试

新增 `tests/test_ga_subagent_permissions.py`：

- read_only 允许 `file_read`。
- read_only 拒绝 `file_write` / `file_patch` / `code_run`。
- explicit_approval 返回 approval_required，不 hang。
- restricted_mcp 白名单/黑名单。
- permission metadata 写入 state/registry/events。

### 5.4 注意

不要修改 workflow permission 正确行为；先写 process subagent 专用 glue 测试。

## 6. P0：close_agent 生命周期

### 6.1 问题

当前主要工具是 `interrupt_agent`，manager 内部虽有 `close_agent()`，但没有产品化工具语义。

### 6.2 方案

新增工具：

```text
close_agent
```

#### schema

```json
{
  "target": "/root/researcher",
  "reason": "parent_cleanup",
  "cascade": false,
  "timeout_seconds": 2.0
}
```

#### 返回

```json
{
  "status": "closed",
  "target": "/root/researcher",
  "previous_status": {
    "turn_status": "completed",
    "process_status": "waiting_reply"
  },
  "closed_descendants": [],
  "final_output_ref": "output1.txt",
  "final_output_sha256": "...",
  "graceful": true,
  "reason": "parent_cleanup"
}
```

#### 规则

- `/root` 不能 close。
- close 已 final agent 返回 no-op + previous_status。
- `cascade=true` 关闭 descendants。
- close 不删除 artifact。
- close 写 event：`agent_close_requested`、`agent_closed`。
- close 更新 registry/state。

> **✅ 已实现（2026-07-30）**：`cascade` 参数与 `closed_descendants` 返回字段已落地
> （`subagent_manager.py` `close_agent(cascade=...)` / `_close_descendants()` /
> `_close_single_agent()`，`subagent_registry.py` `descendants()`，工具层 `ga.py do_close_agent`
> 与中英文 schema 的 `cascade` 字段）。三条实现决定及其理由：
> 1. **后代由深到浅关，目标最后关**：子必须先于派生它的父消失，否则父的停机会和自己子进程的写入相互竞争；
>    目标放最后是为了"cascade 中途失败也不会只剩目标还活着"。
> 2. **单个后代失败只记录不抛**：某个后代 `state.json` 被别的进程锁住时若直接抛出，
>    剩余后代和目标都会继续运行 —— 半棵树加一个异常是最坏结果。失败以
>    `{"status": "error", "msg": ...}` 记入 `closed_descendants`。
> 3. **后代的 `close_reason` 写 `cascade_close:<ancestor>` 而非沿用父的 reason**：
>    事后看 `state.json` 能直接知道它是被谁的收尾带走的，而不是显示成一次独立的 `parent_cleanup`。
>
> 前缀匹配用 `path == prefix or path.startswith(prefix + "/")`，`/root/a` 不会误伤 `/root/ab`。

### 6.3 P0 测试

- close waiting agent。
- close running agent。
- close unknown target。
- close root 被拒。
- close descendants。
- previous_status 正确。
- closed 后 artifact 仍可 read。

## 7. P1：事件通知机制按 Codex 方向实现

### 7.1 问题

当前是文件轮询：

```text
父进程 wait_agent 轮询 state/events/inbox/process
子进程 waiting_reply 轮询 _stop/mailbox/reply.txt
```

这不适合继续扩展复杂协作。

### 7.2 方案：SubagentEventBus

新增：

```text
subagent_event_bus.py
```

#### event envelope

```json
{
  "schema_version": 1,
  "event_id": "evt_...",
  "event_seq": 42,
  "type": "agent_waiting_reply",
  "agent_path": "/root/researcher",
  "run_id": "agent_...",
  "task_name": "researcher",
  "created_at": "...",
  "status": {
    "turn_status": "completed",
    "process_status": "waiting_reply"
  },
  "payload": {}
}
```

#### event types

```text
agent_started
turn_started
turn_snapshot
turn_completed
agent_waiting_reply
message_queued
message_consumed
interrupt_requested
close_requested
agent_closed
agent_exited
agent_error
final_output_ready
```

#### cursor

registry/state 记录：

```json
{
  "last_event_seq": 42,
  "status_version": 7,
  "last_notification_id": "ntf_..."
}
```

### 7.3 wait_agent 改造

短期底层仍可 poll 文件，但抽象改为等事件 cursor：

```python
wait_agent(targets, since_event_seq=None, timeout=...)
```

返回：

```json
{
  "status": "changed | timeout",
  "events": [...],
  "agents": [...],
  "next_event_seq": 43
}
```

重点：不再扫描 output 判断完成，completion 以 `final_output_ready` / `turn_completed` event 为准。

### 7.4 notification 注入

定义 GA 版 contextual fragment：

```text
<ga_subagent_notification>
{
  "agent_path": "/root/researcher",
  "run_id": "agent_...",
  "status": "completed",
  "event_seq": 42,
  "summary": "...",
  "final_output_ref": "..."
}
</ga_subagent_notification>
```

注入策略：

- 后台 agent 完成/失败/关闭后，写 parent notification queue。
- 父 agent 下一轮构造 prompt 时消费未读 notifications。
- UI 可同时显示通知。
- `read_agent_result` 仍是读取完整输出权威入口。

### 7.5 与 Codex 对齐点

Codex：

```text
CollabAgentSpawnBegin/End
CollabWaitingBegin/End
CollabAgentInteractionBegin/End
CollabCloseBegin/End
SubagentNotification
```

GA 对应：

```text
agent_spawn_begin/end
agent_wait_begin/end
agent_message_begin/end
agent_close_begin/end
ga_subagent_notification
```

### 7.6 P1 测试

新增 `tests/test_subagent_event_bus.py`：

- event_seq 单调递增。
- append/read since cursor。
- notification queue 只消费一次。
- wait_agent 返回 matching event。
- timeout 不丢 cursor。
- duplicate event_id 去重。

## 8. P1：mailbox 单一协议

### 8.1 问题

当前 `followup_task` 同时写 `mailbox.jsonl` 和 `reply.txt`。这是历史兼容，不适合继续扩展。

### 8.2 方案

新增：`subagent_mailbox.py`

#### message envelope

```json
{
  "schema_version": 1,
  "message_id": "msg_...",
  "author": "/root",
  "recipient": "/root/researcher",
  "content": "...",
  "delivery_mode": "queue_only | trigger_turn",
  "trigger_turn": true,
  "priority": "normal",
  "created_at": "...",
  "consumed_at": null,
  "acknowledged_at": null,
  "reply_to": null,
  "source_tool": "followup_task"
}
```

#### send_message

```text
QueueOnly：只入队，不触发新 turn。
```

#### followup_task

```text
TriggerTurn：入队，并唤醒/触发下一轮。
```

#### 消费规则

当 worker 消费 TriggerTurn 消息时：

- 同时带上此前未消费 QueueOnly 消息。
- 按 created_at / message_id 排序。
- 所有进入本轮上下文的消息写 consumed_at。
- 写 `message_consumed` event。

### 8.3 兼容策略

- P1 初期保留 `reply.txt` fallback。
- 新测试覆盖 mailbox 主路径。
- P2 移除或只作为旧数据恢复 fallback。

### 8.4 测试

新增 `tests/test_subagent_mailbox.py`：

- queue-only 不触发。
- trigger-turn 触发。
- trigger-turn 包含之前 queue-only 消息。
- consumed_at 写入。
- malformed row 跳过。
- message_id 去重。

## 9. P1：fork policy 收敛

### 9.1 问题

默认 `fork_turns="all"` 成本高且可能泄漏不必要上下文。

### 9.2 方案

明确 fork policy：

```text
fork_turns: none | all | positive integer string
```

建议默认逐步调整：

- 兼容期：默认仍 `all`，但 state 记录 token estimate 和 warning。
- 新 schema 文档建议：复杂但边界清楚任务用 `none` 或 N。
- 后续版本可考虑默认 `last 6 turns`。

### 9.3 full fork 规则

借鉴 Codex：

- `fork_turns=all` 代表 full-history fork。
- full fork 不建议同时覆盖 agent_type/model/permission profile，除非显式允许。
- 如果允许覆盖，必须记录到 registry，避免调试混乱。

### 9.4 记录字段

```json
{
  "fork_turns": "all",
  "fork_history_count": 32,
  "fork_history_token_estimate": 12000,
  "fork_redacted": false,
  "fork_policy_warning": "full history fork may include unrelated context"
}
```

### 9.5 测试

- `fork_turns=none` 不写 `_history.json`。
- `fork_turns=3` 裁剪最近 3 turns。
- `fork_turns=all` 写全部。
- invalid fork_turns 报错。
- state 记录 count/estimate。

## 10. P2：final artifact 事件化

### 10.1 问题

`[ROUND END]` 文本 marker 不够稳。

### 10.2 方案

新增 artifact manifest：

```text
temp/subagents/runs/<run_id>/artifacts.json
```

结构：

```json
{
  "schema_version": 1,
  "artifacts": [
    {
      "artifact_id": "final_output_round_1",
      "type": "final_output",
      "path": "output1.txt",
      "sha256": "...",
      "bytes": 1234,
      "round": 1,
      "created_at": "..."
    }
  ]
}
```

`turn_completed` event 包含：

```json
{
  "final_output_ref": "final_output_round_1"
}
```

`read_agent_result` 读取 artifact ref，而不是依赖 marker。

### 10.3 兼容

- `[ROUND END]` 保留为旧 output fallback。
- 新路径优先 artifact manifest。

### 10.4 测试

- final_output_ref 生成。
- sha256 匹配。
- read_agent_result 走 artifact。
- marker 出现在普通文本中不影响 final 判断。
- artifact missing fallback 到旧逻辑并 warning。

## 11. P2：process subagent 与 workflow child 的抽象对齐

### 11.1 目标

不改 workflow 正确代码，但让两个体系逐步共享概念。

### 11.2 可共享概念

```text
AgentStatus
AgentEvent
AgentResult
ArtifactRef
TranscriptEvent
PermissionDecision
CapabilitySnapshot
```

### 11.3 边界

第一步不要移动 workflow 代码。可以新增：

```text
agent_runtime_models.py
```

但 workflow 不立即依赖它。等 process subagent 稳定后，再评估是否让 workflow 迁移。

### 11.4 测试

- process subagent event 能转换为 common event。
- workflow 原测试不变。
- common dataclass 不改变 workflow 行为。

## 12. P2：sidechain transcript 基础

### 12.1 目标

先不做完整 resume，先保存可审计 transcript。

### 12.2 文件路径

```text
temp/sessions/<session_id>/subagents/<run_id>.jsonl
temp/sessions/<session_id>/subagents/<run_id>.meta.json
```

### 12.3 transcript events

```text
metadata
request
assistant
user_followup
tool_call
tool_result
permission_decision
compact
final_output
error
```

### 12.4 借鉴 workflow child

可参考 `workflow_child_agent.py` 的 transcript events，但先复制思路，不直接改 workflow。

### 12.5 测试

- spawn 写 metadata。
- turn started/completed 写 transcript。
- followup 第二轮追加。
- tool args/results 脱敏。
- error event 写入。

## 13. P2：notification 注入父上下文

### 13.1 目标

父 agent 不需要反复 wait 才知道后台 agent 完成。

### 13.2 注入位置

在主 agent 每轮构造 prompt 或 next_prompt 时追加未读通知：

```text
[GA_SUBAGENT_NOTIFICATIONS]
<ga_subagent_notification>...</ga_subagent_notification>
[/GA_SUBAGENT_NOTIFICATIONS]
```

### 13.3 消费规则

- 每个 notification 有 id。
- 注入一次后标记 consumed。
- 如果父 agent 没有启动新 turn，通知仍保留。
- UI 可显示但不等于模型已消费。

### 13.4 测试

- completed notification 注入下一轮。
- consumed 后不重复注入。
- failed/closed notification 也注入。
- notification 不包含大 final output，只含 summary/ref。

## 14. P3：agent definition / role registry

### 14.1 目标

让 subagent 专业化不完全依赖每次 spawn 手写 prompt。

### 14.2 方案

新增：

```text
subagent_roles.py
```

配置文件可选：

```text
.ga/subagents/*.md
.ga/subagents/*.json
```

字段：

```json
{
  "name": "researcher",
  "description": "Read-only research agent",
  "when_to_use": "...",
  "system_prompt": "...",
  "permission_profile": "read_only",
  "allowed_tools": ["file_read", "load_skill"],
  "model_profile": "inherit",
  "fork_turns_default": "none"
}
```

### 14.3 与 Claude Code 区别

先做最小子集，不做 plugin/policy/hook/remote。

### 14.4 测试

- load role definition。
- spawn_agent(agent_type=...) 应用 permission/fork defaults。
- unknown role 报错或 fallback。

## 15. P3：foreground/background、worktree、实时 IPC

这些是高级能力，排在后面。

### 15.1 foreground → background

借鉴 Claude Code：

- 同步 agent 运行一段时间后可转后台。
- 保留同一 run id / transcript。

### 15.2 worktree isolation

用于并行写代码 subagent：

- `isolation="worktree"`
- 自动创建 worktree。
- 完成后保留 diff 或清理。

### 15.3 实时 IPC

在 durable event bus 稳定后，可考虑：

- local socket。
- pipe。
- multiprocessing connection。
- lightweight event server。

文件仍作为 fallback。

## 16. 工具 schema 变更计划

### 16.1 spawn_agent

新增参数：

```json
{
  "task_name": "researcher",
  "message": "...",
  "agent_type": "researcher",
  "fork_turns": "none|all|N",
  "permission_profile": "read_only",
  "allowed_tools": [],
  "denied_tools": [],
  "background": true
}
```

返回新增：

```json
{
  "agent_path": "/root/researcher",
  "run_id": "agent_...",
  "status": "started",
  "notification_hint": "wait for ga_subagent_notification or call wait_agent"
}
```

### 16.2 list_agents

新增：

```json
{
  "path_prefix": "/root/researcher",
  "include_closed": false
}
```

### 16.3 wait_agent

新增：

```json
{
  "targets": ["/root/researcher"],
  "since_event_seq": 0,
  "timeout_seconds": 30
}
```

### 16.4 read_agent_result

支持：

```json
{
  "target": "/root/researcher",
  "artifact_id": "final_output_round_1",
  "max_output_chars": 20000
}
```

### 16.5 close_agent

新增正式工具。

### 16.6 send_message / followup_task

target 推荐 canonical path。

## 17. 状态模型

### 17.1 turn_status

```text
pending
running
completed
errored
interrupted
cancelled
```

### 17.2 process_status

```text
starting
alive
waiting_reply
exited
shutdown
killed
closed
unknown
```

### 17.3 agent lifecycle status

```text
created
running
waiting_reply
completed
failed
interrupted
closed
stale
not_found
```

### 17.4 final 判定

final states：

```text
completed
failed
closed
killed
cancelled
```

waiting_reply 不算 final，因为还能 followup。

## 18. 迁移策略

### 18.1 兼容旧 task dir

读取 target 时：

1. registry by agent_path。
2. registry by run_id。
3. registry by unique task_name。
4. fallback legacy `temp/<task_name>`。

### 18.2 兼容旧 output marker

新 read path：

1. artifact manifest。
2. state final_output_path。
3. legacy output + `[ROUND END]`。

### 18.3 兼容旧 mailbox/reply

- 新 worker 优先 mailbox。
- 如果没有新 trigger message，再检查 `reply.txt`。
- deprecation warning 写 event。

### 18.4 渐进发布

环境变量 feature gate：

```text
GA_SUBAGENT_V2=1
```

或内部配置：

```python
SUBAGENT_V2_ENABLED = True
```

建议测试先覆盖 both old/new path，然后默认开启。

## 19. 测试总计划

### P0 测试

```bash
python -m unittest tests.test_subagent_agent_path
python -m unittest tests.test_subagent_registry
python -m unittest tests.test_ga_subagent_permissions
python -m unittest tests.test_ga_subagent_tools
```

### P1 测试

```bash
python -m unittest tests.test_subagent_event_bus
python -m unittest tests.test_subagent_mailbox
python -m unittest tests.test_agentmain_subagent_lifecycle
```

### P2 测试

```bash
python -m unittest tests.test_subagent_artifacts
python -m unittest tests.test_subagent_transcript
python -m unittest tests.test_agentmain_subagent_lifecycle
```

### P3 测试

```bash
python -m unittest tests.test_subagent_roles
python -m unittest tests.test_subagent_worktree
python -m unittest tests.test_subagent_ipc
```

### 回归测试

```bash
python -m unittest tests.test_subagent_manager
python -m unittest tests.test_subagent_state
python -m unittest tests.test_ga_subagent_tools
python -m unittest tests.test_workflow_child_agent
python -m unittest tests.test_workflow_permissions
python -m unittest discover -s tests
```

### 真实 LLM API 测试

只用用户指定 Terra：

```bash
GA_RUN_REAL_PROMPT_PLANNER_E2E=1 \
GA_WORKFLOW_LLM_PROFILE=terra \
GA_REAL_API_PROFILE=terra \
GA_REAL_API_CONFIG=terra \
GA_REAL_API_EXPECTED_MODEL=gpt-5.6-terra \
GA_REAL_API_EXPECTED_NAME=terra \
python tests/prompt_guided_planner_real_e2e.py
```

subagent v2 后续若增加真实 subagent E2E，也必须显式 opt-in，且同样只用 Terra 配置，除非用户授权其他 API。

## 20. 实施里程碑

### Milestone A：身份和 registry

- `subagent_agent_path.py`
- `subagent_registry.py`
- spawn/list/wait/read 支持 `agent_path` / `run_id`
- duplicate task 不覆盖

验收：

```text
可以同时 spawn 两个同名建议名的 agent，不覆盖 artifact；list_agents 能按 /root prefix 展示。
```

### Milestone B：close 和权限

- `close_agent` tool
- permission_profile 参数
- read_only/restricted_mcp/explicit_approval glue

验收：

```text
read_only subagent 无法写文件/跑代码；close_agent 返回 previous_status；root close 被拒。
```

### Milestone C：事件通知和 mailbox

- `subagent_event_bus.py`
- `subagent_mailbox.py`
- QueueOnly/TriggerTurn 单一协议
- notification queue

验收：

```text
followup_task 不再依赖 reply.txt 主路径；agent 完成后有 ga_subagent_notification。
```

### Milestone D：artifact 和 transcript

- artifact manifest
- final_output_ref
- transcript_events

验收：

```text
read_agent_result 通过 final_output_ref 读取；marker 污染不影响完成判定；可审计每轮事件。
```

### Milestone E：抽象统一和高级能力预留

- common model dataclass
- agent role registry 初版
- foreground/background/worktree 设计再评审
- `ipc_mode` 协议元数据；socket/pipe/event_server 暂回退到 durable file event bus

验收：

```text
workflow 原测试不变；process subagent 和 workflow child 的状态/事件术语开始一致；role/worktree/ipc fallback 有最小可测骨架。
```

## 21. 风险和缓解

### 风险 1：改动过大影响 workflow

缓解：

- 初期不改 workflow 主流程。
- 只新增 subagent 专用模块。
- 如果抽 shared model，先证明 workflow tests 不变。

### 风险 2：Windows 文件锁 / 并发写 registry

缓解：

- 复用 `atomic_write_json`。
- registry 更新加 file lock 或 retry。
- JSONL append 尽量单行原子写。

### 风险 3：旧 artifact 兼容

缓解：

- fallback legacy task dir。
- fallback `[ROUND END]`。
- fallback `reply.txt`。
- migration warning 不阻断。

### 风险 4：权限策略误杀工具

缓解：

- 默认 `inherit-current-permissions`。
- read_only/restricted_mcp 先覆盖核心工具。
- approval_required 明确返回给模型。

### 风险 5：事件通知重复注入

缓解：

- notification id。
- consumed cursor。
- exactly-once-to-model，at-least-once-on-disk。

## 22. 最终建议

GA subagent 优化应按以下优先级执行：

```text
P0：AgentPath + registry + run id
P0：permission_profile + allowed/denied tools
P0：close_agent 生命周期
P1：Codex-style event notification / SubagentEventBus
P1：mailbox 单一协议，QueueOnly / TriggerTurn
P1：fork policy 收敛
P2：final artifact event 化
P2：sidechain transcript 基础
P2：workflow child/process subagent 抽象对齐，但不改 workflow 正确主流程
P3：agent definition / role registry
P3：foreground-background / worktree / realtime IPC
```

第一期最小闭环建议：

```text
AgentPath + registry + run_id
  → list_agents(path_prefix)
  → close_agent(previous_status)
  → permission_profile=read_only
  → SubagentEventBus event_seq
  → mailbox QueueOnly/TriggerTurn
```

做完这组，GA 文件/进程型 subagent 就会从“轻量后台进程协议”升级成“可控、可关闭、可通知、有权限边界的 subagent control plane”。
