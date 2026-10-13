# GA Ink workflow 异步执行与「输入框锁死 / 查看卡顿」：对齐 CC 的调研（2026-10-13）

> 本文只做**调研与落盘**，不改代码。代码改动按 §5 另开实施记录并回链本文。
> 证据仓库：`D:/git_codes/claudecode283_hack`（Claude Code v2.1.283 明文 JS 模块 + 剖析文档）。

## 1. 用户实测现象

在 GA Ink 里跑 dynamic workflow（例如「用 workflow 帮我并行调研 claude-haiku-5.5 / gpt-6-luna / gemini-4-argon，然后综合验证」）时：

1. **输入框像被锁死**：workflow 执行期间输入框无法正常输入新内容（按键回显迟钝、光标消失）。
2. **按回车想看 workflow 执行情况很卡**，甚至像「没反应」。

这是**两个独立根因**，下面分别定位。

## 2. 结论速览

| # | 现象 | 根因 | 与 CC 的差异 |
|---|---|---|---|
| A | 输入框被锁死 | GA 的 workflow 后台线程一开始就 `emit status:running`，把**整个会话**钉在 running；Ink 在 running 时隐藏光标、Enter 只保留草稿不提交 | CC 把 workflow 当 background task，会话保持可用 |
| B | 按回车查看卡顿 / 无反应 | ①`workflow_detail`/`workflow_list` 仍在 JSONL stdin 线程**同步**执行；②状态条只在输入框为空时才接管 Enter（草稿非空时按 Enter 静默无反应）；③0.25s 进度轮询 + live 遥测高频触发 Ink 全树 re-render | CC 的 `/workflows` 是纯 UI 对话框，数据来自内存任务注册表 + 磁盘快照 |

## 3. CC 事实：dynamic workflow 是异步的

源码坐标见 `claudecode283/extracted/dynamic-workflow-internals.md` §1 / §3.4 / §7.5 / §8 / §9。

1. **工具调用立即返回**。`m1569_0xdceb889.js @65948 call()` 编译脚本、落盘、注册后台任务后**立刻**返回
   `{status:"async_launched", taskId, taskType:"local_workflow", runId, workflowName, summary, transcriptDir, scriptPath}`。
   工具定义里还有 `backgrounding:"never"`（`@61381`）——它自己起后台任务，不走普通后台化通道。
2. **真正的执行在后台 async IIFE**。`m0869_0xd12567a.js @81169 kSt()` 返回一个
   `(async()=>{ … await Io(...) … })()`，脚本在 Node `vm` 里跑（`Io()` `@78839`）。
3. **模型看到的是「已启动，稍后通知你」**。`@70281` 的工具结果文案：
   `Workflow launched in background. Task ID: … You will be notified when it completes`。
   也就是说**主 agent 的回合可以结束**，不阻塞等待。
4. **完成走 task-notification 唤醒主 agent**。`m0567_0xcdd6311.js @5824 RTn()` 组装 `<task-notification>`，
   以 `mode:"task-notification"`、`priority:"next"` 入队 → 主循环下一轮把它当**用户消息**读入。
   `BV()` 做原子 claim 去重（已 notified 则不再投递）。
5. **UI 不锁输入**。`m2000_0xe395211.js`（状态行）在 `local_workflow` 运行中显示
   `Running in background · /workflows to monitor and save`——这是一条**旁路提示**，composer 照常可用；
   `/workflows`（`m1075_0xd34f8d5.js`）是独立对话框，随时可开。
6. **keepalive 不是锁输入**。`m0357_0xc63ba04.js` 的 keepalive（`keepaliveReasons`）只用于防任务被驱逐 / GC，
   与「用户能否输入」无关。

一句话：**CC 的 dynamic workflow = 后台任务 + 通知唤醒；主会话在 workflow 运行期间是空闲、可用的。**

> 【复刻时暂时不需要参考 claudecode 云端会话的实现】（`remote_launched` / `isolation:"remote"` / `Ho()`）。

## 4. GA 现状：异步已对齐，但「会话占用」没有

- GA 的 workflow **确实是异步的**：`workflow_launch_from_tool`（`frontends/ink_bridge.py:861`）校验后
  `create_draft` → 起 `threading.Thread(target=self._run_workflow_runtime)`（`:970`）→ 立即返回
  `{"status":"async_launched", ...}`（`:982`）。这一步与 CC 一致。
- **差异 A（现象 1 的根因）**：`_run_workflow_runtime`（`:1164`）第一件事就是
  `self.emit({"type": "status", "status": "running"})`（`:1169`）。这条 `status` 是**全局会话状态**，于是：
  - Ink `state.status === 'running'` → 输入框光标隐藏（`frontends/ink-ui/src/App.tsx:1661`
    `showCursor={state.status !== 'running' && state.status !== 'stopping'}`）；
  - 输入提示变成 `Running · Enter keeps draft · PgUp/PgDn · Ctrl+O tools · Esc stop`（`App.tsx:1647`）；
  - Enter 在 running 时**不提交**（`frontends/ink-ui/src/inputController.ts:362`
    `if (status === 'running' || status === 'stopping' || status === 'connecting') return decision(value, offset)`）。
  - 而且 host 会在 workflow 结束后把结果 handoff 回 agent 循环续写答复（`_queue_workflow_handoff`），
    所以从启动到 agent 答完，会话**整段**都是 running。
  → 用户体感就是「输入框被锁死」。
- **差异 A 的放大器**：GA 的模型习惯调 `wait_agent(condition="workflow_terminal", timeout=300)` **阻塞等待** workflow 终态
  （见历史 run 日志）；CC 的模型是**结束回合、等通知**。前者让会话更长时间停在 running。
- **差异 B（现象 2 的根因）**：
  - 只有 **planner** 被移出命令线程（`workflow_plan_async`，见 `AGENTS.md`「planning off the command loop」）；
    **`workflow_list` / `workflow_detail` / `workflow_progress` 仍在 `run_jsonl_loop` 的 stdin 线程里同步执行**
    （定义 `ink_bridge.py:1063/1078/1099`，派发 `:2392/2394/2396`）。`workflow_detail` 一次要做：
    `load_run`（读 `state.json`，实测样本 191 KB）+ `replay_events`（读 `journal.jsonl`）+ 两个 artifact payload
    （`workflow-progress.json` 40 KB、draft）。这几百毫秒内**命令线程处理不了下一条命令**。
  - 状态条接管 Enter 的条件是 `input.trim() === ''`（`App.tsx:1416`）。running 时 Enter 又会「保留草稿」
    （`inputController.ts:362`），于是**只要草稿非空，按 Enter 看 workflow 就静默无反应**——
    这正是「按回车很卡 / 没反应」的直接来源。
  - `_watch_workflow_progress`（`:1247`）每 0.25s 一轮，可能 emit `workflow_live` + `workflow_progress` +
    `agent_read_model`（后者 = `agent_event` + 全量 `agent_snapshot` 序列化）。每个事件都触发 Ink 一次
    **全树 re-render**（2026-10-10 文档 R2 已量化：≈12 ms/tick × 4 Hz）；再加上 in-process 子 agent 线程
    与 stdin 线程抢 GIL，按键回显被挤掉。
- **差异 C（次要）**：`append_event`（`workflow_store.py:121`）每次追加都要**整读 `journal.jsonl`** 求 max sequence
  （O(n²)），事件多时进一步加重后台线程与命令线程的争用。

## 5. 对齐方案（按 CC 的「后台任务」模型）

必做（对齐 CC 的核心）：

1. **workflow 不再占用会话 running 状态**（对应差异 A）。
   - `_run_workflow_runtime` 不再 emit 全局 `status: running`；改发一个**独立**的 background-task 事件
     （或在 `state` 里维护 `backgroundTasks`），让 Ink 显示 CC 同款旁路状态行
     `Running in background · /workflows to monitor and save`，而 composer 保持 idle 可用。
   - `workflow_final` 之后仍走 handoff 唤醒 agent（保留现有语义），但**不阻塞用户输入**。
2. **让 Enter 永远能打开 workflow 视图**（对应差异 B 第 2 点）。
   - 状态条接管 Enter 不再要求 `input.trim()===''`；或 running 时 Enter 优先「打开/切换 workflow 面板」而不是「保留草稿」。
3. **查看路径移出命令线程 / 变廉价**（对应差异 B 第 1 点）。
   - `workflow_detail`/`workflow_list`/`workflow_progress` 改为异步派发（worker 线程 + 事件回投），
     或对 `load_run`/`replay_events` 做 mtime+size 缓存。
   - `append_event` 维护内存 / 文件级 max-sequence 游标，避免每次整读 journal。

建议（进一步降负载）：

4. **事件→帧节流**：Ink 侧把 `workflow_live`/`agent_event`/`agent_snapshot` 合并成单帧刷新
   （对齐 Codex 的 `schedule_frame()` + 帧率限制，见 `docs/20261010-ga-ink-workflow-progress-ui.md` §3）。
5. **有界预览**：面板只渲染有界数据（已有 `WORKFLOW_PANEL_MAX_ROWS`，继续贯彻）。
6. **提示词侧**：鼓励模型「启动后结束回合、等通知」，而不是 `wait_agent` 死等。

## 6. 证据与复现

- CC：`claudecode283/extracted/dynamic-workflow-internals.md` §1（生命周期图，明确「Workflow 工具是异步的」）、
  §3.4、§7.5、§8.3、§9.2。
- GA 代码坐标：见 §4 行号（`frontends/ink_bridge.py`、`frontends/ink-ui/src/App.tsx`、
  `frontends/ink-ui/src/inputController.ts`、`workflow_store.py`）。
- 历史量化：`docs/20261010-ga-ink-workflow-progress-ui.md` §2（R2：≈12 ms/tick × 4 Hz）、
  §8（1 ms / 10 ms 命令线程不阻塞的实测）。
- 样本 run：`temp/sessions/session_00d69a393db64c29a1aab2713b748844/workflows/wf_4f61cc8c2e4d41f0be14c5e14175da12/`
  （`state.json` 191 KB、`journal.jsonl` 64 KB、`workflow-progress.json` 40 KB）。

## 7. 已知限制 / 明确不做

- 云端会话（`remote_launched` / `isolation:"remote"` / `Ho()`）：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
- 本文只做**调研与落盘**；代码改动按 §5 另开实施记录。
