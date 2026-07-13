# GA Subagent 启动与执行记录调研

**调研日期：** 2026-07-13  
**范围：** GenericAgent 当前以 `agentmain.py --task` 启动的后台 subagent；结合当天“法国、阿根廷历次世界杯成绩”任务的实际工件复盘。  
**目的：** 为后续优化 subagent 的调度、状态、可观测性与结果交付提供事实基线。本报告只描述当前实现与一次真实运行，不将单次运行外推为所有场景的结论。

## 1. 结论摘要

GA 当前的 `--task` 模式能够以独立 Python 进程并行启动多个 subagent，并用任务目录交换输入、输出和控制信号。2026-07-13 的实际运行中，法国与阿根廷两个任务在约 1 秒内先后启动，说明基础并行启动链路工作正常。

但本次两个任务的收敛结果不同：

| 任务 | 是否启动 | 是否产出最终结果 | 实际处理方式 |
|---|---:|---:|---|
| `wc_argentina_results` | 是 | 是 | subagent 在 3 个 LLM 回合后写入完整答案和 `[ROUND END]`。 |
| `wc_france_results` | 是 | 否 | subagent 进行到第 6 个回合的检索/核验阶段，未写入最终答案或 `[ROUND END]`；主 agent 改为自行补查、汇总，并通过 `_stop` 请求其停止。 |

因此，本次运行可确认“**已启动两个 subagent 并行调研**”，但不能确认“**两个 subagent 都独立完成了调研**”。现有机制的主要短板不在于创建进程，而在于完成状态、超时/中断原因、主从交接和可观测性没有形成统一且机器可读的模型。

## 2. 当前实现链路

### 2.1 启动入口

主 agent 通过以下形式发起一个子任务：

```text
python agentmain.py --task <task_name> --input <task_prompt>
```

当 `--task` 存在且未指定 `--nobg` 时，`agentmain.py` 会：

1. 建立任务目录 `temp/<task_name>/`；
2. 用 `subprocess.Popen` 再次执行同一脚本，并追加 `--nobg`；
3. 将子进程标准输出和标准错误重定向到任务目录内的 `stdout.log`、`stderr.log`；
4. 立即向调用方输出子进程 PID 并退出。

对应实现见 [`agentmain.py`](../agentmain.py) 的 `args.task and not args.nobg` 分支。

### 2.2 子任务执行与文件协议

带 `--nobg` 的进程成为实际执行者。它读取 `temp/<task_name>/input.txt`，以 `source='task'` 将任务投递给本地 agent，并等待队列返回完成项。

当前目录协议如下：

| 文件 | 写入者 | 作用 | 当前语义问题 |
|---|---|---|---|
| `input.txt` | 主 agent / 启动命令 | 初始任务文本 | 无任务元数据、创建时间、父任务 ID 或配置快照。 |
| `output.txt` | subagent | 最终轮的回答，成功时末尾追加 `[ROUND END]` | 既保存过程文本又充当完成信号，难以可靠解析。 |
| `output<N>.txt` | subagent | 多轮交互中的后续轮次输出 | 文件序号规则不直观，缺少轮次状态。 |
| `stdout.log` | 子进程 | Python 进程的标准输出 | 本次均为空，不能作为进度日志。 |
| `stderr.log` | 子进程 | Python 进程的标准错误 | 本次均为空，不能区分“正常无错误”和“尚无信息”。 |
| `_stop` | 主 agent | 请求子任务停止 | 仅是文件信号，没有确认消费、停止原因、停止时间或最终状态。 |
| `reply.txt` | 主 agent | 给已完成子任务的后续指令 | 子任务完成一轮后最多等待约 10 分钟。 |
| `_history.json` | 可选 | 恢复子任务模型历史 | 缺少版本、来源和安全边界说明。 |

在成功路径上，子进程写入：

```text
<最终回答>

[ROUND END]
```

然后消费一次 `_stop`，并轮询 `reply.txt`。若没有收到后续回复，约 10 分钟后退出。

### 2.3 当前完成判据

主 agent 在这次运行中用 `output.txt` 是否包含 `[ROUND END]` 判断任务是否完成。这与执行器的写入行为一致，但该标记是面向文本展示的约定，而非结构化状态：

- 无法表达 `running`、`succeeded`、`failed`、`cancelled`、`timed_out` 等互斥状态；
- 无法记录最后一次活动、模型调用次数、工具调用次数或失败原因；
- 写文件中断、模型超时、进程被杀和逻辑异常都可能表现为“没有 `[ROUND END]`”；
- 主 agent 可以在任务尚未结束时写 `_stop`，但文件本身不能证明子进程已响应停止。

## 3. 2026-07-13 实际运行复盘

### 3.1 主任务与启动命令

用户请求为“启动两个 sub agent 调研法国和阿根廷的历次世界杯成绩”。主会话（`temp/sessions/session_85a0069709f94ef4a84df0a46b9d2afa.jsonl`）记录了以下两个启动命令：

```text
python agentmain.py --task wc_france_results --input "调研法国国家队历次男足世界杯成绩……"
python agentmain.py --task wc_argentina_results --input "调研阿根廷国家队历次男足世界杯成绩……"
```

对应任务输入分别保存在：

- [`temp/wc_france_results/input.txt`](../temp/wc_france_results/input.txt)
- [`temp/wc_argentina_results/input.txt`](../temp/wc_argentina_results/input.txt)

### 3.2 时间线

| 时间（UTC+8） | 事件 | 证据 |
|---|---|---|
| 09:37:31 | 法国任务创建并开始执行 | `wc_france_results/input.txt` 修改时间；对应 task session 创建时间。 |
| 09:37:32 | 阿根廷任务创建并开始执行 | `wc_argentina_results/input.txt` 修改时间；对应 task session 创建时间。 |
| 09:39:03 | 阿根廷任务完成并落盘 | `wc_argentina_results/output.txt` 修改时间；task session 第 1 回合记录。 |
| 09:40:44 | 法国任务最后一次输出落盘 | `wc_france_results/output.txt` 修改时间；内容停在第 6 回合。 |
| 09:41:32 | 主会话最终答复 | 主会话明确说明法国任务未收尾，转由主控补查并写入 `_stop`。 |

两项任务的启动时差约 1 秒，且各自拥有独立的输入、输出和 session JSONL，支持它们是两个独立的后台 subagent，而非主 agent 的串行文本模拟。

### 3.3 阿根廷任务：成功路径

阿根廷任务的输出文件为 [`temp/wc_argentina_results/output.txt`](../temp/wc_argentina_results/output.txt)。该文件显示：

- 经历 3 个模型回合；
- 使用 Tavily 搜索与页面抽取工具；
- 产出历届参赛成绩表、来源列表与总结；
- 末尾存在 `[ROUND END]`；
- 对应 session 为 [`temp/sessions/session_49e2fe14d7fc4231a3ca785028c51191.jsonl`](../temp/sessions/session_49e2fe14d7fc4231a3ca785028c51191.jsonl)，记录来源为 `task`。

按当前协议，该任务可以判定为成功完成。

### 3.4 法国任务：未完成与主控接管

法国任务的输出文件为 [`temp/wc_france_results/output.txt`](../temp/wc_france_results/output.txt)。该文件显示：

- 已进行至少 6 个模型回合；
- 前 5 个回合主要是 FIFA、RSSSF 等来源检索与抽取；
- 第 6 回合后直接结束；
- 没有结构化的最终表格、总结或 `[ROUND END]`；
- `stdout.log` 和 `stderr.log` 均为空，无法从进程日志还原卡住的直接原因。

主会话随后记录“法国 agent 中途卡在核验阶段，我已用主控补查并停止后台 agent”。任务目录随后出现 `_stop`。因此，此任务的最终法国表格属于主 agent 汇总结果，而不是法国 subagent 的已完成交付。

## 4. 本次运行中发现的工程问题

### 4.1 状态信号与内容耦合

`[ROUND END]` 嵌在自然语言输出中，是当前唯一的主要完成信号。它不能覆盖失败、取消、超时、部分完成等状态，也不适合供 UI、调度器和恢复逻辑稳定消费。

### 4.2 主控无法判断“卡住”的原因

法国任务未完成时，主控只能看到输出不再增长。由于 `stderr.log` 为空、没有心跳和工具调用事件流，无法判断它是：

- 正在等待模型响应；
- 正在执行工具调用；
- 模型回合异常结束；
- 子进程已经退出；
- 队列等待超时；
- 受到 `_stop` 或其他外部信号影响。

这会迫使主 agent 用经验性轮询决定是否接管，容易重复工作，也可能过早取消仍在推进的任务。

### 4.3 停止协议没有确认闭环

主 agent 通过写 `_stop` 请求停止，但没有 `cancel_requested`、`cancelled`、`acknowledged` 等状态回写。当前也没有记录：谁停止、何时停止、停止前是否安全落盘、子进程最终退出码为何。

### 4.4 输出工件不利于恢复与审计

过程输出、最终答案和控制标记混合在 `output.txt`。当一个任务在工具调用中断时，后续组件无法可靠读取“已获得的来源”“当前计划”“最后一次模型响应”和“是否可继续”。

### 4.5 主 agent 的接管结果未建立归因

法国任务失败后，主 agent 补查并给出最终表格是合理的降级行为，但当前没有在任务目录中保存主控接管的输入、补查来源、接管时间、最终归因与原因码。后续复盘只能从主会话大文本中寻找线索。

## 5. 建议的目标模型

建议保留“每任务一个目录、独立进程”的简单优势，但将文件协议升级为显式的任务状态模型。最小状态机可为：

```text
queued -> starting -> running -> succeeded
                      |          |
                      v          v
                 cancel_requested  awaiting_followup
                      |
                      v
                  cancelled

starting/running -> failed | timed_out
```

关键原则：

1. **状态与自然语言输出分离。** 最终答案可以是 Markdown，但状态必须由 JSON 明确表达。
2. **每次状态变化可审计。** 记录时间、原因码、进程 PID、父任务 ID、模型配置摘要和最近活动时间。
3. **停止是双向协议。** 主控写入取消请求，worker 确认后更新终态；主控超时后才负责升级为强制终止。
4. **主控接管有明确归因。** 任务结果应标明 `worker`、`parent_takeover` 或 `mixed`，避免把主控答案误计为 subagent 成功率。
5. **恢复依赖结构化快照。** 把最后的模型回合、工具调用摘要和中间结论放入可读快照，而不是只依赖拼接文本。

## 6. 推荐的任务目录契约（草案）

```text
temp/tasks/<run_id>/
  request.json          # 初始请求、父 run、创建时间、配置摘要
  status.json           # 当前状态的唯一真源
  events.jsonl          # append-only 状态/模型/工具事件
  result.md             # 仅最终用户可读结果
  result.json           # result 状态、归因、结构化摘要、引用
  checkpoint.json       # 可恢复的最小上下文和最后活动
  stdout.log
  stderr.log
  cancel.json           # 主控取消请求和原因
```

`status.json` 最小示例：

```json
{
  "run_id": "wc_france_results",
  "parent_run_id": "session_85a0069709f94ef4a84df0a46b9d2afa",
  "status": "running",
  "pid": 12345,
  "created_at": "2026-07-13T09:37:31+08:00",
  "started_at": "2026-07-13T09:37:31+08:00",
  "last_activity_at": "2026-07-13T09:40:44+08:00",
  "attempt": 1,
  "last_event": "tool_call_finished"
}
```

终态应额外包含：

```json
{
  "status": "succeeded",
  "finished_at": "2026-07-13T09:39:03+08:00",
  "exit_code": 0,
  "result_path": "result.md",
  "result_owner": "worker",
  "failure_reason": null
}
```

对于本次法国任务，理想记录应类似：

```json
{
  "status": "cancelled",
  "cancel_requested_at": "2026-07-13T09:41:xx+08:00",
  "cancel_reason": "parent_takeover_after_no_final_result",
  "result_owner": "parent_takeover",
  "worker_partial_result_path": "checkpoint.json"
}
```

## 7. 优化优先级

### P0：先补齐正确性与可观测性

1. 写入并维护 `status.json`，替代对 `[ROUND END]` 的状态依赖。
2. 对每个子任务记录 PID、启动时间、最后活动时间、退出码和异常摘要。
3. 将 stdout/stderr 的路径、大小和最后写入时间展示给主控/UI。
4. 将取消改为“请求—确认—超时强制终止”的闭环，并记录原因。
5. 仅在 `status=succeeded` 且存在 `result.md` 时认定 subagent 成功。

### P1：降低主控接管成本

1. 把每轮工具调用、摘要和来源记录为 `events.jsonl`。
2. 每个回合或每次工具调用后更新 `checkpoint.json`。
3. 为“父 agent 接管”定义标准接口：读取 partial result，指定接管原因，保存最终结果并标记归因。
4. UI 中区分“worker 成功”“主控接管完成”“取消”“失败”“超时”。

### P2：调度与质量控制

1. 为任务设置总时限、无活动时限、最大回合数和最大工具调用数。
2. 按任务类型定义完成验收条件。例如调研任务可要求：结构化表、来源数、结论、引用可访问性检查。
3. 支持受控重试：仅对可重试错误重试，并保留每次 attempt 的历史。
4. 增加结果质量检查 worker 或 verification 阶段，但必须将其与原执行 worker 的状态和产物分开记录。

## 8. 建议的验证场景

后续改造应至少覆盖以下场景：

| 场景 | 预期验收结果 |
|---|---|
| 两个正常研究任务并行完成 | 各自独立 `run_id`、PID、事件流、`succeeded` 状态与 `result.md`。 |
| 一个任务模型超时 | 进入 `timed_out`，保留 checkpoint、原因码和退出码；另一个任务不受影响。 |
| 主控取消运行中任务 | 出现 `cancel_requested`，worker 确认后为 `cancelled`；若未确认，记录强制终止。 |
| 子进程崩溃 | `failed`，`stderr.log` 与异常摘要可定位，主控不把它误判为仍在运行。 |
| worker 部分完成后主控接管 | 原任务标记接管原因；最终结果标记 `result_owner=parent_takeover`。 |
| 重启 GA 后恢复查看 | 能从 `status.json` 和 `events.jsonl` 恢复所有非终态与终态任务，不依赖内存对象。 |

## 9. 本次调研的证据索引

| 证据 | 说明 |
|---|---|
| [`agentmain.py`](../agentmain.py) | 当前 `--task` 后台启动、文件输出和 `_stop`/`reply.txt` 轮询实现。 |
| [`temp/wc_france_results/input.txt`](../temp/wc_france_results/input.txt) | 法国 subagent 的任务输入。 |
| [`temp/wc_france_results/output.txt`](../temp/wc_france_results/output.txt) | 法国 subagent 的未完成过程输出。 |
| [`temp/wc_argentina_results/input.txt`](../temp/wc_argentina_results/input.txt) | 阿根廷 subagent 的任务输入。 |
| [`temp/wc_argentina_results/output.txt`](../temp/wc_argentina_results/output.txt) | 阿根廷 subagent 的完整结果和 `[ROUND END]`。 |
| [`temp/sessions/session_f8059ba4774446a485b59e32a37d36ce.jsonl`](../temp/sessions/session_f8059ba4774446a485b59e32a37d36ce.jsonl) | 法国 task session。 |
| [`temp/sessions/session_49e2fe14d7fc4231a3ca785028c51191.jsonl`](../temp/sessions/session_49e2fe14d7fc4231a3ca785028c51191.jsonl) | 阿根廷 task session。 |
| [`temp/sessions/session_85a0069709f94ef4a84df0a46b9d2afa.jsonl`](../temp/sessions/session_85a0069709f94ef4a84df0a46b9d2afa.jsonl) | 主会话，包含任务发起、轮询、接管和停止记录。 |

## 10. 后续设计切入点

建议先以 P0 为一个小而独立的改造阶段：不改变现有 `--task` 命令形态，不改变模型调用流程，只新增结构化状态写入、退出状态收集与取消确认。这样可以先让“一个任务究竟是否完成、为何停止、由谁交付结果”变得可判定，再考虑调度器、断点恢复和复杂的多智能体编排。

