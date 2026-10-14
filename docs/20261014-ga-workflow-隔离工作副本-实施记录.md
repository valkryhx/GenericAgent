# GA workflow 隔离目录「工作副本」实施记录（对齐 CC worktree 语义）

- 日期：2026-10-14
- 状态：**已实施，focused 测试通过**
- 对应调研：`docs/20261014-ga-workflow-worktree隔离-CC机制补充调研.md`
- 上游证据仓库：`D:\git_codes\claudecode283_hack`（CC 2.1.283）
- 一句话：把 GA 隔离 child 的私有目录从「空目录」改成「共享 workspace 的工作副本（同相对文件结构）」，
  并把 child 提示词对齐 CC 的「同相对结构 / 路径换算 / 编辑前重读」三条语义。

---

## 0. 为什么改

CC 的 `agent({isolation:'worktree'})` 给 child 的**不是空目录**，而是
「a separate working copy of the repo」——**同仓库、同相对文件结构**，能读到仓库里已有的文件
（证据见调研文档 §1.2，原文在 `m0869` / `m0357`）。

GA 阶段 1-3 把隔离目录做成了**空目录**（`_isolated_workspace` 只 `mkdir`），于是隔离 child：
- 读不到共享 workspace 里已存在的文件；
- 继承上下文里的路径（指向主工作区）在它自己的目录里不存在 → 更容易「找不到文件」。

---

## 1. 改动 1：隔离目录在创建时预填「工作副本」

**文件**：`workflow_child_agent.py`

- `_isolated_workspace(job)`：新增 `fresh = not workspace.exists()`；仅当**新建**时调
  `_seed_isolated_workspace(job, workspace)`（幂等，已存在的目录不重复预填）。
- 新增 `_seed_isolated_workspace(job, target)`：
  - 取 `_shared_workspace(job)`（run 的共享 workspace）；没有就跳过（纯私有目录场景不受影响）。
  - `src_root.rglob("*")` 逐个**文件**复制到 `target` 的同相对路径（`shutil.copy2`）。
  - **排除 host 自有目录** `HOST_OWNED_WORKSPACE_DIRS`（`__pycache__` / `.git` / `workflow-handoffs`），
    避免把控制面状态当产物。
  - **入口上限** `_ISOLATED_SEED_MAX_ENTRIES = 20000`（与 `snapshot_workspace` 默认一致），超限即停。
  - 任何 `OSError` 都吞掉：最坏情况 child 拿到一个**部分/空**的私有目录，**绝不因预填失败而失败 job**。

**为什么不破坏隔离保证**：只复制 **job 启动那一刻**的快照；并发 sibling 之后的写入不会出现在这里，
before/after diff 仍以**隔离目录**为准，回收逻辑不变（被改动的文件才进 `written`）。

**为什么不用 `git worktree`**：GA 的 run workspace（`temp/workflow-runs/<runId>/`）通常不是 git 仓库，
`git worktree` 不适用；「复制工作副本」是等价形态（方案 §3.2 已把子目录列为主路径）。

---

## 2. 改动 2：child 提示词对齐 CC 三条语义

**文件**：`workflow_child_agent.py` `_build_prompt`（隔离分支文案）

原：
> isolatedWorkspace: you have your own private working directory for this job; other jobs cannot see or
> overwrite your files. Write each artifact at its declared workspace-relative path and it will be
> collected into the shared workspace.

新（对齐 CC 的 `m0357` 原文语义）：
> isolatedWorkspace: you are running in an isolated working copy at your workspacePath -- **the same relative
> file structure** as the shared run workspace, but a separate copy. Files that already existed when this job
> started are present here; concurrent jobs' new writes are not. **Paths in your inherited context refer to the
> shared workspace, so translate them to your workspacePath root**, and **re-read a file before editing it** if it
> may have changed since it appeared in context. Your changes stay in this copy and **do NOT affect the shared
> workspace or other agents**; write each artifact at its declared workspace-relative path and it will be
> collected back into the shared workspace.

---

## 3. 改动 3：SKILL 契约同步

**文件**：`skills/workflow-authoring/SKILL.md`

`isolation` 条目补上「working copy / same relative file structure / pre-filled at job start /
translate inherited paths」的说明，让作者知道隔离目录**能读到既有文件**（此前只讲「私有目录」）。

---

## 4. 测试（focused，未跑 full）

- `tests/test_workflow_child_agent.py` 新增 2 例：
  - `test_isolated_workspace_is_seeded_with_working_copy`：隔离目录含 `existing.txt`/`sub/keep.py`
    同相对结构；`workflow-handoffs/` **未**被复制；新建后再写共享 workspace 不回流（幂等 + 快照语义）。
  - `test_isolated_prompt_states_same_structure_and_path_translation`：提示词含
    `isolated working copy` / `same relative file structure` / `translate them to your workspacePath root` /
    `re-read a file before editing`，且不含 `sharedWorkspace:`。
- 命令与结果：
  - `python -m pytest tests/test_workflow_child_agent.py tests/test_workflow_isolation_cleanup.py -q`
    → **69 passed, 6 subtests passed**。
  - `python -m pytest tests/test_workflow_implicit_isolation.py tests/test_workflow_workspace.py tests/test_workflow_scheduler.py -q`
    → **101 passed, 10 subtests passed**。

---

## 5. 未做（明确排除）

- `isolation:"remote"` / 云端会话：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
- OS 级隔离（seatbelt / bubblewrap）：超出 workflow 复刻范围，见 `AGENTS.md` 遗留项。
- 清理 / 移除守卫 / 值传递：**已对齐 CC**（阶段 1-3 + 隔离目录清理），本轮不动。
