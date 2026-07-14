# GA UI 用户输入重复显示 — 详细修复方案（Codex 对齐修订版）

**日期：** 2026-07-14  
**状态：** 已实施主方案 P0（`messagePartition` Codex 对齐）；进度见 `docs/ga_ui_user_input_duplicate_display_implementation_progress_2026-07-14.md`  
**关联诊断：** `docs/ga_ui_user_input_duplicate_display_diagnosis_2026-07-14.md`  
**复现测试：** `frontends/ink-ui/src/grok_user_input_duplicate.test.ts`（5/5 pass，锁定现状 bug）  
**Codex 参考树：** `D:\git_codes\codex\codex-rs\tui`  
**范围：** 默认 Ink UI inline scrollback（`ga` / `ga ink`）

---

## 0. 一页纸摘要

| 项 | 内容 |
|---|---|
| 现象 | 提交用户消息后，同一条 `> …` 在 scrollback 与 live 区各出现一次 |
| 根因 | 生产序 `user → status:running`：user 先作为 finalized 进 `<Static>`（正确方向），随后 `keepLatestTaskActive` 又把**整轮含 user** 拉进 live 再画一次（错误） |
| Codex 对照结论 | Codex **故意**把 user **立即且仅一次**写入 terminal scrollback；live/`active_cell` 只承载 streaming/tool，**不重画 user** |
| 原方案偏差 | 旧稿“open-turn 期间 user 只留 live、禁止进 Static”与 Codex **相反**，且与 GA 已选择的 inline scrollback 目标不一致 |
| **修订后主方案** | **分区语义对齐 Codex：done 的 user 只走 Static；active 仅含未完成消息（streaming assistant 等）** |
| 辅方案 | 可选：user 去重键（对齐 `last_rendered_user_message_display`）；bridge 调序非必须 |
| 不修什么 | 不回退 alt-screen；不做字符串糊弄去重；不把 user 长期钉在 live |

验收一句话：

> 生产顺序 `user → status:running → assistant_delta…` 下，用户探针在 Static 写 **一次**，live 帧 **不含** 该 user 文本；仅 assistant/thinking 在 live。

---

## 1. Codex 源码怎么做（只读结论）

### 1.1 架构分层（`chatwidget.rs` 模块头注释）

Codex 明确区分：

| 层 | 名称 | 内容 |
|---|---|---|
| 已提交 transcript | finalized `HistoryCell` | 经 `InsertHistoryCell` → `insert_history_lines` 写入**终端正常 scrollback** |
| 进行中 | `transcript.active_cell` | 可原地突变的 streaming / exec / tool 组 |
| 底部 | composer + task running spinner | busy 指示，不重复画 user prompt |

关键注释原文意图：

> committed transcript cells (finalized) **vs** in-flight `active_cell` that mutates while streaming.

### 1.2 User 提交 → **立刻**进 history（不是等 turn 结束）

调用链：

```text
submit_user_message*
  → on_user_message_display(display)
       → last_rendered_user_message_display = Some(display)   // 去重锚点
       → add_to_history(new_user_prompt(...))
            → AppEvent::InsertHistoryCell
                 → app 侧 transcript_cells.push
                 → insert_history_cell_lines → insert_history_lines*
```

证据：

- `chatwidget/input_submission.rs`：提交后 `on_user_message_display(display)`（“Show replayable user content in conversation history”）
- `chatwidget.rs` `on_user_message_display`：直接 `add_to_history(history_cell::new_user_prompt(...))`
- `chatwidget.rs` `add_boxed_history`：对可见 cell 发 `InsertHistoryCell`（**显式 commit API**，不是每帧从全量 messages 推导 Static 列表）
- `insert_history.rs`：模块说明 *“Inserts finalized history rows into terminal scrollback”*；用 scroll region / 反向索引把行插到 viewport **上方**

含义：

1. User **不属于** live active cell。  
2. User **一提交就 finalized 进 scrollback**。  
3. 之后 agent streaming 只动 active / stream controller，**不会再把同一 user 画一遍**。

### 1.3 防双写：显示层去重

`on_committed_user_message` / steer 路径会比较：

```rust
last_rendered_user_message_display.as_ref() != Some(&display)
```

同一 display 不二次 `on_user_message_display`。这是 **commit 幂等**，不是 “keep user in live”。

### 1.4 Streaming 的 live commit（相关但非本 bug 主因）

`vt100_live_commit` / `RowBuilder::drain_commit_ready`：流式行稳定后 **分批 commit 进 scrollback**，live ring 只保留尾部。  
模式仍是：**只 commit 一次；live 与 scrollback 不重叠同一行**。

### 1.5 与 GA 现状对照

| 维度 | Codex | GA 默认 inline（现状） | 评价 |
|---|---|---|---|
| scrollback 写入模型 | 显式 `InsertHistoryCell` 追加 | 每帧用 `staticMessages` 喂 Ink `<Static>`（按 length 追加） | GA 更脆：列表成员在 static↔active 间搬家会“先写死后又活” |
| user 归属 | **立即 finalized** | user 恒 `done:true`；idle 时进 static（碰巧像 Codex），running 时又进 active | **半对齐 + 半冲突** |
| active 内容 | streaming/tool cell | 最新 task 的 **全部**消息（含 done user） | **此处偏离导致双显** |
| 去重 | `last_rendered_user_message_display` | 无 | GA 缺 commit 幂等 |
| busy 与 transcript | bottom pane spinner，不绑 user 分区 | `keepLatestTaskActive` 绑 `status`，误伤 user 分区 | 信号用错层 |

**一句话对齐目标：**

> 学 Codex：user **只 commit 到 scrollback 一次**；live **永不重画已 commit 的 user**。  
> 不要学成：“user 整轮关在 live 里、结束再进 Static”——那是旧稿方案 A，与 Codex 相反。

---

## 2. 问题与约束回顾

### 2.1 已证实事实

1. bridge 每任务只 emit 一次 `user`；state 里也只有一条。  
2. 生产序：`user` → `status:running`。  
3. T1（仅 user、status idle）：partition 把 user 放进 `staticMessages` → Static 写屏。  
4. T2（status running）：`keepLatestTaskActive=true` 把 **含 user 的整 task** 放进 `activeMessages` → live 再画。  
5. Static 不可撤销 → 肉眼双显。  
6. grok 测试已锁定上述链路。

### 2.2 修复后必须遵守的产品意图

- 保持 inline scrollback / 可复制 / 原生滚动（Codex 主聊天同款方向）。  
- finalized 进终端 scrollback；live 只放“进行中”内容。  
- `history_replace` / `rewind` / `/clear` 仍可 `staticTranscriptGeneration` 重置。  
- `GA_INK_MOUSE=full` 不强制改行为。

### 2.3 非目标

- 本轮不重写为完整 Codex `insert_history_lines` + scroll region 引擎（中长期可演进）。  
- 不引入 ratatui；仍用 Ink `<Static>` 作为“追加 commit”近似。  
- 不把 user 改成 `done:false` 全协议大改（除非后续单独立项）。

---

## 3. 方案比选（修订）

| 方案 | 做法 | 与 Codex | 单独能否修 | 结论 |
|---|---|---|---|---|
| **P0. 分区：active 排除已 done 的 user** | 最新 task 进 active 时跳过 `role==user && done`；static 保留全部 done | **同向** | **能** | **主方案** |
| **P0′. active 仅 `!done` 消息** | active = 全表 `!done`（通常仅 streaming assistant）；static = 全部 `done` | **更贴 Codex** | **能** | **主方案优选表述** |
| P1. commit 去重键 | 记录已 Static 提交的 `u-${taskId}`，live 强制跳过 | 对齐 `last_rendered_*` | 能（防御） | **辅** |
| P2. openTaskId 把 user 钉在 live | user 结束前不进 Static | **反向** | 能消双显但偏离 scrollback 目标 | **降级 / 不推荐** |
| P3. bridge 先 running 再 user | 缩窗口 | 无关 | 不能从根上保证 | **可选、低优先级** |
| P4. 显式 InsertHistory 队列 | 仿 Codex 事件 commit，Static 只消费 commit 队列 | 最像 Codex | 能 | **中长期** |
| P5. live 文本去重 | 按字符串丢行 | 弱 | 否 | **不做** |

### 3.1 为何废弃“主方案 = open-turn 留 live”

旧稿目标：

> 从 user 到达起到本轮结束，user 不得进入 Static。

Codex 实际是：

> user **立即**进入 scrollback；之后 **不得再进入 live**。

GA 的 bug 出在第二句失败，不是第一句“进了 Static”。  
T1 把 user 写入 Static **正是 Codex 方向**；T2 把 user 拉进 live 才是缺陷。

修订后的不变量：

| ID | 不变量 |
|---|---|
| **I1** | 任一 `taskId` 的 user 文本，在默认 inline 模式下至多被 `<Static>` 提交一次（clear/replace 重置除外） |
| **I2** | live viewport **不得**渲染已 `done` 的 user 行 |
| **I3** | streaming assistant（`!done`）只在 live（或 Codex 式分批 commit 后的尾部；本轮保持整段 live 即可） |
| **I4** | `state.messages` 对每个 task 至多一条 user |

---

## 4. 主方案详细设计（P0 / P0′：Codex 对齐分区）

### 4.1 目标语义

对最新 task `T`：

| 阶段 | messages(T) | static | active (live) |
|---|---|---|---|
| 刚提交 user | `[user done]` | `[user]` | `[]` 或仅占位 ready |
| running + 尚无 assistant | `[user done]` | `[user]` | `[]`（thinking 走 activity 行，不走 user 行） |
| streaming | `[user done, asst !done]` | `[user]` | `[asst streaming]` |
| 完成 | `[user done, asst done]` | `[user, asst]` | `[]` |
| put_task 失败 | `[user done]` | `[user]` | `[]` |

全程 user **只出现在 static 通道**，故 Static 写一次、live 零次 → 无双显。

### 4.2 推荐算法（`messagePartition.ts`）

将“是否 keep 最新 task”与“active 里放什么”拆开：

```ts
export function splitStaticAndActiveMessages(
  messages: ChatMessage[],
  options: { keepLatestTaskActive?: boolean } = {},
): MessagePartition {
  const latestTaskId = findLatestTaskId(messages)
  if (latestTaskId === undefined) {
    return {
      staticMessages: messages.filter(m => m.done),
      activeMessages: messages.filter(m => !m.done),
    }
  }

  const hasPending = messages.some(m => m.taskId === latestTaskId && !m.done)
  const holdActive = hasPending || options.keepLatestTaskActive

  if (!holdActive) {
    return {
      staticMessages: messages.filter(m => m.done),
      activeMessages: [],
    }
  }

  // Codex 对齐：active 只收“未完成”内容；已 done 的 user/assistant 一律 static
  const staticMessages = messages.filter(m => m.done)
  const activeMessages = messages.filter(m => !m.done)
  // 可选收紧：active 仅 latestTaskId 的 !done
  // const activeMessages = messages.filter(m => m.taskId === latestTaskId && !m.done)

  return { staticMessages, activeMessages }
}
```

要点：

1. **删除**“从 `activeStart` 起整切片”逻辑——那会把 done user 拖进 active。  
2. `keepLatestTaskActive` 仅决定 **是否需要 live 区**（running 时即使还没有 `!done` assistant，也可显示空 live + Thinking activity）；**不再**决定 user 进 active。  
3. 与 Codex 一致：user done ⇒ 只在 committed/static。

### 4.3 `App.tsx` 侧

```ts
const keepLatestTaskActive =
  state.status === 'running' || state.status === 'stopping'

const messagePartition = useMemo(
  () => splitStaticAndActiveMessages(state.messages, { keepLatestTaskActive }),
  [keepLatestTaskActive, state.messages],
)
```

`keepLatestTaskActive` 可保留，用于：

- running 且尚无 assistant 时 `planMessageViewport` 仍可 `live`/`ready`（避免布局塌缩）  
- **不再**把 user 塞进 `activeTranscriptRows`

若 `activeTranscriptRows.length === 0` 且 running：沿用现有 activity 行（`Thinking...`），与 Codex bottom spinner 类似。

### 4.4 时序（修复后，生产顺序）

```text
user(task=9)
  messages=[u-9 done]
  partition: static=[u-9], active=[]
  Static 写入 "> …" 一次          ← 唯一 user 显示（Codex 同）

status:running
  keepLatestTaskActive=true
  partition: static=[u-9], active=[]   // user 不再进 active
  live: 空 / Thinking activity
  Static 不再重复写（items 无新增）

assistant_delta
  messages=[u-9 done, a-9 !done]
  static=[u-9], active=[a-9]
  live 只画助手流

assistant_done + idle
  static=[u-9, a-9], active=[]
  Static 追加助手行一次
```

### 4.5 与旧 partition 测试的关系

现有 `messagePartition.test.ts`：

```ts
// keepLatestTaskActive:true 时 active 含 u-2
assert active == [u-2]
```

修复后应改为：

```ts
// keep true + 仅 user done → active []，static 含 u-2
// keep true + user done + asst !done → active [a-2]，static 含 u-2 与更早 done
```

这是 **有意的语义变更**，不是测试噪音。

### 4.6 为何不必 `openTaskId`（相对旧稿）

| 旧 openTaskId 职责 | 新分区是否覆盖 |
|---|---|
| 防止 user 在 idle 帧进 Static | 不需要防——**应该**进 Static |
| running 时 user 在 live | **禁止**——与 Codex 冲突 |
| 失败收口 | user 已在 static，idle 即可 |

因此 **P2 openTaskId 降级为不推荐**；除非未来做“user 乐观 live 预览再 commit”的动画，本 bug 不需要。

---

## 5. 辅方案

### 5.1 P1 — 已提交 user 去重（对齐 Codex `last_rendered_user_message_display`）

在 `App.tsx` 或 state 中维护：

```ts
committedUserTaskIds: Set<number> // 或 lastCommittedUserKey: string
```

规则：

- 当某 `u-${taskId}` **首次**进入 `staticTranscriptRows` 并完成 Static 渲染后，记入 set。  
- 计算 `activeTranscriptRows` 时过滤 `role==user && committedUserTaskIds.has(taskId)`（主方案下本已不含，属双保险）。  
- `history_replace` / `clear` / `staticTranscriptGeneration++` 时清空 set。

收益：防止未来其它路径把 user 又推进 active；接近 Codex 幂等 commit。

### 5.2 P3 — bridge 调序（低优先级）

```python
# ink_bridge.submit
self.emit({"type": "status", "status": "running", "taskId": task_id})
self.emit({"type": "user", "taskId": task_id, "text": visible_text})
```

主方案落地后 **非必须**。仅当希望 status 与 user 更贴近“已在 turn 中”的 UI 文案时可做。  
单独调序 **不能** 替代 P0：即便先 running，若 active 仍切片含 user，一旦某路径 keep 为 true 仍会双画（例如先 user 的其它入口）。

### 5.3 P4 — 中长期：显式 commit 队列（真·Codex）

```text
messages 状态机
  → commitQueue: TranscriptLine[]   // 仅追加 finalized 行
  → <Static items={commitQueue}>   // 或自定义 insert_history
  → live = streaming only
```

`history_replace`：generation++ 并重建 queue。  
resize reflow：从源 messages 重算 queue（Codex 有 `transcript_reflow` / resize 路径）。

本轮 bugfix **不阻塞**在 P4；P0 足够。

---

## 6. 实施步骤（TDD）

### Phase 0 — 准备

1. 读诊断文档 + 本修订方案 + Codex `chatwidget.rs` / `insert_history.rs` / `on_user_message_display`。  
2. 跑 grok 测试确认 bug 仍锁定。  
3. 分支：`fix/ink-user-static-once`。

### Phase 1 — 红：改断言方向（注意：与旧稿相反）

**旧稿错误门禁：** running 窗口 user 不得进 Static。  
**修订门禁：**

| 阶段 | 断言 |
|---|---|
| `user` 后（可尚 idle） | 允许/期望 Static 含探针；**live GenericAgent 帧不含探针** |
| `status:running` 后 | Static 仍可含探针；**live 仍不含探针**；探针在“Static 风格 chunk”中出现，不在 live 帧重复叠加导致 `total>=2` 的 **双通道** |
| `assistant_delta` | live 可含助手文本；仍不含 user 探针 |
| 对照 `running→user` | 同样 user 不进 live |

更硬的计数方式（推荐）：

```ts
const staticHits = countIn(staticOnlyChunks, UNIQUE)
const liveHits = countIn(liveFrames, UNIQUE)
assert.equal(liveHits, 0)
assert.ok(staticHits >= 1)
// 注意：Static 追加模型下 staticHits 对同一行通常为 1
```

同步改 `messagePartition.test.ts`：

- `keepLatestTaskActive:true` + 仅 user → `active=[]`，static 含该 user  
- + streaming assistant → active 仅 assistant  

### Phase 2 — 绿：实现 P0

1. 改 `splitStaticAndActiveMessages` 为 §4.2。  
2. 确认 `App.tsx` 无需 openTaskId。  
3. 跑 partition / grok / App 测试。

### Phase 3 —（可选）P1 去重 set

仅当 Phase 2 后仍有边角双写，或希望显式文档化 Codex 幂等。

### Phase 4 —（可选）P3 bridge 调序

独立小 commit；更新 `tests/test_ink_bridge.py` 若断言顺序。

### Phase 5 — 回归与提交

```bash
cd frontends/ink-ui && npm test
python -m unittest tests.test_ink_bridge
```

人工：`ga` 提问 → user 一条；streaming 时上方 scrollback 已有 user，下方 live 只有助手/Thinking。

Commit 示例：

```text
fix(ink-ui): 对齐 Codex，避免已提交 user 再进入 live 导致双显
```

---

## 7. 预期改动文件

| 文件 | 改动 |
|---|---|
| `frontends/ink-ui/src/messagePartition.ts` | **主**：active 不含 done user；停止整 task 切片 |
| `frontends/ink-ui/src/messagePartition.test.ts` | 更新 keep-active 期望 |
| `frontends/ink-ui/src/grok_user_input_duplicate.test.ts` | 断言改为 static 一次、live 零次 user |
| `frontends/ink-ui/src/App.test.ts` | 检查依赖旧 active 含 user 的用例并更新 |
| `frontends/ink-ui/src/App.tsx` | 通常仅注释/小调整；不引入 openTaskId |
| `frontends/ink_bridge.py` | 可选调序 |
| `docs/ga_ui_user_input_duplicate_display_diagnosis_2026-07-14.md` | 可选：注明“修复方向已 Codex 修订” |

**预计：** 小（partition + 测试为主，约 30–80 行逻辑）。

---

## 8. 边界情况

| 场景 | 期望 |
|---|---|
| 生产序 user→running | user 仅 Static；live 无 user |
| 对照 running→user | 同上 |
| streaming | live 仅助手；user 留在 scrollback |
| 仅 running 无 assistant | live 空 + Thinking；user 已在上方 scrollback |
| put_task_failed | user 已在 Static 一次；不进 live |
| 连续两轮相同文本 | 两条 user 各 commit 一次（不同 taskId）；允许文本相同 |
| history_replace / resume | generation 重置，整段重放 Static；live 空 |
| rewind | 截断 + generation；无双显 |
| local command | 无 taskId，行为不变 |
| full mouse | 单一 viewport；本 bug 本不存在；勿破坏 |

### 8.1 危险错误实现

1. **继续用 `messages.slice(activeStart)` 含 done user** → bug 原样。  
2. **强制 user 整轮只在 live**（旧 open-turn）→ 与 Codex/可复制 scrollback 目标相反；resize/选中历史 user 变差。  
3. **按文本全局去重** → 用户连发相同问题丢行。  
4. **修复后不更新 partition 测试** → CI 假绿或假红。  
5. **只调 bridge 顺序** → 未改 active 切片则仍可能双显。

---

## 9. 测试计划

### 9.1 自动化

```bash
cd frontends/ink-ui
npx tsx --test src/messagePartition.test.ts
npx tsx --test src/grok_user_input_duplicate.test.ts
npx tsx --test src/App.test.ts
npm test
python -m unittest tests.test_ink_bridge
```

### 9.2 修复后 grok/App 断言模板

```ts
// user → delay → running → settle
assert.equal(liveHits(UNIQUE), 0)
assert.ok(staticHits(UNIQUE) >= 1)

// + assistant_delta
assert.equal(liveHits(UNIQUE), 0)
assert.match(lastLiveFrame(), /助手片段/)

// + assistant_done + idle
assert.equal(liveHits(UNIQUE), 0)
assert.ok(staticChunks.some(c => c.includes(UNIQUE) && c.includes('助手最终')))
```

### 9.3 人工清单

1. `ga` 默认模式发问：scrollback 一条 user，running 时下方不重复。  
2. 观察 Thinking / 流式输出只在 live。  
3. 完成后历史顺序 user→assistant 正常。  
4. `/clear`、resume、rewind 无异常双倍历史。  
5. 选中 scrollback 中的 user 文本可复制（inline 目标不回退）。

---

## 10. 风险与回滚

| 风险 | 等级 | 缓解 |
|---|---|---|
| running 早期 live 过空，布局跳动 | 低 | 保留 activity 行 / ready 占位 |
| 依赖“active 含 user”的 UI 假设 | 中 | 全量 App.test；人工看标题区 |
| Static 在 user 到达瞬间写入，失败轮次也进 scrollback | 低（Codex 同） | 可接受；失败可再 system 行说明 |
| 旧诊断/旧方案读者混淆 | 中 | 本文标明修订；诊断可加勘误指针 |

回滚：还原 `messagePartition.ts` + 测试即可。

---

## 11. Definition of Done

- [ ] partition：done user 永不进 `activeMessages`  
- [ ] 生产序自动化：`liveHits(user)=0` 且 `staticHits(user)>=1`  
- [ ] streaming 时 live 有助手、无 user  
- [ ] 既有 resume/static scrollback 测试通过或按新语义更新  
- [ ] 人工验收 1–4  
- [ ] 不引入 openTaskId 作为主路径（除非文档化例外）

---

## 12. 工作量

| 项 | 人时 |
|---|---|
| partition + 单测 | 0.5–1 h |
| grok/App 断言修订 | 0.5–1 h |
| 回归 + 人工 | 0.5 h |
| 可选 P1/P3 | +0.5 h |
| **合计** | **约 0.5 日**（比旧 open-turn 方案更小） |

---

## 13. 与旧版方案文档的差异（勘误）

| 主题 | 旧版 fix plan | 本修订版 |
|---|---|---|
| 主修复目标 | user 结束前禁止进 Static | user **立即**进 Static；**禁止再进 live** |
| 主机制 | `openTaskId` + keep active | **partition 排除 done user** |
| bridge 调序 | 辅且强调 | 可选、低优先级 |
| Codex | 未深入 | 以 `on_user_message_display` / `InsertHistoryCell` / `insert_history` 为准绳 |
| 验收 | stdout 探针 running 窗口恰好 1 且不在 Static | **live 0 + static 1**（通道分离） |
| 工作量 | 0.5–1 日偏状态机 | 更小，集中 partition |

旧版对根因 **T1/T2 时序** 的描述仍正确；错误在于 **把 T1 的 Static 写入当成必须消灭的缺陷**。相对 Codex，T1 是正确方向，T2 才是 bug。

---

## 14. 伪代码汇总

### 14.1 partition（核心）

```ts
const staticMessages = messages.filter(m => m.done)
const activeMessages = holdActive
  ? messages.filter(m => !m.done) // 可选: && m.taskId === latestTaskId
  : []
```

### 14.2 不要写

```ts
// 反模式：整 task 切片（会含 done user）
const activeMessages = messages.slice(activeStart)

// 反模式：为消双显禁止 user 进 static
if (isOpenTurn) keepUserOutOfStatic()
```

### 14.3 可选去重

```ts
if (event.type === 'history_replace' || clear || rewind) committedUserTaskIds.clear()
// live 渲染：
activeMessages.filter(m => !(m.role === 'user' && committedUserTaskIds.has(m.taskId)))
```

---

## 15. 结论

Codex 用 **显式、单向、幂等的 history commit** 把 user 放进 terminal scrollback，live 只服务 streaming——从结构上消灭“同一 user 两通道”。

GA 已用 Ink `<Static>` 近似 commit，但 `splitStaticAndActiveMessages` 在 `keepLatestTaskActive` 时把 **已 done 的 user** 又切进 live，与 Codex 冲突，造成双显。

**修复应向 Codex 靠拢：改分区，而不是把 user 从 Static 抢回 live。**

主路径：

1. `activeMessages = only !done`  
2. `staticMessages = all done`（含最新 user）  
3. 测试改为断言 **live 无 user / static 有 user**  
4. 可选 commit 去重 set；bridge 调序非必须  

实施后，用户输入在 scrollback 中只出现一次，并可继续被终端原生选中复制——这才是 inline scrollback 的本意。
