# GA subagent 链路的真实证据核验手册（2026-11-08）

> 适用对象：任何改动 subagent 通知机制、工具面、进程生命周期、UI 实时显示之后的自测。
> 姊妹篇：`docs/20261015-GA-ink-真实LLM-e2e-自测SOP.md`（那篇讲 L1/L2/L3 三层与证据包骨架），
> 本文只讲**这个子系统**：怎么让结论建立在真实进程、真实 LLM、真实字节上。

---

## 0. 为什么必须先写这份手册

本轮自测里，**单元测试全绿但功能已死**的情况真实发生了两次：

| 现象 | 单测为什么绿 | 真实链路为什么死 |
| --- | --- | --- |
| 子代理完成后主 agent 永远收不到通知 | 测试自己写 `<root>/temp/subagents`，读取方也读这里，天然一致 | 产品路径下写入方用项目桶 `<projectData>/<session>/subagents`，读取方读 `<projectRoot>/temp/subagents`，**两个都在、都健康、队列永远空** |
| 子代理把自己事件镜像到了孤儿总线 | 同上，legacy 布局下猜的路径刚好是对的 | 产品布局下 `dirname(task_dir)/subagents` = `.../subagents/tasks/subagents`，没人读 |

共同点：**测试构造的路径空间和产品构造的路径空间不是同一个**。这不会体现在任何断言里，
因为断言用的也是同一个错误假设。所以本手册的第一条不是"怎么测"，而是"测哪条路径"。

---

## 1. 铁律：路径必须问代码要，不能自己算

```python
# ❌ 错：测试里重新推导一遍公式
reader = Path(agent.project_root) / "temp" / "subagents"

# ✅ 对：问读取方自己
from genericagent.agents.notifications import resolve_notification_bus_dir
reader = resolve_notification_bus_dir(
    agent.project_root, data_root=agent.data_root, session_id=agent.session_id
)
```

判据很硬：**如果测试里出现路径拼接，而这个拼接在源码里也存在一份，那这个测试就在验证
那个拼接，而不是验证行为。** 本轮 `_probe_three_way_bus.py` 第一次跑就踩了这个坑——
探针自己按旧公式算 reader，于是修复后仍报 `DIFFERENT`。改成调用
`resolve_notification_bus_dir` 后才变成 `SAME`。

---

## 2. 四层证据模型

由便宜到贵。**任何一层通过都不代表上一层通过**，反过来也一样。

| 层 | 驱动什么 | 真实 LLM | 能证伪什么 | 入口 |
| --- | --- | --- | --- | --- |
| **P** 探针 | 只构造对象 / 只读盘上产物 | 否 | 路径分裂、对象不一致、落盘位置错 | `tests/probes/python/_probe_*.py` |
| **U** 单测 | 真实模块 + 假 LLM | 否 | 逻辑分支、契约、序列化格式 | `tests/test_subagent_*.py` |
| **C** 真实子进程 | 真实 OS 进程 + 真实模型 | 是 | 工具面、启动握手、进程退出、真实回合 | `tests/probes/python/_probe_real_child_tools.py`、`tests/real_subagent_*.py` |
| **I** 真实 Ink 桥 | 真实 bridge + 真实 UI 事件流 | 是 | 通知是否到模型、UI 是否实时显示、主 agent 是否真收到 | `tests/real_subagent_fold_e2e.py` |

**关键：C 和 I 不能互相替代。** C 证明子代理干得了活；I 证明主代理知道它干完了。
本轮工具面缺陷在 C 层暴露，通知缺陷在 I 层暴露——只看任意一层都会漏掉另一层。

---

## 3. 层 P：探针（秒级，先跑这个）

探针不调模型、不启子进程，只回答"东西在不在对的地方"。

### 3.1 三方路径是否一致（最重要的一个）

```bash
cd /d/git_repos/GenericAgent
python tests/probes/python/_probe_three_way_bus.py; echo "EXIT=$?"
# 读 D:/tmp/ga_probe_three_way.txt
```

必须全部是 `SAME`：

```
bridge vs handler manager : SAME
bridge vs reader          : SAME
handler vs reader         : SAME
child mirror vs bridge manager : SAME
```

**为什么是三方**：bridge 的 manager 负责写事件和派子进程；handler 的 manager 才是工具调用
真正用的那个；reader 是主 agent 下一轮 chat 前去 drain 的那个。三者任一不同，事件就写到了
没人读的目录。

> 探针**不能 print**：构造 `GenericAgentBridge` 时 `backend_output_redirect()` 会把进程
> stdout 重定向进 `temp/ink_bridge_backend.log`，打印内容直接消失。所有探针写文件。

### 3.2 工具面（不启进程）

```bash
python tests/probes/python/_probe_subagent_toolset.py
```

期望：任何 `role_tools` 取值下，普通工具数都应是 **15**（本机 34 工具 schema 下），
`spawn_agent` 为 False。空 `role_tools` 只拿到 `no_tool` 就是回归。

### 3.3 legacy 兼容不能破

自带 session 的旧调用方必须仍然解析到 `<project_root>/temp/subagents`：

```bash
python -m unittest tests.test_subagent_notification_bus_path -v
```

`test_legacy_layout_is_unchanged` 红掉说明为了修产品路径把老路径改坏了。

---

## 4. 层 C：真实子进程（证明"干得了活"）

### 4.1 跑法

```bash
cd /d/git_repos/GenericAgent
export LANG=C.UTF-8 LC_ALL=C.UTF-8
python tests/probes/python/_probe_real_child_tools.py; echo "EXIT=$?"
```

### 4.2 判定不变量（不是比对固定输出）

子代理的任务**必须靠工具才能完成**：读一个内容不可猜的文件 + 跑一条命令。

| 不变量 | 含义 |
| --- | --- |
| 输出里出现种子文件里的 marker | `file_read` 真的可用 |
| 输出里出现 `CODE_RUN_OK` | `code_run` 真的可用 |
| `capability_profile` == `isolated` | 派的是普通子代理，不是特例 |
| `process_status` 最终离开 `running` | 进程正常退出 |

工具面被裁掉时，子代理会明确说"我只有这些工具"——那正是要抓的现象。基线（修复前）
形态是 `role_tools=[] + team_mode` → 只有 `task_*` / `send_message` / `team_status`。

### 4.3 别自己猜子进程落在哪

产品布局下子进程在 `%LOCALAPPDATA%\GenericAgent\projects\<key>\<session>\subagents\tasks\<name>`，
**不在** 仓库的 `temp/` 下。从 manager 取：

```python
task_root = Path(manager.task_root)
task_dir = next(p for p in sorted(task_root.glob(name + "*")) if (p / "state.json").exists())
```

否则你会对着一个不存在的目录断言"子代理没产出"，而它其实干得好好的。

---

## 5. 层 I：通知 fold 点（证明"主代理真的知道了"）

这是本轮最贵的验证，也是最容易自欺的一层。

### 5.1 跑法

```bash
cd /d/git_repos/GenericAgent
export LANG=C.UTF-8 LC_ALL=C.UTF-8
export GA_RUN_REAL_API_E2E=1
export GA_REAL_API_EXPECTED_MODEL=moma/deepseek-latest-flash
export GA_REAL_API_EXPECTED_NAME=default
python tests/real_subagent_fold_e2e.py; echo "EXIT=$?"
```

证据落在 `temp/real-subagent-fold-e2e/<stamp>/`：bridge 事件流、本轮所有
`model_responses_*.txt`、子进程的 `state/events/startup/output`。

### 5.2 判据：什么才算"fold 点送达"

**不能**用"Step N"字样判断。Step 横幅是显示文本，**从不进模型**。实测证据：

```
<summary>子代理已启动；开始第2步第1次 sleep 12 秒。</summary>
```
—— 这是 assistant 自己写的 summary，不是 harness 的 Step 号。

正确的判据是**消息结构**：fold 点的通知会和 `tool_result` 块出现在**同一条** user 消息里
（它是折进刚结束的工具轮，而不是贴在一轮新用户输入前面）：

```
=== Prompt ===
{
  "role": "user",
  "content": [
    {"type": "tool_result", "tool_use_id": "call-...", "content": "{... sleep_1_done ...}"},
    {"type": "text", "text": "\n[GA_SUBAGENT_NOTIFICATIONS]\n[SYSTEM NOTIFICATION - NOT USER INPUT]\n..."}
  ]
}
```

所以判据是 `'"type": "tool_result"' in block`，见
`tests/real_subagent_fold_e2e.py` 的 `foldedOntoToolResult`。

### 5.3 必须同时成立的四件事

| 检查 | 证明什么 |
| --- | --- |
| `a notification was injected into a main-agent prompt` | 通知真的到了模型可见的 prompt 里 |
| `the notification landed at a fold point` | 是工具轮中途，不是等用户下次说话 |
| `notification carries the anti-spoof preamble` | 防伪前置在（模型不会当成用户批准） |
| `main agent acknowledged the notice` | 模型**读懂并复述**了 task-id，不是碰巧有字样 |

最后一条最重要：只断言"prompt 里有字样"证明的是注入；主 agent 能贴出
`/root/fold_probe_child` 才证明**送达且可理解**。

### 5.4 负面判据：别让噪声冒充成功

- **只对比本轮新增的 `model_responses_*.txt`**（跑前打快照，跑后取差集）。整目录扫会把
  上一次会话的通知也算进来。
- **子代理自己也写 prompt 日志**（它有自己的进程）。通知只在**主 agent** 的日志里出现，
  所以 triplet 里 `hasMarker`（子代理 marker）为 False 才是对的。
- 前置一条**强约束任务**，逼主 agent 真的在子代理运行期间做事：

  ```
  1) 派 spawn_agent（子代理只输出一个 marker）
  2) 你自己连续调用 code_run 三次 Start-Sleep -Seconds 12（不要合并）
  3) 回答：第 2 步期间是否收到通知？收到就把 <task-id> 贴出来
  ```

  合并成一次 sleep 就只有一个工具轮，fold 点只有一次机会，且子代理可能还没跑完。

### 5.5 一次合格运行长什么样（本轮实测）

```
passed: True elapsed: 61.4
OK   child task dir exists      ...\subagents\tasks\fold_probe_child
OK   child produced a turn output ['output.txt']
OK   child answered without reporting a missing tool
OK   a notification was injected into a main-agent prompt 1
OK   the notification landed at a fold point  True
OK   notification carries the anti-spoof preamble
OK   notification names the child task
OK   main agent acknowledged the notice   /root/fold_probe_child
OK   child process exited  {'pid': 21412, 'alive': False, 'processStatus': 'killed'}
```

---

## 6. UI 实时显示怎么验

UI 的数据来源是 bridge 的两条通道，**不是**子代理的 stdout：

| 事件 | 何时发 | 触发条件 |
| --- | --- | --- |
| `agent_event` | 事件总线有新事件 | `events_since(cursor)` 非空 |
| `agent_snapshot` | 权威快照指纹变化 | `registry.json` 或任一 `tasks/*/state.json` 的 stat 变了 |

`_watch_agent_state` 是 **0.5s 纯 stat 轮询**，不读 `events.jsonl`（那文件会涨到 MB 级）。

验证要点：
1. **必须真的改了盘**，watcher 才会发。手工改内存对象不算。
2. 快照有指纹去重（`_agent_snapshot_fingerprint`），**内容不变不发第二帧**；看不到重复帧是
   正确行为，不是丢帧。
3. 起新 Ink 会话时 `_initial_agent_cursors` 会把游标播种到当前尾部，**故意不回放历史**——
   想看到历史事件要显式从 0 读。

```bash
cd /d/git_repos/GenericAgent
python tests/real_ink_selftest.py          # 结构检查，秒级
GA_RUN_REAL_API_E2E=1 GA_SELFTEST_TIMEOUT=180 python tests/real_ink_selftest.py
```

后者会落证据包 `temp/ink-selftest/<stamp>/`（`events.jsonl` + 后端日志尾巴）。

---

## 7. 进程正常退出怎么验

三个独立信号，**必须都查**：

| 信号 | 从哪读 | 期望 |
| --- | --- | --- |
| `state.json` 的 `process_status` | 子代理任务目录 | 离开 `running`/`waiting_reply`（`killed`/`shutdown`） |
| registry 的 `closed` + `process_status` | `registry.json` | `closed=True` 或终态 |
| OS 层 pid | `psutil.pid_exists(pid)` | `False` |

**只用 pid 判断死活是错的**：pid 会被回收，且一行可能正在 spawn 中途。源码里
`_reject_dead_mailbox` 明确只用**持久信号**（registry 的 `closed` / `process_status`），
理由写在它的 docstring 里，别凭直觉加 pid 探活。

`close_agent` 之后还要查 authkey 已删（`tests/real_subagent_realtime_e2e.py` 的
`authkeyRemovedOnClose`）—— key 还在就说明通道没真正关。

---

## 8. 反模式表（全部在本轮真实踩过）

| 反模式 | 后果 | 正确做法 |
| --- | --- | --- |
| 测试里重新推导路径公式 | 测试和产品用同一个错误假设，永远绿 | 调 `resolve_notification_bus_dir` / `manager.task_root` |
| 用 "Step N" 判断 fold 点 | 横幅不进模型，永远判成"没送达" | 判 `tool_result` 与通知块同消息 |
| 探针 `print` | bridge 重定向 stdout，输出消失 | 写文件 |
| 拿 `dirname(task_dir)/subagents` 当总线 | 产品布局下是孤儿总线 | 用 `GA_SUBAGENTS_STATE_DIR` 显式传 |
| 只跑单测就宣布修好 | 路径分裂全绿 | 至少再跑一次层 P + 层 I |
| 用 pid 判断 agent 死活 | 误拒合法投递 | 只用 registry/state 的持久状态 |
| 整目录扫 `model_responses` | 歷史会话通知冒充本轮 | 跑前快照，取差集 |
| 把多次 sleep 合并成一次 | 只有一个 fold 点，且子代理可能没跑完 | 明确要求分三次工具轮 |
| 对着 `temp/<name>` 断言子进程产出 | 产品布局下那里什么都没有 | 从 `manager.task_root` 取 |
| 只读 `output.txt` 判后续轮次 | 第 N 轮写在 `outputN.txt`，working 的投递看起来像丢了消息 | 合并 `output*.txt` 全部轮次 |
| 只断言"没有 forged 行" | 消息根本没入队时也通过 | 要求子进程复述只存在于消息里的随机 token |
| 没在修复前的代码上跑过 | 无法证明测试真的抓得住缺陷 | `git stash` 后必须变红，见 §10 |

---

## 9. 一条命令的快查顺序

```bash
cd /d/git_repos/GenericAgent
export LANG=C.UTF-8 LC_ALL=C.UTF-8

# P：秒级，先排除路径分裂
python tests/probes/python/_probe_three_way_bus.py
python tests/probes/python/_probe_subagent_toolset.py

# U：契约
python -m unittest tests.test_subagent_notification_bus_path \
                   tests.test_subagent_notifications \
                   tests.test_subagent_notification_fold_point \
                   tests.test_subagent_tool_surface

# C：真实子进程 + 真实模型
python tests/probes/python/_probe_real_child_tools.py

# I：真实 bridge + 真实模型（最贵，最后跑）
GA_RUN_REAL_API_E2E=1 python tests/real_subagent_fold_e2e.py

# 全量
python -m unittest discover -s tests -t .
```

**顺序不能乱**：P 挂了就不要再往下跑，那是路径问题，跑多久都是错的。

---

## 10. 已知与本轮无关的既有失败（别再排查）

用 `git stash` 在未应用本轮改动的代码上复跑确认过：

| 用例 | 基线表现 |
| --- | --- |
| `test_workflow_runtime.WorkflowCancellationSemanticsTest.*` | 3 次复跑：2/3/3 失败（本身不稳） |
| `test_workflow_runtime.WorkflowRuntimeTest.test_runtime_timeout_cancels_running_child_agents` | 单独跑必失败 |
| `test_mcp_runtime.*`（fixture 冷启动相关） | 偶发，MCP 冷启动 7.6-8.0s 撞超时 |

判定方法：`git stash push -- genericagent/` → 复跑 → `git stash pop`。
**不经这一步不要把这些记到本轮改动账上。**

---

## 11. 双向验证：只有"修复前会红"的测试才算数（2026-11-08 补）

本手册 §1 写着"路径必须问代码要"，那一轮之后又出现了同类缺陷的第二批（7 处），
原因是：**我按"我遇到过的那条路径"去扫，而不是按表达式扫**。同一轮里我还写了两处假验收：

| 假验收 | 它为什么会绿 | 怎么发现的 |
| --- | --- | --- |
| 探针自己重新推导 `dirname(task_dir)/subagents` | 探针与产品共用同一个错误假设 | 改成调真实函数后结论反转 |
| 守卫测试用 tokenize 逐 token 拼行 | 每个 token 单独成行，`dirname(task_dir)` 永远拼不出来 | `git stash` 后仍绿 |

两条都是同一类错误：**验证代码本身没有经过验证**。

### 11.1 强制流程

```powershell
# 1. RED：先写测试，在未修复代码上跑
python -m pytest tests/test_child_bus_handdown_paths.py -q     # 必须变红

# 2. GREEN：写最小实现，跑绿
python -m pytest tests/test_child_bus_handdown_paths.py -q     # 必须全绿

# 3. 反向确认：把实现 stash 掉，测试必须重新变红
git stash push -- genericagent/core/app.py genericagent/agents/receipts.py
python -m pytest tests/test_child_bus_handdown_paths.py -q
git stash pop
```

第 3 步不能省。它抓出了本文档里记录的两处假验收，也抓出了"守卫范围过宽
（把与总线无关的 `dirname(dirname(task_dir))` 也算进去）"这个问题。

### 11.2 真实 E2E 同样要双向

层 C / 层 I 的真实模型跑动也要在 pre-fix 代码上跑一次。实测对比（`tests/real_child_handdown_delivery_e2e.py`）：

| 信号 | pre-fix | fixed |
| --- | --- | --- |
| `mailbox.jsonl` state | `forged` | `consumed` |
| 子进程复述 followup-only token | `False` | `True` |
| 孤儿 `tasks/sessions` | 存在 | 不存在 |

pre-fix 那次同时暴露了一个真实的坑：`followup accepted = True` 而消息其实被丢弃了——
**只看工具返回值不足以判断送达**。

### 11.3 判定送达的唯一硬信号

让消息里带一个**只存在于这条消息**的随机 token（不出现在 spawn prompt、任务名、
任何子进程可读的文件里），要求子进程把它复述到输出。然后：

- 合并该任务目录下**全部** `output*.txt`（第 N 轮写 `outputN.txt`，只读 `output.txt`
  会把正常投递误判成丢消息——本轮真的踩了）；
- 再叠加"没有 `forged` 行"作为辅助，但不能只靠它：消息根本没入队时它也是绿的。

### 11.4 一个能挡住复发的守卫

`tests/test_child_bus_handdown_paths.py::NoPathGuessRemainsTest` 用 AST 按函数作用域
检查 `os.path.join(os.path.dirname(task_dir), ...)`，白名单只有两个 helper 自己的
legacy 兜底。要点：

- **不要用文本匹配**：注释里会合法地提到旧表达式，纯文本匹配会让守卫因错误的原因失败，
  然后被下一个人删掉；
- **不要用 tokenize 逐 token 重建**：拼不回原表达式，守卫会在还有 7 处缺陷时保持全绿；
- 用 AST 限定"哪个函数里的哪种调用"，语义和缺陷本身对齐。
