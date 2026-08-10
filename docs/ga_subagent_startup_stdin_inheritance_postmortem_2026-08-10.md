# GenericAgent 子智能体启动延迟故障复盘

**日期：** 2026-08-10

**范围：** Ink UI / JSONL bridge 启动子智能体的 Windows 进程启动链路

**关联提交：** `d36a992`（`fix(subagent): stabilize startup and worktree isolation`）

**当前状态：** 代码修复已提交；旧的 Ink UI 进程需要重启后才会加载修复。

## 1. 摘要

本次故障的主因不是 LLM 模型切换、事件总线锁或 `wait_agent` 的等待逻辑，而是 Windows 下子进程继承了 Ink bridge 的 `stdin` 管道。Ink bridge 主线程同时在该管道上执行阻塞读取；子进程在标准输入句柄初始化阶段被拖住，未能执行 `agentmain.py` 的第一条启动探针 `process_entry`。

原始中钨高新测试中，父端先写出 `agent_started`，两个子进程约 300 秒后才写出 `turn_started`。结合后续探针，最符合现有证据的解释是：阻塞一直持续到下一次 bridge 输入唤醒父端读取；它不是 `dq.get(timeout=300)` 在启动阶段造成的固定等待。后来加入启动握手后，同一问题表现为 10 秒内没有 `process_entry` 的 `startup_timeout`。

修复包括：

1. 所有 subagent `Popen` 路径显式使用 `stdin=subprocess.DEVNULL`；
2. 增加 `process_entry` 启动握手、启动阶段记录和失败快速回收；
3. 为只读搜索任务绕过不必要的 worktree，并为 worktree 超时清理完整进程树。

## 2. 故障影响

- 父代理显示两个 subagent 已启动，但 `wait_agent` 长时间只看到了 `agent_started`；
- 第一次 180 秒等待自然超时，第二次等待又被用户 `/stop` 中断；
- 后续测试出现两个典型症状：
  - `startup_timeout: ... did not emit process_entry within 10s`；
  - 并发启动时只登记或只看到一个 subagent；
- 已经完成的子代理结果仍可通过任务目录中的持久化 artifact 读取，`list_agents` 默认只展示活跃记录，二者职责不同。

## 3. 证据与时间线

### 3.1 原始中钨高新测试

以下时间来自本机任务目录 `temp/ztgx_fundamental/events.jsonl` 和 `temp/ztgx_news/events.jsonl`（这些运行产物未纳入版本库）：

| 时间（北京时间） | 事件 | 解释 |
|---|---|---|
| 10:05:15 | `ztgx_fundamental agent_started` | 父端创建任务目录并记录启动； |
| 10:05:16 | `ztgx_news agent_started` | 第二个任务同样完成父端登记； |
| 约 10:08:15 | 第一次 `wait_agent`（180 秒）超时 | 期间没有新的事件，符合“只等待状态变化”的工具语义； |
| 10:10:16 | 两个任务同时写出 `turn_started` | 从 `agent_started` 分别相隔约 301 秒和 300 秒； |
| 10:14:49 | `ztgx_fundamental turn_completed` | 子代理进入正常 LLM/MCP 执行； |
| 10:17:03 | `ztgx_news turn_completed` | 子代理进入正常 LLM/MCP 执行。 |

`turn_started` 之后才执行 `agent.put_task(...)` 和 `dq.get(timeout=300)`；因此该队列超时不能解释 10:05 到 10:10 之间的启动空窗。[1][2]

### 3.2 启动握手暴露出的同一故障

后续 Sonnet 5.5 测试的记录如下：

| 任务 | `agent_started` | 失败事件 | 结果 |
|---|---|---|---|
| `search_sonnet55_official` | 16:51:28 | 16:51:38 | 10 秒内没有 `process_entry`； |
| `search_sonnet55_rumors` | 16:51:38 | 16:51:48 | 10 秒内没有 `process_entry`。 |

两个 PID 随后都已退出，`stderr.log` 为空，`startup.jsonl` 没有第一条 `process_entry`。这说明阻塞点早于权限初始化、IPC 初始化、模型构造和任务队列读取。

### 3.3 最小化探针

排查时只改变父进程 stdin 是否被占用以及子进程是否继承该句柄：

| 条件 | 观测 |
|---|---:|
| 父进程保持阻塞 stdin 读取，子进程沿用旧的继承方式 | 约 10.2 秒后 `startup_timeout`； |
| 在启动期间释放父进程 stdin 读取 | 约 1.8 秒后进入 `process_entry`； |
| 子进程设置 `stdin=subprocess.DEVNULL` | 约 0.19 秒进入 `process_entry`； |
| 修复后并发启动两个子进程 | 约 0.22 秒、0.27 秒分别进入 `process_entry`。 |

这组对照把变量收敛到标准输入句柄继承，且与原始“双任务同一秒恢复”的现象相符。[3]

## 4. 假设排查记录

| 假设 | 结论 | 依据 |
|---|---|---|
| 事件总线锁竞争或自死锁 | 不是本次启动延迟的主因 | 失败样本连 `process_entry` 都没有；事件总线镜像发生在子进程已经运行之后。 |
| GenericAgentBridge 控制面初始化 | 方向接近，但具体根因是 bridge 的 stdin 句柄 | 只要释放父端读取或改用 `DEVNULL`，子进程立即恢复；不需要改变模型或控制面。 |
| 权限策略或 IPC 初始化 | 不是主因 | 这些阶段均位于 `process_entry` 之后；失败样本没有到达这些阶段。 |
| LLM/backend 冷启动或模型继承 | 不是主因 | 子进程未进入 Python 启动探针；原始任务后来使用同一模型配置正常完成。 |
| `dq.get(timeout=300)` | 不是启动阶段原因 | `dq.get` 位于 `turn_started` 之后，时间顺序不成立。 |
| Git worktree 创建竞争 | 是另一项独立的可靠性问题 | 只读搜索不需要 worktree；worktree 超时还可能遗留 Git 子进程，但它不能解释没有 `process_entry` 的样本。 |

## 5. 根因机制

修复前的进程关系可以简化为：

```text
Node/Ink stdin pipe
        │
        ▼
frontends/ink_bridge.py: run_jsonl_loop()
        │  主线程持续等待下一行输入
        │
        ├── worker thread 调用 SubagentManager.spawn_agent()
        │          │
        │          └── Popen 未指定 stdin
        │                    │
        │                    ▼
        │              子进程继承同一 Windows 管道句柄
        │              卡在 agentmain.py/process_entry 之前
        │
        └── 下一次 bridge 输入到达后（根据时间线推断），挂起读取被唤醒，子进程才继续
```

因此，原始约 300 秒应优先理解为“从子进程创建到父端 stdin 读取再次被唤醒的时间”，而不是一个可靠的系统超时值。由于两个子进程共享同一父端读取状态，它们在同一秒恢复并不矛盾。

## 6. 修复内容

### 6.1 标准输入隔离

- `subagent_manager.py:_child_popen_kwargs()` 增加 `stdin=subprocess.DEVNULL`；
- `agentmain.py:start_task_background()` 增加 `stdin=subprocess.DEVNULL`；
- Windows 子进程继续使用隐藏窗口创建标志，避免额外控制台干扰。[4]

### 6.2 启动握手与可观测性

- `agentmain.py` 在模块入口记录 `process_entry`，并继续记录 imports、参数解析、Agent 初始化、权限和 IPC 等阶段；
- `SubagentManager` 等待对应 PID 的 `process_entry`，默认上限为 10 秒；
- 超时或子进程提前退出时写入错误状态并终止已创建的进程，避免父端继续把失败任务当成活跃任务。

### 6.3 相关但独立的 worktree 修复

- 只读搜索任务的有效 isolation 不再强制创建 Git worktree；
- Git worktree 超时时清理完整的 Windows/POSIX 子进程树；
- spawn 结果保留请求 isolation 和降级原因，便于后续诊断。

## 7. 回归验证

本次修复加入了以下回归断言：

- `tests/test_subagent_manager.py::test_child_launch_does_not_inherit_parent_stdin`；
- `tests/test_agentmain_subagent_lifecycle.py` 对后台启动参数断言 `stdin is subprocess.DEVNULL`；
- 启动握手超时、worktree 进程树回收和只读 isolation 降级测试。

提交前执行：

```text
python -m compileall -q agentmain.py ga.py subagent_manager.py subagent_worktree.py
python -m unittest discover -s tests
```

结果：`Ran 964 tests ... OK (skipped=3)`。

## 8. 后续诊断准则

1. 看到 `agent_started` 但没有 `startup.jsonl` 中的 `process_entry`：先查 Popen、标准句柄、Windows 进程创建和父端 stdin，不要先查 LLM API。
2. 有 `process_entry` 但没有 `imports_complete`：查 Python 导入或解释器初始化。
3. 有权限/IPC 阶段但没有 `turn_started`：再查权限策略、IPC 和事件写入。
4. 已有 `turn_started` 后才长时间无输出：此时才检查 `dq.get`、LLM/MCP 调用和后端超时。
5. 运行修复后的代码前必须重启 Ink UI；已存在的旧进程不会自动获得新的 `Popen` 参数。

## 9. 参考

1. `agentmain.py`：`run_task_worker_loop()` 中 `turn_started` 与 `dq.get(timeout=300)` 的顺序。
2. `frontends/ink_bridge.py`：`run_jsonl_loop()` 对 bridge stdin 的持续读取。
3. 本机 2026-08-10 启动探针：`temp/ztgx_*`、`temp/search_sonnet55_*`、`temp/stdin_*probe*`（仅保留了汇总数据，原始运行目录不提交）。
4. `subagent_manager.py`、`agentmain.py` 及对应测试中的 `stdin=subprocess.DEVNULL` 和 `process_entry` 握手实现。
5. Git 提交：`d36a992`。
