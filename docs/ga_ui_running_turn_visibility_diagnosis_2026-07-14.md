# GA Ink UI — Running 时本轮内容被“刷没”诊断

**日期：** 2026-07-14
**状态：** **阶段 1+2+3 已落地** — P0-A / 增量 commit / viewport 几何（`insertHistory.ts`）；见 `docs/ga_ui_running_visibility_implementation_progress_2026-07-14.md`
**范围：** 默认 inline scrollback（`ga` / `ga ink`，`mouseMode !== 'full'`）
**相关：**
- 用户双显修复：`docs/ga_ui_user_input_duplicate_display_diagnosis_2026-07-14.md`
- 输入框锚点 / content-desired：`docs/ga_ui_composer_position_jump_diagnosis_2026-07-14.md`
**Codex 参考：** `codex-rs/tui` — `insert_history.rs`、`chatwidget/streaming.rs`、`chatwidget/rendering.rs`、`bottom_pane` status indicator、`AppEvent::InsertHistory*`

---

## 0. 结论（先读）

| 项 | 内容 |
|---|---|
| 用户症状 | 本轮提问后出现 `GenericAgent running` / `LLM Running (Turn x)` / `✻ Thinking…` 等，随后 **本轮 user + Running 状态 + 流式内容像被向上盖住/刷没**，直到 **整轮输出完** 才突然整段出现 |
| 用户对比期望 | 应像多轮会话整体推进：旧内容进 scrollback，**本轮 Running + 流式尾始终可见**，而不是“轮次级整块被盖掉” |
| 是否像“只滚 turn1” | **观感像**；机制上是 **Static 不可撤销上写 + live dock 在同一终端区域重绘/增高**，盖住刚写入的 Static 尾（含本轮 user），并不是真正的“只隐藏 turn1 数据结构” |
| 与近期改动关系 | **双显修复**（user 立刻进 Static）+ **二期 content-desired**（running 时空 live 很矮、stream 时 live 长高）**叠加放大** 该覆盖问题 |
| Codex 怎么做 | user → **InsertHistory**（scroll region 上方，必要时 **下移 viewport**）；running 时 **status 在 bottom_pane**；stream → **active_cell 尾在 dock 内**；commit 动画把稳定行再 **InsertHistory**，viewport 跟着走 |
| 推荐修法 | **流式稳定段尽早 commit 进 Static + 增高时先/同步 scroll 历史区**；或 **running 期间 user 不进 Static 直到 turn 结束**（二选一，见 §6）；目标：**Running + 本轮流式始终可见** |

一句话：

> **不是 LLM 没吐字，而是 inline 布局把“本轮 Static 尾 + 增高中的 live dock”画在同一屏幕带上互相覆盖；Codex 用 insert_history 下移 viewport 避免这种盖写。**

---

## 1. 用户现象拆解

### 1.1 时间线（用户体感）

```text
T0  用户提交本轮问题
T1  界面出现 GenericAgent running / ✻ Thinking… / LLM Running(Turn x)
T2  本轮 user 行、Running 行、后续流式字 **看不见 / 被刷没**
    （像 turn 被向上盖住，而不是整会话一起往上挪）
T3  assistant_done / idle 后，**突然**整段本轮内容出现在 scrollback
```

### 1.2 与“正常会话滚动”的差别

| 期望（Codex 族） | 当前 GA 观感 |
|---|---|
| 旧 history 进 scrollback，**viewport 下移** | Static 追加写屏，但 **live Box 仍从当前光标带重绘** |
| dock 内始终能看到 status + stream 尾 | dock 很矮或增高时 **盖住** 刚 Static 的本轮 user |
| 流式过程中内容持续可见 | 过程中“没了”，结束才“啪”一下全出来 |

---

## 2. GA 当前渲染通道（为何会盖）

### 2.1 双通道（inline 默认）

```text
<Static items={staticTranscriptRows} />     // 只追加，进终端 scrollback，不可撤销
<Box>                                       // 每帧可擦除重绘的 live dock
  Header (GenericAgent · running/idle)
  MessageViewport?                          // plan: none | ready | live(content-desired)
  BottomChrome (ActivityView + hint + input + slash)
</Box>
```

分区（`messagePartition.ts`，双显修复后）：

```ts
staticMessages = messages.filter(m => m.done)           // 含本轮 user（done:true 立即）
activeMessages = latestTask 且 !done                    // 仅 streaming assistant
```

状态机（`state.ts`）：

| 事件 | messages 变化 | Static | live |
|---|---|---|---|
| `user` | user `done:true` 追加 | **立刻追加 user 行** | 仍空（若尚无 delta） |
| `status:running` | status 变 running | 不变 | header/activity 显示 Running |
| `assistant_delta` | assistant `done:false` 增长 | 不变 | live 槽 **按行数长高** |
| `assistant_done` | assistant `done:true` | **整段 assistant 进 Static** | live 变 none |

### 2.2 content-desired 后的高度（二期）

`planMessageViewport`：

| 时刻 | live 高度 |
|---|---|
| user 已 Static，尚无 delta | **`none`（0）** |
| 短 stream | `min(liveLines, 12, messageRows)` **逐帧变高** |
| 长 stream | 封顶 12 |

BottomChrome 始终在：`ActivityView`（Running）+ input。

### 2.3 覆盖机制（核心推断）

Ink / 终端行为叠加：

1. **Static 只追加**：本轮 `> user` 被写到 **当前输出位置** 的 scrollback。
2. **live `Box` 每帧从 dock 顶重绘**：header +（可选）MessageViewport + BottomChrome。
3. **stream 时 live 高度变大** → dock 占用更多行 → 相对“刚写完 Static 的光标邻域”做 **向上擦除/重画**。
4. 用户肉眼看到：
   - 刚出现的 **本轮 user** 被 dock 顶上来盖住；
   - 或 Running 行本身在矮 dock 与增高之间 **闪没**；
   - 流式字只在 live 里，被盖/被裁后像“没输出”；
5. **`assistant_done` 后** assistant 整段进 Static，live 塌成 none，Static 尾一次性露出 → **“突然全部显示”**。

这不是 `messages` 丢了，而是 **渲染通道时序 + 高度变化** 造成的 **可见性 bug**。

### 2.4 为何像“轮次级覆盖”而不是“整会话移动”

Codex 在插入 history 时：

```rust
// insert_history.rs
if area.bottom() < screen_size.height {
    area.y += scroll_amount;  // 整块 viewport（含 composer）下移
}
// 历史写在 1..area.top() 的 scroll region
```

GA：

- Static 追加 **不** 同步 `viewport.y +=`；
- live dock 仍按 Ink 的 inline 帧逻辑在 **底部区域原地增高**；
- 结果：**盖住邻近的 Static 尾（往往就是本轮 user）**，而不是把“整个多轮会话 + dock”当作一体下移。

---

## 3. Codex 源码对照（Running 时为何一直看得见）

### 3.1 通道分离

| 内容 | Codex | GA 现状 |
|---|---|---|
| 用户消息 | `InsertHistory` → scrollback（viewport 上方） | Static 追加（**无** viewport 下移） |
| Running / Thinking 状态 | **bottom_pane status indicator**（dock 内，不进 history 乱盖） | live Header + `ActivityView`（与 MessageViewport 同 Box） |
| 流式 assistant | `stream_controller` + **`active_cell` = StreamingAgentTailCell** 画在 dock | live MessageViewport 画 `!done` assistant |
| 稳定行 | commit tick → 再 InsertHistory，viewport 可下移 | 仅 `assistant_done` 时整段进 Static |

### 3.2 流式路径（`chatwidget/streaming.rs`）

```text
on_agent_message_delta
  → handle_streaming_delta
      → StreamController::push
      → StartCommitAnimation / catch_up_commit_tick   // 稳定行逐步 commit
      → sync_active_stream_tail
          → active_cell = StreamingAgentTailCell(tail_lines)  // 只留“尾”
          → 有时 hide_status_indicator（尾可见时）
  → request_redraw  // 只重绘 viewport(dock)
```

要点：

1. **已稳定的流式行** 会通过 commit 动画进 history（scrollback），不是等整轮结束一次性倒出。
2. **dock 里只保留 tail**，高度 content-desired，且插入 history 时 **下移 viewport**，tail 始终贴在 history 下方。
3. Status/Running 属于 **bottom_pane**，与 history 插入的 scroll region 分离。

### 3.3 用户消息（`input_submission` / history）

提交后走 **InsertHistoryCell / on_user_message_display** 一类路径：user 进 scrollback，**同时** viewport 几何按 `insert_history` 更新——user 不会被随后的 dock 重绘“盖没”。

### 3.4 渲染树（`chatwidget/rendering.rs`）

```text
Flex:
  active_cell (stream tail)   flex
  active_hook_cell
  BottomPane (status + composer + popup)  fixed desired_height
```

整块作为 **viewport** 由 `tui.draw(desired_height)` 画在 `viewport_area`；history **从不** 与 viewport 抢同一屏幕带原地增高。

### 3.5 对照表

| 维度 | Codex | GA（双显 + content-desired 后） |
|---|---|---|
| user 可见性 | InsertHistory + 可能 y++ | Static 写后可被 live 盖 |
| Running 可见性 | bottom_pane 稳定 | Header/Activity 在可变高 live Box 内 |
| stream 过程可见 | tail 在 dock + 稳定行持续 commit | 全文挤在 live，增高盖 Static；done 才整段 Static |
| 结束时 | 已大部分在 scrollback | **突然** Static 灌入 → “刷出来” |
| 会话推进 | **viewport 下移** | Static 追加 + live 原地重绘 |

---

## 4. 根因归纳

### 4.1 主因（充分条件组合）

1. **本轮 user 在 running 前就进 Static**（双显修复正确方向，但缺 Codex 的 viewport 下移）。
2. **live dock 在 Static 输出邻域增高/重绘**，Ink 无 `insert_history` 式 scroll region 保护。
3. **stream 只在 live 攒全文**，`assistant_done` 才整段 Static → 过程不可见、结束突然可见。
4. **content-desired** 使 running 初 live=0、随后长高，**高度变化更剧烈**，覆盖更明显。

### 4.2 放大因素

| 因素 | 作用 |
|---|---|
| 双显修复 user→Static | 本轮 user 更早出现在可被盖的 Static 尾 |
| 二期 content-desired | live 从 0→N 变高，覆盖窗口更大 |
| Activity + Header 与 MessageViewport 同 Box | Running 文案与 stream 同区抖动 |
| 无 commit 动画 | 无“边流边落 scrollback” |
| 窄终端 / 长中文 wrap | live 行数涨得更快，更易盖 |

### 4.3 排除

| 假设 | 为何不像主因 |
|---|---|
| bridge 没发 delta | done 后能突然显示全文 → state 里一直有数据 |
| messages 被 clear | partition/state 无 turn 级清空 |
| 仅 CSS/颜色看不见 | 用户描述的是位置/覆盖与“突然出现” |

---

## 5. 问题定义（产品）

### 5.1 目标

1. **running 全程**能看到：本轮 user（或明确在 scrollback 尾）、Running/Thinking 状态、**当前 stream 尾**。
2. 旧多轮 history 正常进 scrollback；推进方式是 **整体上移/viewport 下移**，不是盖住本轮。
3. 输出结束不应“从空白突然蹦出整段”（过程中应已逐步可见）。
4. **不回退** 用户双显修复的正确语义（同一 user 不得 Static+live 双画）。
5. 尽量保持 content-desired（composer 贴内容），不要回到满高 spacer。

### 5.2 非目标（本轮文档）

- 完整移植 Codex commit 动画每一细节
- full mouse 模式重做
- 改 LLM / bridge 协议语义（除非选方案时需要事件序微调）

---

## 6. 修复方案比选

| 方案 | 做法 | 优点 | 风险 | 建议 |
|---|---|---|---|---|
| **P0-A. running 期延后 user 进 Static** | user 先 `done:false` 或单独 hold 在 live，直到 `assistant_done`/`idle` 再标 done 进 Static | 立刻减少“user 被盖”；实现面相对小 | 与双显修复“user 立即 Static”字面冲突，需改成“立即唯一通道=live，结束再 Static”；要防再次双显 | **可作快修** |
| **P0-B. stream 稳定行增量 commit Static** | 仿 Codex：live 只留 tail（如最后 3～8 行）；更早行 `done` 分段或影子 id 写入 Static；每 commit 后 live 变矮 | 过程可见；结束不再“整段弹出” | 需设计分段 id / Static 只追加一致性；测双显 | **主推中期** |
| **P1. live 增高前 scroll / 清区策略** | dock 变高时先对上方做受控 scroll 或固定 live 几何，避免擦 Static 尾 | 治覆盖 | 与 Ink 抢 stdout，易花屏 | 配合 P0 |
| **P2. 真·insert_history + viewport.y** | 移植 Codex scroll region | 根治 | 工作量大 | 长期 |
| **P3. Running 状态移出可变高区** | status 固定 1 行与 MessageViewport 高度解耦 | Running 不闪 | 不单独解决 stream 盖写 | 配套 |
| **不建议** | 回退双显让 user 全程 live 再 Static 双画 | — | 双显回归 | 否 |
| **不建议** | 回退 content-desired 满高 spacer | 盖写可能减轻 | A' 远距离回归 | 否 |

### 6.1 推荐组合（分阶段）

**阶段 1（快，对症“Running 时看不见”）**

1. **running / 有 `!done` assistant 时**：保证 live 至少 N 行（status 占位 + tail），避免 `none` 与突变。
2. **本轮 user 可见性**：
   - 要么 hold user 在 live 直到 turn 结束再 Static（P0-A）；
   - 要么 user Static 后 **禁止** live 区向上侵占 Static 尾（P1 简化：固定 dock 顶在终端底、用 ANSI 预留）。
3. Activity/Running **始终**在 BottomChrome 可见（已有则加固，勿被 MessageViewport 挤没）。

**阶段 2（对齐 Codex 流式体验）**

1. **StreamController 语义**：`liveTranscriptViewportLines` 不仅截尾显示，还要把“离开窗口的行”**标记为可 Static 的 finalized 片段**（或独立 commit 队列）。
2. commit 节奏：按行/按 tick，而不是仅 `assistant_done`。
3. 回归：过程中 stdout 持续出现 assistant 文本；done 后 **不出现第二次整段重复**。

**阶段 3（根治）**

- `insert_history` + `viewport_area.y` 下移（见 composer 诊断 §3.5）。

### 6.2 与双显修复的兼容原则

> **同一逻辑消息只允许进入一个“不可撤销通道”一次。**
> - 若 user 已 Static → live **禁止**再画同一 user。
> - 若 user 暂 live → Static **禁止**在 running 中写入该 user。
> Codex：user 只 InsertHistory 一次；stream tail 只在 active_cell，commit 后再进 history 一次。

---

## 7. 详细设计草案（阶段 1+2 可实施）

### 7.1 分区状态机（修订）

```text
事件 user:
  方案 P0-A: user.done = false（或 finalized=false）→ 仅 active
  方案 现状: user.done = true → 仅 static（保持）+ 必须做 P1/P2

事件 assistant_delta:
  active 含 assistant(!done)
  可选: 当 active 行数 > maxLive 时，把溢出前缀 commit → static 影子消息 / 行缓冲

事件 assistant_done:
  assistant.done = true → 全部进 static
  若 P0-A: 同时 user.done = true
```

### 7.2 live 高度（与 content-desired 共存）

```ts
// 伪代码
if (status running && liveLineCount === 0) {
  // 至少保留 status 友好的 1 行占位，避免“全空 + 只闪 header”
  return { kind: 'live', height: 1 }  // 或 ready 文案 “Thinking…”
}
if (liveLineCount > 0) {
  return { kind: 'live', height: min(liveLineCount, maxLive, room) }
}
// idle + static
return { kind: 'none' }
```

注意：阶段 2 commit 后 `liveLineCount` 应变小，高度回落，**同时** Static 已有前缀，整体像 Codex。

### 7.3 App 渲染

- `MessageViewport` 只渲染 **tail**；
- commit 队列驱动 `<Static items={...}>` 只追加新行；
- **禁止** 在 commit 后仍把已 Static 的文本留在 live。

### 7.4 光标 / 清理

- `liveViewportGeometryRef.rows` 随 content-desired 变；Ctrl+C 清理高度必须用**当前** dock 高（已有测需覆盖 running 中变高）。
- 增高/减高时验证 `clearInlineLiveViewportSequence` 不误清 Static。

---

## 8. 测试计划

### 8.1 自动化（建议新建 `grok_running_visibility*.test.ts`）

| # | 场景 | 期望 |
|---|---|---|
| 1 | `user → running`（尚无 delta） | stdout 仍能匹配本轮 user **或** live 含 user（取决于 P0-A/B）；且含 running/Thinking |
| 2 | 连续 `assistant_delta` | **每个** delta 后合并输出中探针文本出现次数 ≥1（过程可见，不是只在 done 后） |
| 3 | `assistant_done` | 全文在 Static；live 无重复双显 |
| 4 | 长 stream > maxLive | live 仅 tail；前缀已在 Static（阶段 2） |
| 5 | 双显回归 | user 全文出现次数 = 1 |
| 6 | content-desired | idle 无满高 spacer（slot 仍矮） |

### 8.2 人工

1. 多轮后新开一轮：提问 → **立刻**能看到自己的问题 + Running。
2. 流式输出中途截图：应看到正在增长的助手尾，而不是空白。
3. 结束后：内容连续，无“空白很久再弹整段”。
4. 对照 Codex 同终端高度。

---

## 9. 风险与回滚

| 风险 | 缓解 |
|---|---|
| P0-A 再引入双显 | 严格“单通道”；grok 双显测必跑 |
| 增量 commit 行序错乱 | 单测 commit 队列顺序 |
| 与 content-desired 冲突 | 共用以 tail 高为 desired_height |
| 性能 | commit 批量、限制 Static 行频 |

回滚：恢复“仅 assistant_done 进 Static + 当前 partition”；不回退 slash 顺序。

---

## 10. 涉及文件

| 文件 | 角色 |
|---|---|
| `messagePartition.ts` | user/assistant 进出 Static 时机 |
| `state.ts` | user.done / 分段 finalize 字段 |
| `messageViewportPlan.ts` | running 空 live 占位；与 commit 后高度 |
| `messageWindow.ts` | `liveTranscriptViewportLines` + 未来 commit 切分 |
| `App.tsx` | Static items 来源、live 渲染、geometry |
| `activityStatus.ts` / BottomChrome | Running 可见性 |
| Codex `streaming.rs` `sync_active_stream_tail` | tail + commit 参考 |
| Codex `insert_history.rs` | viewport 下移参考 |

---

## 11. 与既有文档关系

| 文档 | 关系 |
|---|---|
| 用户双显 | 修对了“user 双通道”；**未**处理 Static 后 live 盖写 |
| 输入框锚点 / content-desired | 修对了“离内容太远”；**加剧** live 高度从 0 猛涨的盖写 |
| 本文 | 专治 **Running 过程不可见 / 结束才弹出** |

三者需 **一起回归**，避免修 A 伤 B。

---

## 12. 总结

- **现象**：Running 后本轮 user / 状态 / 流式像被向上盖住，结束才突然全显示。
- **机制**：Static 早写本轮 user + live dock 原地增高重绘 + stream 延迟整段 Static；缺 Codex 的 **InsertHistory 下移 viewport** 与 **边流边 commit**。
- **Codex**：user/history 进 scrollback 并可能 `area.y +=`；stream **active_cell 尾** 在 dock；稳定行 commit 动画进 history。
- **修法**：阶段 1 保证 running 可见（P0-A 或 P1）；阶段 2 增量 commit + live tail；阶段 3 真 viewport 引擎。
- **原则**：单通道、过程可见、结束无二次双显、保持 content-desired 贴内容。

本文 **仅诊断与方案**，不改代码。实施时 TDD，并与双显 / composer 自测一并跑。
