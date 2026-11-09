# GA Agent Team 是怎么跑的

> 配套图：[`ga-agent-team-diagram.html`](./ga-agent-team-diagram.html) —— 可交互，支持明暗主题、搜索、聚焦与动效
>
> 代码版本：`e68e983`（图中每条引用都标注了文件与行号，可用 `git show e68e983:<path>` 复核）

---

## 一句话版本

lead 用 `task_create` 把活写进一块**共享任务板**（一个 JSON 文件），用 `spawn_agent(team_mode=true)` 起若干个**独立的 teammate 进程**；teammate 每回合结束后自己从板上认领下一条任务，干完写 `output.txt`、发事件；lead 下一轮开口前，把通知与结果文件读进上下文。`/team` 面板只是把 **registry + 任务板** 这两个文件投影成一行行状态。

和普通子代理唯一的区别：普通子代理是"派一次、跑一次、交一次"；teammate 是"派一次、自己循环领活"，直到 lead 把它关掉。

---

## 八个组成部分

| 组件 | 是什么 | 代码位置 |
| --- | --- | --- |
| Lead agent | 根会话 `/root` 的回合循环，负责建任务、派单、收汇报、写总结 | `genericagent/core/app.py:1808` |
| Team 工具层 | `spawn_agent` / `followup_task` / `send_message` / `task_create` / `task_update` / `close_agent` | `genericagent/core/handler.py:1598`、`:2043`、`:2176` |
| teammate 子进程 | `team_mode` 子代理，每个都是独立 OS 进程，跑自己的模型回合 | `genericagent/core/app.py:2673` |
| 共享任务板 | `tasks.json` + 文件锁，任务 id / subject / status / owner / blockedBy | `genericagent/agents/tasks.py:52` |
| SubagentRegistry | 一行一个代理：`status`、`previous_status`、`turn_status`、`process_status`、pid | `genericagent/agents/registry.py:312` |
| IPC 事件总线 | `events.jsonl` + 命名管道，`turn_completed` / `agent_waiting_reply` / `agent_closed` | `genericagent/agents/event_bus.py:26` |
| parent inbox | lead 的"信箱"，事件与 `send_message` 都追加到这里，下一个 fold 点读取 | `genericagent/agents/manager.py:2867` |
| `/team` 面板 | Ink 覆盖层，读 registry + 任务板画成员行与任务板 | `frontends/ink/ui/src/teamPanel.ts:90` |

任务板落在 `%LOCALAPPDATA%\GenericAgent\tasks\<项目key>--<teamName>\tasks.json`，
不在仓库里，所以同一个仓库跑多次团队互不干扰。

---

## 一次完整链路（按发生顺序）

1. **建任务**：lead 调 `task_create`，任务以 `pending` 落进 `tasks.json`。
2. **派单**：`spawn_agent(task_name="w1", team_mode=true, task_id="1")` 起进程，registry 写一行，任务 `owner` 绑到 `/root/w1`。
3. **认领**：teammate 开跑后先 `task_list`，认领自己的任务（或后续 idle hook 自动认领下一条）。
4. **干活**：正常调工具、读写文件；完成时由 **owner 自己** `task_update(taskId, status="completed")`。
5. **汇报**：`send_message(target="/root", ...)` 或 `target="team-lead"`——两者都路由到 lead 的 parent inbox。
6. **收尾**：回合结束，`output.txt` 写入最终文本 + `[ROUND END]`，`state.json` 记 `turn_status=completed`，总线发 `turn_completed`。
7. **继续认领**：idle hook 看板上还有没有 `pending`；有就认领，没有就进 `waiting_reply` 等 lead 的 followup。
8. **lead 下一轮**：fold 点把 inbox 里的通知与 teammate 的 `output.txt` 读进上下文，继续决策。
9. **关闭**：lead 调 `close_agent` → 写 `_stop` 文件 → 等 `grace_s`（默认 2 s）→ 仍存活才 terminate → **重读 `state.json`** → registry 记 `previous_status` / `closed_status`。

---

## `/team` 面板的结论是怎么算出来的

面板不读进程内存，只读两个文件：registry 的成员行、任务板的 15 条任务。
每个成员的结论由 **verdict 链**得出（`teamPanel.ts::teamMemberVerdict`）：

```
非 closed 行：
  turnStatus 是终态（completed / errored / interrupted …） → 用它
  否则 live === false（registry 证明进程已死）           → stale（×）
  否则                                                   → running / idle

closed 行：
  previousStatus 是终态                                   → 用它
  closedStatus 是 worktree_error / killed                 → failed
  turnStatus 是终态                                       → 用它
  closedStatus 是 stale / released                        → 原样
  否则                                                   → previousStatus
```

为什么这么绕：`closed` 只是**生命周期词**，它说明进程被回收了，不说明活干得怎么样。
用 `closed` 当结论，三个干完活的 teammate 会被画成三个失败。

面板还有两条硬规则：

- 认不出的结论显示 `?`，绝不回退成一个"看起来合理"的图标——宁可明显不确定，也不要自信地错。
- 15 条任务超过面板高度时，保留表头与键位提示，中间显示 `… (+N more line(s); r refresh)`，不静默截断。

---

## 六个容易误解的点

1. **teammate 是进程，不是线程**。它有自己的 stdout、cwd、权限与生命周期；跨进程只能靠文件与事件总线通信。
2. **lead 不用逐条派活**。`spawn_agent` 绑定前几条即可，剩下的由 idle hook 在每回合结束时自动认领。
3. **给 lead 发消息的地址**：`team-lead` / `lead` / `main` / `root` / `/root` 都路由到 parent inbox。
   `/root` 就是 lead 本体，**不是**某个叫 root 的子代理（`root` 已被列为保留名）。
4. **任务完成 ≠ 回合完成**。回合完成只代表这一轮模型停了；任务状态必须在 `task_update` 里显式写。
   反过来，任务 completed 也不代表 teammate 这个回合一定成功。
5. **close 会先等 2 秒**。子进程收到 `_stop` 需要时间收尾（写 `[ROUND END]`、发 `turn_completed`），
   所以关闭流程在 grace 之后重读 `state.json`，把"grace 期间刚完成"的回合记成 completed 而不是 interrupted。
6. **面板显示的是文件，不是实时进程状态**。registry 的 `running` 行可能进程早就没了，
   所以面板会向 registry 要活体判定（`is_live`），"说不清"一律算活。

---

## 关键代码索引

| 想了解 | 看这里 |
| --- | --- |
| team 工具的参数与校验 | `genericagent/core/handler.py::do_spawn_agent` / `do_task_create` / `do_send_message` |
| teammate 主循环、idle 认领、ROUND END | `genericagent/core/app.py::run_task_worker_loop`、`_teammate_idle_hook` |
| 共享任务板与 owner 规则 | `genericagent/agents/tasks.py::SubagentTaskStore` |
| registry 行与活体判定 | `genericagent/agents/registry.py::mark_closed`、`_row_is_live` |
| 关闭流程与 grace | `genericagent/agents/manager.py::_close_single_agent` |
| 事件 → parent inbox | `genericagent/agents/manager.py::_append_parent_inbox` |
| `/team` 面板结论与截断 | `frontends/ink/ui/src/teamPanel.ts::teamMemberVerdict`、`teamPanelViewport` |

---

## 这套机制是怎么验证的

2026-11-09 用真实 LLM（`zhanlu-ds/moma/deepseek-latest-flash`）跑了 15 任务（10 简单 + 5 中等）的规模回归：
3 个 teammate 全部 `completed`、任务板 15/15、`/team` 执行期采样 137 次、完成数 0→15 单调，
10 个简单答案由测试独立复算一致。过程中发现并修掉的关闭竞态、`/root` 路由、工具异常掀翻 worker
三个缺陷，逐条记录在仓库 `docs/20261109-agent-team真实回归与UX验证.md`。
