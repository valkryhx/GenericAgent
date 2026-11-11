# GA 动态工作流（dynamic workflow）是怎么跑的

> 配套图：[`ga-workflow-diagram.html`](./ga-workflow-diagram.html) —— 可交互，点工具栏「动态」看信号持续流动
>
> 代码版本：`c290cb0`（图中每条引用都标注了文件与行号，可用 `git show c290cb0:<path>` 复核）

---

## 一句话版本

主代理不直接干活，而是**写一段确定性的 JavaScript 脚本**；脚本在 Node vm 沙箱里跑，每调一次 `agent()` 就通过 RPC 让 Python 侧登记一个排队任务，由调度器按 `dependsOn` 分波次、按并发上限派给子 agent（**线程**），结果落盘成 artifact，最后以 `workflow_handoff` 消息唤醒主代理。

和另外两种多 agent 方式的区别，一句话就能说清：

| | 派活方式 | 子 agent 载体 | 适合什么 |
| --- | --- | --- | --- |
| subagent | `spawn_agent` 派一次干一次 | 独立**进程** | 需要强隔离、独立权限、独立工作目录 |
| Agent Team | teammate 自己循环领活 | 独立**进程** | 长期在线、反复领任务的团队 |
| **workflow** | **编排逻辑写成脚本** | **线程** | 一次性的、结构固定的批量编排 |

---

## 三个角色

| 角色 | 是什么 | 代码位置 |
| --- | --- | --- |
| workflow 工具 | 主代理的入口，立即返回 `runId` 不阻塞 | `genericagent/core/handler.py:1406` |
| 脚本契约 | 校验 `meta` 必须是纯字面量、脚本必须确定性 | `genericagent/workflow/js/script_contract.js:147`、`determinism.py:35` |
| Node worker | 在 vm 沙箱里执行脚本，负责把 `agent()` 转成 RPC | `genericagent/workflow/js/worker.js:135` |
| AgentScheduler | 分波次、控并发、管重试与跳过 | `genericagent/workflow/scheduler.py:433` |
| ChildAgentRunner | 起**线程**跑子 agent | `genericagent/workflow/child_agent.py:799`、`:820` |
| WorkflowStore | 落 `run.json`、进度、artifact | `genericagent/workflow/store.py:430` |
| `/workflows` 面板 | Ink 覆盖层，看实时进度/停止/重跑 | `frontends/ink/ui/src/workflowPanel.ts` |

---

## 一次完整链路（按发生顺序）

1. **写脚本**：主代理调 `workflow` 工具，`script` 里是一段 JS。
2. **契约校验**：第一条语句必须是 `export const meta = { name, description, phases? }`，
   且 `meta` 只能是纯字面量（不许有变量、函数调用、模板插值）。
3. **确定性闸门**：扫描 `Date.now()` / `Math.random()` / 无参 `new Date()`，命中直接拒绝。
4. **进沙箱**：脚本被包成 `(async () => { ... })()` 丢进 `vm.createContext`，
   沙箱里 `require`、`process`、`fetch`、`setTimeout` 全是 `undefined`；
   `Date` 和 `Math` 被替换成抛错的 Proxy。
5. **`agent()` → RPC**：脚本每调一次 `agent(prompt, options)`，
   worker 就往 stdout 写一行 `{type:'rpc', method:'agent'}`；
   Python 侧 `_handle_rpc` 收到后调 `scheduler.register_agent(...)`，登记成 `queued`。
6. **`parallel()` / `pipeline()`**：这两者只是 `Promise.allSettled` 的包装，
   作用是把同一批 `agent()` 标记成一组，供隐式隔离判定用。
7. **分波次**：`dependsOn` 里每个被依赖任务的 `wave + 1` 就是本任务的波次；
   没有依赖的是第 1 波。
8. **并发派发**：`_start_queued_jobs` 给「依赖已满足 + 未到重试时间」的任务分配槽位，
   默认 `max_concurrent = 4`（上限 16），超出的留在队列。
9. **子 agent 开跑**：`ChildAgentRunner.start()` 起一个
   `threading.Thread(target=self._run_job)`——**线程，不是进程**。
10. **完成后落盘**：结果写成 artifact，`store.write_workflow_progress` 更新进度。
11. **汇总唤醒**：整轮结束后，给主代理排一条 `<workflow_handoff>` 消息。

---

## 为什么非要「确定性」

因为**缓存和 resume 的前提是「同脚本 + 同参数 = 同 `agent()` 调用序列」**。

`_cache_key` 由 `scriptHash`、`argsHash`、工作区哈希、`callIndex` 组成
（`scheduler.py:1720`）。只要脚本里混进 `Date.now()`，
换个时间重跑，生成的 prompt 就变了，缓存全部失效，resume 也就无从谈起。

两层防护：

1. **静态扫描**（`determinism.py`）：剥掉注释与字符串后正则匹配三个模式。
   它**不**能抓到 `Date["now"]`、别名、`globalThis.Date.now`——文档里明确写了「这不是沙箱」。
2. **运行时 Proxy**（`worker.js:29`）：`shimDate` 把无参 `new Date()` 和 `Date.now()` 
   都替换成抛错函数，`shimMath` 同理。静态漏掉的写法在这里也跑不通。

> 顺带一提：`Date.parse`、`Date.UTC`、`new Date(value)` 仍然可用——它们不依赖当前时钟。

---

## 五个容易误解的点

1. **workflow 的子 agent 是线程，不是进程。** 它共享主进程的内存和 LLM 客户端。
   所以起得快、开销小，但**隔离性弱于 subagent**——真需要独立权限/独立目录时，还是得用 `spawn_agent`。

2. **工具立刻返回，不阻塞。** `workflow` 工具返回的是 `runId`（`wf_` + 32 位十六进制），
   不是执行结果。想阻塞等待得显式用 `wait_agent(condition="workflow_terminal", target=runId)`。

3. **`degraded` 不算失败，但也不是成功。** 上游 `degraded` 时下游仍可消费其结果
   （`_dependencies_satisfied` 把 `succeeded/cached/degraded` 都算满足），
   但整轮会记为 `degraded` 而不是 `succeeded`。

4. **依赖失败会连坐。** 上游进了 `failed/cancelled/killed`，下游直接被标 `skipped`，
   错误是 `dependency_failed`——它不会去跑，也不会假装成功。

5. **脚本里不能有副作用。** `setTimeout`、`fetch`、`require`、`process` 全被显式设为 `undefined`。
   需要在脚本之外做的事（打时间戳、发请求），要么用 `args` 传进去，要么等 workflow 返回后再做。

---

## 缓存与 resume

- 缓存键在 `scheduler.py:1720`，包含脚本哈希、参数哈希、工作区哈希与 `callIndex`。
- 工作区哈希刻意用 **base root** 而不是 run 目录：
  run 目录是隔离的实现细节，每次都不一样，拿它做哈希会让 resume 时所有缓存全部失效。
- `resumeFromRunId` 允许从某个 run 接着跑；已完成的 `agent()` 调用按 `callIndex` 命中缓存，
  不会重复烧 token。

---

## 关键代码索引

| 想了解 | 看这里 |
| --- | --- |
| 工具入口与参数 | `genericagent/core/handler.py::do_workflow` |
| 脚本契约（meta 必须是字面量） | `genericagent/workflow/js/script_contract.js::parseWorkflowScript` |
| 确定性静态扫描 | `genericagent/workflow/determinism.py::find_nondeterminism` |
| 沙箱与 `agent()` 转发 | `genericagent/workflow/js/worker.js::executeScript` |
| RPC 分发 | `genericagent/workflow/runtime.py::_handle_rpc` |
| 任务登记与波次推导 | `genericagent/workflow/scheduler.py::register_agent`、`_resolve_wave` |
| 并发派发与依赖判断 | `scheduler.py::_start_queued_jobs`、`_dependencies_satisfied` |
| 子 agent 线程启动 | `genericagent/workflow/child_agent.py::start` |
| 结果摘要与状态机 | `genericagent/workflow/models.py::summarize_workflow_jobs`、`project_workflow_execution_outcome` |
| 进度落盘 | `genericagent/workflow/store.py::write_workflow_progress` |
| 面板渲染 | `frontends/ink/ui/src/workflowPanel.ts` |

---

## 和另外两种多 agent 方式怎么选

| 你的情况 | 选什么 |
| --- | --- |
| 只是派一个活、跑完收结果 | `spawn_agent`（subagent） |
| 多个 agent 长期在线、反复领任务 | Agent Team |
| 结构固定、需要并发/依赖/缓存/重放的批量编排 | workflow |
| 需要子 agent 有独立权限或独立工作目录 | `spawn_agent`，**不要**用 workflow |

一句话：**workflow 强在编排表达力和可重放性，弱在隔离性。** 
它把「怎么调度」这件事从模型手里拿走，变成一段可校验、可缓存的脚本。
