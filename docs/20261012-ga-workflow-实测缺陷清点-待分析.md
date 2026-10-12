# GA workflow 实测缺陷清点（待分析）

- 日期：2026-10-12
- 状态：**仅记录，未分析根因，未改代码**（次日分析用）
- 触发场景：`/workflow` 启动「5 路并行 author + 1 路独立 verify」任务
- 真实 runId：`wf_3332887439d547609a91dde829e89a43`
- 关联 session：`session_83a3cb3e1df34496b6d8c142304910ed`

> 本文档只做「把 bug 摆清楚、给出坐标与证据、标出待分析点」，**不含结论性修复方案**。
> 分析、定级、修复方案在次日单独进行，并另开实施记录。

---

## 0. 现象概述

用户实测时观察到两段输出：

1. 编排层（主代理）发出：

   ```text
   🔨 Tool: wait_agent
   { "condition": "workflow_terminal",
     "target": "wf_3332887439d547609a91dde829e89a43",
     "timeout_seconds": 300 }
   [Action] Waiting for subagent update (300s, condition=workflow_terminal).
   ```

   随后「感觉卡住」，长时间无输出。

2. 主代理事后「如实汇报」：

   ```text
   - 用 workflow 启动了 5 路并行 author + 1 路独立 verify（共 6 个子代理），runId wf_3332...
   - 4 个 Python 文件由子代理并行产出，verify 子代理已用 runpy 同进程执行，确认 exit=0 且输出符合规格。
   - qipao.html 未被产出：负责它的子代理（agent_2）只输出了推理文字，handoff 里没有落盘记录，属于"未产出却上报成功"。
     根因是子代理的写入受 workflow workspace guard 限制（子进程被禁，写入范围受限）。
   - 已知环境限制：workflow 子代理内 code_run 的 subprocess 被 guard 禁用，只能用 runpy 同进程执行做验证。
   ```

---

## 1. 缺陷清单（A~E）

| 编号 | 名称 | 类型 | 严重度（初判） | 状态 |
| --- | --- | --- | --- | --- |
| A | `wait_agent(condition="workflow_terminal")` 假实现，空等 timeout | Bug | 高 | 根因已确认，待修 |
| B | `workflow workspace guard` 全禁 `subprocess`，子代理无法真实运行代码 | 设计缺陷 | 高 | 待分析/出方案 |
| C | 并行 workflow `artifact_path_collision`，`writeScope` 未隔离 | Bug | 高 | 待分析 |
| D | 子代理「未产出却上报成功」，缺 artifact 存在性校验 | Bug | 高 | 待分析 |
| E | verify 被迫 `runpy` 同进程执行，验证强度打折 | 设计缺陷 | 中 | 待分析（与 B 相关） |

---

## 2. 缺陷详情

### A. `wait_agent(condition="workflow_terminal")` 假实现，空等 timeout

**现象**：`wait_agent(condition="workflow_terminal", target=<runId>)` 不返回，一直等到 300s timeout。

**已确认根因**：`workflow_terminal` 只是 `all_terminal` 的别名，只认 subagent task_name，**完全不读 workflow run 终态**。

**源码坐标**：

- `subagent_wait.py:78`：

  ```python
  elif condition in {"all_terminal", "workflow_terminal"}:
      satisfied = bool(observed) and len(turn_terminal) == len(observed)
      remaining = [str(_value(state, "task_name")) for state in observed if not _turn_terminal(state)]
      terminal = [str(_value(state, "task_name")) for state in turn_terminal]
  ```

- `subagent_manager.py:33`：`normalize_task_name()` 接受 `wf_...`（lowercase + 数字 + 下划线合法），把 runId 当 task_name。
- `subagent_manager.py:609`：`probe_agent()` 找不到 `state.json` 时 `raw = {}`，`_refresh_state()` 合成非终态（`turn_status="running"`, `process_status="exited"`），于是永不满足。
- `ga.py:1098`：`do_wait_agent()` 把 `target` 原样交给 `SubagentManager.wait_agents()`，无 workflow runId 解析。
- `assets/tools_schema_cn.json:100-106`：schema 暴露了 `workflow_terminal`，但未说明 target 语义。

**真实证据**：run 实际早已结束（`status=degraded`，6/6 terminal），wait_agent 却对着孤儿 state 空等（`temp/subagents/wf_3332.../state.json` 为合成状态，`turn_status=running`、`process_status=exited`、`updated_at=19:01:25`，无人推进）。

**待分析点**：

- `workflow_terminal` 的预期语义（文档 `docs/20261001-workflow-reference-architecture-comparison.md:683` 记为「workflow run 达到 succeeded/failed/aborted/killed」）。
- 终态集合是否包含 `degraded`。
- runId 不存在时应立即返回还是报错。
- 是否同步修 `assets/tools_schema*.json` 与提示词。

---

### B. `workflow workspace guard` 全禁 `subprocess`

**现象**：workflow 子代理里任何 `subprocess.*` 抛 `PermissionError: workflow code_run subprocess is disabled by workspace guard`；子代理无法 `python xxx.py`、`pip`、`git` 等，只能退化成 `runpy` 同进程执行。

**源码坐标**：

- `workflow_workspace_guard.py:74-82`：

  ```python
  def deny_process(*args, **kwargs):
      raise PermissionError("workflow code_run subprocess is disabled by workspace guard")

  subprocess.Popen = deny_process
  subprocess.run = deny_process
  subprocess.call = deny_process
  subprocess.check_call = deny_process
  subprocess.check_output = deny_process
  ```

- `workflow_workspace_guard.py:12`：`install()` 还 patch 了 `builtins.open` / `os.open` / `os.remove` / `os.mkdir` / `os.makedirs` / `os.rename` / `os.replace` / `shutil.rmtree`，全部限定在 workspace 内。
- `assets/code_run_header.py`：检测 `GA_WORKFLOW_WORKSPACE_ROOT` 后 `install(...)`。
- `ga.py:64`：`workspace_root` 下只允许 Python 类型 `code_run`，shell 类型直接拒绝。

**设计意图（来自 GA 文档 `docs/20261009-ga-workflow-stability-stepcode-borrow.md`）**：对齐 Step-Code 的路径 ACL，做「best-effort in-process guard」，限制子代理只能碰自己 run 的 workspace。

**待分析点**：

- guard 是「尽力而为」的进程内 patch，是否可被 `os.system` / `os.spawn*` / `ctypes` / `importlib.reload` 绕过（安全有效性）。
- 「全禁 subprocess」与「让子代理真实运行/验证代码」这一核心用例是否直接冲突。
- 是否偏离 Claude Code（CC 的 workflow 子代理可在 workspace 内自由跑命令）。
- 正确形态是否是「允许子进程 + 限定 cwd/可写路径 + 危险命令 denylist」或 OS 级隔离，而非全禁。
- 与宿主侧 `workflow_path_acl.py` 的职责边界是否需要重新划分。

---

### C. 并行 workflow `artifact_path_collision`

**现象**：并行 author 互相写同一文件，run 判 `degraded`。

**证据**：`wf_3332.../run.json` → `workflowIssues` / `artifactCollisions`：

```json
{
  "code": "artifact_path_collision",
  "message": "wf_parallel_demo/eight_queens.py was written by multiple jobs: author:eight_queens, author:primes",
  "path": "wf_parallel_demo/eight_queens.py",
  "writers": ["author:eight_queens", "author:primes"]
}
```

冲突文件（来自 `run.json`）：

- `wf_parallel_demo/eight_queens.py` ← `author:eight_queens`, `author:primes`（及其它）
- `wf_parallel_demo/newton_method.py` ← 5 个 author
- `wf_parallel_demo/primes_1_100.py` ← 4 个 author
- `wf_parallel_demo/poker_suit.py` ← `author:qipao_html`, `author:newton`

**待分析点**：

- 每个 author 都声明了 `writeScope: [t.file]`，为何仍能互相写对方的文件（隔离在哪一层失效）。
- `writeScope` 是否只是声明/提示，未在工具层强制执行。
- 是否需要给每个并行 agent 独立工作目录（worktree / 子目录），再合并。

---

### D. 子代理「未产出却上报成功」

**现象**：负责 `qipao.html` 的 agent_2 只输出推理文字，handoff 无落盘记录，job 却计 `succeeded`。

**证据**：

- `run.json` → `childSummary.succeeded = 6`（含 agent_2）。
- verify 子代理结论（`result-agent_6.json`）：`qipao.html` 不存在，全盘 `os.walk` 无果，`overallOk: false`。
- agent_2 的 handoff 仅含被截断的推理文字（`Turn 1 ...`），无 `runCommand` / `observedOutput`。

**待分析点**：

- 声明 artifact 缺失时，job/run 的终态是否应 fail-closed（`failed`/`degraded`）。
- 成功判定是否只看了「agent 自述」，未做宿主侧产物存在性校验。
- 与 `docs/20261009-ga-workflow-stability-stepcode-borrow.md` 中「degraded 终态语义」的关系（是否已部分实现但未覆盖此路径）。

---

### E. verify 被迫 `runpy` 同进程执行

**现象**：verify 子代理无法用子进程真实运行，只能 `runpy.run_path(..., run_name="__main__")` 同进程执行。

**证据**：`result-agent_6.json` 自述：

```text
Subprocess execution is blocked by the workspace guard (PermissionError: workflow code_run subprocess is disabled by workspace guard)
— same limitation the authors hit — so I executed each file's real __main__ entry in-process via runpy.run_path(...)
```

**待分析点**：

- 同进程执行只对「纯自包含、不调用外部命令」的脚本成立；一旦脚本内部用 `subprocess` 或依赖隔离的全局状态，结论不可靠。
- 这是 B 的连带后果，还是需要独立处理。
- verify 的「真实运行」契约如何定义与保证。

---

## 3. 真实 run 证据汇总

| 项 | 值 |
| --- | --- |
| run 目录 | `temp/sessions/session_83a3cb3e1df34496b6d8c142304910ed/workflows/wf_3332887439d547609a91dde829e89a43/` |
| `run.json` | `status: "degraded"`，`childSummary` 6/6 terminal，`executionOutcome: "succeeded"` |
| `final-result.json` | `status: "degraded"`，含 `workflowIssues` / `artifactCollisions` |
| `workflow-progress.json` | 6 个 `workflow_agent` 进度条目，均 `succeeded` |
| handoffs | `temp/workflow-runs/wf_3332.../workflow-handoffs/result-agent_1..6.json` |
| 孤儿 subagent state | `temp/subagents/wf_3332887439d547609a91dde829e89a43/state.json`（`turn_status=running`, `process_status=exited`） |

**关键事实**：workflow run **并非卡死**，实际早已到终态（degraded）；「卡住」是 `wait_agent` 缺陷 A 造成的观感。

---

## 4. 与 Claude Code 的对照备注

> 【复刻时暂时不需要参考 claudecode 云端会话的实现】

- CC 的 workflow 子代理可在 workspace 内自由运行命令（对应本文档 B/E）。
- CC 对「声明产物缺失」为 fail-closed 语义（对应 D）。
- CC 的并行 agent 隔离边界更明确（对应 C）。
- 以上对照仅作背景标注，**具体是否对齐、如何对齐留待次日分析**。

---

## 5. 次日分析建议顺序（仅建议，非方案）

1. A：根因已确认，先定终态语义与 runId 解析，可直接修。
2. B / E：一起分析（guard 与验证能力耦合），需先出改造方案。
3. C：确认 `writeScope` 在工具层的执行情况。
4. D：确认产物存在性校验的落点（scheduler / runtime / artifact verification）。

---

## 6. 相关文档索引

- `docs/20261009-ga-workflow-stability-stepcode-borrow.md`（guard / 路径 ACL /
