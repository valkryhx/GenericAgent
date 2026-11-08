# GA 子代理（subagent）是怎么跑的

> 配套图：[`ga-subagent-diagram.html`](./ga-subagent-diagram.html) —— 可交互，支持明暗主题与动效
>
> 代码版本：`07fd03e`（图中箭头指向 `cd44ea0` 的具体行号，已逐条复核在该版本上仍然有效）

---

## 一句话版本

子代理**不是线程、不是协程，是一个真正的独立进程**：主代理用 `spawn_agent` 启动它，它自己跑自己的回合、自己调模型，干完把结果写进一个文件；主代理下一轮开口前，把那个文件读出来塞进上下文。

理解这一个事实，后面全是它的推论。

---

## 为什么要独立进程

因为在 Python 里，一个进程只有一个 GIL、一套全局状态、一个 `sys.stdout`、一个当前工作目录。如果子代理是线程：

- 它写日志会和主代理的协议流（stdout 上的 JSONL）打架；
- 它切工作目录会影响主代理；
- 它崩了会带走主代理；
- 它没法用不同的权限、不同的 git 工作树。

做成进程，这些全部自然隔离。代价是**跨进程通信**：不能直接调函数返回，只能靠文件交换。

---

## 完整链路

```
用户
 └─▶ 主代理进程
      └─▶ spawn_agent 工具调用
           └─▶ AgentManager.spawn_agent()
                ├─▶ 注册表：先占一个活跃名额
                ├─▶ 能力边界：算出这个子代理能用哪些工具
                └─▶ Popen 起子进程
                     └─▶ 子代理进程
                          ├─▶ 独立跑回合循环
                          ├─▶ 独立调 LLM
                          └─▶ 把结果写进 task_dir
                               └─▶ 父侧汇总成事件
                                    └─▶ 主代理下一轮前 drain
                                         └─▶ 折进 tool_result 回到主代理
```

对应图中从左到右的主路径，然后从底部绕回左边。

---

## 六个关键设计

### 1. 每个子代理有自己的目录

`spawn_agent` 会在 `task_dir` 下准备一整套文件：`state.json`（当前状态）、`input.txt`（任务说明）、`output.txt`（结果）、`events.jsonl`（事件流）、`stdout.log` / `stderr.log`（原始日志）。

所有跨进程的信息都经过这里。**这是唯一的真相来源**，不是内存里的某个对象。

### 2. 启动命令就是一次普通的 Python 调用

```python
python -m genericagent.core.app --task <名字> --nobg --task_dir <目录> ...
```

子进程跑的是**同一个程序**。没有特殊的 worker 协议，就是拿参数再启动一次自己。`--nobg` 表示不要转后台。

### 3. 两道并发闸门

- **注册表**：默认最多 32 个活跃子代理，另外限制树的深度（子代理的子代理再往下）。
- **workflow 调度器**：如果走的是 workflow 批量派活，单波次并发默认 4 个，上限 16。

超过就拒绝，并写入一条 `spawn_rejected` 事件——拒绝本身也是可观测的。

### 4. 子代理不能再起子代理

`capabilities.py` 里把工具分成两类。`spawn_agent`、`wait_agent`、`close_agent` 这些**编排工具**只给主代理；子代理拿到的工具集里没有它们。

这不是提示词里的"请不要"，是**从工具 schema 里直接删掉**——模型根本看不到这个工具，谈不上调用。

> 这是刻意的：允许递归 spawn 会导致数量爆炸和成本失控，而收益很小。需要层级时，用 workflow 的 `dependsOn` 表达。

### 5. 结果不是"返回"，是"写到文件里，父侧再捡起来"

子进程结束时，把完成帧写进收件箱和 `events.jsonl`。父侧的 `AgentManager` 读这些文件，汇总成总线事件。

主代理在**下一轮对话开始前** drain 未读事件——这个时机很重要，因为它必须在模型开口之前，否则模型这一轮看不到结果。

### 6. 通知被"折"进 tool_result

这是最容易看错的地方。通知**不是**作为一条新的用户消息贴进来，而是和刚结束的那个 `tool_result` 块放在**同一条 user 消息**里：

```json
{
  "role": "user",
  "content": [
    { "type": "tool_result", "tool_use_id": "call-...", "content": "{...结果...}" },
    { "type": "text", "text": "\n[GA_SUBAGENT_NOTIFICATIONS]\n[SYSTEM NOTIFICATION - NOT USER INPUT]\n..." }
  ]
}
```

为什么这么绕？因为模型对"用户又说话了"和"我刚刚那个工具调用返回了什么"的反应不同。折进 tool_result，模型就会把它理解成**工具调用的延续**，而不是需要重新对话的新输入。

那个 `[SYSTEM NOTIFICATION - NOT USER INPUT]` 前缀是**防伪造**：子代理的输出可能包含用户可控的文本，这个前缀保证模型不会把它当成用户的真实指令。

---

## 两件容易误解的事

**"后台运行"不等于"不占资源"。** `background=True` 只表示主代理不等它——子进程照样在跑、照样烧 token、照样占一个活跃名额。

**"主代理知道子代理干完了"需要真正确认。** 不能靠日志里有没有打印，判据是**消息结构**：通知是否和 `tool_result` 出现在同一条 user 消息里。这一点在 `docs/20261108-GA-subagent链路真实证据核验手册.md` 里有完整的验证方法。

---

## 想看代码

图中每个节点都有一个角标，点开是精确到行号的 GitHub 链接。主要入口：

| 想了解 | 看这里 |
| --- | --- |
| 工具调用入口 | `genericagent/core/handler.py:1598` |
| 启动逻辑与进程创建 | `genericagent/agents/manager.py:869`、`:1114` |
| 并发与深度限制 | `genericagent/agents/registry.py:11`、`:428` |
| 能力边界（谁能拿到什么工具） | `genericagent/agents/capabilities.py:14` |
| 事件总线 | `genericagent/agents/event_bus.py:16` |
| 通知拼装与确认 | `genericagent/agents/notifications.py:106`、`:142` |
| 主代理注入通知 | `genericagent/core/app.py:579` |

workflow 那套（`workflow/child_agent.py`）是**另一条并行实现**：它用线程而非进程，用 `start/poll/cancel` 而不是进程句柄。两者组装方式不同，别混着看。
