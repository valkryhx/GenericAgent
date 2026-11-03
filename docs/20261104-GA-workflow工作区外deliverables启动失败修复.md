# GA workflow 并行 agent 全部 fail：工作区外 deliverables 启动即崩 修复记录

日期：2026-11-04
关联：最近 4 次提交 `ec2a880 / e48dbae / 62c3409 / 94a1022`；线上会话
`session_7bad95ff...`（2026-11-03 11:19，`/goal` + workflow 开启）。

---

## 1. 现象

用户开启 goal + workflow，让主 agent 写一个动态 workflow：对最近 4 个 commit
各自做只读审查、每个 commit 写一份 Markdown 报告到 `D:\git_codes\GenericAgent\bug\tests`，
最后汇总。结果 **4 个 agent 全部 fail**，一份报告都没出。

`run.json` / `journal.jsonl` 里 4 个 job 的轨迹完全一致：

```
agent_registered → agent_isolated(reason=implicit-parallel-writer, parallelBatch=4) → agent_failed
agent_failed.payload.error = "path is outside workspace: D:/git_codes/GenericAgent/bug/tests/commit_1_....md"
```

`tokenUsage` 为空、`toolCalls` 为空——**模型一步都没跑**就挂了。

关键元数据（`run.json`）：
- `metadata.pathPolicy = "unrestricted"`
- `metadata.workspacePolicy = "unrestricted"`
- 每个 job `options.deliverables = ["D:\git_codes\GenericAgent\bug\tests\...md"]`（工作区外绝对路径）
- 每个 job `isolation = "worktree"`、`isolationReason = "implicit-parallel-writer"`

workflow 脚本里 `args.outDir = D:\git_codes\GenericAgent\bug\tests`，模型据此为每个
agent 声明了工作区外的绝对 `deliverables`——**这是用户希望的行为**。

---

## 2. 根因（代码 bug，不是模型乱写路径）

设计契约（`skills/workflow-authoring/references/api.md`、`artifacts.md` 明确写死）：

- `pathPolicy` 默认 `"unrestricted"`：`workspacePath` 只是默认 cwd / artifact root，
  **文件/命令工具可以访问其他路径**。设 `"workspace"` 才是硬边界。
- `deliverables` / `writeScope` 是 **advisory**：驱动归属与 handoff，**不是**写 ACL，
  **不会**因为缺失或越界而 fail 一个 job。

运行时也确实遵守了这个契约：`child_agent._build_handler` 里
`workspace_root = child_cwd if path_policy == "workspace" else None`，即
`unrestricted` 下 `workspace_root=None`，`handler._check_workspace_path_access` 直接
`return None`，child 工具不会做工作区 ACL，能写工作区外。

**但启动前的目录预建 `_prepare_declared_workspace()` 违背了这个契约**：它把
`deliverables`/`writeScope` 里的每一项都无条件丢给 workspace-only 的
`resolve_workspace_child()`，遇到工作区外绝对路径抛 `WorkspacePathError`，
这个异常从 `_start_queued_jobs()` 的 `try: prepare; runner.start()` 冒出来，
被 `except Exception as exc: self._fail_job(job, ...)` 捕获 → **整个 job 直接 failed**。

于是出现了最讽刺的组合：**运行时明明允许写工作区外，但启动前的“预建父目录”这个小
便利动作，把合法的任务判成了非法并杀掉**。4 个并行 writer 全部踩中，所以全 fail。

---

## 3. 修复

`genericagent/workflow/scheduler.py::_prepare_declared_workspace`：
把“预建父目录”从“硬校验 + 硬失败”改成“尽力而为、绝不失败”：

- 路径在工作区内 → 照旧 `resolve_workspace_child` 后 `mkdir(parents=True)`；
- 路径在工作区外：
  - `pathPolicy=unrestricted`（默认）→ 按绝对路径 `mkdir` 预建父目录（child 工具
    本来就能写这里）；
  - `pathPolicy=workspace` → 直接跳过，**不创建、也不报错**；
- 任何单条路径解析/创建失败（`WorkspacePathError/OSError/ValueError`）→ 只跳过该条，
  **绝不向上抛**，因此再也不会把 job 判 failed。（child 真正写文件时若目录不存在，
  `file_write` 会自行创建父目录；预建只是省事。）

一句话：**启动前的便利动作不该拥有“否决整个 job”的权力。**

---

## 4. 验证（严格按《20261103 真实 LLM 测试策略：过程断言优先》）

### 4.1 确定性单测（不走 LLM，秒级、可回归）

在 `tests/test_workflow_scheduler.py` 新增 3 个用例：

- `test_prepares_outside_deliverable_parent_under_unrestricted`
- `test_start_path_does_not_fail_job_for_outside_deliverable`
- `test_workspace_policy_skips_outside_parent_without_failing`

有效性自证：在 **buggy 代码**上这 3 个全红（`'failed' == 'failed' : path is outside workspace`），
在 **修复后**全绿——是合格的回归测试，不是摆设。

### 4.2 真实 LLM 测试（过程证据优先，不只结果）

两个新增 e2e，均驱动真实默认 LLM `deepseek-v4.1-flash`：

`tests/real_workflow_external_dir_deliverables_e2e.py`（WorkflowRuntime 引擎）
- 3 个并行 writer，`deliverables` 在工作区外；断言：run/jobs 全 succeeded、
  无 `outside workspace` 错误、**每个 job output_tokens>0**、每个 job 跑过 writer 工具、
  3 份报告在工作区外、非空、mtime 属于本次运行、没有漏写到工作区内。
- 结果：**10/10 PASS**，jobs token 116/107/112，隔离原因 `implicit-parallel-writer`。

`tests/real_workflow_external_reports_via_tool_e2e.py`（真实 workflow 工具入口 = 线上形态）
- 走 `GenericAgentBridge.workflow_launch_from_tool(script, args)`，与 `ga ink` 里模型调用
  `workflow` 工具同一条路径；3 个隔离并行 agent 把 `review_*.md` 写到工作区外目录。
- 结果：**10/10 PASS**，run succeeded，jobs token 349/562/656，报告非空且本次新写入。

> 关键点：单看“最终 succeeded”是不够的——旧 bug 里模型根本没跑也会“结束”。
> 所以两个 e2e 都断言 **token>0 / 跑过 writer 工具 / mtime 属于本次 / 报告在工作区外**，
> 这些是“只设置状态不干活”骗不过去的过程证据。

### 4.3 回归面

- `tests/test_workflow*.py` 全量：**505 passed（skip 1）**
- scheduler/workspace/guard/path_acl/isolation/child 子集：**214 passed**

---

## 5. 影响面与边界

- 只改 `_prepare_declared_workspace` 一个方法；不触碰运行时 ACL、隔离收集、产物归属逻辑。
- `pathPolicy="workspace"` 的硬边界职责仍在运行时 `path_acl`（`workspace_root` 非空时生效），
  本次改动没有放松它；它只是不再因为“预建父目录”而误杀 job。
- `deliverables` 仍然不是写 ACL、不强制存在——与 `references/api.md` 的既有契约一致。

---

## 6. 教训（与 20261103 测试策略呼应）

这是又一次“**结果对/错被过程掩盖**”的反面教材的镜像：
- 线上：模型没跑（token 空、工具空）却“启动后立刻 failed”，因为失败发生在模型之前；
- 若只看 `run.status`，会误以为是“模型没干好”，其实是**宿主预建目录越权否决了任务**。

因此判断 workflow 类问题，一律先看三个过程指纹：**token 是否>0、工具是否真被调用、
失败枚举里是否有 `outside workspace` / `PathACL` 之类宿主层错误**，再谈结果。
