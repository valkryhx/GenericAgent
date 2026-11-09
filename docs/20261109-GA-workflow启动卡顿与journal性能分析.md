# GA 启动变慢与 workflow journal 性能分析（2026-11-09）

> 现象：用户感觉"自从昨天提交之后，GA 启动很卡、也有些慢"。
> 本文是实测结论，不是猜测：先给出可复现的测量，再给根因与修复方案。
>
> 代码版本：`0e7daa0`（HEAD）；对照版本：`ad86758`（2026-11-08 14:30，昨天晚间那批提交之前）。

---

## 0. 结论先行

| 结论 | 证据 |
| --- | --- |
| **不是昨天提交的代码回归** | 同一个数据目录下，`ad86758` 与 HEAD 启动耗时一致（1.83–1.92s vs 1.80–1.88s） |
| **是数据累积 + 两处老代码的放大** | 空数据目录启动 0.74–0.81s；当前数据（118 个 workflow run，106 个 session）启动 1.80–1.88s |
| 根因 1（启动） | `frontends/ink/bridge.py::_initial_agent_cursors()` 调 `store.list_runs()` **不带 session 过滤**，启动时把所有历史 session 的 workflow run 全部加载并重放事件 |
| 根因 2（运行期） | `genericagent/workflow/store.py::append_event()` 每次追加都**完整重读 journal** 来算最大 sequence → 单个 run 的事件写入是 O(n²) |
| 影响 | 启动每次多付约 1 秒（随历史线性增长，无上限）；长 workflow 的 journal 写入会越跑越慢（1000 条 13.3s，下一个 1000 条 18.4s） |

---

## 1. 测量方法（可复现）

### 1.1 启动耗时

固定项目数据根（`GA_PROJECT_ROOT`），起真实 bridge 进程，量"到发出 `{"type":"ready"}`"的时间：

```python
# temp/perf/time_bridge3.py（临时脚本，不入库）
p = subprocess.Popen([sys.executable, "frontends/ink/bridge.py"],
                     cwd=cwd, env={**os.environ, "GA_PROJECT_ROOT": project},
                     stdin=PIPE, stdout=PIPE)
# 读 stdout 直到 {"type":"ready"}，记录 perf_counter 差值，重复 3 次
```

### 1.2 热点

```bash
python -m cProfile -o temp/perf/head.prof frontends/ink/bridge.py < /dev/null
python -c "import pstats; pstats.Stats('temp/perf/head.prof').sort_stats('cumulative').print_stats(18)"
```

### 1.3 journal 写入

构造临时 `WorkflowStore`，连续 append 1000 条事件，再 append 1000 条，分别计时。

---

## 2. 测量结果

### 2.1 启动：代码没变慢，数据让它变慢

| 场景 | ready 耗时（3 次） |
| --- | --- |
| HEAD + 空数据根 | 0.81s / 0.74s / 0.80s |
| HEAD + 当前真实数据根 | 1.85s / 1.80s / 1.88s |
| `ad86758`（昨天晚间提交前）+ 同一真实数据根 | 1.92s / 1.83s / 1.92s |
| `ad86758` + 空数据根（首次测量） | 1.81s / 0.77s / 0.75s |

结论：同一份数据下两个版本没有差别；差别全部来自数据量。
昨天到今天跑了大量真实 e2e，`temp/sessions` 里累积了 118 个 run、106 个 session，
于是每次启动都要多花约 1 秒，而且会继续增长。

### 2.2 cProfile：98% 的启动增量集中在一个函数

```
1.287s  frontends/ink/bridge.py:1842(_initial_agent_cursors)
1.055s    workflow/store.py:733(_find_run_dir)      # 247 次 glob
0.650s    workflow/store.py:739(list_runs)
0.633s    workflow/store.py:141(load_run) / replay_events
```

### 2.3 直接量 `list_runs` 的代价

```python
store.list_runs()               # 118 runs → 3.75s（冷）/ 0.65s（热）
store.list_runs(session_id=..)  # 当前 session → 0.02s
```

---

## 3. 根因 1：启动时未按 session 过滤的 workflow 扫描

`_initial_agent_cursors()` 的 workflow 分支：

```python
store = getattr(adapter, "store", None)
for run in store.list_runs():          # ← 没有 session_id
    rows = store.replay_events(run.run_id)
    cursors[make_workflow_source_cursor(run.run_id)] = max(row.sequence for row in rows)
```

为什么这是错的：

1. **语义上**：GA 的 workflow 视图本来就是 per-session（adapter 注释与
   `_list_workflow_runs()` 都按当前 session 过滤；CC 也是 "No dynamic workflows in this session."）。
   其它 session 的 run 根本不会出现在这个 Ink 会话里，给它们建 cursor 没有意义。
2. **性能上**：`temp/sessions` 是**所有** GA 会话共享的，`list_runs()` 会 glob 全部
   `*/workflows/*/state.json`，再对每个 run `load_run()` → `replay_events()`。
   成本随历史线性增长，且没有上限。
3. **代码里已经有正确做法**：`WorkflowChildAdapter` 早就有
   `session_id_provider` 与 `_runs()`，注释甚至写明"unscoped 会让 list_records/events_since
   秒级"。`_initial_agent_cursors` 绕过了 adapter，直接摸 store，等于把踩过的坑又踩了一遍。


---

## 4. 根因 2：journal 追加是 O(n²)

`WorkflowStore.append_event()` 的现有实现：

```python
with cross_process_lock(artifact_dir / ".journal.lock"):
    current_max = 0
    if journal_path.exists():
        for line in journal_path.read_text(...).splitlines():   # ← 每次追加重读整份 journal
            current_max = max(current_max, int(json.loads(line).get("sequence") or 0))
    if event.sequence <= current_max:
        event.sequence = current_max + 1
    journal_path.open("a")...                                    # 追加一行
```

第 n 次追加要解析 n-1 行，n 次追加总成本 O(n²)。实测：

| 操作 | 耗时 |
| --- | --- |
| 前 1000 次 append | **13.27s** |
| 再 1000 次 append | **18.35s** |
| `replay_events()` 读 2000 条 | 0.03s |

写 2000 条事件要 30+ 秒，而读只要 30 毫秒——瓶颈完全在"为了算下一个序号而重读全文"。
workflow 一旦变长（多 agent、多 log/progress 事件），运行期会明显变慢，且越跑越慢。
这与用户"有些慢"的体感一致。

---

## 5. 修复方案

### 5.1 启动：把 cursor 播种交还给 per-session 的 adapter

- `WorkflowChildAdapter` 新增 `initial_cursors()`：只遍历自己 `_runs()`（已按 session 过滤）
  的 run，返回 `{source_cursor: tail_sequence}`；
- `GenericAgentBridge._initial_agent_cursors()` 的 workflow 分支改为
  `cursors.update(adapter.initial_cursors())`，不再直接碰 `store.list_runs()`；
- 没有 session（`None`）时不播种 workflow cursor——本来也不会显示这些 run。

预期：启动从 1.8s 回到 ~0.8s，并且**不再随其它 session 的历史增长**。

### 5.2 journal：只读尾部一行，而不是全文

- 新增 `_tail_sequence(journal_path)`：以二进制 `seek` 到文件末尾 64 KiB，
  从后往前找到最后一行可解析的 JSON，取 `sequence`；
- `append_event()` 用它替代 `read_text().splitlines()` 全量扫描；
- 仍保留 `cross_process_lock`，多写者下尾部读取是安全的（锁内读到最后一行即最大序号）。

复杂度：每次追加 O(1)（固定 64 KiB 上限），总成本 O(n)；2000 条从 30s 降到毫秒级。

### 5.3 顺带记录、本轮不改

- `load_run()` 内部 `replay_events()` 一次是 O(n)，`list_runs(session=…)` 是 O(runs×n)；
  单 session 的 run 数很少，先不动，避免扩大改动面。
- `WorkflowStore` 的 run 列表没有索引文件；如果将来单 session 也积累大量 run，
  可以再加 `runs.index.json`。

---

## 6. 验证计划

1. 单测（TDD，先红后绿）：
   - adapter `initial_cursors()` 只包含当前 session 的 run；
   - bridge 的 `_initial_agent_cursors()` 走 adapter，不调用无参 `list_runs()`；
   - `append_event()` 在 journal 已有 5000 行时仍是毫秒级（且序号严格递增）；
   - 多写者：两个 store 实例交替 append，序号不重复。
2. 端到端：
   - 真实数据根启动 bridge 计时（期望 ~0.8s）；
   - 真实 LLM workflow e2e（zhanlu-ds）跑一次，确认 journal/进度/详情不回归。

---

## 7. 修复后实测（2026-11-09 晚）

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| 启动（真实数据根，118 runs / 106 sessions） | 1.80 / 1.85 / 1.88s | **0.89 / 0.92 / 0.77s** |
| 启动（空数据根） | 0.74–0.81s | 0.79–0.81s |
| 前 1000 次 journal append | 13.27s | **9.26s** |
| 再 1000 次 append | 18.35s | **9.31s**（不再随行数增长） |
| 单测 | — | 新增 5 例全绿；`test_workflow_store` + `test_workflow_runtime` 144 例、`test_ink_bridge` 149 例全绿 |

启动已经回到空数据水平，并且**再也不会随其它 session 的历史增长**。

journal 剩余的 ~9s/2000 条不是算法问题：profile 显示耗时几乎全在 `_io.open`
（每次约 3ms，本机 Windows Defender 实时扫描的典型开销），200 次 append 有 400 次 open。
O(n²) 已消除——第二个 1000 条与第一个 1000 条耗时相同。
如果以后要再压，方向是"同进程内缓存 last sequence + 用文件大小校验"，
把每次 append 的 open 减到 1 次；本轮不做，避免引入多写者下的序号风险。

## 8. 变更清单

| 文件 | 改动 |
| --- | --- |
| `genericagent/core/control/workflow.py` | 新增 `WorkflowChildAdapter.initial_cursors()`（per-session 播种，session 未知时不扫描） |
| `frontends/ink/bridge.py` | `_initial_agent_cursors()` 的 workflow 分支改为委托给 adapter，不再调用无参 `store.list_runs()` |
| `genericagent/workflow/store.py` | 新增 `_tail_sequence()`（64 KiB 尾部窗口），`append_event()` 不再全量重读 journal |
| `tests/test_workflow_perf_paths.py` | 新增 5 例：adapter 作用域、session 未知不扫描、bridge 委托、5000 行 journal 追加耗时、双写者序号唯一 |
