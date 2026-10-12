# GenericAgent Dynamic Workflow 实施记录：C1 `/workflows` 历史复用 + C2 状态行

> 日期：2026-10-12
> 仓库：`D:\git_codes\GenericAgent`
> 阶段：C1 / C2（对应实施方案 G10）
> 上游参考：Claude Code 2.1.283 真实源码（只读）
> 状态：✅ 已完成

---

## 0. 本阶段目标

把 Claude Code（下称 CC）`/workflows` 的 UI 能力补齐到 GA：

- **C1**：对齐 CC "再次执行历史 workflow"的四条路径（指南 §9.5）；
- **C2**：对齐 CC 状态行的信息层级，运行中必须能看到 `/workflows` 入口。

> **边界**：**不复刻 CC 云端会话**（`remote_launched` / `Ho()` / `Uo` / `WP()`）。
> **【复刻时暂时不需要参考 claudecode 云端会话的实现】**
> CC 路径 ④（工具侧 `runId` 运行操作）在 v2.1.283 是**关闭**的（`m1569 @55937 he()` 恒返回 `undefined`），
> 因此 GA **不需要**做，也不应该做。

---

## 1. CC 源码证据

| CC 符号 | 模块 | 位置 | 职责 |
| --- | --- | --- | --- |
| `/workflows` 命令 | `m1075_0xd34f8d5.js` | — | 历史列表入口（`local-jsx`，`immediate:true`） |
| 历史列表对话框 | `m1906_0xe19cc02.js` | `Save as:` @732 | 列表 + **另存为命名 workflow**（默认名 = `meta.name` 或 summary slug） |
| 详情对话框 | `m1460_0xd973957.js` | — | 进度树 + 按需读 agent 转录 |
| 进度树 helpers | `m1456_0xd96e09a.js` | — | 折叠 / 分组 |
| 状态行 | `m2000_0xe395211.js` | @10379（格式化文件 370 行） | `Running in background · /workflows to monitor and save` |
| 恢复指令文本 | `m0567_0xcdd6311.js` | `CTn()` @4818 | `Resume the paused workflow by calling: Workflow({scriptPath, resumeFromRunId}) — completed agents return cached results.` |
| `<recovery>` 通知块 | `m0567_0xcdd6311.js` | @360 | `To resume after editing the script, call: Workflow({...})` + `Agent transcripts: <dir>` |
| run-op provider | `m1569_0xdceb889.js` | `he()` @55937 | 恒 `undefined` → 路径 ④ 在本 build 关闭 |

### 1.1 状态行原文（`m2000`）

```js
// running（local_workflow）
dimColor: "Running in background · "  +  suggestion: "/workflows"  +  dimColor: " to monitor and save"
// 无 run 时
suggestion: "/workflows"  +  dimColor: " to view dynamic workflow runs"
// 终态
`${icon} ${T}${R}${f}${p}`   // T = Failed / Stopped / Completed, R = " in <dur>", f = " · N agents", p = " · X tokens"
```

### 1.2 "另存为"的原文与重名提示（`m1906`）

- 保存对话框标题 `Save as:`；
- 默认名 = `Fy(script)` 解析出的 `meta.name`，退化时用 summary slug；
- 重名提示：`" already exists. Press Enter again to overwrite, or change the name."`。

---

## 2. GA 现状（改动前）

| 能力 | 改动前 |
| --- | --- |
| `/workflows` 历史列表 | ✅ 已有（`inputController.ts` → `workflow_list`） |
| 详情视图（进度树 + 按需读转录） | ✅ 已有（`workflow_detail` / `workflow_progress`） |
| 实时执行情况 | ✅ 已有（`workflow_live` + `workflow_event`） |
| 恢复（路径 ①） | ✅ 已有（`/workflow resume <id>` + 面板 `r`） |
| 失败/被杀后的恢复提示（路径 ③） | ⚠️ 部分（有 resume，但 run payload 里没有 CC 式 recovery 指引） |
| **另存为命名 workflow（路径 ②）** | ❌ **完全缺失** |
| 状态行 `/workflows` 入口（C2） | ❌ 缺失（全仓 grep 无该文案） |

---

## 3. C1 改动

### 3.1 命名 workflow 库（`workflow_store.py`）

新增 `normalize_named_workflow_name()` 与三个方法：

```python
store.save_named_workflow(name, script, *, source_run_id=None, description=None, overwrite=False) -> dict
store.load_named_workflow(name) -> dict          # 含 script
store.list_named_workflows() -> list[dict]       # 不含 script
```

- 落盘位置：`<root>/named-workflows/<slug>.json`（slug 由名字安全化得到，`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`）。
- 记录字段：`name` / `requestedName` / `description` / `script` / `scriptBytes` / `scriptHash` / `sourceRunId` / `savedAt`。
- 重名：默认抛 `FileExistsError`（= CC 的 "Press Enter again to overwrite"），`overwrite=True` 才覆盖。

### 3.2 桥接命令（`frontends/ink_bridge.py`）

| 方法 | 命令 | 行为 |
| --- | --- | --- |
| `workflow_save(run_id, name=None, overwrite=False)` | `workflow_save` | 取 run 的 `script` 存成命名 workflow；默认名 = `metadata.workflowName` → runId；重名发 `workflow_name_exists`；成功发 `workflow_saved` + `named_workflows` |
| `workflow_run_named(name, args=None)` | `workflow_run_named` | 读命名 workflow → `create_draft` → `status=running` → 起 runtime 线程（= CC `Workflow({name, args})`）；找不到发 `workflow_name_not_found` |
| `workflow_named_list()` | `workflow_named_list` | 发 `named_workflows` |

`workflow_list()` 的 `workflow_runs` 事件额外带 `savedWorkflows: [name, ...]`，
让列表面板直接显示已保存的命名 workflow（对齐 CC m1906 把 saved 与 history 放在同一视图）。

### 3.3 恢复指引（路径 ③）

`_workflow_run_payload()` 对 `failed` / `killed` / `cancelled` / `interrupted` 的 run 附带：

```json
"recovery": {
  "command": "/workflow resume <runId>",
  "message": "To resume after editing the script, run: /workflow resume <runId> — completed agents return cached results.",
  "agentTranscriptsDir": "<artifactDir>/agents"
}
```

对应 CC 的 `CTn()` 文本 + `<recovery>` 的 `Agent transcripts: <dir>`；
GA 没有 `Workflow()` 工具，所以指引落到 GA 自己的 resume 入口（行为一致：未变的 agent 走缓存）。

### 3.4 前端

| 文件 | 改动 |
| --- | --- |
| `protocol.ts` | 新增命令 `workflow_save` / `workflow_run_named` / `workflow_named_list`；新增事件 `workflow_saved` / `named_workflows`；`WorkflowRun.recovery`；`NamedWorkflow` 类型 |
| `state.ts` | `AppState.namedWorkflows` + 处理 `named_workflows` / `workflow_saved` |
| `inputController.ts` | `/workflow save <runId> [name] [--overwrite]`、`/workflow run <name> [json-args]`、`/workflows saved` |
| `localCommandTranscript.ts` | 上述命令的回显文本 |
| `workflowPanel.ts` | **`s` = save（对齐 CC m1906）**，`x` = stop；控件提示同步；列表面板底部多一行 `Saved: <names> - /workflow run <name>` |
| `App.tsx` | `/workflows` 列表事件带 `savedWorkflows`，打开列表即能看到已保存的命名 workflow |

> **关键按键变更**：GA 原先 `s` 和 `x` 都是 stop。按用户"按照 CC 改"的决定，现在 `s` 归 save、`x` 归 stop。
> `--overwrite` 是 GA 对 CC "Press Enter again to overwrite" 非模态对话框的等价替代。

---

## 4. C2 改动（`workflowStatusBar.ts`）

- 运行中状态行由 `Enter view · x stop` 改为 **`Enter · /workflows · x stop`**；
- 第二行保留 GA 更丰富的 `N/M agents done · <active>: <tool> · <live> · <tokens> · integration/audit`；
- 终态图标继续区分 `✓ / ✗ / ! / ◌`；
- **不复刻云端 `remote_launched` 状态行**（【复刻时暂时不需要参考 claudecode 云端会话的实现】）。

> **为什么不是逐字照抄**：CC 的运行行是 `Running in background · /workflows to monitor and save`，
> 它**不显示 workflow 名字**。GA 的状态行是单行且要同时显示名字与进度，实测在 78 列终端下
> 逐字照抄会把 `1/2 agents done` 截断成 `1/2 agents d…`。因此 GA 用 `/workflows` 短入口 + 保留 `x stop`，
> 把 `Enter view` 缩成 `Enter` 以腾出宽度。

---

## 5. 测试

### 5.1 Python

| 测试 | 覆盖 |
| --- | --- |
| `tests/test_workflow_store.py::NamedWorkflowLibraryTest`（5 条） | 保存/列出/读取往返、重名拒绝与 `overwrite`、空名/空脚本拒绝、缺失名、空库 |
| `tests/test_ink_bridge.py`（新增 7 条） | `workflow_save` 写库、默认名取 `workflowName`、重名 `workflow_name_exists` + overwrite、`workflow_run_named` 起新 run 且脚本一致、未知名 `workflow_name_not_found`、recovery 仅出现在停止类终态、`workflow_named_list` |

### 5.2 前端

| 测试 | 覆盖 |
| --- | --- |
| `workflowPanel.test.ts` | `s` → `workflow_save`、`x` → `workflow_stop`、控件文案 |
| `inputController.test.ts` | `/workflow save …`（含 `--overwrite`）、`/workflow run …`、`/workflows saved` |
| `workflowStatusBar.test.ts` | 运行行含 `/workflows` |
| `workflowList.app.test.ts` | 真渲染帧同时含 `/workflows`、workflow 名、`1/2 agents done` |

### 5.3 验证命令

```bash
python -m unittest tests.test_workflow_store tests.test_ink_bridge
npm --prefix frontends/ink-ui run typecheck
npm --prefix frontends/ink-ui run test
```

---

## 6. 与实施方案的对应

- 实施方案 §4 缺口清单 G10。
- 实施方案 §5 阶段 C1 / C2（现已标记"已实施"）。
- 相关文档：`docs/20261012-ga-dynamic-workflow-复刻实施方案.md`、
  `docs/20261012-复刻dynamic-workflow指南.md` §9.4 / §9.5 / §9.7。
