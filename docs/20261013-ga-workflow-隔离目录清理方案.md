# GA workflow 隔离目录清理方案（对齐 CC「worktree is auto-removed if unchanged」）

> 目标读者：后续实施者（Codex / 人）。本文件是**方案**，落盘后再实施。
> 上游证据仓库：`D:\git_codes\claudecode283_hack`（Claude Code 2.1.283 源码 + 剖析文档）。
> 本方案对应 GA 阶段 1-3 里刻意留白的「隔离目录清理」。

---

## 1. 背景与问题

阶段 1-3（提交 `06445a3`）给 GA 的 workflow `agent()` 加了 `isolation:"worktree"`：
每个隔离子代理在自己的私有目录里跑，diff 只落在私有目录，跑完把产物**回收**到共享 workspace。

私有目录路径：`temp/workflow_child_agents/<runId>/<jobId>`（见 `workflow_child_agent._isolated_workspace`）。

**问题**：这个目录**当前从不删除**。阶段 1 是刻意留白（避免递归删除风险），但后果是：

- 每次隔离运行都会在 `temp/workflow_child_agents/` 下累积目录，长期运行会无限增长（磁盘泄漏）。
- 与 CC 的行为不一致：CC 的 worktree **跑完无改动会自动清理**。

本方案把 GA 的清理行为对齐 CC。

---

## 2. CC 的权威证据（2.1.283）

### 2.1 工具描述（作者可见的契约）

`claudecode283/modules/m1502_0xdb4a986.js`（原始字符串，grep -a 可复现）：

```
opts.isolation: 'worktree' runs the agent in a fresh git worktree — EXPENSIVE
(~200-500ms setup + disk per agent), use ONLY when agents mutate files in parallel
and would otherwise conflict; the worktree is auto-removed if unchanged.
```

关键句：**the worktree is auto-removed if unchanged**（无改动 → 自动移除）。
即：清理是「按需」的——**只有当 worktree 没有改动时才自动删**；有改动则保留。

### 2.2 实现（agent 结算时的 `getWorktreeResult`）

`reconstructed/deminified/m0511_0xcd25473.js:855-1013`（Agent 工具 = subagent spawn 路径）：

```js
if (effectiveIsolation_2 === "worktree") {
  Q = await sDe(lSr(agentId_2), {...});      // 建 worktree
  ...
}
...
getWorktreeResult = async () => {            // 结算钩子
  if (!Q) return {};
  ...
  if (hookBased && (baseChangeset === void 0 || vettedRealPath === void 0))
      return t(`Hook-based agent worktree kept at: ${r}`), { worktreePath: r };   // 保留
  if (nbr(taskRegistry.get(agentId_2)))
      return t(`Agent worktree kept at ${r}: backgrounded owner awaits keepalive, resume pending`), {...};  // 保留
  if (headCommit || baseChangeset !== void 0) {
    if (!(headCommit ? await UAt(r, headCommit)
                     : await qve(r, ...).then(O => O.dirty || O.checkoutMoved === !0))   // 判定「有没有改动」
        && (await j8(r, worktreeBranch, gitRoot, hookBased ?? !1, "agent_tool")).outcome === "removed") {
      return Flr({ agentId, removedWorktreePath: r, ... });    // 无改动 → 移除
    }
    ...
  }
  if (gitRoot) await n3(r, gitRoot);
  return t(`Agent worktree kept at: ${r}`), { worktreePath: r, worktreeBranch };  // 有改动 → 保留并报告路径
};
```

结论（三条语义）：

1. **无改动 → 移除**（`outcome === "removed"`）。
2. **有改动 → 保留**，并打日志 `Agent worktree kept at: <path>`，把 `worktreePath`/`worktreeBranch` 返回给上层（供人查看 / merge）。
3. 边界情况（hook-based 缺 baseChangeset、后台 owner 等 keepalive）→ **一律保留**（宁可留，不误删）。

### 2.3 移除动作本身是「带守卫的强删」

`reconstructed/deminified/m0319_0xc61d343.js:80-170`（`j8` 走的 worktree remove）：

```js
let O = await s([...w, "worktree", "remove", "--force", ...(n.forceTwice ? ["--force"] : []), I]);
return O.code === 0 ? { outcome: "removed" } : { outcome: "failed", stderr: O.stderr };
```

在删之前有一串**安全检查**（对照 §2.2/§2.3 源码）：不是主 worktree、不在另一台 host、
组件里没有 reparse point（Windows junction/symlink）、路径完好、能 resolve。
任一条不满足 → 返回 `kept`/`failed`，**不删**。即 CC 的删除是 fail-closed 的。

### 2.4 CC 自己也承认会无限增长

`reconstructed/deminified/m0357_0xc63ba04.js:39113`：

```
...registered git worktrees, which grow this list without bound. From another terminal,
remove worktrees you no longer need (git worktree remove <path>; git worktree prune ...)
```

CC 对「保留的 worktree 会无限增长」的处理是：**告知用户手动清理**，而不是自动 TTL 删。
GA 是本地长跑 agent，比 CC 更需要有界的自动清理，因此本方案在「对齐 CC」之外**多加一条 TTL 兜底**（见 §4.4）。

---

## 3. 映射：CC worktree → GA 私有目录

| CC 概念 | GA 对应物 | 说明 |
| --- | --- | --- |
| 每个 agent 一个 fresh git worktree | `temp/workflow_child_agents/<runId>/<jobId>` 私有目录 | GA 不是 git worktree，是普通目录 |
| 无改动 → auto-remove | diff 出来的 `written` 为空 → 删私有目录 | 对齐 §2.2 语义 1 |
| 有改动 → 保留 + `kept at` 日志 | `written` 非空 → 默认保留 + 记录 `keptWorkspacePath` | 对齐 §2.2 语义 2 |
| `git worktree remove --force`（带守卫） | 带守卫的 `shutil.rmtree`（限定在固定 base 下） | 对齐 §2.3 fail-closed |
| 无限增长 → 手动清理 | 额外 TTL 兜底自动清理 | GA 增强，见 §4.4 |

**关键差异**：CC 保留有改动的 worktree，是因为**改动只存在于 worktree**（要留给人 merge）。
GA 不同——GA 已经把产物**回收**到共享 workspace（`_collect_isolated_artifacts`），所以私有目录对功能不是必需。
保留它的价值是**排查**（例如用户实测的「agent_2 未产出 qipao.html 却上报成功」——保留私有目录就能直接看现场）。
因此 GA 的默认策略选 CC 同款（有改动默认保留），但**提供 `never` 开关**给不想要残留的场景。

---

## 4. 设计

### 4.1 清理策略开关（env）

新增环境变量 `GA_WORKFLOW_ISOLATION_KEEP`，取值：

| 取值 | 语义 | 对应 CC |
| --- | --- | --- |
| `changed`（**默认**） | 无改动→删；有改动→保留 | CC 原义 |
| `always` | 一律保留（只靠 TTL 兜底） | 更保守 |
| `never` | 一律删（有改动也删，产物已回收） | 更激进 |

非法值 → 回退 `changed`（不抛错，避免一个拼写错误让整个 run 挂掉）。

### 4.2 带守卫的删除

新增 `_remove_isolated_workspace(job)`：

- 目标路径 = `_isolated_workspace_base() / <runId> / <jobId>`（**重新计算，不信任传入值**）。
- 守卫（fail-closed，任一不满足即**不删**）：
  1. 解析后的绝对路径必须**严格位于** base 之下（`Path.resolve()` 后做 `is_relative_to`）。
  2. 目标最后一段必须等于 `job.job_id`；父目录名必须等于 `runId`（或 jobId 直接挂在 base 下，无 runId 时）。
  3. 目标不是符号链接 / reparse point。
  4. 目标是目录。
- 删除用 `shutil.rmtree`，包在 `try/except OSError` 里（清理失败只记 warning，**绝不影响 job 结果**）。
- 删除后，若 runId 父目录已空，一并 `rmdir`（不留空壳）。

> 注意：这里的「递归删除」是**程序对自己创建的固定私有目录**做的清理（有 §4.2 的路径守卫），
> 不是对用户数据 / 任意路径的删除。项目铁律「禁止 `rm -rf` / 递归删除」针对的是**命令行操作**，
> 且本实现先做了严格的路径归属校验，语义上等价于 CC 的 `git worktree remove`（同为程序内受控删除）。

### 4.3 清理时点

- 在 `_run_tool_job` 末尾：`_collect_isolated_artifacts(...)` **之后**（必须先回收，再删），
  调用 `_maybe_cleanup_isolated_workspace(job, written)`：
  - `written` 为空 → 删（无论 policy）。
  - `written` 非空 → `policy==never` 删，否则保留。
  - 保留时把路径写进 `tool_summary["keptWorkspacePath"]`（对齐 CC 的 `kept at`）。
- 非隔离但**没有共享 workspace** 的 fallback 私有目录（`_child_cwd` 的最后一支）同样处理：
  无产物就删（避免同样泄漏）。
- 清理必须在 `state["done"]=True` **之前**完成？——不必。清理放在 `_run_tool_job` 返回前即可；
  `poll()` 只读 `state`，与目录清理无耦合。但**产物回收必须先于删除**（同一函数内顺序保证）。

### 4.4 TTL 兜底清理（GA 增强）

新增 `prune_isolated_workspaces()`（模块级函数 + runner 上的薄封装）：

- 环境变量 `GA_WORKFLOW_ISOLATION_TTL_HOURS`，默认 `168`（7 天）；`0` = 关闭 TTL 清理。
- 逻辑：扫 base 下的 `<runId>`（或裸 `<jobId>`）目录，`mtime` 早于 `now - ttl` 的**受守卫删除**（复用 §4.2 的守卫）。
- 触发点：runner `start()` 里**限频**调用（模块级 `_last_prune` 时间戳，最多每 10 分钟一次），
  避免每个 job 都扫盘。

### 4.5 base 可覆盖（测试友好）

新增环境变量 `GA_WORKFLOW_CHILD_AGENTS_ROOT` 覆盖 base（默认 `temp/workflow_child_agents`）。
测试用 tmp 目录，避免污染仓库（当前测试会往真实 `temp/workflow_child_agents/` 写 `agent_iso`/`wf_test`）。

---

## 5. 实施步骤（代码落点）

1. `workflow_child_agent.py`：
   - 新增 `_isolated_workspace_base()`（读 env）、`_isolated_workspace_keep_policy()`（读 env）。
   - `_isolated_workspace()` 改用 `_isolated_workspace_base()`。
   - 新增 `_isolated_workspace_path(job)`（只算路径，不建目录）+ `_remove_isolated_workspace(job)`（守卫删除）。
   - 新增 `_maybe_cleanup_isolated_workspace(job, written) -> str | None`（返回保留路径）。
   - `_run_tool_job` 末尾接上清理；`tool_summary["keptWorkspacePath"]`。
   - 新增模块级 `prune_isolated_workspaces()`；`start()` 限频调用。
2. `skills/workflow-authoring/SKILL.md`：补「隔离目录跑完清理（对齐 CC auto-removed if unchanged）」一段 + 三个 env。
3. 测试 `tests/test_workflow_isolation_cleanup.py`（新）：
   - 无改动 → 目录被删。
   - 有改动 + 默认 `changed` → 目录保留，`keptWorkspacePath` 记录，产物已回收。
   - `never` → 有改动也删。
   - `always` → 无改动也保留。
   - 守卫：路径不在 base 下 / 目标是 symlink → 拒删。
   - TTL：旧目录删、新目录留；`0` 关闭。
   - base env 覆盖生效。
4. 更新 `docs/20261013-ga-workflow-多agent冲突-实施记录-阶段1-3.md` §5（把「隔离目录清理」从「未做」改为「已完成」）。
5. 跑 focused + 全量 `unittest discover`，提交。

---

## 6. 风险与不做项

- **风险**：清理时点若早于产物回收 → 丢产物。缓解：清理严格在 `_collect_isolated_artifacts` 之后。
- **风险**：误删非私有目录。缓解：§4.2 的四重守卫 + 只删 base 之下、名字匹配 runId/jobId 的目录。
- **不做**：把 GA 私有目录改造成真正的 git worktree（那是更大的工程，且 GA 的共享 workspace 语义不同）。
- **不做**：`isolation:"remote"`（**复刻时暂时不需要参考 claudecode 云端会话的实现**）。
- **不做**：删除失败的自动重试 / 告警升级（保持 best-effort，失败只记 warning）。
