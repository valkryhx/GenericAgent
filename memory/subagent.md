# Subagent 调用 SOP

## 核心原则：Codex 式委派，不是“开子进程查岗”

GA 的 subagent 是完整 agent，拥有同等工具能力。父 agent 的职责是拆分、委派、等待事件、整合结果；不是反复读取 `output.txt` 监督执行细节。

使用 `spawn_agent` 前必须先判断：

- 用户明确要求 subagent/委派/并行，或当前 GA SOP 明确要求验证/探索 subagent。
- 先找出关键路径：下一步马上依赖的阻塞任务通常由父 agent 本地做。
- 只把可并行推进的旁路任务交给 subagent。
- 子任务必须具体、有边界、自包含，并且能实质推进主任务。
- 不要让多个 subagent 或父 agent 做同一件事。
- 委派后不要重复执行已经委派给子智能体的任务；父 agent 应做非重叠工作或等待整合。
- 谨慎调用 `wait_agent`；只有下一步关键路径需要 subagent 更新时才等。

## 父 agent 写给 subagent 的 prompt 契约

`message` 不应是泛泛目标，也不应写成长篇操作手册。它应是一个可独立执行的任务契约：

```text
任务目标：你要完成什么。
范围/非目标：只做哪些事，不做哪些事。
上下文/路径：需要读取的文件、目录、日志或用户输入；路径尽量给绝对路径。
约束：语言、格式、工具、联网、不要修改哪些文件、写入范围等。
最终结果契约：最终回答必须包含什么；如果生成文件，必须列出文件路径。
完成/失败判定：什么算完成；无法完成时返回失败原因和已验证事实，不要空 completed。
```

示例：

```text
读取 D:\git_codes\GenericAgent\README.md，用 3 条简短中文要点总结项目。只读，不修改文件。最终只返回 3 条要点；如果文件不存在，返回明确错误和你检查过的路径。
```

文件生成任务示例：

```text
使用 office-mcp skill 生成一个包含两句春天诗句的 docx，保存到 D:\git_codes\GenericAgent\temp\spring_poem.docx。只负责生成该文件，不做其他调研。最终回答必须列出生成文件的绝对路径、文件是否存在、大小；失败时说明在哪一步失败。
```

编码任务示例：

```text
只修改 tests/test_x.py，补充覆盖 Y 行为的 unittest。不要修改生产代码。最终回答列出修改文件和测试命令。
```

## 文件 IO 协议

- 目录：`temp/{task_name}/`（cwd 在 `temp/` 时即 `./{task_name}/`）。
- 启动：`python agentmain.py --task {name} [--input "短文本"] [--llm_no N]`（cwd=代码根）。
- `--input` 自动建目录、清旧 `output*.txt`、写 `input.txt`；长文本先手动写 `input.txt` 再启动，不带 `--input`。
- 自动后台启动，打印 PID 后父命令退出。
- subagent 的 cwd 仍是 `temp/`，不是 task 目录。
- 可选 fork：优先用 `subagent_manager.spawn_agent(..., fork_turns="all"|"N", fork_history=history)` 自动写 `_history.json`；兼容旧方式：手动将 history 写入 task 目录下 `_history.json`。

## 通信与状态

- `output.txt` 是可读轨迹/快照，`[ROUND END]` 只是兼容轮完成标记，不是权威结果。
- `state.json` / `events.jsonl` 是状态真源。
- 子端关键生命周期事件推送到 `temp/subagents/inbox.jsonl`。
- 父端优先用 `wait_agent` / `wait_agents()` 等 mailbox/status 更新。
- 收到 completed 后，再用 `read_agent_result` 显式读取权威最终结果。
- 普通父 agent 不主动反复读 `output.txt` 查岗；只有调试/监察任务明确要求时，才读 raw output。
- `send_message`：只入队，不触发新 turn。
- `followup_task`：入队并触发空闲 subagent 的下一轮。
- 干预文件：`_stop`（当轮结束退出）| `_keyinfo`（注入 working memory）| `_intervene`（追加指令）。
- `--verbose` 会让 output 包含工具执行结果，适合调试/监察，不适合作为默认父子通信方式。

## 标准父端流程

```text
1. 判断是否真的需要 subagent。
2. 拆出独立、可并行、非关键路径阻塞的子任务。
3. 为每个子任务写清 message 契约。
4. spawn_agent。
5. 父 agent 做非重叠本地工作；不要重复做已委派任务。
6. 需要结果时 wait_agent 等事件更新。
7. completed 后 read_agent_result 收权威结果。
8. 对 errored/interrupted/invalid_result 明确 fallback、重派或报告失败。
9. 汇总时只信 final result / artifact 信息；raw output 仅用于排查。
```

## Map 模式：并行同构任务

适用场景：多个输入彼此独立、可并行处理，且共享资源不会冲突。

约束：

- 不同 agent 处理不同输入文件，产生不同输出文件。
- 浏览器、键鼠、同一 GUI 资源通常不可并行共享。
- 不满足 map 模式的任务，父 agent 顺序执行即可。
- 每个 subagent 的写入范围必须互不重叠。

流程：

```text
1. 父 agent 准备多个独立输入文件或清晰任务契约。
2. 同一轮启动多个 subagent。
3. 父 agent 做本地非重叠工作或等待事件。
4. wait_agent 收 completed/errored/interrupted。
5. read_agent_result 收集结果。
6. reduce 汇总。
```

## 测试/监察模式

用途：观察 agent 真实行为，修正 RULES/L2/L3/SOP。

原则：

- 只给目标，不诱导路径和做法，观察自主选择。
- 如果要测试 SOP，input 指定 SOP 名，排除导航干扰。
- 如果要测试导航能力，input 只写目标，不内联 SOP 内容。
- 发现问题后按闭环：现象 → 复现 → 定位根因 → patch → 验证。

## subagent 内部 plan_mode 使用

subagent 本身是完整 agent。任务包含 3 个以上子步骤、有依赖关系或需要 checkpoint 时，可以在内部创建 `./subagent_plan.md` 管理执行。

父 agent 只需要在 message 中说明任务多步骤、建议内部规划；不要把每一步手把手写死。

文件传递机制：

- 父 agent 可在 task_dir 中生成 `context.json`。
- `context.json` 必须使用绝对路径。
- 如果 message 要求读取 `context.json`，subagent 启动后第一步必须读取它。

示例：

```json
{
  "task": "任务描述",
  "work_dir": "D:\\git_codes\\GenericAgent\\temp\\plan_dir",
  "input_files": {
    "paper_info": "D:\\git_codes\\GenericAgent\\temp\\paper_info.txt"
  },
  "output_files": {
    "pdf": "D:\\git_codes\\GenericAgent\\temp\\paper.pdf",
    "report": "D:\\git_codes\\GenericAgent\\temp\\paper_report.md"
  },
  "dependencies": ["paper_info.txt 必须存在"]
}
```

## 反模式

- 因为任务“复杂/全面/要深入”就自动 spawn。
- 把下一步马上需要的关键路径任务派出去，然后父 agent 原地等待。
- 多个 subagent 调研同一个问题，输出重复结论。
- 子任务 prompt 只写“调研一下 X”，没有最终结果契约。
- 父 agent 反复读取 `output.txt` 查岗。
- `wait_agent` 后不读 `read_agent_result`，直接猜结果。
- 文件生成任务没有要求最终列出路径、存在性和大小。
- 把 raw output 当最终答案。
