# GA Subagent 优化方案：借鉴 Codex 控制面设计

## 0. 目标

把 GA 当前的 subagent 从“父进程手写 PID + 轮询 output 文本 + 猜测 `[ROUND END]`”升级为一套轻量、可测试、可逐步落地的 subagent 控制面。

核心目标：

- 不重写 GA 主运行时，不引入复杂 IPC；
- 保留当前 `python agentmain.py --task ...` 子进程模式；
- 保持旧的 `input.txt`、`output*.txt`、`reply.txt`、`_stop`、`_intervene`、`_keyinfo` 协议兼容；
- 新增结构化状态、事件、邮箱和 manager API；
- 让父进程能准确区分“任务轮次已完成”和“子进程仍存活等待 reply”；
- 为后续 UI、SOP、自动调度和多层 subagent 提供稳定接口。

本方案不是要复刻 Codex 的 Rust async runtime，而是借鉴它的控制面语义：registry、path、status、wait、mailbox、close。

## 1. 当前 GA 机制

当前实现主要分散在三个位置：

- `agentmain.py`
  - `--task` 进入文件 IO 任务模式；
  - 不带 `--nobg` 时后台启动自身，并打印 PID；
  - 带 `--nobg` 时实际执行 worker；
  - `--input` 会创建 `temp/{task}/input.txt` 并清理旧 `output*.txt`；
  - worker 每轮写 `output{nround}.txt`；
  - 轮完成后写入最终文本并追加 `[ROUND END]`；
  - 然后等待 `reply.txt`，最多 10 分钟；
  - 读取 `_history.json` 支持手工 fork 历史。
- `ga.py`
  - `consume_file()` 负责读后删除文件；
  - `GenericAgentHandler._get_anchor_prompt()` 消费 `_keyinfo`、`_intervene`；
  - `_stop` 在 `agentmain.py` 和工具执行中被检查。
- `memory/subagent.md`、`memory/supervisor_sop.md`
  - 记录启动、轮询、干预、map-reduce、plan mode 等 SOP。

当前机制能用，但关键语义混在一起：

- `[ROUND END]` 是“本轮完成”，不是“进程退出”；
- PID alive 可能只是 worker 等待 `reply.txt`，不是任务未完成；
- PID dead 可能是正常退出、被 kill、异常退出或启动失败；
- `output*.txt` 是人读的快照，不是可靠事件日志；
- `memory/subagent.md` 写的是 output append，但 `agentmain.py` 实际是覆盖写；
- 父进程 cleanup 前没有统一的逐 agent 状态复查入口；
- 没有标准 `list/wait/send/followup/close` API，调用者只能临时拼文件和进程逻辑。

法国/巴西世界杯案例暴露的核心问题就是这个：巴西 worker 没有写出最终 `[ROUND END]`，主进程接管合理；法国 worker 后续写出 `[ROUND END]`，但父进程没有在 cleanup 前以 worker 粒度重新收集状态，最终按 PID 统一终止，语义不清。

## 2. Codex 可借鉴点

Codex 的实现不适合直接照搬，但以下设计适合 GA：

### 2.1 AgentRegistry

Codex 为每个 agent 维护 metadata：

- agent id；
- canonical path；
- nickname / role；
- last task message；
- live agents 列表；
- spawn depth / max agents 限制。

GA 可借鉴为 `temp/subagents/registry.json` 或 session scoped registry，用来回答：

- 当前有哪些 subagent；
- 属于哪个 root session；
- task name 对应哪个目录和 PID；
- last task message 是什么；
- 当前状态文件在哪里；
- 关闭或 cleanup 时应该处理哪些 agent。

### 2.2 AgentStatus

Codex 的 `AgentStatus` 由事件派生，常见状态包括：

- `pending_init`
- `running`
- `interrupted`
- `completed(final_message)`
- `errored(error)`
- `shutdown`
- `not_found`

GA 应拆出两类状态：

- `turn_status`：本轮任务是否完成；
- `process_status`：worker 进程当前怎样。

这能准确表达“法国已完成本轮，但进程仍 waiting_reply”。

### 2.3 wait_agent

Codex v2 的 `wait_agent` 等待 mailbox/status 更新，不把完整 final content 直接塞进工具返回值。

GA 可借鉴为：

- `wait_agents()` 返回哪个 agent 有状态变化；
- final content 仍通过 output path / final output path 读取；
- 避免每次 wait 都读全文 output；
- 避免 partial output 和 final output 混淆。

### 2.4 send_message 与 followup_task

Codex 区分：

- `send_message`：只入队，不触发新 turn；
- `followup_task`：入队并触发下一轮。

GA 当前 `reply.txt` 是单槽触发文件。短期可以兼容，但需要引入 `mailbox.jsonl`：

- 普通消息入 mailbox；
- followup 消息再落到 `reply.txt` 触发下一轮；
- `_intervene` 可视为高优先级控制消息，也写入事件。

### 2.5 close_agent

Codex `close_agent` 关闭前读取 previous status，并返回给调用者。

GA 最需要借鉴这一点。`close_agent(task)` 不能只是 kill PID，至少要：

1. 读取 `state.json`；
2. 检查 output 是否已有 `[ROUND END]`；
3. 保存 final output 路径和摘要；
4. 若 worker 正在等待 reply，优先 graceful stop；
5. 必要时 terminate；
6. 返回 previous status。

## 3. 推荐总体架构

保持进程模型不变，新增一个轻量控制面：

```text
agentmain.py --task
  ├─ 继续负责真实 worker 执行
  ├─ 写 output*.txt 给人读
  ├─ 写 state.json 给程序判断状态
  ├─ 写 events.jsonl 给程序消费事件
  └─ 消费 mailbox/reply/intervene/stop

subagent_manager.py
  ├─ spawn_agent()
  ├─ list_agents()
  ├─ read_agent()
  ├─ wait_agents()
  ├─ send_message()
  ├─ followup_task()
  └─ close_agent()

temp/subagents/registry.json
  └─ root/session/task/path/PID/state path 索引

temp/{task}/
  ├─ input.txt
  ├─ output.txt / output1.txt ...
  ├─ state.json
  ├─ events.jsonl
  ├─ mailbox.jsonl
  ├─ reply.txt
  ├─ stdout.log
  └─ stderr.log
```

设计原则：

- `output*.txt` 继续给人和旧 SOP 使用；
- `state.json` 是当前状态的权威快照；
- `events.jsonl` 是状态变化的权威事件流；
- `registry.json` 是父进程查找 agent 的入口；
- manager 是唯一推荐给父进程和 UI 使用的 API；
- 旧文件协议继续可用，但新代码不再直接解析 `output*.txt` 来判断生命周期。

## 4. 数据模型

### 4.1 `state.json`

每个 `temp/{task_name}/state.json` 代表一个 subagent 的当前快照。

建议字段：

```json
{
  "schema_version": 1,
  "task_name": "wc_france_history",
  "agent_path": "/root/wc_france_history",
  "parent_session_id": "session_4a4cebb9c14a49b68d9acdb4c4f788fd",
  "pid": 22596,
  "round": 0,
  "turn_status": "completed",
  "process_status": "waiting_reply",
  "started_at": "2026-07-13T10:20:00+08:00",
  "updated_at": "2026-07-13T10:24:49+08:00",
  "last_round_started_at": "2026-07-13T10:20:01+08:00",
  "last_round_end_at": "2026-07-13T10:24:49+08:00",
  "input_path": "temp/wc_france_history/input.txt",
  "output_path": "temp/wc_france_history/output.txt",
  "final_output_path": "temp/wc_france_history/output.txt",
  "final_output_sha256": "hex-string-or-null",
  "last_message": "已完成法国历届世界杯成绩表",
  "last_error": null,
  "close_reason": null
}
```

`turn_status` 取值：

- `pending`
- `running`
- `completed`
- `interrupted`
- `errored`

`process_status` 取值：

- `starting`
- `alive`
- `waiting_reply`
- `exited`
- `shutdown`
- `killed`
- `not_found`

关键约束：

- `turn_status=completed` 不要求 `process_status=exited`；
- `process_status=alive` 不能覆盖 `turn_status=completed`；
- `final_output_path` 只有确认 `[ROUND END]` 或 `turn_completed` 事件后才能写；
- JSON 必须原子写：先写临时文件，再 `os.replace()`。

### 4.2 `events.jsonl`

`events.jsonl` 记录生命周期事件。程序等事件，UI 显示事件，排查问题也读事件。

示例：

```jsonl
{"schema_version":1,"type":"agent_started","task_name":"wc_france_history","pid":22596,"ts":"2026-07-13T10:20:00+08:00"}
{"schema_version":1,"type":"turn_started","task_name":"wc_france_history","round":0,"ts":"2026-07-13T10:20:01+08:00"}
{"schema_version":1,"type":"output_snapshot","task_name":"wc_france_history","round":0,"output_path":"temp/wc_france_history/output.txt","bytes":2048,"ts":"2026-07-13T10:22:00+08:00"}
{"schema_version":1,"type":"turn_completed","task_name":"wc_france_history","round":0,"output_path":"temp/wc_france_history/output.txt","sha256":"...","ts":"2026-07-13T10:24:49+08:00"}
{"schema_version":1,"type":"agent_waiting_reply","task_name":"wc_france_history","round":0,"ts":"2026-07-13T10:24:49+08:00"}
{"schema_version":1,"type":"agent_closed","task_name":"wc_france_history","previous_turn_status":"completed","previous_process_status":"waiting_reply","reason":"parent_cleanup","ts":"2026-07-13T10:25:30+08:00"}
```

初期不需要记录每个 token delta。只记录关键事件即可：

- `agent_started`
- `turn_started`
- `output_snapshot`
- `turn_completed`
- `agent_waiting_reply`
- `message_queued`
- `message_consumed`
- `intervention_queued`
- `stop_requested`
- `agent_exited`
- `agent_closed`
- `agent_error`

### 4.3 `mailbox.jsonl`

`mailbox.jsonl` 解决 `reply.txt` 单槽问题。每行一条消息：

```json
{
  "schema_version": 1,
  "id": "msg_20260713_102530_0001",
  "author": "/root",
  "recipient": "/root/wc_france_history",
  "content": "继续补充 1930-2022 的数据来源",
  "trigger_turn": true,
  "priority": "normal",
  "created_at": "2026-07-13T10:25:30+08:00",
  "consumed_at": null
}
```

兼容策略：

- `followup_task()` 写 `mailbox.jsonl`，同时写 `reply.txt` 触发当前旧 worker 继续；
- `send_message()` 只写 `mailbox.jsonl`，不写 `reply.txt`；
- `_intervene` 暂时保留，但 manager 也写一条 `intervention_queued` event；
- 后续再让 `agentmain.py` 优先消费 mailbox，而不是只看 `reply.txt`。

### 4.4 `registry.json`

建议先放在 `temp/subagents/registry.json`。

示例：

```json
{
  "schema_version": 1,
  "updated_at": "2026-07-13T10:25:30+08:00",
  "agents": {
    "/root/wc_france_history": {
      "task_name": "wc_france_history",
      "agent_path": "/root/wc_france_history",
      "parent_path": "/root",
      "parent_session_id": "session_4a4cebb9c14a49b68d9acdb4c4f788fd",
      "pid": 22596,
      "task_dir": "temp/wc_france_history",
      "state_path": "temp/wc_france_history/state.json",
      "last_task_message": "调研法国国家队历届世界杯成绩",
      "created_at": "2026-07-13T10:20:00+08:00",
      "closed_at": null
    }
  }
}
```

路径规范：

- root 固定为 `/root`；
- 子 agent path 为 `/root/{task_name}`；
- `task_name` 只允许小写字母、数字、下划线；
- 后续多层 subagent 可扩展为 `/root/research/france`；
- registry 应可从 state 文件重建，避免单点损坏。

## 5. `subagent_manager.py` 设计

新增文件：`subagent_manager.py`。

它不负责模型调用，只负责文件协议、状态解析、进程检查和兼容封装。

### 5.1 推荐公开 API

```python
def spawn_agent(task_name, message, *, llm_no=0, verbose=False, fork_turns="none", parent_session_id=None):
    """启动 subagent，写 registry/state，返回 AgentHandle。"""

def list_agents(path_prefix=None, *, include_closed=False):
    """读取 registry 和 state，返回 AgentState 列表。"""

def read_agent(target):
    """按 task_name 或 agent_path 读取单个 AgentState。"""

def wait_agents(targets=None, *, timeout_s=30, poll_interval_s=0.5, since_event_offsets=None):
    """等待任意目标 agent 出现状态/event/mailbox 更新。"""

def send_message(target, message, *, author="/root"):
    """只入 mailbox，不触发下一轮。"""

def followup_task(target, message, *, author="/root"):
    """写 mailbox，并通过 reply.txt 兼容触发下一轮。"""

def close_agent(target, *, reason="parent_cleanup", grace_s=2.0):
    """关闭前复查状态，保存 previous_status，再 graceful/terminate。"""
```

### 5.2 返回对象

可以先用 `dataclasses`，避免引入额外依赖。

```python
@dataclass
class AgentState:
    task_name: str
    agent_path: str
    pid: int | None
    task_dir: str
    turn_status: str
    process_status: str
    round: int
    output_path: str | None
    final_output_path: str | None
    updated_at: str | None
    last_message: str | None
    last_error: str | None

@dataclass
class WaitResult:
    timed_out: bool
    changed_agents: list[AgentState]
    message: str

@dataclass
class CloseResult:
    target: str
    previous_state: AgentState
    closed_state: AgentState
    final_output_path: str | None
```

### 5.3 状态刷新规则

`read_agent()` 每次读取 state 后都应做轻量 refresh：

1. 检查 PID 是否存在；
2. 检查 `output{round}.txt` 是否含 `[ROUND END]`；
3. 如 state 缺失但 output 已完成，则从 output 补出 `turn_status=completed`；
4. 如 PID dead 且无 `[ROUND END]`，标记 `process_status=exited`，`turn_status` 保持 `running` 或转 `errored`，具体看 stderr；
5. 如 PID alive 且 `[ROUND END]` 已存在，标记 `turn_status=completed`、`process_status=waiting_reply`；
6. refresh 后必要时原子写回 state，并追加 event。

这一步是兼容旧 worker 和历史目录的关键。

## 6. `agentmain.py` 改造点

`agentmain.py` 只做必要改造，不把 manager 逻辑塞回主文件。

### 6.1 后台启动阶段

当前逻辑：

- 创建 `temp/{task}`；
- `subprocess.Popen(...)`；
- stdout/stderr 写日志；
- print PID。

建议新增：

- 写初始 `state.json`：`process_status=starting`；
- Popen 成功后写 `pid`、`process_status=alive`；
- 写 `agent_started` event；
- 注册 `registry.json`；
- 启动失败时写 `agent_error` event。

### 6.2 worker 执行阶段

当前逻辑：

- 读取 `input.txt`；
- `agent.put_task(raw, source='task')`；
- 中间随机写一次 output snapshot；
- 收到 `done` 后写 `[ROUND END]`；
- 等 `reply.txt`。

建议新增：

- 读取 input 前写 `turn_started`；
- 写中间 output 时更新 `state.updated_at`、`output_path`，追加 `output_snapshot` event；
- 写 `[ROUND END]` 后：
  - 计算 output sha256；
  - 写 `turn_status=completed`；
  - 写 `final_output_path`；
  - 写 `turn_completed` event；
- 进入 reply 等待前写 `process_status=waiting_reply` 和 `agent_waiting_reply` event；
- 收到 `reply.txt` 或 mailbox trigger 后：
  - 写 `message_consumed` event；
  - `round += 1`；
  - `turn_status=running`；
  - `process_status=alive`；
- 超时退出时写 `process_status=exited` 和 `agent_exited` event；
- 捕获异常时写 `turn_status=errored`、`last_error` 和 `agent_error` event，再抛出或退出。

### 6.3 干预文件兼容

当前 `_intervene` 和 `_keyinfo` 在 `ga.py` 消费。

短期策略：

- 保留原逻辑；
- manager 写 `_intervene` / `_keyinfo` 时同步写 event；
- 后续可将 `_intervene` 统一为 mailbox 高优先级消息。

### 6.4 输出文件兼容

不要立刻修改 `output*.txt` 的命名和内容格式。

但需要修正文档语义：

- 当前实现是覆盖写，不是 append；
- 新代码不能把 output 当事件日志；
- 判断 final 以 state/event 为主，`[ROUND END]` 仅作为兼容信号。

## 7. 分阶段落地路线

### Phase 1：状态层 MVP

目标：不改变用户交互，只补结构化状态。

改动：

- 新增 `subagent_state.py`
  - `atomic_write_json()`
  - `read_json_or_none()`
  - `append_jsonl_event()`
  - `sha256_file()`
  - `now_iso()`
- 修改 `agentmain.py`
  - task 启动写 `state.json`；
  - turn start / complete / waiting reply / exit 写 state；
  - 写 `events.jsonl`；
  - 保持旧 output/reply/intervene 文件不变。
- 新增 `tests/test_subagent_state.py`
  - 测原子写；
  - 测 `[ROUND END]` 后 state 为 completed + waiting_reply；
  - 测异常状态可写入。

验收：

- 旧 `python agentmain.py --task name --input "..."` 行为不变；
- `temp/{task}/state.json` 可读；
- `temp/{task}/events.jsonl` 有 started/turn_started/turn_completed/waiting_reply/exited；
- 不需要真实 LLM 的测试能通过。

### Phase 2：只读 manager

目标：先让父进程能稳定读取状态，不负责启动/关闭。

改动：

- 新增 `subagent_manager.py`
  - `read_agent()`
  - `list_agents()`
  - `refresh_agent_state()`
  - `discover_agents_from_temp()`
- 支持没有 registry 的旧 task 目录：
  - 扫描 `temp/*/output*.txt`；
  - 根据 `[ROUND END]` 和 PID/state 推导状态；
  - 推导结果写回 state。
- 新增 `tests/test_subagent_manager_read.py`
  - 构造临时 task 目录；
  - output 有 `[ROUND END]` 且 fake PID alive 时，返回 completed + waiting_reply；
  - output 无 `[ROUND END]` 且进程不存在时，返回 running/exited 或 errored/exited；
  - state 缺失时能兼容推导。

验收：

- manager 能解释今天法国/巴西这种目录；
- 读状态不依赖真实模型；
- 父进程后续可以不直接 grep output。

### Phase 3：registry + spawn_agent

目标：把“启动 subagent”从 SOP 命令升级为 API。

改动：

- `subagent_manager.py` 新增：
  - `spawn_agent()`
  - `register_agent()`
  - `load_registry()`
  - `save_registry()`
  - `resolve_target()`
- `spawn_agent()` 内部仍调用：
  - `python agentmain.py --task {task_name} --input ...`
- 校验 `task_name`：
  - 只允许 `[a-z0-9_]+`；
  - 禁止路径穿越；
  - 目录必须落在 `temp/` 下。
- 新增 `tests/test_subagent_manager_spawn.py`
  - mock `subprocess.Popen`；
  - 验证 registry 写入；
  - 验证非法 task name 被拒绝；
  - 验证 fork_turns 参数合法性。

验收：

- 新接口可启动 worker；
- registry 可列出 live agent；
- 旧命令行启动仍可用。

### Phase 4：wait_agents

目标：替代“无脑 sleep + 反复读 output 全文”。

改动：

- `wait_agents(targets=None, timeout_s=30, poll_interval_s=0.5, since_event_offsets=None)`：
  - 读取目标 state；
  - 记录 events 文件大小/mtime；
  - 等待状态变化或 event 增长；
  - 超时返回 `timed_out=True`；
  - 返回 changed agents，不直接返回完整 output。
- 支持“任意 live agent 变化”模式，类似 Codex mailbox wait。
- 新增 `tests/test_subagent_manager_wait.py`
  - 后台线程追加 event；
  - wait 能提前返回；
  - 无 event 时超时；
  - completed agent 不被 running PID 覆盖。

验收：

- 父进程可以等事件；
- UI 可以等事件；
- 大 output 不会被反复塞入上下文。

### Phase 5：close_agent

目标：修复 cleanup 语义。

改动：

- `close_agent(target, reason="parent_cleanup", grace_s=2.0)`：
  1. `read_agent()` 刷新 previous state；
  2. 如果 output 已 `[ROUND END]`，补写 final output 信息；
  3. 写 `_stop` 请求 graceful stop；
  4. 若仍 alive，调用 terminate；
  5. 超时后 kill；
  6. 写 `agent_closed` event；
  7. 更新 state；
  8. 返回 `CloseResult(previous_state, closed_state)`。
- 新增 `tests/test_subagent_manager_close.py`
  - previous completed/waiting_reply 时不丢 final_output_path；
  - running agent close 后记录 interrupted/shutdown；
  - not_found 返回可解释状态；
  - close 不允许删除 output 和 logs。

验收：

- cleanup 前状态可追溯；
- 法国这种 completed/waiting_reply 不再被误报为 unfinished；
- 巴西这种 running/stuck 可以被明确 close 为 interrupted/shutdown。

### Phase 6：mailbox + send_message/followup_task

目标：替代单槽 `reply.txt`，但保持兼容。

改动：

- `subagent_manager.py` 新增：
  - `send_message()`
  - `followup_task()`
- `followup_task()`：
  - 写 mailbox；
  - 写 `reply.txt` 兼容触发；
  - 写 `message_queued` event。
- `send_message()`：
  - 只写 mailbox；
  - 不触发旧 worker 下一轮。
- 修改 `agentmain.py`：
  - 等待 reply 时优先检查 mailbox 中 `trigger_turn=true` 且未消费消息；
  - 兼容继续检查 `reply.txt`；
  - 消费后写 `message_consumed` event。
- 新增 `tests/test_subagent_mailbox.py`
  - send 不创建 reply；
  - followup 创建 reply；
  - mailbox 每条消息有 id/author/recipient/trigger_turn；
  - 消费消息后可标记 consumed。

验收：

- 可以区分普通消息和下一轮任务；
- 不再因为 `reply.txt` 覆盖导致消息丢失；
- 旧 SOP 写 `reply.txt` 仍然可用。

### Phase 7：SOP 和 UI 接入

目标：让人和 UI 默认使用 manager，而不是手写文件轮询。

改动：

- 更新 `memory/subagent.md`
  - 说明 output 是 snapshot，不是 append；
  - 推荐使用 manager API；
  - 标明 `[ROUND END]` 是兼容标记；
  - 增加 state/events/mailbox 说明。
- 更新 `memory/supervisor_sop.md`
  - 监控循环改为 `wait_agents()`；
  - cleanup 改为 `close_agent()`；
  - 干预优先使用 manager API。
- UI 后续可读取：
  - `list_agents()` 展示 agent tree；
  - `state.json` 展示状态；
  - `events.jsonl` 展示 timeline；
  - output 仍作为详情文本。

验收：

- 文档和实现一致；
- 后续主进程/前端无需直接猜 `[ROUND END]`。

### Phase 8：fork_turns 和多层 path

目标：补齐 Codex 风格的上下文继承和层级。

改动：

- `spawn_agent(..., fork_turns="none|all|N")`
  - `none`：不写 `_history.json`；
  - `all`：写完整 history；
  - `N`：写最近 N 轮；
- agent path 支持多层：
  - `/root/research/france`
  - `/root/research/brazil`
- registry 加：
  - `parent_path`
  - `depth`
  - `max_depth`
  - `max_agents`
- 新增测试：
  - fork_turns 参数解析；
  - illegal depth 拒绝；
  - path resolve。

验收：

- map-reduce 和多层 supervisor 有正式路径；
- 不再依赖手工 `_history.json`。

## 8. 状态机

建议实现以下状态流：

```text
pending / starting
  -> running / alive
  -> completed / waiting_reply
  -> running / alive             # 收到 followup/reply 后下一轮
  -> completed / waiting_reply
  -> completed / exited          # reply timeout 正常退出

running / alive
  -> interrupted / shutdown      # close_agent graceful stop
  -> interrupted / killed        # terminate/kill

running / alive
  -> errored / exited            # worker 异常
```

关键判定：

- `completed/waiting_reply` 是正常状态，不是 stuck；
- `running/alive` 且长时间无 event 才可能是 stuck；
- `exited` 不等于 `completed`；
- `closed` 不应该抹掉 previous state。

## 9. 测试策略

测试必须避免真实 API 和真实 LLM。

推荐测试层次：

### 9.1 纯文件测试

覆盖：

- 原子写 JSON；
- append JSONL；
- sha256；
- state 读写；
- registry 读写；
- mailbox 写入和消费。

### 9.2 manager 状态推导测试

用 `tempfile.TemporaryDirectory()` 构造：

- 有 `[ROUND END]` 的 output；
- 无 `[ROUND END]` 的 partial output；
- 缺 state 的旧目录；
- 损坏 state；
- stderr 有异常文本；
- fake PID alive/dead。

进程检查函数应可注入：

```python
SubagentManager(process_exists=lambda pid: pid == 1234)
```

不要在测试里真的启动 LLM worker。

### 9.3 agentmain 轻量集成测试

只测 CLI 文件行为，不测真实模型：

- 可以把 `GenericAgent` 或 `put_task` mock 成固定输出；
- 验证 `[ROUND END]`、state、events 一致；
- 验证 reply timeout 后写 exited；
- 验证 followup 进入下一轮。

### 9.4 回归场景测试

用今天的法国/巴西问题抽象成测试：

1. `france`：
   - output 有 `[ROUND END]`；
   - PID alive；
   - manager 返回 `turn_status=completed`、`process_status=waiting_reply`；
   - `close_agent` previous_state 仍是 completed。
2. `brazil`：
   - output 无 `[ROUND END]`；
   - PID alive 但 event 长时间无更新；
   - manager 返回 running/alive；
   - 父进程可以选择接管或 close；
   - 不把 partial output 当 final。

## 10. 兼容与迁移

必须兼容：

- 直接命令：`python agentmain.py --task {name} --input "..."`
- 旧目录：只有 `output*.txt`，没有 state/events/registry；
- 旧 SOP：手工写 `reply.txt`、`_intervene`、`_keyinfo`；
- verbose output；
- 当前 `temp/{task}` 目录结构。

不建议第一阶段做：

- 引入数据库；
- 引入 asyncio IPC；
- 改写 `GenericAgent` 主循环；
- 改变 output 文件命名；
- 删除 `_intervene` / `_keyinfo`；
- 把所有历史 session 迁移成新格式。

迁移方式：

1. 新 manager 先能读旧目录；
2. `agentmain.py` 新运行写新 state/events；
3. SOP 改为推荐新 API，但保留旧文件用法；
4. UI 和主进程逐步切到 manager；
5. 一段时间后再减少对直接 grep output 的依赖。

## 11. 文件清单

建议新增：

- `subagent_state.py`
  - 低层文件协议工具；
  - 不依赖 `GenericAgent`；
  - 可被 agentmain 和 manager 共用。
- `subagent_manager.py`
  - 高层 agent 控制 API；
  - 管理 registry、state refresh、wait、close、message。
- `tests/test_subagent_state.py`
- `tests/test_subagent_manager_read.py`
- `tests/test_subagent_manager_spawn.py`
- `tests/test_subagent_manager_wait.py`
- `tests/test_subagent_manager_close.py`
- `tests/test_subagent_mailbox.py`

建议修改：

- `agentmain.py`
  - task mode 写 state/events；
  - 后台启动注册 registry；
  - reply 等待兼容 mailbox。
- `ga.py`
  - 保留 `_intervene` / `_keyinfo` 消费；
  - 可选增加事件记录 hook，但不要第一阶段强依赖。
- `memory/subagent.md`
  - 修正 output append/overwrite 描述；
  - 增加 state/events/mailbox/manager 推荐流程。
- `memory/supervisor_sop.md`
  - 监控/干预/cleanup 改用 manager 语义。

## 12. 建议实现顺序

按这个顺序做，风险最低：

1. `subagent_state.py` + 单测；
2. `agentmain.py` 写 state/events，但不改旧交互；
3. `subagent_manager.py` 只读 state/output，兼容旧目录；
4. `close_agent()`，解决 cleanup 误判；
5. `wait_agents()`，替代父进程轮询；
6. `spawn_agent()` + registry；
7. mailbox + `send_message()` / `followup_task()`；
8. SOP 文档更新；
9. UI/主进程接入 manager；
10. fork_turns + 多层 path。

每一步都应单独 commit，并带对应 unittest。

## 13. 最小可交付版本

如果只做一个 MVP，建议包含：

- `state.json`
- `events.jsonl`
- `subagent_manager.read_agent()`
- `subagent_manager.close_agent()`
- `agentmain.py` 在 `[ROUND END]` 后写 `turn_status=completed`、`process_status=waiting_reply`
- 回归测试：completed 但 PID alive 不被误判为 running

这个 MVP 就能覆盖法国/巴西案例里的主要问题。

## 14. 验收标准

优化完成后至少满足：

1. worker 写出 `[ROUND END]` 后仍等待 `reply.txt`，manager 返回 completed/waiting_reply。
2. worker 没写 `[ROUND END]` 但 PID alive，manager 返回 running/alive，不把 partial output 当 final。
3. `close_agent()` 返回 previous_state，且 previous_state 不因 kill 被覆盖。
4. cleanup 前可以逐 agent 复查状态，只关闭未完成或需要关闭的 agent。
5. `wait_agents()` 能在 event/state 变化后提前返回，超时才 timed_out。
6. `send_message()` 不触发下一轮，`followup_task()` 触发下一轮。
7. 旧 `reply.txt`、`_intervene`、`_keyinfo` 仍然可用。
8. `memory/subagent.md` 与真实实现一致，不再说 output append。
9. 全部新增测试可用 `python -m unittest discover -s tests` 运行。

## 15. 与 Codex 的对应关系

| Codex 机制 | GA 对应方案 | 说明 |
|---|---|---|
| `AgentControl` | `subagent_manager.py` | 轻量控制面，不管理模型 runtime |
| `AgentRegistry` | `temp/subagents/registry.json` | path、PID、task_dir、state_path 索引 |
| `AgentStatus` | `state.json` 的 `turn_status` + `process_status` | 避免完成状态和进程状态混淆 |
| `spawn_agent` | `subagent_manager.spawn_agent()` | 内部仍调用 `agentmain.py --task` |
| `list_agents` | `subagent_manager.list_agents()` | 读取 registry + state refresh |
| `wait_agent` | `subagent_manager.wait_agents()` | 等 state/event/mailbox 变化 |
| `send_message` | `subagent_manager.send_message()` | 只写 mailbox |
| `followup_task` | `subagent_manager.followup_task()` | 写 mailbox + reply.txt |
| `close_agent` | `subagent_manager.close_agent()` | 返回 previous_state，保留 final output |
| canonical path | `/root/{task_name}` | 后续扩展多层 path |

## 16. 主要风险

### 风险 1：状态文件和 output 不一致

处理方式：

- 写 final output 后再写 state；
- state 写入使用原子替换；
- `read_agent()` 每次 refresh 时用 `[ROUND END]` 做兼容修正；
- events 只 append，不覆盖。

### 风险 2：旧目录没有 state

处理方式：

- manager 支持从 output/stdout/stderr/PID 推导；
- 推导出的状态写回 state；
- registry 可从 `temp/*` 重建。

### 风险 3：Windows 文件占用

处理方式：

- 读文件使用 `errors='replace'`；
- JSON 写入先写同目录临时文件，再 `os.replace()`；
- close 时先 graceful，再 terminate，最后 kill；
- 不删除日志和 output。

### 风险 4：manager 变成第二个复杂 runtime

处理方式：

- manager 不调用 LLM；
- manager 不理解业务内容；
- manager 只处理文件协议、状态和进程；
- 真正任务执行仍在 `GenericAgent`。

### 风险 5：SOP 与代码继续漂移

处理方式：

- 改代码时同步改 `memory/subagent.md`；
- 新增测试覆盖 SOP 中承诺的关键语义；
- 文档明确 output 是 snapshot，不是事件流。

## 17. 后续实现建议

第一轮实现建议只做 Phase 1 + Phase 2 + Phase 5 的最小闭环：

- 先让 worker 写 state/events；
- 再让 manager 能读旧目录和新 state；
- 最后实现 close_agent previous_state。

这三步完成后，GA 就能解决目前 subagent 最大的问题：父进程不再把“完成但等待 reply”的 worker 当成“未完成”，cleanup 也不会再丢掉已经完成的子任务结果。
