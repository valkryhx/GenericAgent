# GA workflow 隔离机制对齐 CC：worktree 隔离 + 值传递（补充调研）

- 日期：2026-10-14
- 状态：**调研 + 本轮实施**（实施记录见 §4）
- 上游证据仓库：`D:\git_codes\claudecode283_hack`（Claude Code 2.1.283 源码 + 剖析文档）
- 关联文档：
  - `docs/20261013-ga-workflow-多agent冲突-对齐CC-worktree隔离与值传递.md`（方案）
  - `docs/20261013-ga-workflow-多agent冲突-实施记录-阶段1-3.md`（阶段 1-3 实施记录）
  - `docs/20261013-ga-workflow-隔离目录清理方案.md`、`docs/20261013-ga-workflow-隐式默认隔离方案.md`

---

## 0. 一句话结论

CC 的「worktree 隔离」**不是「给 agent 一个空目录」**，而是「给 agent 一份**与主工作区同相对文件结构、独立的 git 工作副本**」，
并在 child 的系统提示里**显式告诉它这件事**。GA 阶段 1-3 把私有目录做成了**空目录**、提示词只有一句，
因此隔离 child 看不到共享 workspace 里已存在的文件，继承上下文里的路径也读不到 —— 本轮补齐这两处。

**【复刻时暂时不需要参考 claudecode 云端会话的实现】** —— `isolation:"remote"` 是云端路径，本 build 直接抛错，不复刻。

---

## 1. CC 证据（逐条源码坐标）

### 1.1 创建：只有显式 `isolation:"worktree"` 才建

`claudecode283/modules/m0869_0xd12567a.js`（workflow agent runner）：

```js
Je = null;                                    // 默认不开隔离，所有 agent 共享 cwd
if (ve?.isolation === "worktree" && nv(Ie))
  t("[web-fetch agent] isolation:'worktree' ignored; the built-in web-fetch agent always runs as a local agent");
else if (ve?.isolation === "worktree") {
  let E = g ? `${g}-${M}` : `wf-${M}`;        // 命名：<runId|label>-<agentId> 或 wf-<agentId>
  Je = await vt(E, { storageV5: D.storageV5, credentials: D.credentials });  // 建 worktree
}
let fn = Je?.worktreePath;                    // 传给 child runner 的 worktreePath
```

- **默认不开**：`Je = null` 初始，只有显式声明才建（GA 在此基础上加了「隐式默认隔离」增强，见 §3.2）。
- **特例**：内置 web-fetch agent 强制本地（忽略 `isolation`）。
- **`worktreePath` 透传**：`worktreePath: fn` 进入 child 的 `toolUseContext` / query 参数。

### 1.2 提示词注入：明确告诉 child「你在隔离副本里」（关键）

CC **两处**注入隔离提示，语义一致：

`claudecode283/modules/m0869_0xd12567a.js`（workflow 子代理，worktreePath 注入）：

> You are running in an isolated git worktree at `<worktreePath>` (a separate working copy of the repo).
> Changes you make here do NOT affect the main working directory (`<cwd>`) or other agents.
> Work normally — the worktree will be cleaned up automatically if you made no changes, or preserved for review if you did.

`claudecode283/modules/m0357_0xc63ba04.js`（Agent 工具 / 会话级 worktree 注入，信息更全）：

> You are running in an isolated git worktree at `${n}` — **same repository, same relative file structure, separate working copy**.
> **Paths in the inherited context refer to the parent's working directory; translate them to your worktree root.**
> **Re-read files before editing if the parent may have modified them since they appear in the context.**
> Your changes stay in this worktree and will not affect the parent's files.

要点（GA 要对齐的三条）：
1. **同仓库、同相对文件结构**（不是空目录）—— worktree 里能读到仓库里已有的文件。
2. **继承上下文里的路径是父工作目录的**，agent 要**换算到自己的 worktree 根**。
3. **编辑前重读**（父可能已改过它）。

### 1.3 生命周期 / 移除守卫（GA 已对齐的部分）

- 事件：`worktree_created` / `worktree_kept` / `worktree_removed`（`m0531_0xcd4d7c8.js`）。
- 状态探测 `qo()`（@4333）：`changedFiles`（`git status` 行数 + submodule work）、`commits`（`git rev-list --count <originalHeadCommit>..HEAD`）。
- 移除守卫（fail-closed）：
  - 非 owner（session 不拥有该 worktree）→ 拒绝，提示 `git worktree remove`。
  - 无法验证状态 → 拒绝，要求 `discard_changes: true`。
  - 有未提交文件 / 未合并 commit → 拒绝并列出，要求确认。
  - 移除动作：`git worktree remove --force`（`m0319_0xc61d343.js`，删前守卫：非主 worktree / 非他机 / 无 reparse point / 路径完好）。
- 移除后保留：`worktreePath` / `worktreeBranch` / `restoredCwd` / `originalCwdMissing`。
- `movedToChangeset`：commit 已在服务端存储（**云端，不复刻**）。

### 1.4 值传递为默认

`claudecode283/extracted/workflow-authoring-reference.md`：

> `agent(prompt, opts?)` — spawn a subagent. **Without schema, returns its final text as a string. With schema (a JSON Schema), the subagent is forced to call a StructuredOutput tool and `agent()` returns the validated object — no parsing needed.**

> `opts.isolation: 'worktree'` runs the agent in a fresh git worktree — **EXPENSIVE (~200-500ms setup + disk per agent), use ONLY when agents mutate files in parallel and would otherwise conflict; the worktree is auto-removed if unchanged.**

- `parallel()` 返回数组（barrier）、`pipeline()` 串值 —— **默认就是「值在 agent 之间流动」**，天然无文件冲突。
- 文件写只在「产出型阶段」出现，且并行写时用 `isolation:'worktree'`。

### 1.5 无「冲突检测」概念（关键否定证据）

```
grep -rl "artifactCollision"  claudecode283/modules/  → 0 命中
grep -rl "artifactOwnership"  claudecode283/modules/  → 0 命中
```

CC **全仓没有**事后冲突检测 / 归属索引 —— 它靠「值传递 + 按需隔离」**从源头避免**。GA 的
`artifactCollision`/`artifactOwnership` 是自创的**兜底**（隔离失效时才应触发）。

---

## 2. GA 现状与差距（本轮开工前）

| 项 | GA 现状 | CC | 差距 |
| --- | --- | --- | --- |
| 隔离目录内容 | `temp/workflow_child_agents/<runId>/<jobId>/`，**空目录**（`_isolated_workspace` 只 `mkdir`） | 同仓库**工作副本**（同相对文件结构） | ❌ 隔离 child 读不到共享 workspace 已有文件 |
| child 提示词 | 一句 `isolatedWorkspace:` 文案（「你有私有目录，按声明的相对路径写，会被回收」） | 明确讲「同相对结构 / 路径换算 / 编辑前重读 / 改动不影响父」 | ❌ 缺路径换算与重读语义 |
| 创建时机 | 显式 `isolation` **或**隐式（并行写者） | 仅显式 | ✅ GA 增强（保留） |
| 清理 | 无改动删、有改动留 + TTL 兜底 + 四重守卫 | auto-removed if unchanged + fail-closed 守卫 | ✅ 已对齐 |
| 值传递 | schema → `StructuredOutput` → 校验对象 | 同 | ✅ 已对齐 |

---

## 3. 本轮设计

### 3.1 隔离目录 = 共享 workspace 的「工作副本」（seed）

隔离 child 的私有目录在**创建时**用共享 workspace 的现有内容**预填一份副本**（对齐 CC「same relative file structure」）：

- 排除 host 自有目录（`HOST_OWNED_WORKSPACE_DIRS` = `__pycache__` / `.git` / `workflow-handoffs`），避免把控制面状态当产物。
- 只在目录**新建**时预填（幂等）；已有内容的目录不重复预填。
- 入口上限（`max_entries`，默认 20000，与 `snapshot_workspace` 一致）；超限跳过预填并记录，不阻塞 job。
- before/after diff 仍以**隔离目录**为准 —— 预填文件在快照里，只有被改动的才会进 `written`，回收逻辑不变。

效果：隔离 child 既能**读到**共享 workspace 的既有文件（对齐 CC），又**看不到**并发 sibling 的新写入（隔离保证不变）。

### 3.2 提示词对齐 CC 的三条语义

把隔离分支的 `isolatedWorkspace:` 文案扩写为 CC 的口径：

- 你有**一份独立工作副本**（与主工作区同相对文件结构）；
- 继承上下文里的路径是**主工作区**的，要**换算到你的工作副本根**；
- **编辑前重读**（父可能已改过）；
- 你的改动**不影响**主工作区或其它 agent；产物按声明的相对路径写，会被回收。

### 3.3 不做（明确排除）

- `isolation:"remote"` / 云端会话：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
- OS 级隔离（seatbelt / bubblewrap）：超出 workflow 复刻范围，本轮不做（见 AGENTS.md 遗留项）。

---

## 4. 实施记录

见 `AGENTS.md` 的「实施 2026-10-14：隔离目录预填（工作副本）+ 提示词对齐 CC」小节，以及
`docs/20261014-ga-workflow-隔离工作副本-实施记录.md`。

---

## 5. 相关证据坐标

- CC 创建：`claudecode283/modules/m0869_0xd12567a.js`（`isolation==="worktree"` 分支、`worktreePath:fn`）
- CC 提示词：`m0869_0xd12567a.js`、`m0357_0xc63ba04.js`（「isolated git worktree at ...」）
- CC 生命周期：`m0531_0xcd4d7c8.js`（`qo()` @4333、remove/keep 守卫）、`reconstructed/deminified/m0511_0xcd25473.js`（`getWorktreeResult()`）
- CC 移除动作：`reconstructed/deminified/m0319_0xc61d343.js`（`git worktree remove --force` 守卫）
- CC 代价声明 / 值传递：`claudecode283/extracted/workflow-authoring-reference.md`
- CC 无冲突检测：`grep -rl "artifactCollision\|artifactOwnership" claudecode283/modules/` → 0
- GA 复用件：`workflow_child_agent.py`（`_isolated_workspace` / `_child_cwd` / `_collect_isolated_artifacts` / `_build_prompt`）
