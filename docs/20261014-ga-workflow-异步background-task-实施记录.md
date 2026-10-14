# GA workflow 对齐 CC「异步 background task」——实施记录（2026-10-14）

> 上游调研：`docs/20261013-ga-workflow-异步执行与Ink输入卡顿-对齐CC.md`（§5 四项对齐方案）。
> CC 证据仓（只读）：`D:\git_codes\claudecode283_hack`，剖析文档
> `claudecode283/extracted/dynamic-workflow-internals.md` §1/§3.4/§7.5/§8/§9.2。
> **云端不复刻：复刻时暂时不需要参考 claudecode 云端会话的实现。**

本文只记录本轮**已实施**的代码改动、实测数据、新增测试与遗留项。

---

## 0. 一句话结论

CC 的 dynamic workflow 是一个**后台任务**（`call()` 立即返回 `async_launched`），
主 agent 回合**可以结束**，UI 用旁路状态行「Running in background · /workflows to monitor and save」
提示，**不锁输入框**。本轮把 GA Ink 的 workflow 从「占用会话 running 状态 + 查看路径跑重活」
改成同一形态：**运行期 composer 可正常输入；有草稿时 Enter 也能打开 `/workflows` 面板；
查看 detail/list/progress 从秒级降到百毫秒级。**

---

## 1. CC 侧依据（原文坐标）

| 事实 | 坐标 |
| --- | --- |
| 工具 `call()` 立即返回 `{status:"async_launched", taskId, runId, ...}` | `m1569_0xdceb889.js @65948` |
| 工具定义 `backgrounding:"never"`（自己起后台任务，不走普通后台化通道） | `m1569 @61381` |
| 工具结果文案「Workflow launched in background … You will be notified when it completes」 | `m1569 @70281` |
| 后台 async IIFE + Node `vm` 跑脚本 | `m0869_0xd12567a.js @81169 kSt()`、`Io() @78839` |
| 用 `task-notification`（`mode:"task-notification"`, `priority:"next"`）唤醒主 agent；`BV()` 原子 claim 去重 | `m0567_0xcdd6311.js @5824 RTn()` |
| 状态行「Running in background · /workflows to monitor and save」是**旁路提示**，composer 不锁 | `m2000_0xe395211.js` |
| `/workflows` 是独立对话框 | `m1075` |

**核心设计点**：workflow 的生命周期与「主会话是否 running」**解耦**。
主 agent 只负责「启动 + 一行汇报 + 结束回合」，真正的执行在后台，
完成后由 `task-notification` 作为下一条消息唤醒主 agent。

---

## 2. 方案 1：workflow 不再占用会话 running 状态

**现象（改前）**：跑 workflow 时 Ink 输入框无法输入新内容（`Running · Enter keeps draft`），
因为 `_run_workflow_runtime` 一开始就 emit 全局 `{"type":"status","status":"running"}`，
Ink 据此隐藏光标（`App.tsx` `showCursor`）并让 Enter 不提交（`inputController.ts`）。

**改动**（`frontends/ink_bridge.py`）：

- `_run_workflow_runtime`：
  - **删除**开头的 `emit({"type":"status","status":"running"})` + `emit({"type":"activity","label":...})`；
  - **删除** `finally` 里的 `activity:None` + `status:idle`（整块只保留 `watch_stop.set()`）；
  - **删除** `handoff_queued` 变量（两处赋值改为裸 `self._queue_workflow_handoff(current)`）。
  - 保留一行注释说明「workflow 是 background task，绝不翻转全局会话状态」。
- `_queue_workflow_handoff`：
  - 在 `put_task` 之后、`thread.start()` 之前 **新增** `self.emit({"type":"status","status":"running","taskId":self._task_seq})`。
  - 理由：workflow 本身是 background，但 handoff 是**真实 agent turn**，此时才锁 composer；
    该 turn 结束后由 `_consume_display_queue` 末尾 emit `idle`。

**效果**：workflow 运行期 composer 不再锁死，输入框可正常输入/提交；
只有 handoff（agent 真正在答）那一小段才短暂 running。

---

## 3. 方案 2：Enter 永远能打开 workflow 面板

**现象（改前）**：状态条只在 `input.trim() === ''` 时接管 Enter（`App.tsx` ~1416），
所以当输入框有草稿时按 Enter 想看 `/workflows` **静默无反应**（直接提交草稿）。

**改动**（`frontends/ink-ui/src/App.tsx`，状态条按键块）：

```ts
if (showWorkflowStatusBar && workflowStatusBar) {
  const command = workflowStatusBarCommandForKey(workflowStatusBar, key, rawInput)
  if (command) {
    const statusBusy = state.status === 'running' || state.status === 'stopping'
    if (input.trim() === '' || (key.return && statusBusy)) {
      bridgeRef.current?.send(command); return
    }
  }
}
```

语义：
- **空输入**：Enter 开面板（+ `x` stop 照旧）；
- **有草稿且 turn running/stopping**：Enter 也开面板（运行期用户想看一眼，不被草稿吞掉）；
- **有草稿且 idle**：Enter 提交草稿（单字母不抢，避免误触发 stop）。

**效果**：无论有没有草稿，运行期按 Enter 都能打开 `/workflows` 面板。

---

## 4. 方案 3：查看路径移出重活（本会话最大发现）

### 4.1 实测根因

`workflow_detail` 原来要 **7.5–16.8 秒**，emit 2 个事件共 **2.3 MB**：

- `agent_snapshot` **1.63 MB**：`list_records` 扫**全仓 629 条记录**，其中 workflow 记录来自
  `agent_control_workflow` 扫**全部 108 个 run**；`subagent_manager` 侧还有 424 次 `probe_agent`。
- `workflow_detail` **687 KB**：`_workflow_run_payload` 的 jobs 带长 prompt 共 440 KB
  + journal 全量 152 事件 75 KB。

而 UI 只需要**当前 session 的少量 run** 和**尾部若干事件**。

### 4.2 改动

1. `workflow_store.py::list_runs` → 新增 `*, session_id=None` 参数
   （scoped glob `f"{scope}/workflows/*/state.json"`）。
2. `agent_control_workflow.py::WorkflowChildAdapter.__init__` → 新增 `session_id_provider=None` 参数
   + `_session_id()` + `_runs()`；3 处 `self.store.list_runs()` 改为 `self._runs()`。
3. `frontends/ink_bridge.py`：
   - `WorkflowChildAdapter(..., session_id_provider=lambda: getattr(self.agent, "session_id", None))`
   - 新增 `_current_session_id()`；`_list_workflow_runs` 改为
     `self.workflow_store.list_runs(session_id=self._current_session_id())`
   - **移除** `workflow_list` / `workflow_detail` / `workflow_progress` / `workflow_launch_from_tool`
     / `_run_workflow_runtime`（成功+失败两处）里的 `_emit_agent_read_model()`
   - `_watch_workflow_progress`：**删除** `_emit_agent_read_model()` + `last_read_model_at`
     + 模块级 `_AGENT_READ_MODEL_MIN_INTERVAL` 常量
   - `workflow_detail` 的 `events` 改为 `replay_events(...)[-_WORKFLOW_DETAIL_EVENT_TAIL:]`
     （新增常量 `_WORKFLOW_DETAIL_EVENT_TAIL = 50`）
   - **保留** `_emit_agent_read_model()` 在控制类命令：
     `workflow_draft` / `workflow_approve` / `workflow_resume` / `workflow_run_named` /
     `workflow_deny` / `workflow_stop`

### 4.3 实测结果

（108 runs 的 store，仅 1 个 run 属于该 session）

| 命令 | 改前 | 改后 |
| --- | --- | --- |
| workflow_detail | 7565–16853 ms / 2.3 MB | **120–144 ms / 585 KB** |
| workflow_list | 1525 ms / 710 KB | **85 ms / 439 KB** |
| workflow_progress | — | **25 ms / 44 KB** |

### 4.4 仍可再优化（未做，记为后续）

- `_workflow_run_payload` 的 jobs 带完整 `prompt`（440 KB，UI 只用 `stringMetadata(job?.prompt)`）→ 应截断。
- `workflow_list` 仍 `load_run` 全部 run（439 KB）→ 应做轻量列表 payload。

---

## 5. 方案 4：提示词侧「启动后结束回合」

CC 的工具结果明确要求主 agent「启动后汇报一行并结束回合」，不阻塞等待。GA 同步：

- `ga.py::do_workflow`（~790）yield 文案改为：
  `"[Workflow] launched {name} ({run_id}) in the background. You will be notified when it completes; do not wait for it. Report the launch in one line and end your turn."`
- `frontends/ink_bridge.py::workflow_launch_from_tool` 返回的 `"message"` 同步 CC 口径
  （含「do not block waiting (no wait_agent)」）。
- `skills/workflow-authoring/SKILL.md`（§2 末尾）新增段落 **Do not block on the run**：
  明确禁止 `wait_agent` / 轮询，引用 `m2000` 与 `<workflow_handoff>`。

---

## 6. 测试（focused，全绿）

| 测试 | 结果 |
| --- | --- |
| `tests.test_ink_bridge` + `test_workflow_store` + `test_agent_control_workflow` | **159 OK** |
| `npx tsc --noEmit`（frontends/ink-ui） | exit 0 |
| `npx tsx --test src/workflowList.app.test.ts` | **4 pass / 0 fail** |

新增/调整测试：

- `tests/test_workflow_store.py::test_list_runs_scopes_to_session_when_requested`
- `tests/test_ink_bridge.py::test_workflow_detail_does_not_publish_heavy_agent_read_model`
- `tests/test_ink_bridge.py::test_background_workflow_never_flips_global_session_status`
- `tests/test_ink_bridge.py::assert_bridge_idle_tail` 改为断言整条事件流**无任何 `status` 事件**
- `tests/test_ink_bridge.py`：`test_workflow_control_command_emits_common_records_and_events_alongside_legacy_workflow_events`
  （原 refresh 测试改名，`workflow_detail` 改为 `workflow_deny`）
- `frontends/ink-ui/src/workflowList.app.test.ts::App opens the workflow panel with Enter while a running turn holds a draft`

---

## 7. 改动文件清单

- `frontends/ink_bridge.py`
- `frontends/ink-ui/src/App.tsx`
- `frontends/ink-ui/src/workflowList.app.test.ts`
- `workflow_store.py`
- `agent_control_workflow.py`
- `ga.py`
- `skills/workflow-authoring/SKILL.md`
- `tests/test_ink_bridge.py`
- `tests/test_workflow_store.py`
- `AGENTS.md`（本记录小节）

---

## 8. 建议自测（用户手动）

`.\ga` → 跑一个 workflow，验证：

1. workflow 运行期输入框可正常输入并提交新内容（不再锁死）。
2. 有草稿时按 **Enter** 能打开 `/workflows` 面板（不再静默提交草稿）。
3. `/workflows` 列表与 detail 打开速度明显变快（百毫秒级）。

---

## 9. 遗留 / 后续（未实施）

- `_workflow_run_payload` job prompt 截断。
- `workflow_list` 轻量 payload。
- 事件 → 帧节流（对齐 Codex `schedule_frame()`）。
- 子 agent 协作冲突（CC 靠 worktree 隔离 + 值传递）—— 见相关专项调研文档。
