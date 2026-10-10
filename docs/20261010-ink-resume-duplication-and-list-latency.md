# GA Ink `/resume`：恢复后内容重复 + 历史会话列表卡顿

日期：2026-10-10
证据截图：`resume_bug.png`（用户本地提供，未入库）（同一轮 `> 简单回答 2 / LLM Running (Turn 1) … / Summary: … / 2` 完整出现两次，
最后一行是 `恢复完成：1 轮历史 · session_106e1603742e4b9c9956e1855e8986c0.jsonl`）

用户报告两个现象：

1. `/resume` 恢复之后，GA Ink 的转录区出现**重复**：恢复内容里"尾部"那一段在终端里出现了两份。
2. 在 GA Ink 里输入 `/resume` 之后，历史会话列表要**卡顿一会儿**才出现。

两个现象根因完全无关，分开处理。

---

## 一、`/resume` 恢复后重复

### 根因：两次 commit 各写一次（先"追加尾部"，再"全量重印"）

`App.tsx` 的 `history_replace` 分支原来是：

```ts
dispatch(event)                 // messages 变长
resetStaticTranscriptOutput()   // staticTranscriptGeneration + 1 → <Static> 换 key 重挂
```

Ink v5 用的是**legacy root**（`node_modules/ink/build/ink.js`：`reconciler.createContainer(this.rootNode, 0, …)`
注释写着 `// Legacy mode`）。legacy root **不会**对 React 事件之外的多次更新做自动批处理，于是
上面两行是**两次独立的 commit**：

1. 第一次 commit：`messages` 变长、`<Static>` 的 key 没变 → ink 只把"新增的那几条"追加写进
   scrollback（`> RESTORED-USER-2 / RESTORED-ASSISTANT-2`）；
2. 第二次 commit：key 变化 → `<Static>` 重挂 → ink 把**整段历史**再写一遍。

尾部因此写了两份。`/clear`、`/compact`、`new_session` 不受影响（它们的 `history_replace`
要么 messages 为空、要么比当前历史短，第一次 commit 什么都不追加），所以只有
**恢复的历史比当前会话长**时才看得见 —— 正是用户截图里的场景。

字节级证据（探针，临时文件已删）：

```
--- 1 ---                       ← 第一次 commit：只有恢复内容的「尾巴」
> RESTORED-USER-2
RESTORED-ASSISTANT-2
--- 4 ---                       ← 重挂 Static 后的第二次 commit：全量重印
> RESTORED-USER-1
RESTORED-ASSISTANT-1
> RESTORED-USER-2
RESTORED-ASSISTANT-2
### total RESTORED-USER-2 = 2
```

顺带确认：`CURRENT-TURN-ALPHA` 出现 2 次是**正常**的（Static 写一份进 scrollback，live 帧里还有
一份），不要当 bug 修。

### 修复：把"重印代数"并入 reducer，让 messages 与 key 落在同一次 commit

`staticGeneration` 从 App 的 `useState` 移进 `state.ts` 的 `AppState`，由 `history_replace` /
`rewind_done` 两个 action **原子地** `+1`：

```ts
if (event.type === 'history_replace') {
  return { ...state, messages: …, staticGeneration: state.staticGeneration + 1, error: null }
}
```

`<Static key={String(state.staticGeneration) + ':' + staticTranscriptGeneration}>` 把两个来源合起来：
reducer 的代数负责"整段替换/裁剪"，App 本地那个 `staticTranscriptGeneration` 只留给 resize
重绘用。这样一次 `dispatch(event)` 就是一次 commit、一次重印，不依赖 React 的批处理行为
（legacy root 上没有批处理可依赖）。

### 同一类的兄弟 bug：`/rewind` 会把"留下的历史"再印一遍

`/rewind` 让历史**变短**，但 `<Static>` 只能追加、无法撤销已经写进 scrollback 的行。旧代码同样
是 `dispatch(event)` + `resetStaticTranscriptOutput()`：第一次 commit 什么都不追加，第二次 commit
重挂 `<Static>` → 把裁剪后**仍在屏幕上**的那部分历史又印一遍。探针（虚拟终端）实测
`ORIGINAL-USER-1` 在屏幕上出现 2 行。

修法：rewind 先做一次整屏重置（清屏 + 清 scrollback + 光标归位，与 resize 用的是同一段
`resetViewportSequence()` / `cursorPark.resetViewport()`），再由 reducer 原子地重印 —— 屏幕上只剩
裁剪后的那一份。`App.tsx` 里新增的 `resetTranscriptViewport()` 同时被 resize 与 rewind 复用，
避免两处各写一份。

---

## 二、`/resume` 列表卡顿

### 根因：列表路径把"全量解析"做了两遍，还要做 O(文件数²) 的子序列比对

`/resume` 的列表来自 `frontends/continue_cmd.py::list_sessions()`，它有两处开销：

1. **transcript 侧**：`session_transcript.list_sessions()` 对每个 `session_*.jsonl` 调
   `load_session()` —— 全量 JSON 解析 + 每个 turn 的 `backend_history_before/after` 深拷贝。
   本机 1336 个文件 / 78.9 MB。
2. **legacy 侧**：`temp/model_responses/model_responses_*.txt`，本机 1135 个文件 / 145.9 MB。
   每个文件先整读一遍做 `_pairs()`（惰性 `.*?` + 前瞻的正则，逐块回溯），再被
   `_ui_session_users(f)` **第二次整读 + 全量重建**（`extract_ui_messages` 会对每个 response 做
   `ast.literal_eval`、对每个 prompt 做 `json.loads`）—— 只是为了拿"用户消息文本"这一列。
3. **去重**：每个 legacy 文件的用户消息序列都要和**所有** transcript 序列做滑窗比对。

实测（修复前）：

| 阶段 | 耗时 |
|---|---|
| `load_session()` × 1336 | 995 ms |
| legacy 整读 145.9 MB | 650 ms |
| legacy `_pairs()`（旧正则） | 3526 ms |
| legacy `_ui_session_users()` | 5003 ms |
| `_preview_text()` | 41 ms |
| **合计** | **≈ 9–17 s** |

而且 Ink bridge 一次 `/resume` 会调它**两次**（`list_resume_sessions()` 列列表，
`resume_session_by_index()` 再按序号取一次），所以用户感受到的是 2 倍。

### 修复

1. **列表不再走 `load_session()`**：新增 `SessionSummary` + `_scan_session_summary()`，只扫
   列表需要的字段（session_id / preview / rounds / last_seq / 用户消息文本），不做深拷贝、
   不构造 `ui_messages`。turn 簿记（推断式 rewind、显式 `rewind`、`compact`）与 `load_session`
   逐条对齐，所以 `preview`/`rounds`/`last_seq` 与全量加载一致（对 1336 个真实文件逐一比对，
   0 处不一致）。`list_sessions()` 的返回类型由 `LoadedSession` 改为 `SessionSummary`，并明确
   写着"要恢复请用 `load_session()`"。
2. **`_pairs()` 换成 `re.split`**：按标记行切分，取代惰性 `.*?` + 前瞻的回溯式 `findall`。
   在全部 1135 个真实 legacy 日志上与旧正则输出**完全一致**（含空文件、缺尾换行等边界），
   耗时 3526 ms → ≈ 1.3 s。`export_cmd.py` 复用同一个 `_pairs()`，一并受益。
3. **用户消息文本直接从已切好的 pairs 取**：`_scan_legacy_summary()` 用 `_user_text(prompt)`，
   不再二次整读 + 重建整段对话。5003 ms → ≈ 0.26 s，且与旧 `_ui_session_users()` 在全部
   1135 个文件上逐条相等。
4. **去重加索引**：只有"首个用户消息出现在某条 transcript 序列里"的 legacy 文件才可能是它的
   连续子序列，因此按用户消息文本建 `sequences_by_first_user` 索引 —— 这是**必要条件**，
   结果与全量比对等价，但把 O(文件数²) 的滑窗比对压到个位数候选。
5. **持久化列表缓存**（新增根模块 `listing_cache.py`）：每个文件的列表字段按
   `(mtime, size)` 缓存在同目录的 `.listing_cache.json`（`temp/sessions/` 与
   `temp/model_responses/` 各一份，都在 `.gitignore` 的 `temp/` 里）。追加会同时改变 mtime 与
   size，所以过期条目不可能被当成新数据；写盘用 `mkstemp + os.replace` 原子替换；缓存缺失/
   损坏/不可写都只是"重新扫描"，绝不影响列表内容。
   另外：内容决定"没有可解析对话"的文件记一条负结果（1135 个 legacy 日志里有 367 个是这种），
   否则它们每次 `/resume` 都要被重读。

### 修复后实测（同一台机器、同一份 2471 个文件的语料）

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 冷启动（无缓存，第一次 `/resume`） | 9–17 s | **3.0 s** |
| 热启动（缓存就绪，之后每次 `/resume`） | 9–17 s | **0.09–0.15 s** |

`/resume` 现在两次调用（列列表 + 按序号恢复）都是热路径，用户感知到的等待从"十几秒"降到
"几乎瞬时"。

真实链路（不是进程内调用）：直接启动 `frontends/ink_bridge.py`，向它的 JSONL 循环发命令实测 ——
bridge ready 2.2 s，`list_resume_sessions` **245 / 260 / 284 ms**（1032 条会话），也就是 Ink UI
里 `/resume` 打开选择器与按序号恢复这两次调用现在都是几百毫秒级。

正确性：把旧实现（旧 `_pairs` 正则 + `_ui_session_users` + `load_session` 全量列表）原样重建，
对 `{}` / `exclude_session_id` / `exclude_pid` / `exclude_path` 四种调用各比对一次，
**输出逐条相等**（含排序）。

---

## 三、验证

- Ink UI：`npx tsx --test src/*.test.ts` → 399/399；`npx tsc --noEmit` 干净。
  新增 `resumeTranscriptDuplication.test.ts`：
  - `resume prints each restored transcript row exactly once when restored history is longer`
    （逐探针断言出现次数 == 1，修复前为 2）；
  - `rewind leaves exactly one copy of the trimmed transcript on screen`
    （用 `resizeReflowModel.ReflowTerminal` 做屏幕级断言：留下的 turn 各 1 行，被 rewind 掉的
    answer 0 行）。
  两个用例在 stash 掉修复后**都是红的**，修复后转绿。
- Python：`python -m unittest tests.test_continue_cmd_resume tests.test_session_transcript
  tests.test_resume_listing_cache tests.test_ink_bridge tests.test_agentmain_llm_sessions
  tests.test_agentmain_yaml_sessions tests.test_tui_recent_sessions tests.test_tui_input_history
  tests.test_tgapp_stream_segments` → 157/157。
  新增 `tests/test_resume_listing_cache.py`（7 例）：二次列表不重扫、追加只重扫那一个文件、
  列表字段与全量加载一致（含推断式 rewind + 显式 rewind）、缓存文件损坏可自愈、
  legacy 追加后重扫、无 pairs 的日志只读一次、`_pairs` 与旧正则等价。

## 四、必须保持的不变量

1. **`history_replace` / `rewind_done` 只允许一次 commit 完成"换历史 + 换 Static key"。**
   重印代数必须由 reducer 原子 `+1`（`state.staticGeneration`）；不要在 bridge 事件处理里再
   单独调 `resetStaticTranscriptOutput()`，否则 legacy root 会先追加、后重印 → 重复。
   `resetStaticTranscriptOutput()` 只留给"历史没变、纯粹重画"的场景（终端 resize）。
2. **`<Static>` 只能追加。** 任何"历史变短/被替换后要重印"的路径都必须先整屏重置
   （`resetTranscriptViewport()`），否则旧行会留在 scrollback 上。
3. **列表路径不得调用 `load_session()`。** 列表只需要 `SessionSummary` 的字段；要恢复才用
   `load_session()`。`SessionSummary.user_texts` 是 `continue_cmd` 去重用的唯一来源。
4. **`(mtime, size)` 是列表缓存的唯一失效依据。** 任何"原地改写、长度不变"的写入方式都会
   破坏这个前提（当前 transcript / legacy 日志都是纯追加）。
5. **缓存只能是加速器。** 缓存读失败、写失败、条目缺失都必须退化成"重新扫描"，绝不能改变
   列表内容；`listing_cache` 的任何异常都不允许向上抛。

## 五、遗留与边界

- 冷启动 3.0 s 仍然要读 225 MB 并解析一次。若以后还要压，方向是"只读文件头/尾做预筛"或
  多进程并行；本轮没有做，因为收益/风险比不划算，且热路径已经是毫秒级。
- 缓存文件放在被扫描目录里（`.listing_cache.json`）。这些目录只会被 `session_*.jsonl` /
  `model_responses_*.txt` 的 glob 命中，点文件不会被当成会话。
- 本轮的"重复"是**写入侧**重复（多写了一份字节）。终端**显示侧**的重复（reflow 残留）是另一个
  bug，已在 `docs/20261010-ink-resize-reflow-duplication-fix.md` 处理。
