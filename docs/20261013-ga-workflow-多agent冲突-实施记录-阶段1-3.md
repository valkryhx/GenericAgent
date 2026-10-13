# GA workflow 多 agent 冲突优化：实施记录（阶段 1-3）

- 日期：2026-10-13
- 状态：**阶段 1 / 2 / 3 已实施，全量测试通过**
- 对应方案：`docs/20261013-ga-workflow-多agent冲突-对齐CC-worktree隔离与值传递.md`
- 上游证据仓库：`D:\git_codes\claudecode283_hack`（CC 2.1.283 源码 + 剖析文档）
- 一句话：把 GA workflow 的多 agent 协作从「事后冲突检测」改为 CC 的「从源头避免」——值传递为默认，
  并行写文件用 `isolation:"worktree"` 隔离，并让冲突检测只在归属可信时才报。

---

## 0. 背景回顾（为什么改）

CC 减少多 agent 冲突**不靠冲突检测**，靠两条（见方案 §1/§2.1）：

1. **值传递为默认**：`agent()` 返回文本或 schema 校验对象，`parallel()`/`pipeline()` 传值 → 天然无文件冲突。
2. **并行写文件才隔离**：`agent({isolation:'worktree'})` 给每个 agent 独立 git worktree，各写各的。

CC 全仓**没有** `artifactCollision`/`artifactOwnership`（grep 0 命中）。GA 原有的「共享 workspace + 事后 diff 归属」
必然产生**假冲突**：并行窗口内 A 写的文件会出现在 B 的 before/after diff 里，被算作 B 写的
（实测 run `wf_3332887439d547609a91dde829e89a43`：`author:eight_queens` 被归属了它没写的
`newton_method.py`/`primes_1_100.py`，但文件内容正确、未真覆盖）。

---

## 1. 阶段 1（核心）：workflow `agent()` 支持 `isolation`

**目标**：`agent(prompt, {isolation:'worktree'})` 让 child 在**私有工作目录**里跑，diff 落在私有目录内 → 归属天然正确。

改动：

| 位置 | 改动 |
| --- | --- |
| `workflow_runtime.py` `_handle_rpc`（agent 分支，`options = params.get("options") or {}` 之后） | 白名单校验：只接受 `isolation:"worktree"`；其余（含 `remote`）抛 `RuntimeError("agent({isolation:'<x>'}) is not available in this build")`，对齐 CC「remote 路径存在但不可达」。 |
| `workflow_child_agent.py` 新增 `_job_isolation(job)` | 从 `job.metadata["options"]["isolation"]` 读并归一化（返回 `"worktree"` 或 `None`）。 |
| `workflow_child_agent.py` 新增 `_shared_workspace(job)` | 返回 run 共享 workspace 路径（`job.metadata["workspacePath"]`）。 |
| `workflow_child_agent.py` 新增 `_isolated_workspace(job)` | 返回私有目录 `temp/workflow_child_agents/<runId>/<jobId>`（mkdir）。**按 runId+jobId 双键**，避免两个并发 run 都注册 `agent_1` 时目录串味。 |
| `workflow_child_agent.py` `_child_cwd(job)` | `isolation=="worktree"` 时返回私有目录；否则保持原逻辑（共享 workspace 或 per-job fallback）。 |
| `workflow_child_agent.py` `_run_tool_job`（diff 之后） | 若隔离：`written` 由**私有目录**的 before/after diff 得到，再调 `_collect_isolated_artifacts` 把产物按 workspace 相对路径拷回共享 workspace。 |
| `workflow_child_agent.py` 新增 `_collect_isolated_artifacts(job, isolated_root, written)` | `shutil.copy2` 把私有目录里 diff 出的文件复制回共享 workspace 的同名相对路径（父目录自动建）。 |
| `workflow_child_agent.py` 子代理系统提示 | `sharedWorkspace:` 文案条件化：隔离时改为 `isolatedWorkspace:`，告诉 child「你有私有目录，按声明的 workspace 相对路径写，会被回收」。 |

**隔离目录选址说明**：GA 的 run workspace 通常是 `temp/workflow-runs/<runId>/`，私有目录放在
`temp/workflow_child_agents/<runId>/<jobId>`（与 workspace 平级、不在其内），因此：
- child 的 workspace guard root = `_child_cwd` = 私有目录，写私有目录合法；
- 非隔离 sibling 的共享 diff 看不到私有目录，不产生噪声；
- 产物回收在父进程做（不受 guard 限制），能写回共享 workspace。

**未采用 git worktree**：本 build 的 run workspace 通常不是 git 仓库，`git worktree` 不适用；
GA 用「私有子目录」这一等价形态（方案 §3.2 已把 per-agent 子目录列为主路径）。

`optionsHash`（`workflow_scheduler.py:1500 _stable_hash(options)`）自动纳入 `isolation`，隔离/非隔离的
resume 缓存不会串味；`workflow_js_worker.js:92` 的 `agent:(prompt,options)=>rpc('agent',{prompt,options})`
本就透传 options，无需改 worker。

---

## 2. 阶段 2：修归属误判（即使不开隔离也不再假报）

**目标**：共享 workspace 下，只有**可信归属**才参与冲突判定。

**归属可信度模型**（`job.metadata["artifactConfidence"]`：`{path: "exact"|"declared"|"inferred"}`）：

| 取值 | 含义 | 触发条件 |
| --- | --- | --- |
| `exact` | 精确归属 | child 隔离（diff 落在私有目录），**或**该 job 是**所在 wave 的唯一 job**（无并发 sibling，diff 不可能被污染）。 |
| `declared` | 声明归属 | 非隔离且与 sibling 同 wave（并发窗口），但该路径在 job 的 `writeScope`/`deliverables` 里声明过。 |
| `inferred` | 低置信归属 | 非隔离、同 wave 并发、且路径未声明 → 可能是 sibling 的文件被本 job 的 diff 捕获，**不据此判冲突**。 |

改动：

| 位置 | 改动 |
| --- | --- |
| `workflow_scheduler.py` 新增 `_job_isolation(job)` / `_declared_write_paths(job, workspace)` / `_wave_sibling_count(job)` | 读隔离模式、归一化 `writeScope`+`deliverables`、统计同 wave job 数。 |
| `workflow_scheduler.py` `_record_observed_artifacts` | 计算并写入 `job.metadata["artifactConfidence"]`；`observedArtifacts` 结构**不变**（仍是 `{path, writer}` 列表），只额外挂一张置信度表，避免影响既有消费者与测试。 |
| `workflow_scheduler.py` `_record_artifact_collisions` | 用 `artifactConfidence` 分三档：①≥2 个**可信生产者**（exact/declared）→ `artifactCollisions`（会 degrade，附 `confidence` 字段）；②≥2 个生产者但可信≤1（典型=共享 diff 污染）→ **新增** `unverifiedWrites`（**不** degrade，只记录+事件 `unverified_write`）；③单生产者(+验证者)→ `verificationSideEffects`（保持原语义）。 |

**语义变化（重要）**：非隔离、并发、未声明的同路径写入，从「硬冲突（degrade）」降级为
`unverifiedWrites`（仅记录）。这正是 CC 的取舍——不在共享 workspace 里做冲突检测，而是要求
「声明（writeScope/deliverables）或隔离」。真正的硬冲突仍会被抓：两个**隔离** job 写同一路径（exact），
或两个 job 都**声明**了同一路径（declared）。

---

## 3. 阶段 3：提示词 / 文档对齐

`skills/workflow-authoring/SKILL.md`：

- 开头「不要发明 CC-only 字段」列表：`isolation` 从"不支持"里移除，改为「GA 支持 `isolation:"worktree"`（见 §4）」，
  并明确「不要用 `isolation:"remote"`（云端/会话隔离超出范围）」。
- §4 `agent()` 选项表：新增 `isolation` —— 仅 `"worktree"`；私有目录运行；产物回收回共享 workspace；
  **EXPENSIVE**，仅在「多个 agent 并行写文件且会互相覆盖」时才用；优先用 `schema` 传值而不是写文件。
- §5 新增模式 **Pass values, don't share files** —— analyze/research/verify 阶段优先返回 `schema` 结构化值，
  只让产出型阶段写文件；并行写文件必须 `isolation:"worktree"`，不要靠「后写覆盖」。
- §7 边界：改为「GA 支持 `isolation:"worktree"`，不实现 `budget`/`model`/`effort`/`agentType`/`disallowedTools`/`bashCommandClamp`」。

> 注：workflow 工具自身的 description（`assets/tools_schema.json` / `_cn.json`）保持高层描述，细节由
> `workflow-authoring` skill 承载（工具描述本就要求先加载该 skill），故未改工具描述。

---

## 4. 验收与测试

新增/更新 focused 测试（全部通过）：

- `tests/test_workflow_child_agent.py`
  - `test_isolation_uses_private_cwd_and_collects_back`：隔离时 `_child_cwd` ≠ 共享 workspace，且产物被回收。
  - `test_non_isolated_job_uses_shared_workspace`：非隔离用共享 workspace，`_job_isolation` 返回 `None`。
- `tests/test_workflow_runtime.py`
  - `test_runtime_accepts_worktree_isolation_option`：`agent(...,{isolation:'worktree'})` 透传到 job.metadata.options。
  - `test_runtime_rejects_unknown_isolation_option`：`isolation:'remote'` 抛 `is not available in this build`。
- `tests/test_workflow_scheduler.py`
  - `test_a_path_written_by_two_jobs_is_recorded_as_a_collision`（更新）：两个 job 都声明 `writeScope:["report.md"]` → 仍判 `artifactCollisions`。
  - `test_undeclared_shared_writes_are_not_a_hard_collision`（新增）：并发未声明同路径 → `unverifiedWrites`，**不** degrade。
  - `test_isolated_jobs_sharing_a_path_are_a_collision`（新增）：两个隔离 job 写同一路径 → `artifactCollisions`，`confidence="exact"`。

回归：

```
python -m unittest tests.test_workflow_scheduler tests.test_workflow_workspace tests.test_workflow_store   # 109 OK
python -m unittest tests.test_workflow_child_agent tests.test_workflow_runtime tests.test_workflow_integration \
    tests.test_workflow_verification tests.test_workflow_path_acl tests.test_workflow_workspace_guard          # 174 OK
python -m unittest discover -s tests -p "test_*.py"                                                        # 1331 OK (skipped=3)
```

---

## 5. 未做 / 后续（不属本批范围）

1. ~~**阶段 4（可选）**：隔离目录 + 允许受控 subprocess，解决缺陷 B/E。~~ → **已完成**，
   见 `docs/20261013-ga-workflow-受控subprocess-实施记录-阶段4.md`。
2. ~~**隔离目录清理**~~ → **已完成**（对齐 CC「worktree is auto-removed if unchanged」）：
   默认无改动删、有改动保留（`GA_WORKFLOW_ISOLATION_KEEP=changed`，另 `always`/`never`）；
   带守卫的 `_remove_isolated_workspace`；TTL 兜底 `prune_isolated_workspaces()`（`GA_WORKFLOW_ISOLATION_TTL_HOURS`，默认 168h）；
   见 `docs/20261013-ga-workflow-隔离目录清理方案.md` 与下方 §5.2。
3. ~~**run 级归属索引 `artifactOwnership` 未按置信度过滤**。~~ → **已完成**，见 §5.1。
4. **提示词默认值**：GA 目前仍需 workflow 作者显式声明 `writeScope`/`isolation`；未做「未声明则自动隔离」的隐式默认
   （避免在单写者常见场景下引入无谓开销）。
5. **CC 云端会话**：`isolation:"remote"` 明确不做，**复刻时暂时不需要参考 claudecode 云端会话的实现**。

### 5.1 补充（阶段 2b）：归属索引按置信度过滤

**动机**：阶段 2 让**冲突判定**忽略了 `inferred`，但 run 级归属索引 `artifactOwnership` 仍收录全部
`observedArtifacts`——于是 handoff 仍会宣称「`newton_method.py` 由 `author:eight_queens` 写」（它没写）。修冲突解决了
「假 degraded」，但「谁写的」标签仍误导。

**设计（避免误删正确归属）**：不「一律删 inferred」，而是——当一个路径**同时有可信归属**（exact/declared）时，
才丢弃该路径上的 `inferred` 声明；**只有 inferred 的路径保留**（单个 inferred 很可能就是真实但未声明的写者）。
- wf_3332：`newton_method.py` 的真实作者声明过（declared）→ 只丢 `eight_queens` 等多余 inferred，保住正确作者。
- 单写者未声明：只有一个 inferred → 保留，不误删。

**实现**：新增 `workflow_workspace.artifact_ownership_index(jobs)` 作为唯一实现；三处消费者改为调用它——
`workflow_store.build_artifact_ownership_index`（持久化索引 + progress）、`workflow_scheduler._artifact_owners_for`
（job handoff）、`frontends/ink_bridge._workflow_job_artifact_owners`（Ink handoff）。旧数据无 confidence → 默认
`exact`（可信），向后兼容。

**测试**：`tests/test_workflow_workspace.py`（3 例：丢 inferred / 保留 lone inferred / 缺省 exact）、
`tests/test_workflow_scheduler.py::test_ownership_index_drops_inferred_claims_in_a_real_run`。全量 discover **1344 OK (skipped=3)**。

---

### 5.2 补充（隔离目录清理，对齐 CC auto-removed if unchanged）

**动机**：阶段 1-3 的私有目录 `temp/workflow_child_agents/<runId>/<jobId>` 从不删除 → 无限增长。

**CC 证据**（仓库 `D:\git_codes\claudecode283_hack`）：
- 工具描述 `modules/m1502_0xdb4a986.js`：`the worktree is auto-removed if unchanged`。
- 实现 `reconstructed/deminified/m0511_0xcd25473.js:855-1013` `getWorktreeResult()`：无改动→移除；有改动→保留 + `Agent worktree kept at: <path>`；边界情况一律保留。
- 移除动作 `reconstructed/deminified/m0319_0xc61d343.js:80-170`：`git worktree remove --force`，删前有守卫（非主 worktree/非他机/无 reparse point/路径完好）→ fail-closed。

**实现**（`workflow_child_agent.py`）：
- env `GA_WORKFLOW_ISOLATION_KEEP` = `changed`(默认，对齐 CC) / `always` / `never`；非法值回退 `changed`。
- `_isolated_workspace_base()` / `_isolated_workspace_path()`；base 可用 env `GA_WORKFLOW_CHILD_AGENTS_ROOT` 覆盖（测试友好）。
- `_remove_isolated_workspace(job)`：四重守卫（严格在 base 之下、叶子 == jobId、父 == runId、非 symlink、是目录）+ `shutil.rmtree`，失败只吞掉；删后清空 runId 空壳。
- `_maybe_cleanup_isolated_workspace(job, written)` + `_finalize_isolated_workspace(job, tool_summary)`：在 `_run_job` 成功路径、**产物回收之后**调用；保留时写 `tool_summary["keptWorkspacePath"]`。
- 模块级 `prune_isolated_workspaces()`（TTL 兜底，默认 168h，`0`=关），`start()` 限频（≥600s）调用；兼容 `<runId>/<jobId>` 与裸 `<jobId>` 两种形态。
- 非隔离但无共享 workspace 的 fallback 私有目录同样纳入（无产物即删）。

**测试**：`tests/test_workflow_isolation_cleanup.py`（12 例：unchanged 删 / changed 默认留 + `keptWorkspacePath` / `never` / `always` / 非法值回退 / 非隔离不动 / 拒 base 外路径 / 拒 `..` 穿越 / 拒 symlink / TTL 删旧留新 / TTL=0 关 / base env 覆盖）。全量 discover 见提交。

---

## 6. 相关文档与证据

- 方案：`docs/20261013-ga-workflow-多agent冲突-对齐CC-worktree隔离与值传递.md`
- CC 侧证据：`D:\git_codes\claudecode283_hack\claudecode283\extracted\workflow-authoring-reference.md:37`、
  `...\modules\m0869_0xd12567a.js` @57526、`...\modules\m0531_0xcd4d7c8.js`、
  `D:\git_codes\claudecode283_hack\docs\20261012-复刻dynamic-workflow指南.md`
- GA 复用件：`subagent_worktree.py`、`ga.py:30 _resolve_subagent_isolation`
- 缺陷清点：`docs/20261012-ga-workflow-实测缺陷清点-待分析.md`（B/C/E）
