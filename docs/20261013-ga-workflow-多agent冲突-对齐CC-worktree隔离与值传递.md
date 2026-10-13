# GA workflow 多 agent 冲突优化方案：对齐 CC「worktree 隔离 + 值传递」

- 日期：2026-10-13
- 状态：**阶段 1-3 已实施**（实施记录：`docs/20261013-ga-workflow-多agent冲突-实施记录-阶段1-3.md`）；阶段 4 待办。
- 目标：把 GA workflow 的多 agent 协作冲突，从「事后检测」改为「从源头避免」，对齐 Claude Code 2.1.283 dynamic workflow。
- 关联缺陷：C（`artifact_path_collision` 归属误判）、B/E（workspace guard 禁 subprocess）
- 上游证据仓库：`D:\git_codes\claudecode283_hack`（CC 2.1.283 源码 + 剖析文档）

---

## 1. 结论（TL;DR）

CC 减少多 agent 冲突**不靠冲突检测**，靠两条：

1. **值传递为默认**：`agent()` 返回文本或 schema 校验对象，`parallel()`/`pipeline()` 传值 —— 天然无文件冲突。
2. **并行写文件时才隔离**：`agent({isolation:'worktree'})` 给每个 agent 一个独立 git worktree —— 各写各的，不冲突。

**CC 全仓没有 `artifactCollision` / `artifactOwnership` 概念**（grep 命中 0）；那是 GA 自创的「事后检测」。
GA 现在的做法（共享 workspace + 事后 diff 归属）必然产生**假冲突**。

**优化方向**：给 workflow `agent()` 加 `isolation`（复用 GA 已有的 subagent worktree 能力）+ 把值传递设为默认 + 修掉归属误判。

---

## 2. 证据

### 2.1 CC 怎么做（源码坐标）

| 项 | 证据 |
| --- | --- |
| 文档原文 | `claudecode283/extracted/workflow-authoring-reference.md:37`：`opts.isolation:'worktree'` runs the agent in a fresh git worktree — **EXPENSIVE (~200-500ms setup + disk per agent), use ONLY when agents mutate files in parallel and would otherwise conflict; the worktree is auto-removed if unchanged.** |
| 创建 | `claudecode283/modules/m0869_0xd12567a.js` @57526：`else if(ve?.isolation==="worktree"){let E=g?`${g}-${M}`:`wf-${M}`;Je=await vt(E,{storageV5:D.storageV5,credentials:D.credentials})}` → `let fn=Je?.worktreePath` → 传给 child runner |
| 默认不开 | 同处 `Je=null` 初始；只有显式 `isolation:"worktree"` 才建。默认所有 agent 共享 cwd |
| 特例 | `if(ve?.isolation==="worktree"&&nv(Ie)) t("[web-fetch agent] isolation:'worktree' ignored; ...")` —— 内置 web-fetch agent 强制本地 |
| worktree 模块 | `claudecode283/modules/m0531_0xcd4d7c8.js`：`.claude/worktrees/`、`worktree_created`/`worktree_kept`/`worktree_removed`、`changedFiles/commits/movedToChangeset` |
| remote 路径 | `isolation:"remote"` 本 build 直接抛错（死代码）。**【复刻时暂时不需要参考 claudecode 云端会话的实现】** |
| 无冲突检测 | `grep -rl "artifactCollision\|artifactOwnership" claudecode283/modules/` → **0 命中** |
| 值传递 | `workflow-authoring-reference.md:37`：无 schema 返回最终文本；有 schema 强制 `StructuredOutput`，返回**已校验对象**；`parallel()` 返回数组（barrier），`pipeline()` 串值 |

### 2.2 GA 现状与根因

| 项 | GA 坐标 / 证据 |
| --- | --- |
| workflow `agent()` 无 isolation | `workflow_runtime.py:476` 只读 `label` / `requiredTools` / `capabilities`；`isolation` 未被识别 |
| 并行 child 共享 workspace | `workflow_child_agent.py:971 _child_cwd(job)`：有 `job.metadata["workspacePath"]` 就返回**共享**目录；否则才用 per-job 目录 `temp/workflow_child_agents/<jobId>` |
| 归属 = 共享 workspace 的 before/after diff | `workflow_child_agent.py:733-758`：`workspace_before=snapshot_workspace(共享root)` … `written=diff_workspace(...)` → `tool_summary["writtenPaths"]` |
| 归属记录 | `workflow_scheduler.py:1329 _record_observed_artifacts`（用 writtenPaths + job label 记 owner） |
| 假冲突产生 | `workflow_scheduler.py:1409 _record_artifact_collisions`：同路径 >1 producer → `artifact_path_collision` |
| 实测（run wf_3332…） | `author:eight_queens` 的 `observedArtifacts` = `eight_queens.py` + `newton_method.py` + `primes_1_100.py`（后两个它没写）；`author:qipao_html` 被归属 4 个文件却没有自己的 `qipao.html`。**但文件内容正确（未真覆盖）→ 纯归属误判** |

### 2.3 GA 已有可复用件（降低实施成本）

| 复用件 | 位置 | 用途 |
| --- | --- | --- |
| 子代理 worktree | `subagent_worktree.py:89 create_subagent_worktree` / `:135 remove_subagent_worktree` | Agent 工具 `isolation:"worktree"` 已在用 |
| isolation 白名单校验 | `ga.py:30 _resolve_subagent_isolation` | 只接受 `worktree`，只读 profile 自动降级 |
| per-job 目录 fallback | `workflow_child_agent.py:971` | 已有隔离目录雏形 |
| schema 值传递 | `workflow_scheduler.py:156 validate_agent_payload_against_schema` + `_apply_schema_contract` | 结构化返回 |
| DSL 透传 options | `workflow_js_worker.js:92`（`agent:(prompt,options)=>rpc('agent',{prompt,options})`） | 加 isolation 无需改 worker |

---

## 3. 对齐设计

### 3.1 三条原则

1. **值传递优先**：analyze/read 类阶段默认返回结构化值（schema），不写共享文件。
2. **并行写文件 → 隔离**：写文件的并行 agent 必须各自独立工作目录，杜绝共享写。
3. **不靠事后检测**：`artifactCollision` 退化为「隔离失效时的告警」，不再是主要防线。

### 3.2 隔离模式（对齐 CC 的 `isolation:'worktree'`）

- 语义白名单：**只接受 `isolation:"worktree"`，其余取值直接拒绝**（对齐 CC，`m0869` 只认 worktree/remote，remote 已禁）。
- 隔离实现二选一（按 workspace 是否 git 仓库）：
  - workspace 是 git 仓库 → `git worktree`（复用 `subagent_worktree`）；
  - 否则 → **per-agent 子目录** `<workspace>/agents/<jobId>/`（GA 的 workflow workspace 是 `temp/workflow-runs/<runId>/`，通常非 git 仓库，这是主路径）。
- child 的 `_child_cwd(job)` 在隔离模式下返回隔离目录；before/after diff 在**隔离目录**内做 → 归属正确，无假冲突。
- 完成后：把隔离目录里**声明的**产物收集/合并回 run workspace 的对应路径（`deliverables`/`writeScope`），其余保留在隔离目录并报告路径。
- 代价：与 CC 一样是「昂贵」选项 —— 默认不开，仅当并行 agent 写文件时开。

### 3.3 值传递

- 已有 schema 能力；补：workflow 提示词/工具描述里，把「返回结构化值」作为 analyze/read 阶段的推荐形态，把「写文件」限定为产出型阶段。

---

## 4. 实施步骤（分阶段）

> 每个阶段独立可验收、可回退；先 `--only`/单测抽查再全量。

### 阶段 1（核心）：workflow `agent()` 支持 `isolation`

1. **runtime 识别**：`workflow_runtime.py:_handle_workflow_rpc`（agent 分支，@476 附近）读 `options.get("isolation")`，做白名单校验（只接受 `worktree`，其余抛错，文案对齐 CC）。
2. **job 记录**：`workflow_scheduler.register_agent` 把 `isolation` 存进 `job.metadata`（并纳入 resume 缓存键 `optionsHash`，避免隔离/非隔离结果串味）。
3. **隔离目录**：`workflow_child_agent._child_cwd(job)` 增加分支——`job.metadata.isolation=="worktree"` 时返回隔离目录（git worktree 或 `<workspace>/agents/<jobId>/`），并确保目录存在。
4. **diff 落在隔离目录**：`workflow_child_agent` 的 `snapshot_workspace/diff_workspace` 改用隔离目录 root（即 `_child_cwd`），归属自然正确。
5. **产物收集**：job 完成后，把隔离目录里命中的 `deliverables`/`writeScope` 复制回 run workspace；更新 `observedArtifacts` 为收集后的相对路径。
6. **清理**：复用 `remove_subagent_worktree`；unchanged 自动移除（对齐 CC）。
7. **验收**：并行写不同文件的 run，`artifactCollisions` 为空；每个 job 的 `observedArtifacts` 只含它自己写的文件。

### 阶段 2：修归属误判（即使不开隔离也正确）

- `_record_observed_artifacts` 在**非隔离**模式下，不再把共享 diff 全量算作本 job 写入；改为：
  - 只认本 job `deliverables`/`writeScope` 命中的路径，或
  - 标记并行窗口内的归属为「低置信」，不据此判 collision。
- `_record_artifact_collisions` 仅对**隔离失效**（真共享写）才报；加 `confidence` 字段。

### 阶段 3：值传递默认化 + 提示词/文档

- 更新 workflow authoring skill / 工具描述 / 系统提示：
  - analyze/read 阶段优先返回 schema 结构化值；
  - 并行写文件必须用 `isolation:"worktree"`；
  - 说明代价（~200-500ms + 磁盘/agent），避免滥用。
- 更新 `docs/GA_workflow_user_guide.md`。

### 阶段 4（可选）：与缺陷 B/E 联动

- 隔离目录 + 允许受控 subprocess（解决 guard 全禁 subprocess 导致 verify 只能 runpy 的问题，见缺陷 B/E）。

---

## 5. 验收标准

1. 并行 workflow（N 个 author 各写自己的文件）跑完：`artifactCollisions` 为空，`childSummary` 无 degraded 由 collision 触发。
2. 每个 job 的 `observedArtifacts` = 它真实写的文件集合。
3. `isolation:"worktree"` 生效时，各 agent 在独立目录运行（可从 transcript/日志验证 cwd）。
4. `isolation` 非法取值被拒（白名单）。
5. 回归：`python -m unittest discover -s tests` 全绿；新增 focused 测试覆盖隔离归属。

---

## 6. 风险与回退

| 风险 | 缓解 |
| --- | --- |
| 隔离目录产物收集遗漏 | 收集按 `deliverables`/`writeScope` 精确匹配；未命中保留并报告路径 |
| git worktree 对非仓库 workspace 不适用 | 主路径用 per-agent 子目录；git worktree 仅当 workspace 是仓库 |
| resume 缓存串味 | isolation 纳入 `optionsHash` |
| 增加磁盘/耗时 | 默认不开，仅并行写文件时开（对齐 CC 的代价声明） |
| 与现有 artifactOwnership 逻辑冲突 | 隔离模式下以隔离目录 diff 为准，覆盖旧归属 |

---

## 7. 相关文档

- CC 侧：`D:\git_codes\claudecode283_hack\claudecode283\extracted\workflow-authoring-reference.md:37`、`...\dynamic-workflow-internals.md`、`...\modules\m0869_0xd12567a.js`、`...\modules\m0531_0xcd4d7c8.js`
- CC 复刻指南：`D:\git_codes\claudecode283_hack\docs\20261012-复刻dynamic-workflow指南.md`
- GA 侧：`AGENTS.md`（2026-10-13 中途调研记录）、`docs/20261012-ga-workflow-实测缺陷清点-待分析.md`（缺陷 C/B/E）
- GA 复用件：`subagent_worktree.py`、`workflow_child_agent.py`、`workflow_scheduler.py`、`workflow_runtime.py`、`workflow_js_worker.js`
